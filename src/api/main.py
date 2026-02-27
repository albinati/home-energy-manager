"""FastAPI application for home-energy-manager REST API."""
import logging
import os
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import config
from ..daikin.client import DaikinClient, DaikinError
from ..foxess.client import FoxESSClient, FoxESSError
from ..foxess.models import ChargePeriod
from ..foxess.service import get_cached_realtime, get_refresh_stats
from ..energy.monthly import get_monthly_insights, get_period_insights

logger = logging.getLogger(__name__)

from .models import (
    DaikinStatusResponse,
    FoxESSStatusResponse,
    PowerRequest,
    TemperatureRequest,
    LWTOffsetRequest,
    ModeRequest,
    TankTemperatureRequest,
    TankPowerRequest,
    FoxESSModeRequest,
    ChargePeriodRequest,
    PendingActionResponse,
    ConfirmRequest,
    ActionResult,
    ActionStatus,
    ErrorResponse,
    OpenClawAction,
    OpenClawExecuteRequest,
    OpenClawCapability,
    OpenClawCapabilitiesResponse,
    EnergyProviderEnum,
    EnergyProviderInfo,
    EnergyProvidersResponse,
    TariffResponse,
    TariffTypeEnum,
    EnergyUsageResponse,
    MonthlyInsightsResponse,
    MonthlyEnergySummaryResponse,
    MonthlyCostSummaryResponse,
    PeriodInsightsResponse,
    EnergyReportResponse,
    HeatingAnalyticsResponse,
    TempBandSummaryResponse,
    ChartDataPoint,
    EnergyInsightsTextResponse,
    AssistantRecommendRequest,
    AssistantRecommendResponse,
    SuggestedActionSchema,
    AssistantApplyRequest,
    AssistantApplyResponse,
    AssistantApplyResultItem,
    SchedulerStatusResponse,
)
from . import safeguards
from ..assistant import build_context, get_suggestions, validate_suggested_actions, SuggestedAction
from ..scheduler.runner import (
    get_scheduler_status,
    pause_scheduler,
    resume_scheduler,
    start_background_scheduler,
    stop_background_scheduler,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_background_scheduler()
    yield
    stop_background_scheduler()
    safeguards.cleanup_expired_actions()


app = FastAPI(
    title="Home Energy Manager API",
    description="REST API for controlling Daikin heat pump and Fox ESS battery system",
    version="1.0.0",
    lifespan=lifespan,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_daikin_client() -> DaikinClient:
    return DaikinClient()


def get_foxess_client() -> FoxESSClient:
    return FoxESSClient(**config.foxess_client_kwargs())


@app.get("/", response_class=HTMLResponse)
async def web_dashboard(request: Request):
    """Serve the web dashboard."""
    daikin_status = None
    foxess_status = None
    daikin_error = None
    foxess_error = None

    try:
        client = get_daikin_client()
        devices = client.get_devices()
        if devices:
            dev = devices[0]
            s = client.get_status(dev)
            daikin_status = {
                "device_id": dev.id,
                "device_name": dev.model or s.device_name or dev.id,
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
                "weather_regulation_settable": dev.weather_regulation_settable,
                "lwt_offset_range": {
                    "min": dev.lwt_offset_range.min_value,
                    "max": dev.lwt_offset_range.max_value,
                    "step": dev.lwt_offset_range.step_value,
                },
                "room_temp_range": {
                    "min": dev.room_temp_range.min_value,
                    "max": dev.room_temp_range.max_value,
                    "step": dev.room_temp_range.step_value,
                    "settable": dev.room_temp_range.settable,
                },
                "tank_temp_range": {
                    "min": dev.tank_temp_range.min_value,
                    "max": dev.tank_temp_range.max_value,
                    "step": dev.tank_temp_range.step_value,
                },
            }
    except Exception as e:
        daikin_error = str(e)
        logger.warning("Dashboard: Daikin status failed: %s", e, exc_info=True)

    if config.FOXESS_API_KEY or (config.FOXESS_USERNAME and config.FOXESS_PASSWORD):
        try:
            d = get_cached_realtime()
            last_ts, refresh_count = get_refresh_stats()
            updated_at_str = None
            if last_ts is not None:
                from datetime import datetime, timezone
                updated_at_str = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            foxess_status = {
                "soc": d.soc,
                "solar_power": d.solar_power,
                "grid_power": d.grid_power,
                "battery_power": d.battery_power,
                "load_power": d.load_power,
                "work_mode": d.work_mode,
                "updated_at": updated_at_str,
                "refresh_count_24h": refresh_count,
                "refresh_limit_24h": 1440,
            }
            logger.debug("Dashboard: Fox ESS data loaded soc=%.1f solar=%.2f", d.soc, d.solar_power)
        except TimeoutError as e:
            foxess_error = "Fox ESS cloud request timed out."
            logger.warning("Dashboard: Fox ESS timeout: %s", e)
        except OSError as e:
            foxess_error = f"Fox ESS cloud unreachable: {e}"
            logger.warning("Dashboard: Fox ESS connection error: %s", e)
        except Exception as e:
            foxess_error = str(e)
            logger.warning("Dashboard: Fox ESS status failed: %s", e, exc_info=True)

    logger.info(
        "Dashboard: daikin=%s foxess=%s daikin_error=%s foxess_error=%s",
        "ok" if daikin_status else "none",
        "ok" if foxess_status else "none",
        bool(daikin_error),
        bool(foxess_error),
    )
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "daikin": daikin_status,
            "foxess": foxess_status,
            "daikin_error": daikin_error,
            "foxess_error": foxess_error,
        },
    )


@app.get("/api/v1/health")
async def health():
    """Lightweight health check for gateways and process managers."""
    return {"status": "ok"}


@app.get("/api/v1/daikin/status", response_model=list[DaikinStatusResponse])
async def daikin_status():
    """Get status of all Daikin devices."""
    logger.debug("GET /api/v1/daikin/status requested")
    try:
        client = get_daikin_client()
        devices = client.get_devices()
        result = []
        for dev in devices:
            s = client.get_status(dev)
            result.append(DaikinStatusResponse(
                device_id=dev.id,
                device_name=s.device_name,
                model=dev.model,
                is_on=s.is_on,
                mode=s.mode,
                room_temp=s.room_temp,
                target_temp=s.target_temp,
                outdoor_temp=s.outdoor_temp,
                lwt=s.lwt,
                lwt_offset=s.lwt_offset,
                tank_temp=s.tank_temp,
                tank_target=s.tank_target,
                weather_regulation=s.weather_regulation,
            ))
        logger.info("Daikin status: %d device(s)", len(result))
        return result
    except FileNotFoundError as e:
        logger.warning("Daikin not configured: %s", e)
        raise HTTPException(status_code=503, detail=f"Daikin not configured: {e}")
    except DaikinError as e:
        logger.warning("Daikin API error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/v1/daikin/power", response_model=PendingActionResponse | ActionResult)
