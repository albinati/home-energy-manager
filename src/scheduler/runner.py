"""Scheduler runner: status, pause/resume, and periodic tick (APScheduler)."""
import logging
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..config import config
from .agile import fetch_agile_rates, get_current_and_next_slots
from .daikin import compute_lwt_adjustment, run_daikin_scheduler_tick

logger = logging.getLogger(__name__)

_scheduler_paused: bool = False
_background_scheduler: Any = None


def get_scheduler_paused() -> bool:
    return _scheduler_paused


def pause_scheduler() -> None:
    global _scheduler_paused
    _scheduler_paused = True


def resume_scheduler() -> None:
    global _scheduler_paused
    _scheduler_paused = False


def get_scheduler_status() -> dict:
    """Return current price, next cheap window, planned LWT adjustment, and paused state."""
    out = {
        "enabled": config.SCHEDULER_ENABLED,
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
    if current_price is not None and not get_scheduler_paused():
        out["planned_lwt_adjustment"] = compute_lwt_adjustment(
            current_price,
            config.SCHEDULER_CHEAP_THRESHOLD_PENCE,
            config.SCHEDULER_PEAK_START,
            config.SCHEDULER_PEAK_END,
            config.SCHEDULER_PREHEAT_LWT_BOOST,
        )
    return out


def run_scheduler_tick() -> Optional[str]:
    """Run one scheduler tick (fetch rates, adjust Daikin LWT). Returns error message or None."""
    return run_daikin_scheduler_tick(get_scheduler_paused())


def start_background_scheduler() -> None:
    """Start APScheduler job that runs every 30 minutes."""
    from ..optimization.engine import optimization_dispatch_job, optimization_watchdog_job
    global _background_scheduler
    if _background_scheduler is not None:
        return
    if not config.OCTOPUS_TARIFF_CODE:
        return
    if not config.SCHEDULER_ENABLED and not config.OPTIMIZATION_ENGINE_ENABLED:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        _background_scheduler = BackgroundScheduler()
        if config.SCHEDULER_ENABLED and config.OCTOPUS_TARIFF_CODE:
            _background_scheduler.add_job(
                run_scheduler_tick, "interval", minutes=30, id="agile_daikin"
            )
            logger.info("Agile Daikin scheduler started (every 30 min)")
        if config.OPTIMIZATION_ENGINE_ENABLED and config.OCTOPUS_TARIFF_CODE:
            tz = ZoneInfo(config.OPTIMIZATION_TIMEZONE)
            _background_scheduler.add_job(
                optimization_watchdog_job,
                CronTrigger(
                    hour=config.OPTIMIZATION_WATCHDOG_HOUR_LOCAL,
                    minute=config.OPTIMIZATION_WATCHDOG_MINUTE_LOCAL,
                    timezone=tz,
                ),
                id="optimization_agile_watchdog",
            )
            _background_scheduler.add_job(
                optimization_dispatch_job,
                "interval",
                minutes=30,
                id="optimization_solver_refresh",
            )
            logger.info(
                "Optimization engine jobs started (watchdog %02d:%02d %s, solver every 30 min)",
                config.OPTIMIZATION_WATCHDOG_HOUR_LOCAL,
                config.OPTIMIZATION_WATCHDOG_MINUTE_LOCAL,
                config.OPTIMIZATION_TIMEZONE,
            )
        _background_scheduler.start()
        if config.OPTIMIZATION_ENGINE_ENABLED and config.OCTOPUS_TARIFF_CODE:
            try:
                optimization_watchdog_job()
                optimization_dispatch_job()
            except Exception as e:
                logger.warning("Optimization engine bootstrap failed: %s", e)
    except Exception as e:
        logger.warning("Could not start background scheduler: %s", e)


def stop_background_scheduler() -> None:
    global _background_scheduler
    if _background_scheduler is None:
        return
    try:
        _background_scheduler.shutdown(wait=False)
    except Exception:
        pass
    _background_scheduler = None
    logger.info("Agile Daikin scheduler stopped")
