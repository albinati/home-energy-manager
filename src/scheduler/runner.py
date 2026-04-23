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
from ..foxess.service import get_cached_realtime
from ..notifier import notify_risk, push_cheap_window_start, push_peak_window_start
from ..state_machine import heartbeat_repair_fox_scheduler, reconcile_daikin_schedule_for_date
from .agile import fetch_agile_rates, get_current_and_next_slots
from .daikin import compute_lwt_adjustment, run_daikin_scheduler_tick

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
_comfort_morning_logged: set[str] = set()


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


def run_scheduler_tick() -> str | None:
    """Run one legacy scheduler tick (Daikin LWT only)."""
    return run_daikin_scheduler_tick(get_scheduler_paused())


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


def bulletproof_octopus_fetch_job() -> None:
    from .octopus_fetch import fetch_and_store_rates

    fetch_and_store_rates(_try_fox())


def bulletproof_octopus_retry_job() -> None:
    from .octopus_fetch import fetch_and_store_rates, should_run_retry_fetch

    if not should_run_retry_fetch():
        return
    fetch_and_store_rates(_try_fox())


def bulletproof_daily_brief_job() -> None:
    from ..analytics.daily_brief import send_daily_brief_webhook

    try:
        send_daily_brief_webhook()
    except Exception as e:
        logger.warning("Daily brief failed: %s", e)


def mpc_should_skip_hour_for_octopus_fetch(local_hour: int) -> bool:
    """When True, skip MPC at this local hour — the Octopus fetch cron runs later the same hour.

    Avoids two full PuLP runs within minutes (MPC at :00 vs fetch at :05 with fresh rates). #34
    """
    return int(local_hour) == int(config.OCTOPUS_FETCH_HOUR)


def bulletproof_mpc_job() -> None:
    """Intra-day MPC re-optimise: refresh forecast + live SoC + live PV, re-upload Fox/Daikin.

    Reads Fox realtime (SoC%, solar_power_kw, load_power_kw) and passes them into the LP
    initial state so the re-optimisation reflects the actual current energy state rather than
    yesterday's estimate.  Only runs when USE_BULLETPROOF_ENGINE=true and OPTIMIZER_BACKEND=lp.
    Skips if scheduler is paused or if OPERATION_MODE is not operational/simulation.
    """
    if not config.USE_BULLETPROOF_ENGINE:
        return
    if get_scheduler_paused():
        return
    backend = (config.OPTIMIZER_BACKEND or "lp").strip().lower()
    if backend != "lp":
        logger.debug("MPC skipped: OPTIMIZER_BACKEND=%s", backend)
        return
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE if config.USE_BULLETPROOF_ENGINE else config.OPTIMIZATION_TIMEZONE)
    now_local = datetime.now(tz)
    if mpc_should_skip_hour_for_octopus_fetch(now_local.hour):
        logger.info(
            "MPC skipped: local hour %02d matches OCTOPUS_FETCH_HOUR — fetch at %02d:%02d will run optimizer",
            now_local.hour,
            int(config.OCTOPUS_FETCH_HOUR),
            int(config.OCTOPUS_FETCH_MINUTE),
        )
        return
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

        # Only dispatch to hardware if explicitly enabled; default is compute-only.
        result = run_optimizer(
            fox if config.LP_MPC_WRITE_DEVICES else None,
            daikin if config.LP_MPC_WRITE_DEVICES else None,
        )
        logger.info(
            "MPC re-optimise: ok=%s lp_status=%s objective=%.0fp soc=%.1f%% solar=%.2fkW write_devices=%s",
            result.get("ok"),
            result.get("lp_status"),
            result.get("lp_objective_pence", 0),
            rt_soc_pct or 0,
            rt_solar_kw or 0,
            config.LP_MPC_WRITE_DEVICES,
        )
    except Exception as e:
        logger.warning("MPC job failed: %s", e)


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

        result = run_optimizer(fox, daikin)
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