async def daikin_power(req: PowerRequest):
    """Turn Daikin climate control on or off."""
    action_type = "daikin.power"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    if safeguards.requires_confirmation(action_type) and not req.skip_confirmation:
        action = safeguards.create_pending_action(
            action_type=action_type,
            description=f"Turn Daikin climate control {'ON' if req.on else 'OFF'}",
            parameters={"on": req.on},
        )
        return PendingActionResponse(
            action=action,
            message=f"Confirm: Turn Daikin climate control {'ON' if req.on else 'OFF'}?",
        )
    
    try:
        client = get_daikin_client()
        devices = client.get_devices()
        if not devices:
            raise HTTPException(status_code=404, detail="No Daikin devices found")
        
        for dev in devices:
            client.set_power(dev, req.on)
        
        safeguards.record_action_time(action_type)
        safeguards.audit_log(action_type, {"on": req.on}, "api", True, "Power set successfully")
        
        return ActionResult(
            success=True,
            message=f"Daikin turned {'ON' if req.on else 'OFF'}",
        )
    except DaikinError as e:
        safeguards.audit_log(action_type, {"on": req.on}, "api", False, str(e))
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/v1/daikin/temperature", response_model=ActionResult)
async def daikin_temperature(req: TemperatureRequest):
    """Set Daikin target room temperature. Blocked when weather regulation is active."""
    action_type = "daikin.temperature"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        client = get_daikin_client()
        devices = client.get_devices()
        if not devices:
            raise HTTPException(status_code=404, detail="No Daikin devices found")
        
        for dev in devices:
            if dev.weather_regulation_enabled:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot set room temperature while weather regulation is active. "
                           "Use LWT offset instead, or disable weather regulation first.",
                )
            mode = req.mode or dev.operation_mode
            client.set_temperature(dev, req.temperature, mode)
        
        safeguards.record_action_time(action_type)
        safeguards.audit_log(action_type, {"temperature": req.temperature}, "api", True, "Temperature set")
        
        return ActionResult(
            success=True,
            message=f"Temperature set to {req.temperature}°C",
        )
    except DaikinError as e:
        safeguards.audit_log(action_type, {"temperature": req.temperature}, "api", False, str(e))
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/v1/daikin/lwt-offset", response_model=ActionResult)
async def daikin_lwt_offset(req: LWTOffsetRequest):
    """Set Daikin leaving water temperature offset."""
    action_type = "daikin.lwt_offset"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        client = get_daikin_client()
        devices = client.get_devices()
        if not devices:
            raise HTTPException(status_code=404, detail="No Daikin devices found")
        
        for dev in devices:
            mode = req.mode or dev.operation_mode
            client.set_lwt_offset(dev, req.offset, mode)
        
        safeguards.record_action_time(action_type)
        safeguards.audit_log(action_type, {"offset": req.offset}, "api", True, "LWT offset set")
        
        return ActionResult(
            success=True,
            message=f"LWT offset set to {req.offset:+g}",
        )
    except DaikinError as e:
        safeguards.audit_log(action_type, {"offset": req.offset}, "api", False, str(e))
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/v1/daikin/mode", response_model=ActionResult)
async def daikin_mode(req: ModeRequest):
    """Set Daikin operation mode."""
    action_type = "daikin.mode"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        client = get_daikin_client()
        devices = client.get_devices()
        if not devices:
            raise HTTPException(status_code=404, detail="No Daikin devices found")
        
        for dev in devices:
            client.set_operation_mode(dev, req.mode.value)
        
        safeguards.record_action_time(action_type)
        safeguards.audit_log(action_type, {"mode": req.mode.value}, "api", True, "Mode set")
        
        return ActionResult(
            success=True,
            message=f"Mode set to {req.mode.value}",
        )
    except (DaikinError, ValueError) as e:
        safeguards.audit_log(action_type, {"mode": req.mode.value}, "api", False, str(e))
        raise HTTPException(status_code=400 if isinstance(e, ValueError) else 502, detail=str(e))


@app.post("/api/v1/daikin/tank-temperature", response_model=ActionResult)
async def daikin_tank_temperature(req: TankTemperatureRequest):
    """Set Daikin DHW tank target temperature."""
    action_type = "daikin.tank_temperature"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        client = get_daikin_client()
        devices = client.get_devices()
        if not devices:
            raise HTTPException(status_code=404, detail="No Daikin devices found")
        
        for dev in devices:
            if dev.tank_target is not None:
                client.set_tank_temperature(dev, req.temperature)
        
        safeguards.record_action_time(action_type)
        safeguards.audit_log(action_type, {"temperature": req.temperature}, "api", True, "Tank temp set")
        
        return ActionResult(
            success=True,
            message=f"DHW tank target set to {req.temperature}°C",
        )
    except (DaikinError, ValueError) as e:
        safeguards.audit_log(action_type, {"temperature": req.temperature}, "api", False, str(e))
        raise HTTPException(status_code=400 if isinstance(e, ValueError) else 502, detail=str(e))


