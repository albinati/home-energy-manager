"""Pure-function diff computers for every state-changing API route.

Each function builds an :class:`~src.api.simulation.ActionDiff` from cached
state only (no cloud API calls). The handlers in ``main.py`` call these from
their ``/simulate`` routes and register the result with the store.

Hard rule: these functions MUST NOT call:
- ``DaikinClient.set_*`` or ``FoxESSClient.set_*``
- ``daikin_service.force_refresh_devices``
- ``foxess_service.force_refresh``
- Anything that issues HTTP to ``cloud.daikineurope.com`` or ``foxesscloud.com``

They may read:
- ``daikin_service.get_cached_devices(allow_refresh=False)``
- ``foxess_service.get_cached_realtime(allow_refresh=False)``
- SQLite via ``src.db``
- ``src.runtime_settings.get_setting``
"""
from __future__ import annotations

from typing import Any

from ..config import config
from ..daikin import service as daikin_service
from ..daikin.models import DaikinDevice
from ..runtime_settings import get_setting
from .simulation import ActionDiff


# ---------------------------------------------------------------------------
# Daikin diff computers
# ---------------------------------------------------------------------------

def _cached_first_device() -> DaikinDevice | None:
    """Read first device from cache only. Never refreshes."""
    try:
        result = daikin_service.get_cached_devices(allow_refresh=False, actor="simulate")
        return result.devices[0] if result.devices else None
    except Exception:
        return None


def _passive_mode_flag() -> list[str]:
    """Return a safety flag if Daikin is in passive mode (write would be blocked)."""
    if config.DAIKIN_CONTROL_MODE == "passive":
        return ["passive_mode_active"]
    return []


def diff_daikin_power(on: bool) -> ActionDiff:
    dev = _cached_first_device()
    before = {"is_on": getattr(dev, "is_on", None) if dev else None}
    after = {"is_on": bool(on)}
    flags = _passive_mode_flag()
    if dev is None:
        flags.append("no_cached_device")
    summary = (
        f"Turn Daikin climate {'ON' if on else 'OFF'} "
        f"(currently {'ON' if before['is_on'] else 'OFF' if before['is_on'] is False else 'unknown'})"
    )
    return ActionDiff(
        action="daikin.set_power",
        before=before,
        after=after,
        safety_flags=flags,
        human_summary=summary,
    )


def diff_daikin_temperature(temperature: float, mode: str | None) -> ActionDiff:
    dev = _cached_first_device()
    before = {
        "target_temp": getattr(dev, "target_temp", None) if dev else None,
        "weather_regulation": getattr(dev, "weather_regulation_enabled", None) if dev else None,
    }
    after = {"target_temp": float(temperature), "mode": mode or "heating"}
    flags = _passive_mode_flag()
    if dev is None:
        flags.append("no_cached_device")
    if before["weather_regulation"]:
        flags.append("weather_regulation_blocks_room_temperature")
    summary = (
        f"Set Daikin target room temperature → {temperature}°C "
        f"(currently {before['target_temp']}°C)"
    )
    return ActionDiff(
        action="daikin.set_temperature",
        before=before,
        after=after,
        safety_flags=flags,
        human_summary=summary,
    )


def diff_daikin_lwt_offset(offset: float, mode: str | None) -> ActionDiff:
    dev = _cached_first_device()
    before = {
        "lwt_offset": getattr(dev, "lwt_offset", None) if dev else None,
        "is_on": getattr(dev, "is_on", None) if dev else None,
    }
    after = {"lwt_offset": float(offset), "mode": mode or "heating"}
    flags = _passive_mode_flag()
    if dev is None:
        flags.append("no_cached_device")
    if before["is_on"] is False:
        flags.append("climate_off_lwt_offset_not_settable")
    if not (-10 <= offset <= 10):
        flags.append("lwt_offset_out_of_range")
    summary = (
        f"Set Daikin LWT offset → {offset:+g} "
        f"(currently {before['lwt_offset']})"
    )
    return ActionDiff(
        action="daikin.set_lwt_offset",
        before=before,
        after=after,
        safety_flags=flags,
        human_summary=summary,
    )


def diff_daikin_mode(mode: str) -> ActionDiff:
    dev = _cached_first_device()
    before = {"mode": getattr(dev, "operation_mode", None) if dev else None}
    after = {"mode": str(mode)}
    flags = _passive_mode_flag()
    if dev is None:
        flags.append("no_cached_device")
    summary = f"Set Daikin operation mode → {mode} (currently {before['mode']})"
    return ActionDiff(
        action="daikin.set_operation_mode",
        before=before,
        after=after,
        safety_flags=flags,
        human_summary=summary,
    )


