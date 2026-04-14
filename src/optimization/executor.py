"""Optimization executor — applies dispatch hints to hardware or simulates them.

OPERATION_MODE controls behaviour:
  simulation  (default): compute hints, log what would happen, send notification.
                          Never writes to hardware.
  operational:           compute hints from the APPROVED plan, write to Daikin and Fox ESS,
                          audit-log every action, fall back to Self Use on any write failure.

The executor will not dispatch in operational mode unless there is a user-approved
plan in the consent store. This enforces the consent-first design principle.
"""
import logging
from typing import Optional

from ..config import config
from ..daikin.client import DaikinClient
from ..foxess.client import FoxESSClient
from ..notifier import notify
from .consent import get_approved_plan
from .dispatcher import build_macro_from_clients
from .engine import get_optimization_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

def _simulate_hints(hints, daikin_status, fox_soc: Optional[float]) -> None:
    """Log what would be dispatched in simulation mode and notify via OpenClaw."""
    lines = ["[SIMULATION] Optimization dispatch — what would happen this tick:"]

    if hints.disable_weather_regulation and daikin_status.weather_regulation:
        lines.append("  • Would disable Daikin weather regulation (switch to fixed setpoint)")

    current_lwt = daikin_status.lwt_offset or 0.0
    if abs(hints.lwt_offset - current_lwt) >= 0.1:
        lines.append(
            f"  • Would set Daikin LWT offset: {current_lwt:+g} → {hints.lwt_offset:+g}"
        )

    if hints.daikin_tank_target_c is not None:
        current_tank = daikin_status.tank_target
        if current_tank is None or abs(hints.daikin_tank_target_c - current_tank) >= 0.5:
            lines.append(
                f"  • Would set Daikin DHW tank target: "
                f"{current_tank}°C → {hints.daikin_tank_target_c}°C"
            )

    if hints.fox_work_mode:
        lines.append(f"  • Would set Fox ESS work mode: {hints.fox_work_mode}")

    if fox_soc is not None:
        lines.append(f"  • Battery SoC at dispatch: {fox_soc:.0f}%")

    lines.append(f"  • Reason: {hints.reason}")
    lines.append(
        "  [To activate, approve a plan and set OPERATION_MODE=operational]"
    )

    msg = "\n".join(lines)
    logger.info(msg)
    notify(msg)


# ---------------------------------------------------------------------------
# Live write helpers
# ---------------------------------------------------------------------------

def _write_daikin(client: DaikinClient, dev, status, hints, audit_fn) -> None:
    """Apply Daikin hints with individual error handling per write."""
    if hints.disable_weather_regulation and status.weather_regulation:
        try:
            client.set_fixed_setpoint_mode(dev)
            audit_fn("daikin.weather_regulation", {"action": "disable"}, True, "Disabled weather regulation")
            logger.info("[OPERATIONAL] Disabled Daikin weather regulation")
        except Exception as exc:
            audit_fn("daikin.weather_regulation", {}, False, str(exc))
            logger.error("Failed to disable weather regulation: %s", exc)

    current_lwt = status.lwt_offset or 0.0
    if abs(hints.lwt_offset - current_lwt) >= 0.1:
        try:
            op_mode = status.mode or "heating"
            client.set_lwt_offset(dev, hints.lwt_offset, op_mode)
            audit_fn(
                "daikin.lwt_offset",
                {"offset": hints.lwt_offset},
                True,
                f"LWT offset {current_lwt:+g} → {hints.lwt_offset:+g}",
            )
            logger.info("[OPERATIONAL] Daikin LWT offset set to %s", hints.lwt_offset)
        except Exception as exc:
            audit_fn("daikin.lwt_offset", {"offset": hints.lwt_offset}, False, str(exc))
            logger.error("Failed to set Daikin LWT offset: %s", exc)

    if hints.daikin_tank_target_c is not None:
        current_tank = status.tank_target
        if current_tank is None or abs(hints.daikin_tank_target_c - current_tank) >= 0.5:
            try:
                client.set_tank_temperature(dev, hints.daikin_tank_target_c)
                audit_fn(
                    "daikin.tank_temperature",
                    {"temperature": hints.daikin_tank_target_c},
                    True,
                    f"DHW tank {current_tank}°C → {hints.daikin_tank_target_c}°C",
                )
                logger.info(
                    "[OPERATIONAL] Daikin DHW tank target set to %s°C",
                    hints.daikin_tank_target_c,
                )
            except Exception as exc:
                audit_fn(
                    "daikin.tank_temperature",
                    {"temperature": hints.daikin_tank_target_c},
                    False,
                    str(exc),
                )
                logger.error("Failed to set DHW tank temperature: %s", exc)