@app.post("/api/v1/daikin/tank-power", response_model=PendingActionResponse | ActionResult)
async def daikin_tank_power(req: TankPowerRequest):
    """Turn Daikin DHW tank on or off."""
    action_type = "daikin.tank_power"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    if safeguards.requires_confirmation(action_type) and not req.skip_confirmation:
        action = safeguards.create_pending_action(
            action_type=action_type,
            description=f"Turn DHW tank {'ON' if req.on else 'OFF'}",
            parameters={"on": req.on},
        )
        return PendingActionResponse(
            action=action,
            message=f"Confirm: Turn DHW tank {'ON' if req.on else 'OFF'}?",
        )
    
    try:
        client = get_daikin_client()
        devices = client.get_devices()
        if not devices:
            raise HTTPException(status_code=404, detail="No Daikin devices found")
        
        for dev in devices:
            client.set_tank_power(dev, req.on)
        
        safeguards.record_action_time(action_type)
        safeguards.audit_log(action_type, {"on": req.on}, "api", True, "Tank power set")
        
        return ActionResult(
            success=True,
            message=f"DHW tank turned {'ON' if req.on else 'OFF'}",
        )
    except DaikinError as e:
        safeguards.audit_log(action_type, {"on": req.on}, "api", False, str(e))
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/v1/foxess/status", response_model=FoxESSStatusResponse)
async def foxess_status():
    """Get Fox ESS battery/solar status."""
    logger.debug("GET /api/v1/foxess/status requested")
    try:
        d = get_cached_realtime()
        last_ts, refresh_count = get_refresh_stats()
        updated_at_str = None
        if last_ts is not None:
            from datetime import datetime, timezone
            updated_at_str = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        out = FoxESSStatusResponse(
            soc=d.soc,
            solar_power=d.solar_power,
            grid_power=d.grid_power,
            battery_power=d.battery_power,
            load_power=d.load_power,
            work_mode=d.work_mode,
            updated_at=updated_at_str,
            refresh_count_24h=refresh_count,
            refresh_limit_24h=1440,
        )
        logger.info(
            "Fox ESS status: soc=%.1f solar=%.2f grid=%.2f battery=%.2f load=%.2f work_mode=%s refresh_24h=%s",
            out.soc, out.solar_power, out.grid_power, out.battery_power, out.load_power,
            out.work_mode, (out.refresh_count_24h or 0),
        )
        return out
    except ValueError as e:
        logger.warning("Fox ESS not configured: %s", e)
        raise HTTPException(status_code=503, detail=f"Fox ESS not configured: {e}")
    except FoxESSError as e:
        logger.warning("Fox ESS API error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except TimeoutError as e:
        logger.warning("Fox ESS API timeout: %s", e)
        raise HTTPException(status_code=504, detail="Fox ESS cloud request timed out. Try again shortly.")
    except OSError as e:
        logger.warning("Fox ESS API connection error: %s", e)
        raise HTTPException(status_code=502, detail=f"Fox ESS cloud unreachable: {e}")


@app.post("/api/v1/foxess/mode", response_model=PendingActionResponse | ActionResult)
async def foxess_mode(req: FoxESSModeRequest):
    """Set Fox ESS inverter work mode."""
    action_type = "foxess.mode"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    if safeguards.requires_confirmation(action_type) and not req.skip_confirmation:
        action = safeguards.create_pending_action(
            action_type=action_type,
            description=f"Set inverter mode to '{req.mode.value}'",
            parameters={"mode": req.mode.value},
        )
        return PendingActionResponse(
            action=action,
            message=f"Confirm: Set inverter mode to '{req.mode.value}'?",
        )
    
    try:
        client = get_foxess_client()
        client.set_work_mode(req.mode.value)
        
        safeguards.record_action_time(action_type)
        safeguards.audit_log(action_type, {"mode": req.mode.value}, "api", True, "Mode set")
        
        return ActionResult(
            success=True,
            message=f"Work mode set to: {req.mode.value}",
        )
    except (FoxESSError, ValueError) as e:
        safeguards.audit_log(action_type, {"mode": req.mode.value}, "api", False, str(e))
        raise HTTPException(status_code=400 if isinstance(e, ValueError) else 502, detail=str(e))


@app.post("/api/v1/foxess/charge-period", response_model=ActionResult)
async def foxess_charge_period(req: ChargePeriodRequest):
    """Set Fox ESS charge period."""
    action_type = "foxess.charge_period"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        client = get_foxess_client()
        cp = ChargePeriod(
            start_time=req.start_time,
            end_time=req.end_time,
            target_soc=req.target_soc,
            enable=True,
        )
        client.set_charge_period(req.period_index, cp)
        
        safeguards.record_action_time(action_type)
        safeguards.audit_log(
            action_type,
            {"start": req.start_time, "end": req.end_time, "soc": req.target_soc},
            "api",
            True,
            "Charge period set",
        )
        
        return ActionResult(
            success=True,
            message=f"Charge period {req.period_index + 1} set: {req.start_time}–{req.end_time}, target SoC {req.target_soc}%",
        )
    except (FoxESSError, ValueError) as e:
        safeguards.audit_log(action_type, {"start": req.start_time, "end": req.end_time}, "api", False, str(e))
        raise HTTPException(status_code=400 if isinstance(e, ValueError) else 502, detail=str(e))


@app.post("/api/v1/confirm/{action_id}", response_model=ActionResult)
async def confirm_action(action_id: str, req: ConfirmRequest):
    """Confirm or cancel a pending action."""
    action = safeguards.get_pending_action(action_id)
    
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    
    if action.status == ActionStatus.EXPIRED:
        raise HTTPException(status_code=410, detail="Action has expired")
    
    if action.status != ActionStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Action already {action.status.value}")
    
    if not req.confirmed:
        safeguards.cancel_action(action_id)
        return ActionResult(
            success=True,
            message="Action cancelled",
            action_id=action_id,
        )
    
    safeguards.confirm_action(action_id)
    
    try:
        if action.action_type == "daikin.power":
            client = get_daikin_client()
            devices = client.get_devices()
            for dev in devices:
                client.set_power(dev, action.parameters["on"])
            msg = f"Daikin turned {'ON' if action.parameters['on'] else 'OFF'}"
        
        elif action.action_type == "daikin.tank_power":
            client = get_daikin_client()
            devices = client.get_devices()
            for dev in devices:
                client.set_tank_power(dev, action.parameters["on"])
            msg = f"DHW tank turned {'ON' if action.parameters['on'] else 'OFF'}"
        
        elif action.action_type == "foxess.mode":
            client = get_foxess_client()
            client.set_work_mode(action.parameters["mode"])
            msg = f"Work mode set to: {action.parameters['mode']}"
        
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action type: {action.action_type}")
        
        safeguards.mark_executed(action_id)
        safeguards.record_action_time(action.action_type)
        safeguards.audit_log(action.action_type, action.parameters, "api", True, msg)
        
        return ActionResult(
            success=True,
            message=msg,
            action_id=action_id,
        )
    
    except (DaikinError, FoxESSError) as e:
        safeguards.audit_log(action.action_type, action.parameters, "api", False, str(e))
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/v1/pending/{action_id}")
async def get_pending_action(action_id: str):
    """Get status of a pending action."""
    action = safeguards.get_pending_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return action


OPENCLAW_CAPABILITIES = [
    OpenClawCapability(
        action="daikin.power",
        description="Turn Daikin climate control on or off",
        parameters={"on": {"type": "boolean", "description": "True to turn on, False to turn off"}},
        requires_confirmation=True,
        safeguards=["confirmation_required", "rate_limited"],
    ),
    OpenClawCapability(
        action="daikin.temperature",
        description="Set target room temperature. BLOCKED when weather regulation is active — use daikin.lwt_offset instead.",
        parameters={
            "temperature": {"type": "number", "min": 15, "max": 30, "description": "Target temperature in Celsius"},
            "mode": {"type": "string", "optional": True, "description": "Operation mode (uses current if not specified)"},
        },
        requires_confirmation=False,
        safeguards=["range_validation", "rate_limited", "weather_regulation_check"],
    ),
    OpenClawCapability(
        action="daikin.lwt_offset",
        description="Set leaving water temperature offset",
        parameters={
            "offset": {"type": "number", "min": -10, "max": 10, "description": "LWT offset value"},
            "mode": {"type": "string", "optional": True, "description": "Operation mode"},
        },
        requires_confirmation=False,
        safeguards=["range_validation", "rate_limited"],
    ),
    OpenClawCapability(
        action="daikin.mode",
        description="Set operation mode (heating/cooling/auto)",
        parameters={"mode": {"type": "string", "enum": ["heating", "cooling", "auto", "fan_only", "dry"]}},
        requires_confirmation=False,
        safeguards=["enum_validation", "rate_limited"],
    ),
    OpenClawCapability(
        action="daikin.tank_temperature",
        description="Set DHW tank target temperature",
        parameters={"temperature": {"type": "number", "min": 30, "max": 60, "description": "Tank target in Celsius"}},
        requires_confirmation=False,
        safeguards=["range_validation", "rate_limited"],
    ),
    OpenClawCapability(
        action="daikin.tank_power",
        description="Turn DHW tank on or off",
        parameters={"on": {"type": "boolean", "description": "True to turn on, False to turn off"}},
        requires_confirmation=True,
        safeguards=["confirmation_required", "rate_limited"],
    ),
    OpenClawCapability(
        action="foxess.mode",
        description="Set inverter work mode",
        parameters={"mode": {"type": "string", "enum": ["Self Use", "Feed-in Priority", "Back Up", "Force charge", "Force discharge"]}},
        requires_confirmation=True,
        safeguards=["confirmation_required", "enum_validation", "rate_limited"],
    ),
    OpenClawCapability(
        action="foxess.charge_period",
        description="Set battery charge schedule",
        parameters={
            "start_time": {"type": "string", "pattern": "HH:MM", "description": "Start time"},
            "end_time": {"type": "string", "pattern": "HH:MM", "description": "End time"},
            "target_soc": {"type": "integer", "min": 10, "max": 100, "description": "Target state of charge (%)"},
            "period_index": {"type": "integer", "min": 0, "max": 1, "default": 0, "description": "Period slot (0 or 1)"},
        },
        requires_confirmation=False,
        safeguards=["range_validation", "rate_limited"],
    ),
]


@app.get("/api/v1/openclaw/capabilities", response_model=OpenClawCapabilitiesResponse)
async def openclaw_capabilities():
    """List all available actions and their parameters for OpenClaw integration."""
    return OpenClawCapabilitiesResponse(capabilities=OPENCLAW_CAPABILITIES)


@app.post("/api/v1/openclaw/execute", response_model=PendingActionResponse | ActionResult)
async def openclaw_execute(req: OpenClawExecuteRequest):
    """
    Execute an action via OpenClaw.
    
    When OPENCLAW_READ_ONLY=true (default), returns 403 so the agent can only recommend;
    apply changes via the dashboard or CLI.
    
    For actions requiring confirmation:
    1. First call returns a pending action with confirmation_token
    2. Second call with confirmation_token executes the action
    """
    if config.OPENCLAW_READ_ONLY:
        raise HTTPException(
            status_code=403,
            detail="OpenClaw is in recommendation-only mode. Apply changes via the dashboard or CLI. Set OPENCLAW_READ_ONLY=false to allow execution.",
        )
    action_type = req.action.value
    params = req.parameters

    if req.confirmation_token:
        action = safeguards.get_pending_action(req.confirmation_token)
        if action is None:
            raise HTTPException(status_code=404, detail="Confirmation token not found or expired")
        if action.status == ActionStatus.EXPIRED:
            raise HTTPException(status_code=410, detail="Confirmation token has expired")
        if action.status != ActionStatus.PENDING:
            raise HTTPException(status_code=409, detail=f"Action already {action.status.value}")
        
        safeguards.confirm_action(req.confirmation_token)
        
        try:
            result = await _execute_action(action.action_type, action.parameters)
            safeguards.mark_executed(req.confirmation_token)
            safeguards.record_action_time(action.action_type)
            safeguards.audit_log(action.action_type, action.parameters, "openclaw", True, result.message)
            result.action_id = req.confirmation_token
            return result
        except Exception as e:
            safeguards.audit_log(action.action_type, action.parameters, "openclaw", False, str(e))
            raise
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    if safeguards.requires_confirmation(action_type):
        action = safeguards.create_pending_action(
            action_type=action_type,
            description=_get_action_description(action_type, params),
            parameters=params,
        )
        return PendingActionResponse(
            action=action,
            message=f"Confirmation required: {action.description}. Re-send with confirmation_token='{action.action_id}' to execute.",
        )
    
    try:
        result = await _execute_action(action_type, params)
        safeguards.record_action_time(action_type)
        safeguards.audit_log(action_type, params, "openclaw", True, result.message)
        return result
    except Exception as e:
        safeguards.audit_log(action_type, params, "openclaw", False, str(e))
        raise


def _get_action_description(action_type: str, params: dict) -> str:
    """Generate human-readable description for an action."""
    descriptions = {
        "daikin.power": lambda p: f"Turn Daikin {'ON' if p.get('on') else 'OFF'}",
        "daikin.temperature": lambda p: f"Set temperature to {p.get('temperature')}°C",
        "daikin.lwt_offset": lambda p: f"Set LWT offset to {p.get('offset'):+g}",
        "daikin.mode": lambda p: f"Set mode to {p.get('mode')}",
        "daikin.tank_temperature": lambda p: f"Set tank temperature to {p.get('temperature')}°C",
        "daikin.tank_power": lambda p: f"Turn DHW tank {'ON' if p.get('on') else 'OFF'}",
        "foxess.mode": lambda p: f"Set inverter mode to '{p.get('mode')}'",
        "foxess.charge_period": lambda p: f"Set charge period {p.get('start_time')}-{p.get('end_time')} to {p.get('target_soc')}%",
    }
    return descriptions.get(action_type, lambda p: action_type)(params)


async def _execute_action(action_type: str, params: dict) -> ActionResult:
    """Execute an action and return the result."""
    try:
        if action_type == "daikin.power":
            client = get_daikin_client()
            devices = client.get_devices()
            if not devices:
                raise HTTPException(status_code=404, detail="No Daikin devices found")
            for dev in devices:
                client.set_power(dev, params["on"])
            return ActionResult(success=True, message=f"Daikin turned {'ON' if params['on'] else 'OFF'}")
        
        elif action_type == "daikin.temperature":
            client = get_daikin_client()
            devices = client.get_devices()
            if not devices:
                raise HTTPException(status_code=404, detail="No Daikin devices found")
            temp = params["temperature"]
            if temp < 15 or temp > 30:
                raise HTTPException(status_code=400, detail="Temperature must be between 15 and 30°C")
            for dev in devices:
                if dev.weather_regulation_enabled:
                    raise HTTPException(
                        status_code=409,
                        detail="Cannot set room temperature while weather regulation is active. "
                               "Use LWT offset instead, or disable weather regulation first.",
                    )
                mode = params.get("mode") or dev.operation_mode
                client.set_temperature(dev, temp, mode)
            return ActionResult(success=True, message=f"Temperature set to {temp}°C")
        
        elif action_type == "daikin.lwt_offset":
            client = get_daikin_client()
            devices = client.get_devices()
            if not devices:
                raise HTTPException(status_code=404, detail="No Daikin devices found")
            offset = params["offset"]
            if offset < -10 or offset > 10:
                raise HTTPException(status_code=400, detail="LWT offset must be between -10 and +10")
            for dev in devices:
                mode = params.get("mode") or dev.operation_mode
                client.set_lwt_offset(dev, offset, mode)
            return ActionResult(success=True, message=f"LWT offset set to {offset:+g}")
        
        elif action_type == "daikin.mode":
            client = get_daikin_client()
            devices = client.get_devices()
            if not devices:
                raise HTTPException(status_code=404, detail="No Daikin devices found")
            mode = params["mode"]
            for dev in devices:
                client.set_operation_mode(dev, mode)
            return ActionResult(success=True, message=f"Mode set to {mode}")
        
        elif action_type == "daikin.tank_temperature":
            client = get_daikin_client()
            devices = client.get_devices()
            if not devices:
                raise HTTPException(status_code=404, detail="No Daikin devices found")
            temp = params["temperature"]
            if temp < 30 or temp > 60:
                raise HTTPException(status_code=400, detail="Tank temperature must be between 30 and 60°C")
            for dev in devices:
                if dev.tank_target is not None:
                    client.set_tank_temperature(dev, temp)
            return ActionResult(success=True, message=f"Tank temperature set to {temp}°C")
        
        elif action_type == "daikin.tank_power":
            client = get_daikin_client()
            devices = client.get_devices()
            if not devices:
                raise HTTPException(status_code=404, detail="No Daikin devices found")
            for dev in devices:
                client.set_tank_power(dev, params["on"])
            return ActionResult(success=True, message=f"DHW tank turned {'ON' if params['on'] else 'OFF'}")
        
        elif action_type == "foxess.mode":
            client = get_foxess_client()
            mode = params["mode"]
            client.set_work_mode(mode)
            return ActionResult(success=True, message=f"Work mode set to: {mode}")
        
        elif action_type == "foxess.charge_period":
            client = get_foxess_client()
            cp = ChargePeriod(
                start_time=params["start_time"],
                end_time=params["end_time"],
                target_soc=params["target_soc"],
                enable=True,
            )
            period_index = params.get("period_index", 0)
            client.set_charge_period(period_index, cp)
            return ActionResult(
                success=True,
                message=f"Charge period {period_index + 1} set: {params['start_time']}–{params['end_time']}, target SoC {params['target_soc']}%",
            )
        
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action_type}")
    
    except (DaikinError, FoxESSError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Assistant Endpoints ─────────────────────────────────────────────────────

def _get_assistant_context() -> tuple[list[dict], Optional[dict], Optional[dict]]:
    """Return (daikin_status_list, foxess_status_dict, tariff_dict) for assistant context."""
    daikin_list: list[dict] = []
    foxess_status: Optional[dict] = None
    tariff: Optional[dict] = None
    if config.MANUAL_TARIFF_IMPORT_PENCE > 0 or config.MANUAL_TARIFF_EXPORT_PENCE > 0:
        tariff = {
            "import_rate": config.MANUAL_TARIFF_IMPORT_PENCE,
            "export_rate": config.MANUAL_TARIFF_EXPORT_PENCE,
            "tariff_name": "Manual",
        }
    try:
        client = get_daikin_client()
        devices = client.get_devices()
        for dev in devices or []:
            s = client.get_status(dev)
            daikin_list.append({
                "device_id": dev.id,
                "device_name": dev.model or s.device_name or dev.id,
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
            })
    except Exception:
        pass
    if config.FOXESS_API_KEY or (config.FOXESS_USERNAME and config.FOXESS_PASSWORD):
        try:
            d = get_cached_realtime()
            foxess_status = {
                "soc": d.soc,
                "solar_power": d.solar_power,
                "grid_power": d.grid_power,
                "battery_power": d.battery_power,
                "load_power": d.load_power,
                "work_mode": d.work_mode,
            }
        except Exception as e:
            logging.getLogger(__name__).warning("Fox ESS unavailable for assistant context: %s", e)
    return daikin_list, foxess_status, tariff


@app.post("/api/v1/assistant/recommend", response_model=AssistantRecommendResponse)
async def assistant_recommend(req: AssistantRecommendRequest):
    """Get optimization suggestions from the AI assistant (comfort vs cost)."""
    daikin_list, foxess_status, tariff = _get_assistant_context()
    context = build_context(daikin_list, foxess_status, tariff)
    reply, actions = get_suggestions(
        context,
        req.preference.value,
        req.message,
    )
    return AssistantRecommendResponse(
        reply=reply,
        suggested_actions=[
            SuggestedActionSchema(action=a.action, parameters=a.parameters, reason=a.reason)
            for a in actions
        ],
    )


@app.post("/api/v1/assistant/apply", response_model=AssistantApplyResponse)
async def assistant_apply(req: AssistantApplyRequest):
    """Apply suggested actions; returns confirmation tokens for actions that require confirm."""
    validated = validate_suggested_actions([
        SuggestedAction(a.action, a.parameters, None) for a in req.actions
    ])
    results: list[AssistantApplyResultItem] = []
    for action in validated:
        action_type = action.action
        params = action.parameters
        if safeguards.requires_confirmation(action_type):
            allowed, wait_time = safeguards.check_rate_limit(action_type)
            if not allowed:
                results.append(AssistantApplyResultItem(
                    action_type=action_type,
                    success=False,
                    message=f"Rate limited. Try again in {wait_time:.1f}s.",
                ))
                continue
            pending = safeguards.create_pending_action(
                action_type=action_type,
                description=_get_action_description(action_type, params),
                parameters=params,
            )
            results.append(AssistantApplyResultItem(
                action_type=action_type,
                success=False,
                message=pending.description,
                requires_confirmation=True,
                confirmation_token=pending.action_id,
                action_id=pending.action_id,
            ))
        else:
            try:
                allowed, wait_time = safeguards.check_rate_limit(action_type)
                if not allowed:
                    results.append(AssistantApplyResultItem(
                        action_type=action_type,
                        success=False,
                        message=f"Rate limited. Try again in {wait_time:.1f}s.",
                    ))
                    continue
                result = await _execute_action(action_type, params)
                safeguards.record_action_time(action_type)
                safeguards.audit_log(action_type, params, "assistant", True, result.message)
                results.append(AssistantApplyResultItem(
                    action_type=action_type,
                    success=result.success,
                    message=result.message,
                ))
            except HTTPException as e:
                results.append(AssistantApplyResultItem(
                    action_type=action_type,
                    success=False,
                    message=e.detail if isinstance(e.detail, str) else str(e.detail),
                ))
            except Exception as e:
                results.append(AssistantApplyResultItem(
                    action_type=action_type,
                    success=False,
                    message=str(e),
                ))
    return AssistantApplyResponse(results=results)


# ── Energy Provider Endpoints (Stubs) ────────────────────────────────────────

def _is_manual_tariff_configured() -> bool:
    return config.MANUAL_TARIFF_IMPORT_PENCE > 0 or config.MANUAL_TARIFF_EXPORT_PENCE > 0


ENERGY_PROVIDERS = [
    EnergyProviderInfo(
        provider=EnergyProviderEnum.OCTOPUS,
        name="Octopus Energy",
        is_configured=bool(config.OCTOPUS_API_KEY),
        description="Agile, Go, Tracker, and fixed tariffs with half-hourly pricing data",
    ),
    EnergyProviderInfo(
        provider=EnergyProviderEnum.BRITISH_GAS,
        name="British Gas",
        is_configured=bool(config.BRITISH_GAS_API_KEY),
        description="Fixed and variable tariffs, SEG export payments",
    ),
    EnergyProviderInfo(
        provider=EnergyProviderEnum.MANUAL,
        name="Manual Entry",
        is_configured=_is_manual_tariff_configured(),
        description="Manually enter your tariff rates for cost tracking",
    ),
]


@app.get("/api/v1/energy/providers", response_model=EnergyProvidersResponse)
async def energy_providers():
    """List available energy providers and their configuration status."""
    configured = sum(1 for p in ENERGY_PROVIDERS if p.is_configured)
    return EnergyProvidersResponse(
        providers=ENERGY_PROVIDERS,
        configured_count=configured,
    )


@app.get("/api/v1/energy/tariff", response_model=TariffResponse)
async def energy_tariff():
    """Get current tariff information from configured energy provider.
    
    Uses manual tariff (MANUAL_TARIFF_IMPORT_PENCE / MANUAL_TARIFF_EXPORT_PENCE) when set.
    Returns 503 if no provider and no manual tariff configured.
    """
    if _is_manual_tariff_configured():
        return TariffResponse(
            provider=EnergyProviderEnum.MANUAL,
            tariff_name="Manual",
            tariff_type=TariffTypeEnum.FIXED,
            import_rate=config.MANUAL_TARIFF_IMPORT_PENCE,
            export_rate=config.MANUAL_TARIFF_EXPORT_PENCE if config.MANUAL_TARIFF_EXPORT_PENCE > 0 else None,
        )
    configured = [p for p in ENERGY_PROVIDERS if p.is_configured]
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="No energy provider configured. Set OCTOPUS_API_KEY, BRITISH_GAS_API_KEY, or MANUAL_TARIFF_IMPORT_PENCE in .env"
        )
    raise HTTPException(
        status_code=501,
        detail="Energy provider integration not yet implemented. Coming soon!"
    )


