"""Compare and apply scheduled Daikin params (Bulletproof heartbeat / recovery)."""
from __future__ import annotations

from typing import Any, Optional

from .config import config
from . import db
from .daikin.client import DaikinClient, DaikinError
from .daikin.models import DaikinDevice


def _fclose(a: Optional[float], b: float, *, eps: float = 0.35) -> bool:
    if a is None:
        return False
    return abs(float(a) - float(b)) < eps


def daikin_device_matches_params(dev: DaikinDevice, params: dict[str, Any]) -> bool:
    """Return True if live readings match scheduled params (skips redundant PATCHes)."""
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
        if "lwt_offset" in params:
            client.set_lwt_offset(dev, float(params["lwt_offset"]))
        if "climate_on" in params:
            client.set_power(dev, bool(params["climate_on"]))
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
