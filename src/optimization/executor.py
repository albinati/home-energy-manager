import logging
from typing import Optional

from ..config import config
from ..daikin.client import DaikinClient
from ..foxess.client import FoxESSClient
from .engine import get_optimization_engine
from .dispatcher import build_macro_from_clients

logger = logging.getLogger(__name__)

def execute_dispatch() -> None:
    eng = get_optimization_engine()
    if not eng.is_enabled():
        return
        
    plan = eng.solve_from_cache()
    if not plan:
        logger.warning("No Agile plan available for dispatch")
        return
        
    try:
        daikin_client = DaikinClient()
        devices = daikin_client.get_devices()
        if not devices:
            logger.error("No Daikin devices found")
            return
        dev = devices[0]
        daikin_status = daikin_client.get_status(dev)
    except Exception as e:
        logger.exception("Failed to get Daikin status: %s", e)
        return
        
    try:
        fox_client = FoxESSClient(**config.foxess_client_kwargs())
        fox_status = fox_client.get_realtime()
        soc = fox_status.soc
    except Exception as e:
        logger.exception("Failed to get FoxESS status: %s", e)
        soc = None

    macro = build_macro_from_clients(
        room_temp=daikin_status.room_temp,
        tank_temp=daikin_status.tank_temp,
        tank_target=daikin_status.tank_target,
        outdoor_temp=daikin_status.outdoor_temp,
        battery_soc=soc,
        weather_regulation=daikin_status.weather_regulation,
        operation_mode=daikin_status.mode or "heating"
    )
    
    hints = eng.dispatch_hints(macro)
    if not hints:
        return
        
    logger.info("Dispatch hints: %s", hints)
    
    # Apply Daikin hints
    try:
        if hints.disable_weather_regulation and daikin_status.weather_regulation:
            daikin_client.set_weather_regulation(dev, False)
            logger.info("Disabled weather regulation (V7 requirement)")
            
        current_lwt = daikin_status.lwt_offset or 0.0
        if hints.lwt_offset != current_lwt:
            daikin_client.set_lwt_offset(dev, hints.lwt_offset, daikin_status.mode or "heating")
            logger.info("Set LWT offset to %s", hints.lwt_offset)
            
        if hints.daikin_tank_target_c is not None:
            if daikin_status.tank_target != hints.daikin_tank_target_c:
                daikin_client.set_tank_temperature(dev, hints.daikin_tank_target_c)
                logger.info("Set DHW tank target to %s", hints.daikin_tank_target_c)
    except Exception as e:
        logger.exception("Failed to apply Daikin hints: %s", e)
        
    # Apply FoxESS hints
    try:
        if hints.fox_work_mode:
            fox_client.set_work_mode(hints.fox_work_mode)
            logger.info("Set FoxESS mode to %s", hints.fox_work_mode)
    except Exception as e:
        logger.exception("Failed to apply FoxESS hints: %s", e)

