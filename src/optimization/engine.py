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
    """V7 structural orchestration — simulation-first, consent-gated dispatch."""

    def __init__(self) -> None:
        self._last_plan: Optional[SolverPlan] = None

    def is_enabled(self) -> bool:
        return bool(config.OPTIMIZATION_ENGINE_ENABLED and config.OCTOPUS_TARIFF_CODE)

    def is_operational(self) -> bool:
        """True only when OPERATION_MODE=operational (writes to hardware enabled)."""
        return config.OPERATION_MODE == "operational"

    def watchdog_tick(self) -> None:
        """Refresh Agile rates (scheduled ~16:00 local)."""
        if not config.OCTOPUS_TARIFF_CODE:
            return
        refresh_agile_rates()

    def solve_from_cache(
        self,
        preset: Optional[OperationPreset] = None,
        target_price_pence: Optional[float] = None,
    ) -> Optional[SolverPlan]:
        """Run solver on cached rates, respecting TARGET_PRICE_PENCE and export rates."""
        cache = get_agile_cache()
        if not cache.rates:
            return None
        pr = preset or _preset_from_config()
        tgt = target_price_pence if target_price_pence is not None else config.TARGET_PRICE_PENCE
        soc: Optional[float] = None
        try:
            from ..foxess.service import get_cached_realtime

            soc = float(get_cached_realtime().soc)
        except Exception:
            pass
        self._last_plan = solve_plan(
            cache.rates,
            preset=pr,
            tariff_code=cache.tariff_code or None,
            target_price_pence=tgt if tgt > 0 else None,
            export_rates=cache.export_rates or None,
            battery_soc_percent=soc,
        )
        return self._last_plan

    def get_last_plan(self) -> Optional[SolverPlan]:
        return self._last_plan

    def apply_v7_safeties(self, pr: OperationPreset) -> dict:
        """Return safeties mapped to current preset (from config)."""
        away_like = pr in (OperationPreset.AWAY, OperationPreset.TRAVEL)
        dhw_min = config.TARGET_DHW_TEMP_MIN_NORMAL_C
        if pr == OperationPreset.GUESTS:
            dhw_min = config.TARGET_DHW_TEMP_MIN_GUESTS_C
        elif away_like:
            dhw_min = 10.0  # hibernating — Legionella cycle only

        return {
            "room_temp_min": 12.0 if away_like else config.TARGET_ROOM_TEMP_MIN_C,
            "room_temp_max": config.TARGET_ROOM_TEMP_MAX_C,
            "dhw_temp_min": dhw_min,
            "dhw_temp_max": config.TARGET_DHW_TEMP_MAX_C,
            "min_soc_reserve": config.MIN_SOC_RESERVE_PERCENT,
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

        safeties = self.apply_v7_safeties(pr)
        logger.debug("Applied V7 safeties for preset %s: %s", pr.value, safeties)

        return compute_dispatch_hints(plan, macro, preset=pr, base_lwt_offset=base_lwt_offset)

    def status_dict(self) -> dict:
        """Lightweight JSON-friendly status for GET /optimization/status."""
        from .consent import consent_status_dict
        cache = get_agile_cache()
        fetched = cache.fetched_at_utc
        pr = _preset_from_config()
        return {
            "enabled": self.is_enabled(),
            "operation_mode": config.OPERATION_MODE,
            "preset": config.OPTIMIZATION_PRESET,
            "energy_strategy_mode": config.ENERGY_STRATEGY_MODE,
            "export_discharge_min_soc_percent": config.EXPORT_DISCHARGE_MIN_SOC_PERCENT,
            "target_price_pence": config.TARGET_PRICE_PENCE,
            "tariff_code": config.OCTOPUS_TARIFF_CODE or None,
            "cache_slots": len(cache.rates or []),
            "cache_fetched_at_utc": fetched.isoformat() if fetched else None,
            "cache_error": cache.error,
            "last_plan_at_utc": (
                self._last_plan.computed_at.isoformat() if self._last_plan else None
            ),
            "target_mean_price_pence": (
                self._last_plan.target_mean_price_pence if self._last_plan else None
            ),
            "v7_safeties": self.apply_v7_safeties(pr),
            "consent": consent_status_dict(),
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
    """Scheduler hook: execute dispatch (evaluate plan and apply to devices)."""
    eng = get_optimization_engine()
    if not eng.is_enabled():
        return
    try:
        from .executor import execute_dispatch
        execute_dispatch()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Optimization dispatch failed: %s", e)
