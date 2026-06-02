"""LWT offset adjustment from the live Agile rate position.

Only ``compute_lwt_adjustment`` survives — it feeds the scheduler-status display
(``runner.get_scheduler_status`` → ``planned_lwt_adjustment``). The old
``run_daikin_scheduler_tick`` / ``apply_scheduler_offset`` legacy LWT tick (only
ever wired under ``not USE_BULLETPROOF_ENGINE``) was removed as dead code.
"""
from datetime import UTC, datetime

from ..config import config
from .agile import utc_instant_in_scheduler_peak


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
