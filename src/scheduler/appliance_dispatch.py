"""Smart appliance scheduling — heartbeat-free, LP-solve-driven.

Per LP solve, :func:`reconcile` queries SmartThings for each enabled
appliance's ``remoteControlEnabled`` flag and:

* arms a new ``appliance_jobs`` row (status='scheduled') + registers an
  APScheduler one-shot ``DateTrigger`` cron when remote_mode goes true,
* re-plans (replace_existing=True) when remote_mode stays true but the
  cheapest window shifts (e.g. fresh Octopus rates landed),
* cancels (drops the cron, marks 'cancelled') when remote_mode goes false
  while a session is still in 'scheduled' state.

The cron itself, :func:`_fire_cron`, re-checks remote_mode as a pre-fire
safety, then issues ``washerOperatingState.setMachineState run`` on
SmartThings — honouring the ``OPENCLAW_READ_ONLY`` global kill switch.

The LP solve picks up the planned load via :func:`appliance_load_profile_kw`,
which contributes to ``residual_load_kw`` on every slot covered by an
'scheduled' or 'running' job.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..notifier import notify_risk
from ..smartthings.client import SmartThingsError
from ..smartthings.service import get_client as _get_st_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-process error counters (transient API health signal). Reset on success.
# ---------------------------------------------------------------------------
_reconcile_errors: dict[int, int] = {}
_pat_invalid_notified: bool = False


def _record_reconcile_error(appliance_id: int, err: Exception) -> None:
    """Bump the consecutive-error counter for this appliance. Pings via
    notify_risk on the threshold tick (and only the threshold tick) so we
    don't spam the user during an outage."""
    global _pat_invalid_notified
    n = _reconcile_errors.get(appliance_id, 0) + 1
    _reconcile_errors[appliance_id] = n
    threshold = int(config.APPLIANCE_RECONCILE_ERROR_PING_THRESHOLD)

    is_pat_invalid = (
        isinstance(err, SmartThingsError) and err.code == "pat_invalid"
    )
    if is_pat_invalid:
        if not _pat_invalid_notified:
            notify_risk(
                f"SmartThings PAT rejected (HTTP 401). Appliance dispatch is paused. "
                f"Re-set the token via /api/v1/integrations/smartthings/credentials.",
                extra={"appliance_id": appliance_id, "code": "pat_invalid"},
            )
            _pat_invalid_notified = True
        # PAT-invalid is a single-ping condition — skip the threshold path.
        return

    if n == threshold:
        notify_risk(
            f"SmartThings reconcile failed {n} times in a row for appliance #{appliance_id}: {err}",
            extra={"appliance_id": appliance_id, "consecutive_errors": n},
        )