def diff_daikin_tank_temperature(temperature: float) -> ActionDiff:
    dev = _cached_first_device()
    before = {
        "tank_target": getattr(dev, "tank_target", None) if dev else None,
        "tank_temp": getattr(dev, "tank_temp", None) if dev else None,
        "tank_on": getattr(dev, "tank_on", None) if dev else None,
    }
    after = {"tank_target": float(temperature)}
    flags = _passive_mode_flag()
    if dev is None:
        flags.append("no_cached_device")
    if before["tank_on"] is False:
        flags.append("tank_off_temperature_not_settable")
    if not (30.0 <= temperature <= 65.0):
        flags.append("tank_temp_out_of_range")
    summary = (
        f"Set DHW tank target → {temperature}°C "
        f"(currently {before['tank_target']}°C, actual {before['tank_temp']}°C)"
    )
    return ActionDiff(
        action="daikin.set_tank_temperature",
        before=before,
        after=after,
        safety_flags=flags,
        human_summary=summary,
    )


def diff_daikin_tank_power(on: bool) -> ActionDiff:
    dev = _cached_first_device()
    before = {"tank_on": getattr(dev, "tank_on", None) if dev else None}
    after = {"tank_on": bool(on)}
    flags = _passive_mode_flag()
    if dev is None:
        flags.append("no_cached_device")
    summary = (
        f"Turn DHW tank {'ON' if on else 'OFF'} "
        f"(currently {'ON' if before['tank_on'] else 'OFF' if before['tank_on'] is False else 'unknown'})"
    )
    return ActionDiff(
        action="daikin.set_tank_power",
        before=before,
        after=after,
        safety_flags=flags,
        human_summary=summary,
    )


# ---------------------------------------------------------------------------
# Fox ESS diff computers
# ---------------------------------------------------------------------------

def diff_foxess_mode(mode: str) -> ActionDiff:
    """Set Fox V3 work mode."""
    from ..foxess import service as foxess_service

    snap_dict: dict[str, Any] = {}
    try:
        snap = foxess_service.get_cached_realtime(allow_refresh=False)
        # Tolerate either dataclass or dict shape — service has changed shape over releases.
        if hasattr(snap, "soc"):
            snap_dict = {"soc": snap.soc, "work_mode": getattr(snap, "work_mode", None),
                         "load_power": getattr(snap, "load_power", None),
                         "solar_power": getattr(snap, "solar_power", None)}
        elif isinstance(snap, dict):
            snap_dict = snap
    except Exception:
        snap_dict = {"_unavailable": True}

    before = {
        "work_mode": snap_dict.get("work_mode"),
        "soc": snap_dict.get("soc"),
    }
    after = {"work_mode": str(mode)}
    flags: list[str] = []
    if before["work_mode"] is None:
        flags.append("no_cached_realtime")
    # Manual mode change overrides any active LP-managed Fox V3 schedule.
    flags.append("overrides_active_lp_dispatch_until_next_solve")
    summary = (
        f"Switch Fox work mode → {mode} "
        f"(currently {before['work_mode']}, SoC {before['soc']}%)"
    )
    return ActionDiff(
        action="foxess.set_mode",
        before=before,
        after=after,
        safety_flags=flags,
        human_summary=summary,
    )


def diff_foxess_charge_period(periods: list[Any]) -> ActionDiff:
    """Set Fox V2 charge periods (legacy charge-period schema)."""
    n = len(periods or [])
    return ActionDiff(
        action="foxess.set_charge_period",
        before={"period_count": "unknown_from_cache"},
        after={"period_count": n, "periods": periods},
        safety_flags=["overrides_active_lp_dispatch_until_next_solve"],
        human_summary=f"Replace Fox charge periods with {n} new period(s)",
    )


# ---------------------------------------------------------------------------
# Optimization diff computers
# ---------------------------------------------------------------------------

def diff_optimization_propose() -> ActionDiff:
    """Force re-solve and propose a new plan. Doesn't apply yet (next-step approve does)."""
    return ActionDiff(
        action="optimization.propose",
        before={"description": "current active plan (if any)"},
        after={"description": "fresh LP solve will be proposed for review"},
        safety_flags=[],
        human_summary="Re-run LP optimizer and propose a new plan (does not auto-apply unless PLAN_AUTO_APPROVE=true)",
    )


