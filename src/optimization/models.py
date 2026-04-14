"""Dataclasses and enums for the V7 optimization engine (solver + dispatcher)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OperationPreset(str, Enum):
    """Household preset from architecture §3 — drives solver aggressiveness."""

    NORMAL = "normal"
    GUESTS = "guests"
    TRAVEL = "travel"


class SlotKind(str, Enum):
    """Tariff / time classification for a 30-minute block."""

    CHEAP = "cheap"
    PEAK = "peak"
    STANDARD = "standard"


class FoxESSWorkModeHint(str, Enum):
    """Suggested inverter mode for a window (dispatcher may clamp by SoC / safeties)."""

    SELF_USE = "Self Use"
    FORCE_CHARGE = "Force charge"
    FORCE_DISCHARGE = "Force discharge"
    FEED_IN_PRIORITY = "Feed-in Priority"


@dataclass(frozen=True)
class HalfHourSlotPlan:
    """One half-hour row in the 48-block daily plan."""

    valid_from: datetime
    valid_to: datetime
    import_price_pence: float
    slot_kind: SlotKind
    lwt_offset_delta: float
    fox_mode_hint: FoxESSWorkModeHint
    notes: str = ""


@dataclass
class SolverPlan:
    """Output of the solver: 48 blocks + headline financial target."""

    computed_at: datetime
    preset: OperationPreset
    tariff_code: str
    slots: list[HalfHourSlotPlan] = field(default_factory=list)
    target_mean_price_pence: float = 0.0
    cheap_slot_count: int = 0
    peak_slot_count: int = 0


@dataclass
class MacroSnapshot:
    """Macro sensors (§6 dispatcher) — room / DHW / battery for guardrails."""

    room_temp_c: Optional[float] = None
    tank_temp_c: Optional[float] = None
    tank_target_c: Optional[float] = None
    outdoor_temp_c: Optional[float] = None
    battery_soc_percent: Optional[float] = None
    weather_regulation_on: bool = False
    operation_mode: str = "heating"


@dataclass
class DispatchHints:
    """Concrete commands for this tick (may be applied or logged only)."""

    lwt_offset: float
    daikin_tank_target_c: Optional[float] = None
    fox_work_mode: Optional[str] = None
    disable_weather_regulation: bool = False
    reason: str = ""