@app.get("/api/v1/energy/usage", response_model=EnergyUsageResponse)
async def energy_usage():
    """Get energy usage and cost summary.
    
    Returns 503 if no energy provider is configured.
    """
    configured = [p for p in ENERGY_PROVIDERS if p.is_configured]
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="No energy provider configured. Set OCTOPUS_API_KEY or BRITISH_GAS_API_KEY in .env"
        )
    raise HTTPException(
        status_code=501,
        detail="Energy provider integration not yet implemented. Coming soon!"
    )


def _foxess_configured() -> bool:
    return bool(config.FOXESS_API_KEY or (config.FOXESS_USERNAME and config.FOXESS_PASSWORD))


@app.get("/api/v1/energy/monthly", response_model=MonthlyInsightsResponse)
async def energy_monthly(month: str):
    """Get monthly energy, cost, heating estimate and gas comparison.
    
    Query: month=YYYY-MM. Returns 503 if Fox ESS not configured; 400 if month format invalid.
    """
    if not _foxess_configured():
        raise HTTPException(
            status_code=503,
            detail="Fox ESS not configured. Set FOXESS_API_KEY or FOXESS_USERNAME+FOXESS_PASSWORD and FOXESS_DEVICE_SN.",
        )
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(status_code=400, detail="Use month=YYYY-MM (e.g. 2025-02)")
    try:
        year = int(month[:4])
        month_num = int(month[5:7])
        if not (1 <= month_num <= 12):
            raise ValueError("month out of range")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        insights = get_monthly_insights(year, month_num)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("Monthly insights Fox ESS error: %s", e, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Fox ESS error: " + str(e),
        )
    if insights is None:
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch monthly data from Fox ESS. Try again later.",
        )
    cost = insights.cost
    return MonthlyInsightsResponse(
        energy=MonthlyEnergySummaryResponse(
            year=insights.energy.year,
            month=insights.energy.month,
            month_str=insights.energy.month_str,
            import_kwh=insights.energy.import_kwh,
            export_kwh=insights.energy.export_kwh,
            solar_kwh=insights.energy.solar_kwh,
            load_kwh=insights.energy.load_kwh,
            charge_kwh=insights.energy.charge_kwh,
            discharge_kwh=insights.energy.discharge_kwh,
        ),
        cost=MonthlyCostSummaryResponse(
            import_cost_pence=cost.import_cost_pence,
            export_earnings_pence=cost.export_earnings_pence,
            standing_charge_pence=cost.standing_charge_pence,
            net_cost_pence=cost.net_cost_pence,
            net_cost_pounds=cost.net_cost_pounds,
            import_cost_pounds=cost.import_cost_pounds,
            export_earnings_pounds=cost.export_earnings_pounds,
        ),
        heating_estimate_kwh=insights.heating_estimate_kwh,
        heating_estimate_cost_pence=insights.heating_estimate_cost_pence,
        equivalent_gas_cost_pence=insights.equivalent_gas_cost_pence,
        equivalent_gas_cost_pounds=insights.equivalent_gas_cost_pounds,
        gas_comparison_ahead_pounds=insights.gas_comparison_ahead_pounds,
    )


