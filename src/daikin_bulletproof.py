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

    For ``tank_power`` and ``tank_powerful`` the device model does not expose live
    values in this snapshot, so we cannot confirm a match. Those fields always trigger
    a write to ensure correct state (conservative but safe).
    All other fields (lwt_offset, tank_temp, climate_on) use tolerance-based comparison.
    """
    if "lwt_offset" in params and dev.lwt_offset is not None:
        if not _fclose(dev.lwt_offset, float(params["lwt_offset"])):
            return False
    if "tank_temp" in params and dev.tank_target is not None:
        if not _fclose(dev.tank_target, float(params["tank_temp"]), eps=0.6):
            return False
    if "climate_on" in params:
        if dev.is_on != bool(params["climate_on"]):
            return False
    if "tank_power" in params or "tank_powerful" in params:
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
    if skip_if_matches and daikin_device_matches_params(dev, params):
        return False
    if config.OPERATION_MODE != "operational" or config.OPENCLAW_READ_ONLY:
        db.log_action(
            device="daikin",
            action="scheduled_apply",
            params=params,
            result="skipped",
            trigger=trigger,
            error_msg="read_only or simulation",
        )
        return False
    try:
        climate_going_off = "climate_on" in params and not bool(params["climate_on"])
        climate_going_on = "climate_on" in params and bool(params["climate_on"])
        device_is_on = bool(dev.is_on)
        has_dhw_cmds = "tank_power" in params or "tank_powerful" in params

        # leavingWaterOffset is read-only when the climate zone is physically OFF.
        # Zone will be ON after this call only if it's currently ON (and not turning off)
        # OR if we're explicitly turning it on.
        zone_will_be_on = (device_is_on and not climate_going_off) or climate_going_on

        # If turning ON: send power first so lwt_offset is writable on the next call.
        # The 3-way valve takes 10-15 s to settle; sleep before DHW commands to prevent
        # silent command drops from the Daikin mainboard.
        if climate_going_on:
            client.set_power(dev, True)
            if has_dhw_cmds:
                time.sleep(10)

        if "lwt_offset" in params:
            if not zone_will_be_on:
                logger.debug("Skipping lwt_offset: zone is OFF or turning OFF (read-only characteristic)")
            elif dev.lwt_offset_range is not None and not getattr(dev.lwt_offset_range, "settable", True):
                logger.debug("Skipping lwt_offset: device reports characteristic as non-settable (weatherDependent mode)")
            else:
                try:
                    client.set_lwt_offset(dev, float(params["lwt_offset"]))
                except DaikinError as exc:
                    if "[read_only]" in str(exc):
                        # Non-fatal: lwt_offset read-only (e.g. weatherDependent setpoint mode).
                        # Continue applying remaining commands (tank_temp, tank_power, etc.)
                        logger.debug("lwt_offset rejected as read-only, continuing: %s", exc)
                    else:
                        raise

        if climate_going_off:
            client.set_power(dev, False)

        if "tank_temp" in params:
            client.set_tank_temperature(dev, float(params["tank_temp"]))
        if "tank_power" in params:
            client.set_tank_power(dev, bool(params["tank_power"]))
        if "tank_powerful" in params:
            client.set_tank_powerful(dev, bool(params["tank_powerful"]))
    except (DaikinError, ValueError) as e:
        db.log_action(
            device="daikin",
            action="scheduled_apply",
            params=params,
            result="failure",
            trigger=trigger,
            error_msg=str(e),
        )
        raise
    db.log_action(
        device="daikin",
        action="scheduled_apply",
        params=params,
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
