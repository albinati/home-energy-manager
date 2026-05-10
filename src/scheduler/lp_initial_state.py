"""Read physical initial state for the PuLP MILP (battery SoC + DHW tank).

PR Phase B (#306 follow-up): ``indoor_temp_c`` removed. The Daikin Altherma
exposes no room sensor (0% heartbeat coverage observed) and the LP no longer
carries an indoor-temp decision variable — heating demand is bound by
``get_daikin_heating_kw(t_outdoor)`` directly. This file no longer reads
``room_temperature`` from Daikin.
"""
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
    """SoC from FoxESS cache (or DB realtime snapshot); tank from Daikin cache.

    Priority order for SoC:
    1. Live Fox realtime cache (``get_cached_realtime``) — fresh within seconds
    2. DB ``fox_realtime_snapshot`` — fresh within 15 min (set by MPC job)
    3. Config default (50% capacity)

    Daikin telemetry is sourced from the cached service (``get_cached_devices``)
    — the ``daikin`` parameter is kept for backwards-compat but no longer issues
    HTTP calls. Quota is preserved.

    ``allow_daikin_refresh=False`` forbids any cache-miss refresh that would
    burn Daikin quota. Simulation paths pass this to honor the "no quota burn"
    guarantee even when the MCP-process cache is cold.
    """
    soc_kwh = float(config.BATTERY_CAPACITY_KWH) * 0.5
    soc_source = "default"
    try:
        rt = get_cached_realtime()
        soc_kwh = float(config.BATTERY_CAPACITY_KWH) * float(rt.soc) / 100.0
        soc_source = "fox_realtime_cache"
    except Exception as e:
        logger.debug("LP initial SoC live fallback: %s", e)
        try:
            snap = db.get_fox_realtime_snapshot()
            if snap and snap.get("soc_pct") is not None:
                soc_kwh = float(config.BATTERY_CAPACITY_KWH) * float(snap["soc_pct"]) / 100.0
                soc_source = "db_realtime_snapshot"
        except Exception as e2:
            logger.debug("LP initial SoC DB snapshot fallback: %s", e2)

    logger.debug("LP initial SoC=%.2f kWh (source=%s)", soc_kwh, soc_source)

    tank = float(config.DHW_TEMP_NORMAL_C)
    tank_source = "default"

    try:
        if allow_daikin_refresh:
            state = daikin_service.get_lp_state_cached_or_estimated(actor="lp_init")
        else:
            # Simulation: cache-only path; never burns Daikin quota.
            result = daikin_service.get_cached_devices(
                allow_refresh=False,
                max_age_seconds=config.DAIKIN_LP_INIT_CACHE_MAX_AGE_SECONDS,
                actor="lp_init",
            )
            state = {
                "tank_temp_c": result.devices[0].tank_temperature
                if result.devices
                else None,
                "source": result.source,
            }
        src = str(state.get("source") or "daikin_unknown")
        if state.get("tank_temp_c") is not None:
            tank = float(state["tank_temp_c"])
            tank_source = src
        logger.debug("LP Daikin seed: tank=%.1f°C source=%s", tank, state.get("source"))
    except Exception as e:
        logger.debug("LP Daikin telemetry fallback: %s", e)

    return LpInitialState(
        soc_kwh=soc_kwh,
        tank_temp_c=tank,
        soc_source=soc_source,
        tank_source=tank_source,
    )
