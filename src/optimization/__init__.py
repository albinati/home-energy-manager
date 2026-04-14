"""V7 predictive optimization: Watchdog (Agile), Solver (48 half-hours), Dispatcher (macro sensors).

Architecture reference: home_energy_architecture V7 — feedback loop with Octopus Agile,
Fox ESS work modes / charge windows, and Daikin LWT offset + DHW targets under hard safeties.
"""
from .engine import OptimizationEngine
from .models import (
    DispatchHints,
    FoxESSWorkModeHint,
    HalfHourSlotPlan,
    MacroSnapshot,
    OperationPreset,
    SlotKind,
    SolverPlan,
)

__all__ = [
    "OptimizationEngine",
    "OperationPreset",
    "SlotKind",
    "HalfHourSlotPlan",
    "SolverPlan",
    "MacroSnapshot",
    "DispatchHints",
    "FoxESSWorkModeHint",
]
