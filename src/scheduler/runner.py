"""Scheduler runner: legacy Agile tick, Bulletproof heartbeat, APScheduler jobs."""
from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..daikin import service as daikin_service
from ..foxess.client import FoxESSClient
from ..foxess.service import derive_fox_mode_from_schedule, get_cached_realtime
from ..notifier import (
    notify_risk,
    push_cheap_window_start,
    push_negative_window_start,
    push_peak_window_start,
)
from ..state_machine import heartbeat_repair_fox_scheduler, reconcile_daikin_schedule_for_date
from .agile import fetch_agile_rates, get_current_and_next_slots
from .daikin import compute_lwt_adjustment

logger = logging.getLogger(__name__)

_scheduler_paused: bool = False
_background_scheduler: Any = None
_heartbeat_thread: threading.Thread | None = None
_heartbeat_stop = threading.Event()
_last_fox_verify_monotonic: float = 0.0
_last_exec_halfhour_key: str | None = None
_last_room_temp: float | None = None
_last_room_wall_utc: datetime | None = None
_last_notified_slot_kind: str | None = None
# Lazy-init flag: True once we've read the persisted value from runtime_settings
# at module load (or first heartbeat tick). Without persistence the dedupe state
# is lost on every container restart, causing a fresh ping for the *same* slot
# kind we already announced. The 2026-04-30 active-mode rollout fired three
# duplicate "🔵 PAID to use" notifications across three restarts inside the
# same negative-price window because of this gap.
_last_notified_slot_kind_loaded: bool = False
_comfort_morning_logged: set[str] = set()

# Event-driven MPC ("Waze") — Epic #73.
# Cooldown gate: any MPC run (cron / event / dynamic_replan) stamps this; the next
# `bulletproof_mpc_job` call within MPC_COOLDOWN_SECONDS is short-circuited.
_last_mpc_run_at: datetime | None = None
# Solve+dispatch mutex (#676). The cooldown above is check-at-entry /
# stamp-at-completion, so it does NOT exclude overlap: a heartbeat-thread
# trigger (soc_drift, pv_*, load_upside) and an APScheduler worker-thread
# trigger (tier_boundary, dynamic_replan, forecast_revision, plan_push,
# octopus_fetch) can both pass the gate and interleave two `run_optimizer`
# executions — and with them two Fox V3 uploads (group swaps mid-fill).
# Solves take ~10s typical / ~100s bounded since #673, so the window is real.
#
# Lock LEVEL (do not change without re-reading this): the lock is held by the
# JOB WRAPPERS + the direct API/MCP `run_optimizer` call sites — never inside
# `run_optimizer` itself. `bulletproof_mpc_job` calls `run_optimizer` while
# holding the lock, so putting it inside `run_optimizer` too would deadlock.
# One consistent level: whoever calls `run_optimizer` acquires this first.
optimizer_dispatch_lock = threading.Lock()
# Wedge guard for the nightly plan push's BLOCKING acquire: solves are bounded
# ~100s (#673), so a 600s wait means the in-flight solve is wedged — the push
# then proceeds unserialized rather than losing the canonical commitment.
PLAN_PUSH_LOCK_TIMEOUT_SECONDS: float = 600.0
# Hysteresis on the SoC drift trigger: count consecutive heartbeat ticks above
# threshold; only fire when we cross MPC_DRIFT_HYSTERESIS_TICKS. Resets on recovery.
_consecutive_drift_ticks: int = 0
# Consecutive heartbeats the ahead-of-plan gate has suppressed. Bounds how
# long an at-the-boundary suppression can hold (review P1: plan plateaus at
# exactly soc − threshold → suppressed forever); past the cap the drift fires.
_consecutive_ahead_suppressed: int = 0
_consecutive_pv_up_ticks: int = 0
_consecutive_pv_down_ticks: int = 0
_consecutive_load_up_ticks: int = 0


def _can_run_mpc_now() -> bool:
    """True if the cooldown window has elapsed since the last MPC run."""
    if _last_mpc_run_at is None:
        return True
    elapsed = (datetime.now(UTC) - _last_mpc_run_at).total_seconds()
    return elapsed >= float(config.MPC_COOLDOWN_SECONDS)


