"""V7 predictive optimization: Watchdog (Agile), Solver (48 half-hours), Dispatcher (macro sensors).

Architecture reference: home_energy_architecture V7 — feedback loop with Octopus Agile,
Fox ESS work modes / charge windows, and Daikin LWT offset + DHW targets under hard safeties.
"""
from .models import (
    DispatchHints,
    FoxESSWorkModeHint,
    HalfHourSlotPlan,
    MacroSnapshot,
    OperationPreset,
    SlotKind,
    SolverPlan,
)

def __getattr__(name: str):
    """Lazy attribute loading to avoid circular imports during module execution."""
    if name == "OptimizationEngine":
        from .engine import OptimizationEngine
        return OptimizationEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
