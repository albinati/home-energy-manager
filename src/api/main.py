"""FastAPI application: HTTP interface for the Home Energy Manager **planning brain**.

The service ingests tariffs, fuses weather and execution history, runs the bulletproof
optimizer (`run_optimizer`), and keeps schedules aligned via the heartbeat. This module
exposes REST for dashboards, scripts, and **OpenClaw** (``HOME_ENERGY_API_URL``).
Optional MCP (``python -m src.mcp_server``) is another client interface to the same backend.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import config
from ..daikin import service as daikin_service
from ..daikin.client import DaikinClient, DaikinError
from ..energy.monthly import get_monthly_insights, get_period_insights
from ..foxess.client import FoxESSClient, FoxESSError
from ..foxess.models import ChargePeriod
from ..foxess.service import get_cached_realtime, get_refresh_stats, get_refresh_stats_extended
from ..state_machine import apply_safe_defaults, recover_on_boot

logger = logging.getLogger(__name__)

from ..agile_cache import get_agile_cache, refresh_agile_rates
from ..assistant import SuggestedAction, build_context, get_suggestions, validate_suggested_actions
from ..config_snapshots import list_snapshots, restore_snapshot, rollback_latest, save_snapshot
from ..scheduler.optimizer import run_optimizer
from ..scheduler.runner import (
    get_scheduler_status,
    pause_scheduler,
    reregister_cron_jobs,
    resume_scheduler,
    start_background_scheduler,
    stop_background_scheduler,
)
from . import safeguards
from .models import (
    ActionResult,
    ActionStatus,
    ApprovePlanRequest,
    ApprovePlanResponse,
    AssistantApplyRequest,
    AssistantApplyResponse,
    AssistantApplyResultItem,
    AssistantRecommendRequest,
    AssistantRecommendResponse,
    ChargePeriodRequest,
    ChartDataPoint,
    ConfirmRequest,
    DaikinStatusResponse,
    EnergyInsightsTextResponse,
    EnergyReportResponse,
    FoxESSModeRequest,
    FoxESSStatusResponse,
    HeatingAnalyticsResponse,
    ListAvailableTariffsResponse,
    ListSnapshotsResponse,
    LWTOffsetRequest,
    ModeRequest,
    MonthlyCostSummaryResponse,
    MonthlyEnergySummaryResponse,
    MonthlyInsightsResponse,
    OctopusAccountResponse,
    OctopusAutoDetectResponse,
    OctopusConsumptionResponse,
    OctopusConsumptionSlotResponse,
    OctopusCurrentTariffResponse,
    OpenClawCapabilitiesResponse,
    OpenClawCapability,
    OpenClawExecuteRequest,
    OptimizationDispatchPreviewResponse,
    OptimizationStatusExtendedResponse,
    PendingActionResponse,
    PeriodInsightsResponse,
    PowerRequest,
    ProposePlanResponse,
    RejectPlanRequest,
    RollbackResponse,
    SchedulerStatusResponse,
    SetAutoApproveRequest,
    SetAutoApproveResponse,
    SetOperationModeRequest,
    SetOperationModeResponse,
    SetOptimizerBackendRequest,
    SetOptimizerBackendResponse,
    SetPresetRequest,
    SetPresetResponse,
    SnapshotSummary,
    SuggestedActionSchema,
    TankPowerRequest,
    TankTemperatureRequest,
    TariffCompareRequest,
    TariffDashboardRequest,
    TariffDashboardResponse,
    TariffPeriodCosts,
    TariffPolicyResponse,
    TariffProductResponse,
    TariffRatesResponse,
    TariffRecommendationResponse,
    TariffSimulationResultResponse,
    TariffTotalRow,
    TempBandSummaryResponse,
    TemperatureRequest,
)
from .routers import energy_providers as energy_providers_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(db.init_db)
    fox = None
    daikin = None
    try:
        fox = get_foxess_client()
    except Exception:
        pass
    try:
        daikin = get_daikin_client()
    except Exception:
        pass
    await asyncio.to_thread(recover_on_boot, fox, daikin)
    start_background_scheduler()
    yield
    try:
        if fox is not None and daikin is not None:
            await asyncio.to_thread(apply_safe_defaults, fox, daikin)
    except Exception:
        logger.warning("Safe defaults on shutdown failed", exc_info=True)
    stop_background_scheduler()
    safeguards.cleanup_expired_actions()


app = FastAPI(
    title="Home Energy Manager API",
    description="REST API for controlling Daikin heat pump and Fox ESS battery system",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(energy_providers_router.router)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def get_daikin_client() -> DaikinClient:
    """Return a DaikinClient for write operations only. For reads, use daikin_service."""
    return DaikinClient()


def get_foxess_client() -> FoxESSClient:
    return FoxESSClient(**config.foxess_client_kwargs())


def _require_active_daikin() -> None:
    """Raise 409 PassiveModeLocked when DAIKIN_CONTROL_MODE=passive.

    Called at the top of every Daikin write route so manual API calls cannot
    bypass the passive-mode guarantee. Flip DAIKIN_CONTROL_MODE=active first.
    """
    if config.DAIKIN_CONTROL_MODE == "passive":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "PassiveModeLocked",
                "message": "DAIKIN_CONTROL_MODE=passive — set to 'active' to allow writes",
            },
        )


@app.get("/", response_class=HTMLResponse)
async def web_cockpit(request: Request):
    """v10.1 cockpit (mobile-first; simulate-first action paradigm)."""
    return templates.TemplateResponse(request, "cockpit.html", {"active_page": "cockpit"})


@app.get("/insights", response_class=HTMLResponse)
async def web_insights(request: Request):
    return templates.TemplateResponse(request, "insights.html", {"active_page": "insights"})


@app.get("/plan", response_class=HTMLResponse)
async def web_plan(request: Request):
    return templates.TemplateResponse(request, "plan.html", {"active_page": "plan"})


@app.get("/settings", response_class=HTMLResponse)
async def web_settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", {"active_page": "settings"})


@app.get("/legacy", response_class=HTMLResponse)
async def web_dashboard_legacy(request: Request):
    """v9 dashboard, kept for one week as a fallback during the v10.1 cockpit rollout."""
    daikin_status = None
    foxess_status = None
    daikin_error = None
    foxess_error = None

    try:
        cached = daikin_service.get_cached_devices(allow_refresh=False, actor="dashboard")
        devices = cached.devices
        if devices:
            dev = devices[0]
            client = get_daikin_client()
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
                "cache_stale": cached.stale,
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
                from datetime import datetime
                updated_at_str = datetime.fromtimestamp(last_ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
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
        request,
        "dashboard_legacy.html",
        {
            "daikin": daikin_status,
            "foxess": foxess_status,
            "daikin_error": daikin_error,
            "foxess_error": foxess_error,
        },
    )


@app.get("/api/v1/metrics")
async def api_v1_metrics():
    """Bulletproof: PnL, VWAP, SLA, battery SoC (JSON)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ..analytics import pnl, sla
    from ..foxess.service import get_cached_realtime

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    today = datetime.now(tz).date()
    daily = pnl.compute_daily_pnl(today)
    weekly = pnl.compute_weekly_pnl(today)
    monthly = pnl.compute_monthly_pnl(today)
    tgt = db.get_daily_target(today)
    soc = None
    try:
        soc = get_cached_realtime().soc
    except Exception:
        pass
    peak_pct = pnl.compute_peak_ratio(today)
    ofs = db.get_octopus_fetch_state()
    return {
        "pnl": {
            "daily": {
                "delta_vs_svt_pounds": daily.get("delta_vs_svt_gbp"),
                "delta_vs_fixed_pounds": daily.get("delta_vs_fixed_gbp"),
            },
            "weekly": {"delta_vs_svt_pounds": weekly.get("delta_vs_svt_gbp")},
            "monthly": {"delta_vs_svt_pounds": monthly.get("delta_vs_svt_gbp")},
        },
        "target_vwap_pence": (tgt or {}).get("target_vwap"),
        "realised_vwap_pence": pnl.compute_vwap(today),
        "slippage_pence": pnl.compute_slippage(today),
        "arbitrage_efficiency_pct": pnl.compute_arbitrage_efficiency(today),
        "peak_import_pct": peak_pct,
        "off_peak_import_pct": round(100.0 - peak_pct, 2) if peak_pct is not None else None,
        "battery_soc_percent": soc,
        "battery_capacity_kwh": config.BATTERY_CAPACITY_KWH,
        "octopus_fetch": {
            "last_success_at": ofs.last_success_at,
            "consecutive_failures": ofs.consecutive_failures,
            "survival_mode_since": ofs.survival_mode_since,
            "failure_streak_started_at": ofs.failure_streak_started_at,
        },
        "sla": sla.compute_sla_metrics(),
        "today_strategy": (tgt or {}).get("strategy_summary"),
        "cheap_threshold_pence": (tgt or {}).get("cheap_threshold"),
        "peak_threshold_pence": (tgt or {}).get("peak_threshold"),
    }


