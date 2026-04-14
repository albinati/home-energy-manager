"""V7 §2 hard limits — loaded from config, used by solver and dispatcher."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import config


@dataclass(frozen=True)
class OptimizationLimits:
    """Universal safeties for room, DHW, and battery."""

    target_room_temp_min_c: float
    target_room_temp_max_c: float
    target_dhw_temp_min_normal_c: float
    target_dhw_temp_min_guests_c: float
    target_dhw_temp_max_c: float
    min_soc_reserve_percent: float


def load_limits() -> OptimizationLimits:
    return OptimizationLimits(
        target_room_temp_min_c=config.TARGET_ROOM_TEMP_MIN_C,
        target_room_temp_max_c=config.TARGET_ROOM_TEMP_MAX_C,
        target_dhw_temp_min_normal_c=config.TARGET_DHW_TEMP_MIN_NORMAL_C,
        target_dhw_temp_min_guests_c=config.TARGET_DHW_TEMP_MIN_GUESTS_C,
        target_dhw_temp_max_c=config.TARGET_DHW_TEMP_MAX_C,
        min_soc_reserve_percent=config.MIN_SOC_RESERVE_PERCENT,
    )


def clamp_dhw_target_c(value: float, limits: OptimizationLimits, preset: str) -> float:
    """Clamp DHW setpoint to preset min / global max."""
    lo = (
        limits.target_dhw_temp_min_guests_c
        if preset == "guests"
        else limits.target_dhw_temp_min_normal_c
    )
    return max(lo, min(limits.target_dhw_temp_max_c, value))


def clamp_lwt_offset(
    offset: float,
    min_offset: float | None = None,
    max_offset: float | None = None,
) -> float:
    """Clamp LWT offset to device or config bounds."""
    lo = min_offset if min_offset is not None else config.OPTIMIZATION_LWT_OFFSET_MIN
    hi = max_offset if max_offset is not None else config.OPTIMIZATION_LWT_OFFSET_MAX
    return max(lo, min(hi, offset))
