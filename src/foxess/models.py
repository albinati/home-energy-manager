"""Pydantic models for Fox ESS API responses."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RealTimeData:
    """Real-time device snapshot."""
    soc: float = 0.0              # Battery state of charge (%)
    solar_power: float = 0.0      # PV generation (kW)
    grid_power: float = 0.0       # Grid import (+) / export (-) (kW)
    battery_power: float = 0.0    # Battery charge (+) / discharge (-) (kW)
    load_power: float = 0.0       # Home consumption (kW)
    generation_power: float = 0.0 # Total generation (kW)
    feed_in_power: float = 0.0    # Feed-in to grid (kW)
    work_mode: str = "unknown"    # e.g. "Self Use", "Feed-in Priority"


@dataclass
class ChargePeriod:
    """A timed charge/discharge period."""
    start_time: str    # "HH:MM"
    end_time: str      # "HH:MM"
    target_soc: int    # 0–100
    enable: bool = True


@dataclass
class DeviceInfo:
    """Basic device info."""
    device_sn: str
    device_type: str = ""
    station_name: str = ""
    status: str = "unknown"