def _write_fox(client: FoxESSClient, hints, audit_fn) -> None:
    """Apply Fox ESS mode hint with fallback to Self Use on failure."""
    if not hints.fox_work_mode:
        return
    try:
        client.set_work_mode(hints.fox_work_mode)
        audit_fn(
            "foxess.mode",
            {"mode": hints.fox_work_mode},
            True,
            f"Work mode set to {hints.fox_work_mode}",
        )
        logger.info("[OPERATIONAL] Fox ESS work mode set to %s", hints.fox_work_mode)
    except Exception as exc:
        audit_fn("foxess.mode", {"mode": hints.fox_work_mode}, False, str(exc))
        logger.error("Failed to set Fox ESS mode: %s — falling back to Self Use", exc)
        try:
            client.set_work_mode("Self Use")
        except Exception as fb_exc:
            logger.error("Fallback to Self Use also failed: %s", fb_exc)


# ---------------------------------------------------------------------------
# Main dispatch entry point
# ---------------------------------------------------------------------------

def execute_dispatch() -> None:
    """Run one dispatch tick.

    In simulation mode: reads live state, computes hints, logs + notifies, no writes.
    In operational mode: same, but also writes to hardware — only if a plan is approved.
    """
    eng = get_optimization_engine()
    if not eng.is_enabled():
        return

    operation_mode = config.OPERATION_MODE

    # In operational mode, require an approved plan before doing anything
    if operation_mode == "operational":
        approved = get_approved_plan()
        if approved is None:
            logger.info(
                "[OPERATIONAL] No approved plan — skipping dispatch. "
                "Propose a plan and approve it to activate optimization."
            )
            return
        plan = approved.plan
    else:
        # Simulation: always recompute from cache (no consent needed to simulate)
        plan = eng.solve_from_cache()
        if not plan:
            logger.warning("No Agile plan available for simulation")
            return

    # Read Daikin status
    try:
        daikin_client = DaikinClient()
        devices = daikin_client.get_devices()
        if not devices:
            logger.error("No Daikin devices found for dispatch")
            return
        dev = devices[0]
        daikin_status = daikin_client.get_status(dev)
    except Exception as exc:
        logger.exception("Failed to get Daikin status: %s", exc)
        return

    # Read Fox ESS SoC (non-fatal if unavailable)
    soc: Optional[float] = None
    fox_client: Optional[FoxESSClient] = None
    try:
        fox_client = FoxESSClient(**config.foxess_client_kwargs())
        fox_status = fox_client.get_realtime()
        soc = fox_status.soc
    except Exception as exc:
        logger.warning("Failed to get Fox ESS status: %s", exc)

    macro = build_macro_from_clients(
        room_temp=daikin_status.room_temp,
        tank_temp=daikin_status.tank_temp,
        tank_target=daikin_status.tank_target,
        outdoor_temp=daikin_status.outdoor_temp,
        battery_soc=soc,
        weather_regulation=daikin_status.weather_regulation,
        operation_mode=daikin_status.mode or "heating",
    )

    # Use the plan from engine (already set via solve_from_cache or approved plan)
    eng._last_plan = plan
    hints = eng.dispatch_hints(macro)
    if not hints:
        return

    if operation_mode == "simulation":
        _simulate_hints(hints, daikin_status, soc)
        return

    # Operational mode — apply writes
    from ..api import safeguards

    def audit(action, params, ok, msg):
        safeguards.audit_log(action, params, "optimization_executor", ok, msg)

    logger.info("[OPERATIONAL] Dispatching hints: %s", hints)

    _write_daikin(daikin_client, dev, daikin_status, hints, audit)

    if fox_client is not None:
        _write_fox(fox_client, hints, audit)
