"""MCP (Model Context Protocol) server over stdio for Home Energy Manager.

Fox ESS tools delegate to ``FoxESSClient`` and the ``foxess.service`` cache layer.
Daikin tools delegate to ``DaikinClient`` (Onecta OAuth tokens from env / token file).

Run: ``./bin/mcp`` from project root (picks Python 3.11 in Docker, ``.venv`` on the host), or
``python -m src.mcp_server`` with ``PYTHONPATH`` including the project root.

Writes honour ``OPENCLAW_READ_ONLY`` (default true) and the same rate limits as the REST API.

v10 (S5a): the singleton flock is acquired BEFORE heavy imports when run as the
main module. This prevents the zombie accumulation observed in production, where
20+ processes spawned by openclaw all completed config/FastMCP/client init before
any single one reached the lock check inside ``main()``. With early acquisition,
losers exit within ~10ms (stdlib imports only), never holding heavy resources.
"""
from __future__ import annotations

# --- Stdlib-only zone (must stay cheap; runs in every spawned process) -------
import atexit
import fcntl
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def _lock_path() -> Path:
    """Lock file path — /run if writable (systemd tmpfs on prod), else /tmp."""
    for base in ("/run", "/tmp"):
        d = Path(base)
        if d.is_dir() and os.access(d, os.W_OK):
            return d / "hem-mcp.lock"
    return Path("/tmp/hem-mcp.lock")


def _acquire_singleton_lock_early() -> int | None:
    """Acquire the flock before heavy imports. Returns fd or None to exit silently.

    Uses sys.stderr (no logging dep) and runs only if ``__name__ == "__main__"``
    so plain imports (tests, audits) bypass it.

    Behaviour on contention (v10.1 hotfix):
      - If another live process holds the lock → exit silently with code 0.
        Do NOT SIGTERM it — openclaw maintains a persistent stdio connection
        to a single MCP child and would lose its pipe (observed in prod as
        ``MCP error -32000: Connection closed`` followed by repeated
        ``Not connected`` failures).
      - POSIX flock auto-releases on process death, so true crashes recover
        on the next spawn without needing SIGTERM.

    Manual-recovery escape hatch: set ``HEM_MCP_FORCE_KILL_PRIOR=1`` in the
    environment to restore the SIGTERM-and-retry behaviour. Use only when you
    have manually verified the prior holder is unresponsive.
    """
    path = _lock_path()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        prior_pid: int | None = None
        try:
            raw = os.read(fd, 32).decode().strip()
            prior_pid = int(raw) if raw else None
        except (OSError, ValueError):
            prior_pid = None

        force_kill = os.environ.get("HEM_MCP_FORCE_KILL_PRIOR", "").lower() in ("1", "true", "yes")
        if force_kill and prior_pid and prior_pid != os.getpid():
            print(
                f"WARNING mcp_server.bootstrap: HEM_MCP_FORCE_KILL_PRIOR set — "
                f"sending SIGTERM to pid={prior_pid} and retrying",
                file=sys.stderr,
            )
            try:
                os.kill(prior_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.1)
            else:
                print(
                    f"ERROR mcp_server.bootstrap: lock still held by pid={prior_pid} "
                    f"after SIGTERM — exiting",
                    file=sys.stderr,
                )
                os.close(fd)
                return None
        else:
            # Default v10.1 path: yield to the existing instance. Do NOT kill it.
            # Openclaw's stdio pipe to the prior MCP must remain intact.
            print(
                f"INFO mcp_server.bootstrap: another instance is live (pid={prior_pid}); "
                f"exiting cleanly so the existing one keeps serving openclaw",
                file=sys.stderr,
            )
            os.close(fd)
            return None
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd


# Critical: acquire the lock BEFORE the heavy imports below. Losers exit here
# without ever touching FastMCP/config/DaikinClient init. _EARLY_LOCK_FD is None
# when this module is imported (not run), so tests/audits skip the lock entirely.
_EARLY_LOCK_FD: int | None = None
if __name__ == "__main__":
    _EARLY_LOCK_FD = _acquire_singleton_lock_early()
    if _EARLY_LOCK_FD is None:
        sys.exit(0)

# --- Heavy imports (only reached by the singleton winner or by `import`-ers) -
import concurrent.futures
from datetime import UTC

from mcp.server.fastmcp import FastMCP

from .api import safeguards
from .config import config
from .daikin.client import DaikinClient, DaikinError
from .daikin.models import DaikinDevice
from .foxess.client import WORK_MODE_VALID, FoxESSClient, FoxESSError
from .foxess.service import get_cached_realtime, get_refresh_stats
from .scheduler.lp_simulation import run_lp_simulation

# Single-worker executor for non-blocking optimizer calls from the MCP transport.
# max_workers=1 ensures only one plan runs at a time (no concurrent LP solves).
_optimizer_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-optimizer")

# Phase 4.5: hardware-write tool name prefixes. The boot-time surface audit warns
# when any tool matching these prefixes lacks a ``confirmed`` parameter — that's
# the only enforceable gate between OpenClaw and live hardware.
_HARDWARE_WRITE_TOOL_PREFIXES = ("set_daikin_", "set_inverter_")


