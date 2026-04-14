"""Optimization engine façade: watchdog cache + solver + dispatch hints."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..config import config
from .dispatcher import DispatchHints, MacroSnapshot, compute_dispatch_hints
from .models import OperationPreset, SolverPlan
from .solver import solve_plan
from .watchdog import get_agile_cache, refresh_agile_rates

logger = logging.getLogger(__name__)

def _preset_from_config() -> OperationPreset:
    try:
        return OperationPreset(config.OPTIMIZATION_PRESET)
    except ValueError:
        return OperationPreset.NORMAL


class OptimizationEngine:
    """V7 structural orchestration (read-mostly; writes belong to a future executor)."""

    def __init__(self) -> None:
        self._last_plan: Optional[SolverPlan] = None
        # V7 Architecture Constants
        self.TARGET_ROOM_TEMP_MIN = 20.0
        self.TARGET_ROOM_TEMP_MAX = 23.0
        self.TARGET_DHW_TEMP_MIN_NORMAL = 45.0
        self.TARGET_DHW_TEMP_MIN_GUESTS = 48.0
        self.TARGET_DHW_TEMP_MAX = 65.0
        self.MIN_SOC_RESERVE = 15.0

    def is_enabled(self) -> bool:
        return bool(config.OPTIMIZATION_ENGINE_ENABLED and config.OCTOPUS_TARIFF_CODE)

    def watchdog_tick(self) -> None:
        """Refresh Agile rates (scheduled ~16:00 local)."""
        if not config.OCTOPUS_TARIFF_CODE:
            return
        refresh_agile_rates()

    def solve_from_cache(
        self,
        preset: Optional[OperationPreset] = None,
    ) -> Optional[SolverPlan]:
        """Run solver on cached rates."""
        cache = get_agile_cache()
        if not cache.rates:
            return None
        pr = preset or _preset_from_config()
        self._last_plan = solve_plan(
            cache.rates,
            preset=pr,
            tariff_code=cache.tariff_code or None,
        )
        return self._last_plan

    def get_last_plan(self) -> Optional[SolverPlan]:
        return self._last_plan

    def apply_v7_safeties(self, pr: OperationPreset) -> dict:
        """Return safeties mapped to current preset."""
        dhw_min = self.TARGET_DHW_TEMP_MIN_NORMAL
        if pr == OperationPreset.GUESTS:
            dhw_min = self.TARGET_DHW_TEMP_MIN_GUESTS
        elif pr == OperationPreset.AWAY:
            dhw_min = 10.0 # hibernating

        return {
            "room_temp_min": self.TARGET_ROOM_TEMP_MIN if pr != OperationPreset.AWAY else 12.0,
            "room_temp_max": self.TARGET_ROOM_TEMP_MAX,
            "dhw_temp_min": dhw_min,
            "dhw_temp_max": self.TARGET_DHW_TEMP_MAX,
            "min_soc_reserve": self.MIN_SOC_RESERVE
        }

    def dispatch_hints(
        self,
        macro: MacroSnapshot,
        *,
        preset: Optional[OperationPreset] = None,
        base_lwt_offset: float = 0.0,
    ) -> Optional[DispatchHints]:
        """Compute hints for the current half-hour; requires a prior :meth:`solve_from_cache`."""
        plan = self._last_plan
        if plan is None:
            plan = self.solve_from_cache(preset=preset)
        if plan is None:
            return None
        pr = preset or _preset_from_config()
        
        # Inject V7 safeties into hint computation context
        safeties = self.apply_v7_safeties(pr)
        logger.debug(f"Applied V7 safeties for preset {pr.value}: {safeties}")
        
        return compute_dispatch_hints(plan, macro, preset=pr, base_lwt_offset=base_lwt_offset)

    def status_dict(self) -> dict:
        """Lightweight JSON-friendly status for GET /optimization/status."""
        cache = get_agile_cache()
        fetched = cache.fetched_at_utc
        return {
            "enabled": self.is_enabled(),
            "preset": config.OPTIMIZATION_PRESET,
            "tariff_code": config.OCTOPUS_TARIFF_CODE or None,
            "cache_slots": len(cache.rates or []),
            "cache_fetched_at_utc": fetched.isoformat() if fetched else None,
            "cache_error": cache.error,
            "last_plan_at_utc": (
                self._last_plan.computed_at.isoformat()
                if self._last_plan
                else None
            ),
            "target_mean_price_pence": (
                self._last_plan.target_mean_price_pence if self._last_plan else None
            ),
            "v7_safeties": self.apply_v7_safeties(_preset_from_config())
        }


_default_engine: Optional[OptimizationEngine] = None


def get_optimization_engine() -> OptimizationEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = OptimizationEngine()
    return _default_engine


def optimization_watchdog_job() -> None:
    get_optimization_engine().watchdog_tick()


def optimization_dispatch_job() -> None:
    """Scheduler hook: refresh plan from cache (no extra Octopus call if watchdog filled cache)."""
    eng = get_optimization_engine()
    if not eng.is_enabled():
        return
    eng.solve_from_cache()
