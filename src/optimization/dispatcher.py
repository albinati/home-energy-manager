"""Dispatcher: map solver output + macro sensors to concrete hints (V7 §6)."""
from __future__ import annotations

from typing import Optional

from ..config import config
from .models import DispatchHints, MacroSnapshot, OperationPreset, SolverPlan
from .safeties import clamp_dhw_target_c, clamp_lwt_offset, load_limits
from .solver import current_slot_plan


def build_macro_from_clients(
    *,
    room_temp: Optional[float] = None,
    tank_temp: Optional[float] = None,
    tank_target: Optional[float] = None,
    outdoor_temp: Optional[float] = None,
    battery_soc: Optional[float] = None,
    weather_regulation: bool = False,
    operation_mode: str = "heating",
) -> MacroSnapshot:
    """Helper for tests and service layer when device clients already ran."""
    return MacroSnapshot(
        room_temp_c=room_temp,
        tank_temp_c=tank_temp,
        tank_target_c=tank_target,
        outdoor_temp_c=outdoor_temp,
        battery_soc_percent=battery_soc,
        weather_regulation_on=weather_regulation,
        operation_mode=operation_mode or "heating",
    )


def compute_dispatch_hints(
    plan: SolverPlan,
    macro: MacroSnapshot,
    *,
    preset: OperationPreset = OperationPreset.NORMAL,
    base_lwt_offset: float = 0.0,
) -> DispatchHints:
    """Derive this tick's LWT / DHW / Fox suggestions without calling vendor APIs."""
    limits = load_limits()
    row = current_slot_plan(plan)
    lwt_delta = row.lwt_offset_delta if row else 0.0
    lwt = clamp_lwt_offset(base_lwt_offset + lwt_delta)

    # DHW target from preset (architecture §3); dispatcher only suggests; comfort logic expands later.
    if preset == OperationPreset.TRAVEL:
        tank_target: Optional[float] = None
        reason = "travel: DHW off except Legionella Sunday (not automated in baseline)"
    else:
        base_tank = (
            limits.target_dhw_temp_min_guests_c
            if preset == OperationPreset.GUESTS
            else limits.target_dhw_temp_min_normal_c
        )
        if row and row.slot_kind.value == "cheap" and preset != OperationPreset.TRAVEL:
            tank_target = clamp_dhw_target_c(
                min(limits.target_dhw_temp_max_c, base_tank + 5.0),
                limits,
                preset.value,
            )
        else:
            tank_target = clamp_dhw_target_c(base_tank, limits, preset.value)
        reason = f"preset={preset.value} slot={row.slot_kind.value if row else 'unknown'}"

    fox_mode: Optional[str] = None
    if row:
        fox_mode = row.fox_mode_hint.value

    if macro.battery_soc_percent is not None and fox_mode == "Force discharge":
        if macro.battery_soc_percent <= limits.min_soc_reserve_percent + 0.5:
            fox_mode = "Self Use"
            reason += "; fox=Self Use (SoC at reserve floor)"

    disable_weather = config.OPTIMIZATION_DISABLE_WEATHER_REGULATION
    if macro.weather_regulation_on and disable_weather:
        reason += "; recommend fixed setpoint mode (weather regulation on)"

    return DispatchHints(
        lwt_offset=lwt,
        daikin_tank_target_c=tank_target,
        fox_work_mode=fox_mode,
        disable_weather_regulation=bool(macro.weather_regulation_on and disable_weather),
        reason=reason.strip(),
    )
