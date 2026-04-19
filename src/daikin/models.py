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
    is_on: bool = False
    operation_mode: str = "heating"
    temperature: TemperatureControlSettings = field(default_factory=TemperatureControlSettings)
    leaving_water_temperature: float | None = None
    lwt_offset: float | None = None
    tank_temperature: float | None = None
    tank_target: float | None = None
    tank_target_min: float | None = None
    tank_target_max: float | None = None
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
    """Summarised status for display."""
    device_name: str
    is_on: bool
    mode: str
    room_temp: float | None
    target_temp: float | None
    outdoor_temp: float | None
    lwt: float | None
    lwt_offset: float | None
    tank_temp: float | None
    tank_target: float | None
    weather_regulation: bool
