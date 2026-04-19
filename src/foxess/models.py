"""Dataclasses for Fox ESS API responses."""
from dataclasses import dataclass, field
from typing import Any


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


@dataclass
class SchedulerGroup:
    """One Scheduler V3 time segment (Fox Open API v3)."""

    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    work_mode: str  # SelfUse, ForceCharge, ForceDischarge, Feedin, Backup
    min_soc_on_grid: int = 10
    fd_soc: int | None = None
    fd_pwr: int | None = None
    max_soc: int | None = None
    import_limit: int | None = None
    export_limit: int | None = None

    def to_api_dict(self) -> dict[str, Any]:
        """Build group payload for /op/v3/device/scheduler/enable."""
        extra: dict[str, Any] = {"minSocOnGrid": self.min_soc_on_grid}
        if self.fd_soc is not None:
            extra["fdSoc"] = self.fd_soc
        if self.fd_pwr is not None:
            extra["fdPwr"] = self.fd_pwr
        if self.max_soc is not None:
            extra["maxSoc"] = self.max_soc
        if self.import_limit is not None:
            extra["importLimit"] = self.import_limit
        if self.export_limit is not None:
            extra["exportLimit"] = self.export_limit
        return {
            "startHour": self.start_hour,
            "startMinute": self.start_minute,
            "endHour": self.end_hour,
            "endMinute": self.end_minute,
            "workMode": self.work_mode,
            "extraParam": extra,
        }


@dataclass
class SchedulerState:
    """Parsed Scheduler V3 state from device."""

    enabled: bool
    groups: list[SchedulerGroup]
    max_group_count: int = 8
    properties: dict[str, Any] = field(default_factory=dict)
