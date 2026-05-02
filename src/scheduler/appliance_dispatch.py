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

def find_cheapest_window(
    earliest_start_utc: datetime,
    deadline_utc: datetime,
    duration_minutes: int,
) -> tuple[datetime, datetime, float]:
    """Return ``(planned_start_utc, planned_end_utc, avg_price_pence)`` for the
    cheapest contiguous window of ``duration_minutes`` whose end ≤ ``deadline_utc``.

    Sliding-window minimum over half-hourly Agile rates from the
    ``agile_rates`` table. When the table is empty for the horizon, falls
    back to ``config.APPLIANCE_FALLBACK_WINDOW_LOCAL`` interpreted in the
    BULLETPROOF_TIMEZONE.
    """
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    duration = max(30, int(duration_minutes))
    # Slot grid: 30-minute slots starting at the next half-hour boundary.
    earliest_start_utc = _ceil_to_half_hour_utc(earliest_start_utc)
    if deadline_utc - earliest_start_utc < timedelta(minutes=duration):
        raise ValueError(
            f"deadline_utc {deadline_utc.isoformat()} is < {duration}min after "
            f"earliest_start_utc {earliest_start_utc.isoformat()}"
        )

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
    duration = int(appliance.get("default_duration_minutes") or 120)
    deadline_local = appliance.get("deadline_local_time") or config.APPLIANCE_DEFAULT_DEADLINE_LOCAL
    deadline_utc = _next_deadline_utc(deadline_local)
    now = _now_utc()

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

    try:
        start_utc, end_utc, avg_price = find_cheapest_window(now, deadline_utc, duration)
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