def bulletproof_heartbeat_tick() -> None:
    """2-minute monitor: Daikin schedule execution, telemetry, Fox flag check."""
    global _last_exec_halfhour_key, _last_fox_verify_monotonic, _last_room_temp, _last_room_wall_utc, _last_notified_slot_kind
    import time

    if not config.USE_BULLETPROOF_ENGINE:
        return
    if get_scheduler_paused():
        return

    if config.DAIKIN_CLIENT_ID and config.DAIKIN_CLIENT_SECRET and config.DAIKIN_TOKEN_FILE.exists():
        try:
            from ..daikin.auth import prefetch_daikin_access_token

            prefetch_daikin_access_token()
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("Daikin OAuth prefetch (before device calls): %s", e)

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
            # Heartbeat reads from cache only — no auto-refresh to protect 200/day quota.
            # allow_refresh=True fires only when we are in the Octopus pre-slot window.
            in_pre_slot = _in_octopus_pre_slot_window(now_utc)
            daikin_result = daikin_service.get_cached_devices(
                allow_refresh=in_pre_slot,
                actor="heartbeat",
            )
            devices = daikin_result.devices
            if in_pre_slot and daikin_result.source == "fresh":
                logger.info(
                    "Daikin pre-slot refresh: fetched %d device(s) (next Octopus slot in <5 min)",
                    len(devices),
                )
        except Exception as e:
            logger.debug("Daikin heartbeat skip: %s", e)
            devices = []

    soc = None
    fox_mode = None
    try:
        rt = get_cached_realtime()
        soc = rt.soc
        fox_mode = rt.work_mode
    except Exception:
        pass

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
            if float(price) > float(tgt.get("peak_threshold") or 99):
                slot_kind = "peak"
            elif float(price) < float(tgt.get("cheap_threshold") or 0):
                slot_kind = "cheap"
            else:
                slot_kind = "standard"
        from ..analytics.shadow_pricing import fixed_shadow_rate_pence, svt_rate_pence

        svt = svt_rate_pence()
        fix = fixed_shadow_rate_pence()
        # v10.1: real per-tick consumption from Fox load_power × heartbeat interval.
        # Pre-v10.1 used db.mean_consumption_kwh_from_execution_logs() — a self-
        # referential constant (mean of itself) that produced fake per-slot values
        # in the cockpit. With Fox snapshot we get actual per-tick energy; the
        # global mean stays as fallback when Fox is unavailable.
        snap = db.get_fox_realtime_snapshot() or {}
        load_kw = snap.get("load_power_kw")
        if load_kw is not None:
            kwh_est = float(load_kw) * (config.HEARTBEAT_INTERVAL_SECONDS / 3600.0)
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

        if slot_kind != _last_notified_slot_kind:
            _last_notified_slot_kind = slot_kind
            if slot_kind in ("cheap", "negative"):
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

        if config.SCHEDULER_ENABLED and config.OCTOPUS_TARIFF_CODE and not config.USE_BULLETPROOF_ENGINE:
            _background_scheduler.add_job(
                run_scheduler_tick, "interval", minutes=30, id="agile_daikin"
            )
            logger.info("Agile Daikin scheduler started (every 30 min)")

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
                bulletproof_daily_brief_job,
                CronTrigger(
                    hour=config.DAILY_BRIEF_HOUR,
                    minute=config.DAILY_BRIEF_MINUTE,
                    timezone=tz,
                ),
                id="bulletproof_daily_brief",
            )
            # MPC intra-day re-runs (LP only): scheduled at each hour in LP_MPC_HOURS
            for mpc_hour in config.LP_MPC_HOURS_LIST:
                _background_scheduler.add_job(
                    bulletproof_mpc_job,
                    CronTrigger(hour=mpc_hour, minute=0, timezone=tz),
                    id=f"bulletproof_mpc_{mpc_hour:02d}",
                )
            if config.LP_MPC_HOURS_LIST:
                logger.info(
                    "MPC re-optimise cron scheduled at hours %s (%s)",
                    config.LP_MPC_HOURS_LIST,
                    tz,
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
            logger.info(
                "Bulletproof cron: Octopus %02d:%02d, brief %02d:%02d (%s); plan push %02d:%02d UTC",
                config.OCTOPUS_FETCH_HOUR,
                config.OCTOPUS_FETCH_MINUTE,
                config.DAILY_BRIEF_HOUR,
                config.DAILY_BRIEF_MINUTE,
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


def reregister_cron_jobs(reason: str = "runtime_settings_change") -> dict[str, Any]:
    """Tear down and re-create the cadence-tunable cron jobs (#52).

    Invoked by the settings PUT handler after ``LP_PLAN_PUSH_HOUR``,
    ``LP_PLAN_PUSH_MINUTE``, or ``LP_MPC_HOURS`` change. Jobs handled:

    - ``bulletproof_plan_push``: single UTC-anchored push.
    - ``bulletproof_mpc_*``: one per hour in ``LP_MPC_HOURS_LIST``.

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

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)

    removed: list[str] = []
    for job in list(_background_scheduler.get_jobs()):
        jid = job.id
        if jid == "bulletproof_plan_push" or jid.startswith("bulletproof_mpc_"):
            try:
                _background_scheduler.remove_job(jid)
                removed.append(jid)
            except Exception as e:
                logger.warning("remove_job(%s) failed: %s", jid, e)

    added: list[str] = []
    for mpc_hour in config.LP_MPC_HOURS_LIST:
        jid = f"bulletproof_mpc_{mpc_hour:02d}"
        _background_scheduler.add_job(
            bulletproof_mpc_job,
            CronTrigger(hour=mpc_hour, minute=0, timezone=tz),
            id=jid,
        )
        added.append(jid)

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

    logger.info(
        "Cron jobs re-registered (reason=%s): removed=%s added=%s "
        "plan_push=%02d:%02d UTC mpc_hours=%s",
        reason,
        removed,
        added,
        config.LP_PLAN_PUSH_HOUR,
        config.LP_PLAN_PUSH_MINUTE,
        config.LP_MPC_HOURS_LIST,
    )
    return {
        "status": "ok",
        "reason": reason,
        "removed": removed,
        "added": added,
        "plan_push_utc": f"{config.LP_PLAN_PUSH_HOUR:02d}:{config.LP_PLAN_PUSH_MINUTE:02d}",
        "mpc_hours": config.LP_MPC_HOURS_LIST,
    }
