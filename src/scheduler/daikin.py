"""Translate Agile rate forecast into Daikin LWT offset adjustments."""
import logging
from datetime import UTC, datetime

from ..config import config
from ..daikin import service as daikin_service
from .agile import (
    fetch_agile_rates,
    get_current_and_next_slots,
    utc_instant_in_scheduler_peak,
)

logger = logging.getLogger(__name__)

def compute_lwt_adjustment(
    current_price_pence: float | None,
    cheap_threshold_pence: float,
    peak_start: str,
    peak_end: str,
    preheat_boost: float,
) -> float:
    """Return target LWT offset for scheduler: +preheat_boost in cheap slots, -2 in peak, 0 otherwise."""
    if current_price_pence is None:
        return 0.0
    if current_price_pence <= cheap_threshold_pence:
        return preheat_boost

    now_utc = datetime.now(UTC)
    if utc_instant_in_scheduler_peak(
        now_utc, peak_start, peak_end, config.BULLETPROOF_TIMEZONE
    ):
        return -min(2.0, preheat_boost)
    return 0.0


def apply_scheduler_offset(
    base_offset: float,
    adjustment: float,
    min_offset: float = -10,
    max_offset: float = 10,
) -> float:
    """Clamp base_offset + adjustment to [min_offset, max_offset]."""
    return max(min_offset, min(max_offset, base_offset + adjustment))


def run_daikin_scheduler_tick(is_paused: bool) -> str | None:
    """Fetch rates, compute LWT delta, set LWT offset via the cached service. Returns error message or None."""
    if is_paused:
        return None

    if not config.OCTOPUS_TARIFF_CODE:
        return "OCTOPUS_TARIFF_CODE not set"
    if not config.SCHEDULER_ENABLED:
        return None

    rates = fetch_agile_rates()
    if not rates:
        return "No Agile rates fetched"

    current, _next_cheap, current_price = get_current_and_next_slots(
        rates,
        cheap_threshold_pence=config.SCHEDULER_CHEAP_THRESHOLD_PENCE,
        peak_start=config.SCHEDULER_PEAK_START,
        peak_end=config.SCHEDULER_PEAK_END,
    )
    if current is None or current_price is None:
        return None

    adjustment = compute_lwt_adjustment(
        current_price,
        config.SCHEDULER_CHEAP_THRESHOLD_PENCE,
        config.SCHEDULER_PEAK_START,
        config.SCHEDULER_PEAK_END,
        config.SCHEDULER_PREHEAT_LWT_BOOST,
    )
    try:
        result = daikin_service.get_cached_devices(
            allow_refresh=True,
            max_age_seconds=config.DAIKIN_LEGACY_TICK_CACHE_MAX_AGE_SECONDS,
            actor="legacy_lwt_tick",
        )
        if not result.devices:
            return "No Daikin devices"
        dev = result.devices[0]
        # Use absolute target offset (no compounding with previous value)
        new_offset = apply_scheduler_offset(adjustment, 0.0, -10, 10)
        mode = dev.operation_mode or "heating"
        daikin_service.set_lwt_offset(new_offset, mode=mode, actor="legacy_lwt_tick")
        logger.info("Scheduler LWT offset set to %s (adjustment %s)", new_offset, adjustment)
        return None
    except Exception as e:
        logger.exception("Scheduler Daikin tick failed")
        return str(e)
