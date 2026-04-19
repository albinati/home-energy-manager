"""Read physical initial state for the PuLP MILP (battery SoC, DHW tank, indoor air)."""
from __future__ import annotations

import logging
from typing import Any

from .. import db
from ..config import config
from ..foxess.service import get_cached_realtime
from .lp_optimizer import LpInitialState

logger = logging.getLogger(__name__)


def read_lp_initial_state(daikin: Any | None = None) -> LpInitialState:
    """SoC from FoxESS cache (or DB realtime snapshot); tank and room from Daikin if available; else execution_log / defaults.

    Priority order for SoC:
    1. Live Fox realtime cache (get_cached_realtime) — fresh within seconds
    2. DB fox_realtime_snapshot — fresh within 15 min (set by MPC job)
    3. Config default (50% capacity)
    """
    soc_kwh = float(config.BATTERY_CAPACITY_KWH) * 0.5
    soc_source = "default"
    try:
        rt = get_cached_realtime()
        soc_kwh = float(config.BATTERY_CAPACITY_KWH) * float(rt.soc) / 100.0
        soc_source = "fox_realtime_cache"
    except Exception as e:
        logger.debug("LP initial SoC live fallback: %s", e)
        # Try DB snapshot (written by MPC job)
        try:
            snap = db.get_fox_realtime_snapshot()
            if snap and snap.get("soc_pct") is not None:
                soc_kwh = float(config.BATTERY_CAPACITY_KWH) * float(snap["soc_pct"]) / 100.0
                soc_source = "db_realtime_snapshot"
        except Exception as e2:
            logger.debug("LP initial SoC DB snapshot fallback: %s", e2)

    logger.debug("LP initial SoC=%.2f kWh (source=%s)", soc_kwh, soc_source)

    tank = float(config.DHW_TEMP_NORMAL_C)
    indoor = float(config.INDOOR_SETPOINT_C)

    if daikin is not None:
        try:
            devices = daikin.get_devices()
            if devices:
                d0 = devices[0]
                if d0.tank_temperature is not None:
                    tank = float(d0.tank_temperature)
                rt_room = getattr(d0.temperature, "room_temperature", None)
                if rt_room is not None:
                    indoor = float(rt_room)
        except Exception as e:
            logger.debug("LP Daikin telemetry fallback: %s", e)

    # Last known room from execution logs if still default-shaped
    try:
        rows = db.get_execution_logs(limit=1)
        if rows and indoor == float(config.INDOOR_SETPOINT_C):
            r = rows[0].get("daikin_room_temp")
            if r is not None:
                indoor = float(r)
    except Exception:
        pass

    return LpInitialState(soc_kwh=soc_kwh, tank_temp_c=tank, indoor_temp_c=indoor)
