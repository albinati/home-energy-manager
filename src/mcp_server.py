"""MCP (Model Context Protocol) server over stdio for Home Energy Manager.

Fox ESS tools delegate to ``FoxESSClient`` and the ``foxess.service`` cache layer.
Daikin tools delegate to ``DaikinClient`` (Onecta OAuth tokens from env / token file).

Run: ``python -m src.mcp_server`` (from project root, with ``PYTHONPATH`` including the root).

Writes honour ``OPENCLAW_READ_ONLY`` (default true) and the same rate limits as the REST API.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .api import safeguards
from .config import config
from .daikin.client import DaikinClient, DaikinError
from .daikin.models import DaikinDevice
from .foxess.client import FoxESSClient, FoxESSError, WORK_MODE_VALID
from .foxess.service import get_cached_realtime, get_refresh_stats

logger = logging.getLogger(__name__)

FOXESS_MODE_ACTION = "foxess.mode"

DAIKIN_POWER_ACTION = "daikin.power"
DAIKIN_TEMPERATURE_ACTION = "daikin.temperature"
DAIKIN_LWT_OFFSET_ACTION = "daikin.lwt_offset"
DAIKIN_MODE_ACTION = "daikin.mode"
DAIKIN_TANK_TEMP_ACTION = "daikin.tank_temperature"
DAIKIN_TANK_POWER_ACTION = "daikin.tank_power"


def _foxess_client() -> FoxESSClient:
    return FoxESSClient(**config.foxess_client_kwargs())


def _daikin_client() -> DaikinClient:
    return DaikinClient()


def _write_blocked_message() -> str:
    return (
        "Writes are disabled (OPENCLAW_READ_ONLY=true). "
        "Set OPENCLAW_READ_ONLY=false in the environment to allow control changes."
    )


def _daikin_write_preamble(action_type: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Return an error result dict if the write must not proceed, else None."""
    if config.OPENCLAW_READ_ONLY:
        safeguards.audit_log(action_type, params, "mcp", False, _write_blocked_message())
        return {"ok": False, "error": _write_blocked_message()}
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        return {
            "ok": False,
            "error": f"Rate limited. Try again in {wait_time:.1f} seconds.",
        }
    return None


def _daikin_write_api_error(
    action_type: str, params: dict[str, Any], exc: BaseException
) -> dict[str, Any]:
    """Map cloud/network errors to an MCP result and audit failure."""
    if isinstance(exc, DaikinError):
        msg = str(exc)
    else:
        msg = f"Daikin unreachable: {exc}"
    safeguards.audit_log(action_type, params, "mcp", False, msg)
    return {"ok": False, "error": msg}


def _device_status_dict(client: DaikinClient, dev: DaikinDevice) -> dict[str, Any]:
    s = client.get_status(dev)
    return {
        "device_id": dev.id,
        "device_name": s.device_name,
        "model": dev.model or "",
        "is_on": s.is_on,
        "mode": s.mode,
        "room_temp": s.room_temp,
        "target_temp": s.target_temp,
        "outdoor_temp": s.outdoor_temp,
        "lwt": s.lwt,
        "lwt_offset": s.lwt_offset,
        "tank_temp": s.tank_temp,
        "tank_target": s.tank_target,
        "weather_regulation": s.weather_regulation,
    }


