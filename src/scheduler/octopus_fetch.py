"""Daily Octopus Agile fetch → SQLite, retries, survival mode after 24h without rates."""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import db
from ..config import config
from ..foxess.client import FoxESSClient, FoxESSError
from ..notifier import notify_critical
from .agile import fetch_agile_export_rates, fetch_agile_rates

logger = logging.getLogger(__name__)

_RETRY_DELAYS_SEC = [600, 1800, 3600]


# The daily cron (16:05 local) and the 10-min retry job are distinct
# APScheduler jobs sharing a thread pool — per-job max_instances doesn't stop
# them overlapping. A full fetch runs calibrations + an LP solve (minutes on a
# slow host), so serialize: the loser skips and lets the winner finish (#726
# review, finding 4).
_fetch_in_flight = threading.Lock()


def fetch_and_store_rates(fox: FoxESSClient | None = None) -> dict[str, Any]:
    """Fetch Agile rates, store in DB, run optimizer. Updates octopus_fetch_state."""
    if not _fetch_in_flight.acquire(blocking=False):
        logger.info("Octopus fetch already in flight — skipping concurrent call")
        return {"ok": False, "error": "fetch already in flight"}
    try:
        return _fetch_and_store_rates_locked(fox)
    finally:
        _fetch_in_flight.release()


