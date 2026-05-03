"""Pydantic-style models for Daikin Onecta API."""
from dataclasses import dataclass, field


@dataclass
class SetpointRange:
    """Min/max/step for a settable value."""
    min_value: float | None = None
    max_value: float | None = None
    step_value: float | None = None
    settable: bool = True


@dataclass
class TemperatureControlSettings:
    set_point: float | None = None
    room_temperature: float | None = None
    outdoor_temperature: float | None = None


@dataclass
class DaikinDevice:
    """A Daikin gateway device (usually one per unit)."""
    id: str
    name: str
    model: str = ""
    # Optional so ``detect_user_override``'s ``is_on is not None`` guard can distinguish
    # "device reports off" from "unknown / parse did not populate". Without this, a parse
    # glitch would leave is_on=False and false-flag climate_on=True rows as overridden.
    is_on: bool | None = None
    operation_mode: str = "heating"
    temperature: TemperatureControlSettings = field(default_factory=TemperatureControlSettings)
    leaving_water_temperature: float | None = None
    lwt_offset: float | None = None
    tank_temperature: float | None = None
    tank_target: float | None = None
    tank_target_min: float | None = None
    tank_target_max: float | None = None
    tank_on: bool | None = None
    tank_powerful: bool | None = None
    weather_regulation_enabled: bool = False
    weather_regulation_settable: bool = True
    lwt_offset_range: SetpointRange = field(default_factory=SetpointRange)
    room_temp_range: SetpointRange = field(default_factory=SetpointRange)
    tank_temp_range: SetpointRange = field(default_factory=SetpointRange)
    climate_mp_id: str = "climateControlMainZone"
    dhw_mp_id: str = "domesticHotWaterTank"
    raw: dict = field(default_factory=dict)


@dataclass
class DaikinStatus:
    """Summarised status for display.

    Naming caveat for downstream consumers (especially LLM agents): ``is_on``
    is **only** the climate (room-heating) zone's onOffMode — it does NOT
    mean "is the heat pump powered on" and it does NOT cover DHW. Use
    ``climate_on`` / ``dhw_on`` for unambiguous semantics; ``is_on`` is kept
    for backwards-compatibility with existing callers.
    """
    device_name: str
    is_on: bool             # alias of climate_on (deprecated label kept for compat)
    mode: str
    room_temp: float | None
    target_temp: float | None
    outdoor_temp: float | None
    lwt: float | None
    lwt_offset: float | None
    tank_temp: float | None
    tank_target: float | None
    weather_regulation: bool
    climate_on: bool | None = None  # climate (space-heating) zone onOffMode
    dhw_on: bool | None = None      # DHW (tank) zone onOffMode
