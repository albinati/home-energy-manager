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
        
    logger.info("Dispatch hints (DRY-RUN MODE): %s", hints)
    
    # DRY-RUN / SIMULATION MODE
    logger.info("CRITICAL: Executing in simulation/read-only mode. No commands will be sent to hardware.")
    
    # Simulate Daikin hints
    try:
        if hints.disable_weather_regulation and daikin_status.weather_regulation:
            logger.info("[DRY-RUN] Would disable weather regulation on Daikin")
            
        current_lwt = daikin_status.lwt_offset or 0.0
        if hints.lwt_offset != current_lwt:
            logger.info("[DRY-RUN] Would set Daikin LWT offset to %s", hints.lwt_offset)
            
        if hints.daikin_tank_target_c is not None:
            if daikin_status.tank_target != hints.daikin_tank_target_c:
                logger.info("[DRY-RUN] Would set Daikin DHW tank target to %s", hints.daikin_tank_target_c)
    except Exception as e:
        logger.exception("Failed during Daikin simulation: %s", e)
        
    # Simulate FoxESS hints
    try:
        if hints.fox_work_mode:
            logger.info("[DRY-RUN] Would set FoxESS mode to %s", hints.fox_work_mode)
    except Exception as e:
        logger.exception("Failed during FoxESS simulation: %s", e)