@app.get("/api/v1/schedule")
async def api_v1_schedule():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    plan_date = datetime.now(tz).date().isoformat()
    return {
        "plan_date": plan_date,
        "actions": db.schedule_for_date(plan_date),
        "fox": db.get_latest_fox_schedule_state(),
    }


@app.get("/api/v1/schedule/history")
async def api_v1_schedule_history(limit: int = 200):
    return {"action_log": db.get_action_logs(limit=limit)}


@app.get("/api/v1/weather")
async def api_v1_weather():
    from ..weather import fetch_forecast

    fc = fetch_forecast(hours=48)
    out = [{"time": f.time_utc.isoformat(), "temp_c": f.temperature_c, "pv_kw": f.estimated_pv_kw} for f in fc]
    daikin = None
    try:
        cached = daikin_service.get_cached_devices(allow_refresh=False, actor="weather")
        if cached.devices:
            dev = cached.devices[0]
            c = get_daikin_client()
            s = c.get_status(dev)
            daikin = {
                "room_temp": s.room_temp,
                "outdoor_temp": s.outdoor_temp,
                "lwt": s.lwt,
                "tank_temp": s.tank_temp,
            }
    except Exception as e:
        daikin = {"error": str(e)}
    return {"forecast": out[:48], "daikin": daikin}


@app.get("/api/v1/health")
async def health():
    """Lightweight health check for gateways and process managers."""
    return {"status": "ok"}


@app.get("/api/v1/daikin/quota")
async def daikin_quota():
    """Return Daikin API quota usage, cache age, and stale status.

    Useful for dashboards and debugging to verify the quota-management layer is working.
    Does NOT make any API calls — reads from the in-memory service and SQLite quota log.
    """
    return daikin_service.get_quota_status_daikin()


@app.get("/api/v1/foxess/quota")
async def foxess_quota():
    """Return Fox ESS API quota usage, cache age, and stale status."""
    return get_refresh_stats_extended()


# ---------------------------------------------------------------------------
# Runtime-tunable settings (#52). PUT takes effect within the 30-sec cache
# TTL; settings that drive cron cadence trigger an APScheduler re-register.
# ---------------------------------------------------------------------------


@app.get("/api/v1/settings")
async def settings_list():
    """Return every runtime-tunable setting with its current value, env default,
    range, and ``overridden`` flag (true when the DB row supplants the env)."""
    from .. import runtime_settings as rts
    return {"settings": rts.list_settings()}


@app.get("/api/v1/settings/{key}")
async def settings_get(key: str):
    from .. import runtime_settings as rts
    if key not in rts.SCHEMA:
        raise HTTPException(status_code=404, detail=f"unknown setting {key!r}")
    try:
        value = rts.get_setting(key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"key": key, "value": value}