def _augment_actions_with_local_time(
    actions: list[dict[str, Any]], tz_name: str = "Europe/London"
) -> list[dict[str, Any]]:
    """Add ``start_time_local`` / ``end_time_local`` siblings to each action row.

    The DB stores timestamps in canonical UTC (Z-suffix). External consumers
    (OpenClaw, other agents, dashboards) that read raw UTC strings tend to
    mis-read the local hour during BST months — #47 was filed because an agent
    saw a ``"start_time": "2026-04-21T15:00:00Z"`` row and concluded the peak
    window was firing an hour early, when in fact 15:00 UTC is 16:00 BST and
    the scheduler was correctly targeting the 16:00–19:00 local peak.

    Adding a human-readable local rendering alongside the canonical UTC makes
    the ambiguity disappear without changing the underlying storage contract.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    out: list[dict[str, Any]] = []
    for a in actions:
        copy = dict(a)
        for key in ("start_time", "end_time"):
            raw = copy.get(key)
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(tz)
            except (TypeError, ValueError):
                continue
            # e.g. "2026-04-21T16:00:00 BST" — same digits a UK dashboard shows,
            # plus the abbreviation so no agent can confuse it with UTC.
            copy[f"{key}_local"] = dt.strftime(f"%Y-%m-%dT%H:%M:%S {dt.tzname()}")
        out.append(copy)
    return out


def audit_mcp_tool_surface(mcp_app) -> list[str]:
    """Emit WARN for any hardware-write tool that lacks a ``confirmed`` parameter.

    Returns the list of warning strings so tests can assert on regressions.

    The audit reaches into FastMCP private internals; if a future release renames or
    restructures them, we would get an empty tools dict and the boundary check would
    silently become a no-op — exactly the worst failure mode. Detect empty-registry
    as a distinct *error* so it stays loud.
    """
    warnings: list[str] = []
    tools = getattr(getattr(mcp_app, "_tool_manager", None), "_tools", {}) or {}
    if not tools:
        msg = (
            "[OpenClaw boundary] audit_mcp_tool_surface observed ZERO registered tools — "
            "likely a FastMCP private-API break. The boundary check is a no-op until fixed. "
            "See docs/OPENCLAW_BOUNDARY.md."
        )
        logger.error(msg)
        warnings.append(msg)
        return warnings
    for name, tool in tools.items():
        if not any(name.startswith(p) for p in _HARDWARE_WRITE_TOOL_PREFIXES):
            continue
        params = (tool.parameters or {}).get("properties", {}) or {}
        if "confirmed" not in params:
            msg = (
                f"[OpenClaw boundary] hardware-write tool '{name}' lacks 'confirmed' "
                "parameter — OpenClaw can invoke it without explicit user approval. "
                "See docs/OPENCLAW_BOUNDARY.md."
            )
            logger.warning(msg)
            warnings.append(msg)
    return warnings


# Phase 4.4: whitelist of safe override keys accepted by simulate_plan.
# Additions must be deliberate — overrides shadow config values during the solve.
_SIMULATE_PLAN_OVERRIDE_WHITELIST = frozenset({
    "occupancy_mode",
    "residents",
    "extra_visitors",
    "dhw_temp_normal_c",
    "target_dhw_min_guests_c",
    "optimization_preset",
})

# Maps override key → config attribute on ``src.config.config``. Keys in the
# whitelist but not in this map are validated and returned under
# ``ignored_overrides`` (the occupancy layer that consumes them is on other
# branches; applying them here would be a silent no-op that misleads callers).
_SIMULATE_PLAN_CONFIG_MAP = {
    "dhw_temp_normal_c": "DHW_TEMP_NORMAL_C",
    "target_dhw_min_guests_c": "TARGET_DHW_TEMP_MIN_GUESTS_C",
    "optimization_preset": "OPTIMIZATION_PRESET",
}

# Phase 4 review — per-key value validators for simulate_plan.
_VALID_OPTIMIZATION_PRESETS = frozenset({"normal", "guests", "travel", "away"})
_VALID_OCCUPANCY_MODES = frozenset({"normal", "guests", "travel", "away"})


def _validate_simulate_override(key: str, value: Any) -> tuple[Any, str | None]:
    """Return (coerced_value, error). error is None if valid. Attacker-supplied
    override VALUES are validated before any config mutation happens."""
    if key in ("dhw_temp_normal_c", "target_dhw_min_guests_c"):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None, f"{key} must be a number, got {type(value).__name__}"
        if not 30.0 <= v <= 70.0:
            return None, f"{key} out of range (30..70 °C): {v}"
        return v, None
    if key == "optimization_preset":
        if not isinstance(value, str) or value not in _VALID_OPTIMIZATION_PRESETS:
            return None, f"{key} must be one of {sorted(_VALID_OPTIMIZATION_PRESETS)}"
        return value, None
    if key == "occupancy_mode":
        if not isinstance(value, str) or value not in _VALID_OCCUPANCY_MODES:
            return None, f"{key} must be one of {sorted(_VALID_OCCUPANCY_MODES)}"
        return value, None
    if key in ("residents", "extra_visitors"):
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None, f"{key} must be an integer, got {type(value).__name__}"
        if not 0 <= v <= 20:
            return None, f"{key} out of range (0..20): {v}"
        return v, None
    return None, f"{key}: no validator"


def _simulate_plan_empty_response(ok: bool, error: str | None, received: dict) -> dict[str, Any]:
    """Base response shape — same top-level keys on success and error paths.

    Phase 4 review: consumers (OpenClaw) couldn't previously rely on any key being
    present without branching on ``ok``. Now every field exists on every path;
    ``applied_overrides``/``ignored_overrides`` tell the caller what took effect.
    """
    return {
        "ok": ok,
        "error": error,
        "plan_date": "",
        "plan_window": "",
        "slot_count": 0,
        "objective_pence": 0.0,
        "status": "",
        "actual_mean_agile_pence": 0.0,
        "forecast_solar_kwh_horizon": 0.0,
        "pv_scale_factor": 0.0,
        "mu_load_kwh_per_slot": 0.0,
        "initial_state": {"soc_kwh": None, "tank_temp_c": None, "indoor_temp_c": None},
        "received_overrides": dict(received),
        "applied_overrides": {},
        "ignored_overrides": {},
    }


def _run_simulate_plan_body(overrides: dict[str, Any]) -> dict[str, Any]:
    """Config-mutating core of simulate_plan. Runs inside _optimizer_executor
    so it serializes against propose_optimization_plan's background thread
    (shared max_workers=1 queue — no config-mutation race)."""
    validated: dict[str, Any] = {}
    for k, v in overrides.items():
        coerced, err = _validate_simulate_override(k, v)
        if err is not None:
            return _simulate_plan_empty_response(False, f"invalid override: {err}", overrides)
        validated[k] = coerced

    applied: dict[str, Any] = {}
    ignored: dict[str, Any] = {}
    saved: dict[str, Any] = {}
    try:
        for k, v in validated.items():
            attr = _SIMULATE_PLAN_CONFIG_MAP.get(k)
            if attr is None:
                ignored[k] = v
                continue
            if hasattr(config, attr):
                saved[attr] = getattr(config, attr)
                setattr(config, attr, v)
                applied[k] = v

        # Phase 4 review C10: explicitly forbid cache refresh so a cold MCP-process
        # cache cannot burn Daikin quota during a "no quota" simulation.
        result = run_lp_simulation(allow_daikin_refresh=False)
    finally:
        for attr, val in saved.items():
            setattr(config, attr, val)

    if not result.ok:
        resp = _simulate_plan_empty_response(False, result.error or "simulation failed", overrides)
        resp["plan_date"] = getattr(result, "plan_date", "") or ""
        resp["plan_window"] = getattr(result, "plan_window", "") or ""
        resp["status"] = getattr(result, "status", "") or ""
        resp["applied_overrides"] = applied
        resp["ignored_overrides"] = ignored
        return resp

    initial = getattr(result, "initial", None)
    resp = _simulate_plan_empty_response(True, None, overrides)
    resp["plan_date"] = result.plan_date
    resp["plan_window"] = result.plan_window
    resp["slot_count"] = result.slot_count
    resp["objective_pence"] = result.objective_pence
    resp["status"] = result.status
    resp["actual_mean_agile_pence"] = result.actual_mean_agile_pence
    resp["forecast_solar_kwh_horizon"] = result.forecast_solar_kwh_horizon
    resp["pv_scale_factor"] = result.pv_scale_factor
    resp["mu_load_kwh_per_slot"] = result.mu_load_kwh
    resp["initial_state"] = {
        "soc_kwh": getattr(initial, "soc_kwh", None),
        "tank_temp_c": getattr(initial, "tank_temp_c", None),
        "indoor_temp_c": getattr(initial, "indoor_temp_c", None),
    }
    resp["applied_overrides"] = applied
    resp["ignored_overrides"] = ignored
    return resp

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
    if config.DAIKIN_CONTROL_MODE == "passive":
        msg = "DAIKIN_CONTROL_MODE=passive — set to 'active' to allow writes"
        safeguards.audit_log(action_type, params, "mcp", False, msg)
        return {"ok": False, "error": msg, "passive_mode": True}
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


def _check_plan_consent_conflict(plan_date: str) -> str | None:
    """Return a warning string if a plan is pending approval for *plan_date*, else None."""
    from . import db
    try:
        consent = db.get_plan_consent(plan_date)
    except Exception:
        return None
    if consent and consent.get("status") == "pending_approval":
        return (
            f"WARNING: plan {consent['plan_id']} is pending your approval. "
            "Manual Daikin changes may be overwritten when the plan is approved. "
            "Pass confirmed=True to proceed anyway, or use confirm_plan/reject_plan first."
        )
    return None


def _plan_date_today(tz_name: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


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
        description=(
            "Turn Daikin climate control on or off for all gateway devices. "
            "When a plan is pending approval, pass confirmed=True to override it."
        ),
    )
    def set_daikin_power(on: bool, confirmed: bool = False) -> dict[str, Any]:
        params = {"on": on}
        blocked = _daikin_write_preamble(DAIKIN_POWER_ACTION, params)
        if blocked is not None:
            return blocked
        plan_date = _plan_date_today(config.BULLETPROOF_TIMEZONE)
        conflict_warn = _check_plan_consent_conflict(plan_date)
        if conflict_warn and not confirmed:
            return {"ok": False, "requires_confirmation": True, "warning": conflict_warn}
        try:
            from .daikin import service as _daikin_svc
            _daikin_svc.set_power(on, actor="mcp")
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_POWER_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_POWER_ACTION)
        safeguards.audit_log(DAIKIN_POWER_ACTION, params, "mcp", True, "Power set")
        result: dict[str, Any] = {"ok": True, "message": f"Daikin climate turned {'ON' if on else 'OFF'}"}
        if conflict_warn:
            result["warning"] = conflict_warn
        return result

    @mcp.tool(
        name="set_daikin_temperature",
        description=(
            "Set target room temperature (°C) for all devices. Blocked when weather "
            "regulation is active — use set_daikin_lwt_offset. Optional mode overrides "
            "operation mode (e.g. heating). Pass confirmed=True to override a pending plan."
        ),
    )
    def set_daikin_temperature(temperature: float, mode: str | None = None, confirmed: bool = False) -> dict[str, Any]:
        params = {"temperature": temperature, "mode": mode}
        blocked = _daikin_write_preamble(DAIKIN_TEMPERATURE_ACTION, params)
        if blocked is not None:
            return blocked
        if temperature < 15 or temperature > 30:
            return {"ok": False, "error": "Temperature must be between 15 and 30°C"}
        plan_date = _plan_date_today(config.BULLETPROOF_TIMEZONE)
        conflict_warn = _check_plan_consent_conflict(plan_date)
        if conflict_warn and not confirmed:
            return {"ok": False, "requires_confirmation": True, "warning": conflict_warn}
        try:
            from .daikin import service as _daikin_svc
            cached = _daikin_svc.get_cached_devices(allow_refresh=False, actor="mcp")
            for dev in (cached.devices or []):
                if dev.weather_regulation_enabled:
                    msg = (
                        "Cannot set room temperature while weather regulation is active. "
                        "Use set_daikin_lwt_offset instead, or disable weather regulation first."
                    )
                    safeguards.audit_log(DAIKIN_TEMPERATURE_ACTION, params, "mcp", False, msg)
                    return {"ok": False, "error": msg}
            _daikin_svc.set_temperature(temperature, mode or "heating", actor="mcp")
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_TEMPERATURE_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_TEMPERATURE_ACTION)
        safeguards.audit_log(DAIKIN_TEMPERATURE_ACTION, params, "mcp", True, "Temperature set")
        result: dict[str, Any] = {"ok": True, "message": f"Temperature set to {temperature}°C"}
        if conflict_warn:
            result["warning"] = conflict_warn
        return result

    @mcp.tool(
        name="set_daikin_lwt_offset",
        description=(
            "Set leaving-water temperature offset (-10 to +10) for all devices. "
            "Preferred when weather regulation is active. Pass confirmed=True to override a pending plan."
        ),
    )
    def set_daikin_lwt_offset(offset: float, mode: str | None = None, confirmed: bool = False) -> dict[str, Any]:
        params = {"offset": offset, "mode": mode}
        blocked = _daikin_write_preamble(DAIKIN_LWT_OFFSET_ACTION, params)
        if blocked is not None:
            return blocked
        if offset < -10 or offset > 10:
            return {"ok": False, "error": "LWT offset must be between -10 and +10"}
        plan_date = _plan_date_today(config.BULLETPROOF_TIMEZONE)
        conflict_warn = _check_plan_consent_conflict(plan_date)
        if conflict_warn and not confirmed:
            return {"ok": False, "requires_confirmation": True, "warning": conflict_warn}
        try:
            from .daikin import service as _daikin_svc
            _daikin_svc.set_lwt_offset(offset, mode or "heating", actor="mcp")
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_LWT_OFFSET_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_LWT_OFFSET_ACTION)
        safeguards.audit_log(DAIKIN_LWT_OFFSET_ACTION, params, "mcp", True, "LWT offset set")
        result: dict[str, Any] = {"ok": True, "message": f"LWT offset set to {offset:+g}"}
        if conflict_warn:
            result["warning"] = conflict_warn
        return result

    @mcp.tool(
        name="set_daikin_mode",
        description=(
            "Set Daikin operation mode: heating, cooling, auto, fan_only, or dry. "
            "Pass confirmed=True to override a pending plan."
        ),
    )
    def set_daikin_mode(mode: str, confirmed: bool = False) -> dict[str, Any]:
        params = {"mode": mode}
        blocked = _daikin_write_preamble(DAIKIN_MODE_ACTION, params)
        if blocked is not None:
            return blocked
        plan_date = _plan_date_today(config.BULLETPROOF_TIMEZONE)
        conflict_warn = _check_plan_consent_conflict(plan_date)
        if conflict_warn and not confirmed:
            return {"ok": False, "requires_confirmation": True, "warning": conflict_warn}
        try:
            from .daikin import service as _daikin_svc
            _daikin_svc.set_operation_mode(mode, actor="mcp")
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except ValueError as e:
            safeguards.audit_log(DAIKIN_MODE_ACTION, params, "mcp", False, str(e))
            return {"ok": False, "error": str(e)}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_MODE_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_MODE_ACTION)
        safeguards.audit_log(DAIKIN_MODE_ACTION, params, "mcp", True, "Mode set")
        result: dict[str, Any] = {"ok": True, "message": f"Mode set to {mode}"}
        if conflict_warn:
            result["warning"] = conflict_warn
        return result

    @mcp.tool(
        name="set_daikin_tank_temperature",
        description="Set DHW tank target temperature (30–65°C) where supported. Pass confirmed=True to override a pending plan.",
    )
    def set_daikin_tank_temperature(temperature: float, confirmed: bool = False) -> dict[str, Any]:
        params = {"temperature": temperature}
        blocked = _daikin_write_preamble(DAIKIN_TANK_TEMP_ACTION, params)
        if blocked is not None:
            return blocked
        if temperature < 30 or temperature > 65:
            return {"ok": False, "error": "Tank temperature must be between 30 and 65°C"}
        plan_date = _plan_date_today(config.BULLETPROOF_TIMEZONE)
        conflict_warn = _check_plan_consent_conflict(plan_date)
        if conflict_warn and not confirmed:
            return {"ok": False, "requires_confirmation": True, "warning": conflict_warn}
        try:
            from .daikin import service as _daikin_svc
            _daikin_svc.set_tank_temperature(temperature, actor="mcp")
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except ValueError as e:
            safeguards.audit_log(DAIKIN_TANK_TEMP_ACTION, params, "mcp", False, str(e))
            return {"ok": False, "error": str(e)}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_TANK_TEMP_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_TANK_TEMP_ACTION)
        safeguards.audit_log(DAIKIN_TANK_TEMP_ACTION, params, "mcp", True, "Tank temp set")
        result: dict[str, Any] = {"ok": True, "message": f"DHW tank target set to {temperature}°C"}
        if conflict_warn:
            result["warning"] = conflict_warn
        return result

    @mcp.tool(
        name="set_daikin_tank_power",
        description="Turn domestic hot water (tank) on or off for all devices. Pass confirmed=True to override a pending plan.",
    )
    def set_daikin_tank_power(on: bool, confirmed: bool = False) -> dict[str, Any]:
        params = {"on": on}
        blocked = _daikin_write_preamble(DAIKIN_TANK_POWER_ACTION, params)
        if blocked is not None:
            return blocked
        plan_date = _plan_date_today(config.BULLETPROOF_TIMEZONE)
        conflict_warn = _check_plan_consent_conflict(plan_date)
        if conflict_warn and not confirmed:
            return {"ok": False, "requires_confirmation": True, "warning": conflict_warn}
        try:
            from .daikin import service as _daikin_svc
            _daikin_svc.set_tank_power(on, actor="mcp")
        except FileNotFoundError as e:
            return {"ok": False, "error": f"Daikin not configured: {e}"}
        except (DaikinError, TimeoutError, OSError) as e:
            return _daikin_write_api_error(DAIKIN_TANK_POWER_ACTION, params, e)
        safeguards.record_action_time(DAIKIN_TANK_POWER_ACTION)
        safeguards.audit_log(DAIKIN_TANK_POWER_ACTION, params, "mcp", True, "Tank power set")
        result: dict[str, Any] = {"ok": True, "message": f"DHW tank turned {'ON' if on else 'OFF'}"}
        if conflict_warn:
            result["warning"] = conflict_warn
        return result

    # ── Bulletproof planner: presets, mode, snapshots (V7 consent stack removed) ──

    @mcp.tool(
        name="get_optimization_status",
        description=(
            "Bulletproof brain status: scheduler, Octopus fetch health, operation mode, preset. "
            "Call before running propose_optimization_plan."
        ),
    )
    def get_optimization_status() -> dict[str, Any]:
        from dataclasses import asdict

        from . import db
        from .agile_cache import get_agile_cache
        from .scheduler.runner import get_scheduler_status

        cache = get_agile_cache()
        return {
            "ok": True,
            "bulletproof": True,
            "preset": config.OPTIMIZATION_PRESET,
            "optimizer_backend": config.OPTIMIZER_BACKEND,
            "scheduler": get_scheduler_status(),
            "octopus_fetch": asdict(db.get_octopus_fetch_state()),
            "agile_cache_slots": len(cache.rates or []),
            "agile_cache_error": cache.error,
        }

    @mcp.tool(
        name="get_optimization_plan",
        description=(
            "Today's SQLite action_schedule rows and last Fox Scheduler V3 snapshot "
            "(replaces the retired 48-slot V7 solver table)."
        ),
    )
    def get_optimization_plan() -> dict[str, Any]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from . import db

        if not config.OCTOPUS_TARIFF_CODE:
            return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}
        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        plan_date = datetime.now(tz).date().isoformat()
        return {
            "ok": True,
            "bulletproof": True,
            "plan_date": plan_date,
            "timezone": config.BULLETPROOF_TIMEZONE,
            "daikin_actions": _augment_actions_with_local_time(
                db.schedule_for_date(plan_date), config.BULLETPROOF_TIMEZONE
            ),
            "fox_schedule_state": db.get_latest_fox_schedule_state(),
        }

    @mcp.tool(
        name="simulate_plan",
        description=(
            "Phase 4.4 — run the LP optimizer READ-ONLY (no DB, no Fox, no Daikin writes, "
            "no quota burn) with optional whitelisted config overrides. Use this to preview "
            "'what would the plan look like if residents=4 tomorrow?' without touching hardware. "
            "Whitelist: occupancy_mode, residents, extra_visitors, dhw_temp_normal_c, "
            "target_dhw_min_guests_c, optimization_preset. Any other key is rejected."
        ),
    )
    def simulate_plan(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = overrides or {}
        bad = [k for k in overrides if k not in _SIMULATE_PLAN_OVERRIDE_WHITELIST]
        if bad:
            return _simulate_plan_empty_response(
                False,
                f"unsupported override key(s): {', '.join(sorted(bad))}. "
                f"Whitelist: {sorted(_SIMULATE_PLAN_OVERRIDE_WHITELIST)}",
                overrides,
            )

        # Phase 4 review C1: serialize config mutation through the optimizer
        # executor (max_workers=1, shared with propose_optimization_plan) so a
        # simulate_plan cannot race a concurrent live optimizer run.
        try:
            future = _optimizer_executor.submit(_run_simulate_plan_body, overrides)
            return future.result(timeout=120)
        except concurrent.futures.TimeoutError:
            return _simulate_plan_empty_response(
                False, "simulate_plan timed out after 120s", overrides
            )
        except Exception as e:
            return _simulate_plan_empty_response(
                False, f"simulate_plan failed: {e}", overrides
            )

    @mcp.tool(
        name="propose_optimization_plan",
        description=(
            "Run the Bulletproof daily planner (SQLite + optional Fox V3 upload). "
            "Returns immediately with status='planning'. You will receive a PLAN_PROPOSED "
            "notification via OpenClaw when the plan is ready — then call confirm_plan to "
            "activate Daikin, or reject_plan to discard. "
            "Use get_pending_approval to poll for the result. "
            "Set PLAN_AUTO_APPROVE=true to skip the consent step."
        ),
    )
    def propose_optimization_plan() -> dict[str, Any]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from .scheduler.optimizer import run_optimizer

        if not config.OCTOPUS_TARIFF_CODE:
            return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}

        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        plan_date = datetime.now(tz).date().isoformat()
        plan_id = f"lp-{plan_date}"

        # Check cooldown — prevent rapid re-plan spam
        from . import db
        existing = db.get_plan_consent(plan_date)
        cooldown_s = int(getattr(config, "PLAN_REGEN_COOLDOWN_SECONDS", 300))
        if existing and existing.get("status") in ("approved", "pending_approval"):
            import time
            age_s = time.time() - float(existing.get("proposed_at", 0))
            if age_s < cooldown_s:
                remaining = int(cooldown_s - age_s)
                return {
                    "ok": True,
                    "status": existing["status"],
                    "plan_id": plan_id,
                    "plan_date": plan_date,
                    "cooldown_active": True,
                    "retry_in_seconds": remaining,
                    "message": (
                        f"Plan {plan_id} already exists ({existing['status']}). "
                        f"Re-planning is throttled for {remaining}s. "
                        "Use get_pending_approval to see the current plan, "
                        "or call reject_plan first to force a re-plan now."
                    ),
                }

        # Submit optimizer to background thread — returns immediately
        fox = None
        try:
            fox = FoxESSClient(**config.foxess_client_kwargs())
        except Exception:
            pass

        def _run_bg() -> None:
            try:
                run_optimizer(fox, None)
            except Exception as exc:
                logger.warning("Background optimizer error: %s", exc)

        try:
            _optimizer_executor.submit(_run_bg)
        except RuntimeError as exc:
            return {"ok": False, "error": f"Optimizer executor unavailable: {exc}"}

        mode_note = (
            "Read-only: Fox upload and hardware writes are skipped (OPENCLAW_READ_ONLY=true)."
            if config.OPENCLAW_READ_ONLY
            else "Live: SQLite schedule updated; Fox V3 uploaded when API key present."
        )
        return {
            "ok": True,
            "bulletproof": True,
            "status": "planning",
            "plan_id": plan_id,
            "plan_date": plan_date,
            "mode_note": mode_note,
            "message": (
                f"Optimizer is running in the background for {plan_date}. "
                "You will receive a PLAN_PROPOSED notification when ready. "
                "Use get_pending_approval to poll for status."
            ),
        }

    @mcp.tool(
        name="approve_optimization_plan",
        description=(
            "Approve a pending plan by plan_id to activate Daikin hardware execution. "
            "Alias for confirm_plan — use confirm_plan for new code."
        ),
    )
    def approve_optimization_plan(plan_id: str) -> dict[str, Any]:
        from . import db
        ok = db.approve_plan(plan_id)
        if not ok:
            row = db.get_plan_consent(plan_id.replace("lp-", ""))
            if row and row.get("status") != "pending_approval":
                return {
                    "ok": False,
                    "plan_id": plan_id,
                    "status": row.get("status"),
                    "message": f"Plan is already {row.get('status')} — cannot approve again.",
                }
            return {
                "ok": False,
                "plan_id": plan_id,
                "message": "Plan not found or not in pending_approval state.",
            }
        return {
            "ok": True,
            "plan_id": plan_id,
            "status": "approved",
            "message": "Plan approved. Daikin execution will proceed on next heartbeat.",
        }

    @mcp.tool(
        name="reject_optimization_plan",
        description=(
            "Reject a pending plan — clears its action_schedule rows. "
            "Alias for reject_plan — use reject_plan for new code."
        ),
    )
    def reject_optimization_plan(plan_id: str, reason: str | None = None) -> dict[str, Any]:
        from . import db
        ok = db.reject_plan(plan_id)
        if not ok:
            return {
                "ok": False,
                "plan_id": plan_id,
                "message": "Plan not found or not in pending_approval state.",
            }
        return {
            "ok": True,
            "plan_id": plan_id,
            "status": "rejected",
            "reason": reason,
            "message": "Plan rejected and schedule cleared. Call propose_optimization_plan to rebuild.",
        }

    @mcp.tool(
        name="confirm_plan",
        description=(
            "Confirm (approve) an energy plan that is waiting for consent. "
            "Pass the plan_id from the PLAN_PROPOSED notification (e.g. 'lp-2026-04-19'). "
            "Once confirmed, Daikin hardware execution resumes on the next heartbeat tick."
        ),
    )
    def confirm_plan(plan_id: str) -> dict[str, Any]:

        from . import db
        from .notifier import notify_action_confirmation

        ok = db.approve_plan(plan_id)
        if not ok:
            plan_date = plan_id.replace("lp-", "")
            row = db.get_plan_consent(plan_date)
            if row and row["status"] != "pending_approval":
                return {
                    "ok": False,
                    "plan_id": plan_id,
                    "status": row["status"],
                    "message": f"Cannot confirm: plan is already '{row['status']}'.",
                }
            return {
                "ok": False,
                "plan_id": plan_id,
                "message": "Plan not found or not awaiting approval. Use get_pending_approval to check.",
            }
        plan_date = plan_id.replace("lp-", "")
        actions = db.schedule_for_date(plan_date)
        daikin_actions = [a for a in actions if a.get("device") == "daikin"]
        try:
            notify_action_confirmation(f"Plan {plan_id} confirmed — Daikin execution active.")
        except Exception:
            pass
        return {
            "ok": True,
            "plan_id": plan_id,
            "status": "approved",
            "daikin_pending_actions": len(daikin_actions),
            "message": (
                f"Plan {plan_id} approved. Daikin will execute {len(daikin_actions)} action(s) "
                "on schedule from the next heartbeat. Fox ESS schedule was already uploaded."
            ),
        }

    @mcp.tool(
        name="reject_plan",
        description=(
            "Reject an energy plan that is waiting for consent — clears the Daikin action schedule. "
            "Pass the plan_id (e.g. 'lp-2026-04-19') and an optional reason string. "
            "After rejecting, call propose_optimization_plan to build a new plan."
        ),
    )
    def reject_plan(plan_id: str, reason: str | None = None) -> dict[str, Any]:
        from . import db
        from .notifier import notify_action_confirmation

        ok = db.reject_plan(plan_id)
        if not ok:
            plan_date = plan_id.replace("lp-", "")
            row = db.get_plan_consent(plan_date)
            if row and row["status"] != "pending_approval":
                return {
                    "ok": False,
                    "plan_id": plan_id,
                    "status": row["status"],
                    "message": f"Cannot reject: plan is already '{row['status']}'.",
                }
            return {
                "ok": False,
                "plan_id": plan_id,
                "message": "Plan not found or not awaiting approval.",
            }
        try:
            notify_action_confirmation(
                f"Plan {plan_id} rejected{f' — {reason}' if reason else ''}. Schedule cleared."
            )
        except Exception:
            pass
        return {
            "ok": True,
            "plan_id": plan_id,
            "status": "rejected",
            "reason": reason,
            "message": (
                "Plan rejected and pending Daikin actions cleared. "
                "Call propose_optimization_plan to rebuild."
            ),
        }

    @mcp.tool(
        name="get_pending_approval",
        description=(
            "Return the latest plan awaiting user approval (plan_consent status = pending_approval). "
            "Shows plan_id, expiry time, strategy summary, and the Daikin action schedule. "
            "Use this to check what's waiting for confirmation before calling confirm_plan or reject_plan."
        ),
    )
    def get_pending_approval() -> dict[str, Any]:
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from . import db

        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        plan_date = datetime.now(tz).date().isoformat()
        consent = db.get_plan_consent(plan_date)
        if not consent:
            return {
                "ok": True,
                "pending": False,
                "message": "No plan awaiting approval today.",
            }
        if consent["status"] != "pending_approval":
            return {
                "ok": True,
                "pending": False,
                "plan_id": consent["plan_id"],
                "status": consent["status"],
                "message": f"Plan {consent['plan_id']} is already {consent['status']}.",
            }
        remaining_s = max(0.0, float(consent["expires_at"]) - time.time())
        remaining_min = int(remaining_s / 60)
        actions = db.schedule_for_date(plan_date)
        daikin_actions = [a for a in actions if a.get("device") == "daikin"]
        return {
            "ok": True,
            "pending": True,
            "plan_id": consent["plan_id"],
            "plan_date": plan_date,
            "status": "pending_approval",
            "expires_in_minutes": remaining_min,
            "summary": consent.get("summary", ""),
            "daikin_actions": daikin_actions,
            "message": (
                f"Plan {consent['plan_id']} is waiting for your approval. "
                f"Auto-approves in ~{remaining_min} min. "
                "Use confirm_plan or reject_plan to respond."
            ),
        }

    @mcp.tool(
        name="set_optimization_preset",
        description=(
            "Switch the household preset. "
            "Options: normal (standard comfort), guests (higher DHW, warmer, less cost-cutting), "
            "travel/away (frost protection; peak grid export only if ENERGY_STRATEGY_MODE=savings_first "
            "and battery SoC >= EXPORT_DISCHARGE_MIN_SOC_PERCENT at plan time), "
            "boost (temporary full-comfort override, ignores price). "
            "After switching, call propose_optimization_plan to re-solve with the new preset."
        ),
    )
    def set_optimization_preset(preset: str) -> dict[str, Any]:
        valid = {"normal", "guests", "travel", "away"}
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
        name="set_optimizer_backend",
        description=(
            "Set the daily planner backend: 'lp' (PuLP MILP, default) or 'heuristic' (legacy price-quantile). "
            "Then call propose_optimization_plan."
        ),
    )
    def set_optimizer_backend(backend: str) -> dict[str, Any]:
        b = (backend or "").strip().lower()
        if b not in ("lp", "heuristic"):
            return {"ok": False, "error": "backend must be 'lp' or 'heuristic'"}
        config.OPTIMIZER_BACKEND = b
        return {
            "ok": True,
            "optimizer_backend": b,
            "message": f"Backend set to '{b}'. Call propose_optimization_plan to regenerate.",
        }

    @mcp.tool(
        name="rollback_config",
        description=(
            "Restore the latest config snapshot. "
            "Use this in emergencies or when something unexpected happens. "
            "After rollback, review the system state before re-approving any plan."
        ),
    )
    def rollback_config(snapshot_id: str | None = None) -> dict[str, Any]:
        from .config_snapshots import restore_snapshot, rollback_latest
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
                "message": (
                    f"Config restored from snapshot {sid}. "
                    "Review state and re-approve a plan before resuming."
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
        from .config_snapshots import list_snapshots
        snaps = list_snapshots()
        return {"ok": True, "snapshots": snaps, "count": len(snaps)}

    @mcp.tool(
        name="set_auto_approve",
        description=(
            "Toggle PLAN_AUTO_APPROVE: when true, plans are auto-approved immediately "
            "on propose (no consent gate, Daikin fires on next heartbeat). "
            "When false (default), plans wait for confirm_plan before Daikin executes."
        ),
    )
    def set_auto_approve(enabled: bool) -> dict[str, Any]:
        config.PLAN_AUTO_APPROVE = enabled
        logger.info("PLAN_AUTO_APPROVE set to %s", enabled)
        if enabled:
            msg = "PLAN_AUTO_APPROVE enabled — new plans will be auto-approved immediately on propose."
        else:
            msg = "PLAN_AUTO_APPROVE disabled — new plans will wait for confirm_plan before Daikin executes."
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

        from datetime import datetime

        from .energy.octopus_client import fetch_consumption, get_mpan_roles

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

    @mcp.tool(
        name="get_energy_metrics",
        description=(
            "Bulletproof: daily/weekly PnL vs SVT/fixed shadow, VWAP, arbitrage efficiency, "
            "peak ratio, SLA snapshot, battery SoC."
        ),
    )
    def get_energy_metrics() -> dict[str, Any]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from . import db
        from .analytics import pnl, sla
        from .foxess.service import get_cached_realtime

        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        today = datetime.now(tz).date()
        daily = pnl.compute_daily_pnl(today)
        weekly = pnl.compute_weekly_pnl(today)
        monthly = pnl.compute_monthly_pnl(today)
        tgt = db.get_daily_target(today)
        soc = None
        try:
            soc = get_cached_realtime().soc
        except Exception:
            pass
        return {
            "ok": True,
            "pnl": {
                "daily": {
                    "delta_vs_svt_pounds": daily.get("delta_vs_svt_gbp"),
                    "delta_vs_fixed_pounds": daily.get("delta_vs_fixed_gbp"),
                },
                "weekly": {"delta_vs_svt_pounds": weekly.get("delta_vs_svt_gbp")},
                "monthly": {"delta_vs_svt_pounds": monthly.get("delta_vs_svt_gbp")},
            },
            "target_vwap_pence": (tgt or {}).get("target_vwap") if tgt else None,
            "realised_vwap_pence": pnl.compute_vwap(today),
            "slippage_pence": pnl.compute_slippage(today),
            "arbitrage_efficiency_pct": pnl.compute_arbitrage_efficiency(today),
            "peak_import_pct": pnl.compute_peak_ratio(today),
            "battery_soc_percent": soc,
            "sla": sla.compute_sla_metrics(),
        }

    @mcp.tool(
        name="get_schedule",
        description="Bulletproof: today's Daikin action_schedule rows and last Fox V3 state from SQLite.",
    )
    def get_schedule() -> dict[str, Any]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from . import db

        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        plan_date = datetime.now(tz).date().isoformat()
        return {
            "ok": True,
            "plan_date": plan_date,
            "timezone": config.BULLETPROOF_TIMEZONE,
            "actions": _augment_actions_with_local_time(
                db.schedule_for_date(plan_date), config.BULLETPROOF_TIMEZONE
            ),
            "fox": db.get_latest_fox_schedule_state(),
        }

    @mcp.tool(
        name="get_daily_brief",
        description="Bulletproof: on-demand morning-style brief (yesterday PnL + today strategy).",
    )
    def get_daily_brief() -> dict[str, Any]:
        from .analytics.daily_brief import build_daily_brief_text

        return {"ok": True, "markdown": build_daily_brief_text()}

    @mcp.tool(
        name="get_battery_forecast",
        description="Bulletproof: current SoC and daily_targets snapshot.",
    )
    def get_battery_forecast() -> dict[str, Any]:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from . import db
        from .foxess.service import get_cached_realtime

        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        today = datetime.now(tz).date()
        tgt = db.get_daily_target(today)
        soc = None
        try:
            soc = get_cached_realtime().soc
        except Exception:
            pass
        return {
            "ok": True,
            "soc_percent": soc,
            "usable_capacity_kwh": config.BATTERY_CAPACITY_KWH,
            "daily_target": tgt,
        }

    @mcp.tool(
        name="get_weather_context",
        description="Bulletproof: Open-Meteo forecast plus live Daikin temps when available.",
    )
    def get_weather_context() -> dict[str, Any]:
        from .weather import fetch_forecast

        fc = [{"time": f.time_utc.isoformat(), "temp_c": f.temperature_c} for f in fetch_forecast(hours=48)]
        daikin = None
        try:
            c = _daikin_client()
            devs = c.get_devices()
            if devs:
                daikin = _device_status_dict(c, devs[0])
        except Exception as e:
            daikin = {"error": str(e)}
        return {"ok": True, "forecast_hourly": fc[:48], "daikin": daikin}

    @mcp.tool(
        name="get_action_log",
        description="Bulletproof: recent device commands from SQLite.",
    )
    def get_action_log(device: str | None = None, trigger: str | None = None, limit: int = 100) -> dict[str, Any]:
        from . import db

        return {"ok": True, "entries": db.get_action_logs(device=device, trigger=trigger, limit=limit)}

    @mcp.tool(
        name="get_optimizer_log",
        description="Bulletproof: recent optimizer runs.",
    )
    def get_optimizer_log(limit: int = 20) -> dict[str, Any]:
        from . import db

        return {"ok": True, "entries": db.get_optimizer_logs(limit=limit)}

    @mcp.tool(
        name="override_schedule",
        description=(
            "Bulletproof: temporary Daikin boost window. Requires OPENCLAW_READ_ONLY=false."
        ),
    )
    def override_schedule(
        hours: float = 2.0,
        lwt_offset: float = 3.0,
        tank_temp: float | None = None,
    ) -> dict[str, Any]:
        blocked = _daikin_write_preamble("bulletproof.override", {})
        if blocked:
            return blocked
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from . import db

        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        now = datetime.now(UTC)
        end = now + timedelta(hours=hours)
        plan_date = datetime.now(tz).date().isoformat()
        params: dict[str, Any] = {"lwt_offset": lwt_offset, "tank_powerful": True, "climate_on": True}
        if tank_temp is not None:
            params["tank_temp"] = tank_temp
        restore_params = {
            "lwt_offset": 0,
            "tank_powerful": False,
            "tank_temp": config.DHW_TEMP_NORMAL_C,
            "tank_power": True,
            "climate_on": True,
        }
        rid = db.upsert_action(
            plan_date=plan_date,
            start_time=end.isoformat().replace("+00:00", "Z"),
            end_time=(end + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            device="daikin",
            action_type="restore",
            params=restore_params,
            status="pending",
        )
        aid = db.upsert_action(
            plan_date=plan_date,
            start_time=now.isoformat().replace("+00:00", "Z"),
            end_time=end.isoformat().replace("+00:00", "Z"),
            device="daikin",
            action_type="pre_heat",
            params=params,
            status="pending",
            restore_action_id=rid,
        )
        db.update_action_restore_link(aid, rid)
        safeguards.audit_log("bulletproof.override", params, "mcp", True, "override inserted")
        return {"ok": True, "action_id": aid, "restore_id": rid}

    @mcp.tool(
        name="acknowledge_warning",
        description="Bulletproof: acknowledge a warning_key to reduce repeat alerts.",
    )
    def acknowledge_warning(warning_key: str) -> dict[str, Any]:
        from . import db

        db.acknowledge_warning(warning_key)
        return {"ok": True, "warning_key": warning_key}

    # ── Notification routing tools ──────────────────────────────────────────

    @mcp.tool(
        name="list_notification_routes",
        description=(
            "List all alert notification routes with their current settings "
            "(enabled, severity, target, channel, silent flag) and the resolved "
            "final destination for each alert type. "
            "Use this to see what notifications will be sent and where."
        ),
    )
    def list_notification_routes() -> dict[str, Any]:
        from . import db
        from .notifier import AlertType, _resolve_route

        rows = db.list_notification_routes()
        result = []
        for row in rows:
            resolved = _resolve_route(row["alert_type"])
            result.append({
                **row,
                "enabled": bool(row.get("enabled", 1)),
                "silent": bool(row.get("silent", 0)),
                "resolved_channel": resolved.get("channel") if resolved else None,
                "resolved_target": resolved.get("target") if resolved else None,
                "will_send": resolved is not None,
            })
        # Include any AlertType not yet in DB (uses env defaults)
        existing_types = {r["alert_type"] for r in rows}
        for at in AlertType:
            if at.value not in existing_types:
                resolved = _resolve_route(at.value)
                result.append({
                    "alert_type": at.value,
                    "enabled": True,
                    "severity": "critical" if at.value in ("risk_alert", "critical_error", "peak_window_start", "cheap_window_start") else "reports",
                    "target_override": None,
                    "channel_override": None,
                    "silent": at.value in ("strategy_update", "action_confirmation"),
                    "updated_at": None,
                    "resolved_channel": resolved.get("channel") if resolved else None,
                    "resolved_target": resolved.get("target") if resolved else None,
                    "will_send": resolved is not None,
                    "note": "using env defaults (no DB row yet)",
                })
        return {"ok": True, "routes": result, "count": len(result)}

    @mcp.tool(
        name="set_notification_route",
        description=(
            "Update notification routing for a specific alert type at runtime "
            "(no service restart required). "
            "alert_type: one of risk_alert, critical_error, peak_window_start, "
            "cheap_window_start, morning_report, daily_pnl, strategy_update, action_confirmation, plan_proposed. "
            "enabled: true/false to mute or unmute. "
            "severity: 'critical' or 'reports' (determines which env target is used as fallback). "
            "target: override destination (e.g. a Telegram chat ID). "
            "channel: override channel (e.g. 'telegram', 'discord'). "
            "silent: true = prefer silent delivery (passed to the hook agent for channels that support it). "
            "Omit a parameter to leave it unchanged."
        ),
    )
    def set_notification_route(
        alert_type: str,
        enabled: bool | None = None,
        severity: str | None = None,
        target: str | None = None,
        channel: str | None = None,
        silent: bool | None = None,
        clear_target_override: bool = False,
        clear_channel_override: bool = False,
    ) -> dict[str, Any]:
        from . import db
        from .notifier import AlertType, _resolve_route

        valid_types = {at.value for at in AlertType}
        if alert_type not in valid_types:
            return {
                "ok": False,
                "error": f"Invalid alert_type '{alert_type}'. Valid: {sorted(valid_types)}",
            }
        if severity is not None and severity not in ("critical", "reports"):
            return {"ok": False, "error": "severity must be 'critical' or 'reports'"}

        db.upsert_notification_route(
            alert_type,
            enabled=enabled,
            severity=severity,
            target_override=target,
            channel_override=channel,
            silent=silent,
            clear_target_override=clear_target_override,
            clear_channel_override=clear_channel_override,
        )
        resolved = _resolve_route(alert_type)
        return {
            "ok": True,
            "alert_type": alert_type,
            "will_send": resolved is not None,
            "resolved_channel": resolved.get("channel") if resolved else None,
            "resolved_target": resolved.get("target") if resolved else None,
            "message": f"Route updated for '{alert_type}'. Changes take effect immediately.",
        }

    @mcp.tool(
        name="test_notification",
        description=(
            "Fire a test notification for a specific alert type to verify "
            "OpenClaw Gateway hook delivery (POST /hooks/agent). "
            "Requires OPENCLAW_HOOKS_URL and OPENCLAW_HOOKS_TOKEN. "
            "alert_type: the AlertType to test (e.g. 'risk_alert'). "
            "message: optional custom text (defaults to a test string). "
            "Returns the resolved route; delivery is queued asynchronously."
        ),
    )
    def test_notification(
        alert_type: str = "risk_alert",
        message: str | None = None,
    ) -> dict[str, Any]:
        from .notifier import AlertType, _dispatch, _hooks_credentials_configured, _resolve_route

        valid_types = {at.value for at in AlertType}
        if alert_type not in valid_types:
            return {
                "ok": False,
                "error": f"Invalid alert_type '{alert_type}'. Valid: {sorted(valid_types)}",
            }

        resolved = _resolve_route(alert_type)
        if not resolved:
            return {
                "ok": False,
                "alert_type": alert_type,
                "will_send": False,
                "message": (
                    "Route is disabled or no target configured. "
                    "Set OPENCLAW_NOTIFY_TARGET in .env or use set_notification_route."
                ),
            }

        if not _hooks_credentials_configured():
            return {
                "ok": False,
                "alert_type": alert_type,
                "will_send": False,
                "message": (
                    "Set OPENCLAW_HOOKS_URL and OPENCLAW_HOOKS_TOKEN to match the Gateway hooks.token."
                ),
            }

        test_msg = message or f"[TEST] energy-manager notification check — alert_type={alert_type}"
        urgent = alert_type in ("risk_alert", "critical_error")
        _dispatch(AlertType(alert_type), test_msg, urgent=urgent)
        return {
            "ok": True,
            "alert_type": alert_type,
            "will_send": True,
            "queued": True,
            "channel": resolved["channel"],
            "target": resolved["target"],
            "silent": resolved["silent"],
            "message": (
                "Hook delivery queued. Check logs for [openclaw hooks] if delivery fails."
            ),
        }

    # -----------------------------------------------------------------------
    # Runtime-tunable settings (#52)
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="list_settings",
        description=(
            "List every runtime-tunable setting with current value, env default, "
            "range, and `overridden` flag. Pairs with set_setting for live tuning."
        ),
    )
    def list_settings() -> dict[str, Any]:
        from . import runtime_settings as rts
        try:
            return {"ok": True, "settings": rts.list_settings()}
        except Exception as e:
            logger.warning("list_settings failed: %s", e)
            return {"ok": False, "error": str(e)}

    @mcp.tool(
        name="get_setting",
        description="Return the current value of a single runtime-tunable setting.",
    )
    def get_setting(key: str) -> dict[str, Any]:
        from . import runtime_settings as rts
        if key not in rts.SCHEMA:
            return {"ok": False, "error": f"unknown setting {key!r}"}
        try:
            return {"ok": True, "key": key, "value": rts.get_setting(key)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @mcp.tool(
        name="set_setting",
        description=(
            "Update a runtime-tunable setting. Takes effect within the 30 s cache "
            "TTL; schedule-class keys (LP_PLAN_PUSH_HOUR/MINUTE, LP_MPC_HOURS) "
            "also trigger an APScheduler cron re-register. Pass confirmed=True "
            "to actually apply — the default is a dry-run that returns the "
            "canonical (post-validation) value without persisting, so an agent "
            "can show the user what would change before committing."
        ),
    )
    def set_setting(
        key: str, value: Any, confirmed: bool = False
    ) -> dict[str, Any]:
        from . import runtime_settings as rts
        from .scheduler.runner import reregister_cron_jobs
        if key not in rts.SCHEMA:
            return {"ok": False, "error": f"unknown setting {key!r}"}
        spec = rts.SCHEMA[key]
        try:
            # Always validate first — dry-runs exercise the schema.
            canonical = rts._validate(spec, value)
        except rts.SettingValidationError as e:
            return {"ok": False, "error": str(e)}
        if not confirmed:
            return {
                "ok": True,
                "confirmed": False,
                "key": key,
                "would_set": canonical,
                "current": rts.get_setting(key),
                "cron_reload": spec.cron_reload,
                "message": "dry-run; pass confirmed=True to apply",
            }
        try:
            canonical = rts.set_setting(key, value, actor="mcp")
        except rts.SettingValidationError as e:
            return {"ok": False, "error": str(e)}
        cron_status = None
        if spec.cron_reload:
            cron_status = reregister_cron_jobs(reason=f"mcp:{key}")
        return {
            "ok": True,
            "confirmed": True,
            "key": key,
            "value": canonical,
            "cron_status": cron_status,
        }

    # Phase 4.5 — boundary audit. Emits WARN per hardware-write tool that lacks
    # a `confirmed` parameter. Clean surface = silent; regressions are loud.
    audit_mcp_tool_surface(mcp)

    return mcp


def main() -> None:
    """Entry point: MCP over stdio (do not write logs to stdout).

    Singleton enforcement happens at module top — see ``_acquire_singleton_lock_early``.
    By the time we reach here we already own the lock (or the call site is a unit
    test importing ``main`` programmatically — fall back to acquiring late).
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("mcp_server")

    # Reuse the early-acquired fd from the module bootstrap. If absent (programmatic
    # call, e.g. from tests), acquire late — heavy imports already ran in this case
    # so the protection is moot, but the lock still serializes runs.
    global _EARLY_LOCK_FD
    if _EARLY_LOCK_FD is None:
        _EARLY_LOCK_FD = _acquire_singleton_lock_early()
        if _EARLY_LOCK_FD is None:
            sys.exit(0)
    lock_fd = _EARLY_LOCK_FD

    def _release_lock() -> None:
        # S5b: shut down the optimizer executor before releasing — clean hygiene.
        try:
            _optimizer_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass

    atexit.register(_release_lock)

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("MCP received signal %d — shutting down", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGHUP, _on_signal)

    try:
        build_mcp().run(transport="stdio")
    except KeyboardInterrupt:
        pass
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