def _fetch_and_store_rates_locked(fox: FoxESSClient | None = None) -> dict[str, Any]:
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    now = datetime.now(UTC)
    db.update_octopus_fetch_state(last_attempt_at=now.isoformat())

    # Opportunistically sync Fox ESS daily energy history for PV calibration
    if fox:
        try:
            _sync_fox_energy_history(fox)
        except Exception as e:
            logger.debug("Fox energy history sync (non-fatal): %s", e)

    if not tariff:
        return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}

    rates = fetch_agile_rates(tariff_code=tariff)
    if not rates:
        st = db.get_octopus_fetch_state()
        fails = st.consecutive_failures + 1
        streak = st.failure_streak_started_at or now.isoformat()
        db.update_octopus_fetch_state(
            consecutive_failures=fails,
            failure_streak_started_at=streak,
        )
        _maybe_survival_mode(fox, fails, streak, now)
        return {"ok": False, "error": "fetch failed", "consecutive_failures": fails}

    n = db.save_agile_rates(rates, tariff)
    db.update_octopus_fetch_state(
        last_success_at=now.isoformat(),
        consecutive_failures=0,
        survival_mode_since=None,
        clear_failure_streak=True,
    )

    # Proactive appliance load nudge — fresh day-ahead rates just landed (~16:00),
    # the natural moment to discover tomorrow's negative/cheap windows and prompt
    # the user to LOAD the washer/dishwasher (the physical Smart-Control is the
    # consent gate; HEM can only nudge). Debounced once per appliance per window.
    # Non-fatal: a nudge failure must never block the rate store / LP solve.
    try:
        from .appliance_dispatch import nudge_appliance_windows
        nudge_appliance_windows(now=now, rates=rates)
    except Exception as e:
        logger.warning("appliance window nudge (non-fatal): %s", e)

    # Fetch + persist Octopus Outgoing (export) rates when configured. Stored separately
    # in agile_export_rates so the LP can use a per-slot export price (Outgoing Agile
    # varies ±20p/kWh half-hourly, just like the import side). Failure here is non-fatal —
    # the LP falls back to per-hour priors (or the flat EXPORT_RATE_PENCE constant) when
    # the table has no rows in the planning window — but a coverage gap left standing
    # mis-prices every export the LP plans (#691: the 2026-07-12 plunge day solved at
    # 15p flat vs a true 1.4–3.5p), so the 10-min retry job keeps re-fetching until
    # export coverage catches up with import coverage.
    export_n = 0
    export_code = (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
    if export_code:
        try:
            export_rates = fetch_agile_export_rates(export_tariff_code=export_code)
            if export_rates:
                export_n = db.save_agile_export_rates(export_rates, export_code)
        except Exception as e:
            logger.warning("Octopus export-rates fetch failed (non-fatal): %s", e)
        if export_rates_coverage_gap():
            logger.warning(
                "Outgoing export rates lag import coverage after fetch "
                "(export_rows=%d) — likely the Outgoing publication trailing the "
                "import side; the 10-min retry job will keep re-fetching (#691)",
                export_n,
            )

    # Refresh the per-hour-of-day + cloud-aware PV calibration tables.
    # PR L1.1 (2026-05-24) — tables are now Quartz-trained (actual /
    # direct_pv_kw from meteo_forecast_value), so we ALWAYS recompute
    # regardless of FORECAST_SOURCE. Quartz path applies the new factors
    # (PR L1); Open-Meteo fallback path also benefits (orientation
    # correction is source-agnostic in the limit).
    # Daily cadence is sufficient — bias drifts over weeks, not minutes.
    # Failure is non-fatal; LP falls back to the flat factor.
    try:
        from ..weather import compute_pv_calibration_hourly_table

        cal_status = compute_pv_calibration_hourly_table()
        logger.info("PV per-hour calibration recompute: %s", cal_status)
    except Exception as e:
        logger.warning("PV per-hour calibration recompute failed (non-fatal): %s", e)

    try:
        from ..weather import compute_pv_calibration_hourly_cloud_table

        cloud_status = compute_pv_calibration_hourly_cloud_table()
        logger.info("PV cloud-aware calibration recompute: %s", cloud_status)
    except Exception as e:
        logger.warning("PV cloud-aware calibration recompute failed (non-fatal): %s", e)

    # PR L3 (2026-05-24): 3D table (hour × cloud × solar elevation). Adds
    # seasonal sun-position separation on top of the 2D cloud table.
    # Sparse cells fall through to the 2D table via the lookup chain so
    # a partial-coverage 3D table is strictly additive (no regression).
    try:
        from ..weather import compute_pv_calibration_3d_table

        d3_status = compute_pv_calibration_3d_table()
        logger.info("PV 3D calibration recompute: %s", d3_status)
    except Exception as e:
        logger.warning("PV 3D calibration recompute failed (non-fatal): %s", e)

    summary: dict[str, Any] = {"ok": True, "rows": n, "export_rows": export_n}
    if config.USE_BULLETPROOF_ENGINE:
        try:
            # Route through bulletproof_mpc_job so the cooldown gate, plan-delta logging
            # and trigger_reason tagging apply uniformly across all event-driven re-plans.
            # Octopus rate publication is itself an event ("new prices known") so we ALWAYS
            # write the hardware on this path, regardless of LP_MPC_WRITE_DEVICES.
            from .runner import bulletproof_mpc_job

            # bulletproof_mpc_job returns falsy when the cooldown gate or the
            # dispatch lock skipped the solve — report that honestly so the
            # #726 gap retry can arm a pending re-solve instead of assuming
            # the fresh rates were priced into a committed plan.
            solved = bulletproof_mpc_job(
                force_write_devices=True, trigger_reason="octopus_fetch"
            )
            summary["optimizer"] = {"ok": bool(solved), "trigger": "octopus_fetch"}
        except Exception as e:
            logger.exception("Optimizer after fetch failed: %s", e)
            summary["optimizer_error"] = str(e)

    return summary


def _maybe_survival_mode(
    fox: FoxESSClient | None,
    consecutive_failures: int,
    streak_started_iso: str,
    now: datetime,
) -> None:
    """After 24h without a successful fetch in this streak, lock Self Use and disable V3."""
    if consecutive_failures < 1:
        return
    st = db.get_octopus_fetch_state()
    if st.survival_mode_since:
        return
    try:
        t0 = datetime.fromisoformat(streak_started_iso.replace("Z", "+00:00"))
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=UTC)
    except ValueError:
        return
    if now - t0 < timedelta(hours=24):
        return

    db.update_octopus_fetch_state(survival_mode_since=now.isoformat())
    notify_critical(
        "Survival mode: no Octopus Agile rates for 24h in this failure streak. "
        "Fox ESS Scheduler V3 disabled; inverter set to Self Use until the next successful fetch."
    )
    if not fox or config.OPENCLAW_READ_ONLY:
        return
    try:
        if fox.api_key:
            fox.set_scheduler_flag(False)
            # Persist the scheduler-off state so local derivations (e.g. the
            # heartbeat's execution_log fox_mode, #669) stop walking the last
            # uploaded groups — they are no longer in force on the inverter.
            db.save_fox_schedule_state([], enabled=False)
        fox.set_work_mode("Self Use")
        fox.set_min_soc(10)
        db.log_action(
            device="foxess",
            action="survival_mode",
            params={"scheduler_flag": False},
            result="success",
            trigger="scheduler",
        )
    except FoxESSError as e:
        logger.warning("Survival mode Fox fallback failed: %s", e)