def _record_reconcile_success(appliance_id: int) -> None:
    global _pat_invalid_notified
    if appliance_id in _reconcile_errors:
        del _reconcile_errors[appliance_id]
    _pat_invalid_notified = False


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    """Canonical 'YYYY-MM-DDTHH:MM:SSZ' UTC ISO."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _next_deadline_utc(deadline_local_hhmm: str, *, now: datetime | None = None) -> datetime:
    """Return the next occurrence of ``HH:MM`` in BULLETPROOF_TIMEZONE,
    converted to UTC. If today's deadline already passed, returns tomorrow's.
    """
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now_local = (now or _now_utc()).astimezone(tz)
    h, m = _parse_hhmm(deadline_local_hhmm)
    candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now_local:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(UTC)


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = (s or "").strip().split(":")
    h = int(parts[0]) if parts and parts[0] else 0
    m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    return max(0, min(23, h)), max(0, min(59, m))


def _ceil_to_half_hour_utc(dt: datetime) -> datetime:
    base = dt.replace(second=0, microsecond=0)
    if dt.minute < 30 and (dt.minute > 0 or dt.second > 0 or dt.microsecond > 0):
        return base.replace(minute=30)
    if dt.minute >= 30 and (dt.minute > 30 or dt.second > 0 or dt.microsecond > 0):
        return (base + timedelta(hours=1)).replace(minute=0)
    return base


# ---------------------------------------------------------------------------
# Cheapest-window picker
# ---------------------------------------------------------------------------

def build_marginal_cost_per_slot(
    earliest_start_utc: datetime,
    deadline_utc: datetime,
    appliance_kw: float,
) -> dict[datetime, float] | None:
    """Build a per-slot marginal-cost map for running the appliance in that slot.

    The dispatcher uses this to **price PV opportunity correctly**: running the
    washer when PV is exporting costs us the *forgone export revenue*, not the
    *Agile import price* (which we wouldn't pay anyway because PV covers the
    load). Without this, ``find_cheapest_window`` picks the cheapest Agile slot
    even when daytime PV is genuinely cheaper.

    Per-slot marginal cost (pence) for ``washer_kwh = appliance_kw × 0.5``::

        residual_pv = max(0, pv_forecast_kwh - base_load_kwh)
        if residual_pv >= washer_kwh:
            cost = washer_kwh × export_rate    # we forgo export revenue
        else:
            cost = (washer_kwh - residual_pv) × import_rate
                 + residual_pv × export_rate

    Returns ``None`` when forecasts are unavailable (caller falls back to the
    legacy import-only path). Forecast inputs:

      - PV: ``weather.fetch_forecast`` × hourly calibration table
      - Base load: ``db.half_hourly_residual_load_profile_kwh()`` (post-Daikin)
      - Import rate: ``agile_rates`` for the period
      - Export rate: ``agile_export_rates`` (or flat ``EXPORT_RATE_PENCE`` fallback)
    """
    if not getattr(config, "APPLIANCE_PV_AWARE_DISPATCH", True):
        return None
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return None
    earliest_start_utc = _ceil_to_half_hour_utc(earliest_start_utc)

    # Import rates
    try:
        import_rows = db.get_rates_for_period(tariff, earliest_start_utc, deadline_utc)
    except Exception as e:
        logger.warning("appliance pv-aware: get_rates_for_period failed: %s", e)
        return None
    if not import_rows:
        return None
    import_by_start: dict[datetime, float] = {}
    for r in import_rows:
        try:
            import_by_start[_parse_iso(r["valid_from"])] = float(r["value_inc_vat"])
        except (KeyError, ValueError, TypeError):
            continue
    if not import_by_start:
        return None

    # Export rates (best-effort; fall back to flat EXPORT_RATE_PENCE per slot)
    flat_export = float(config.EXPORT_RATE_PENCE)
    export_by_start: dict[datetime, float] = {}
    if (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip():
        try:
            export_rows = db.get_agile_export_rates_in_range(
                earliest_start_utc.isoformat(), deadline_utc.isoformat(),
            )
            for r in export_rows:
                export_by_start[_parse_iso(r["valid_from"])] = float(r["value_inc_vat"])
        except Exception as e:
            logger.warning("appliance pv-aware: export rates fetch failed: %s", e)

    # PV forecast (kWh per slot)
    pv_per_slot = _residual_pv_kwh_per_slot(
        sorted(import_by_start.keys()),
    )

    # Base-load profile (kWh/half-hour by hour-of-day)
    try:
        base_load_profile = db.half_hourly_residual_load_profile_kwh()
    except Exception as e:
        logger.warning("appliance pv-aware: load profile fetch failed: %s", e)
        base_load_profile = {}
    base_load_flat = float(getattr(config, "APPLIANCE_DEFAULT_BASE_LOAD_KW", 0.4))

    washer_kwh_per_slot = float(appliance_kw) * 0.5

    out: dict[datetime, float] = {}
    tz_name = config.BULLETPROOF_TIMEZONE
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    for slot_start in sorted(import_by_start.keys()):
        if slot_start < earliest_start_utc:
            continue
        if slot_start + timedelta(minutes=30) > deadline_utc:
            break
        local = slot_start.astimezone(tz)
        bucket = (local.hour, 30 if local.minute >= 30 else 0)
        base_load = base_load_profile.get(bucket, base_load_flat)
        pv = pv_per_slot.get(slot_start, 0.0)
        residual_pv = max(0.0, pv - base_load)
        import_p = import_by_start[slot_start]
        export_p = export_by_start.get(slot_start, flat_export)
        if residual_pv >= washer_kwh_per_slot:
            cost = washer_kwh_per_slot * export_p
        else:
            grid_share = washer_kwh_per_slot - residual_pv
            cost = grid_share * import_p + residual_pv * export_p
        out[slot_start] = cost

    return out if out else None


def _residual_pv_kwh_per_slot(
    slot_starts_utc: list[datetime],
) -> dict[datetime, float]:
    """Return ``{slot_start_utc: pv_kwh}`` per half-hour using the Open-Meteo
    forecast + per-hour-of-day calibration table the LP uses.

    Empty dict when the forecast fetch fails — caller's marginal-cost path
    treats missing slots as ``pv=0`` (purely-import cost = legacy behavior).
    """
    if not slot_starts_utc:
        return {}
    horizon_h = max(
        4,
        int(
            (slot_starts_utc[-1] - slot_starts_utc[0]).total_seconds() // 3600 + 2,
        ),
    )
    try:
        from ..weather import (
            compute_pv_calibration_factor,
            compute_today_pv_correction_factor,
            fetch_forecast,
            get_pv_calibration_factor_for,
        )

        forecast = fetch_forecast(hours=horizon_h)
    except Exception as e:
        logger.warning("appliance pv-aware: fetch_forecast failed: %s", e)
        return {}
    if not forecast:
        return {}

    cal_cloud: dict[tuple[int, int], float] = {}
    cal_hourly: dict[int, float] = {}
    try:
        cal_cloud = db.get_pv_calibration_hourly_cloud()
        cal_hourly = db.get_pv_calibration_hourly()
    except Exception:
        cal_cloud, cal_hourly = {}, {}
    # Today-aware adjuster on top of per-hour table (or flat fallback).
    # Same logic the LP uses (see optimizer.py) — keeps appliance dispatch's
    # PV view consistent with the LP's PV view per solve.
    today_factor = 1.0
    try:
        today_factor, _ = compute_today_pv_correction_factor()
    except Exception:
        pass
    flat_cal = 1.0
    if not cal_hourly and not cal_cloud:
        try:
            flat_cal = float(compute_pv_calibration_factor() or 1.0)
        except Exception:
            flat_cal = 1.0

    # forecast is hourly; expand to half-hourly by halving the hourly kWh
    # estimate (good enough for this dispatch decision).
    by_hour: dict[datetime, tuple[float, float | None]] = {
        f.time_utc: (float(f.estimated_pv_kw), float(f.cloud_cover_pct))
        for f in forecast
    }
    out: dict[datetime, float] = {}
    for slot_start in slot_starts_utc:
        hour_anchor = slot_start.replace(minute=0, second=0, microsecond=0)
        pv_kw, cloud_pct = by_hour.get(hour_anchor, (0.0, None))
        scale = get_pv_calibration_factor_for(
            hour_anchor.hour, cloud_pct,
            cloud_table=cal_cloud, hourly_table=cal_hourly, flat=flat_cal,
        )
        # Half-hour kWh = kW × 0.5h × calibration × today-aware factor
        out[slot_start] = pv_kw * 0.5 * scale * today_factor
    return out


def find_cheapest_window(
    earliest_start_utc: datetime,
    deadline_utc: datetime,
    duration_minutes: int,
    *,
    marginal_cost_per_slot: dict[datetime, float] | None = None,
) -> tuple[datetime, datetime, float]:
    """Return ``(planned_start_utc, planned_end_utc, avg_price_pence)`` for the
    cheapest contiguous window of ``duration_minutes`` whose end ≤ ``deadline_utc``.

    When ``marginal_cost_per_slot`` is supplied (PV-aware dispatch — see
    :func:`build_marginal_cost_per_slot`), the chosen window minimises total
    marginal cost (= forgone export + grid import for the appliance), not
    raw Agile import price. The reported ``avg_price_pence`` then reflects
    pence/kWh of *real opportunity cost* per slot.

    When ``marginal_cost_per_slot`` is ``None`` (legacy / no forecasts), the
    function falls back to sliding-window minimum on Agile import rates from
    the ``agile_rates`` table. Cold-start fallback uses
    ``APPLIANCE_FALLBACK_WINDOW_LOCAL``.
    """
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    duration = max(30, int(duration_minutes))
    earliest_start_utc = _ceil_to_half_hour_utc(earliest_start_utc)
    if deadline_utc - earliest_start_utc < timedelta(minutes=duration):
        raise ValueError(
            f"deadline_utc {deadline_utc.isoformat()} is < {duration}min after "
            f"earliest_start_utc {earliest_start_utc.isoformat()}"
        )

    if marginal_cost_per_slot:
        result = _cheapest_from_marginal_cost(
            marginal_cost_per_slot, earliest_start_utc, deadline_utc, duration,
        )
        if result is not None:
            return result
        # fall through to import-only when marginal-cost path can't fit a window

    rates: list[dict[str, Any]] = []
    if tariff:
        try:
            rates = db.get_rates_for_period(tariff, earliest_start_utc, deadline_utc)
        except Exception as e:
            logger.warning("appliance: get_rates_for_period failed: %s", e)
            rates = []

    if rates:
        return _cheapest_from_rates(rates, deadline_utc, duration)

    return _fallback_window(earliest_start_utc, deadline_utc, duration)


def _cheapest_from_marginal_cost(
    marginal_cost_per_slot: dict[datetime, float],
    earliest_start_utc: datetime,
    deadline_utc: datetime,
    duration_minutes: int,
) -> tuple[datetime, datetime, float] | None:
    """Sliding-window minimum on the marginal-cost map. Returns ``None`` when
    no contiguous window of ``duration_minutes`` fits before ``deadline_utc``."""
    n_slots = max(1, duration_minutes // 30)
    starts = sorted(s for s in marginal_cost_per_slot if s >= earliest_start_utc)
    if len(starts) < n_slots:
        return None

    best_total: float | None = None
    best_start: datetime | None = None
    for i in range(len(starts) - n_slots + 1):
        window = starts[i : i + n_slots]
        # Contiguity check
        contiguous = all(
            window[j + 1] - window[j] == timedelta(minutes=30)
            for j in range(n_slots - 1)
        )
        if not contiguous:
            continue
        end_utc = window[-1] + timedelta(minutes=30)
        if end_utc > deadline_utc:
            continue
        total = sum(marginal_cost_per_slot[s] for s in window)
        if best_total is None or total < best_total:
            best_total = total
            best_start = window[0]

    if best_start is None or best_total is None:
        return None
    end_utc = best_start + timedelta(minutes=duration_minutes)
    # Convert total pence → avg pence/kWh for back-compat (callers expect it)
    # We don't know exact kWh here, so report total/n_slots as "p per slot avg".
    avg_per_slot_p = best_total / float(n_slots)
    return best_start, end_utc, avg_per_slot_p


def _cheapest_from_rates(
    rates: list[dict[str, Any]],
    deadline_utc: datetime,
    duration_minutes: int,
) -> tuple[datetime, datetime, float]:
    """Sliding-window minimum mean-price over a sorted list of half-hour rates.

    Each rate row has ``valid_from`` (ISO UTC), ``valid_to`` (ISO UTC),
    ``value_inc_vat`` (pence/kWh).
    """
    n_slots = max(1, duration_minutes // 30)
    parsed: list[tuple[datetime, datetime, float]] = []
    for r in rates:
        try:
            vf = _parse_iso(r["valid_from"])
            vt = _parse_iso(r["valid_to"])
            v = float(r["value_inc_vat"])
        except (KeyError, ValueError, TypeError):
            continue
        parsed.append((vf, vt, v))
    parsed.sort(key=lambda t: t[0])

    best_avg: float | None = None
    best_start: datetime | None = None
    best_end: datetime | None = None

    for i in range(len(parsed) - n_slots + 1):
        window = parsed[i : i + n_slots]
        # Require contiguity (each slot's end == next slot's start)
        contiguous = all(
            window[j][1] == window[j + 1][0] for j in range(n_slots - 1)
        )
        if not contiguous:
            continue
        end_utc = window[-1][1]
        if end_utc > deadline_utc:
            continue
        mean_p = sum(w[2] for w in window) / float(n_slots)
        if best_avg is None or mean_p < best_avg:
            best_avg = mean_p
            best_start = window[0][0]
            best_end = end_utc

    if best_start is None or best_end is None or best_avg is None:
        # No contiguous window fits before the deadline: fall back.
        return _fallback_window(parsed[0][0] if parsed else _now_utc(),
                                deadline_utc, duration_minutes)
    return best_start, best_end, float(best_avg)


def _fallback_window(
    earliest_start_utc: datetime,
    deadline_utc: datetime,
    duration_minutes: int,
) -> tuple[datetime, datetime, float]:
    """Use ``APPLIANCE_FALLBACK_WINDOW_LOCAL`` (HH:MM-HH:MM in local TZ) as
    the planned window. Returns ``avg_price_pence=0.0`` (unknown).
    """
    raw = (config.APPLIANCE_FALLBACK_WINDOW_LOCAL or "02:00-05:00").strip()
    try:
        from_str, to_str = raw.split("-")
        h1, m1 = _parse_hhmm(from_str)
        h2, m2 = _parse_hhmm(to_str)
    except ValueError:
        h1, m1, h2, m2 = 2, 0, 5, 0

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    deadline_local = deadline_utc.astimezone(tz)
    # Anchor to the local day before the deadline so the window ends ≤ deadline.
    candidate_start = deadline_local.replace(
        hour=h1, minute=m1, second=0, microsecond=0
    )
    if candidate_start >= deadline_local:
        candidate_start = candidate_start - timedelta(days=1)
    end_local = candidate_start + timedelta(minutes=duration_minutes)

    # If the configured fallback window is shorter than the duration, just
    # use the start anchor + duration anyway (the LP residual-load profile
    # absorbs it cleanly even if it's not within the fallback "preferred"
    # window).
    start_utc = candidate_start.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)
    if end_utc > deadline_utc:
        end_utc = deadline_utc
        start_utc = end_utc - timedelta(minutes=duration_minutes)
    if start_utc < earliest_start_utc:
        start_utc = _ceil_to_half_hour_utc(earliest_start_utc)
        end_utc = start_utc + timedelta(minutes=duration_minutes)
    return start_utc, end_utc, 0.0


# ---------------------------------------------------------------------------
# Reconcile (called from the LP solve)
# ---------------------------------------------------------------------------

def reconcile() -> None:
    """One-shot pass over enabled appliances. Called BEFORE each LP solve.

    Reads the current SmartThings ``remoteControlEnabled`` for each appliance
    and arms / cancels / re-plans accordingly. Never raises — failures are
    logged + counted; callers see best-effort behaviour.

    Also polls running jobs for cycle completion (PR #234) — single SmartThings
    call per running job, fires ``notify_appliance_finished`` on transition
    out of ``run``/``pause``.
    """
    if not config.APPLIANCE_DISPATCH_ENABLED:
        return
    try:
        appliances = db.list_appliances(enabled_only=True)
    except Exception:
        logger.exception("appliance reconcile: list_appliances failed")
        return
    for appliance in appliances:
        if appliance.get("vendor") != "smartthings":
            continue
        try:
            _reconcile_one(appliance)
        except Exception:
            logger.exception(
                "appliance reconcile: _reconcile_one failed for #%s",
                appliance.get("id"),
            )
    # PR #234: cycle-completion poll. Runs after the arm/cancel pass so we
    # don't re-fire a notification for a job that just got armed.
    try:
        _poll_running_jobs()
    except Exception:
        logger.exception("appliance reconcile: _poll_running_jobs failed (non-fatal)")


def _poll_running_jobs() -> None:
    """Detect cycle completion on every running job and fire the finished hook.

    A job stays in ``status='running'`` until SmartThings reports the unit
    has left ``run``/``pause``. On detection: mark ``status='completed'``,
    stamp ``completed_at_utc``, and dispatch one ``APPLIANCE_FINISHED``
    notification (idempotent — DB update is the dedup key).

    Best-effort: any error short-circuits this single job, never blocks
    the broader reconcile pass.
    """
    try:
        running_jobs = db.get_appliance_jobs(status="running", limit=20)
    except Exception:
        logger.exception("poll_running: list jobs failed")
        return
    if not running_jobs:
        return

    try:
        client = _get_st_client()
    except SmartThingsError as e:
        logger.debug("poll_running: SmartThings client unavailable (%s) — skipping pass", e.code)
        return

    for job in running_jobs:
        try:
            appliance = db.get_appliance(int(job["appliance_id"]))
            if appliance is None:
                continue
            state = client.get_machine_state(appliance["vendor_device_id"])
        except SmartThingsError as e:
            logger.debug(
                "poll_running: machine_state lookup failed for job %s (%s) — retry next pass",
                job.get("id"), e.code,
            )
            continue

        if state in (None, "run", "pause"):
            # Still running (or capability unavailable — don't guess).
            continue

        # Transitioned out → mark complete and notify.
        ended_at = _iso(_now_utc())
        try:
            db.update_appliance_job(
                int(job["id"]), status="completed", completed_at_utc=ended_at,
            )
        except Exception:
            logger.exception("poll_running: DB update_appliance_job failed for %s", job.get("id"))
            continue

        logger.info(
            "appliance complete: job %s state=%s, marking completed",
            job.get("id"), state,
        )

        # Build the finished notification (best-effort)
        try:
            from ..analytics.daily_brief import build_brief_48h_summary
            from ..notifier import notify_appliance_finished
            tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
            actual_start = job.get("actual_start_utc") or job.get("planned_start_utc")
            started_local_str = "—"
            duration_min = int(job.get("duration_minutes") or 0)
            if actual_start:
                start_dt = datetime.fromisoformat(
                    str(actual_start).replace("Z", "+00:00")
                ).astimezone(tz)
                started_local_str = start_dt.strftime("%H:%M")
                end_dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00")).astimezone(tz)
                duration_min = max(1, int((end_dt - start_dt).total_seconds() // 60))
            ended_local_dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00")).astimezone(tz)
            avg_p = job.get("avg_price_pence")
            est_kwh = None
            est_cost_p = None
            try:
                typical_kw = float(appliance.get("typical_kw") or 0.0)
                if typical_kw > 0 and duration_min > 0:
                    est_kwh = typical_kw * (duration_min / 60.0)
                    if avg_p is not None:
                        est_cost_p = est_kwh * float(avg_p)
            except (TypeError, ValueError):
                pass
            notify_appliance_finished(
                appliance_name=str(
                    appliance.get("name") or appliance.get("device_type") or "Appliance"
                ),
                started_local=started_local_str,
                ended_local=ended_local_dt.strftime("%H:%M"),
                duration_minutes=duration_min,
                avg_price_pence=float(avg_p) if avg_p is not None else None,
                estimated_kwh=est_kwh,
                estimated_cost_p=est_cost_p,
                brief_md=build_brief_48h_summary(),
            )
        except Exception:
            logger.exception(
                "poll_running: finished-notification failed for job %s (non-fatal)",
                job.get("id"),
            )


def _reconcile_one(appliance: dict[str, Any]) -> None:
    appliance_id = int(appliance["id"])
    try:
        client = _get_st_client()
    except SmartThingsError as e:
        # PAT not configured — only ping if the user previously had an
        # active job (otherwise they just haven't configured it yet).
        if e.code == "pat_missing":
            logger.debug(
                "appliance reconcile: PAT not configured (#%d): %s",
                appliance_id, e,
            )
            return
        _record_reconcile_error(appliance_id, e)
        return

    try:
        remote_mode = client.get_remote_control_enabled(appliance["vendor_device_id"])
    except SmartThingsError as e:
        _record_reconcile_error(appliance_id, e)
        return
    _record_reconcile_success(appliance_id)

    job = db.get_active_appliance_job(appliance_id)
    job_status = job.get("status") if job else None

    if remote_mode and job_status != "running":
        _arm_or_replan(appliance, job)
    elif (not remote_mode) and job and job_status == "scheduled":
        _cancel(appliance_id, job, reason="remote_mode_dropped")
    # else: running → leave alone; or no job + remote_mode false → no-op.


def _arm_or_replan(
    appliance: dict[str, Any],
    existing_job: dict[str, Any] | None,
) -> None:
    appliance_id = int(appliance["id"])
    deadline_local = appliance.get("deadline_local_time") or config.APPLIANCE_DEFAULT_DEADLINE_LOCAL
    deadline_utc = _next_deadline_utc(deadline_local)
    now = _now_utc()

    # Cycle-aware duration: when the washer exposes
    # ``samsungce.washerDelayEnd.minimumReservableTime``, prefer it over the
    # static ``default_duration_minutes`` from registration. The live value
    # reflects the user's actual cycle selection (eco vs cotton vs hot wash);
    # the static default is a guess. Falls back gracefully when the capability
    # is absent or the value is null/zero.
    static_duration = int(appliance.get("default_duration_minutes") or 120)
    duration = static_duration
    try:
        client = _get_st_client()
        live_status = client.get_full_status(appliance["vendor_device_id"])
        delay = live_status.get("components", {}).get("main", {}).get(
            "samsungce.washerDelayEnd", {}
        )
        live_min_reservable = delay.get("minimumReservableTime", {}).get("value")
        if isinstance(live_min_reservable, (int, float)) and live_min_reservable >= 30:
            duration = int(live_min_reservable)
            if duration != static_duration:
                logger.info(
                    "appliance #%d: cycle-aware duration %d min "
                    "(static default was %d min)",
                    appliance_id, duration, static_duration,
                )
    except Exception as e:  # noqa: BLE001 — fall back to static, never fatal
        logger.debug(
            "appliance #%d: cycle-aware duration read failed (%s) — using static %d min",
            appliance_id, type(e).__name__, static_duration,
        )

    # Reject if there isn't enough headroom before the deadline.
    if deadline_utc - now < timedelta(minutes=duration):
        if existing_job is not None:
            logger.info(
                "appliance #%d: deadline %s within duration window — leaving "
                "existing job #%d alone",
                appliance_id, deadline_utc.isoformat(), existing_job["id"],
            )
        else:
            logger.info(
                "appliance #%d: deadline %s within duration window — skipping arm",
                appliance_id, deadline_utc.isoformat(),
            )
        return

    appliance_kw = float(appliance.get("typical_kw") or 0.5)
    marginal = build_marginal_cost_per_slot(now, deadline_utc, appliance_kw)
    if marginal:
        logger.info(
            "appliance #%d: PV-aware dispatch (%d candidate slots, "
            "marginal-cost range %.2f-%.2f pence)",
            appliance_id, len(marginal),
            min(marginal.values()), max(marginal.values()),
        )

    try:
        start_utc, end_utc, avg_price = find_cheapest_window(
            now, deadline_utc, duration,
            marginal_cost_per_slot=marginal,
        )
    except ValueError as e:
        logger.warning("appliance #%d: cheapest-window infeasible: %s", appliance_id, e)
        return

    if existing_job is None:
        try:
            job_id = db.create_appliance_job(
                appliance_id=appliance_id,
                armed_at_utc=_iso(now),
                deadline_utc=_iso(deadline_utc),
                duration_minutes=duration,
                planned_start_utc=_iso(start_utc),
                planned_end_utc=_iso(end_utc),
                avg_price_pence=avg_price,
                last_replan_at_utc=_iso(now),
                status="scheduled",
            )
        except Exception:
            logger.exception("appliance #%d: failed to create job row", appliance_id)
            return
        _register_cron(appliance_id, job_id, start_utc)
        logger.info(
            "appliance #%d armed: job=%d planned_start=%s avg_price=%.2fp",
            appliance_id, job_id, start_utc.isoformat(), avg_price,
        )
        return

    # Re-plan: only touch the cron if the slot actually shifted.
    if existing_job["planned_start_utc"] == _iso(start_utc):
        db.update_appliance_job(
            existing_job["id"],
            last_replan_at_utc=_iso(now),
        )
        return

    db.update_appliance_job(
        existing_job["id"],
        planned_start_utc=_iso(start_utc),
        planned_end_utc=_iso(end_utc),
        avg_price_pence=avg_price,
        last_replan_at_utc=_iso(now),
    )
    _register_cron(appliance_id, int(existing_job["id"]), start_utc)
    logger.info(
        "appliance #%d re-planned: job=%d new_start=%s avg_price=%.2fp",
        appliance_id, existing_job["id"], start_utc.isoformat(), avg_price,
    )


def _cancel(appliance_id: int, job: dict[str, Any], *, reason: str) -> None:
    db.update_appliance_job(int(job["id"]), status="cancelled", error_msg=reason)
    _remove_cron(appliance_id)
    logger.info(
        "appliance #%d job=%d cancelled (reason=%s)",
        appliance_id, job["id"], reason,
    )


# ---------------------------------------------------------------------------
# APScheduler integration
# ---------------------------------------------------------------------------

def _cron_id(appliance_id: int) -> str:
    return f"appliance_fire_{int(appliance_id)}"


def _register_cron(appliance_id: int, job_id: int, run_at_utc: datetime) -> None:
    """Register or replace the one-shot DateTrigger for this appliance."""
    from .runner import get_background_scheduler
    scheduler = get_background_scheduler()
    if scheduler is None:
        logger.warning(
            "appliance #%d: APScheduler not running; cron not registered "
            "(rehydrate_crons will pick this up after start_background_scheduler)",
            appliance_id,
        )
        return
    try:
        from apscheduler.triggers.date import DateTrigger
    except ImportError:
        logger.warning("APScheduler not installed — appliance cron disabled")
        return
    try:
        scheduler.add_job(
            _fire_cron,
            DateTrigger(run_date=run_at_utc),
            id=_cron_id(appliance_id),
            replace_existing=True,
            args=[int(job_id)],
        )
    except Exception:
        logger.exception(
            "appliance #%d: APScheduler add_job failed", appliance_id
        )


def _remove_cron(appliance_id: int) -> None:
    from .runner import get_background_scheduler
    scheduler = get_background_scheduler()
    if scheduler is None:
        return
    try:
        scheduler.remove_job(_cron_id(appliance_id))
    except Exception:
        # JobLookupError or generic — both fine; the job is already gone.
        return


def _fire_cron(job_id: int) -> None:
    """APScheduler DateTrigger callback — the one-shot fire path.

    Re-checks remote_mode, then issues setMachineState run on SmartThings.
    Honours OPENCLAW_READ_ONLY (the start_cycle method itself does too;
    we mark the row 'skipped_readonly' here so the audit trail is clear).
    """
    try:
        job = db.get_appliance_job(int(job_id))
    except Exception:
        logger.exception("appliance _fire_cron: get_appliance_job failed")
        return
    if job is None:
        logger.info("appliance fire: job %d not found (cancelled)", job_id)
        return
    if job["status"] != "scheduled":
        logger.info(
            "appliance fire: job %d in status %s (no-op)", job_id, job["status"]
        )
        return

    appliance = db.get_appliance(int(job["appliance_id"]))
    if appliance is None:
        db.update_appliance_job(int(job_id), status="failed",
                                error_msg="appliance row deleted")
        return

    actual_start = _iso(_now_utc())

    if config.OPENCLAW_READ_ONLY:
        db.update_appliance_job(
            int(job_id), status="skipped_readonly", actual_start_utc=actual_start,
        )
        logger.info(
            "appliance fire: job %d skipped (OPENCLAW_READ_ONLY=true)", job_id
        )
        return

    # Pre-fire safety: reread remote_mode. If we can't confirm it's still
    # in remote-start mode, do NOT fire — better to no-op than send a start
    # to a unit the user has since cancelled.
    try:
        client = _get_st_client()
    except SmartThingsError as e:
        db.update_appliance_job(
            int(job_id), status="failed",
            error_msg=f"safety_check_failed:{e.code}",
            actual_start_utc=actual_start,
        )
        notify_risk(
            f"Wash didn't fire — SmartThings PAT unavailable ({e.code}).",
            extra={"job_id": int(job_id), "code": e.code},
        )
        return
    try:
        if not client.get_remote_control_enabled(appliance["vendor_device_id"]):
            db.update_appliance_job(
                int(job_id), status="cancelled",
                error_msg="remote_mode_dropped_at_fire",
                actual_start_utc=actual_start,
            )
            notify_risk(
                "Wash didn't fire — washer left remote-start mode just before the planned start.",
                extra={"job_id": int(job_id)},
            )
            return
    except SmartThingsError as e:
        db.update_appliance_job(
            int(job_id), status="failed",
            error_msg=f"safety_check_failed:{e.code}",
            actual_start_utc=actual_start,
        )
        notify_risk(
            f"Wash didn't fire — couldn't verify remote-start state ({e.code}).",
            extra={"job_id": int(job_id), "code": e.code},
        )
        return

    try:
        client.start_cycle(appliance["vendor_device_id"])
    except SmartThingsError as e:
        db.update_appliance_job(
            int(job_id), status="failed", error_msg=str(e),
            actual_start_utc=actual_start,
        )
        notify_risk(
            f"Wash failed to start: {e}.",
            extra={"job_id": int(job_id), "code": e.code},
        )
        return

    db.update_appliance_job(
        int(job_id), status="running", actual_start_utc=actual_start,
    )
    logger.info("appliance fire: job %d → running", job_id)

    # PR #234: notify the family that laundry is starting + inline forward brief.
    # Best-effort — never let a notification failure block the dispatch path.
    try:
        from ..analytics.daily_brief import build_brief_48h_summary
        from ..notifier import notify_appliance_starting
        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        planned_start = datetime.fromisoformat(
            str(job["planned_start_utc"]).replace("Z", "+00:00")
        ).astimezone(tz)
        deadline = datetime.fromisoformat(
            str(job["deadline_utc"]).replace("Z", "+00:00")
        ).astimezone(tz)
        notify_appliance_starting(
            appliance_name=str(appliance.get("name") or appliance.get("device_type") or "Appliance"),
            planned_start_local=planned_start.strftime("%a %H:%M"),
            deadline_local=deadline.strftime("%a %H:%M"),
            avg_price_pence=float(job.get("avg_price_pence") or 0.0),
            duration_minutes=int(job.get("duration_minutes") or 0),
            brief_md=build_brief_48h_summary(),
        )
    except Exception:
        logger.exception("appliance fire: starting-notification failed (non-fatal)")


# ---------------------------------------------------------------------------
# LP-solve hook: residual-load contribution
# ---------------------------------------------------------------------------

def appliance_load_profile_kw(
    start_utc: datetime, end_utc: datetime
) -> dict[datetime, float]:
    """Return ``{slot_start_utc: kw}`` for every armed/running appliance job
    overlapping the half-hour grid in ``[start_utc, end_utc)``.

    Each session contributes ``appliance.typical_kw`` to every half-hour slot
    its [planned_start_utc, planned_end_utc) covers. When multiple sessions
    overlap a slot, contributions are summed.
    """
    if not config.APPLIANCE_DISPATCH_ENABLED:
        return {}
    try:
        rows = db.get_active_appliance_jobs_overlapping(
            from_utc=_iso(start_utc), to_utc=_iso(end_utc),
        )
    except Exception:
        logger.exception("appliance_load_profile_kw: query failed")
        return {}

    profile: dict[datetime, float] = {}
    for row in rows:
        try:
            ps = _parse_iso(row["planned_start_utc"])
            pe = _parse_iso(row["planned_end_utc"])
        except (KeyError, ValueError):
            continue
        kw = float(row.get("appliance_typical_kw") or 0.0)
        if kw <= 0:
            continue
        # Snap to half-hour grid and walk slots.
        slot = ps.replace(second=0, microsecond=0)
        if slot.minute < 30:
            slot = slot.replace(minute=0)
        else:
            slot = slot.replace(minute=30)
        while slot < pe and slot < end_utc:
            if slot >= start_utc:
                profile[slot] = profile.get(slot, 0.0) + kw
            slot = slot + timedelta(minutes=30)
    return profile


# ---------------------------------------------------------------------------
# Boot-time rehydration of in-flight crons
# ---------------------------------------------------------------------------

def rehydrate_crons() -> dict[str, int]:
    """Re-register APScheduler crons for every 'scheduled' job whose
    planned_start_utc is in the future. Mark already-passed jobs as 'expired'.

    Returns a small summary dict for logging / observability.
    """
    if not config.APPLIANCE_DISPATCH_ENABLED:
        return {"registered": 0, "expired": 0, "skipped": 0}
    summary = {"registered": 0, "expired": 0, "skipped": 0}
    now = _now_utc()
    try:
        rows = db.get_appliance_jobs(status="scheduled", limit=1000)
    except Exception:
        logger.exception("rehydrate_crons: query failed")
        return summary

    for row in rows:
        try:
            planned_start = _parse_iso(row["planned_start_utc"])
        except (KeyError, ValueError):
            summary["skipped"] += 1
            continue
        if planned_start <= now:
            db.update_appliance_job(
                int(row["id"]),
                status="expired",
                error_msg="HEM down at planned_start_utc; window passed",
            )
            notify_risk(
                f"Appliance job #{row['id']} expired during downtime "
                f"(planned_start={row['planned_start_utc']}).",
                extra={"job_id": int(row["id"])},
            )
            summary["expired"] += 1
            continue
        _register_cron(int(row["appliance_id"]), int(row["id"]), planned_start)
        summary["registered"] += 1

    if any(summary.values()):
        logger.info("appliance rehydrate_crons: %s", summary)
    return summary
