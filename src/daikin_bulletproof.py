"""Compare and apply scheduled Daikin params (Bulletproof heartbeat / recovery)."""
from __future__ import annotations

import logging
import time
from typing import Any

from . import db
from .config import config
from .daikin.client import DaikinClient, DaikinError
from .daikin.models import DaikinDevice

logger = logging.getLogger(__name__)


def _fclose(a: float | None, b: float, *, eps: float = 0.35) -> bool:
    if a is None:
        return False
    return abs(float(a) - float(b)) < eps


def daikin_device_matches_params(dev: DaikinDevice, params: dict[str, Any]) -> bool:
    """Return True if live readings match scheduled params (skips redundant PATCHes).

    ``tank_power`` and ``tank_powerful`` compare against ``dev.tank_on`` /
    ``dev.tank_powerful`` when the live value is known. If the live value is ``None``
    (not populated from the Onecta snapshot), fall back to writing — conservative.
    All other fields (lwt_offset, tank_temp, climate_on) use tolerance-based comparison.
    """
    if "lwt_offset" in params and dev.lwt_offset is not None:
        # leavingWaterOffset is not writable when climate is off — ignore mismatch (#18).
        if not ("climate_on" in params and not bool(params["climate_on"])):
            if not _fclose(dev.lwt_offset, float(params["lwt_offset"])):
                return False
    if "tank_temp" in params and dev.tank_target is not None:
        if not _fclose(dev.tank_target, float(params["tank_temp"]), eps=0.6):
            return False
    if "climate_on" in params:
        if dev.is_on != bool(params["climate_on"]):
            return False
    if "tank_power" in params:
        if dev.tank_on is None or dev.tank_on != bool(params["tank_power"]):
            return False
    if "tank_powerful" in params:
        if dev.tank_powerful is None or dev.tank_powerful != bool(params["tank_powerful"]):
            return False
    return True


def apply_scheduled_daikin_params(
    dev: DaikinDevice,
    client: DaikinClient,
    params: dict[str, Any],
    *,
    trigger: str,
    skip_if_matches: bool = True,
) -> bool:
    """Apply params; return True if any write was attempted (and not skipped)."""
    p = dict(params)
    if "climate_on" in p and not bool(p["climate_on"]):
        p.pop("lwt_offset", None)

    if skip_if_matches and daikin_device_matches_params(dev, p):
        return False
    if config.OPERATION_MODE != "operational" or config.OPENCLAW_READ_ONLY:
        db.log_action(
            device="daikin",
            action="scheduled_apply",
            params=p,
            result="skipped",
            trigger=trigger,
            error_msg="read_only or simulation",
        )
        return False
    settle = max(0, int(getattr(config, "DAIKIN_VALVE_SETTLE_SECONDS", 10)))
    try:
        climate_going_off = "climate_on" in p and not bool(p["climate_on"])
        climate_going_on = "climate_on" in p and bool(p["climate_on"])
        device_is_on = bool(dev.is_on)
        has_dhw_cmds = "tank_power" in p or "tank_powerful" in p

        # leavingWaterOffset is read-only when the climate zone is physically OFF.
        # Zone will be ON after this call only if it's currently ON (and not turning off)
        # OR if we're explicitly turning it on.
        zone_will_be_on = (device_is_on and not climate_going_off) or climate_going_on

        # If turning ON: send power first so lwt_offset is writable on the next call.
        # The 3-way valve needs time to settle; sleep before DHW commands to prevent
        # silent command drops from the Daikin mainboard.
        if climate_going_on:
            client.set_power(dev, True)
            if has_dhw_cmds and settle:
                time.sleep(settle)

        if "lwt_offset" in p:
            if not zone_will_be_on:
                logger.debug("Skipping lwt_offset: zone is OFF or turning OFF (read-only characteristic)")
            elif dev.lwt_offset_range is not None and not getattr(dev.lwt_offset_range, "settable", True):
                logger.debug("Skipping lwt_offset: device reports characteristic as non-settable (weatherDependent mode)")
            else:
                try:
                    client.set_lwt_offset(dev, float(p["lwt_offset"]))
                except DaikinError as exc:
                    if "[read_only]" in str(exc):
                        # Non-fatal: lwt_offset read-only (e.g. weatherDependent setpoint mode).
                        # Continue applying remaining commands (tank_temp, tank_power, etc.)
                        logger.debug("lwt_offset rejected as read-only, continuing: %s", exc)
                    else:
                        raise

        if climate_going_off:
            client.set_power(dev, False)
            if has_dhw_cmds and settle:
                time.sleep(settle)

        # tank_power must be set before tank_temp: Daikin returns READ_ONLY_CHARACTERISTIC
        # for the target temperature when the tank is powered off.
        tank_turning_on = "tank_power" in p and bool(p["tank_power"])
        if tank_turning_on:
            client.set_tank_power(dev, True)
            if "tank_temp" in p and settle:
                time.sleep(settle)  # onOffMode must settle before temperatureControl is writable

        if "tank_temp" in p:
            try:
                client.set_tank_temperature(dev, float(p["tank_temp"]))
            except DaikinError as exc:
                if "[read_only]" in str(exc) and tank_turning_on:
                    # Cloud hasn't propagated tank-on yet; heartbeat will retry next tick
                    logger.warning("tank_temp read-only after tank_power=on (cloud lag) — will retry: %s", exc)
                else:
                    raise
        if not tank_turning_on and "tank_power" in p:
            client.set_tank_power(dev, bool(p["tank_power"]))
        if "tank_powerful" in p:
            client.set_tank_powerful(dev, bool(p["tank_powerful"]))
    except (DaikinError, ValueError) as e:
        db.log_action(
            device="daikin",
            action="scheduled_apply",
            params=p,
            result="failure",
            trigger=trigger,
            error_msg=str(e),
        )
        raise
    db.log_action(
        device="daikin",
        action="scheduled_apply",
        params=p,
        result="success",
        trigger=trigger,
    )
    return True


def apply_comfort_restore(
    dev: DaikinDevice,
    client: DaikinClient,
    *,
    trigger: str,
) -> None:
    """Neutral LWT + normal tank targets during peak comfort override."""
    params = {
        "lwt_offset": 0.0,
        "climate_on": True,
        "tank_power": True,
        "tank_powerful": False,
        "tank_temp": float(config.DHW_TEMP_NORMAL_C),
    }
    apply_scheduled_daikin_params(dev, client, params, trigger=trigger, skip_if_matches=False)