# #691 export-gap retry state (in-process; a restart re-solves on boot anyway).
# _export_resolve_pending survives cooldown/lock skips of the MPC job: fresh
# export rates demand a re-solve until one actually RUNS, not until one was
# merely attempted.
_export_resolve_pending = False
_export_gap_last_warn_at: datetime | None = None


_import_gap_last_attempt_at: datetime | None = None
_import_gap_last_warn_at: datetime | None = None
# Mirrors _export_resolve_pending (#691): fresh import coverage must not go
# unpriced just because the fetch's internal re-solve hit the MPC cooldown or
# the dispatch lock. Armed when a gap-retry fetch advances coverage but the
# solve was skipped; cleared only when a solve actually completes.
_import_resolve_pending = False


def import_rates_coverage_gap(now_utc: datetime | None = None) -> bool:
    """True when ``agile_rates`` coverage is missing rates we should have (#726).

    The daily fetch races Octopus's ~16:00-local publication: a fetch that
    lands minutes early stores today's curve, records SUCCESS (no failure
    streak), and nothing retried — tomorrow stayed rateless until the next
    day's fetch, degrading the nightly plan push AND leaving the family
    calendar without tomorrow's windows (observed 2026-07-16). This is the
    import-side twin of ``export_rates_coverage_gap`` (#691).

    Two clauses:

    * coverage short of TODAY ~18:00 local → gap at ANY hour. This is the
      post-midnight continuation of a day-long publication outage: the
      missing day becomes "today" at 00:00 and a fetch-due test alone would
      disarm exactly when the plan is running rateless (review finding).
    * past the fetch-due moment (fetch hour + 15 min grace; unsupported for
      fetch hours ≥ ~23:45 local, where fetch-due rolls past midnight) →
      gap when coverage is short of TOMORROW ~18:00 local. Tomorrow's batch
      runs to ~23:00 local, so 18:00 is a safe "tomorrow landed" test.

    An empty table is NOT reported here: that's failure-streak territory,
    handled by ``should_run_retry_fetch``.
    """
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return False
    cov = db.get_agile_rates_coverage_max("agile_rates", tariff_code=tariff)
    if not cov:
        return False
    try:
        cov_dt = datetime.fromisoformat(str(cov).replace("Z", "+00:00"))
        if cov_dt.tzinfo is None:
            cov_dt = cov_dt.replace(tzinfo=UTC)
    except ValueError:
        return False
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now_local = (now_utc or datetime.now(UTC)).astimezone(tz)
    today_evening = now_local.replace(hour=18, minute=0, second=0, microsecond=0)
    if cov_dt < today_evening.astimezone(UTC):
        return True
    fetch_due = now_local.replace(
        hour=int(config.OCTOPUS_FETCH_HOUR),
        minute=int(config.OCTOPUS_FETCH_MINUTE),
        second=0,
        microsecond=0,
    ) + timedelta(minutes=15)
    if now_local < fetch_due:
        return False
    tomorrow_evening = (now_local + timedelta(days=1)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    return cov_dt < tomorrow_evening.astimezone(UTC)


def retry_import_rates_if_gap(fox: FoxESSClient | None = None) -> dict[str, Any]:
    """Close an import-rates coverage gap (#726). Called from the 10-min retry job.

    Cheap while Octopus is late: each attempt (≥ 30 min apart) PROBES with one
    rates GET and compares against current coverage — the full
    ``fetch_and_store_rates`` path (calibrations, Fox sync, forced re-solve)
    only runs when the probe shows new coverage to store. Warns at most
    hourly while the gap persists.

    When the full fetch's internal re-solve is skipped (MPC cooldown /
    dispatch lock), a pending re-solve is armed and retried every tick until
    a solve completes — fresh prices must end up in a committed plan
    (mirrors ``_export_resolve_pending``, #691).
    """
    global _import_gap_last_attempt_at, _import_gap_last_warn_at, _import_resolve_pending
    if _import_resolve_pending and _resolve_after_export_rates():
        _import_resolve_pending = False
    if not import_rates_coverage_gap():
        _import_gap_last_warn_at = None
        return {"ok": True, "gap": False, "fetched": False,
                "resolve_pending": _import_resolve_pending}
    now = datetime.now(UTC)
    if (
        _import_gap_last_attempt_at is not None
        and now - _import_gap_last_attempt_at < timedelta(minutes=30)
    ):
        return {"ok": False, "gap": True, "fetched": False, "throttled": True}
    _import_gap_last_attempt_at = now

    # Probe: one GET, no side effects. Skip the heavyweight path when Octopus
    # still hasn't published anything newer than what we already hold.
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    cov = db.get_agile_rates_coverage_max("agile_rates", tariff_code=tariff)
    try:
        probe = fetch_agile_rates(tariff_code=tariff)
    except Exception as e:
        logger.warning("import-gap probe failed (non-fatal): %s", e)
        probe = []
    probe_max = max((str(r.get("valid_from") or "") for r in probe), default="")
    if not probe_max or (cov and probe_max <= str(cov)):
        if (
            _import_gap_last_warn_at is None
            or now - _import_gap_last_warn_at >= timedelta(hours=1)
        ):
            _import_gap_last_warn_at = now
            logger.warning(
                "tomorrow's import rates still unpublished (probe max %s, have %s) "
                "— Octopus late; probing every 30 min, warning hourly (#726)",
                probe_max or "none", cov,
            )
        return {"ok": False, "gap": True, "fetched": False, "advanced": False}

    result = fetch_and_store_rates(fox)
    opt_ok = bool((result.get("optimizer") or {}).get("ok")) if result.get("ok") else False
    if result.get("ok") and not opt_ok:
        _import_resolve_pending = True
    still = import_rates_coverage_gap()
    if not still:
        _import_gap_last_warn_at = None
        logger.info(
            "import-rates gap closed (#726) — new Agile coverage landed via gap "
            "retry (fetch ok=%s, solved=%s, resolve_pending=%s)",
            result.get("ok"), opt_ok, _import_resolve_pending,
        )
    return {"ok": not still, "gap": still, "fetched": True, "fetch": result,
            "resolve_pending": _import_resolve_pending}


def export_rates_coverage_gap() -> bool:
    """True when Outgoing Agile pricing is in use and ``agile_export_rates``
    coverage (MAX ``valid_from`` for the configured tariff) lags ``agile_rates``
    coverage for the configured import tariff.

    #691: Octopus publishes the Outgoing day-ahead rates minutes after the
    import side. When the daily fetch lands in that window it stores tomorrow's
    import rates but misses the export rates, and nothing retried — the LP then
    priced a whole day's exports at the flat fallback. Import slots the export
    table doesn't cover yet are exactly the gap this detects.

    ``seg_flat`` mode returns False: the LP prices exports at the flat SEG rate
    there, so a stale Outgoing table must not force device-writing re-solves.
    Coverage is scoped to the configured tariff codes — both tables are
    upsert-only, so rows from a previously configured code would otherwise mask
    a real gap after a tariff switch.
    """
    if config.EXPORT_TARIFF_MODE != "outgoing_agile":
        return False
    export_code = (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
    if not export_code:
        return False
    import_code = (config.OCTOPUS_TARIFF_CODE or "").strip() or None
    import_max = db.get_agile_rates_coverage_max("agile_rates", tariff_code=import_code)
    if not import_max:
        return False
    export_max = db.get_agile_rates_coverage_max("agile_export_rates", tariff_code=export_code)
    return export_max is None or export_max < import_max


def retry_export_rates_if_gap() -> dict[str, Any]:
    """Re-fetch Outgoing rates while their coverage lags import coverage (#691).

    Called from the 10-min retry job. Import-side state (failure streaks,
    survival mode) is untouched — a missing export curve degrades pricing, it
    doesn't threaten planning itself.

    Whenever fresh coverage lands (even partial — real rates in the DB beat
    priors immediately), a re-solve through the same ``octopus_fetch`` trigger
    as the daily fetch is marked pending. Pending is cleared only when
    ``bulletproof_mpc_job`` reports an actual solve — a cooldown or
    dispatch-lock skip leaves it armed for the next tick, so the corrected
    prices can't be silently dropped. While a structural gap persists the
    retry keeps running (one cheap GET per tick) but warns at most hourly.
    """
    global _export_resolve_pending, _export_gap_last_warn_at
    gap = export_rates_coverage_gap()
    export_n = 0
    if gap:
        export_code = (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
        before_max = db.get_agile_rates_coverage_max(
            "agile_export_rates", tariff_code=export_code
        )
        try:
            export_rates = fetch_agile_export_rates(export_tariff_code=export_code)
            if export_rates:
                export_n = db.save_agile_export_rates(export_rates, export_code)
        except Exception as e:
            logger.warning("Octopus export-rates gap retry failed (non-fatal): %s", e)
        after_max = db.get_agile_rates_coverage_max(
            "agile_export_rates", tariff_code=export_code
        )
        if after_max is not None and (before_max is None or after_max > before_max):
            # Fresh coverage landed. The committed plan was solved on the
            # priors/flat fallback — a re-solve is due even if the export tail
            # is still short of import coverage.
            _export_resolve_pending = True
        gap = export_rates_coverage_gap()
        if gap:
            now = datetime.now(UTC)
            if (
                _export_gap_last_warn_at is None
                or now - _export_gap_last_warn_at >= timedelta(hours=1)
            ):
                _export_gap_last_warn_at = now
                logger.warning(
                    "Outgoing export rates still lag import coverage after retry "
                    "(saved %d rows, coverage %s) — retrying every tick, warning "
                    "hourly (#691)",
                    export_n,
                    after_max,
                )
            else:
                logger.debug(
                    "Outgoing export coverage still lags (saved %d rows)", export_n
                )
        else:
            _export_gap_last_warn_at = None
            logger.info(
                "Outgoing export coverage gap closed (+%d rows) — re-solving so "
                "the plan prices exports on the published curve",
                export_n,
            )
    if _export_resolve_pending and _resolve_after_export_rates():
        _export_resolve_pending = False
    return {
        "ok": not gap,
        "gap": gap,
        "export_rows": export_n,
        "resolve_pending": _export_resolve_pending,
    }


def _resolve_after_export_rates() -> bool:
    """Run the standard octopus_fetch re-solve; True only if a solve completed."""
    if not config.USE_BULLETPROOF_ENGINE:
        return True  # nothing to re-solve; don't hold pending forever
    try:
        from .runner import bulletproof_mpc_job

        return bool(
            bulletproof_mpc_job(force_write_devices=True, trigger_reason="octopus_fetch")
        )
    except Exception as e:
        logger.exception("Re-solve after export rates landed failed: %s", e)
        return False


def next_retry_seconds(failures: int) -> int:
    if failures <= len(_RETRY_DELAYS_SEC):
        return _RETRY_DELAYS_SEC[max(0, failures - 1)]
    return 3600


def should_run_retry_fetch() -> bool:
    """True if we are in a failure streak and backoff elapsed since last attempt."""
    st = db.get_octopus_fetch_state()
    if st.consecutive_failures == 0 or not st.failure_streak_started_at:
        return False
    try:
        last = datetime.fromisoformat((st.last_attempt_at or "").replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
    except ValueError:
        last = datetime.now(UTC) - timedelta(days=1)
    delay = next_retry_seconds(st.consecutive_failures)
    return (datetime.now(UTC) - last).total_seconds() >= delay


def _sync_fox_energy_history(fox: FoxESSClient, months_back: int = 3) -> int:
    """Pull Fox ESS monthly daily energy breakdown and upsert into fox_energy_daily.

    v10.2: delegates to ``foxess.service.ensure_fox_month_cached`` which is
    SQLite-first — already-cached months are no-ops, only missing days fire
    a cloud call. The ``fox`` argument is kept for backward compat but unused
    (the service uses its own client singleton).

    Fetches the current month plus ``months_back`` prior months.
    Returns total rows present after sync (close to the historical 'rows
    upserted' figure for new installs).
    """
    from ..foxess import service as _fox_svc

    now = datetime.now(UTC)
    total = 0
    seen_months: set[tuple[int, int]] = set()

    for delta in range(months_back + 1):
        yr = now.year
        mo = now.month - delta
        while mo <= 0:
            mo += 12
            yr -= 1
        if (yr, mo) in seen_months:
            continue
        seen_months.add((yr, mo))
        try:
            rows = _fox_svc.ensure_fox_month_cached(yr, mo)
            total += len(rows)
            logger.debug("Fox energy sync %d-%02d via cache: %d rows", yr, mo, len(rows))
        except Exception as e:
            logger.debug("Fox energy sync %d-%02d failed: %s", yr, mo, e)

    return total