def build_mcp() -> FastMCP:
    """Construct the FastMCP app with Fox ESS and Daikin tools registered."""
    instructions = (
        "Home Energy Manager — Fox ESS (battery/inverter) and Daikin (heat pump) tools. "
        "Fox reads use a short-lived cache to stay within Fox ESS API daily limits. "
        "Daikin uses Onecta cloud (OAuth); when OPENCLAW_READ_ONLY is true (default), "
        "writes (inverter mode, Daikin power/temperature/LWT/mode/tank) are rejected. "
        "If weather regulation is active on a device, use set_daikin_lwt_offset instead of "
        "set_daikin_temperature."
    )
    mcp = FastMCP(
        name="home-energy-manager",
        instructions=instructions,
        log_level="WARNING",
    )

    @mcp.tool(
        name="get_soc",
        description=(
            "Return battery state of charge (%) from Fox ESS. "
            "Uses the same cached realtime path as the REST API (respects API call limits)."
        ),
    )
    def get_soc() -> dict[str, Any]:
        try:
            d = get_cached_realtime()
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except FoxESSError as e:
            logger.warning("get_soc FoxESSError: %s", e)
            return {"ok": False, "error": str(e)}
        except (TimeoutError, OSError) as e:
            logger.warning("get_soc network error: %s", e)
            return {"ok": False, "error": f"Fox ESS unreachable: {e}"}
        last_ts, refresh_count = get_refresh_stats()
        return {
            "ok": True,
            "soc": d.soc,
            "work_mode": d.work_mode,
            "last_updated_epoch": last_ts,
            "refresh_count_24h": refresh_count,
            "refresh_limit_24h": 1440,
        }

    @mcp.tool(
        name="set_inverter_mode",
        description=(
            "Set Fox ESS inverter work mode. Valid modes: Self Use, Feed-in Priority, "
            "Back Up, Force charge, Force discharge."
        ),
    )
    def set_inverter_mode(mode: str) -> dict[str, Any]:
        if config.OPENCLAW_READ_ONLY:
            safeguards.audit_log(FOXESS_MODE_ACTION, {"mode": mode}, "mcp", False, _write_blocked_message())
            return {"ok": False, "error": _write_blocked_message()}
        if mode not in WORK_MODE_VALID:
            return {
                "ok": False,
                "error": f"Invalid mode {mode!r}. Use one of: {sorted(WORK_MODE_VALID)}",
            }
        allowed, wait_time = safeguards.check_rate_limit(FOXESS_MODE_ACTION)
        if not allowed:
            return {
                "ok": False,
                "error": f"Rate limited. Try again in {wait_time:.1f} seconds.",
            }
        try:
            client = _foxess_client()
            client.set_work_mode(mode)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except FoxESSError as e:
            safeguards.audit_log(FOXESS_MODE_ACTION, {"mode": mode}, "mcp", False, str(e))
            return {"ok": False, "error": str(e)}
        safeguards.record_action_time(FOXESS_MODE_ACTION)
        safeguards.audit_log(FOXESS_MODE_ACTION, {"mode": mode}, "mcp", True, "Mode set")
        return {"ok": True, "message": f"Work mode set to: {mode}"}

    @mcp.tool(
        name="get_daikin_status",
        description=(
            "Return status for all Daikin (Onecta) devices: temperatures, LWT offset, "
            "tank, weather regulation flag, etc. Requires Daikin OAuth token file."
        ),
    )
    def get_daikin_status() -> dict[str, Any]:
        try:
            client = _daikin_client()
            devices = client.get_devices()
        except FileNotFoundError as e:
            logger.warning("get_daikin_status not configured: %s", e)
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except DaikinError as e:
            logger.warning("get_daikin_status DaikinError: %s", e)
            return {"ok": False, "error": str(e)}
        except (TimeoutError, OSError) as e:
            logger.warning("get_daikin_status network error: %s", e)
            return {"ok": False, "error": f"Daikin unreachable: {e}"}
        payload = [_device_status_dict(client, d) for d in devices]
        return {"ok": True, "devices": payload}

    @mcp.tool(
        name="set_daikin_power",
        description="Turn Daikin climate control on or off for all gateway devices.",
    )
    def set_daikin_power(on: bool) -> dict[str, Any]:
        params = {"on": on}
        blocked = _daikin_write_preamble(DAIKIN_POWER_ACTION, params)
        if blocked is not None:
            return blocked
        try:
            client = _daikin_client()
            devices = client.get_devices()
            if not devices:
                return {"ok": False, "error": "No Daikin devices found"}
            for dev in devices:
                client.set_power(dev, on)
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_POWER_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_POWER_ACTION)
        safeguards.audit_log(DAIKIN_POWER_ACTION, params, "mcp", True, "Power set")
        return {"ok": True, "message": f"Daikin climate turned {'ON' if on else 'OFF'}"}

    @mcp.tool(
        name="set_daikin_temperature",
        description=(
            "Set target room temperature (°C) for all devices. Blocked when weather "
            "regulation is active — use set_daikin_lwt_offset. Optional mode overrides "
            "operation mode (e.g. heating)."
        ),
    )
    def set_daikin_temperature(temperature: float, mode: str | None = None) -> dict[str, Any]:
        params = {"temperature": temperature, "mode": mode}
        blocked = _daikin_write_preamble(DAIKIN_TEMPERATURE_ACTION, params)
        if blocked is not None:
            return blocked
        if temperature < 15 or temperature > 30:
            return {"ok": False, "error": "Temperature must be between 15 and 30°C"}
        try:
            client = _daikin_client()
            devices = client.get_devices()
            if not devices:
                return {"ok": False, "error": "No Daikin devices found"}
            for dev in devices:
                if dev.weather_regulation_enabled:
                    msg = (
                        "Cannot set room temperature while weather regulation is active. "
                        "Use set_daikin_lwt_offset instead, or disable weather regulation first."
                    )
                    safeguards.audit_log(DAIKIN_TEMPERATURE_ACTION, params, "mcp", False, msg)
                    return {"ok": False, "error": msg}
                op_mode = mode or dev.operation_mode
                client.set_temperature(dev, temperature, op_mode)
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_TEMPERATURE_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_TEMPERATURE_ACTION)
        safeguards.audit_log(DAIKIN_TEMPERATURE_ACTION, params, "mcp", True, "Temperature set")
        return {"ok": True, "message": f"Temperature set to {temperature}°C"}

    @mcp.tool(
        name="set_daikin_lwt_offset",
        description=(
            "Set leaving-water temperature offset (-10 to +10) for all devices. "
            "Preferred when weather regulation is active."
        ),
    )
    def set_daikin_lwt_offset(offset: float, mode: str | None = None) -> dict[str, Any]:
        params = {"offset": offset, "mode": mode}
        blocked = _daikin_write_preamble(DAIKIN_LWT_OFFSET_ACTION, params)
        if blocked is not None:
            return blocked
        if offset < -10 or offset > 10:
            return {"ok": False, "error": "LWT offset must be between -10 and +10"}
        try:
            client = _daikin_client()
            devices = client.get_devices()
            if not devices:
                return {"ok": False, "error": "No Daikin devices found"}
            for dev in devices:
                op_mode = mode or dev.operation_mode
                client.set_lwt_offset(dev, offset, op_mode)
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_LWT_OFFSET_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_LWT_OFFSET_ACTION)
        safeguards.audit_log(DAIKIN_LWT_OFFSET_ACTION, params, "mcp", True, "LWT offset set")
        return {"ok": True, "message": f"LWT offset set to {offset:+g}"}

    @mcp.tool(
        name="set_daikin_mode",
        description=(
            "Set Daikin operation mode: heating, cooling, auto, fan_only, or dry."
        ),
    )
    def set_daikin_mode(mode: str) -> dict[str, Any]:
        params = {"mode": mode}
        blocked = _daikin_write_preamble(DAIKIN_MODE_ACTION, params)
        if blocked is not None:
            return blocked
        try:
            client = _daikin_client()
            devices = client.get_devices()
            if not devices:
                return {"ok": False, "error": "No Daikin devices found"}
            for dev in devices:
                client.set_operation_mode(dev, mode)
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except ValueError as e:
            safeguards.audit_log(DAIKIN_MODE_ACTION, params, "mcp", False, str(e))
            return {"ok": False, "error": str(e)}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_MODE_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_MODE_ACTION)
        safeguards.audit_log(DAIKIN_MODE_ACTION, params, "mcp", True, "Mode set")
        return {"ok": True, "message": f"Mode set to {mode}"}

    @mcp.tool(
        name="set_daikin_tank_temperature",
        description="Set DHW tank target temperature (30–65°C) where supported.",
    )
    def set_daikin_tank_temperature(temperature: float) -> dict[str, Any]:
        params = {"temperature": temperature}
        blocked = _daikin_write_preamble(DAIKIN_TANK_TEMP_ACTION, params)
        if blocked is not None:
            return blocked
        if temperature < 30 or temperature > 65:
            return {"ok": False, "error": "Tank temperature must be between 30 and 65°C"}
        try:
            client = _daikin_client()
            devices = client.get_devices()
            if not devices:
                return {"ok": False, "error": "No Daikin devices found"}
            for dev in devices:
                if dev.tank_target is not None:
                    client.set_tank_temperature(dev, temperature)
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except ValueError as e:
            safeguards.audit_log(DAIKIN_TANK_TEMP_ACTION, params, "mcp", False, str(e))
            return {"ok": False, "error": str(e)}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_TANK_TEMP_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_TANK_TEMP_ACTION)
        safeguards.audit_log(DAIKIN_TANK_TEMP_ACTION, params, "mcp", True, "Tank temp set")
        return {"ok": True, "message": f"DHW tank target set to {temperature}°C"}

    @mcp.tool(
        name="set_daikin_tank_power",
        description="Turn domestic hot water (tank) on or off for all devices.",
    )
    def set_daikin_tank_power(on: bool) -> dict[str, Any]:
        params = {"on": on}
        blocked = _daikin_write_preamble(DAIKIN_TANK_POWER_ACTION, params)
        if blocked is not None:
            return blocked
        try:
            client = _daikin_client()
            devices = client.get_devices()
            if not devices:
                return {"ok": False, "error": "No Daikin devices found"}
            for dev in devices:
                client.set_tank_power(dev, on)
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_TANK_POWER_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_TANK_POWER_ACTION)
        safeguards.audit_log(DAIKIN_TANK_POWER_ACTION, params, "mcp", True, "Tank power set")
        return {"ok": True, "message": f"DHW tank turned {'ON' if on else 'OFF'}"}

    # ── Optimization: simulation/operational mode, consent, presets, target price, snapshots ──

    @mcp.tool(
        name="get_optimization_status",
        description=(
            "Return the full optimization engine status: operation mode (simulation/operational), "
            "preset, target price, Agile cache state, last plan, and consent state (pending/approved plan). "
            "Always call this first before proposing or approving plans."
        ),
    )
    def get_optimization_status() -> dict[str, Any]:
        from .optimization.engine import get_optimization_engine
        eng = get_optimization_engine()
        return {"ok": True, "status": eng.status_dict()}

    @mcp.tool(
        name="get_optimization_plan",
        description=(
            "Return the current 48-slot optimization plan (recomputes from Agile cache if needed). "
            "Shows per-slot import price, classification (cheap/peak/standard), LWT delta, "
            "and Fox mode hints. Use this to review what the system intends to do."
        ),
    )
    def get_optimization_plan() -> dict[str, Any]:
        from .optimization.engine import get_optimization_engine
        eng = get_optimization_engine()
        if not config.OCTOPUS_TARIFF_CODE:
            return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}
        plan = eng.solve_from_cache()
        if plan is None:
            return {
                "ok": False,
                "error": "No Agile rate cache. Refresh with POST /api/v1/optimization/refresh first.",
            }
        from .optimization.consent import _make_plan_summary
        return {
            "ok": True,
            "summary": _make_plan_summary(plan),
            "computed_at": plan.computed_at.isoformat(),
            "preset": plan.preset.value,
            "target_mean_price_pence": plan.target_mean_price_pence,
            "cheap_slot_count": plan.cheap_slot_count,
            "peak_slot_count": plan.peak_slot_count,
            "tariff_code": plan.tariff_code,
            "slots": [
                {
                    "time": s.valid_from.strftime("%H:%M"),
                    "price_pence": s.import_price_pence,
                    "kind": s.slot_kind.value,
                    "lwt_delta": s.lwt_offset_delta,
                    "fox_mode": s.fox_mode_hint.value,
                    "notes": s.notes,
                }
                for s in plan.slots
            ],
        }

    @mcp.tool(
        name="propose_optimization_plan",
        description=(
            "Compute an optimization plan and propose it for user consent. "
            "Returns a summary and plan_id. Present the summary to the user and ask for approval. "
            "The plan expires after 60 minutes if not acted on. "
            "Use approve_optimization_plan(plan_id) to activate it."
        ),
    )
    def propose_optimization_plan() -> dict[str, Any]:
        from .optimization.engine import get_optimization_engine
        from .optimization.consent import propose_plan
        if not config.OCTOPUS_TARIFF_CODE:
            return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}
        eng = get_optimization_engine()
        plan = eng.solve_from_cache()
        if plan is None:
            return {
                "ok": False,
                "error": "No Agile rate cache. Refresh with POST /api/v1/optimization/refresh first.",
            }
        pending = propose_plan(plan)
        auto = config.PLAN_AUTO_APPROVE
        mode_note = (
            "System is in SIMULATION mode — no hardware writes will occur. Shadow-run only."
            if config.OPERATION_MODE != "operational"
            else "System is in OPERATIONAL mode — plan controls Fox ESS and Daikin."
        )
        instruction = (
            "Plan was AUTO-APPROVED and is now active. "
            f"Call reject_optimization_plan('{pending.plan_id}') to cancel it."
            if auto
            else (
                f"Show the user the summary above. If they approve, call "
                f"approve_optimization_plan with plan_id='{pending.plan_id}'."
            )
        )
        return {
            "ok": True,
            "plan_id": pending.plan_id,
            "expires_at": pending.expires_at.isoformat(),
            "status": pending.status.value,
            "auto_approved": auto,
            "summary": pending.summary,
            "operation_mode": config.OPERATION_MODE,
            "mode_note": mode_note,
            "instruction": instruction,
        }

    @mcp.tool(
        name="approve_optimization_plan",
        description=(
            "Approve a pending optimization plan by its plan_id. "
            "Only call this after presenting the plan summary to the user and receiving their explicit approval. "
            "In simulation mode, the plan runs in shadow mode (logs only). "
            "In operational mode, the plan controls Fox ESS and Daikin hardware."
        ),
    )
    def approve_optimization_plan(plan_id: str) -> dict[str, Any]:
        from .optimization.consent import approve_plan
        from .optimization.models import PlanConsentStatus
        result = approve_plan(plan_id)
        if result is None:
            return {"ok": False, "error": f"Plan '{plan_id}' not found or already expired."}
        if result.status == PlanConsentStatus.EXPIRED:
            return {"ok": False, "error": "Plan has expired. Propose a new one."}
        mode_note = (
            "Running in SIMULATION mode — shadow logging only, no hardware writes."
            if config.OPERATION_MODE != "operational"
            else "OPERATIONAL mode active — plan is now controlling Fox ESS and Daikin."
        )
        return {
            "ok": True,
            "plan_id": plan_id,
            "status": result.status.value,
            "approved_at": result.approved_at.isoformat() if result.approved_at else None,
            "message": f"Plan approved. {mode_note}",
        }

    @mcp.tool(
        name="reject_optimization_plan",
        description="Reject a pending or approved optimization plan. The system returns to baseline.",
    )
    def reject_optimization_plan(plan_id: str) -> dict[str, Any]:
        from .optimization.consent import reject_plan
        result = reject_plan(plan_id)
        if result is None:
            return {"ok": False, "error": f"Plan '{plan_id}' not found."}
        return {
            "ok": True,
            "plan_id": plan_id,
            "status": result.status.value,
            "message": "Plan rejected. System will use baseline settings.",
        }

    @mcp.tool(
        name="set_optimization_preset",
        description=(
            "Switch the household preset. "
            "Options: normal (standard comfort), guests (higher DHW, warmer, less cost-cutting), "
            "travel/away (frost protection only, max battery export), "
            "boost (temporary full-comfort override, ignores price). "
            "After switching, call propose_optimization_plan to re-solve with the new preset."
        ),
    )
    def set_optimization_preset(preset: str) -> dict[str, Any]:
        valid = {"normal", "guests", "travel", "away", "boost"}
        if preset not in valid:
            return {"ok": False, "error": f"Invalid preset '{preset}'. Valid: {sorted(valid)}"}
        config.OPTIMIZATION_PRESET = preset
        return {
            "ok": True,
            "preset": preset,
            "message": (
                f"Preset set to '{preset}'. "
                "Call propose_optimization_plan to generate a new plan with this preset."
            ),
        }

    @mcp.tool(
        name="set_target_price",
        description=(
            "Set the target average import price (p/kWh). "
            "The solver will exploit cheap windows aggressively enough to achieve this average. "
            "Lower target = more load shifting; higher target = more comfort. Use 0 to disable."
        ),
    )
    def set_target_price(target_price_pence: float) -> dict[str, Any]:
        if target_price_pence < 0 or target_price_pence > 100:
            return {"ok": False, "error": "Target price must be between 0 and 100 p/kWh"}
        config.TARGET_PRICE_PENCE = target_price_pence
        msg = (
            f"Target price set to {target_price_pence}p/kWh. "
            "Call propose_optimization_plan to regenerate the plan."
            if target_price_pence > 0
            else "Target price disabled. Solver uses fixed cheap threshold."
        )
        return {"ok": True, "target_price_pence": target_price_pence, "message": msg}

    @mcp.tool(
        name="set_operation_mode",
        description=(
            "Switch between simulation (safe, shadow-run only) and operational (writes to hardware). "
            "IMPORTANT: Always present the implications to the user and get explicit confirmation before "
            "switching to operational. A config snapshot is saved automatically before any transition. "
            "Switching to operational requires an approved plan to be present."
        ),
    )
    def set_operation_mode(mode: str) -> dict[str, Any]:
        if mode not in ("simulation", "operational"):
            return {"ok": False, "error": "Mode must be 'simulation' or 'operational'"}
        from .optimization.snapshots import save_snapshot
        from .optimization.consent import get_approved_plan, clear_approved_plan

        current_mode = config.OPERATION_MODE
        if current_mode == mode:
            return {"ok": True, "mode": mode, "message": f"Already in {mode} mode."}

        snap = save_snapshot(trigger=f"mode_change: {current_mode} -> {mode}")
        snapshot_id = snap.get("snapshot_id")

        if mode == "operational":
            approved = get_approved_plan()
            if approved is None:
                return {
                    "ok": False,
                    "mode": current_mode,
                    "snapshot_id": snapshot_id,
                    "message": (
                        "Cannot switch to operational: no approved plan. "
                        "Call propose_optimization_plan, review the summary, "
                        "and call approve_optimization_plan first."
                    ),
                }

        config.OPERATION_MODE = mode
        if mode == "simulation":
            clear_approved_plan()
            msg = (
                f"Switched to simulation mode (snapshot {snapshot_id} saved). "
                "Approved plan cleared. No hardware writes will occur."
            )
        else:
            msg = (
                f"Switched to OPERATIONAL mode (snapshot {snapshot_id} saved). "
                "Fox ESS and Daikin will now be controlled by the approved plan on each 30-min tick."
            )
        return {"ok": True, "mode": mode, "snapshot_id": snapshot_id, "message": msg}

    @mcp.tool(
        name="rollback_config",
        description=(
            "Restore the latest config snapshot and force simulation mode. "
            "Use this in emergencies or when something unexpected happens. "
            "After rollback, review the system state before re-approving any plan."
        ),
    )
    def rollback_config(snapshot_id: str | None = None) -> dict[str, Any]:
        from .optimization.snapshots import rollback_latest, restore_snapshot
        try:
            if snapshot_id:
                snap = restore_snapshot(snapshot_id)
            else:
                snap = rollback_latest()
                if snap is None:
                    return {"ok": False, "error": "No snapshots found to roll back to."}
            sid = snap.get("snapshot_id", "unknown")
            return {
                "ok": True,
                "snapshot_id": sid,
                "restored_mode": snap.get("operation_mode"),
                "message": (
                    f"Config restored from snapshot {sid}. "
                    "System is now in simulation mode. "
                    "Review and re-approve a plan before going operational again."
                ),
            }
        except FileNotFoundError:
            return {"ok": False, "error": f"Snapshot '{snapshot_id}' not found."}
        except Exception as exc:
            return {"ok": False, "error": f"Rollback failed: {exc}"}

    @mcp.tool(
        name="get_config_snapshots",
        description="List all available config snapshots (newest first) with their trigger and mode.",
    )
    def get_config_snapshots() -> dict[str, Any]:
        from .optimization.snapshots import list_snapshots
        snaps = list_snapshots()
        return {"ok": True, "snapshots": snaps, "count": len(snaps)}

    @mcp.tool(
        name="set_auto_approve",
        description=(
            "Enable or disable automatic plan approval. "
            "When enabled, every new plan is immediately approved without waiting for user consent — "
            "useful for hands-off operation once the user trusts the system. "
            "A notification is always sent even in auto-approve mode. "
            "The user can still call reject_optimization_plan at any time to cancel an active plan. "
            "IMPORTANT: Only suggest enabling this if the user explicitly asks for it, "
            "or has already expressed trust in fully automated operation."
        ),
    )
    def set_auto_approve(enabled: bool) -> dict[str, Any]:
        from .optimization.consent import set_auto_approve as _set
        _set(enabled)
        if enabled:
            msg = (
                "Auto-approve ENABLED. New plans will be approved automatically. "
                "Notifications will still be sent. "
                "Use reject_optimization_plan to cancel the active plan at any time."
            )
        else:
            msg = (
                "Auto-approve DISABLED. Plans now require explicit approval "
                "before they take effect."
            )
        return {"ok": True, "auto_approve": enabled, "message": msg}

    # ── Tariff comparison tools ────────────────────────────────────────────

    @mcp.tool(
        name="list_available_tariffs",
        description=(
            "List currently available Octopus Energy electricity tariffs with rates, "
            "standing charges, and contract terms. Returns product codes, pricing "
            "structures (flat, time-of-use, half-hourly/Agile, tracker), and policy "
            "details (lock-in months, exit fees, green credentials). "
            "Use this to show the user what's on the market before running a comparison."
        ),
    )
    def list_available_tariffs(max_tariffs: int = 15) -> dict[str, Any]:
        from .energy.octopus_products import get_available_tariffs
        tariffs = get_available_tariffs(max_products=max_tariffs)
        return {
            "ok": True,
            "count": len(tariffs),
            "tariffs": [
                {
                    "product_code": t.product_code,
                    "display_name": t.display_name,
                    "pricing": t.pricing.value,
                    "unit_rate_pence": t.rates.unit_rate_pence,
                    "standing_charge_pence_per_day": t.rates.standing_charge_pence_per_day,
                    "contract_type": t.policy.contract_type.value,
                    "contract_months": t.policy.contract_months,
                    "exit_fee_pence": t.policy.exit_fee_pence,
                    "is_green": t.policy.is_green,
                    "summary": t.summary_line(),
                }
                for t in tariffs
            ],
        }

    @mcp.tool(
        name="compare_tariffs",
        description=(
            "Compare available Octopus tariffs against the household's actual energy usage "
            "and produce a ranked recommendation. Uses Fox ESS import/export kWh for the "
            "specified period (1–12 months back). Accounts for standing charges, unit rates, "
            "export payments, lock-in periods, and exit fees. "
            "Returns a ranked list with annual cost projections and a summary with the best "
            "tariff, potential savings vs the current tariff, and policy warnings."
        ),
    )
    def compare_tariffs_tool(
        months_back: int = 1,
        max_tariffs: int = 15,
    ) -> dict[str, Any]:
        from .energy.tariff_engine import get_tariff_recommendation
        rec = get_tariff_recommendation(
            months_back=months_back,
            max_tariffs=max_tariffs,
        )
        results = []
        for r in rec.candidates[:10]:
            results.append({
                "rank": len(results) + 1,
                "product_code": r.tariff.product_code,
                "display_name": r.tariff.display_name,
                "pricing": r.tariff.pricing.value,
                "annual_net_cost_pounds": r.annual_net_cost_pounds,
                "annual_standing_charge_pounds": r.annual_standing_charge_pounds,
                "standing_charge_per_day": r.tariff.rates.standing_charge_pence_per_day,
                "unit_rate_pence": r.tariff.rates.unit_rate_pence,
                "exit_fee_pounds": r.exit_fee_pounds,
                "lock_in_months": r.lock_in_months,
                "first_year_effective_cost_pounds": r.first_year_effective_cost_pounds,
                "contract_type": r.tariff.policy.contract_type.value,
                "is_green": r.tariff.policy.is_green,
            })
        usage_info = {}
        if rec.candidates:
            c = rec.candidates[0]
            usage_info = {
                "import_kwh": c.import_kwh,
                "export_kwh": c.export_kwh,
                "period_days": c.period_days,
            }
        return {
            "ok": True,
            "summary": rec.summary,
            "best_product_code": rec.best.tariff.product_code if rec.best else None,
            "savings_vs_current_pounds": rec.savings_vs_current_pounds,
            "results": results,
            "usage": usage_info,
            "generated_at": rec.generated_at.isoformat() if rec.generated_at else None,
        }

    @mcp.tool(
        name="get_tariff_recommendation",
        description=(
            "Get a concise tariff recommendation: the best available tariff, projected annual "
            "savings, and any policy caveats. This is the high-level tool for quick answers like "
            "'What's the best tariff for me right now?'. Uses 1 month of usage data by default. "
            "For detailed breakdowns, use compare_tariffs instead."
        ),
    )
    def get_tariff_recommendation_tool(months_back: int = 1) -> dict[str, Any]:
        from .energy.tariff_engine import get_tariff_recommendation
        rec = get_tariff_recommendation(months_back=months_back)
        result: dict[str, Any] = {
            "ok": True,
            "summary": rec.summary,
        }
        if rec.best:
            result["best"] = {
                "product_code": rec.best.tariff.product_code,
                "display_name": rec.best.tariff.display_name,
                "annual_net_cost_pounds": rec.best.annual_net_cost_pounds,
                "contract_type": rec.best.tariff.policy.contract_type.value,
                "lock_in_months": rec.best.lock_in_months,
                "exit_fee_pounds": rec.best.exit_fee_pounds,
                "is_green": rec.best.tariff.policy.is_green,
            }
        if rec.savings_vs_current_pounds is not None:
            result["savings_vs_current_pounds"] = rec.savings_vs_current_pounds
        if rec.current_tariff:
            result["current"] = {
                "product_code": rec.current_tariff.tariff.product_code,
                "display_name": rec.current_tariff.tariff.display_name,
                "annual_net_cost_pounds": rec.current_tariff.annual_net_cost_pounds,
            }
        return result

    @mcp.tool(
        name="compare_tariffs_dashboard",
        description=(
            "Get granular tariff comparison data for daily, weekly, or monthly views. "
            "Returns per-period cost breakdowns showing which tariff wins each day/week/month, "
            "total rankings with savings vs current tariff, and win counts. "
            "The current tariff (Octopus Flexible by default) is flagged as baseline. "
            "Use this for detailed analysis like 'Show me which tariff was cheapest each day this month'."
        ),
    )
    def compare_tariffs_dashboard_tool(
        months_back: int = 1,
        granularity: str = "daily",
        max_tariffs: int = 10,
    ) -> dict[str, Any]:
        from .energy.tariff_engine import get_tariff_comparison_dashboard
        data = get_tariff_comparison_dashboard(
            months_back=months_back,
            granularity=granularity,
            max_tariffs=max_tariffs,
        )
        if not data.get("ok"):
            return {"ok": False, "error": data.get("error", "Unknown error")}
        # Summarise for MCP (full periods would be too verbose)
        totals = data.get("totals", [])
        periods_count = len(data.get("periods", []))
        summary_lines = []
        for i, t in enumerate(totals[:5]):
            marker = " (CURRENT)" if t.get("is_current") else ""
            sav = t.get("savings_vs_current_pounds")
            sav_str = f" — saves £{sav:.0f}/yr vs current" if sav and sav > 0 else ""
            summary_lines.append(
                f"  {i+1}. {t['display_name']}: £{t['annual_pounds']:.0f}/yr, "
                f"wins {t['wins']}/{periods_count} periods{marker}{sav_str}"
            )
        return {
            "ok": True,
            "granularity": data.get("granularity"),
            "periods_count": periods_count,
            "ranking": "\n".join(summary_lines),
            "totals": totals[:10],
            "current_product_code": data.get("current_product_code"),
            "current_annual_pounds": data.get("current_annual_pounds"),
            "usage": data.get("usage"),
            "data_source": data.get("data_source"),
        }

    # ── Octopus account + consumption tools ───────────────────────────────────

    @mcp.tool(
        name="get_octopus_account",
        description=(
            "Return Octopus account summary: current tariff product and code, "
            "import/export MPAN roles, GSP (grid supply point), and detection source. "
            "Calls the authenticated Octopus account API. "
            "Use this to confirm the current tariff and MPAN roles are correctly detected."
        ),
    )
    def get_octopus_account() -> dict[str, Any]:
        if not config.OCTOPUS_API_KEY:
            return {
                "ok": False,
                "error": "OCTOPUS_API_KEY not configured in .env",
                "account_number": config.OCTOPUS_ACCOUNT_NUMBER,
            }
        from .energy.octopus_client import get_account_summary
        summary = get_account_summary()
        return {"ok": summary.get("error") is None, **summary}

    @mcp.tool(
        name="get_octopus_consumption",
        description=(
            "Fetch electricity consumption from Octopus smart meter for a period. "
            "group_by: 'day', 'week', 'month', or None (half-hourly). "
            "Defaults to the import MPAN. Returns slots with interval times and kWh. "
            "Useful for understanding real usage patterns and verifying meter data."
        ),
    )
    def get_octopus_consumption(
        mpan: str | None = None,
        serial: str | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        group_by: str | None = "day",
    ) -> dict[str, Any]:
        if not config.OCTOPUS_API_KEY:
            return {"ok": False, "error": "OCTOPUS_API_KEY not configured in .env"}

        from .energy.octopus_client import fetch_consumption, get_mpan_roles
        from datetime import datetime, timezone, timedelta

        roles = get_mpan_roles()
        use_mpan = mpan or roles.import_mpan or config.OCTOPUS_MPAN_1
        use_serial = serial or roles.import_serial or config.OCTOPUS_METER_SN_1

        if not use_mpan or not use_serial:
            return {
                "ok": False,
                "error": "MPAN and serial required. Configure OCTOPUS_MPAN_1/OCTOPUS_METER_SN_1 in .env.",
            }

        pf = pt = None
        if period_from:
            try:
                pf = datetime.fromisoformat(period_from.replace("Z", "+00:00"))
            except ValueError:
                return {"ok": False, "error": "Invalid period_from format"}
        if period_to:
            try:
                pt = datetime.fromisoformat(period_to.replace("Z", "+00:00"))
            except ValueError:
                return {"ok": False, "error": "Invalid period_to format"}

        if group_by and group_by not in ("day", "week", "month"):
            return {"ok": False, "error": "group_by must be day, week, month, or None"}

        try:
            slots = fetch_consumption(use_mpan, use_serial, pf, pt, group_by=group_by)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        total_kwh = round(sum(s.consumption_kwh for s in slots), 3)
        return {
            "ok": True,
            "mpan": use_mpan,
            "serial": use_serial,
            "group_by": group_by,
            "slot_count": len(slots),
            "total_kwh": total_kwh,
            "slots": [
                {
                    "interval_start": s.interval_start.isoformat(),
                    "interval_end": s.interval_end.isoformat(),
                    "consumption_kwh": s.consumption_kwh,
                }
                for s in slots[:50]  # cap for MCP verbosity
            ],
            "note": f"Showing first 50 of {len(slots)} slots" if len(slots) > 50 else None,
        }

    @mcp.tool(
        name="auto_detect_octopus_setup",
        description=(
            "Detect MPAN roles (which is import, which is export) and the current active tariff "
            "from the Octopus account API. Updates runtime config with detected values. "
            "Call this once after first setup, or if you suspect the MPAN roles are wrong. "
            "Returns import/export MPANs, GSP, and current tariff product code."
        ),
    )
    def auto_detect_octopus_setup() -> dict[str, Any]:
        if not config.OCTOPUS_API_KEY:
            return {"ok": False, "error": "OCTOPUS_API_KEY not configured in .env"}
        if not config.OCTOPUS_ACCOUNT_NUMBER:
            return {"ok": False, "error": "OCTOPUS_ACCOUNT_NUMBER not configured in .env"}

        from .energy.octopus_client import auto_detect_mpan_roles, discover_current_tariff
        errors = []
        roles = None
        tariff = None

        try:
            roles = auto_detect_mpan_roles()
            config.OCTOPUS_MPAN_IMPORT = roles.import_mpan
            config.OCTOPUS_MPAN_EXPORT = roles.export_mpan
            config.OCTOPUS_METER_SERIAL_IMPORT = roles.import_serial
            config.OCTOPUS_METER_SERIAL_EXPORT = roles.export_serial
            config.OCTOPUS_GSP = roles.gsp
        except Exception as exc:
            errors.append(f"MPAN detection: {exc}")

        try:
            tariff = discover_current_tariff()
            if tariff and tariff.product_code:
                config.CURRENT_TARIFF_PRODUCT = tariff.product_code
        except Exception as exc:
            errors.append(f"Tariff detection: {exc}")

        return {
            "ok": not errors,
            "error": "; ".join(errors) if errors else None,
            "import_mpan": roles.import_mpan if roles else None,
            "export_mpan": roles.export_mpan if roles else None,
            "gsp": roles.gsp if roles else config.OCTOPUS_GSP,
            "current_tariff_product": tariff.product_code if tariff else None,
            "current_tariff_code": tariff.tariff_code if tariff else None,
            "detection_source": roles.source if roles else "failed",
            "message": (
                "Runtime config updated. Changes are in effect for this session only. "
                "Restart the server or update .env to persist."
            ) if not errors else "Detection partially failed — check errors above.",
        }

    return mcp


def main() -> None:
    """Entry point: MCP over stdio (do not write logs to stdout)."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    build_mcp().run(transport="stdio")


if __name__ == "__main__":
    main()