def diff_optimization_approve(plan_id: str | None) -> ActionDiff:
    return ActionDiff(
        action="optimization.approve",
        before={"plan_status": "pending_approval"},
        after={"plan_status": "applied", "plan_id": plan_id},
        safety_flags=["dispatches_to_fox"] + (
            [] if config.DAIKIN_CONTROL_MODE == "passive" else ["dispatches_to_daikin"]
        ),
        human_summary=f"Approve and dispatch plan {plan_id or '(current pending)'} to Fox" + (
            " (Daikin in passive mode — no Daikin actions will fire)"
            if config.DAIKIN_CONTROL_MODE == "passive"
            else " and Daikin"
        ),
    )


def diff_optimization_reject(plan_id: str | None) -> ActionDiff:
    return ActionDiff(
        action="optimization.reject",
        before={"plan_status": "pending_approval"},
        after={"plan_status": "rejected"},
        safety_flags=[],
        human_summary=f"Reject plan {plan_id or '(current pending)'}",
    )


def diff_optimization_rollback() -> ActionDiff:
    return ActionDiff(
        action="optimization.rollback",
        before={"description": "current applied plan + Fox V3 schedule"},
        after={"description": "previous snapshot restored"},
        safety_flags=["overwrites_fox_schedule", "may_lose_in_flight_dispatch"],
        human_summary="Rollback to the previous configuration snapshot (overwrites current Fox V3 schedule)",
    )


def diff_optimization_preset(preset: str) -> ActionDiff:
    current = config.OPTIMIZATION_PRESET
    return ActionDiff(
        action="optimization.set_preset",
        before={"preset": current},
        after={"preset": preset},
        safety_flags=[] if preset == current else ["takes_effect_on_next_solve"],
        human_summary=f"Change optimization preset: {current} → {preset}",
    )


def diff_optimization_backend(backend: str) -> ActionDiff:
    current = config.OPTIMIZER_BACKEND
    flags: list[str] = []
    if backend != current:
        flags.append("takes_effect_on_next_solve")
    if backend == "heuristic":
        flags.append("heuristic_is_safety_fallback_only")
    return ActionDiff(
        action="optimization.set_backend",
        before={"backend": current},
        after={"backend": backend},
        safety_flags=flags,
        human_summary=f"Change optimizer backend: {current} → {backend}",
    )


def diff_optimization_mode(mode: str) -> ActionDiff:
    """Switch OPERATION_MODE between simulation and operational."""
    current = config.OPERATION_MODE
    flags: list[str] = []
    if mode == "operational" and current != "operational":
        flags.append("enables_real_hardware_writes")
    if mode == "simulation":
        flags.append("disables_all_hardware_writes")
    return ActionDiff(
        action="optimization.set_mode",
        before={"mode": current},
        after={"mode": mode},
        safety_flags=flags,
        human_summary=f"Change operation mode: {current} → {mode}",
    )


def diff_optimization_auto_approve(enabled: bool) -> ActionDiff:
    current = config.PLAN_AUTO_APPROVE
    return ActionDiff(
        action="optimization.set_auto_approve",
        before={"auto_approve": current},
        after={"auto_approve": bool(enabled)},
        safety_flags=["plans_will_apply_without_review"] if enabled and not current else [],
        human_summary=(
            f"{'Enable' if enabled else 'Disable'} plan auto-approve "
            f"(currently {'enabled' if current else 'disabled'})"
        ),
    )


# ---------------------------------------------------------------------------
# Settings + scheduler diff computers
# ---------------------------------------------------------------------------

def diff_setting_change(key: str, value: Any) -> ActionDiff:
    """Generic diff for PUT /api/v1/settings/{key}."""
    try:
        current = get_setting(key)
    except KeyError:
        current = "<unknown_key>"
    flags: list[str] = []
    if key == "DAIKIN_CONTROL_MODE":
        if str(value).lower() == "active" and current == "passive":
            flags.append("enables_daikin_writes")
        if str(value).lower() == "passive" and current == "active":
            flags.append("disables_daikin_writes")
    return ActionDiff(
        action=f"setting.{key}",
        before={key: current},
        after={key: value},
        safety_flags=flags,
        human_summary=f"Change setting {key}: {current!r} → {value!r}",
    )


def diff_scheduler_pause() -> ActionDiff:
    return ActionDiff(
        action="scheduler.pause",
        before={"paused": False},
        after={"paused": True},
        safety_flags=["stops_automatic_replanning"],
        human_summary="Pause scheduler (stops MPC re-solves and nightly plan push)",
    )


def diff_scheduler_resume() -> ActionDiff:
    return ActionDiff(
        action="scheduler.resume",
        before={"paused": True},
        after={"paused": False},
        safety_flags=[],
        human_summary="Resume scheduler",
    )
