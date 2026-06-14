"""Dataclasses for Fox ESS API responses."""
from dataclasses import dataclass, field
from typing import Any

# Modes that actually use fdSoc/fdPwr. On every other mode the inverter still
# ECHOES whatever fd_* last occupied that clock window — stale leftovers that
# must be ignored when comparing a live read against what we uploaded.
_FD_MODES = ("ForceCharge", "ForceDischarge")


def _group_fingerprint(
    start_hour: int, start_minute: int, end_hour: int, end_minute: int,
    work_mode: str | None, min_soc_on_grid: Any,
    fd_soc: Any, fd_pwr: Any, max_soc: Any,
) -> tuple:
    """Canonical, mode-aware fingerprint of one Scheduler V3 group.

    Shared by ``SchedulerGroup.fingerprint`` and the heartbeat drift check so
    both agree with the inverter's echo. See ``SchedulerGroup.fingerprint`` for
    the why (2026-06-14 ~41 h Fox-upload wedge; vendor-echo class of #554).

    Deliberately EXCLUDES import/export limits: the LP/heuristic never sets them
    (always None), so they'd only ever carry a vendor echo on read-back — the
    exact phantom-drift class this fixes. The proven #554 comparator excludes
    them too. If they ever become LP-driven, canonicalise them here first.
    """
    def _f(v: Any) -> float | None:
        return None if v is None else float(v)

    fd_relevant = work_mode in _FD_MODES
    return (
        start_hour, start_minute, end_hour, end_minute, work_mode,
        int(min_soc_on_grid) if min_soc_on_grid is not None else None,
        _f(fd_soc) if fd_relevant else None,
        _f(fd_pwr) if fd_relevant else None,
        # Absent maxSoc == the vendor default 100 (Fox fills it on read-back).
        100.0 if max_soc is None else _f(max_soc),
    )


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

    def fingerprint(self) -> tuple:
        """Stable, MODE-AWARE representation for the skip-when-unchanged guard
        (#38) and live-vs-stored drift checks.

        Canonicalised so a re-read of the SAME schedule from the inverter
        compares equal to what we uploaded. Fox echoes back STALE ``fdSoc`` /
        ``fdPwr`` on SelfUse/Backup/Feedin groups (leftovers from whatever
        ForceCharge/ForceDischarge once occupied that clock window) and fills an
        absent ``maxSoc`` with the vendor default 100 — both of which made a raw
        fingerprint perpetually disagree with the device, wedging Fox uploads
        for ~41 h on 2026-06-14 (the same vendor-echo class fixed for the
        ``schedule_diff`` endpoint in #554; this is the comparator that drives
        ``set_scheduler_v3(skip_if_equal=...)`` and the heartbeat re-upload).

        Rules: ``fdSoc``/``fdPwr`` count only for ForceCharge/ForceDischarge
        (the only modes that use them); absent ``maxSoc`` == 100; numerics
        coerced to float so ``31`` and ``31.0`` match.
        """
        return _group_fingerprint(
            self.start_hour, self.start_minute, self.end_hour, self.end_minute,
            self.work_mode, self.min_soc_on_grid, self.fd_soc, self.fd_pwr,
            self.max_soc,
        )


@dataclass
class SchedulerState:
    """Parsed Scheduler V3 state from device."""

    enabled: bool
    groups: list[SchedulerGroup]
    max_group_count: int = 8
    properties: dict[str, Any] = field(default_factory=dict)
