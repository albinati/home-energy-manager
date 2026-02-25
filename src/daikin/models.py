"""Pydantic-style models for Daikin Onecta API."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TemperatureControlSettings:
    set_point: Optional[float] = None    # Target temperature (°C)
    room_temperature: Optional[float] = None  # Current indoor temp
    outdoor_temperature: Optional[float] = None  # Outdoor temp (if available)


@dataclass
class DaikinDevice:
    """A Daikin gateway device (usually one per unit)."""
    id: str
    name: str
    model: str = ""
    is_on: bool = False
    operation_mode: str = "heating"   # heating / cooling / auto / fan_only / dry
    temperature: TemperatureControlSettings = field(default_factory=TemperatureControlSettings)
    leaving_water_temperature: Optional[float] = None   # Altherma LWT (°C)
    weather_regulation_enabled: bool = False
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
    lwt: Optional[float]          # Leaving water temperature (Altherma)
    weather_regulation: bool