@app.get("/api/v1/energy/period", response_model=PeriodInsightsResponse)
async def energy_period(
    period: str,
    date: Optional[str] = None,
    month: Optional[str] = None,
    year: Optional[int] = None,
):
    """Get energy insights + chart_data for day, week, month, or year.
    period=day|week|month|year. For day/week use date=YYYY-MM-DD; for month use month=YYYY-MM; for year use year=YYYY.
    """
    if not _foxess_configured():
        raise HTTPException(
            status_code=503,
            detail="Fox ESS not configured. Set FOXESS_API_KEY or FOXESS_USERNAME+FOXESS_PASSWORD and FOXESS_DEVICE_SN.",
        )
    if period not in ("day", "week", "month", "year"):
        raise HTTPException(status_code=400, detail="period must be day, week, month, or year")
    if period == "month" and not month:
        raise HTTPException(status_code=400, detail="Use month=YYYY-MM for period=month")
    if period == "year" and year is None:
        raise HTTPException(status_code=400, detail="Use year=YYYY for period=year")
    if period in ("day", "week") and not date:
        raise HTTPException(status_code=400, detail="Use date=YYYY-MM-DD for period=day or period=week")
    if period == "month" and (len(month) != 7 or month[4] != "-"):
        raise HTTPException(status_code=400, detail="Use month=YYYY-MM")
    if period in ("day", "week") and (len(date) != 10 or date[4] != "-" or date[7] != "-"):
        raise HTTPException(status_code=400, detail="Use date=YYYY-MM-DD")
    try:
        out = get_period_insights(period, date_str=date, month_str=month, year=year)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("Period insights Fox ESS error: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail="Fox ESS error: " + str(e))
    if out is None:
        raise HTTPException(status_code=502, detail="Failed to fetch data from Fox ESS.")
    ins = out.insights
    cost = ins.cost
    return PeriodInsightsResponse(
        period=out.period,
        period_label=out.period_label,
        energy=MonthlyEnergySummaryResponse(
            year=ins.energy.year,
            month=ins.energy.month,
            month_str=ins.energy.month_str,
            import_kwh=ins.energy.import_kwh,
            export_kwh=ins.energy.export_kwh,
            solar_kwh=ins.energy.solar_kwh,
            load_kwh=ins.energy.load_kwh,
            charge_kwh=ins.energy.charge_kwh,
            discharge_kwh=ins.energy.discharge_kwh,
        ),
        cost=MonthlyCostSummaryResponse(
            import_cost_pence=cost.import_cost_pence,
            export_earnings_pence=cost.export_earnings_pence,
            standing_charge_pence=cost.standing_charge_pence,
            net_cost_pence=cost.net_cost_pence,
            net_cost_pounds=cost.net_cost_pounds,
            import_cost_pounds=cost.import_cost_pounds,
            export_earnings_pounds=cost.export_earnings_pounds,
        ),
        heating_estimate_kwh=ins.heating_estimate_kwh,
        heating_estimate_cost_pence=ins.heating_estimate_cost_pence,
        equivalent_gas_cost_pence=ins.equivalent_gas_cost_pence,
        equivalent_gas_cost_pounds=ins.equivalent_gas_cost_pounds,
        gas_comparison_ahead_pounds=ins.gas_comparison_ahead_pounds,
        chart_data=[ChartDataPoint(**p) for p in out.chart_data],
        heating_analytics=(
            HeatingAnalyticsResponse(
                heating_percent_of_cost=out.heating_analytics.heating_percent_of_cost,
                heating_percent_of_consumption=out.heating_analytics.heating_percent_of_consumption,
                avg_outdoor_temp_c=out.heating_analytics.avg_outdoor_temp_c,
                degree_days=out.heating_analytics.degree_days,
                cost_per_degree_day_pounds=out.heating_analytics.cost_per_degree_day_pounds,
                heating_kwh_per_degree_day=out.heating_analytics.heating_kwh_per_degree_day,
                temp_bands=[TempBandSummaryResponse(**b) for b in out.heating_analytics.temp_bands],
            )
            if out.heating_analytics else None
        ),
    )


def _build_report_summary(
    period_label: str,
    energy: MonthlyEnergySummaryResponse,
    cost: MonthlyCostSummaryResponse,
    equivalent_gas_cost_pounds: Optional[float],
    gas_comparison_ahead_pounds: Optional[float],
) -> str:
    """Build short narrative for OpenClaw from report data."""
    parts = [
        f"{period_label}: imported {energy.import_kwh:.1f} kWh, exported {energy.export_kwh:.1f} kWh. "
        f"Net cost: £{cost.net_cost_pounds:.2f} (import £{cost.import_cost_pounds:.2f}, export earnings £{cost.export_earnings_pounds:.2f}). "
        f"Production {energy.solar_kwh:.1f} kWh solar, consumption {energy.load_kwh:.1f} kWh."
    ]
    if equivalent_gas_cost_pounds is not None:
        parts.append(f" Equivalent gas cost would be about £{equivalent_gas_cost_pounds:.2f}.")
        if gas_comparison_ahead_pounds is not None:
            if gas_comparison_ahead_pounds >= 0:
                parts.append(f" You are £{gas_comparison_ahead_pounds:.2f} ahead with solar + heat pump.")
            else:
                parts.append(f" Gas would have been £{abs(gas_comparison_ahead_pounds):.2f} cheaper.")
    return "".join(parts)


@app.get("/api/v1/energy/report", response_model=EnergyReportResponse)
async def energy_report(
    period: Optional[str] = None,
    date: Optional[str] = None,
    month: Optional[str] = None,
    year: Optional[int] = None,
):
    """Full data report for OpenClaw and dashboards: energy, cost, chart_data, heating/gas, plus a spoken summary.

    Returns the same structure as GET /energy/period plus a 'summary' field for TTS/chat.
    Default: current month (no query params). Optional: period=day|week|month|year with date=YYYY-MM-DD,
    month=YYYY-MM, or year=YYYY as for /energy/period.
    """
    if not _foxess_configured():
        raise HTTPException(
            status_code=503,
            detail="Fox ESS not configured. Set FOXESS_API_KEY or FOXESS_USERNAME+FOXESS_PASSWORD and FOXESS_DEVICE_SN.",
        )
    from datetime import date as date_type
    today = date_type.today()
    if period is None and date is None and month is None and year is None:
        period = "month"
        month = f"{today.year:04d}-{today.month:02d}"
    if period is None:
        period = "month"
        if month is None and year is not None:
            month = f"{year:04d}-01"
        elif month is None:
            month = f"{today.year:04d}-{today.month:02d}"
    if period not in ("day", "week", "month", "year"):
        raise HTTPException(status_code=400, detail="period must be day, week, month, or year")
    if period == "month" and not month:
        raise HTTPException(status_code=400, detail="Use month=YYYY-MM for period=month")
    if period == "year" and year is None:
        raise HTTPException(status_code=400, detail="Use year=YYYY for period=year")
    if period in ("day", "week") and not date:
        raise HTTPException(status_code=400, detail="Use date=YYYY-MM-DD for period=day or period=week")
    if period == "month" and month and (len(month) != 7 or month[4] != "-"):
        raise HTTPException(status_code=400, detail="Use month=YYYY-MM")
    if period in ("day", "week") and date and (len(date) != 10 or date[4] != "-" or date[7] != "-"):
        raise HTTPException(status_code=400, detail="Use date=YYYY-MM-DD")
    try:
        out = get_period_insights(period, date_str=date, month_str=month, year=year)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("Period insights Fox ESS error: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail="Fox ESS error: " + str(e))
    if out is None:
        raise HTTPException(status_code=502, detail="Failed to fetch data from Fox ESS.")
    ins = out.insights
    cost = ins.cost
    resp = PeriodInsightsResponse(
        period=out.period,
        period_label=out.period_label,
        energy=MonthlyEnergySummaryResponse(
            year=ins.energy.year,
            month=ins.energy.month,
            month_str=ins.energy.month_str,
            import_kwh=ins.energy.import_kwh,
            export_kwh=ins.energy.export_kwh,
            solar_kwh=ins.energy.solar_kwh,
            load_kwh=ins.energy.load_kwh,
            charge_kwh=ins.energy.charge_kwh,
            discharge_kwh=ins.energy.discharge_kwh,
        ),
        cost=MonthlyCostSummaryResponse(
            import_cost_pence=cost.import_cost_pence,
            export_earnings_pence=cost.export_earnings_pence,
            standing_charge_pence=cost.standing_charge_pence,
            net_cost_pence=cost.net_cost_pence,
            net_cost_pounds=cost.net_cost_pounds,
            import_cost_pounds=cost.import_cost_pounds,
            export_earnings_pounds=cost.export_earnings_pounds,
        ),
        heating_estimate_kwh=ins.heating_estimate_kwh,
        heating_estimate_cost_pence=ins.heating_estimate_cost_pence,
        equivalent_gas_cost_pence=ins.equivalent_gas_cost_pence,
        equivalent_gas_cost_pounds=ins.equivalent_gas_cost_pounds,
        gas_comparison_ahead_pounds=ins.gas_comparison_ahead_pounds,
        chart_data=[ChartDataPoint(**p) for p in out.chart_data],
        heating_analytics=(
            HeatingAnalyticsResponse(
                heating_percent_of_cost=out.heating_analytics.heating_percent_of_cost,
                heating_percent_of_consumption=out.heating_analytics.heating_percent_of_consumption,
                avg_outdoor_temp_c=out.heating_analytics.avg_outdoor_temp_c,
                degree_days=out.heating_analytics.degree_days,
                cost_per_degree_day_pounds=out.heating_analytics.cost_per_degree_day_pounds,
                heating_kwh_per_degree_day=out.heating_analytics.heating_kwh_per_degree_day,
                temp_bands=[TempBandSummaryResponse(**b) for b in out.heating_analytics.temp_bands],
            )
            if out.heating_analytics else None
        ),
    )
    summary = _build_report_summary(
        resp.period_label,
        resp.energy,
        resp.cost,
        resp.equivalent_gas_cost_pounds,
        resp.gas_comparison_ahead_pounds,
    )
    return EnergyReportResponse(**resp.model_dump(), summary=summary)


@app.get("/api/v1/energy/insights", response_model=EnergyInsightsTextResponse)
async def energy_insights():
    """Short narrative summary for OpenClaw: this month cost and equivalent gas.
    
    Returns 503 if Fox ESS not configured.
    """
    if not _foxess_configured():
        raise HTTPException(
            status_code=503,
            detail="Fox ESS not configured. Set FOXESS_API_KEY or FOXESS_USERNAME+FOXESS_PASSWORD and FOXESS_DEVICE_SN.",
        )
    from datetime import date
    today = date.today()
    insights = get_monthly_insights(today.year, today.month)
    if insights is None:
        return EnergyInsightsTextResponse(
            summary="Monthly data is temporarily unavailable. Try again later."
        )
    cost = insights.cost
    parts = [
        f"This month ({insights.energy.month_str}): imported {insights.energy.import_kwh:.1f} kWh, "
        f"exported {insights.energy.export_kwh:.1f} kWh. "
        f"Net cost: £{cost.net_cost_pounds:.2f} (import £{cost.import_cost_pounds:.2f}, export earnings £{cost.export_earnings_pounds:.2f})."
    ]
    if insights.equivalent_gas_cost_pounds is not None:
        parts.append(
            f" Equivalent gas cost this month would be about £{insights.equivalent_gas_cost_pounds:.2f}."
        )
        if insights.gas_comparison_ahead_pounds is not None:
            if insights.gas_comparison_ahead_pounds >= 0:
                parts.append(f" You are £{insights.gas_comparison_ahead_pounds:.2f} ahead with solar + heat pump.")
            else:
                parts.append(f" Gas would have been £{abs(insights.gas_comparison_ahead_pounds):.2f} cheaper this month.")
    return EnergyInsightsTextResponse(summary="".join(parts))


# ── Agile Scheduler (Daikin LWT by Octopus price) ─────────────────────────────

@app.get("/api/v1/scheduler/status", response_model=SchedulerStatusResponse)
async def scheduler_status():
    """Current Agile price, next cheap window, planned ASHP LWT adjustment, paused state."""
    raw = get_scheduler_status()
    return SchedulerStatusResponse(
        enabled=raw["enabled"],
        paused=raw["paused"],
        current_price_pence=raw.get("current_price_pence"),
        next_cheap_from=raw.get("next_cheap_from"),
        next_cheap_to=raw.get("next_cheap_to"),
        planned_lwt_adjustment=raw.get("planned_lwt_adjustment", 0.0),
        tariff_code=raw.get("tariff_code"),
    )


@app.post("/api/v1/scheduler/pause")
async def scheduler_pause():
    """Pause the Agile-based Daikin scheduler (no more automatic LWT changes)."""
    pause_scheduler()
    return {"status": "paused"}


@app.post("/api/v1/scheduler/resume")
async def scheduler_resume():
    """Resume the Agile-based Daikin scheduler."""
    resume_scheduler()
    return {"status": "resumed"}


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the API server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    run_server(host, port)
