"""Pydantic-style models for Daikin Onecta API."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TemperatureControlSettings:
    set_point: Optional[float] = None
    room_temperature: Optional[float] = None
    outdoor_temperature: Optional[float] = None


@dataclass
class DaikinDevice:
    """A Daikin gateway device (usually one per unit)."""
    id: str
    name: str
    model: str = ""
    is_on: bool = False
    operation_mode: str = "heating"
    temperature: TemperatureControlSettings = field(default_factory=TemperatureControlSettings)
    leaving_water_temperature: Optional[float] = None
    lwt_offset: Optional[float] = None
    tank_temperature: Optional[float] = None
    tank_target: Optional[float] = None
    tank_target_min: Optional[float] = None
    tank_target_max: Optional[float] = None
    weather_regulation_enabled: bool = False
    climate_mp_id: str = "climateControlMainZone"
    dhw_mp_id: str = "domesticHotWaterTank"
    raw: dict = field(default_factory=dict)


@dataclass
class DaikinStatus:
    """Summarised status for display."""
    device_name: str
    is_on: bool
    mode: str
    room_temp: Optional[float]
    target_temp: Optional[float]
    outdoor_temp: Optional[float]
    lwt: Optional[float]
    lwt_offset: Optional[float]
    tank_temp: Optional[float]
    tank_target: Optional[float]
    weather_regulation: bool