@app.put("/api/v1/settings/{key}")
async def settings_put(key: str, payload: dict, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Validate + persist + (if cron) re-register APScheduler jobs.

    Body: ``{"value": <new>}``. Type coercion follows the schema:
    ``list[int]`` accepts ``"6,12,18"`` or ``[6, 12, 18]``.
    """
    _enforce_simulation_id(f"setting.{key}", x_simulation_id)
    from .. import runtime_settings as rts
    if key not in rts.SCHEMA:
        raise HTTPException(status_code=404, detail=f"unknown setting {key!r}")
    if "value" not in payload:
        raise HTTPException(status_code=400, detail="missing 'value' in body")
    try:
        canonical = rts.set_setting(key, payload["value"], actor="api")
    except rts.SettingValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    cron_status = None
    if rts.SCHEMA[key].cron_reload:
        cron_status = reregister_cron_jobs(reason=f"settings_put:{key}")

    return {"key": key, "value": canonical, "cron_status": cron_status}


@app.delete("/api/v1/settings/{key}")
async def settings_delete(key: str):
    """Clear the override row — the next read returns the env default. Same
    cron side effect as PUT when the key is cadence-related."""
    from .. import runtime_settings as rts
    if key not in rts.SCHEMA:
        raise HTTPException(status_code=404, detail=f"unknown setting {key!r}")
    removed = rts.delete_setting(key, actor="api")
    cron_status = None
    if rts.SCHEMA[key].cron_reload:
        cron_status = reregister_cron_jobs(reason=f"settings_delete:{key}")
    return {"key": key, "removed": removed, "cron_status": cron_status}


@app.get("/api/v1/daikin/status", response_model=list[DaikinStatusResponse])
async def daikin_status(refresh: bool = False):
    """Get status of all Daikin devices.

    Set ?refresh=true to force a live fetch (subject to rate limiting and the daily
    quota). Without the flag the cached value is returned immediately.
    """
    logger.debug("GET /api/v1/daikin/status refresh=%s", refresh)
    try:
        if refresh:
            cached = daikin_service.force_refresh_devices(actor="api")
        else:
            cached = daikin_service.get_cached_devices(allow_refresh=False, actor="api")
        devices = cached.devices
        client = get_daikin_client()
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
                control_mode=config.DAIKIN_CONTROL_MODE,
            ))
        logger.info(
            "Daikin status: %d device(s) source=%s stale=%s",
            len(result), cached.source, cached.stale,
        )
        return result
    except FileNotFoundError as e:
        logger.warning("Daikin not configured: %s", e)
        raise HTTPException(status_code=503, detail=f"Daikin not configured: {e}")
    except DaikinError as e:
        logger.warning("Daikin API error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/v1/daikin/power", response_model=PendingActionResponse | ActionResult)
async def daikin_power(req: PowerRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Turn Daikin climate control on or off."""
    _enforce_simulation_id("daikin.set_power", x_simulation_id)
    _require_active_daikin()
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
        daikin_service.set_power(req.on, actor="api")
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
async def daikin_temperature(req: TemperatureRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Set Daikin target room temperature. Blocked when weather regulation is active."""
    _enforce_simulation_id("daikin.set_temperature", x_simulation_id)
    _require_active_daikin()
    action_type = "daikin.temperature"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        # Check weather regulation before setting temperature
        cached = daikin_service.get_cached_devices(allow_refresh=False, actor="api")
        for dev in cached.devices:
            if dev.weather_regulation_enabled:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot set room temperature while weather regulation is active. "
                           "Use LWT offset instead, or disable weather regulation first.",
                )
        mode = req.mode
        daikin_service.set_temperature(req.temperature, mode or "heating", actor="api")
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
async def daikin_lwt_offset(req: LWTOffsetRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Set Daikin leaving water temperature offset."""
    _enforce_simulation_id("daikin.set_lwt_offset", x_simulation_id)
    _require_active_daikin()
    action_type = "daikin.lwt_offset"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        daikin_service.set_lwt_offset(req.offset, req.mode or "heating", actor="api")
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
async def daikin_mode(req: ModeRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Set Daikin operation mode."""
    _enforce_simulation_id("daikin.set_operation_mode", x_simulation_id)
    _require_active_daikin()
    action_type = "daikin.mode"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        daikin_service.set_operation_mode(req.mode.value, actor="api")
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
async def daikin_tank_temperature(req: TankTemperatureRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Set Daikin DHW tank target temperature."""
    _enforce_simulation_id("daikin.set_tank_temperature", x_simulation_id)
    _require_active_daikin()
    action_type = "daikin.tank_temperature"
    
    allowed, wait_time = safeguards.check_rate_limit(action_type)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds."
        )
    
    try:
        daikin_service.set_tank_temperature(req.temperature, actor="api")
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
async def daikin_tank_power(req: TankPowerRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Turn Daikin DHW tank on or off."""
    _enforce_simulation_id("daikin.set_tank_power", x_simulation_id)
    _require_active_daikin()
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
        daikin_service.set_tank_power(req.on, actor="api")
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
        stats = get_refresh_stats_extended()
        last_ts = stats.get("last_updated_epoch")
        updated_at_str = None
        if last_ts is not None:
            updated_at_str = datetime.fromtimestamp(last_ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        out = FoxESSStatusResponse(
            soc=d.soc,
            solar_power=d.solar_power,
            grid_power=d.grid_power,
            battery_power=d.battery_power,
            load_power=d.load_power,
            work_mode=d.work_mode,
            updated_at=updated_at_str,
            refresh_count_24h=stats.get("refresh_count_24h"),
            refresh_limit_24h=stats.get("daily_budget", 1440),
            quota_used_24h=stats.get("quota_used_24h"),
            quota_remaining_24h=stats.get("quota_remaining_24h"),
            daily_budget=stats.get("daily_budget"),
            quota_blocked=stats.get("blocked"),
            cache_age_seconds=stats.get("cache_age_seconds"),
            cache_stale=stats.get("stale"),
        )
        logger.info(
            "Fox ESS status: soc=%.1f solar=%.2f grid=%.2f battery=%.2f load=%.2f "
            "work_mode=%s refresh_24h=%s quota_used=%s stale=%s",
            out.soc, out.solar_power, out.grid_power, out.battery_power, out.load_power,
            out.work_mode, (out.refresh_count_24h or 0),
            stats.get("quota_used_24h"), stats.get("stale"),
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
async def foxess_mode(req: FoxESSModeRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    _enforce_simulation_id("foxess.set_mode", x_simulation_id)
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
async def foxess_charge_period(req: ChargePeriodRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    _enforce_simulation_id("foxess.set_charge_period", x_simulation_id)
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
            daikin_service.set_power(action.parameters["on"], actor="api_confirm")
            msg = f"Daikin turned {'ON' if action.parameters['on'] else 'OFF'}"
        
        elif action.action_type == "daikin.tank_power":
            daikin_service.set_tank_power(action.parameters["on"], actor="api_confirm")
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
        parameters={"temperature": {"type": "number", "min": 30, "max": 65, "description": "Tank target in Celsius"}},
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
            daikin_service.set_power(params["on"], actor="openclaw")
            return ActionResult(success=True, message=f"Daikin turned {'ON' if params['on'] else 'OFF'}")
        
        elif action_type == "daikin.temperature":
            temp = params["temperature"]
            if temp < 15 or temp > 30:
                raise HTTPException(status_code=400, detail="Temperature must be between 15 and 30°C")
            # Check weather regulation via cache
            cached = daikin_service.get_cached_devices(allow_refresh=False, actor="openclaw")
            for dev in cached.devices:
                if dev.weather_regulation_enabled:
                    raise HTTPException(
                        status_code=409,
                        detail="Cannot set room temperature while weather regulation is active. "
                               "Use LWT offset instead, or disable weather regulation first.",
                    )
            mode = params.get("mode") or "heating"
            daikin_service.set_temperature(temp, mode, actor="openclaw")
            return ActionResult(success=True, message=f"Temperature set to {temp}°C")
        
        elif action_type == "daikin.lwt_offset":
            offset = params["offset"]
            if offset < -10 or offset > 10:
                raise HTTPException(status_code=400, detail="LWT offset must be between -10 and +10")
            mode = params.get("mode") or "heating"
            daikin_service.set_lwt_offset(offset, mode, actor="openclaw")
            return ActionResult(success=True, message=f"LWT offset set to {offset:+g}")
        
        elif action_type == "daikin.mode":
            mode = params["mode"]
            daikin_service.set_operation_mode(mode, actor="openclaw")
            return ActionResult(success=True, message=f"Mode set to {mode}")
        
        elif action_type == "daikin.tank_temperature":
            temp = params["temperature"]
            if temp < 30 or temp > 65:
                raise HTTPException(status_code=400, detail="Tank temperature must be between 30 and 65°C")
            daikin_service.set_tank_temperature(temp, actor="openclaw")
            return ActionResult(success=True, message=f"Tank temperature set to {temp}°C")
        
        elif action_type == "daikin.tank_power":
            daikin_service.set_tank_power(params["on"], actor="openclaw")
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

def _get_assistant_context() -> tuple[list[dict], dict | None, dict | None]:
    """Return (daikin_status_list, foxess_status_dict, tariff_dict) for assistant context."""
    daikin_list: list[dict] = []
    foxess_status: dict | None = None
    tariff: dict | None = None
    if config.MANUAL_TARIFF_IMPORT_PENCE > 0 or config.MANUAL_TARIFF_EXPORT_PENCE > 0:
        tariff = {
            "import_rate": config.MANUAL_TARIFF_IMPORT_PENCE,
            "export_rate": config.MANUAL_TARIFF_EXPORT_PENCE,
            "tariff_name": "Manual",
        }
    try:
        cached = daikin_service.get_cached_devices(allow_refresh=False, actor="assistant")
        for dev in cached.devices or []:
            client = get_daikin_client()
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
    date: str | None = None,
    month: str | None = None,
    year: int | None = None,
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
    equivalent_gas_cost_pounds: float | None,
    gas_comparison_ahead_pounds: float | None,
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
    period: str | None = None,
    date: str | None = None,
    month: str | None = None,
    year: int | None = None,
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
async def scheduler_pause(x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Pause the Agile-based Daikin scheduler (no more automatic LWT changes)."""
    _enforce_simulation_id("scheduler.pause", x_simulation_id)
    pause_scheduler()
    return {"status": "paused"}


@app.post("/api/v1/scheduler/resume")
async def scheduler_resume(x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Resume the Agile-based Daikin scheduler."""
    _enforce_simulation_id("scheduler.resume", x_simulation_id)
    resume_scheduler()
    return {"status": "resumed"}


@app.get("/api/v1/optimization/status", response_model=OptimizationStatusExtendedResponse)
async def optimization_status():
    """Bulletproof brain: mode, preset, Agile cache (legacy path kept for dashboards)."""
    cache = get_agile_cache()
    ofs = db.get_octopus_fetch_state()
    sch = get_scheduler_status()
    return OptimizationStatusExtendedResponse(
        enabled=config.USE_BULLETPROOF_ENGINE,
        operation_mode=config.OPERATION_MODE,
        preset=config.OPTIMIZATION_PRESET,
        optimizer_backend=(config.OPTIMIZER_BACKEND or "lp"),
        tariff_code=config.OCTOPUS_TARIFF_CODE,
        cache_slots=len(cache.rates or []),
        cache_fetched_at_utc=cache.fetched_at_utc.isoformat() if cache.fetched_at_utc else None,
        cache_error=cache.error,
        last_plan_at_utc=ofs.last_success_at,
        target_mean_price_pence=sch.get("current_price_pence"),
        consent={
            "bulletproof": True,
            "detail": "V7 consent flow removed. Run POST /api/v1/optimization/propose to refresh SQLite + Fox V3.",
        },
        v7_safeties=None,
    )


@app.get("/api/v1/optimization/plan")
async def optimization_plan():
    """Bulletproof plan: today's SQLite action_schedule + last Fox V3 snapshot."""
    from zoneinfo import ZoneInfo

    if not config.OCTOPUS_TARIFF_CODE:
        raise HTTPException(status_code=503, detail="OCTOPUS_TARIFF_CODE not set")
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    plan_date = datetime.now(tz).date().isoformat()
    return {
        "ok": True,
        "bulletproof": True,
        "plan_date": plan_date,
        "actions": db.schedule_for_date(plan_date),
        "fox": db.get_latest_fox_schedule_state(),
        "note": "48-slot V7 solver removed; use /api/v1/metrics for thresholds and PnL context.",
    }


@app.get("/api/v1/optimization/dispatch-preview", response_model=OptimizationDispatchPreviewResponse)
async def optimization_dispatch_preview():
    """V7 macro-based hints removed; use /api/v1/schedule and live device status."""
    return OptimizationDispatchPreviewResponse(
        lwt_offset=0.0,
        daikin_tank_target_c=None,
        fox_work_mode=None,
        disable_weather_regulation=False,
        reason="V7 dispatch-preview retired. Use GET /api/v1/schedule and GET /api/v1/metrics.",
    )


@app.post("/api/v1/optimization/refresh")
async def optimization_refresh():
    """Fetch Agile rates from Octopus, persist to SQLite, and update in-memory cache.

    Safe to call at any time — if tomorrow's rates are now available they will be
    stored and the next planner run will pick them up automatically.
    """
    allowed, wait_time = safeguards.check_rate_limit("optimization.refresh")
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {wait_time:.1f} seconds.",
        )
    if not config.OCTOPUS_TARIFF_CODE:
        raise HTTPException(status_code=503, detail="OCTOPUS_TARIFF_CODE not set")

    # Persist to SQLite (the LP optimizer reads from DB, not the in-memory cache)
    from .. import db as _db
    from ..scheduler.agile import fetch_agile_rates
    rates = await asyncio.to_thread(fetch_agile_rates, config.OCTOPUS_TARIFF_CODE)
    saved = 0
    if rates:
        saved = await asyncio.to_thread(_db.save_agile_rates, rates, config.OCTOPUS_TARIFF_CODE)

    # Also refresh in-memory cache for tariff tools / status display
    cache = refresh_agile_rates()
    safeguards.record_action_time("optimization.refresh")

    slot_count = len(rates) if rates else 0
    has_tomorrow = slot_count >= 40  # Octopus publishes ~48 slots for tomorrow after 16:00 UK
    return {
        "status": "ok",
        "slots_fetched": slot_count,
        "slots_saved_to_db": saved,
        "has_tomorrow_rates": has_tomorrow,
        "fetched_at_utc": cache.fetched_at_utc.isoformat() if cache.fetched_at_utc else None,
        "hint": (
            "Tomorrow's rates are available — POST /api/v1/optimization/propose to replan."
            if has_tomorrow
            else "Only today's remaining rates available (Octopus publishes tomorrow ~16:00 UK). "
                 "Optimizer will plan for today-remainder."
        ),
    }


@app.post("/api/v1/optimization/fetch-and-plan")
async def optimization_fetch_and_plan():
    """Fetch latest Agile rates and immediately run the full optimizer.

    Combines /optimization/refresh + /optimization/propose in one call.
    Use for on-demand re-planning after an Octopus rates update, a config change,
    or whenever you want to force a fresh plan at any time of day.

    The planner automatically targets tomorrow (full day) if tomorrow's rates are
    available, or today-remainder if not, and falls back to Self Use if there are
    no usable rates.
    """
    if not config.OCTOPUS_TARIFF_CODE:
        raise HTTPException(status_code=503, detail="OCTOPUS_TARIFF_CODE not set")

    # 1. Fetch + persist rates
    from .. import db as _db
    from ..scheduler.agile import fetch_agile_rates
    rates = await asyncio.to_thread(fetch_agile_rates, config.OCTOPUS_TARIFF_CODE)
    if rates:
        await asyncio.to_thread(_db.save_agile_rates, rates, config.OCTOPUS_TARIFF_CODE)
    refresh_agile_rates()  # update in-memory cache too

    slot_count = len(rates) if rates else 0
    has_tomorrow = slot_count >= 40

    # 2. Run optimizer
    fox = None
    try:
        fox = FoxESSClient(**config.foxess_client_kwargs())
    except Exception:
        pass
    result = await asyncio.to_thread(run_optimizer, fox, None)

    now = datetime.now(UTC)
    plan_id = f"bp-{uuid4().hex[:12]}"
    return {
        "plan_id": plan_id,
        "proposed_at": now.isoformat(),
        "status": "applied" if result.get("ok") else "fallback",
        "slots_fetched": slot_count,
        "has_tomorrow_rates": has_tomorrow,
        "plan_date": result.get("plan_date"),
        "optimizer_backend": result.get("optimizer_backend"),
        "strategy": result.get("strategy") or result.get("error"),
        "fox_uploaded": result.get("fox_uploaded", False),
        "daikin_actions": result.get("daikin_actions", 0),
        "fallback": result.get("fallback"),
    }


# ── Optimization-compatible controls (Bulletproof; no V7 consent) ──────────────

@app.post("/api/v1/optimization/propose", response_model=ProposePlanResponse)
async def optimization_propose(include_plan: bool = False, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Run the Bulletproof daily planner (SQLite + optional Fox V3 upload)."""
    _enforce_simulation_id("optimization.propose", x_simulation_id)
    if not config.OCTOPUS_TARIFF_CODE:
        raise HTTPException(status_code=503, detail="OCTOPUS_TARIFF_CODE not set")
    fox = None
    try:
        fox = FoxESSClient(**config.foxess_client_kwargs())
    except Exception:
        pass
    result = await asyncio.to_thread(run_optimizer, fox, None)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "optimizer failed"))
    now = datetime.now(UTC)
    plan_id = f"bp-{uuid4().hex[:12]}"
    resp = ProposePlanResponse(
        plan_id=plan_id,
        proposed_at=now.isoformat(),
        expires_at=(now + timedelta(hours=24)).isoformat(),
        status="applied",
        summary=result.get("strategy") or "",
        plan=result if include_plan else None,
    )
    return resp


@app.post("/api/v1/optimization/approve", response_model=ApprovePlanResponse)
async def optimization_approve(req: ApprovePlanRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """No-op under Bulletproof (plans apply on propose)."""
    _enforce_simulation_id("optimization.approve", x_simulation_id)
    return ApprovePlanResponse(
        ok=True,
        plan_id=req.plan_id,
        status="not_applicable",
        message="Bulletproof does not use plan consent; POST /api/v1/optimization/propose already persisted the plan.",
    )


@app.post("/api/v1/optimization/reject", response_model=ApprovePlanResponse)
async def optimization_reject(req: RejectPlanRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """No-op under Bulletproof."""
    _enforce_simulation_id("optimization.reject", x_simulation_id)
    return ApprovePlanResponse(
        ok=True,
        plan_id=req.plan_id,
        status="not_applicable",
        message="Bulletproof does not use plan consent. Adjust presets and re-run POST /api/v1/optimization/propose.",
    )


@app.get("/api/v1/optimization/pending")
async def optimization_pending():
    return {"pending": None, "bulletproof": True, "detail": "No consent queue; see GET /api/v1/optimization/plan."}


@app.post("/api/v1/optimization/preset", response_model=SetPresetResponse)
async def optimization_set_preset(req: SetPresetRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Switch the household preset at runtime (normal/guests/travel/away/boost)."""
    _enforce_simulation_id("optimization.set_preset", x_simulation_id)
    config.OPTIMIZATION_PRESET = req.preset
    logger.info("Optimization preset changed to %s", req.preset)
    return SetPresetResponse(
        ok=True,
        preset=req.preset,
        message=(
            f"Preset set to '{req.preset}'. "
            "Call POST /api/v1/optimization/propose to regenerate the plan."
        ),
    )


@app.post("/api/v1/optimization/backend", response_model=SetOptimizerBackendResponse)
async def optimization_set_backend(req: SetOptimizerBackendRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Switch planner: ``lp`` (PuLP MILP) or ``heuristic`` (legacy price-quantile classifier)."""
    _enforce_simulation_id("optimization.set_backend", x_simulation_id)
    config.OPTIMIZER_BACKEND = req.backend
    logger.info("Optimizer backend set to %s", req.backend)
    return SetOptimizerBackendResponse(
        ok=True,
        optimizer_backend=config.OPTIMIZER_BACKEND,
        message=(
            f"Backend set to '{req.backend}'. "
            "Call POST /api/v1/optimization/propose to regenerate the plan."
        ),
    )


@app.post("/api/v1/optimization/mode", response_model=SetOperationModeResponse)
async def optimization_set_mode(req: SetOperationModeRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Switch simulation vs operational. Snapshot saved before each transition."""
    _enforce_simulation_id("optimization.set_mode", x_simulation_id)
    current_mode = config.OPERATION_MODE
    new_mode = req.mode

    if current_mode == new_mode:
        return SetOperationModeResponse(
            ok=True,
            mode=new_mode,
            message=f"Already in {new_mode} mode. No change.",
        )

    snap = save_snapshot(trigger=f"mode_change: {current_mode} -> {new_mode}")
    snapshot_id = snap.get("snapshot_id")

    config.OPERATION_MODE = new_mode

    if new_mode == "simulation":
        msg = (
            f"Switched to simulation mode (snapshot {snapshot_id} saved). "
            "Hardware writes are skipped per OPENCLAW_READ_ONLY / operational rules."
        )
    else:
        msg = (
            f"Switched to operational mode (snapshot {snapshot_id} saved). "
            "Fox V3 and Daikin actions run when keys are present and reads are allowed."
        )

    logger.info("Operation mode changed: %s -> %s (snapshot=%s)", current_mode, new_mode, snapshot_id)
    return SetOperationModeResponse(ok=True, mode=new_mode, snapshot_id=snapshot_id, message=msg)


@app.post("/api/v1/optimization/rollback", response_model=RollbackResponse)
async def optimization_rollback(snapshot_id: str | None = None, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Restore a config snapshot (latest by default). Forces simulation mode on restore."""
    _enforce_simulation_id("optimization.rollback", x_simulation_id)
    try:
        if snapshot_id:
            snap = restore_snapshot(snapshot_id)
        else:
            snap = rollback_latest()
            if snap is None:
                raise HTTPException(status_code=404, detail="No snapshots found to roll back to.")
        sid = snap.get("snapshot_id", "unknown")
        logger.info("Config rolled back to snapshot %s", sid)
        return RollbackResponse(
            ok=True,
            snapshot_id=sid,
            message=(
                f"Config restored from snapshot {sid}. "
                "System is in simulation mode. Re-run POST /api/v1/optimization/propose before operational."
            ),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Rollback failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Rollback failed: {exc}")


@app.post("/api/v1/optimization/auto-approve", response_model=SetAutoApproveResponse)
async def optimization_set_auto_approve(req: SetAutoApproveRequest, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Legacy toggle; Bulletproof does not gate on consent."""
    _enforce_simulation_id("optimization.set_auto_approve", x_simulation_id)
    config.PLAN_AUTO_APPROVE = req.enabled
    logger.info("PLAN_AUTO_APPROVE set to %s", req.enabled)
    msg = (
        "Stored PLAN_AUTO_APPROVE=true (no consent gate under Bulletproof)."
        if req.enabled
        else "Stored PLAN_AUTO_APPROVE=false."
    )
    return SetAutoApproveResponse(ok=True, auto_approve=req.enabled, message=msg)


@app.get("/api/v1/optimization/snapshots", response_model=ListSnapshotsResponse)
async def optimization_snapshots():
    """List all available config snapshots (newest first)."""
    snaps = list_snapshots()
    return ListSnapshotsResponse(
        snapshots=[
            SnapshotSummary(
                snapshot_id=s.get("snapshot_id", ""),
                snapshot_at=s.get("snapshot_at"),
                trigger=s.get("trigger"),
                operation_mode=s.get("operation_mode"),
                preset=s.get("preset"),
            )
            for s in snaps
        ]
    )


# ── Tariff comparison endpoints ──────────────────────────────────────────────

@app.get("/api/v1/tariffs/available", response_model=ListAvailableTariffsResponse)
async def tariffs_available(max_tariffs: int = 15):
    """List currently available Octopus tariff products with rates and policies."""
    from ..energy.octopus_products import get_available_tariffs
    tariffs = get_available_tariffs(max_products=max_tariffs)
    return ListAvailableTariffsResponse(
        ok=True,
        gsp=config.OCTOPUS_GSP if hasattr(config, "OCTOPUS_GSP") else "C",
        tariffs=[
            TariffProductResponse(
                product_code=t.product_code,
                tariff_code=t.tariff_code,
                display_name=t.display_name,
                full_name=t.full_name,
                provider=t.provider,
                pricing=t.pricing.value,
                rates=TariffRatesResponse(
                    unit_rate_pence=t.rates.unit_rate_pence,
                    day_rate_pence=t.rates.day_rate_pence,
                    night_rate_pence=t.rates.night_rate_pence,
                    off_peak_start=t.rates.off_peak_start,
                    off_peak_end=t.rates.off_peak_end,
                    standing_charge_pence_per_day=t.rates.standing_charge_pence_per_day,
                    export_rate_pence=t.rates.export_rate_pence,
                ),
                policy=TariffPolicyResponse(
                    contract_type=t.policy.contract_type.value,
                    contract_months=t.policy.contract_months,
                    exit_fee_pence=t.policy.exit_fee_pence,
                    is_green=t.policy.is_green,
                    is_prepay=t.policy.is_prepay,
                ),
                description=t.description,
                summary_line=t.summary_line(),
            )
            for t in tariffs
        ],
    )


@app.post("/api/v1/tariffs/compare", response_model=TariffRecommendationResponse)
async def tariffs_compare(req: TariffCompareRequest):
    """Compare available tariffs against your actual usage and recommend the best.

    Uses Fox ESS import/export data for the specified period (months_back).
    Factors in standing charges, unit rates, export payments, lock-in periods, and exit fees.
    """
    from ..energy.tariff_engine import get_tariff_recommendation
    rec = get_tariff_recommendation(
        months_back=req.months_back,
        max_tariffs=req.max_tariffs,
    )
    results_out = []
    for r in rec.candidates:
        results_out.append(TariffSimulationResultResponse(
            product_code=r.tariff.product_code,
            display_name=r.tariff.display_name,
            pricing=r.tariff.pricing.value,
            period_days=r.period_days,
            import_kwh=r.import_kwh,
            export_kwh=r.export_kwh,
            import_cost_pence=r.import_cost_pence,
            export_earnings_pence=r.export_earnings_pence,
            standing_charge_pence=r.standing_charge_pence,
            net_cost_pence=r.net_cost_pence,
            annual_net_cost_pounds=r.annual_net_cost_pounds,
            annual_import_cost_pounds=r.annual_import_cost_pounds,
            annual_standing_charge_pounds=r.annual_standing_charge_pounds,
            annual_export_earnings_pounds=r.annual_export_earnings_pounds,
            exit_fee_pounds=r.exit_fee_pounds,
            lock_in_months=r.lock_in_months,
            first_year_effective_cost_pounds=r.first_year_effective_cost_pounds,
            standing_charge_per_day=r.tariff.rates.standing_charge_pence_per_day,
            unit_rate_pence=r.tariff.rates.unit_rate_pence,
            contract_type=r.tariff.policy.contract_type.value,
            is_green=r.tariff.policy.is_green,
        ))
    usage_kwh = rec.candidates[0].import_kwh if rec.candidates else None
    usage_exp = rec.candidates[0].export_kwh if rec.candidates else None
    usage_days = rec.candidates[0].period_days if rec.candidates else None
    return TariffRecommendationResponse(
        ok=True,
        summary=rec.summary,
        best_product_code=rec.best.tariff.product_code if rec.best else None,
        best_display_name=rec.best.tariff.display_name if rec.best else None,
        savings_vs_current_pounds=rec.savings_vs_current_pounds,
        current_product_code=config.OCTOPUS_TARIFF_CODE or None,
        results=results_out,
        usage_import_kwh=usage_kwh,
        usage_export_kwh=usage_exp,
        usage_period_days=usage_days,
        generated_at=rec.generated_at.isoformat() if rec.generated_at else None,
    )


@app.post("/api/v1/tariffs/dashboard", response_model=TariffDashboardResponse)
async def tariffs_dashboard(req: TariffDashboardRequest):
    """Granular tariff comparison dashboard data.

    Returns per-period (daily/weekly/monthly) cost breakdown across all available
    tariffs, identifying the winner for each period. The current tariff (Octopus
    Flexible by default) is flagged as baseline.
    """
    from ..energy.tariff_engine import get_tariff_comparison_dashboard
    data = get_tariff_comparison_dashboard(
        months_back=req.months_back,
        granularity=req.granularity,
        max_tariffs=req.max_tariffs,
    )
    if not data.get("ok"):
        return TariffDashboardResponse(ok=False, error=data.get("error", "Unknown error"))
    return TariffDashboardResponse(
        ok=True,
        granularity=data.get("granularity"),
        periods=[TariffPeriodCosts(**p) for p in data.get("periods", [])],
        totals=[TariffTotalRow(**t) for t in data.get("totals", [])],
        current_product_code=data.get("current_product_code"),
        current_annual_pounds=data.get("current_annual_pounds"),
        usage=data.get("usage"),
        data_source=data.get("data_source"),
    )


# ── Octopus account + consumption endpoints ───────────────────────────────────

@app.get("/api/v1/octopus/account", response_model=OctopusAccountResponse)
async def octopus_account():
    """Return Octopus account summary: current tariff, MPAN roles, GSP, detection status.

    Calls the authenticated Octopus account endpoint — uses OCTOPUS_API_KEY from .env.
    Returns 503 if API key not configured.
    """
    if not config.OCTOPUS_API_KEY:
        return OctopusAccountResponse(
            ok=False,
            error="OCTOPUS_API_KEY not configured in .env",
            account_number=config.OCTOPUS_ACCOUNT_NUMBER,
            api_key_configured=False,
        )
    from ..energy.octopus_client import get_account_summary
    summary = get_account_summary()
    current = summary.get("current_tariff")
    return OctopusAccountResponse(
        ok=summary.get("error") is None,
        error=summary.get("error"),
        account_number=summary.get("account_number", ""),
        api_key_configured=summary.get("api_key_configured", False),
        current_tariff=(
            OctopusCurrentTariffResponse(**current) if current else None
        ),
        mpan_import=summary.get("mpan_import"),
        mpan_export=summary.get("mpan_export"),
        gsp=summary.get("gsp", ""),
        detection_source=summary.get("detection_source", "not_run"),
    )


@app.get("/api/v1/octopus/consumption", response_model=OctopusConsumptionResponse)
async def octopus_consumption(
    mpan: str | None = None,
    serial: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    group_by: str | None = None,
):
    """Proxy to Octopus consumption endpoint for a specific MPAN/serial.

    Defaults to the import MPAN from config if mpan/serial not specified.
    period_from/period_to: ISO datetime strings (defaults to last 30 days).
    group_by: half-hourly (default), day, week, month.
    """
    if not config.OCTOPUS_API_KEY:
        raise HTTPException(status_code=503, detail="OCTOPUS_API_KEY not configured")

    from datetime import datetime

    from ..energy.octopus_client import fetch_consumption, get_mpan_roles

    roles = get_mpan_roles()
    use_mpan = mpan or roles.import_mpan or config.OCTOPUS_MPAN_1
    use_serial = serial or roles.import_serial or config.OCTOPUS_METER_SN_1

    if not use_mpan or not use_serial:
        raise HTTPException(status_code=400, detail="MPAN and meter serial required. Configure OCTOPUS_MPAN_1/OCTOPUS_METER_SN_1 in .env or pass mpan/serial query params.")

    pf = None
    pt = None
    if period_from:
        try:
            pf = datetime.fromisoformat(period_from.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid period_from format. Use ISO datetime.")
    if period_to:
        try:
            pt = datetime.fromisoformat(period_to.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid period_to format. Use ISO datetime.")

    if group_by and group_by not in ("day", "week", "month"):
        raise HTTPException(status_code=400, detail="group_by must be day, week, or month")

    try:
        slots = fetch_consumption(use_mpan, use_serial, pf, pt, group_by=group_by)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.warning("Octopus consumption fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Octopus API error: {exc}")

    return OctopusConsumptionResponse(
        ok=True,
        mpan=use_mpan,
        serial=use_serial,
        group_by=group_by,
        slots=[
            OctopusConsumptionSlotResponse(
                interval_start=s.interval_start.isoformat(),
                interval_end=s.interval_end.isoformat(),
                consumption_kwh=s.consumption_kwh,
            )
            for s in slots
        ],
        total_kwh=round(sum(s.consumption_kwh for s in slots), 3),
    )


@app.post("/api/v1/octopus/auto-detect", response_model=OctopusAutoDetectResponse)
async def octopus_auto_detect():
    """Detect MPAN roles (import/export) and current tariff from the Octopus account API.

    Updates the runtime config with detected values.
    Use this once after setup to confirm your MPANs and current tariff.
    """
    if not config.OCTOPUS_API_KEY:
        return OctopusAutoDetectResponse(
            ok=False,
            error="OCTOPUS_API_KEY not configured in .env",
        )
    if not config.OCTOPUS_ACCOUNT_NUMBER:
        return OctopusAutoDetectResponse(
            ok=False,
            error="OCTOPUS_ACCOUNT_NUMBER not configured in .env",
        )

    from ..energy.octopus_client import auto_detect_mpan_roles, discover_current_tariff
    error = None
    roles = None
    tariff = None

    try:
        roles = auto_detect_mpan_roles()
        # Update runtime config with detected values
        config.OCTOPUS_MPAN_IMPORT = roles.import_mpan
        config.OCTOPUS_MPAN_EXPORT = roles.export_mpan
        config.OCTOPUS_METER_SERIAL_IMPORT = roles.import_serial
        config.OCTOPUS_METER_SERIAL_EXPORT = roles.export_serial
        config.OCTOPUS_GSP = roles.gsp
        logger.info(
            "Auto-detect: import=%s export=%s GSP=%s",
            roles.import_mpan, roles.export_mpan, roles.gsp,
        )
    except Exception as exc:
        error = f"MPAN detection failed: {exc}"
        logger.warning("Auto-detect MPAN roles failed: %s", exc)

    try:
        tariff = discover_current_tariff()
        if tariff and tariff.product_code:
            config.CURRENT_TARIFF_PRODUCT = tariff.product_code
            logger.info("Auto-detect: current tariff = %s", tariff.product_code)
    except Exception as exc:
        if error:
            error += f"; tariff detection failed: {exc}"
        else:
            error = f"Tariff detection failed: {exc}"
        logger.warning("Auto-detect tariff failed: %s", exc)

    return OctopusAutoDetectResponse(
        ok=error is None,
        error=error,
        import_mpan=roles.import_mpan if roles else "",
        export_mpan=roles.export_mpan if roles else "",
        gsp=roles.gsp if roles else config.OCTOPUS_GSP,
        current_tariff_product=tariff.product_code if tariff else None,
        current_tariff_code=tariff.tariff_code if tariff else None,
        detection_source=roles.source if roles else "failed",
    )


# ============================================================================
# v10.1 cockpit redesign — simulate-first action paradigm (PR-A)
# ============================================================================
# Every state-changing route above is paired with a /simulate route that
# returns an ActionDiff. The frontend (PR-B) renders the diff in a modal and
# only triggers the real-write route after operator confirms (passing
# X-Simulation-Id). Simulate endpoints NEVER call cloud APIs — they read
# cached state only, preserving Daikin/Fox quotas.

from .simulation import ActionDiff, get_store as _get_simulation_store  # noqa: E402
from . import simulate_diffs as _diffs  # noqa: E402


def _register_diff(diff: ActionDiff) -> dict:
    """Register an ActionDiff with the store and return the JSON response."""
    sid = _get_simulation_store().register(diff)
    return diff.to_response_dict()


def _require_simulation_id_enabled() -> bool:
    """Whether REQUIRE_SIMULATION_ID is on. Default off until cockpit UI (PR-B) ships."""
    from ..runtime_settings import get_setting
    val = (get_setting("REQUIRE_SIMULATION_ID") or "false").strip().lower()
    return val == "true"


def _enforce_simulation_id(expected_action: str, x_simulation_id: str | None) -> ActionDiff | None:
    """Enforce the simulate-then-confirm flow when REQUIRE_SIMULATION_ID is on.

    Behaviour:
      * setting off → returns None (no-op; legacy callers continue to work).
      * setting on + missing header → 409 PreconditionRequired.
      * setting on + unknown/expired header → 410 Gone.
      * setting on + header for a different action → 409 (anti-replay).
      * setting on + valid header → consumes from store and returns the diff.
    """
    if not _require_simulation_id_enabled():
        return None
    if not x_simulation_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "SimulationIdRequired",
                "message": (
                    "X-Simulation-Id header required. "
                    f"POST {expected_action.replace('.', '/').replace('set_', '')}/simulate first."
                ),
            },
        )
    diff = _get_simulation_store().consume(x_simulation_id)
    if diff is None:
        raise HTTPException(
            status_code=410,
            detail={"error": "SimulationExpired", "message": "simulation_id expired or already used"},
        )
    if diff.action != expected_action:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "SimulationIdMismatch",
                "message": f"simulation_id was for {diff.action!r}, not {expected_action!r}",
            },
        )
    return diff


# --- Daikin simulate routes -------------------------------------------------

@app.post("/api/v1/daikin/power/simulate")
async def daikin_power_simulate(req: PowerRequest):
    return _register_diff(_diffs.diff_daikin_power(req.on))


@app.post("/api/v1/daikin/temperature/simulate")
async def daikin_temperature_simulate(req: TemperatureRequest):
    return _register_diff(_diffs.diff_daikin_temperature(req.temperature, req.mode))


@app.post("/api/v1/daikin/lwt-offset/simulate")
async def daikin_lwt_offset_simulate(req: LWTOffsetRequest):
    return _register_diff(_diffs.diff_daikin_lwt_offset(req.offset, req.mode))


@app.post("/api/v1/daikin/mode/simulate")
async def daikin_mode_simulate(req: ModeRequest):
    return _register_diff(_diffs.diff_daikin_mode(req.mode.value))


@app.post("/api/v1/daikin/tank-temperature/simulate")
async def daikin_tank_temperature_simulate(req: TankTemperatureRequest):
    return _register_diff(_diffs.diff_daikin_tank_temperature(req.temperature))


@app.post("/api/v1/daikin/tank-power/simulate")
async def daikin_tank_power_simulate(req: TankPowerRequest):
    return _register_diff(_diffs.diff_daikin_tank_power(req.on))


# --- Fox ESS simulate routes ------------------------------------------------

@app.post("/api/v1/foxess/mode/simulate")
async def foxess_mode_simulate(req: FoxESSModeRequest):
    return _register_diff(_diffs.diff_foxess_mode(req.mode.value))


@app.post("/api/v1/foxess/charge-period/simulate")
async def foxess_charge_period_simulate(periods: list[ChargePeriod]):
    return _register_diff(_diffs.diff_foxess_charge_period([p.model_dump() for p in periods]))


# --- Optimization simulate routes -------------------------------------------

@app.post("/api/v1/optimization/propose/simulate")
async def optimization_propose_simulate():
    return _register_diff(_diffs.diff_optimization_propose())


@app.post("/api/v1/optimization/approve/simulate")
async def optimization_approve_simulate(req: ApprovePlanRequest | None = None):
    plan_id = getattr(req, "plan_id", None) if req else None
    return _register_diff(_diffs.diff_optimization_approve(plan_id))


@app.post("/api/v1/optimization/reject/simulate")
async def optimization_reject_simulate(req: ApprovePlanRequest | None = None):
    plan_id = getattr(req, "plan_id", None) if req else None
    return _register_diff(_diffs.diff_optimization_reject(plan_id))


@app.post("/api/v1/optimization/rollback/simulate")
async def optimization_rollback_simulate():
    return _register_diff(_diffs.diff_optimization_rollback())


@app.post("/api/v1/optimization/preset/simulate")
async def optimization_preset_simulate(req: SetPresetRequest):
    return _register_diff(_diffs.diff_optimization_preset(req.preset))


@app.post("/api/v1/optimization/backend/simulate")
async def optimization_backend_simulate(req: SetOptimizerBackendRequest):
    return _register_diff(_diffs.diff_optimization_backend(req.backend))


@app.post("/api/v1/optimization/mode/simulate")
async def optimization_mode_simulate(req: SetOperationModeRequest):
    return _register_diff(_diffs.diff_optimization_mode(req.mode))


@app.post("/api/v1/optimization/auto-approve/simulate")
async def optimization_auto_approve_simulate(req: SetAutoApproveRequest):
    return _register_diff(_diffs.diff_optimization_auto_approve(req.enabled))


# --- Settings + scheduler simulate routes -----------------------------------

@app.put("/api/v1/settings/{key}/simulate")
async def settings_simulate(key: str, body: dict):
    return _register_diff(_diffs.diff_setting_change(key, body.get("value")))


@app.post("/api/v1/scheduler/pause/simulate")
async def scheduler_pause_simulate():
    return _register_diff(_diffs.diff_scheduler_pause())


@app.post("/api/v1/scheduler/resume/simulate")
async def scheduler_resume_simulate():
    return _register_diff(_diffs.diff_scheduler_resume())


# --- Lookup helper ----------------------------------------------------------

@app.get("/api/v1/simulate/{simulation_id}")
async def simulate_get(simulation_id: str):
    """Re-fetch a registered diff (non-consuming). Returns 404 if missing/expired."""
    diff = _get_simulation_store().get(simulation_id)
    if diff is None:
        raise HTTPException(status_code=404, detail="simulation_id not found or expired")
    return diff.to_response_dict()


# --- v10.1 cockpit data endpoints -------------------------------------------

@app.get("/api/v1/agile/today")
async def agile_today():
    """Today's Octopus Agile slot rates + the current half-hour's price.

    Returns a list of 48 (or fewer if partial) slots with import prices, plus
    the current import price. Export prices use the configured export tariff
    when available, else the same import series as a placeholder.

    Reads from SQLite ``agile_rates`` only — no cloud calls.
    """
    from datetime import UTC, datetime, timedelta
    from .. import db as _db

    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    export_tariff = (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()

    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    import_rows = _db.get_rates_for_period(tariff, day_start, day_end) if tariff else []
    export_rows = _db.get_rates_for_period(export_tariff, day_start, day_end) if export_tariff else []

    def _slot_price_at(rows: list, t: datetime) -> float | None:
        iso_t = t.isoformat().replace("+00:00", "Z")
        for r in rows:
            if r["valid_from"] <= iso_t < r["valid_to"]:
                return float(r["value_inc_vat"])
        return None

    return {
        "tariff_import_code": tariff or None,
        "tariff_export_code": export_tariff or None,
        "import_slots": [
            {"valid_from": r["valid_from"], "valid_to": r["valid_to"], "p": float(r["value_inc_vat"])}
            for r in import_rows
        ],
        "export_slots": [
            {"valid_from": r["valid_from"], "valid_to": r["valid_to"], "p": float(r["value_inc_vat"])}
            for r in export_rows
        ],
        "current_import_p": _slot_price_at(import_rows, now),
        "current_export_p": _slot_price_at(export_rows, now),
        "now_utc": now.isoformat().replace("+00:00", "Z"),
    }


@app.get("/api/v1/load/breakdown")
async def load_breakdown():
    """House total load split into Daikin (heat-pump) and residual (everything else).

    Reads cached state ONLY — never triggers Daikin or Fox refresh:
    - house_total_kw: from fox_realtime_snapshot.load_power_kw (cache, ~5-min TTL)
    - daikin_estimate_kw: physics estimate from cached Daikin outdoor_temp + climate curve.
      In v10 the Daikin runs autonomously; we predict its electrical draw rather
      than measuring it directly (Onecta has no real-time power channel).
    - residual_kw: house_total - daikin_estimate, floored at 0.

    When ``daikin_consumption_daily`` lands (deferred Epic #70 — D-1 backfill),
    this estimate will be calibrated against yesterday's daily total × today's
    weather curve. Until then we use the instantaneous physics estimate.
    """
    from .. import db as _db
    from ..physics import get_daikin_heating_kw

    house_total_kw = None
    fox_captured_at = None
    # Prefer Fox service cache (updated more frequently than the SQLite snapshot
    # which is only written by the MPC seeding job).
    try:
        from ..foxess.service import get_cached_realtime as _fox_realtime
        snap = _fox_realtime(allow_refresh=False)
        if snap is not None:
            house_total_kw = getattr(snap, "load_power", None) if not isinstance(snap, dict) else snap.get("load_power")
            fox_captured_at = getattr(snap, "updated_at", None) if not isinstance(snap, dict) else snap.get("updated_at")
    except Exception:
        pass
    if house_total_kw is None:
        # Fallback to SQLite snapshot (legacy path)
        snap = _db.get_fox_realtime_snapshot()
        if snap:
            house_total_kw = snap.get("load_power_kw")
            fox_captured_at = snap.get("captured_at")

    daikin_estimate_kw = None
    daikin_outdoor_c = None
    daikin_source = "unavailable"
    try:
        cached = daikin_service.get_cached_devices(allow_refresh=False, actor="dashboard")
        if cached.devices:
            dev = cached.devices[0]
            outdoor = getattr(dev, "outdoor_temp", None)
            if outdoor is not None:
                daikin_outdoor_c = float(outdoor)
                daikin_estimate_kw = float(get_daikin_heating_kw(daikin_outdoor_c))
                daikin_source = "physics_instantaneous"
    except Exception as exc:
        logger.debug("load_breakdown: daikin estimate failed: %s", exc)

    residual_kw = None
    if house_total_kw is not None and daikin_estimate_kw is not None:
        residual_kw = max(0.0, float(house_total_kw) - daikin_estimate_kw)

    return {
        "house_total_kw": house_total_kw,
        "daikin_estimate_kw": daikin_estimate_kw,
        "daikin_outdoor_c": daikin_outdoor_c,
        "daikin_source": daikin_source,  # "physics_instantaneous" | "daily_anchor" (future)
        "residual_kw": residual_kw,
        "fox_captured_at": fox_captured_at,
        "from_cache": True,  # always — this endpoint never refreshes
    }


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the API server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    run_server(host, port)