def _lp_predicted_soc_pct_at(when_utc: datetime) -> float | None:
    """SoC % the most recent LP solution predicts for the slot containing ``when_utc``.

    Returns None when no LP run is on file or the timestamp is outside the latest plan's
    horizon. Used by the heartbeat drift trigger to compare reality vs the plan.
    """
    try:
        run_id = db.find_run_for_time(when_utc.isoformat())
        if not run_id:
            return None
        slots = db.get_lp_solution_slots(run_id)
        if not slots:
            return None
        cap = float(config.BATTERY_CAPACITY_KWH)
        if cap <= 0:
            return None
        target: dict[str, Any] | None = None
        for s in slots:
            st_raw = s.get("slot_time_utc")
            if not st_raw:
                continue
            try:
                st = datetime.fromisoformat(st_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if st <= when_utc:
                target = s
            else:
                break
        if target is None or target.get("soc_kwh") is None:
            return None
        return float(target["soc_kwh"]) / cap * 100.0
    except Exception as e:
        logger.debug("_lp_predicted_soc_pct_at failed: %s", e)
        return None


def _lp_predicted_soc_max_pct_within(when_utc: datetime, hours: float) -> float | None:
    """Max SoC % the committed plan reaches in ``[when_utc, when_utc + hours]``.

    Used by the directional drift gate: when live SoC runs AHEAD of the
    prediction but the plan itself reaches that level within the look-ahead,
    the deviation is early arrival on the planned trajectory (Fox ForceCharge
    fills faster than the LP's gentle per-slot taper), not drift.
    """
    try:
        run_id = db.find_run_for_time(when_utc.isoformat())
        if not run_id:
            return None
        slots = db.get_lp_solution_slots(run_id)
        if not slots:
            return None
        cap = float(config.BATTERY_CAPACITY_KWH)
        if cap <= 0:
            return None
        end_utc = when_utc + timedelta(hours=float(hours))
        best: float | None = None
        for s in slots:
            st_raw = s.get("slot_time_utc")
            soc_kwh = s.get("soc_kwh")
            if not st_raw or soc_kwh is None:
                continue
            try:
                st = datetime.fromisoformat(st_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            # Include the slot CONTAINING when_utc: soc_kwh is end-of-slot SoC
            # keyed by slot start, so the trajectory 0-30 min ahead lives on
            # the slot that started up to 30 min ago.
            if st < when_utc - timedelta(minutes=30) or st > end_utc:
                continue
            pct = float(soc_kwh) / cap * 100.0
            if best is None or pct > best:
                best = pct
        return best
    except Exception as e:
        logger.debug("_lp_predicted_soc_max_pct_within failed: %s", e)
        return None


def _log_plan_delta_after_trigger(prev_run_id: int | None, new_run_id: int | None, trigger_reason: str) -> dict[str, float] | None:
    """Log how much the freshly-solved LP diverges from the previous one.

    Compares the next ``MPC_PLAN_DELTA_LOOKAHEAD_HOURS`` of overlap. Surfaces the
    "is this trigger actually changing anything?" signal so we can detect plan
    thrashing in production without manual log archeology. Best-effort only —
    failures here must never break the optimiser run.

    Returns a dict ``{max_soc_delta_pct, sum_grid_delta_kwh, sum_charge_delta_kwh,
    overlap_count}`` for downstream observability (logged to ``optimizer_log`` /
    journalctl). The user-facing PLAN_REVISION Telegram ping was removed in the
    2026-05-10 cleanup — it duplicated the auto-applied PLAN_PROPOSED ping for
    the same MPC tick. Returns ``None`` when there's nothing to compare.
    """
    if not prev_run_id or not new_run_id:
        return None
    try:
        prev = {s["slot_time_utc"]: s for s in db.get_lp_solution_slots(prev_run_id)}
        new = db.get_lp_solution_slots(new_run_id)
        if not prev or not new:
            return None
        cap = float(config.BATTERY_CAPACITY_KWH) or 1.0
        horizon_end = datetime.now(UTC) + timedelta(hours=int(config.MPC_PLAN_DELTA_LOOKAHEAD_HOURS))
        max_soc_delta_pct = 0.0
        sum_grid_delta_kwh = 0.0
        sum_charge_delta_kwh = 0.0
        overlap_count = 0
        for s in new:
            st_raw = s.get("slot_time_utc")
            if not st_raw or st_raw not in prev:
                continue
            try:
                st = datetime.fromisoformat(st_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if st > horizon_end:
                break
            p = prev[st_raw]
            overlap_count += 1
            new_soc = s.get("soc_kwh")
            old_soc = p.get("soc_kwh")
            if new_soc is not None and old_soc is not None:
                d = abs(float(new_soc) - float(old_soc)) / cap * 100.0
                if d > max_soc_delta_pct:
                    max_soc_delta_pct = d
            new_imp = s.get("import_kwh") or 0.0
            old_imp = p.get("import_kwh") or 0.0
            sum_grid_delta_kwh += abs(float(new_imp) - float(old_imp))
            new_chg = s.get("charge_kwh") or 0.0
            old_chg = p.get("charge_kwh") or 0.0
            sum_charge_delta_kwh += abs(float(new_chg) - float(old_chg))
        logger.info(
            "MPC plan delta (trigger=%s, overlap=%d slots): SoC max-Δ=%.1f%% grid Δ=%.2f kWh charge Δ=%.2f kWh",
            trigger_reason,
            overlap_count,
            max_soc_delta_pct,
            sum_grid_delta_kwh,
            sum_charge_delta_kwh,
        )
        return {
            "max_soc_delta_pct": max_soc_delta_pct,
            "sum_grid_delta_kwh": sum_grid_delta_kwh,
            "sum_charge_delta_kwh": sum_charge_delta_kwh,
            "overlap_count": float(overlap_count),
        }
    except Exception as e:
        logger.debug("plan-delta logging failed (non-fatal): %s", e)
        return None


def _get_forecast_temp_c(now_utc: datetime) -> float | None:
    """Look up the Open-Meteo forecast temperature for *now_utc* from the cached meteo_forecast DB.

    The optimizer saves the forecast after each LP run; this avoids a live HTTP call in the
    heartbeat. Returns None if no cached forecast is available (bootstrapping period).
    """
    today_iso = now_utc.date().isoformat()
    rows = db.get_meteo_forecast(today_iso)
    if not rows:
        return None
    # Find the nearest slot by absolute time difference
    best: float | None = None
    best_delta: float = float("inf")
    for row in rows:
        try:
            slot_dt = datetime.fromisoformat(row["slot_time"].replace("Z", "+00:00"))
            delta = abs((slot_dt - now_utc).total_seconds())
            if delta < best_delta:
                best_delta = delta
                best = row["temp_c"]
        except (KeyError, ValueError):
            continue
    return best


def _get_forecast_pv_kw(now_utc: datetime) -> float | None:
    """Forecast PV kW at *now_utc* from cached meteo rows, mapped through LP transform."""
    try:
        from ..weather import (
            compute_pv_calibration_factor,
            compute_today_pv_correction_factor,
            estimate_pv_kw,
            get_pv_calibration_factor_for,
        )

        today_iso = now_utc.date().isoformat()
        rows = db.get_meteo_forecast(today_iso)
        if not rows:
            return None
        nearest: dict[str, Any] | None = None
        best_delta = float("inf")
        for row in rows:
            st_raw = row.get("slot_time")
            if not st_raw:
                continue
            try:
                slot_dt = datetime.fromisoformat(str(st_raw).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            delta = abs((slot_dt - now_utc).total_seconds())
            if delta < best_delta:
                best_delta = delta
                nearest = row
        if nearest is None:
            return None
        rad_wm2 = float(nearest.get("solar_w_m2") or 0.0)
        cloud_pct = nearest.get("cloud_cover_pct")
        cloud_pct_f = float(cloud_pct) if cloud_pct is not None else 50.0
        att = max(0.0, min(1.0, 1.0 - 0.25 * (cloud_pct_f / 100.0)))
        rad_eff = max(0.0, rad_wm2 * att)
        cal_cloud = db.get_pv_calibration_hourly_cloud()
        cal_hour = db.get_pv_calibration_hourly()
        cal_3d = db.get_pv_calibration_3d()
        flat = compute_pv_calibration_factor() if not cal_cloud and not cal_hour else 1.0
        cal = get_pv_calibration_factor_for(
            now_utc.hour,
            cloud_pct_f,
            cloud_table=cal_cloud,
            hourly_table=cal_hour,
            flat=flat,
            table_3d=cal_3d,
            slot_utc=now_utc,
        )
        today_factor, _diag = compute_today_pv_correction_factor()
        return estimate_pv_kw(rad_eff) * cal * today_factor
    except Exception as e:
        logger.debug("_get_forecast_pv_kw failed: %s", e)
        return None


def _lp_planned_import_kwh_at(slot_start_utc: datetime) -> float | None:
    """Planned grid import for a specific half-hour slot from the most recent
    LP solution active at that slot. Used by the import_overshoot trigger.

    Returns None when no LP run was active for that slot or the LP didn't
    produce an import row for it (LP solve failed / horizon overshot).
    """
    try:
        run_id = db.find_run_for_time(slot_start_utc.isoformat())
        if not run_id:
            return None
        slots = db.get_lp_solution_slots(run_id)
        if not slots:
            return None
        for s in slots:
            st_raw = s.get("slot_time_utc")
            if not st_raw:
                continue
            try:
                st = datetime.fromisoformat(str(st_raw).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            # Match the slot starting at exactly slot_start_utc.
            if st == slot_start_utc:
                return float(s.get("import_kwh") or 0.0)
        return None
    except Exception as e:
        logger.debug("_lp_planned_import_kwh_at failed: %s", e)
        return None


def _actual_import_kwh_for_slot(slot_start_utc: datetime) -> float | None:
    """Average grid_import_kw × 0.5 h across pv_realtime_history samples in
    the half-hour slot starting at ``slot_start_utc``.
    """
    try:
        slot_end = slot_start_utc + timedelta(minutes=30)
        with db._lock:
            conn = db.get_connection()
            try:
                cur = conn.execute(
                    """SELECT AVG(grid_import_kw) FROM pv_realtime_history
                       WHERE captured_at >= ? AND captured_at < ?
                         AND grid_import_kw IS NOT NULL""",
                    (slot_start_utc.isoformat(), slot_end.isoformat()),
                )
                avg_kw = cur.fetchone()[0]
            finally:
                conn.close()
        if avg_kw is None:
            return None
        return float(avg_kw) * 0.5
    except Exception as e:
        logger.debug("_actual_import_kwh_for_slot failed: %s", e)
        return None


def _lp_predicted_load_kw_at(when_utc: datetime) -> float | None:
    """Expected gross AC load kW (incl. heat pump) from the latest LP solution
    at the slot containing ``when_utc``.

    The Fox H1's ``loadsPower`` reading is gross household AC consumption —
    everything passing through the inverter's load CT, including the Daikin
    heat pump on this install (Daikin sits downstream of the load CT). The
    apples-to-apples comparison with ``rt.load_power`` is therefore:

        gross_load = imp + pv_use + dis - exp - chg

    which equals ``base_load + dhw + space`` (the LP's split between
    household base load and heat-pump consumption). Subtracting ``dhw +
    space`` from this would over-estimate the live deviation whenever the
    heat pump is running.

    NOTE: this assumes the inverter's CT placement matches the typical
    retrofit (Daikin downstream). If your install has Daikin upstream of
    the load CT, ``loadsPower`` excludes the heat pump and you'd want to
    subtract ``dhw + space`` here. The live deviation trigger is
    intentionally OFF by default (``MPC_LIVE_DEVIATION_HYSTERESIS_TICKS``
    high enough that triggers won't fire in practice) until this is
    validated against measured data — see PR description.
    """
    try:
        run_id = db.find_run_for_time(when_utc.isoformat())
        if not run_id:
            return None
        slots = db.get_lp_solution_slots(run_id)
        if not slots:
            return None
        target: dict[str, Any] | None = None
        for s in slots:
            st_raw = s.get("slot_time_utc")
            if not st_raw:
                continue
            try:
                st = datetime.fromisoformat(str(st_raw).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if st <= when_utc:
                target = s
            else:
                break
        if target is None:
            return None
        imp = float(target.get("import_kwh") or 0.0)
        pv_use = float(target.get("pv_use_kwh") or 0.0)
        dis = float(target.get("discharge_kwh") or 0.0)
        exp = float(target.get("export_kwh") or 0.0)
        chg = float(target.get("charge_kwh") or 0.0)
        # Gross AC load (matches ``loadsPower`` semantics). Per-slot kWh →
        # average kW: divide by slot duration (0.5 h).
        load_kwh = imp + pv_use + dis - exp - chg
        return max(0.0, load_kwh / 0.5)
    except Exception as e:
        logger.debug("_lp_predicted_load_kw_at failed: %s", e)
        return None


def get_scheduler_paused() -> bool:
    return _scheduler_paused


def pause_scheduler() -> None:
    global _scheduler_paused
    _scheduler_paused = True


def resume_scheduler() -> None:
    global _scheduler_paused
    _scheduler_paused = False


def get_scheduler_status() -> dict:
    """Return scheduler status; includes Bulletproof hints when enabled."""
    out = {
        "enabled": config.SCHEDULER_ENABLED,
        "bulletproof": config.USE_BULLETPROOF_ENGINE,
        "paused": get_scheduler_paused(),
        "current_price_pence": None,
        "next_cheap_from": None,
        "next_cheap_to": None,
        "planned_lwt_adjustment": 0.0,
        "tariff_code": config.OCTOPUS_TARIFF_CODE or None,
    }
    if not config.OCTOPUS_TARIFF_CODE:
        return out

    rates = fetch_agile_rates()
    current, next_cheap, current_price = get_current_and_next_slots(
        rates,
        cheap_threshold_pence=config.SCHEDULER_CHEAP_THRESHOLD_PENCE,
        peak_start=config.SCHEDULER_PEAK_START,
        peak_end=config.SCHEDULER_PEAK_END,
    )
    out["current_price_pence"] = current_price
    if next_cheap:
        out["next_cheap_from"] = next_cheap.get("valid_from")
        out["next_cheap_to"] = next_cheap.get("valid_to")
    if current_price is not None and not get_scheduler_paused() and not config.USE_BULLETPROOF_ENGINE:
        out["planned_lwt_adjustment"] = compute_lwt_adjustment(
            current_price,
            config.SCHEDULER_CHEAP_THRESHOLD_PENCE,
            config.SCHEDULER_PEAK_START,
            config.SCHEDULER_PEAK_END,
            config.SCHEDULER_PREHEAT_LWT_BOOST,
        )
    return out


def _try_fox() -> FoxESSClient | None:
    try:
        return FoxESSClient(**config.foxess_client_kwargs())
    except Exception as e:
        logger.debug("Fox client unavailable: %s", e)
        return None


def _in_octopus_pre_slot_window(
    now: datetime | None = None,
    lead_seconds: int | None = None,
) -> bool:
    """Return True when *now* is in the 5-minute window before an Octopus half-hour boundary.

    Octopus slots start at HH:00 and HH:30 (UTC / wall-clock).  We want to refresh
    Daikin device state in the [HH:25, HH:30) and [HH:55, HH:00) windows so the LP
    has fresh data before the new rate slot begins.

    lead_seconds defaults to DAIKIN_SLOT_TRANSITION_WINDOW_SECONDS (300 = 5 min).
    """
    if now is None:
        now = datetime.now(UTC)
    if lead_seconds is None:
        lead_seconds = config.DAIKIN_SLOT_TRANSITION_WINDOW_SECONDS

    minute = now.minute
    second = now.second
    total_seconds_in_minute = minute * 60 + second
    # Boundary at :00 (0 s) and :30 (1800 s)
    # Lead window is [boundary - lead_seconds, boundary)
    # i.e. [:30 - 300s, :30) → [25:00, 30:00) and [:00 - 300s, :60 end of prev) → [55:00, 60:00)
    lead_start_1 = 1800 - lead_seconds   # seconds from hour start to start of first window
    lead_start_2 = 3600 - lead_seconds   # seconds from hour start to start of second window

    in_window = (
        (lead_start_1 <= total_seconds_in_minute < 1800)
        or (lead_start_2 <= total_seconds_in_minute < 3600)
    )
    return in_window


def _parse_hhmm_to_seconds(value: str) -> int:
    parts = (value or "00:00").strip().split(":")
    hour = int(parts[0]) if parts and parts[0] else 0
    minute = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    return hour * 3600 + minute * 60


def _in_daikin_2h_refresh_window(
    now_utc: datetime | None = None,
    *,
    fire_minute_start: int = 2,
    fire_minute_end: int = 7,
) -> bool:
    """Return True for the first few minutes after each even-hour UTC tick.

    Onecta buffers operational + consumption data in **2-hour buckets** that
    rotate at fixed UTC times (00, 02, 04, …, 22). A refresh fired right
    after rotation captures the fresh bucket data; firing earlier just gets
    the previous bucket repeated. Default window = ``[02, 07)`` minutes
    past each even hour, so 12 refreshes/day land deterministically right
    after each Onecta cache rotation.

    Cost: 12 calls/day vs the 200/day Daikin quota — leaves 188/day for
    Octopus pre-slot + cold-start + manual fetches.

    See issue #267 (Daikin observation strategy epic), story S1.
    """
    if now_utc is None:
        now_utc = datetime.now(UTC)
    # Even-hour rotation: 0, 2, 4, ..., 22 UTC
    if now_utc.hour % 2 != 0:
        return False
    return fire_minute_start <= now_utc.minute < fire_minute_end


def _in_daikin_calibration_window(
    now_local: datetime | None = None,
    windows: str | None = None,
) -> bool:
    """Return True in the local morning/afternoon Daikin calibration windows.

    The default windows are tuned for the user-visible pain points:
    early morning heating/tank decisions and afternoon re-plan opportunities
    when the outdoor temperature trend starts to diverge from the forecast.
    """
    if now_local is None:
        now_local = datetime.now(ZoneInfo(config.BULLETPROOF_TIMEZONE))
    if windows is None:
        windows = getattr(config, "DAIKIN_CALIBRATION_WINDOWS_LOCAL", "06:00-08:00,14:30-16:30")

    current_s = now_local.hour * 3600 + now_local.minute * 60 + now_local.second
    for item in str(windows).split(","):
        item = item.strip()
        if not item or "-" not in item:
            continue
        start_s, end_s = [part.strip() for part in item.split("-", 1)]
        start = _parse_hhmm_to_seconds(start_s)
        end = _parse_hhmm_to_seconds(end_s)
        if start <= end:
            if start <= current_s < end:
                return True
        else:
            if current_s >= start or current_s < end:
                return True
    return False


def bulletproof_daikin_consumption_rollup_job(*, two_hourly_only: bool = False) -> None:
    """Roll daily Daikin consumption from the cached gateway-devices payload (S10.12 / #178).

    Daikin Onecta exposes ``consumptionData.value.electrical.<mode>.w`` — a 14-day
    array (last week + this week per management point). We parse it from the
    already-cached devices payload (zero extra API quota) and upsert per-day rows
    into ``daikin_consumption_daily``.

    ``two_hourly_only`` (intraday cron, 2026-07-05): refresh ONLY the 2-hourly
    table + its telemetry-integral, skipping the daily rollup. The daily table
    already carries a partial "today" row from the nightly run and its
    consumers deliberately end at yesterday (autoscale, k-calibration); there's
    no reason to churn it midday for the Consumption panel, which reads the
    2-hourly split. Keeps the intraday surface to the one table that benefits.
    """
    from .. import db

    try:
        from ..api.main import get_daikin_client
        client = get_daikin_client()
    except Exception as e:
        logger.warning("daikin rollup: client init failed: %s", e)
        return

    if not two_hourly_only:
        try:
            per_day = client.get_daily_consumption_from_cache()
            if not per_day:
                logger.info("daikin rollup: no consumption data in cached payload — skipped")
            else:
                n = 0
                for day, b in per_day.items():
                    db.upsert_daikin_consumption_daily(
                        date=day,
                        kwh_total=b.get("total_kwh"),
                        kwh_heating=b.get("heating_kwh"),
                        kwh_dhw=b.get("dhw_kwh"),
                        source="onecta_cache",
                    )
                    n += 1
                logger.info("daikin_consumption_daily rollup: %d days written from cache", n)
        except Exception as e:
            logger.warning("daikin rollup failed (non-fatal): %s", e)

    # 2-hourly rollup (#238) — feeds the future Daikin physics calibration.
    # Same cached payload, different array (``d`` vs ``w``). Independent
    # try/except so a parse failure here doesn't drop the daily rollup above.
    try:
        per_2h = client.get_2hourly_consumption_from_cache()
        if not per_2h:
            logger.info("daikin 2h rollup: no consumption data in cached payload — skipped")
            return
        n = 0
        for day, day_buckets in per_2h.items():
            for bucket_idx, b in day_buckets.items():
                db.upsert_daikin_consumption_2hourly(
                    date=day,
                    bucket_idx=bucket_idx,
                    kwh_total=b.get("total_kwh"),
                    kwh_heating=b.get("heating_kwh"),
                    kwh_dhw=b.get("dhw_kwh"),
                    source="onecta_cache",
                )
                n += 1
        logger.info("daikin_consumption_2hourly rollup: %d (date,bucket) rows written from cache", n)
    except Exception as e:
        logger.warning("daikin 2h rollup failed (non-fatal): %s", e)

    # Telemetry-integral 2h refinement (#425). Onecta's public API returns
    # INTEGER kWh per 2h bucket — every value under 1 kWh truncates to 0.
    # We integrate the per-30-min telemetry through physics + tank-thermal-
    # mass to recover sub-integer precision. Only overwrites Onecta rows
    # when the integer rounds to 0 or differs by < 0.5 (see decision logic
    # in ``sync_daikin_2hourly_telemetry``).
    try:
        from datetime import date as _date, timedelta as _td
        from ..daikin.service import sync_daikin_2hourly_telemetry
        today = _date.today()
        wrote = 0
        for d in (today - _td(days=1), today):
            try:
                r = sync_daikin_2hourly_telemetry(d)
                wrote += r.get("written", 0)
            except Exception as exc:
                logger.warning("telemetry-integral 2h sync failed for %s: %s", d, exc)
        logger.info("daikin_consumption_2hourly telemetry-integral: %d bucket rows refined", wrote)
    except Exception as e:
        logger.warning("telemetry-integral 2h rollup failed (non-fatal): %s", e)


def bulletproof_fox_energy_rollup_job() -> None:
    """Aggregate ``pv_realtime_history`` into per-day kWh totals (S10.10 / #177).

    Replaces the broken Fox Cloud per-day API rollup. Uses our own heartbeat-
    captured telemetry (~3 min cadence) — zero Fox quota cost. Re-aggregates
    last 35 days to cover any gaps (idempotent upsert).
    """
    from datetime import date as _date, timedelta as _td
    from .. import db

    try:
        end = _date.today().isoformat()
        start = (_date.today() - _td(days=35)).isoformat()
        rows = db.compute_fox_energy_daily_from_realtime(start_date=start, end_date=end)
        if rows:
            n = db.upsert_fox_energy_daily(rows)
            logger.info("fox_energy_daily rollup: %d days computed (%s → %s)", n, start, end)
        else:
            logger.info("fox_energy_daily rollup: no samples in window — skipped")
    except Exception as e:
        logger.warning("fox_energy_daily rollup failed (non-fatal): %s", e)


def bulletproof_octopus_fetch_job() -> None:
    from .octopus_fetch import fetch_and_store_rates

    fetch_and_store_rates(_try_fox())
    # V12 — refresh tier-boundary one-shots whenever fresh rates land. The
    # next-day boundaries can shift by hours between yesterday's plan and
    # today's published rates, so re-register lets the new windows fire.
    try:
        _register_tier_boundary_triggers()
    except Exception as e:  # pragma: no cover — best-effort, never break fetch
        logger.debug("tier_boundary re-register after fetch failed: %s", e)


def bulletproof_octopus_retry_job() -> None:
    from .octopus_fetch import (
        fetch_and_store_rates,
        retry_export_rates_if_gap,
        should_run_retry_fetch,
    )

    if should_run_retry_fetch():
        fetch_and_store_rates(_try_fox())
        try:
            _register_tier_boundary_triggers()
        except Exception as e:  # pragma: no cover
            logger.debug("tier_boundary re-register after retry failed: %s", e)
        return
    # #691 — the import fetch can succeed while the Outgoing publication lags
    # by a few minutes (race at the daily fetch), leaving export coverage a
    # whole day behind with no failure streak to trigger the full retry above.
    # Keep re-fetching export-only until coverage catches up; the gap close
    # re-solves via the standard octopus_fetch trigger.
    try:
        retry_export_rates_if_gap()
    except Exception as e:
        logger.warning("export-rates gap retry failed (non-fatal): %s", e)


def bulletproof_morning_brief_job() -> None:
    """Daily morning digest — today's forecast (V12, was bulletproof_daily_brief_job)."""
    from ..analytics.daily_brief import send_morning_brief_webhook

    try:
        send_morning_brief_webhook()
    except Exception as e:
        logger.warning("Morning brief failed: %s", e)


def bulletproof_consumption_backfill_job() -> None:
    """Daily post-hoc reconciliation: pull yesterday's actual half-hourly
    consumption from Octopus and rewrite the ``execution_log`` rows
    (replacing ``source="estimated"`` heartbeat samples with metered kWh).

    Affects the morning + night brief PnL accuracy from the next run on:
    realised cost, SVT delta, fixed delta all become true measured values
    instead of single-sample × 0.5 h extrapolations. See
    ``src/scheduler/consumption_backfill.py`` for design details.

    Fires at ``CONSUMPTION_BACKFILL_HOUR:MINUTE`` local (default 04:00) and
    sweeps the trailing ``CONSUMPTION_BACKFILL_SWEEP_DAYS`` window (#533) —
    Octopus routinely publishes later than 24 h, and the old yesterday-only
    single attempt permanently lost those days."""
    from .consumption_backfill import backfill_sweep

    try:
        results = backfill_sweep()
        if not results:
            logger.info("consumption_backfill cron: window fully reconciled, nothing to do")
        for result in results:
            logger.info(
                "consumption_backfill cron: date=%s fetched=%d updated=%d missing=%d error=%s",
                result.target_date, result.slots_fetched, result.slots_updated,
                result.slots_missing, result.error or "none",
            )
    except Exception as e:
        logger.warning("Consumption backfill failed (non-fatal): %s", e)


def bulletproof_forecast_skill_log_job() -> None:
    """Rebuild yesterday's UTC forecast-vs-actual skill rows.

    Runs after the nightly consumption backfill so the prior UTC day has the
    fullest available PV + outdoor-temperature actuals. Best-effort only: a
    failure here must not interfere with the rest of the scheduler.
    """
    target_date_utc = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    try:
        rows_written = db.rebuild_forecast_skill_log_for_date(target_date_utc)
        logger.info(
            "forecast_skill_log rebuild: date_utc=%s rows=%d",
            target_date_utc,
            rows_written,
        )
    except Exception as e:
        logger.warning("forecast_skill_log rebuild failed (non-fatal): %s", e)


def bulletproof_pv_error_log_job() -> None:
    """Persist yesterday's per-slot committed-forecast-vs-actual PV rows (#462).

    Runs nightly after the Fox/PV roll-ups so the prior UTC day has its fullest
    actuals. Best-effort only — a failure must not disturb the scheduler.
    """
    target_day = datetime.now(UTC).date() - timedelta(days=1)
    try:
        rows = db.rebuild_pv_error_log_for_date(target_day)
        logger.info("pv_error_log rebuild: date_utc=%s rows=%d", target_day.isoformat(), rows)
    except Exception as e:
        logger.warning("pv_error_log rebuild failed (non-fatal): %s", e)
    # #486 — refresh the adaptive recent-bias corrector from the just-rebuilt
    # error log (no-op effect on the LP unless PV_RECENT_BIAS_ENABLED).
    try:
        from ..weather import refresh_pv_recent_bias
        refresh_pv_recent_bias()
    except Exception as e:
        logger.warning("pv_recent_bias refresh failed (non-fatal): %s", e)


def bulletproof_dhw_error_log_job() -> None:
    """Nightly rebuild of dhw_error_log for YESTERDAY (local) — committed LP
    DHW forecast vs realised Daikin DHW energy per 2h bucket (PR C, 2026-07-02
    LP audit: DHW was the largest unmonitored forecast stream). Runs after the
    02:35 UTC Daikin consumption rollup. Best-effort.
    """
    try:
        target_day = (datetime.now(ZoneInfo(config.BULLETPROOF_TIMEZONE)) - timedelta(days=1)).date()
        rows = db.rebuild_dhw_error_log_for_date(target_day)
        logger.info("dhw_error_log rebuild: day_local=%s rows=%d", target_day.isoformat(), rows)
    except Exception as e:
        logger.warning("dhw_error_log rebuild failed (non-fatal): %s", e)
    # Refresh the per-bucket bias corrector from the just-rebuilt error log.
    # Cheap + observable; NO forecast effect unless DHW_BUCKET_BIAS_ENABLED.
    try:
        from ..dhw_bias import refresh_dhw_bucket_bias
        refresh_dhw_bucket_bias()
    except Exception as e:
        logger.warning("dhw_bucket_bias refresh failed (non-fatal): %s", e)
    # One-shot actionable ping when the enable gate (>= N days + out-of-sample
    # MAE improvement) is met. Never auto-enables; deduped via runtime setting.
    try:
        from ..dhw_bias import maybe_suggest_enable
        maybe_suggest_enable()
    except Exception as e:
        logger.warning("dhw_bias enable-gate check failed (non-fatal): %s", e)


def bulletproof_thermal_learning_job() -> None:
    """Nightly W2 thermal-learner refresh (#540): τ from unheated overnight
    indoor decay + UA HDD re-fit + C = τ·UA into building_thermal_calibration.
    Quality-gated no-op until room sensors push data. Best-effort.
    """
    try:
        from ..analytics.thermal_learning import refresh_building_thermal_calibration
        result = refresh_building_thermal_calibration()
        logger.info("thermal learning refresh: status=%s", result.get("status"))
    except Exception as e:
        logger.warning("thermal learning refresh failed (non-fatal): %s", e)


def bulletproof_load_error_log_job() -> None:
    """Persist yesterday's per-slot committed-LOAD-forecast-vs-actual rows.

    Phase-1 measurement for load calibration (load analog of the PV error log).
    Runs nightly just after the PV error log so the prior UTC day has its fullest
    load samples. Measurement only — the Phase-2 refresh below is gated. Best-effort.
    """
    target_day = datetime.now(UTC).date() - timedelta(days=1)
    try:
        rows = db.rebuild_load_error_log_for_date(target_day)
        logger.info("load_error_log rebuild: date_utc=%s rows=%d", target_day.isoformat(), rows)
    except Exception as e:
        logger.warning("load_error_log rebuild failed (non-fatal): %s", e)
    # Phase 2 — refresh the adaptive load-bias table from the just-rebuilt error
    # log. Cheap + observable; NO LP effect unless LOAD_RECENT_BIAS_ENABLED.
    try:
        from ..load_bias import refresh_load_recent_bias
        refresh_load_recent_bias()
    except Exception as e:
        logger.warning("load_recent_bias refresh failed (non-fatal): %s", e)


def _evaluate_guests_signal(
    daily: list[tuple[float, float]],
    *,
    window_days: int,
    ratio_thr: float,
    min_days: int,
    day_over: float,
    rearm_ratio: float,
) -> dict[str, Any]:
    """Pure decision logic for the guests-elevation detector.

    ``daily`` = list of (actual_kwh, forecast_kwh) per day, oldest→newest. Uses
    only the trailing ``window_days``. Returns ratio, days_over, and two flags:
    ``elevated`` (sustained above thresholds → suggest guests) and ``normalized``
    (signal back to ~normal → re-arm). Both False when there isn't enough data.
    """
    win = [d for d in daily if d[1] > 0][-window_days:]
    if len(win) < window_days:
        return {"ratio": 0.0, "days_over": 0, "elevated": False, "normalized": False,
                "n": len(win), "avg_actual": 0.0, "avg_forecast": 0.0}
    sa = sum(a for a, _ in win)
    sf = sum(f for _, f in win)
    ratio = sa / sf if sf > 0 else 0.0
    days_over = sum(1 for a, f in win if f > 0 and a > f * day_over)
    elevated = ratio > ratio_thr and days_over >= min_days
    normalized = ratio < rearm_ratio
    return {"ratio": ratio, "days_over": days_over, "elevated": elevated,
            "normalized": normalized, "n": len(win), "avg_actual": sa / len(win),
            "avg_forecast": sf / len(win)}


def bulletproof_guests_detector_job() -> None:
    """Nightly: detect SUSTAINED base-load elevation (possible guests) and PROMPT
    the user to enable the guests preset. Never auto-applies — the household knows
    if there are visitors; auto-applying would chase the noisy load (the rejected
    recency corrector's failure mode). One-shot per episode via a runtime 'armed'
    flag that only re-arms once the signal falls back to ~normal. Best-effort.
    """
    if not getattr(config, "GUESTS_DETECT_ENABLED", True):
        return
    try:
        from ..presets import OperationPreset
        already_guests = OperationPreset(
            (config.OPTIMIZATION_PRESET or "normal").strip().lower()
        ) == OperationPreset.GUESTS
    except (ValueError, AttributeError):
        already_guests = False
    try:
        window = int(config.GUESTS_DETECT_WINDOW_DAYS)
        now = datetime.now(UTC)
        start = now - timedelta(days=window + 1)
        rows = db.get_load_error_log_range(
            start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        by_day: dict[str, list[float]] = {}
        for r in rows:
            d = str(r.get("slot_time_utc") or "")[:10]
            a = r.get("actual_kwh"); f = r.get("forecast_kwh")
            if not d or a is None or f is None:
                continue
            agg = by_day.setdefault(d, [0.0, 0.0])
            agg[0] += float(a); agg[1] += float(f)
        daily = [(by_day[d][0], by_day[d][1]) for d in sorted(by_day)]
        sig = _evaluate_guests_signal(
            daily, window_days=window,
            ratio_thr=float(config.GUESTS_DETECT_RATIO),
            min_days=int(config.GUESTS_DETECT_MIN_DAYS),
            day_over=float(config.GUESTS_DETECT_DAY_OVER),
            rearm_ratio=float(config.GUESTS_DETECT_REARM_RATIO),
        )
        armed = (db.get_runtime_setting("guests_suggestion_armed") or "true").lower() != "false"
        # Re-arm once the signal normalises (also resets after guests turned on).
        if sig["normalized"] or already_guests:
            if not armed:
                db.set_runtime_setting("guests_suggestion_armed", "true")
            return
        if sig["elevated"] and armed:
            pct = (sig["ratio"] - 1.0) * 100.0
            from .. import notifier
            notifier.notify_guests_mode_suggested(
                pct_above=pct, days=int(sig["days_over"]),
                avg_actual_kwh=float(sig["avg_actual"]),
                avg_forecast_kwh=float(sig["avg_forecast"]),
            )
            db.set_runtime_setting("guests_suggestion_armed", "false")
            logger.info(
                "guests detector: SUGGESTED (ratio=%.2f days_over=%d) — prompted user",
                sig["ratio"], sig["days_over"],
            )
        else:
            logger.info(
                "guests detector: ratio=%.2f days_over=%d elevated=%s armed=%s (no prompt)",
                sig["ratio"], sig["days_over"], sig["elevated"], armed,
            )
    except Exception as e:
        logger.warning("guests detector job failed (non-fatal): %s", e)


def _evaluate_lp_health(
    *,
    infeasible_24h: int,
    neg_slot_count: int,
    neg_discharge_kwh: float,
    max_infeasible: int,
    neg_discharge_thr: float,
    floor_insurance_24h_pence: float = 0.0,
    floor_slack_max_kwh: float = 0.0,
    floor_insurance_thr_pence: float = 150.0,
    floor_slack_thr_kwh: float = 0.3,
) -> list[str]:
    """Pure regression check for the LP health monitor. Returns a list of issue
    strings (empty = healthy). Covers an LP-infeasible spike, the #607
    Backup-discharge bug regressing (battery discharging during negative
    prices), and — since PR B (pessimistic charge floor) — the floor costing
    more insurance than it plausibly saves, or going unreachable (slack)."""
    issues: list[str] = []
    if infeasible_24h > max_infeasible:
        issues.append(
            f"LP infeasible {infeasible_24h}x nas últimas 24h (limite {max_infeasible})"
        )
    if neg_slot_count > 0 and neg_discharge_kwh > neg_discharge_thr:
        issues.append(
            f"bateria descarregou {neg_discharge_kwh:.2f} kWh durante {neg_slot_count} "
            f"slots de preço NEGATIVO (deveria ser ~0 — regressão do #607)"
        )
    if floor_insurance_24h_pence > floor_insurance_thr_pence:
        issues.append(
            f"charge floor pessimista custou {floor_insurance_24h_pence:.0f}p de seguro em 24h "
            f"(limite {floor_insurance_thr_pence:.0f}p) — avaliar LP_PESS_CHARGE_FLOOR_ENABLED=false"
        )
    if floor_slack_max_kwh > floor_slack_thr_kwh:
        issues.append(
            f"charge floor pessimista com slack máx {floor_slack_max_kwh:.2f} kWh em 24h "
            f"(limite {floor_slack_thr_kwh:.2f}) — floor inatingível, inputs pess/nominal divergindo"
        )
    return issues


def bulletproof_lp_health_monitor_job() -> None:
    """Nightly self-check for performance regressions from the recent dispatch
    changes (#607/#608/#609/#610). Alerts via Telegram ONLY on regression
    (silent when healthy). Deduped one alert per signature per day. Best-effort.
    """
    if not getattr(config, "LP_HEALTH_MONITOR_ENABLED", True):
        return
    try:
        import hashlib
        from contextlib import closing
        now = datetime.now(UTC)
        since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        with db._lock, closing(db.get_connection()) as conn:  # noqa: SLF001 — read-only counts
            # Count failed solves via the STRUCTURED status column (review of
            # #611): lp_inputs_snapshot.lp_status is written for every solve
            # ('Optimal' / 'Infeasible' / …; NULL only on pre-migration rows).
            # The previous string-match on the human-readable strategy_summary
            # would silently stop watching if the summary wording ever changed —
            # the worst failure mode for a watchdog.
            infeasible_24h = conn.execute(
                "SELECT COUNT(*) FROM lp_inputs_snapshot WHERE run_at_utc >= ? "
                "AND lp_status IS NOT NULL AND lp_status != 'Optimal'",
                (since,),
            ).fetchone()[0]
            yday = (now.date() - timedelta(days=1)).isoformat()
            neg_slots = {
                str(r[0])[11:16]
                for r in conn.execute(
                    "SELECT valid_from FROM agile_rates "
                    "WHERE substr(valid_from,1,10)=? AND value_inc_vat < 0",
                    (yday,),
                ).fetchall()
            }
            # PR B (pessimistic charge floor) observability: aggregate the
            # per-solve insurance cost + slack from the persisted exogenous
            # snapshots so a floor that costs more than it saves (or goes
            # unreachable) pages instead of bleeding silently.
            floor_row = conn.execute(
                "SELECT COALESCE(SUM(json_extract(exogenous_snapshot_json,"
                " '$.pess_charge_floor.insurance_cost_pence')), 0),"
                " COALESCE(MAX(json_extract(exogenous_snapshot_json,"
                " '$.pess_charge_floor.slack_kwh')), 0)"
                " FROM lp_inputs_snapshot WHERE run_at_utc >= ?"
                " AND exogenous_snapshot_json LIKE '%pess_charge_floor%'",
                (since,),
            ).fetchone()
            floor_insurance_24h = float(floor_row[0] or 0.0)
            floor_slack_max = float(floor_row[1] or 0.0)
        neg_discharge = 0.0
        if neg_slots:
            disch = db.half_hourly_battery_discharge_kwh_for_day(now.date() - timedelta(days=1))
            for slot_iso, kwh in disch.items():
                if str(slot_iso)[11:16] in neg_slots:
                    neg_discharge += float(kwh or 0.0)
        issues = _evaluate_lp_health(
            infeasible_24h=int(infeasible_24h),
            neg_slot_count=len(neg_slots),
            neg_discharge_kwh=neg_discharge,
            max_infeasible=int(config.LP_HEALTH_MAX_INFEASIBLE_24H),
            neg_discharge_thr=float(config.LP_HEALTH_NEG_DISCHARGE_KWH),
            floor_insurance_24h_pence=floor_insurance_24h,
            floor_slack_max_kwh=floor_slack_max,
            floor_insurance_thr_pence=float(
                getattr(config, "LP_HEALTH_FLOOR_INSURANCE_24H_PENCE", 150.0)
            ),
            floor_slack_thr_kwh=float(
                getattr(config, "LP_HEALTH_FLOOR_SLACK_KWH", 0.3)
            ),
        )
        if not issues:
            logger.info(
                "LP health monitor: OK (infeasible_24h=%d neg_slots=%d neg_discharge=%.2f)",
                infeasible_24h, len(neg_slots), neg_discharge,
            )
            return
        sig = hashlib.sha1((yday + "|" + "|".join(issues)).encode()).hexdigest()[:12]
        if (db.get_runtime_setting("lp_health_last_alert_sig") or "") == sig:
            logger.info("LP health monitor: regression already alerted today (sig=%s)", sig)
            return
        from .. import notifier
        notifier.notify_lp_health_regression(issues)
        db.set_runtime_setting("lp_health_last_alert_sig", sig)
        logger.warning("LP health monitor: REGRESSION alerted — %s", "; ".join(issues))
    except Exception as e:
        logger.warning("LP health monitor job failed (non-fatal): %s", e)


def bulletproof_export_opportunity_job() -> None:
    """Persist yesterday's export opportunity cost (Outgoing Agile − flat SEG).

    The running tally of money left on the table by being on flat SEG export
    instead of Outgoing Agile — ammunition to push Octopus to switch. Backfills
    the on-Agile history on first run (empty table). Best-effort; a failure must
    not disturb the scheduler.
    """
    from datetime import date as _date

    from ..analytics.pnl import (
        backfill_export_opportunity,
        record_export_opportunity_for_day,
    )
    try:
        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    except Exception:
        tz = UTC  # type: ignore[assignment]
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()
    try:
        existing = db.get_export_opportunity(_date(2000, 1, 1), yesterday)
        if not existing:
            # Seed history. Start at the Agile join date if configured, else 60d.
            start = yesterday - timedelta(days=60)
            cfg_start = (getattr(config, "AGILE_TARIFF_START_DATE", "") or "").strip()
            if cfg_start:
                try:
                    start = max(start, _date.fromisoformat(cfg_start))
                except ValueError:
                    pass
            n = backfill_export_opportunity(start, yesterday)
            logger.info("export_opportunity backfill: %d days from %s", n, start.isoformat())
        else:
            record_export_opportunity_for_day(yesterday)
            logger.info("export_opportunity recorded for %s", yesterday.isoformat())
    except Exception as e:
        logger.warning("export_opportunity job failed (non-fatal): %s", e)


def bulletproof_pv_calibration_refresh_job() -> None:
    """Recompute the per-hour and per-(hour, cloud-bucket) PV calibration
    tables from fresh ``pv_realtime_history`` and ``meteo_forecast_value``
    rows.

    The LP reads these tables to translate Open-Meteo / Quartz forecast PV
    into the calibrated per-slot expectation. Without a regular refresh the
    tables drift stale (audit 2026-05-15: 7 days stale, 14-day window —
    PM under-prediction band shifted by 25% in the missed week and the LP
    over-grid-charged in the mornings as a result).

    Best-effort: a failure here must not interfere with the rest of the
    scheduler. Failures are logged and the LP falls back to the previous
    table contents (or to the flat ``compute_pv_calibration_factor()`` if
    both tables are empty).
    """
    from ..weather import (
        compute_pv_calibration_hourly_table,
        compute_pv_calibration_hourly_cloud_table,
    )
    try:
        result_h = compute_pv_calibration_hourly_table()
        logger.info("pv_calibration_hourly refresh: %s", result_h)
    except Exception as e:
        logger.warning("pv_calibration_hourly refresh failed (non-fatal): %s", e)
    try:
        result_c = compute_pv_calibration_hourly_cloud_table()
        logger.info("pv_calibration_hourly_cloud refresh: %s", result_c)
    except Exception as e:
        logger.warning("pv_calibration_hourly_cloud refresh failed (non-fatal): %s", e)


def bulletproof_night_brief_job() -> None:
    """Daily night digest — today's actuals (V12). Companion to morning brief."""
    from ..analytics.daily_brief import send_night_brief_webhook

    try:
        send_night_brief_webhook()
    except Exception as e:
        logger.warning("Night brief failed: %s", e)


def bulletproof_mpc_job(
    *,
    force_write_devices: bool = False,
    trigger_reason: str = "manual",
    bypass_cooldown: bool = False,
) -> bool:
    """Intra-day MPC re-optimise: refresh forecast + live SoC + live PV, re-upload Fox/Daikin.

    Returns True only when a solve actually completed (``result.ok``); False on
    every skip path (engine off, paused, cooldown, dispatch lock) and on solve
    failure — callers that must not lose a re-plan (#691 export-gap retry) use
    this to keep the trigger armed instead of assuming the call did work.

    Reads Fox realtime (SoC%, solar_power_kw, load_power_kw) and passes them into the LP
    initial state so the re-optimisation reflects the actual current energy state rather than
    yesterday's estimate.  Only runs when USE_BULLETPROOF_ENGINE=true and OPTIMIZER_BACKEND=lp.
    Skips if the scheduler is paused.

    ``force_write_devices`` (default False): event-driven callers (drift, forecast revision,
    Octopus fetch, tier_boundary) set this True to override ``LP_MPC_WRITE_DEVICES`` and
    dispatch directly to the hardware — coherent with "Waze recalculating route" semantics.

    ``trigger_reason`` (default "manual"): tags the run for observability. Known reasons:
    ``octopus_fetch``, ``tier_boundary``, ``soc_drift``, ``forecast_revision``,
    ``pv_upside``, ``pv_downside``, ``load_upside``, ``dynamic_replan``,
    ``plan_push``, ``appliance_armed``, ``manual``. The legacy ``cron`` value
    is gone (V12).

    ``bypass_cooldown`` (default False): skip the ``MPC_COOLDOWN_SECONDS``
    gate. Reserved for discrete user gestures (e.g. ``appliance_armed``) that
    fire only on a real state transition and so can't thrash — the user
    deserves prompt feedback rather than waiting out the drift cooldown.
    """
    global _last_mpc_run_at

    if not config.USE_BULLETPROOF_ENGINE:
        return False
    if get_scheduler_paused():
        return False
    backend = (config.OPTIMIZER_BACKEND or "lp").strip().lower()
    if backend != "lp":
        logger.debug("MPC skipped: OPTIMIZER_BACKEND=%s", backend)
        return False
    if not bypass_cooldown and not _can_run_mpc_now():
        logger.info(
            "MPC skipped (cooldown, trigger=%s): last run %.0fs ago < %ds",
            trigger_reason,
            (datetime.now(UTC) - _last_mpc_run_at).total_seconds() if _last_mpc_run_at else 0,
            int(config.MPC_COOLDOWN_SECONDS),
        )
        return False

    # #676 — serialize with any in-flight solve+dispatch. Non-blocking: an
    # event trigger arriving while another solve is mid-flight is redundant
    # (the in-flight solve already reads the freshest state), so skip —
    # mirroring the cooldown-skip semantics above.
    if not optimizer_dispatch_lock.acquire(blocking=False):
        logger.info(
            "MPC skipped (already running, trigger=%s): another optimizer run holds the dispatch lock",
            trigger_reason,
        )
        return False
    solved = False
    try:
        write_devices = bool(config.LP_MPC_WRITE_DEVICES) or force_write_devices
        # Snapshot the previous LP run id BEFORE the new solve so we can compute the plan delta.
        prev_run_id: int | None = None
        try:
            prev_run_id = db.find_run_for_time(datetime.now(UTC).isoformat())
        except Exception as e:
            logger.debug("plan-delta: prev_run_id lookup failed: %s", e)

        try:
            from .optimizer import run_optimizer

            fox = _try_fox()
            daikin = None
            if config.DAIKIN_CLIENT_ID and config.DAIKIN_CLIENT_SECRET:
                try:
                    from ..daikin.client import DaikinClient

                    daikin = DaikinClient()
                except Exception as e:
                    logger.debug("MPC: Daikin client unavailable: %s", e)        # --- Read live Fox realtime: SoC, solar_power_kw, load_power_kw ---
            rt_soc_pct: float | None = None
            rt_solar_kw: float | None = None
            rt_load_kw: float | None = None
            try:
                rt = get_cached_realtime()
                rt_soc_pct = float(rt.soc) if rt.soc is not None else None
                rt_solar_kw = float(rt.solar_power) if rt.solar_power is not None else None
                rt_load_kw = float(rt.load_power) if rt.load_power is not None else None
                logger.info(
                    "MPC live snapshot: SoC=%.1f%% solar=%.2fkW load=%.2fkW",
                    rt_soc_pct or 0,
                    rt_solar_kw or 0,
                    rt_load_kw or 0,
                )
            except Exception as e:
                logger.debug("MPC: Fox realtime unavailable (will use DB state): %s", e)

            # Store live snapshot in DB so the LP initial state reader picks it up
            if rt_soc_pct is not None:
                try:
                    from .. import db as _db

                    _db.upsert_fox_realtime_snapshot(
                        {
                            "captured_at": datetime.now(UTC).isoformat(),
                            "soc_pct": rt_soc_pct,
                            "solar_power_kw": rt_solar_kw,
                            "load_power_kw": rt_load_kw,
                        }
                    )
                except Exception as e:
                    logger.debug("MPC: snapshot upsert failed (non-fatal): %s", e)

            result = run_optimizer(
                fox if write_devices else None,
                daikin if write_devices else None,
                trigger_reason=trigger_reason,
            )
            logger.info(
                "MPC re-optimise: trigger=%s ok=%s lp_status=%s objective=%.0fp soc=%.1f%% solar=%.2fkW write_devices=%s",
                trigger_reason,
                result.get("ok"),
                result.get("lp_status"),
                result.get("lp_objective_pence", 0),
                rt_soc_pct or 0,
                rt_solar_kw or 0,
                write_devices,
            )
            # Stamp the cooldown only on a successful solve so transient errors don't lock us out.
            if result.get("ok"):
                solved = True
                _last_mpc_run_at = datetime.now(UTC)
                # Plan-delta observability for event-driven runs (logged only —
                # the user-facing ping is the digest pair + nightly plan_proposed;
                # in-day deltas are pulled via get_plan_timeline / dispatch_decisions).
                try:
                    new_run_id = db.find_run_for_time(_last_mpc_run_at.isoformat())
                    _log_plan_delta_after_trigger(prev_run_id, new_run_id, trigger_reason)
                except Exception as e:
                    logger.debug("plan-delta post-run hook failed: %s", e)
        except Exception as e:
            logger.warning("MPC job failed (trigger=%s): %s", trigger_reason, e)
    finally:
        optimizer_dispatch_lock.release()
    return solved


def _register_tier_boundary_triggers() -> dict[str, Any]:
    """Schedule one-shot MPC re-plans before every tariff tier transition
    in today + tomorrow's Octopus rates.

    Reuses :func:`src.google_calendar.tiers.classify_day` (the same tier
    classifier the family-calendar publisher uses) so the boundaries match
    word-for-word what the user sees on the calendar.

    Each fire calls ``bulletproof_mpc_job(force_write_devices=True,
    trigger_reason="tier_boundary")`` ``TIER_BOUNDARY_LEAD_MINUTES`` minutes
    BEFORE the window starts, giving the LP fresh data with enough lead time
    to upload a new Fox V3 plan before the tariff actually shifts.

    Idempotent: each window gets a unique APScheduler job id derived from its
    start_utc so re-registration after every Octopus fetch overwrites cleanly.

    Returns a status dict for tests + observability. Never raises — failures
    here must not break the caller (Octopus fetch + lifespan startup).
    """
    out: dict[str, Any] = {"scheduled": [], "skipped": []}
    if _background_scheduler is None:
        out["status"] = "inactive"
        return out
    if get_scheduler_paused():
        out["status"] = "paused"
        return out
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        out["status"] = "no_tariff"
        return out

    try:
        from apscheduler.triggers.date import DateTrigger
        from ..google_calendar.tiers import Slot, classify_day
    except Exception as e:
        out["status"] = "import_error"
        out["error"] = str(e)
        return out

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    today_local = datetime.now(tz).date()
    lead = timedelta(minutes=int(config.TIER_BOUNDARY_LEAD_MINUTES))
    now_utc = datetime.now(UTC)
    # V12 audit: was reusing DYNAMIC_REPLAN_MIN_LEAD_MINUTES=120 which silently
    # dropped any tier transition within 2 h of "now" — most of the day's
    # transitions on a typical Octopus Agile profile. Tier-boundary fires
    # only need enough lead to actually solve + upload (≤ 1 min covers it).
    min_lead = timedelta(minutes=int(config.TIER_BOUNDARY_MIN_LEAD_MINUTES))

    # First sweep: drop any prior tier_boundary jobs so a re-registration
    # after fresh Octopus fetch doesn't leave stale fires from yesterday.
    for job in list(_background_scheduler.get_jobs()):
        if job.id.startswith("tier_boundary_"):
            try:
                _background_scheduler.remove_job(job.id)
            except Exception as e:  # pragma: no cover — best-effort
                logger.debug("tier_boundary remove_job(%s) failed: %s", job.id, e)

    # Re-register for today + tomorrow.
    for day_offset in (0, 1):
        local_date = today_local + timedelta(days=day_offset)
        try:
            rows = db.get_agile_rates_slots_for_local_day(tariff, local_date, tz_name=str(tz))
        except Exception as e:
            logger.debug("tier_boundary: get_agile_rates_slots_for_local_day(%s) failed: %s", local_date, e)
            continue
        if not rows:
            out["skipped"].append({"date": local_date.isoformat(), "reason": "no_rates"})
            continue
        slots = [
            Slot(
                start_utc=datetime.fromisoformat(str(r["valid_from"]).replace("Z", "+00:00")),
                end_utc=datetime.fromisoformat(str(r["valid_to"]).replace("Z", "+00:00")),
                price_p=float(r["value_inc_vat"]),
            )
            for r in rows
        ]
        windows = classify_day(slots)
        for w in windows:
            fire_at = w.start_utc - lead
            if fire_at - now_utc < min_lead:
                # In the past or below the lead-time floor — skip silently.
                continue
            job_id = f"tier_boundary_{int(w.start_utc.timestamp())}"
            try:
                _background_scheduler.add_job(
                    bulletproof_mpc_job,
                    DateTrigger(run_date=fire_at),
                    id=job_id,
                    replace_existing=True,
                    kwargs={"force_write_devices": True, "trigger_reason": "tier_boundary"},
                )
                out["scheduled"].append({
                    "fire_at_utc": fire_at.isoformat(),
                    "window_start_utc": w.start_utc.isoformat(),
                    "tier": w.tier.key,
                    "job_id": job_id,
                })
            except Exception as e:  # pragma: no cover — best-effort
                logger.debug("tier_boundary add_job(%s) failed: %s", job_id, e)

    if out["scheduled"]:
        logger.info(
            "Tier-boundary triggers registered: %d job(s); next fire %s",
            len(out["scheduled"]),
            out["scheduled"][0]["fire_at_utc"],
        )
    out["status"] = "ok"
    return out


def schedule_dynamic_mpc_replan(replan_at_utc: datetime) -> dict[str, Any]:
    """Schedule a one-shot MPC re-plan to fire shortly before ``replan_at_utc``.

    Used when the LP plan exceeded the Fox V3 8-group cap and was truncated:
    the truncated tail must be re-planned before the last surviving window
    runs out, otherwise the inverter would idle in SelfUse with no fresh plan.

    Returns a status dict for callers/tests; never raises. The job uses a fixed
    id (``dynamic_mpc_replan``) with ``replace_existing=True`` so back-to-back
    overflow plans don't pile up multiple one-shots.

    Skipped (no-op) when:
    - The scheduler is not running (returns ``status="inactive"``).
    - The scheduler is paused.
    - Lead time is below ``DYNAMIC_REPLAN_MIN_LEAD_MINUTES`` (avoids hammering).
    - A cron-scheduled MPC fire already falls inside ``[now, replan_at]``.
    """
    out: dict[str, Any] = {"replan_at_utc": replan_at_utc.isoformat()}
    if _background_scheduler is None:
        out["status"] = "inactive"
        return out
    if get_scheduler_paused():
        out["status"] = "paused"
        return out

    now_utc = datetime.now(UTC)
    margin = timedelta(minutes=int(config.REPLAN_SAFETY_MARGIN_MINUTES))
    fire_at_utc = replan_at_utc - margin
    lead = (fire_at_utc - now_utc).total_seconds() / 60.0
    out["fire_at_utc"] = fire_at_utc.isoformat()
    out["lead_minutes"] = round(lead, 1)

    if lead < float(config.DYNAMIC_REPLAN_MIN_LEAD_MINUTES):
        out["status"] = "skipped_lead_too_short"
        return out

    # V12: the legacy cron-overlap dedup is gone with the fixed-hour cron.
    # Tier-boundary fires use unique per-window job ids so they don't
    # collide with this dynamic replan; if both happen to land within
    # MPC_COOLDOWN_SECONDS the cooldown gate handles it.

    try:
        from apscheduler.triggers.date import DateTrigger

        _background_scheduler.add_job(
            bulletproof_mpc_job,
            DateTrigger(run_date=fire_at_utc),
            id="dynamic_mpc_replan",
            replace_existing=True,
            kwargs={"force_write_devices": True, "trigger_reason": "dynamic_replan"},
        )
        out["status"] = "scheduled"
        logger.info(
            "Dynamic MPC replan scheduled at %s (lead %.0fm before plan tail at %s)",
            fire_at_utc.isoformat(),
            lead,
            replan_at_utc.isoformat(),
        )
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)
        logger.warning("Dynamic MPC replan scheduling failed: %s", e)
    return out


def bulletproof_forecast_refresh_job() -> None:
    """Hourly Open-Meteo forecast refresh + revision-trigger detector (Epic #73 — story #144).

    Pulls the latest forecast, persists in ``meteo_forecast_history`` (audit trail) and
    ``meteo_forecast`` (latest-per-slot for the LP). Compares the next
    ``MPC_FORECAST_DRIFT_LOOKAHEAD_HOURS`` against the previous fetch; if either solar
    or temp delta exceeds threshold, fires ``bulletproof_mpc_job(force_write_devices=True,
    trigger_reason='forecast_revision')`` to re-plan immediately.

    Skipped (no-op) when the scheduler is paused or the kill switch is off. Always
    persists the new fetch — even when the kill switch is off, the audit trail
    (and the LP's source of forecast data) stays current.
    """
    if get_scheduler_paused():
        return
    try:
        from .. import db as _db
        from ..weather import _forecast_delta, fetch_forecast_snapshot

        lookahead_h = int(config.MPC_FORECAST_DRIFT_LOOKAHEAD_HOURS)
        # Pull a forecast at least as long as the lookahead window we'll compare on,
        # but cap reasonable: Open-Meteo gives 48h easily.
        forecast_fetch = fetch_forecast_snapshot(hours=max(lookahead_h, 24))
        new_fcst = forecast_fetch.forecast
        if not new_fcst:
            logger.debug("forecast refresh: empty fetch, skipping")
            return
        now_utc = datetime.now(UTC)
        new_rows = [
            {
                "slot_time": f.time_utc.isoformat(),
                "temp_c": f.temperature_c,
                "solar_w_m2": f.shortwave_radiation_wm2,
                "cloud_cover_pct": f.cloud_cover_pct,
                "direct_pv_kw": f.estimated_pv_kw if getattr(f, "pv_direct", False) else None,
            }
            for f in new_fcst
        ]
        prev_rows = _db.get_meteo_forecast_history_latest_before(now_utc.isoformat())
        # Persist once into the canonical forecast snapshot store and mark it latest.
        _db.save_meteo_forecast_snapshot(
            now_utc.isoformat(),
            new_rows,
            source=forecast_fetch.source,
            model_name=forecast_fetch.model_name,
            model_version=forecast_fetch.model_version,
            raw_payload_json=forecast_fetch.raw_payload_json,
            mark_latest=True,
        )

        if not config.MPC_EVENT_DRIVEN_ENABLED:
            logger.debug("forecast refresh persisted; trigger disabled by kill switch")
            return
        if not prev_rows:
            logger.debug("forecast refresh: no previous fetch in history, no comparison")
            return
        delta_pv_kwh, delta_temp_c = _forecast_delta(
            prev_rows, new_rows, lookahead_hours=lookahead_h, horizon_start_utc=now_utc,
        )
        pv_thr = float(config.MPC_FORECAST_DRIFT_SOLAR_KWH_THRESHOLD)
        t_thr = float(config.MPC_FORECAST_DRIFT_TEMP_C_THRESHOLD)
        if delta_pv_kwh >= pv_thr or delta_temp_c >= t_thr:
            logger.info(
                "MPC forecast trigger: ΔPV=%.2f kWh (>=%.1f) ΔT=%.2f°C (>=%.1f) over next %dh",
                delta_pv_kwh, pv_thr, delta_temp_c, t_thr, lookahead_h,
            )
            bulletproof_mpc_job(force_write_devices=True, trigger_reason="forecast_revision")
        else:
            logger.debug(
                "forecast refresh delta below thresholds: ΔPV=%.2f kWh ΔT=%.2f°C",
                delta_pv_kwh, delta_temp_c,
            )
    except Exception as e:
        logger.warning("Forecast refresh job failed: %s", e)


def bulletproof_pv_telemetry_job() -> None:
    """Per-N-min sample of Fox realtime → ``pv_realtime_history`` for PV calibration.

    Reads the heartbeat-cached realtime (zero Fox quota cost) and appends one row.
    Runs in parallel to the heartbeat to avoid coupling cadences. Idempotent:
    duplicate ``captured_at`` is silently dropped by the table's PRIMARY KEY.
    """
    if get_scheduler_paused():
        return
    try:
        # This job is the canonical Fox-snapshot refresher: force a read older than
        # FOX_SNAPSHOT_REFRESH_MAX_AGE_SECONDS so the cockpit serves a snapshot fresh
        # to the telemetry cadence (PV_TELEMETRY_INTERVAL_MINUTES) WITHOUT the API
        # reads ever fetching on the request path (they use the longer default TTL).
        rt = get_cached_realtime(
            max_age_seconds=int(config.FOX_SNAPSHOT_REFRESH_MAX_AGE_SECONDS)
        )
    except Exception as e:
        logger.debug("pv telemetry: realtime unavailable: %s", e)
        return
    if rt.soc is None and rt.solar_power is None:
        return  # nothing meaningful to persist
    try:
        db.save_pv_realtime_sample(
            datetime.now(UTC).isoformat(),
            solar_power_kw=float(rt.solar_power) if rt.solar_power is not None else None,
            soc_pct=float(rt.soc) if rt.soc is not None else None,
            load_power_kw=float(rt.load_power) if rt.load_power is not None else None,
            grid_import_kw=float(rt.grid_power) if rt.grid_power is not None and rt.grid_power > 0 else None,
            grid_export_kw=float(-rt.grid_power) if rt.grid_power is not None and rt.grid_power < 0 else None,
            battery_charge_kw=float(rt.battery_power) if rt.battery_power is not None and rt.battery_power > 0 else None,
            battery_discharge_kw=float(-rt.battery_power) if rt.battery_power is not None and rt.battery_power < 0 else None,
            source="heartbeat",
        )
    except Exception as e:
        logger.debug("pv telemetry: save failed (non-fatal): %s", e)


def bulletproof_viewer_boost_job() -> None:
    """Every 30 s: while someone is watching the cockpit, keep the Fox/Daikin
    caches fresher than their idle baselines — without ever competing with
    control traffic for quota.

    * Viewer signal: /cockpit/now hits (SPA polls 20 s, pauses hidden tabs) —
      see ``src/viewer_activity.py``. No viewer → immediate no-op, so the idle
      cost of this job is a monotonic-clock comparison.
    * Fox: refresh when the realtime cache is older than
      FOX_VIEWER_REFRESH_SECONDS (idle baseline = PV telemetry cadence, 3 min).
    * Daikin: refresh when the device cache is older than
      DAIKIN_VIEWER_REFRESH_SECONDS (idle baseline = opportunistic only, so
      the cockpit's tank/indoor temps could sit 30-60+ min stale).
    * Both gated on quota_remaining > *_VIEWER_QUOTA_RESERVE: when the day's
      budget runs low the boost stops first, and plan pushes / reconciler
      writes / LP reads keep their headroom. Cache-warm calls cost nothing —
      get_cached_realtime/get_cached_devices only hit the wire past max_age.
    """
    if not getattr(config, "VIEWER_BOOST_ENABLED", True):
        return
    if get_scheduler_paused():
        return
    from ..viewer_activity import viewer_active
    if not viewer_active(float(config.VIEWER_ACTIVE_WINDOW_SECONDS)):
        return
    from ..api_quota import quota_remaining

    fox_target = int(config.FOX_VIEWER_REFRESH_SECONDS)
    if fox_target > 0:
        try:
            if quota_remaining("fox") > int(config.FOX_VIEWER_QUOTA_RESERVE):
                get_cached_realtime(max_age_seconds=fox_target)
        except Exception as e:
            logger.debug("viewer boost: fox refresh failed (non-fatal): %s", e)

    daikin_target = int(config.DAIKIN_VIEWER_REFRESH_SECONDS)
    if daikin_target > 0:
        try:
            if quota_remaining("daikin") > int(config.DAIKIN_VIEWER_QUOTA_RESERVE):
                # Gate on cache AGE, not on the service's freshness verdict:
                # every control write sets the stale flag
                # (invalidate_after_write), and an allow_refresh=True call
                # would refetch on the next 30 s tick regardless of
                # max_age_seconds — turning each reconciler write into an
                # extra read. The boost only ever pays for age; the
                # reconciler's own reads handle post-write coherence.
                cached = daikin_service.get_cached_devices(
                    allow_refresh=False, actor="viewer_boost"
                )
                if cached.age_seconds > daikin_target:
                    daikin_service.get_cached_devices(
                        allow_refresh=True,
                        max_age_seconds=daikin_target,
                        actor="viewer_boost",
                    )
        except Exception as e:
            logger.debug("viewer boost: daikin refresh failed (non-fatal): %s", e)


def _hhmm_to_minutes(s: str) -> int:
    parts = (s or "00:00").strip().split(":")
    h = int(parts[0]) if parts else 0
    m = int(parts[1]) if len(parts) > 1 else 0
    return h * 60 + m


def _prune_comfort_morning_keys() -> None:
    global _comfort_morning_logged
    if len(_comfort_morning_logged) <= 120:
        return
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    _comfort_morning_logged = {k for k in _comfort_morning_logged if k[:10] >= cutoff}


def _maybe_log_comfort_morning_check(
    *,
    now_local: datetime,
    now_utc: datetime,
    plan_date: str,
    room_t: float | None,
    soc: float | None,
    fox_mode: str | None,
    outdoor_t: float | None,
    lwt_off: float | None,
    tank_t: float | None,
    tank_tgt: float | None,
    tank_on: bool,
    dev0: Any,
) -> None:
    global _comfort_morning_logged
    if room_t is None or not dev0:
        return
    cur = now_local.hour * 60 + now_local.minute
    sp = float(config.INDOOR_SETPOINT_C)
    for slot_kind, hhmm in (
        ("occupied_morning_start", config.LP_OCCUPIED_MORNING_START),
        ("occupied_morning_end", config.LP_OCCUPIED_MORNING_END),
    ):
        m0 = _hhmm_to_minutes(hhmm)
        if m0 - 2 <= cur < m0 + 8:
            key = f"{plan_date}_{slot_kind}"
            if key in _comfort_morning_logged:
                continue
            _comfort_morning_logged.add(key)
            _prune_comfort_morning_keys()
            fc = _get_forecast_temp_c(now_utc)
            db.log_execution(
                {
                    "timestamp": now_utc.isoformat(),
                    "consumption_kwh": None,
                    "agile_price_pence": None,
                    "svt_shadow_price_pence": None,
                    "fixed_shadow_price_pence": None,
                    "cost_realised_pence": None,
                    "cost_svt_shadow_pence": None,
                    "cost_fixed_shadow_pence": None,
                    "delta_vs_svt_pence": None,
                    "delta_vs_fixed_pence": None,
                    "soc_percent": soc,
                    "fox_mode": fox_mode,
                    "daikin_lwt_offset": lwt_off,
                    "daikin_tank_temp": tank_t,
                    "daikin_tank_target": tank_tgt,
                    "daikin_tank_power_on": 1 if tank_on else 0,
                    "daikin_powerful_mode": None,
                    "daikin_room_temp": room_t,
                    "daikin_outdoor_temp": outdoor_t,
                    "daikin_lwt": dev0.leaving_water_temperature,
                    "forecast_temp_c": fc or outdoor_t,
                    "forecast_solar_kw": None,
                    "forecast_heating_demand": None,
                    "slot_kind": slot_kind,
                    "source": "comfort_check",
                }
            )
            logger.info(
                "Comfort check (%s): room=%.2f°C setpoint=%.2f°C",
                slot_kind,
                room_t,
                sp,
            )


def _daily_history_prune_job() -> None:
    """Run the retention policy for append-only history tables.

    Scheduled by :func:`start_background_scheduler` at 03:15 UTC daily.
    Also runs on every service startup via the FastAPI lifespan hook —
    the cron is insurance for long-uptime deploys.
    """
    try:
        results = db.prune_history_tables()
        interesting = {k: v for k, v in results.items() if v != 0}
        if interesting:
            logger.info("daily history prune: %s", interesting)
    except Exception:
        logger.warning("daily history prune failed", exc_info=True)

    # Sensor-data lifecycle (#540): WARM rollup + COLD archive-before-prune for
    # the room-sensor tables (they have no retention in prune_history_tables —
    # they're tiered here so the full-res raw is gzip-archived before deletion).
    try:
        from ..analytics.data_archival import run_sensor_data_lifecycle
        lc = run_sensor_data_lifecycle()
        logger.info("sensor data lifecycle: %s", lc)
    except Exception:
        logger.warning("sensor data lifecycle failed", exc_info=True)


def bulletproof_calendar_publish_job() -> None:
    """Publish Octopus rate windows to the family Google Calendar.

    Side feature, fully isolated from LP/dispatch: APScheduler runs each job
    in its own context, so an exception here cannot affect Octopus fetch,
    MPC, or hardware writes. Idempotent — re-runs are no-ops when prices
    haven't changed (the publisher diffs against the ``calendar_events``
    table before touching the API).
    """
    if not config.GOOGLE_CALENDAR_ENABLED:
        return
    try:
        from ..google_calendar.publisher import publish_horizon

        result = publish_horizon()
        logger.info("Google Calendar publish: %s", result)
    except Exception:
        logger.warning("Google Calendar publish failed", exc_info=True)


def bulletproof_plan_push_job() -> None:
    """Nightly plan dispatch: push tomorrow's LP plan to Fox ESS + Daikin at LP_PLAN_PUSH_HOUR:MINUTE.

    Re-solves the LP using rates already in DB (fast — no Octopus API call), then uploads
    Fox Scheduler V3 groups and writes Daikin action_schedule entries.  Runs just before
    midnight so devices are programmed before the first slot starts at 00:00.
    """
    if not config.USE_BULLETPROOF_ENGINE:
        return
    if get_scheduler_paused():
        logger.info("Plan push skipped: scheduler paused")
        return
    backend = (config.OPTIMIZER_BACKEND or "lp").strip().lower()
    if backend != "lp":
        logger.info("Plan push skipped: OPTIMIZER_BACKEND=%s (LP only)", backend)
        return
    # #676 — BLOCKING acquire, deliberately asymmetric with bulletproof_mpc_job:
    # the nightly plan push is the canonical commitment of tomorrow's plan and
    # must never be silently skipped. If an event-driven solve is in flight we
    # wait for it to finish (bounded ~100s since #673), then run. The timeout
    # is a wedge guard: if the lock is still held after 600s the in-flight
    # solve is stuck, and proceeding unserialized is the lesser evil vs losing
    # the nightly commitment.
    acquired = optimizer_dispatch_lock.acquire(timeout=float(PLAN_PUSH_LOCK_TIMEOUT_SECONDS))
    if not acquired:
        logger.warning(
            "plan_push proceeding WITHOUT the dispatch lock after %.0fs wait — "
            "in-flight solve appears wedged",
            float(PLAN_PUSH_LOCK_TIMEOUT_SECONDS),
        )
    try:
        try:
            from .optimizer import run_optimizer

            fox = _try_fox()
            daikin = None
            if config.DAIKIN_CLIENT_ID and config.DAIKIN_CLIENT_SECRET:
                try:
                    from ..daikin.client import DaikinClient
                    daikin = DaikinClient()
                except Exception as e:
                    logger.debug("Plan push: Daikin client unavailable: %s", e)

            result = run_optimizer(fox, daikin, trigger_reason="plan_push")
            logger.info(
                "Plan push: ok=%s lp_status=%s objective=%.0fp fox_uploaded=%s daikin_actions=%s",
                result.get("ok"),
                result.get("lp_status"),
                result.get("lp_objective_pence", 0),
                result.get("fox_uploaded"),
                result.get("daikin_actions"),
            )
        except Exception as e:
            logger.warning("Plan push job failed: %s", e)
    finally:
        # Release ONLY if we actually acquired — after a wedge-guard timeout
        # the lock still belongs to the stuck solve.
        if acquired:
            optimizer_dispatch_lock.release()


def bulletproof_heartbeat_tick() -> None:
    """Heartbeat monitor (every HEARTBEAT_INTERVAL_SECONDS, default 5 min): Daikin schedule execution, telemetry, Fox flag check."""
    global _last_exec_halfhour_key, _last_fox_verify_monotonic, _last_room_temp, _last_room_wall_utc, _last_notified_slot_kind, _last_notified_slot_kind_loaded
    import time

    if not config.USE_BULLETPROOF_ENGINE:
        return
    if get_scheduler_paused():
        return

    # PR Phase A — heartbeat NEVER calls Daikin API (#306 follow-up).
    # The Onecta 200-call/day quota is too tight to spend any of it on the
    # heartbeat's monitoring path. Token prefetch removed (auth.py refreshes
    # lazily on next 401). Device cache refresh forced off (allow_refresh=False).
    # Cache stays warm via:
    #   - Plan dispatch (LP → Daikin write events)
    #   - Twice-daily briefs (08:00, 22:00 local — read Daikin state)
    #   - Slot-boundary reconciliation below (cache-only diff check)
    #   - Manual MCP calls
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now_local = datetime.now(tz)
    now_utc = datetime.now(UTC)
    _last_room_wall_utc = now_utc
    plan_date = now_local.date().isoformat()
    mon = time.monotonic()

    fox = _try_fox()
    daikin_result = None
    devices = []
    if config.DAIKIN_CLIENT_ID and config.DAIKIN_CLIENT_SECRET and config.DAIKIN_TOKEN_FILE.exists():
        try:
            daikin_result = daikin_service.get_cached_devices(
                allow_refresh=False,
                actor="heartbeat",
            )
            devices = daikin_result.devices
        except Exception as e:
            logger.debug("Daikin heartbeat cache read skip: %s", e)
            devices = []

    soc = None
    fox_mode = None
    rt_solar_kw: float | None = None
    rt_load_kw: float | None = None
    try:
        rt = get_cached_realtime()
        soc = rt.soc
        fox_mode = rt.work_mode
        rt_solar_kw = float(rt.solar_power) if rt.solar_power is not None else None
        rt_load_kw = float(rt.load_power) if rt.load_power is not None else None
    except Exception:
        pass
    if not fox_mode or fox_mode == "unknown":
        # #669: Fox's realtime query never returns a workMode variable for the
        # H1 series, so rt.work_mode is ALWAYS "unknown". Derive the mode from
        # the persisted uploaded schedule instead (local DB read, zero quota) —
        # labelled "schedule:<mode>" to stay honest about the source.
        fox_mode = derive_fox_mode_from_schedule(now_local)

    # Event-driven MPC: SoC drift trigger (Epic #73 — story #106).
    # Fire bulletproof_mpc_job when live SoC diverges from the LP-predicted trajectory
    # by more than MPC_DRIFT_SOC_THRESHOLD_PERCENT, sustained for MPC_DRIFT_HYSTERESIS_TICKS
    # consecutive heartbeats. Bypasses the cron OCTOPUS_FETCH_HOUR skip (it's an event,
    # not a cron tick) but still gated by the global cooldown inside bulletproof_mpc_job.
    if config.MPC_EVENT_DRIVEN_ENABLED and soc is not None:
        try:
            global _consecutive_drift_ticks
            predicted_pct = _lp_predicted_soc_pct_at(now_utc)
            if predicted_pct is not None:
                drift_pct = abs(float(soc) - predicted_pct)
                threshold = float(config.MPC_DRIFT_SOC_THRESHOLD_PERCENT)
                # Directional gate (2026-07-02 window audit): SoC AHEAD of the
                # prediction is early arrival, not drift, when the plan itself
                # reaches the live level within the look-ahead — Fox executes
                # ForceCharge staging faster than the LP's per-slot taper, and
                # re-solving every heartbeat produced 5-min upload bursts whose
                # group swaps caused mid-fill glitches (battery discharging at
                # negative prices). SoC BELOW prediction always counts.
                global _consecutive_ahead_suppressed
                if (
                    config.MPC_DRIFT_AHEAD_SUPPRESS_ENABLED
                    and drift_pct >= threshold
                    and float(soc) > predicted_pct
                ):
                    plan_max = _lp_predicted_soc_max_pct_within(
                        now_utc, float(config.MPC_DRIFT_AHEAD_LOOKAHEAD_HOURS)
                    )
                    if (
                        plan_max is not None
                        and plan_max >= float(soc) - threshold
                        and _consecutive_ahead_suppressed
                        < int(config.MPC_DRIFT_AHEAD_MAX_SUPPRESSED_TICKS)
                    ):
                        _consecutive_ahead_suppressed += 1
                        logger.debug(
                            "MPC drift suppressed (ahead-of-plan %d/%d): real=%.1f%% "
                            "predicted=%.1f%% plan reaches %.1f%% within %.1fh",
                            _consecutive_ahead_suppressed,
                            int(config.MPC_DRIFT_AHEAD_MAX_SUPPRESSED_TICKS),
                            soc, predicted_pct, plan_max,
                            float(config.MPC_DRIFT_AHEAD_LOOKAHEAD_HOURS),
                        )
                        _consecutive_drift_ticks = 0
                        drift_pct = 0.0
                else:
                    _consecutive_ahead_suppressed = 0
                if drift_pct >= threshold:
                    _consecutive_drift_ticks += 1
                    if _consecutive_drift_ticks >= int(config.MPC_DRIFT_HYSTERESIS_TICKS):
                        logger.info(
                            "MPC drift trigger: real=%.1f%% predicted=%.1f%% drift=%.1f%% (>=%.1f%% for %d ticks)",
                            soc,
                            predicted_pct,
                            drift_pct,
                            threshold,
                            _consecutive_drift_ticks,
                        )
                        _consecutive_drift_ticks = 0
                        bulletproof_mpc_job(
                            force_write_devices=True,
                            trigger_reason="soc_drift",
                        )
                    else:
                        logger.debug(
                            "MPC drift building: drift=%.1f%% (%d/%d ticks)",
                            drift_pct,
                            _consecutive_drift_ticks,
                            int(config.MPC_DRIFT_HYSTERESIS_TICKS),
                        )
                else:
                    if _consecutive_drift_ticks > 0:
                        logger.debug(
                            "MPC drift recovered: drift=%.1f%% < %.1f%% (resetting %d ticks)",
                            drift_pct,
                            threshold,
                            _consecutive_drift_ticks,
                        )
                    _consecutive_drift_ticks = 0
        except Exception as e:
            logger.debug("drift-trigger check failed (non-fatal): %s", e)

    # Event-driven MPC: import_overshoot trigger.
    # Compare actual grid import in the LAST COMPLETED half-hour slot vs the
    # LP plan for that same slot. If actual exceeds plan by >= threshold and
    # we're inside the cooldown-clear window, re-plan immediately. Catches
    # the failure mode where Fox V3 ForceCharge over-pulls vs the LP's
    # tapered schedule (2026-05-08 incident: planned 7.49 kWh / 4 h, actual
    # 10.18 kWh = +36 %). Single-shot — by the time we know a slot
    # overshot, it's already over and we want to revise the remaining
    # ForceCharge window NOW.
    threshold_kwh = float(config.MPC_IMPORT_OVERSHOOT_KWH_THRESHOLD)
    if config.MPC_EVENT_DRIVEN_ENABLED and threshold_kwh > 0:
        try:
            # Last fully completed slot ends at the most recent :00 or :30
            # boundary <= now. Subtract 30 minutes to get its start.
            now_floor = now_utc.replace(second=0, microsecond=0)
            slot_end_minute = 0 if now_floor.minute < 30 else 30
            slot_end = now_floor.replace(minute=slot_end_minute)
            slot_start = slot_end - timedelta(minutes=30)
            actual_kwh = _actual_import_kwh_for_slot(slot_start)
            planned_kwh = _lp_planned_import_kwh_at(slot_start)
            if actual_kwh is not None and planned_kwh is not None:
                overshoot_kwh = actual_kwh - planned_kwh
                if overshoot_kwh >= threshold_kwh:
                    logger.info(
                        "MPC import_overshoot trigger: slot=%s actual=%.2f kWh "
                        "planned=%.2f kWh delta=+%.2f kWh (>=%.2f) — re-planning",
                        slot_start.isoformat(), actual_kwh, planned_kwh,
                        overshoot_kwh, threshold_kwh,
                    )
                    bulletproof_mpc_job(
                        force_write_devices=True,
                        trigger_reason="import_overshoot",
                    )
                else:
                    logger.debug(
                        "MPC import_overshoot check: slot=%s delta=+%.2f kWh < %.2f, no fire",
                        slot_start.isoformat(), overshoot_kwh, threshold_kwh,
                    )
        except Exception as e:
            logger.debug("import_overshoot check failed (non-fatal): %s", e)

    # Event-driven MPC: live PV/load deviation trigger.
    # Complements forecast_revision (forecast-vs-forecast) with real-vs-expected checks.
    if config.MPC_EVENT_DRIVEN_ENABLED and (rt_solar_kw is not None or rt_load_kw is not None):
        try:
            global _consecutive_pv_up_ticks, _consecutive_pv_down_ticks, _consecutive_load_up_ticks
            hyst_ticks = max(1, int(config.MPC_LIVE_DEVIATION_HYSTERESIS_TICKS))
            pv_thr = float(config.MPC_LIVE_PV_KW_THRESHOLD)
            load_thr = float(config.MPC_LIVE_LOAD_KW_THRESHOLD)

            if rt_solar_kw is not None:
                expected_pv_kw = _get_forecast_pv_kw(now_utc)
                if expected_pv_kw is not None:
                    delta_pv_kw = rt_solar_kw - expected_pv_kw
                    if delta_pv_kw >= pv_thr:
                        _consecutive_pv_up_ticks += 1
                        _consecutive_pv_down_ticks = 0
                    elif delta_pv_kw <= -pv_thr:
                        _consecutive_pv_down_ticks += 1
                        _consecutive_pv_up_ticks = 0
                    else:
                        _consecutive_pv_up_ticks = 0
                        _consecutive_pv_down_ticks = 0
                else:
                    _consecutive_pv_up_ticks = 0
                    _consecutive_pv_down_ticks = 0

            if rt_load_kw is not None:
                expected_load_kw = _lp_predicted_load_kw_at(now_utc)
                if expected_load_kw is not None and (rt_load_kw - expected_load_kw) >= load_thr:
                    _consecutive_load_up_ticks += 1
                else:
                    _consecutive_load_up_ticks = 0

            if _consecutive_pv_up_ticks >= hyst_ticks:
                _consecutive_pv_up_ticks = 0
                _consecutive_pv_down_ticks = 0
                logger.info("MPC live trigger: pv_upside sustained for %d tick(s)", hyst_ticks)
                bulletproof_mpc_job(force_write_devices=True, trigger_reason="pv_upside")
            elif _consecutive_pv_down_ticks >= hyst_ticks:
                _consecutive_pv_up_ticks = 0
                _consecutive_pv_down_ticks = 0
                logger.info("MPC live trigger: pv_downside sustained for %d tick(s)", hyst_ticks)
                bulletproof_mpc_job(force_write_devices=True, trigger_reason="pv_downside")
            elif _consecutive_load_up_ticks >= hyst_ticks:
                _consecutive_load_up_ticks = 0
                logger.info("MPC live trigger: load_upside sustained for %d tick(s)", hyst_ticks)
                bulletproof_mpc_job(force_write_devices=True, trigger_reason="load_upside")
        except Exception as e:
            logger.debug("live-deviation trigger check failed (non-fatal): %s", e)

    # Event-driven MPC: appliance arm/cancel trigger (Smart-Control gesture).
    # SmartThings has no polling-free push for device events without a public
    # webhook ingress — which our loopback + Tailscale deployment deliberately
    # avoids — so we detect the user's Smart-Control toggle here on the
    # heartbeat instead. This bounds the arm → plan → notify latency to one
    # heartbeat, rather than waiting for an unrelated LP-solve trigger (tier
    # boundary / drift / octopus fetch) to happen to fire. The detector only
    # returns True on a genuine transition (Smart Control on with no job, or
    # off with a job still armed); once reconcile() creates/cancels the job the
    # next heartbeat sees it and won't re-fire, so the cooldown bypass is safe.
    if config.MPC_EVENT_DRIVEN_ENABLED and config.APPLIANCE_DISPATCH_ENABLED:
        try:
            from . import appliance_dispatch

            if appliance_dispatch.pending_arm_change():
                logger.info(
                    "MPC appliance trigger: Smart-Control state change detected "
                    "— re-planning to arm/cancel the cycle"
                )
                bulletproof_mpc_job(
                    force_write_devices=True,
                    trigger_reason="appliance_armed",
                    bypass_cooldown=True,
                )
        except Exception as e:
            logger.debug("appliance-arm trigger check failed (non-fatal): %s", e)

    room_t: float | None = None
    outdoor_t: float | None = None
    lwt_off: float | None = None
    tank_t: float | None = None
    tank_tgt: float | None = None
    tank_on = True
    dev0 = devices[0] if devices else None
    if dev0:
        room_t = dev0.temperature.room_temperature
        _last_room_temp = room_t
        outdoor_t = dev0.temperature.outdoor_temperature
        lwt_off = dev0.lwt_offset
        tank_t = dev0.tank_temperature
        tank_tgt = dev0.tank_target

    price: float | None = None
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if tariff:
        try:
            rates = db.get_rates_for_period(
                tariff, now_utc - timedelta(hours=1), now_utc + timedelta(hours=1)
            )
            _, _, price = get_current_and_next_slots(
                [
                    {
                        "value_inc_vat": float(r["value_inc_vat"]),
                        "valid_from": r["valid_from"],
                        "valid_to": r["valid_to"],
                    }
                    for r in rates
                ],
                cheap_threshold_pence=config.SCHEDULER_CHEAP_THRESHOLD_PENCE,
                peak_start=config.SCHEDULER_PEAK_START,
                peak_end=config.SCHEDULER_PEAK_END,
            )
        except Exception:
            price = None

    if dev0:
        # Build a lightweight DaikinClient handle for reconcile (it won't call get_devices again).
        from ..daikin.client import DaikinClient as _DC
        _dc = _DC()
        reconcile_daikin_schedule_for_date(
            plan_date,
            _dc,
            dev0,
            now_utc,
            trigger="heartbeat",
            outdoor_c=outdoor_t,
        )

    if dev0:
        _maybe_log_comfort_morning_check(
            now_local=now_local,
            now_utc=now_utc,
            plan_date=plan_date,
            room_t=room_t,
            soc=soc,
            fox_mode=fox_mode,
            outdoor_t=outdoor_t,
            lwt_off=lwt_off,
            tank_t=tank_t,
            tank_tgt=tank_tgt,
            tank_on=tank_on,
            dev0=dev0,
        )

    if mon - _last_fox_verify_monotonic >= 1800 and fox and fox.api_key:
        _last_fox_verify_monotonic = mon
        try:
            heartbeat_repair_fox_scheduler(fox)
        except Exception as e:
            logger.warning("Fox scheduler verify: %s", e)

    hh_key = f"{now_local.date().isoformat()}_{now_local.hour:02d}_{30 if now_local.minute >= 30 else 0:02d}"
    if _last_exec_halfhour_key != hh_key:
        _last_exec_halfhour_key = hh_key
        slot_kind = None
        tgt = db.get_daily_target(now_local.date())
        if tgt and price is not None:
            # Negative is its own tier (V12) — Octopus paying us. Detected
            # before the cheap/peak threshold check because a -5p price would
            # otherwise tag as "cheap".
            if float(price) < 0:
                slot_kind = "negative"
            elif float(price) > float(tgt.get("peak_threshold") or 99):
                slot_kind = "peak"
            elif float(price) < float(tgt.get("cheap_threshold") or 0):
                slot_kind = "cheap"
            else:
                slot_kind = "standard"
        from ..analytics.shadow_pricing import fixed_shadow_rate_pence, svt_rate_pence

        svt = svt_rate_pence()
        fix = fixed_shadow_rate_pence()
        # v10.1: real per-slot consumption from Fox load_power × slot hours.
        # The heartbeat only writes one execution_log row per 30-min slot
        # (gated by hh_key above), so each row represents the WHOLE slot, not
        # just a single 2-min heartbeat sample. We use the instantaneous Fox
        # load_power at write time multiplied by 0.5h as the slot's kWh — a
        # reasonable approximation when load is stable. (For a more accurate
        # measure we'd need to sample every heartbeat and integrate, which is
        # a larger refactor; tracked as a future enhancement.)
        SLOT_HOURS = 0.5
        load_kw = None
        try:
            from ..foxess import service as _fox_svc
            snap = _fox_svc.get_cached_realtime(max_age_seconds=86_400)
            if snap is not None:
                load_kw = getattr(snap, "load_power", None)
        except Exception:
            pass
        if load_kw is None:
            sqlite_snap = db.get_fox_realtime_snapshot() or {}
            load_kw = sqlite_snap.get("load_power_kw")
        if load_kw is not None:
            kwh_est = float(load_kw) * SLOT_HOURS
        else:
            kwh_est = db.mean_consumption_kwh_from_execution_logs()
        p = float(price) if price is not None else 0.0
        db.log_execution(
            {
                "timestamp": now_utc.isoformat(),
                "consumption_kwh": kwh_est,
                "agile_price_pence": p,
                "svt_shadow_price_pence": svt,
                "fixed_shadow_price_pence": fix,
                "cost_realised_pence": kwh_est * p,
                "cost_svt_shadow_pence": kwh_est * svt,
                "cost_fixed_shadow_pence": kwh_est * fix,
                "delta_vs_svt_pence": kwh_est * (svt - p),
                "delta_vs_fixed_pence": kwh_est * (fix - p),
                "soc_percent": soc,
                "fox_mode": fox_mode,
                "daikin_lwt_offset": lwt_off,
                "daikin_tank_temp": tank_t,
                "daikin_tank_target": tank_tgt,
                "daikin_tank_power_on": 1 if tank_on else 0,
                "daikin_powerful_mode": None,
                "daikin_room_temp": room_t,
                "daikin_outdoor_temp": outdoor_t,
                "daikin_lwt": dev0.leaving_water_temperature if dev0 else None,
                "forecast_temp_c": _get_forecast_temp_c(now_utc) or outdoor_t,
                "forecast_solar_kw": None,
                "forecast_heating_demand": None,
                "slot_kind": slot_kind,
                "source": "estimated",
            }
        )

        # Lazy load on first tick after restart so dedupe state survives
        # container restarts (otherwise we re-announce every active slot kind).
        if not _last_notified_slot_kind_loaded:
            try:
                persisted = db.get_runtime_setting("last_notified_slot_kind")
                if persisted:
                    _last_notified_slot_kind = persisted
            except Exception as exc:
                logger.debug("last_notified_slot_kind load skipped: %s", exc)
            _last_notified_slot_kind_loaded = True

        if slot_kind != _last_notified_slot_kind:
            _last_notified_slot_kind = slot_kind
            try:
                db.set_runtime_setting("last_notified_slot_kind", slot_kind or "")
            except Exception as exc:
                logger.debug("last_notified_slot_kind persist skipped: %s", exc)
            # V12 — twice-daily digest model. The morning brief lists today's
            # tariff windows in full, so we no longer ping per crossing for
            # cheap/peak/standard transitions by default. ``negative`` is
            # the exception: rare (~1–2/week), immediately actionable, and
            # always pings regardless of NOTIFY_TARIFF_TRANSITIONS.
            if slot_kind == "negative":
                try:
                    push_negative_window_start(soc=soc, fox_mode=fox_mode, price_pence=price)
                except Exception as exc:
                    logger.debug("Push negative window notification error: %s", exc)
            elif config.NOTIFY_TARIFF_TRANSITIONS:
                if slot_kind == "cheap":
                    try:
                        push_cheap_window_start(soc=soc, fox_mode=fox_mode)
                    except Exception as exc:
                        logger.debug("Push cheap window notification error: %s", exc)
                elif slot_kind == "peak":
                    try:
                        push_peak_window_start(soc=soc)
                    except Exception as exc:
                        logger.debug("Push peak window notification error: %s", exc)

    if (
        soc is not None
        and soc < float(config.FOXESS_ALERT_LOW_SOC)
        and price is not None
        and float(price) > float(config.OPTIMIZATION_PEAK_THRESHOLD_PENCE)
    ):
        key = f"low_soc_peak_{plan_date}"
        if not db.is_warning_acknowledged(key):
            notify_risk(f"Low SOC {soc}% during high price {price}p/kWh", extra={"warning_key": key})

    if (
        soc is not None
        and soc < float(config.MIN_SOC_RESERVE_PERCENT)
        and price is not None
        and float(price) > float(config.OPTIMIZATION_PEAK_THRESHOLD_PENCE)
    ):
        key = f"soc_reserve_floor_peak_{plan_date}"
        if not db.is_warning_acknowledged(key):
            notify_risk(
                f"Battery at {soc}% (below MIN_SOC_RESERVE_PERCENT {config.MIN_SOC_RESERVE_PERCENT}) "
                f"during high price {price}p/kWh",
                extra={"warning_key": key},
            )


def _heartbeat_loop() -> None:
    while not _heartbeat_stop.wait(timeout=config.HEARTBEAT_INTERVAL_SECONDS):
        try:
            bulletproof_heartbeat_tick()
        except Exception:
            logger.exception("Heartbeat tick failed")


def start_heartbeat_background() -> None:
    global _heartbeat_thread
    if not config.USE_BULLETPROOF_ENGINE:
        return
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _heartbeat_stop.clear()
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="bulletproof-heartbeat", daemon=True)
    _heartbeat_thread.start()
    logger.info("Bulletproof heartbeat started (%ss)", config.HEARTBEAT_INTERVAL_SECONDS)


def stop_heartbeat_background() -> None:
    global _heartbeat_thread
    _heartbeat_stop.set()
    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=5.0)
    _heartbeat_thread = None
    logger.info("Bulletproof heartbeat stopped")


def start_background_scheduler() -> None:
    """Start APScheduler job(s) and Bulletproof heartbeat thread."""
    global _background_scheduler
    if _background_scheduler is not None:
        return
    if not config.OCTOPUS_TARIFF_CODE:
        if config.USE_BULLETPROOF_ENGINE:
            start_heartbeat_background()
        return
    if not config.SCHEDULER_ENABLED and not config.USE_BULLETPROOF_ENGINE:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        _background_scheduler = BackgroundScheduler()
        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE if config.USE_BULLETPROOF_ENGINE else config.OPTIMIZATION_TIMEZONE)

        if config.USE_BULLETPROOF_ENGINE and config.OCTOPUS_TARIFF_CODE:
            _background_scheduler.add_job(
                bulletproof_octopus_fetch_job,
                CronTrigger(
                    hour=config.OCTOPUS_FETCH_HOUR,
                    minute=config.OCTOPUS_FETCH_MINUTE,
                    timezone=tz,
                ),
                id="bulletproof_octopus_fetch",
            )
            _background_scheduler.add_job(
                bulletproof_octopus_retry_job,
                "interval",
                minutes=10,
                id="bulletproof_octopus_retry",
            )
            _background_scheduler.add_job(
                bulletproof_morning_brief_job,
                CronTrigger(
                    hour=config.BRIEF_MORNING_HOUR,
                    minute=config.BRIEF_MORNING_MINUTE,
                    timezone=tz,
                ),
                id="bulletproof_morning_brief",
            )
            _background_scheduler.add_job(
                bulletproof_night_brief_job,
                CronTrigger(
                    hour=config.BRIEF_NIGHT_HOUR,
                    minute=config.BRIEF_NIGHT_MINUTE,
                    timezone=tz,
                ),
                id="bulletproof_night_brief",
            )
            logger.info(
                "Twice-daily digest cron: morning %02d:%02d, night %02d:%02d (%s)",
                config.BRIEF_MORNING_HOUR, config.BRIEF_MORNING_MINUTE,
                config.BRIEF_NIGHT_HOUR, config.BRIEF_NIGHT_MINUTE, tz,
            )
            # V13 — nightly consumption backfill from Octopus's smart-meter
            # endpoint. Rewrites yesterday's execution_log rows from estimated
            # (heartbeat single-sample × 0.5 h) to metered (true kWh from the
            # household's smart meter). Morning + night briefs read the
            # rewritten rows so the family sees real PnL, not extrapolations.
            _background_scheduler.add_job(
                bulletproof_consumption_backfill_job,
                CronTrigger(
                    hour=config.CONSUMPTION_BACKFILL_HOUR,
                    minute=config.CONSUMPTION_BACKFILL_MINUTE,
                    timezone=tz,
                ),
                id="bulletproof_consumption_backfill",
            )
            logger.info(
                "Consumption backfill cron: %02d:%02d (%s) — rewrites yesterday's "
                "execution_log with metered kWh from Octopus.",
                config.CONSUMPTION_BACKFILL_HOUR,
                config.CONSUMPTION_BACKFILL_MINUTE,
                tz,
            )
            _background_scheduler.add_job(
                bulletproof_forecast_skill_log_job,
                CronTrigger(hour=4, minute=15, timezone=ZoneInfo("UTC")),
                id="bulletproof_forecast_skill_log",
            )
            logger.info("Forecast skill rebuild cron scheduled (04:15 UTC daily)")
            # Per-slot PV forecast-error log (#462). 04:20 UTC — after the skill
            # rebuild (04:15) and the Fox/PV roll-ups (02:30), before PV
            # calibration refresh (04:30) so it has the prior day's full actuals.
            _background_scheduler.add_job(
                bulletproof_pv_error_log_job,
                CronTrigger(hour=4, minute=20, timezone=ZoneInfo("UTC")),
                id="bulletproof_pv_error_log",
            )
            logger.info("PV error-log rebuild cron scheduled (04:20 UTC daily)")
            # Per-slot LOAD forecast-error log (Phase-1 load calibration). 04:22
            # UTC — just after the PV error log; measurement only, no LP impact.
            _background_scheduler.add_job(
                bulletproof_load_error_log_job,
                CronTrigger(hour=4, minute=22, timezone=ZoneInfo("UTC")),
                id="bulletproof_load_error_log",
            )
            logger.info("Load error-log rebuild cron scheduled (04:22 UTC daily)")
            # Per-bucket DHW forecast-error log (PR C). 04:24 UTC — after the
            # 02:35 UTC Daikin consumption rollup; measurement only.
            _background_scheduler.add_job(
                bulletproof_dhw_error_log_job,
                CronTrigger(hour=4, minute=24, timezone=ZoneInfo("UTC")),
                id="bulletproof_dhw_error_log",
            )
            logger.info("DHW error-log rebuild cron scheduled (04:24 UTC daily)")
            # Daily export opportunity cost (Agile vs SEG). 04:25 UTC — after the
            # Fox/PV roll-ups (02:30) so the prior day's export is complete.
            _background_scheduler.add_job(
                bulletproof_export_opportunity_job,
                CronTrigger(hour=4, minute=25, timezone=ZoneInfo("UTC")),
                id="bulletproof_export_opportunity",
            )
            logger.info("Export-opportunity cron scheduled (04:25 UTC daily)")
            # W2 thermal learner (#540). 05:30 UTC — after the 02:35 Daikin
            # rollup + 04:00 consumption backfill so decay-episode
            # decontamination sees the fullest heating splits. Quiet no-op
            # until indoor sensor data exists.
            if getattr(config, "THERMAL_LEARNING_ENABLED", True):
                _background_scheduler.add_job(
                    bulletproof_thermal_learning_job,
                    CronTrigger(hour=5, minute=30, timezone=ZoneInfo("UTC")),
                    id="bulletproof_thermal_learning",
                )
                logger.info("Thermal-learning cron scheduled (05:30 UTC daily)")
            # Guests-elevation detector — after the load_error_log rebuild (04:22
            # UTC) so yesterday's actual-vs-forecast is fresh. Fires in the local
            # morning (default 08:30) so the prompt lands at a sensible hour. It
            # never auto-applies; it only suggests the guests preset.
            if getattr(config, "GUESTS_DETECT_ENABLED", True):
                _background_scheduler.add_job(
                    bulletproof_guests_detector_job,
                    CronTrigger(
                        hour=int(config.GUESTS_SUGGESTION_HOUR),
                        minute=int(config.GUESTS_SUGGESTION_MINUTE),
                        timezone=ZoneInfo(config.BULLETPROOF_TIMEZONE),
                    ),
                    id="bulletproof_guests_detector",
                )
                logger.info(
                    "Guests-elevation detector cron scheduled (%02d:%02d %s daily)",
                    int(config.GUESTS_SUGGESTION_HOUR), int(config.GUESTS_SUGGESTION_MINUTE),
                    config.BULLETPROOF_TIMEZONE,
                )
            # LP health monitor — nightly regression self-check (#607-#610).
            # Fires after the negative window has fully passed + nightly rebuilds.
            # Alerts only on regression.
            if getattr(config, "LP_HEALTH_MONITOR_ENABLED", True):
                _background_scheduler.add_job(
                    bulletproof_lp_health_monitor_job,
                    CronTrigger(
                        hour=int(config.LP_HEALTH_MONITOR_HOUR),
                        minute=int(config.LP_HEALTH_MONITOR_MINUTE),
                        timezone=ZoneInfo(config.BULLETPROOF_TIMEZONE),
                    ),
                    id="bulletproof_lp_health_monitor",
                )
                logger.info(
                    "LP health monitor cron scheduled (%02d:%02d %s daily)",
                    int(config.LP_HEALTH_MONITOR_HOUR), int(config.LP_HEALTH_MONITOR_MINUTE),
                    config.BULLETPROOF_TIMEZONE,
                )
            # PV calibration refresh — keeps pv_calibration_hourly +
            # pv_calibration_hourly_cloud current. Runs at 04:30 UTC, after
            # skill-log rebuild (04:15) and Fox/Daikin rollups (02:30 / 02:35)
            # so it consumes their fresh outputs. Before the morning brief
            # (08:00 BST = 07:00 UTC) so the brief reflects the new factors.
            _background_scheduler.add_job(
                bulletproof_pv_calibration_refresh_job,
                CronTrigger(hour=4, minute=30, timezone=ZoneInfo("UTC")),
                id="bulletproof_pv_calibration_refresh",
            )
            logger.info(
                "PV calibration refresh cron scheduled (04:30 UTC daily) — "
                "recomputes pv_calibration_hourly + pv_calibration_hourly_cloud"
            )
            # Daikin LWT→kW per-installation calibration runs INLINE at the top
            # of every LP solve (see src.scheduler.optimizer.run_optimizer →
            # db.refresh_daikin_lwt_kw_calibration). No separate cron — the
            # solve already touches the DB and the regression is cheap (~5 ms,
            # 30 daily rows). This guarantees the next solve after a fresh
            # daikin_consumption_daily row lands picks up the new fit
            # immediately, instead of waiting for a fixed-time cron.
            # V12: MPC is fully event-driven. The fixed-hour cron is GONE.
            # Triggers: octopus_fetch (when new rates land), tier_boundary
            # (before every tariff transition), soc_drift / forecast_revision
            # (unforecast events), dynamic_replan (post-truncation tail),
            # plan_push (nightly). Manual re-runs via MCP propose_optimization_plan.
            logger.info(
                "MPC scheduling: fully event-driven (V12) — "
                "octopus_fetch + tier_boundary + soc_drift + forecast_revision + "
                "dynamic_replan + plan_push. No fixed-hour cron."
            )
            # Forecast revision trigger (Waze MPC story #144): hourly Open-Meteo refresh
            # + delta detector. Persists every fetch (audit trail + LP source); fires MPC
            # only when next-6h delta exceeds threshold. Skipped if kill switch off.
            from apscheduler.triggers.interval import IntervalTrigger
            _background_scheduler.add_job(
                bulletproof_forecast_refresh_job,
                IntervalTrigger(minutes=int(config.MPC_FORECAST_REFRESH_INTERVAL_MINUTES)),
                id="bulletproof_forecast_refresh",
            )
            logger.info(
                "Forecast refresh cron scheduled every %d min",
                int(config.MPC_FORECAST_REFRESH_INTERVAL_MINUTES),
            )

            # S10.10 (#177): daily Fox energy rollup from local pv_realtime_history.
            # Runs at 02:30 UTC — yesterday's samples fully captured by then; well
            # before the 03:05 host-level backup so the latest rollup is captured.
            _background_scheduler.add_job(
                bulletproof_fox_energy_rollup_job,
                CronTrigger(hour=2, minute=30, timezone=ZoneInfo("UTC")),
                id="bulletproof_fox_energy_rollup",
            )
            logger.info("fox_energy_daily rollup cron scheduled (02:30 UTC daily)")

            # S10.12 (#178): daily Daikin consumption rollup from cached payload.
            # Runs 02:35 UTC, 5 min after Fox so we don't burst all rollups at once.
            # Reads /gateway-devices cache — no extra Daikin quota.
            _background_scheduler.add_job(
                bulletproof_daikin_consumption_rollup_job,
                CronTrigger(hour=2, minute=35, timezone=ZoneInfo("UTC")),
                id="bulletproof_daikin_consumption_rollup",
            )
            logger.info("daikin_consumption_daily rollup cron scheduled (02:35 UTC daily)")
            # Intraday partial rollups (2026-07-05): the Onecta `d` array
            # carries TODAY's partial 2h buckets (future = None; the upsert's
            # later-polls-overwrite contract was built for exactly this), so
            # running the same job midday gives the Consumption panel its
            # heat-pump split same-day instead of "everything is Base until
            # tomorrow 02:35" (the legionella-Sunday complaint). Each run
            # costs ONE /gateway-devices read — 3 fixed times ≈ 3/200 of the
            # daily quota; the read path fails fast under the quota soft-cap.
            if getattr(config, "DAIKIN_CONSUMPTION_INTRADAY_ENABLED", True):
                _background_scheduler.add_job(
                    bulletproof_daikin_consumption_rollup_job,
                    CronTrigger(hour="10,14,18", minute=5, timezone=ZoneInfo("UTC")),
                    id="bulletproof_daikin_consumption_intraday",
                    kwargs={"two_hourly_only": True},
                )
                logger.info(
                    "daikin consumption intraday rollup scheduled (10:05/14:05/18:05 UTC)"
                )
            # PV realtime telemetry (Solar Sponge analysis): persist Fox cached realtime
            # to pv_realtime_history. Zero Fox quota cost (heartbeat-cached). Used by
            # offline PV calibration analysis.
            _background_scheduler.add_job(
                bulletproof_pv_telemetry_job,
                IntervalTrigger(minutes=int(config.PV_TELEMETRY_INTERVAL_MINUTES)),
                id="bulletproof_pv_telemetry",
            )
            logger.info(
                "PV telemetry cron scheduled every %d min",
                int(config.PV_TELEMETRY_INTERVAL_MINUTES),
            )
            # Viewer-aware freshness boost: while the cockpit is open, keep the
            # Fox/Daikin caches fresher than their idle baselines (quota-guarded).
            # No viewer → the 30 s tick is a monotonic comparison and returns.
            if getattr(config, "VIEWER_BOOST_ENABLED", True):
                _background_scheduler.add_job(
                    bulletproof_viewer_boost_job,
                    IntervalTrigger(seconds=30),
                    id="bulletproof_viewer_boost",
                )
                logger.info(
                    "Viewer freshness boost scheduled (30 s tick; fox<=%ds daikin<=%ds "
                    "while viewing, reserves fox>%d daikin>%d)",
                    int(config.FOX_VIEWER_REFRESH_SECONDS),
                    int(config.DAIKIN_VIEWER_REFRESH_SECONDS),
                    int(config.FOX_VIEWER_QUOTA_RESERVE),
                    int(config.DAIKIN_VIEWER_QUOTA_RESERVE),
                )
            # Nightly plan push: dispatch Fox + Daikin just after the Daikin daily quota
            # rollover (midnight UTC). Anchored to UTC regardless of BULLETPROOF_TIMEZONE
            # so the push always lands on a fresh quota day.
            _background_scheduler.add_job(
                bulletproof_plan_push_job,
                CronTrigger(
                    hour=config.LP_PLAN_PUSH_HOUR,
                    minute=config.LP_PLAN_PUSH_MINUTE,
                    timezone=ZoneInfo("UTC"),
                ),
                id="bulletproof_plan_push",
            )
            # Daily history-table retention sweep. Runs at 03:15 UTC — well
            # clear of the midnight plan-push rollover and the MPC cadence,
            # so the DB stays bounded over multi-month uptimes without
            # contending with write-heavy windows. See
            # db.prune_history_tables() for the per-table retention policies.
            _background_scheduler.add_job(
                _daily_history_prune_job,
                CronTrigger(hour=3, minute=15, timezone=ZoneInfo("UTC")),
                id="daily_history_prune",
            )

            # Google Calendar publisher — separate APScheduler job so a bug
            # here cannot affect octopus_fetch, MPC, dispatch, or LP. Three
            # firings 30 min apart (T+0, T+30, T+60) at GOOGLE_CALENDAR_
            # PUBLISH_HOUR:MINUTE UTC: Octopus sometimes lags publishing
            # tomorrow's rates past 16:00 UTC, so the first firing may find
            # them missing. Each run is idempotent — the first to find full
            # horizon data publishes; later runs match-and-no-op. A service-
            # startup call below covers the "service was down at cron time"
            # case so recovery is automatic.
            if config.GOOGLE_CALENDAR_ENABLED:
                base_h = config.GOOGLE_CALENDAR_PUBLISH_HOUR
                base_m = config.GOOGLE_CALENDAR_PUBLISH_MINUTE
                for offset_min in (0, 30, 60):
                    total_min = base_m + offset_min
                    h = (base_h + total_min // 60) % 24
                    m = total_min % 60
                    _background_scheduler.add_job(
                        bulletproof_calendar_publish_job,
                        CronTrigger(hour=h, minute=m, timezone=ZoneInfo("UTC")),
                        id=f"google_calendar_publish_{h:02d}{m:02d}",
                    )
                logger.info(
                    "Google Calendar publish cron scheduled at %02d:%02d UTC + 2 retries 30 min apart",
                    base_h, base_m,
                )
            logger.info(
                "Bulletproof cron: Octopus %02d:%02d (%s); plan push %02d:%02d UTC; history prune 03:15 UTC",
                config.OCTOPUS_FETCH_HOUR,
                config.OCTOPUS_FETCH_MINUTE,
                tz,
                config.LP_PLAN_PUSH_HOUR,
                config.LP_PLAN_PUSH_MINUTE,
            )

        _background_scheduler.start()

        if config.USE_BULLETPROOF_ENGINE:
            start_heartbeat_background()
            try:
                bulletproof_octopus_fetch_job()
            except Exception as e:
                logger.warning("Initial Octopus fetch failed: %s", e)
            # V12 — register tier-boundary one-shots from whatever rates are
            # already in the DB. ``bulletproof_octopus_fetch_job`` also
            # re-registers, but that path can fail on a network blip and
            # we'd lose all tier-boundary fires until the next retry. This
            # explicit call uses cached rates and is a no-op if fetch already
            # registered the same windows (replace_existing on the same id).
            try:
                _register_tier_boundary_triggers()
            except Exception as e:
                logger.warning("Initial tier-boundary registration failed: %s", e)
            # Initial calendar publish so first-deploy / restart-after-cron-time
            # don't leave the family calendar stale until the next 16:30 UTC.
            if config.GOOGLE_CALENDAR_ENABLED:
                try:
                    bulletproof_calendar_publish_job()
                except Exception as e:
                    logger.warning("Initial calendar publish failed: %s", e)

    except Exception as e:
        logger.warning("Could not start background scheduler: %s", e)


def stop_background_scheduler() -> None:
    global _background_scheduler
    stop_heartbeat_background()
    if _background_scheduler is None:
        return
    try:
        _background_scheduler.shutdown(wait=False)
    except Exception:
        pass
    _background_scheduler = None
    logger.info("Background scheduler stopped")


def get_background_scheduler() -> Any:
    """Return the running APScheduler instance, or ``None`` when scheduler
    bootstrap has not run yet (e.g. tests, CLI scripts). Callers must
    tolerate ``None`` rather than assuming the scheduler is up."""
    return _background_scheduler


def reregister_cron_jobs(reason: str = "runtime_settings_change") -> dict[str, Any]:
    """Tear down and re-create the cadence-tunable cron jobs (#52).

    Invoked by the settings PUT handler after ``LP_PLAN_PUSH_HOUR``,
    ``LP_PLAN_PUSH_MINUTE``, ``MPC_FORECAST_REFRESH_INTERVAL_MINUTES``, or
    ``PV_TELEMETRY_INTERVAL_MINUTES`` change. Jobs handled:

    - ``bulletproof_plan_push``: single UTC-anchored push.
    - ``bulletproof_forecast_refresh``: hot-reloadable interval.
    - ``bulletproof_pv_telemetry``: hot-reloadable interval.

    V12 removed the fixed-hour MPC cron — the MPC is fully event-driven
    (octopus_fetch, tier_boundary, soc_drift, forecast_revision,
    dynamic_replan, plan_push). Stale ``bulletproof_mpc_*`` jobs from a
    prior process generation are still removed below for a clean handover.

    The heartbeat thread and other jobs are untouched. When the background
    scheduler is not yet started (e.g. tests, non-bulletproof mode), this is
    a no-op that returns ``{"status": "inactive"}``.
    """
    if _background_scheduler is None or not config.USE_BULLETPROOF_ENGINE:
        return {"status": "inactive", "reason": reason}

    try:
        from apscheduler.triggers.cron import CronTrigger
    except Exception as e:  # pragma: no cover - only when apscheduler missing
        logger.warning("reregister_cron_jobs: apscheduler import failed: %s", e)
        return {"status": "error", "reason": reason, "error": str(e)}

    removed: list[str] = []
    for job in list(_background_scheduler.get_jobs()):
        jid = job.id
        if (
            jid == "bulletproof_plan_push"
            or jid.startswith("bulletproof_mpc_")  # legacy V11 fixed-hour cron, swept away
            or jid == "bulletproof_forecast_refresh"
            or jid == "bulletproof_pv_telemetry"
        ):
            try:
                _background_scheduler.remove_job(jid)
                removed.append(jid)
            except Exception as e:
                logger.warning("remove_job(%s) failed: %s", jid, e)

    added: list[str] = []

    push_jid = "bulletproof_plan_push"
    _background_scheduler.add_job(
        bulletproof_plan_push_job,
        CronTrigger(
            hour=config.LP_PLAN_PUSH_HOUR,
            minute=config.LP_PLAN_PUSH_MINUTE,
            timezone=ZoneInfo("UTC"),
        ),
        id=push_jid,
    )
    added.append(push_jid)

    # Forecast refresh interval is hot-reloadable via runtime_settings.
    from apscheduler.triggers.interval import IntervalTrigger
    forecast_jid = "bulletproof_forecast_refresh"
    _background_scheduler.add_job(
        bulletproof_forecast_refresh_job,
        IntervalTrigger(minutes=int(config.MPC_FORECAST_REFRESH_INTERVAL_MINUTES)),
        id=forecast_jid,
    )
    added.append(forecast_jid)

    pv_jid = "bulletproof_pv_telemetry"
    _background_scheduler.add_job(
        bulletproof_pv_telemetry_job,
        IntervalTrigger(minutes=int(config.PV_TELEMETRY_INTERVAL_MINUTES)),
        id=pv_jid,
    )
    added.append(pv_jid)

    logger.info(
        "Cron jobs re-registered (reason=%s): removed=%s added=%s "
        "plan_push=%02d:%02d UTC forecast_refresh=%dmin pv_telemetry=%dmin",
        reason,
        removed,
        added,
        config.LP_PLAN_PUSH_HOUR,
        config.LP_PLAN_PUSH_MINUTE,
        int(config.MPC_FORECAST_REFRESH_INTERVAL_MINUTES),
        int(config.PV_TELEMETRY_INTERVAL_MINUTES),
    )
    return {
        "status": "ok",
        "reason": reason,
        "removed": removed,
        "added": added,
        "plan_push_utc": f"{config.LP_PLAN_PUSH_HOUR:02d}:{config.LP_PLAN_PUSH_MINUTE:02d}",
        "forecast_refresh_minutes": int(config.MPC_FORECAST_REFRESH_INTERVAL_MINUTES),
        "pv_telemetry_minutes": int(config.PV_TELEMETRY_INTERVAL_MINUTES),
    }
