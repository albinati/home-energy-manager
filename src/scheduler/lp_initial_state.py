"""Read physical initial state for the PuLP MILP (battery SoC, DHW tank, indoor air)."""
from __future__ import annotations

import logging
from typing import Any

from .. import db
from ..config import config
from ..daikin import service as daikin_service
from ..foxess.service import get_cached_realtime
from .lp_optimizer import LpInitialState

logger = logging.getLogger(__name__)


def read_lp_initial_state(
    daikin: Any | None = None,
    *,
    allow_daikin_refresh: bool = True,
) -> LpInitialState:
    """SoC from FoxESS cache (or DB realtime snapshot); tank and room from Daikin cache if available; else execution_log / defaults.

    Priority order for SoC:
    1. Live Fox realtime cache (get_cached_realtime) — fresh within seconds
    2. DB fox_realtime_snapshot — fresh within 15 min (set by MPC job)
    3. Config default (50% capacity)

    Daikin telemetry is sourced from the cached service (get_cached_devices) — the ``daikin``
    parameter is kept for backwards-compat but no longer issues HTTP calls. Quota is preserved.

    Phase 4 review: ``allow_daikin_refresh=False`` forbids any cache-miss refresh
    that would burn Daikin quota. Simulation paths pass this to honor the
    "no quota burn" guarantee even when the MCP-process cache is cold.
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
    tank_source = "default"
    indoor_source = "default"

    try:
        # #55 — use the cached/estimator wrapper so LP still seeds sensibly
        # when the Daikin daily quota (200/day) is exhausted. When
        # ``allow_daikin_refresh=False`` (simulation paths that must not burn
        # quota), we still read cached live telemetry + estimator — both are
        # local SQLite and don't touch the Onecta API.
        if allow_daikin_refresh:
            state = daikin_service.get_lp_state_cached_or_estimated(actor="lp_init")
        else:
            # Simulation: skip the live-fetch branch by not letting the wrapper
            # call get_cached_devices(allow_refresh=True). A lightweight reread
            # via the cached-devices path is still safe (it never burns quota
            # when allow_refresh=False).
            result = daikin_service.get_cached_devices(
                allow_refresh=False,
                max_age_seconds=config.DAIKIN_LP_INIT_CACHE_MAX_AGE_SECONDS,
                actor="lp_init",
            )
            state = {
                "tank_temp_c": result.devices[0].tank_temperature
                if result.devices
                else None,
                "indoor_temp_c": (
                    getattr(result.devices[0].temperature, "room_temperature", None)
                    if result.devices
                    else None
                ),
                "source": result.source,
            }
        src = str(state.get("source") or "daikin_unknown")
        if state.get("tank_temp_c") is not None:
            tank = float(state["tank_temp_c"])
            tank_source = src
        if state.get("indoor_temp_c") is not None:
            indoor = float(state["indoor_temp_c"])
            indoor_source = src
        logger.debug(
            "LP Daikin seed: tank=%.1f°C indoor=%.1f°C source=%s",
            tank,
            indoor,
            state.get("source"),
        )
    except Exception as e:
        logger.debug("LP Daikin telemetry fallback: %s", e)

    # Last known room from execution logs if still default-shaped
    try:
        rows = db.get_execution_logs(limit=1)
        if rows and indoor == float(config.INDOOR_SETPOINT_C):
            r = rows[0].get("daikin_room_temp")
            if r is not None:
                indoor = float(r)
                indoor_source = "execution_log"
    except Exception:
        pass

    return LpInitialState(
        soc_kwh=soc_kwh,
        tank_temp_c=tank,
        indoor_temp_c=indoor,
        soc_source=soc_source,
        tank_source=tank_source,
        indoor_source=indoor_source,
    )
