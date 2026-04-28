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
from ..mcp_server import build_mcp
from ..scheduler.lp_replay import (
    replay_day as lp_replay_day,
    replay_run as lp_replay_run,
    resolve_run_id_for_date as lp_resolve_run_id_for_date,
    sweep_cadences as lp_sweep_cadences,
)
from . import safeguards
from .middleware import BearerAuthMiddleware
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
from .routers import workbench as workbench_router


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_GIT_SHA_PATH = _PROJECT_ROOT / ".git-sha"


def _bootstrap_openclaw_token() -> str:
    """Ensure ``config.HEM_OPENCLAW_TOKEN`` is set; persist a fresh token on first boot.

    Resolution order: env var (set at construction → already in config), then
    the file at ``HEM_OPENCLAW_TOKEN_FILE``, otherwise generate a new
    ``secrets.token_urlsafe(32)`` and write it (mode 0640). The file lives
    under ``data/`` so it survives container restarts via the bind-mounted
    volume; root on the host can read it (mode 0640) and hand it to the
    OpenClaw user.
    """
    if config.HEM_OPENCLAW_TOKEN:
        return config.HEM_OPENCLAW_TOKEN

    token_path = Path(config.HEM_OPENCLAW_TOKEN_FILE)
    if not token_path.is_absolute():
        token_path = Path.cwd() / token_path

    if token_path.is_file():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            config.HEM_OPENCLAW_TOKEN = existing
            return existing

    import secrets
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token + "\n", encoding="utf-8")
    try:
        token_path.chmod(0o640)
    except OSError:
        logger.debug("Could not chmod %s to 0640 (continuing)", token_path)
    config.HEM_OPENCLAW_TOKEN = token
    logger.info("OpenClaw MCP token generated at %s", token_path)
    return token


# Build the FastMCP server once at import. ``streamable_http_app()`` lazily
# creates the StreamableHTTPSessionManager; we then need to enter
# ``session_manager.run()`` from the FastAPI lifespan below — Starlette does
# NOT propagate sub-app lifespans through ``app.mount()``, so without this
# the task group inside the session manager is never started and the first
# request to /mcp/ fails with "Task group is not initialized".
_mcp_instance = build_mcp()
_mcp_http_app = _mcp_instance.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_openclaw_token()
    await asyncio.to_thread(db.init_db)
    # Prune append-only history tables so the DB doesn't grow unbounded.
    # Non-fatal — deletion failures are logged internally and the service
    # starts regardless.
    try:
        pruned = await asyncio.to_thread(db.prune_history_tables)
        if any(v > 0 for v in pruned.values()):
            logger.info("retention prune: %s", pruned)
    except Exception:
        logger.warning("history-table prune on startup failed", exc_info=True)
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
    async with _mcp_instance.session_manager.run():
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
app.include_router(workbench_router.router)

# Mount the FastMCP streamable-HTTP transport at /mcp, guarded by a bearer
# token. Replaces the legacy stdio subprocess (`bin/mcp`) for OpenClaw in
# production: OpenClaw connects via HTTP MCP transport using the token
# bootstrapped by the lifespan and persisted under data/.openclaw-token.
# The 57 tools registered by build_mcp() are unchanged.
app.mount(
    "/mcp",
    BearerAuthMiddleware(_mcp_http_app, token=lambda: config.HEM_OPENCLAW_TOKEN),
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# auto_reload picks up template edits without a service restart — important
# because cockpit iteration shouldn't require systemctl restart. Set on the
# Jinja env directly (Starlette's Jinja2Templates ctor doesn't expose it).
templates.env.auto_reload = True
templates.env.cache = {}

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    # StaticFiles reads from disk on each request — JS/CSS edits land instantly
    # on `git pull`. To beat browser cache, templates can append the file mtime
    # via the ``static_v`` Jinja global below.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _static_v(rel_path: str) -> int:
    """Cache-busting query-string version: file mtime as int.

    Templates use it like ``<script src="/static/js/cockpit.js?v={{ static_v('js/cockpit.js') }}">``
    so a `git pull` of just static/templates flips the URL → browser fetches
    the new bytes immediately (no service restart, no hard-refresh needed).
    """
    fp = STATIC_DIR / rel_path
    try:
        return int(fp.stat().st_mtime)
    except FileNotFoundError:
        return 0


templates.env.globals["static_v"] = _static_v


def _layout_context() -> dict:
    """Inject layout-wide context into every TemplateResponse — mode badge etc.

    Read directly from runtime_settings/config so each page render shows the
    truth at request time (no caching). Cheap (~3 dict lookups).
    """
    from .. import runtime_settings as rts
    try:
        daikin_mode = config.DAIKIN_CONTROL_MODE
    except Exception:
        daikin_mode = "unknown"
    try:
        require_sim = (rts.get_setting("REQUIRE_SIMULATION_ID") or "false").strip().lower() == "true"
    except Exception:
        require_sim = False
    return {
        "daikin_control_mode": daikin_mode,
        "require_simulation_id": require_sim,
    }


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
    return templates.TemplateResponse(
        request, "cockpit.html", {"active_page": "cockpit", **_layout_context()}
    )


@app.get("/history", response_class=HTMLResponse)
async def web_history(request: Request):
    """Phase 4: History replay — same hero-style layout as Cockpit but frozen
    at a chosen past moment. Reads from the LP snapshot tables (Phase 0) so
    every LP run's inputs, per-slot decision, and plan-vs-actual delta can be
    replayed for any moment we have data for.
    """
    return templates.TemplateResponse(
        request, "history.html", {"active_page": "history", **_layout_context()}
    )


@app.get("/forecast", response_class=HTMLResponse)
async def web_forecast(request: Request):
    """Phase 3: dedicated Forecast tab showing everything the next LP solve
    will see — prices, weather forecast, initial state (with sources),
    thresholds, config snapshot, and tomorrow's tariff preview when
    Octopus has published it. Answers "what will the LP do next?".
    """
    return templates.TemplateResponse(
        request, "forecast.html", {"active_page": "forecast", **_layout_context()}
    )


@app.get("/insights", response_class=HTMLResponse)
async def web_insights(request: Request):
    return templates.TemplateResponse(
        request, "insights.html", {"active_page": "insights", **_layout_context()}
    )


@app.get("/plan")
async def web_plan_redirect():
    """v10.2: Plan tab replaced by Workbench. Redirect old bookmarks."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/workbench", status_code=301)


@app.get("/workbench", response_class=HTMLResponse)
async def web_workbench(request: Request):
    return templates.TemplateResponse(
        request, "workbench.html", {"active_page": "workbench", **_layout_context()}
    )


@app.get("/settings", response_class=HTMLResponse)
async def web_settings(request: Request):
    return templates.TemplateResponse(
        request, "settings.html", {"active_page": "settings", **_layout_context()}
    )


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
    sha = "unknown"
    try:
        if _GIT_SHA_PATH.is_file():
            sha = _GIT_SHA_PATH.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        pass
    return {
        "status": "ok",
        "version": app.version,
        "revision": sha,
        "mcp_token_present": bool(config.HEM_OPENCLAW_TOKEN),
    }


@app.get("/api/v1/system/timezone")
async def system_timezone():
    """Return the timezone the planner + cockpit should display times in.

    ``planner_tz`` is ``config.BULLETPROOF_TIMEZONE`` (used for comfort
    windows, MPC cron firing, Octopus fetch cron, load-profile hour-of-day
    binning). ``plan_push_tz`` is fixed to UTC because the nightly plan-push
    cron is UTC-anchored so the first dispatches of each new plan land on a
    fresh Daikin quota day — this is not configurable and must stay that way.
    ``now_utc`` / ``now_local`` let the frontend cross-check its own clock.
    """
    from zoneinfo import ZoneInfo
    tz_name = config.BULLETPROOF_TIMEZONE or "Europe/London"
    now_utc = datetime.now(UTC)
    try:
        now_local = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        # Fall back to UTC if the config string doesn't resolve.
        now_local = now_utc
        tz_name = "UTC"
    return {
        "planner_tz": tz_name,
        "plan_push_tz": "UTC",
        "now_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "now_local": now_local.isoformat(),
    }


@app.get("/api/v1/cockpit/now")
async def cockpit_now():
    """One-call aggregator for the cockpit's "where are we now?" hero panel.

    Never issues cloud calls — reads from in-memory caches, SQLite, and
    runtime_settings only. Replaces four parallel GETs previously fanned out by
    cockpit.js (foxess/status + daikin/status + agile/today +
    optimization/status) so the hero panel renders from a single coherent
    snapshot instead of four independently-timed fetches.

    Freshness per source (agile / fox / daikin / plan) is surfaced so the
    cockpit can render per-source refresh chips with real ages.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo

    now = _dt.now(_UTC)

    def _age(iso: str | None) -> float | None:
        if not iso:
            return None
        try:
            dt = _dt.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception:
            return None
        return round((now - dt).total_seconds(), 1)

    # --- Agile tariff (SQLite only) ------------------------------------------
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    export_tariff = (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + _td(days=1)
    import_rows = db.get_rates_for_period(tariff, day_start, day_end) if tariff else []
    export_rows = db.get_rates_for_period(export_tariff, day_start, day_end) if export_tariff else []

    def _price_at(rows: list, t: _dt) -> tuple[float | None, str | None, str | None]:
        iso_t = t.isoformat().replace("+00:00", "Z")
        for r in rows:
            if r["valid_from"] <= iso_t < r["valid_to"]:
                return float(r["value_inc_vat"]), r["valid_from"], r["valid_to"]
        return None, None, None

    cur_import_p, slot_from, slot_to = _price_at(import_rows, now)
    cur_export_p, _, _ = _price_at(export_rows, now)

    # Fetch timestamp of the Agile cache — AgileRateCache.fetched_at_utc when
    # available; fall back to the optimizer-snapshot's last successful Octopus
    # fetch state.
    agile_cache_fetched_at = None
    try:
        cache = get_agile_cache()
        if cache and cache.fetched_at_utc:
            agile_cache_fetched_at = cache.fetched_at_utc.isoformat().replace("+00:00", "Z")
    except Exception:
        pass
    if not agile_cache_fetched_at:
        try:
            ofs = db.get_octopus_fetch_state()
            agile_cache_fetched_at = ofs.last_success_at if ofs else None
        except Exception:
            pass

    # --- Fox ESS realtime (in-memory cache, read-only) -----------------------
    fox_block: dict[str, Any] = {
        "soc_pct": None,
        "soc_kwh": None,
        "solar_kw": None,
        "load_kw": None,
        "grid_kw": None,
        "battery_kw": None,
        "fox_mode": None,
    }
    fox_fresh: dict[str, Any] = {"fetched_at_utc": None, "age_s": None, "stale": None}
    try:
        rt = get_cached_realtime()
        fox_block.update({
            "soc_pct": float(rt.soc),
            "soc_kwh": round(float(config.BATTERY_CAPACITY_KWH) * float(rt.soc) / 100.0, 3),
            "solar_kw": round(float(rt.solar_power), 3),
            "load_kw": round(float(rt.load_power), 3),
            "grid_kw": round(float(rt.grid_power), 3),
            "battery_kw": round(float(rt.battery_power), 3),
            "fox_mode": str(rt.work_mode),
        })
    except Exception:
        pass
    try:
        s = get_refresh_stats_extended()
        last_wall = s.get("last_updated_epoch")
        if last_wall:
            iso = _dt.fromtimestamp(last_wall, tz=_UTC).isoformat().replace("+00:00", "Z")
            fox_fresh = {
                "fetched_at_utc": iso,
                "age_s": _age(iso),
                "stale": bool(s.get("stale", False)),
            }
    except Exception:
        pass

    # --- Daikin (cached only; quota-aware) -----------------------------------
    dk_block: dict[str, Any] = {
        "tank_c": None,
        "indoor_c": None,
        "outdoor_c": None,
        "lwt_c": None,
        "control_mode": config.DAIKIN_CONTROL_MODE,
        "mode": None,
    }
    dk_fresh: dict[str, Any] = {"fetched_at_utc": None, "age_s": None, "stale": None}
    try:
        cached = daikin_service.get_cached_devices(allow_refresh=False, actor="cockpit_now")
        if cached.devices:
            d = cached.devices[0]
            tank = getattr(d, "tank_temperature", None)
            room = getattr(getattr(d, "temperature", None), "room_temperature", None)
            outdoor = getattr(d, "outdoor_temperature", None)
            lwt = getattr(d, "leaving_water_temperature", None)
            if tank is not None:
                dk_block["tank_c"] = float(tank)
            if room is not None:
                dk_block["indoor_c"] = float(room)
            if outdoor is not None:
                dk_block["outdoor_c"] = float(outdoor)
            if lwt is not None:
                dk_block["lwt_c"] = float(lwt)
            dk_block["mode"] = getattr(d, "operation_mode", None)
        if cached.fetched_at_wall:
            iso = _dt.fromtimestamp(cached.fetched_at_wall, tz=_UTC).isoformat().replace("+00:00", "Z")
            dk_fresh = {
                "fetched_at_utc": iso,
                "age_s": _age(iso),
                "stale": bool(cached.stale),
            }
    except Exception:
        pass

    # --- Plan (current fox group, last plan timestamp) -----------------------
    plan_block: dict[str, Any] = {
        "current_fox_mode": None,
        "current_slot_utc": slot_from,
        "current_slot_end_utc": slot_to,
        "next_transition_utc": None,
        "next_fox_mode": None,
        "plan_date": None,
    }
    plan_fresh: dict[str, Any] = {"fetched_at_utc": None, "age_s": None, "stale": None}
    try:
        fox_state = db.get_latest_fox_schedule_state() or {}
        groups_json = fox_state.get("groups_json") or "[]"
        import json as _json
        groups = _json.loads(groups_json) if groups_json else []
        # Fox groups are HH:MM cycles in UTC (Fox hardware clock). Match "now".
        now_hm = (now.hour, now.minute)
        def _start(g: dict) -> tuple[int, int]:
            return int(g.get("startHour", 0)), int(g.get("startMinute", 0))
        def _end(g: dict) -> tuple[int, int]:
            return int(g.get("endHour", 0)), int(g.get("endMinute", 0))
        cur = next(
            (g for g in groups
             if _start(g) <= now_hm < _end(g)
             or (_start(g) > _end(g) and (now_hm >= _start(g) or now_hm < _end(g)))),
            None,
        )
        upcoming = sorted(
            (g for g in groups if _start(g) > now_hm),
            key=lambda g: _start(g),
        )
        if cur:
            plan_block["current_fox_mode"] = cur.get("workMode")
        if upcoming:
            nxt = upcoming[0]
            plan_block["next_fox_mode"] = nxt.get("workMode")
            plan_block["next_transition_utc"] = now.replace(
                hour=_start(nxt)[0], minute=_start(nxt)[1], second=0, microsecond=0
            ).isoformat().replace("+00:00", "Z")
        uploaded_at = fox_state.get("uploaded_at")
        if uploaded_at:
            plan_fresh = {
                "fetched_at_utc": uploaded_at,
                "age_s": _age(uploaded_at),
                "stale": False,
            }
    except Exception:
        pass
    try:
        ofs = db.get_octopus_fetch_state()
        if ofs and ofs.last_success_at and not plan_fresh.get("fetched_at_utc"):
            plan_fresh = {
                "fetched_at_utc": ofs.last_success_at,
                "age_s": _age(ofs.last_success_at),
                "stale": False,
            }
    except Exception:
        pass

    # --- Thresholds from daily_targets (LP's classification for today) -------
    tz_name = config.BULLETPROOF_TIMEZONE or "Europe/London"
    try:
        tz = ZoneInfo(tz_name)
        plan_date_local = _dt.now(tz).date().isoformat()
    except Exception:
        plan_date_local = now.date().isoformat()
    plan_block["plan_date"] = plan_date_local
    try:
        tgt = db.get_daily_target(plan_date_local)
    except Exception:
        tgt = None
    thresholds = {
        "cheap_p": (tgt or {}).get("cheap_threshold"),
        "peak_p": (tgt or {}).get("peak_threshold"),
    }

    # --- Compose ------------------------------------------------------------
    return {
        "now_utc": now.isoformat().replace("+00:00", "Z"),
        "planner_tz": tz_name,
        "current_slot": {
            "t_utc": slot_from,
            "t_end_utc": slot_to,
            "price_import_p": cur_import_p,
            "price_export_p": cur_export_p,
            "fox_mode": plan_block["current_fox_mode"],
        },
        "next_transition": {
            "t_utc": plan_block["next_transition_utc"],
            "new_fox_mode": plan_block["next_fox_mode"],
        },
        "state": {
            **fox_block,
            **{
                "tank_c": dk_block["tank_c"],
                "indoor_c": dk_block["indoor_c"],
                "outdoor_c": dk_block["outdoor_c"],
                "lwt_c": dk_block["lwt_c"],
                "daikin_mode": dk_block["mode"],
            },
        },
        "freshness": {
            "agile": {
                "fetched_at_utc": agile_cache_fetched_at,
                "age_s": _age(agile_cache_fetched_at),
                "stale": None,
            },
            "fox": fox_fresh,
            "daikin": dk_fresh,
            "plan": plan_fresh,
        },
        "thresholds": thresholds,
        "modes": {
            "daikin_control_mode": config.DAIKIN_CONTROL_MODE,
            "optimization_preset": config.OPTIMIZATION_PRESET,
            "energy_strategy_mode": config.ENERGY_STRATEGY_MODE,
        },
        "plan_date": plan_block["plan_date"],
    }


@app.get("/api/v1/attribution/day")
async def attribution_day(date: str | None = None):
    """Per-day energy attribution donut — Home Assistant Energy Dashboard idiom.

    "Your solar went to: self-used X%, stored in battery Y%, exported Z%."
    Reads from ``fox_energy_daily`` (populated daily by the Fox rollup job)
    so it's historical-only — today's running totals are not included
    until the rollup fires.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    if date is None:
        date = (_dt.now(_UTC).date() - _td(days=1)).isoformat()
    row = db.get_fox_energy_daily_by_date(date)
    if not row:
        return {
            "date": date,
            "available": False,
            "solar_kwh": None,
            "load_kwh": None,
            "import_kwh": None,
            "export_kwh": None,
            "charge_kwh": None,
            "discharge_kwh": None,
            "shares": None,
        }
    solar = float(row.get("solar_kwh") or 0.0)
    exp = float(row.get("export_kwh") or 0.0)
    chg = float(row.get("charge_kwh") or 0.0)
    imp = float(row.get("import_kwh") or 0.0)
    load = float(row.get("load_kwh") or 0.0)
    dis = float(row.get("discharge_kwh") or 0.0)

    # Solar destinations: export is clear. Charge comes partly from solar +
    # partly from imported cheap slots; approximate with max(0, chg - imp)
    # since a slot can't charge from grid AND solar simultaneously in meaningful
    # excess. Self-use = solar - export - solar_to_battery.
    solar_to_battery = max(0.0, chg - imp)
    solar_to_export = min(solar, exp)
    solar_self_use = max(0.0, solar - solar_to_export - solar_to_battery)
    share_total = solar_self_use + solar_to_battery + solar_to_export
    shares = None
    if share_total > 0:
        shares = {
            "self_use_pct": round(100.0 * solar_self_use / share_total, 1),
            "battery_pct": round(100.0 * solar_to_battery / share_total, 1),
            "export_pct": round(100.0 * solar_to_export / share_total, 1),
        }

    return {
        "date": date,
        "available": True,
        "solar_kwh": solar,
        "load_kwh": load,
        "import_kwh": imp,
        "export_kwh": exp,
        "charge_kwh": chg,
        "discharge_kwh": dis,
        "shares": shares,
    }


@app.get("/api/v1/cockpit/at")
async def cockpit_at(when: str):
    """Reconstruct the same-shape payload as ``/cockpit/now`` but frozen at a
    past moment. Reads exclusively from SQLite snapshots (Phase 0) —
    ``execution_log`` for realised state, ``lp_solution_snapshot`` for the
    LP's per-slot decision at the time, ``lp_inputs_snapshot`` for its
    inputs, ``agile_rates`` for price-at-time, and ``meteo_forecast_history``
    for the forecast version the LP run actually saw.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    try:
        # Accept both 2026-04-24T14:00:00Z and 2026-04-24T14:00:00+00:00.
        w = _dt.fromisoformat(when.replace("Z", "+00:00"))
        if w.tzinfo is None:
            w = w.replace(tzinfo=_UTC)
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid ISO datetime: {when!r}")
    when_iso = w.isoformat()

    tz_name = config.BULLETPROOF_TIMEZONE or "Europe/London"

    # --- Pick the LP run that was active at `when` ---------------------------
    run_id = db.find_run_for_time(when_iso)
    lp_inputs = db.get_lp_inputs(run_id) if run_id is not None else None
    lp_slots = db.get_lp_solution_slots(run_id) if run_id is not None else []

    # --- Price at the moment, from agile_rates -------------------------------
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    export_tariff = (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
    import_rows = []
    export_rows = []
    if tariff:
        from datetime import timedelta as _td
        import_rows = db.get_rates_for_period(tariff, w - _td(hours=1), w + _td(hours=1))
    if export_tariff:
        from datetime import timedelta as _td
        export_rows = db.get_rates_for_period(export_tariff, w - _td(hours=1), w + _td(hours=1))

    def _price_at(rows: list) -> tuple[float | None, str | None, str | None]:
        iso_w = when_iso.replace("+00:00", "Z")
        for r in rows:
            if r["valid_from"] <= iso_w < r["valid_to"]:
                return float(r["value_inc_vat"]), r["valid_from"], r["valid_to"]
        return None, None, None

    price_i, slot_from, slot_to = _price_at(import_rows)
    price_e, _, _ = _price_at(export_rows)

    # --- Realised state from execution_log around `when` ---------------------
    realised: dict[str, Any] = {}
    try:
        conn = db.get_connection()
        try:
            cur = conn.execute(
                """SELECT * FROM execution_log
                   WHERE timestamp <= ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (when_iso,),
            )
            row = cur.fetchone()
            realised = dict(row) if row else {}
        finally:
            conn.close()
    except Exception:
        realised = {}

    state_block = {
        "soc_pct": realised.get("soc_percent"),
        "soc_kwh": round(float(realised["soc_percent"]) / 100.0 * float(config.BATTERY_CAPACITY_KWH), 3)
            if realised.get("soc_percent") is not None else None,
        "solar_kw": None,       # Not tracked per-slot in execution_log
        "load_kw": realised.get("consumption_kwh") is not None
            and round(float(realised["consumption_kwh"]) / 0.5, 3) or None,
        "grid_kw": None,
        "battery_kw": None,
        "fox_mode": realised.get("fox_mode"),
        "tank_c": realised.get("daikin_tank_temp"),
        "indoor_c": realised.get("daikin_room_temp"),
        "outdoor_c": realised.get("daikin_outdoor_temp"),
        "lwt_c": realised.get("daikin_lwt"),
        "daikin_mode": None,
    }

    # --- LP plan for the slot containing `when` ------------------------------
    planned_slot: dict[str, Any] | None = None
    if lp_slots and slot_from:
        iso_from = slot_from.replace("+00:00", "Z")
        for s in lp_slots:
            if s.get("slot_time_utc") in (iso_from, slot_from):
                planned_slot = dict(s)
                break

    return {
        "when_utc": when_iso.replace("+00:00", "Z"),
        "planner_tz": tz_name,
        "source": {
            "run_id": run_id,
            "lp_run_at_utc": (lp_inputs or {}).get("run_at_utc"),
            "execution_log_timestamp": realised.get("timestamp"),
        },
        "current_slot": {
            "t_utc": slot_from,
            "t_end_utc": slot_to,
            "price_import_p": price_i,
            "price_export_p": price_e,
            "fox_mode": realised.get("fox_mode"),
        },
        "state": state_block,
        "planned_slot": planned_slot,  # LP's decision for that slot, or None
        "lp_inputs": lp_inputs,        # Full inputs row at solve time, or None
        "slot_kind": realised.get("slot_kind"),
    }


@app.get("/api/v1/recent-triggers")
async def recent_triggers(limit: int = 20, include_heartbeat: bool = False):
    """Recent manual/scheduler action_log rows for the cockpit's
    'Recent triggers' strip.

    Filters out ``heartbeat`` and ``notification`` triggers by default so
    the user sees meaningful events (manual MCP writes, plan proposes,
    scheduler crons). Rows written via :func:`db.log_action_timed` carry
    ``started_at`` / ``completed_at`` / ``duration_ms`` / ``actor``; legacy
    fast-path rows have nulls in those fields (still rendered, just
    without the duration chip).
    """
    exclude = ["notification"] if include_heartbeat else ["heartbeat", "notification"]
    try:
        rows = db.get_recent_triggers(limit=int(limit), exclude_triggers=exclude)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"recent-triggers: {e}")
    return {"rows": rows, "count": len(rows)}


@app.get("/api/v1/optimization/inputs")
async def optimization_inputs(horizon_hours: int | None = None):
    """Everything the next LP solve will see, merged from caches + SQLite.

    The Forecast tab reads this in one call to answer "what will the LP do
    next, and against which numbers?" without triggering cloud fetches.

    Cache-only contract — mirror of the ``/cockpit/now`` discipline. Weather
    rows come from ``meteo_forecast`` (latest-per-slot, written by the last
    LP solve). Import prices from ``agile_rates``; export prices from
    ``agile_export_rates`` (Octopus Outgoing Agile, mirroring what the LP
    itself reads in :func:`scheduler.optimizer._build_export_price_line`).
    Initial state via ``read_lp_initial_state(allow_daikin_refresh=False)``
    so no Daikin quota is burned on a page load.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo
    from ..scheduler.lp_initial_state import read_lp_initial_state
    from ..scheduler import lp_overrides

    hz = int(horizon_hours or config.LP_HORIZON_HOURS)
    hz = max(4, min(48, hz))
    now = _dt.now(_UTC)
    day_start = now.replace(minute=0, second=0, microsecond=0)
    window_end = day_start + _td(hours=hz)

    # --- Tariff rates (import + export) -------------------------------------
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    export_tariff = (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
    import_rows = db.get_rates_for_period(tariff, day_start, window_end) if tariff else []
    # Outgoing rates live in agile_export_rates, NOT agile_rates — the export
    # tariff code is never written to agile_rates, so get_rates_for_period
    # would return [] regardless of whether Octopus has the data. Use the
    # export-specific helper so the dashboard sees the same per-slot prices
    # the LP objective uses.
    export_rows = (
        db.get_agile_export_rates_in_range(day_start.isoformat(), window_end.isoformat())
        if export_tariff
        else []
    )

    # --- Meteo forecast (latest-per-slot, in the plan window) ---------------
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """SELECT slot_time, temp_c, solar_w_m2 FROM meteo_forecast
               WHERE slot_time >= ? AND slot_time < ?
               ORDER BY slot_time""",
            (day_start.isoformat(), window_end.isoformat()),
        )
        meteo_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # --- Base-load hour-of-day profile --------------------------------------
    profile_limit = int(getattr(config, "LP_LOAD_PROFILE_SLOTS", 2016))
    try:
        # Half-hour granularity (#175) + Daikin physics subtracted (#179) so the LP
        # energy balance doesn't double-count Daikin (base_load + e_dhw + e_space
        # would otherwise sum twice the heat-pump draw).
        load_profile = db.half_hourly_residual_load_profile_kwh()
    except Exception:
        load_profile = {}
    try:
        fox_mean = db.mean_fox_load_kwh_per_slot(limit=60)
    except Exception:
        fox_mean = None
    try:
        flat = fox_mean if fox_mean is not None else db.mean_consumption_kwh_from_execution_logs(limit=profile_limit)
    except Exception:
        flat = 0.4

    # --- Initial state (quota-safe: no Daikin refresh) ----------------------
    try:
        initial = read_lp_initial_state(None, allow_daikin_refresh=False)
        initial_block = {
            "soc_kwh": round(float(initial.soc_kwh), 3),
            "soc_pct": round(float(initial.soc_kwh) / float(config.BATTERY_CAPACITY_KWH) * 100.0, 1) if config.BATTERY_CAPACITY_KWH else None,
            "tank_c": round(float(initial.tank_temp_c), 2),
            "indoor_c": round(float(initial.indoor_temp_c), 2),
            "soc_source": getattr(initial, "soc_source", "unknown"),
            "tank_source": getattr(initial, "tank_source", "unknown"),
            "indoor_source": getattr(initial, "indoor_source", "unknown"),
        }
    except Exception as e:
        initial_block = {
            "soc_kwh": None, "soc_pct": None, "tank_c": None, "indoor_c": None,
            "soc_source": f"error:{e}", "tank_source": "unknown", "indoor_source": "unknown",
        }

    try:
        micro_climate_offset = float(db.get_micro_climate_offset_c(config.DAIKIN_MICRO_CLIMATE_LOOKBACK))
    except Exception:
        micro_climate_offset = 0.0

    # --- Thresholds from today's daily_targets (LP-derived) -----------------
    tz_name = config.BULLETPROOF_TIMEZONE or "Europe/London"
    try:
        tz = ZoneInfo(tz_name)
        plan_date_local = _dt.now(tz).date().isoformat()
    except Exception:
        plan_date_local = now.date().isoformat()
    try:
        tgt = db.get_daily_target(plan_date_local) or {}
    except Exception:
        tgt = {}

    # --- Weather interpolation helpers --------------------------------------
    # meteo_forecast is stored hourly (Open-Meteo only returns hourly data);
    # half-hour slots landed between two hours have no row. The LP itself
    # interpolates via weather._interp_hourly_scalar so it solves with
    # continuous temp/solar values, but this endpoint previously did exact
    # ISO lookups and returned None for the HH:30 slots — confusing users who
    # assumed the LP was blind on those slots (it isn't). Mirror the LP's
    # linear interpolation so the Forecast tab shows the same continuous
    # series the solver actually consumes.
    parsed_meteo: list[tuple[_dt, float | None, float | None]] = []
    for r in meteo_rows:
        try:
            ts = _dt.fromisoformat((r["slot_time"] or "").replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_UTC)
            parsed_meteo.append((
                ts,
                float(r["temp_c"]) if r.get("temp_c") is not None else None,
                float(r["solar_w_m2"]) if r.get("solar_w_m2") is not None else None,
            ))
        except Exception:
            continue
    parsed_meteo.sort(key=lambda x: x[0])

    def _interp(t: _dt, idx: int) -> float | None:
        """Linear interpolate meteo value (idx=1 temp_c, idx=2 solar_w_m2) at *t*.

        Falls back to carry-last / carry-first when *t* is outside the
        available range — mirrors weather._interp_hourly_scalar but returns
        None instead of a magic default so the caller can decide whether to
        surface "—" or a derived value.
        """
        if not parsed_meteo:
            return None
        before = None
        after = None
        for row in parsed_meteo:
            if row[0] <= t:
                before = row
            elif after is None and row[0] > t:
                after = row
                break
        if before is None:
            return parsed_meteo[0][idx]
        if after is None:
            return before[idx]
        va, vb = before[idx], after[idx]
        if va is None and vb is None:
            return None
        if va is None:
            return vb
        if vb is None:
            return va
        ta, tb = before[0].timestamp(), after[0].timestamp()
        if tb <= ta:
            return va
        w = (t.timestamp() - ta) / (tb - ta)
        return va + w * (vb - va)

    # --- Per-slot merged view (prices + weather + base_load, for the horizon)
    slots_out: list[dict[str, Any]] = []
    slot_len = _td(minutes=30)
    import_by_valid_from: dict[str, float] = {r["valid_from"]: float(r["value_inc_vat"]) for r in import_rows}
    export_by_valid_from: dict[str, float] = {r["valid_from"]: float(r["value_inc_vat"]) for r in export_rows}
    t = day_start
    while t < window_end:
        iso = t.isoformat().replace("+00:00", "+00:00")  # preserve offset
        # Agile rates land in SQLite with ±Z suffix; try both.
        iso_candidates = {iso, iso.replace("+00:00", "Z")}
        price_i = None
        price_e = None
        for k in iso_candidates:
            if k in import_by_valid_from:
                price_i = import_by_valid_from[k]; break
        for k in iso_candidates:
            if k in export_by_valid_from:
                price_e = export_by_valid_from[k]; break
        # Interpolated temp + solar — matches what the LP actually sees.
        temp_c = _interp(t, 1)
        solar = _interp(t, 2)
        try:
            local_dt = t.astimezone(ZoneInfo(tz_name))
            hr_local = local_dt.hour
            min_local = 30 if local_dt.minute >= 30 else 0
        except Exception:
            hr_local = t.hour
            min_local = 30 if t.minute >= 30 else 0
        # S10.8 (#175): half-hour bucket lookup; falls back to flat mean.
        bl = float(load_profile.get((hr_local, min_local), flat))
        slots_out.append({
            "t_utc": iso.replace("+00:00", "Z"),
            "price_import_p": price_i,
            "price_export_p": price_e,
            "temp_c": float(temp_c) if temp_c is not None else None,
            "solar_w_m2": float(solar) if solar is not None else None,
            "base_load_kwh": round(bl, 4),
        })
        t = t + slot_len

    # --- Config snapshot — the knobs the LP reads at solve time -------------
    cfg_snap = {
        "LP_HORIZON_HOURS": int(config.LP_HORIZON_HOURS),
        "BATTERY_CAPACITY_KWH": float(config.BATTERY_CAPACITY_KWH),
        "MIN_SOC_RESERVE_PERCENT": float(config.MIN_SOC_RESERVE_PERCENT),
        "BATTERY_RT_EFFICIENCY": float(config.BATTERY_RT_EFFICIENCY),
        "MAX_INVERTER_KW": float(config.MAX_INVERTER_KW),
        "DAIKIN_CONTROL_MODE": str(config.DAIKIN_CONTROL_MODE),
        "OPTIMIZATION_PRESET": str(config.OPTIMIZATION_PRESET),
        "ENERGY_STRATEGY_MODE": str(config.ENERGY_STRATEGY_MODE),
        "DHW_TEMP_COMFORT_C": float(config.DHW_TEMP_COMFORT_C),
        "DHW_TEMP_NORMAL_C": float(config.DHW_TEMP_NORMAL_C),
        "INDOOR_SETPOINT_C": float(config.INDOOR_SETPOINT_C),
        "LP_PRICE_QUANTIZE_PENCE": float(getattr(config, "LP_PRICE_QUANTIZE_PENCE", 0.0)),
        "LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA": float(getattr(config, "LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA", 0.0)),
        "LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA": float(getattr(config, "LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA", 0.0)),
    }

    # --- Tomorrow's rates available? (Octopus publishes ~16:00 UTC) ---------
    tomorrow_start = day_start + _td(days=1)
    tomorrow_end = tomorrow_start + _td(days=1)
    tomorrow_rows = db.get_rates_for_period(tariff, tomorrow_start, tomorrow_end) if tariff else []
    tomorrow_available = len(tomorrow_rows) >= 4

    return {
        "now_utc": now.isoformat().replace("+00:00", "Z"),
        "planner_tz": tz_name,
        "horizon_hours": hz,
        "slots": slots_out,
        "initial": initial_block,
        "micro_climate_offset_c": micro_climate_offset,
        "thresholds": {
            "cheap_p": (tgt or {}).get("cheap_threshold"),
            "peak_p": (tgt or {}).get("peak_threshold"),
        },
        "config_snapshot": cfg_snap,
        "target_vwap_pence": (tgt or {}).get("target_vwap"),
        "estimated_cost_pence": (tgt or {}).get("estimated_cost_pence"),
        "strategy_summary": (tgt or {}).get("strategy_summary"),
        "tomorrow_rates_available": tomorrow_available,
        "workbench_schema": lp_overrides.schema_for_response(),
    }


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


# ---------------------------------------------------------------------------
# LP replay / backtest harness
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field as _PField  # local import — keeps top-of-file lean


class LpReplayRequest(BaseModel):
    run_id: int | None = _PField(None, description="Specific optimizer_log id to replay")
    date: str | None = _PField(None, description="YYYY-MM-DD; first run of the local day is used")
    mode: str = _PField("honest", description="'honest' (snapshotted config) or 'forward' (current config)")


class LpReplayDayRequest(BaseModel):
    date: str = _PField(..., description="YYYY-MM-DD local date")
    cadence: str = _PField("original", description="'original'|'first'|'first:N'|'stride:K'|'subset:0,2,5'")
    mode: str = _PField("honest", description="'honest' or 'forward'")


class LpSweepRequest(BaseModel):
    date: str = _PField(..., description="YYYY-MM-DD local date")
    cadences: list[str] | None = _PField(None, description="Defaults to ['original','first','first:2','stride:2']")
    mode: str = _PField("honest", description="'honest' or 'forward'")


def _replay_dict(r) -> dict:
    """Strip the internal _replayed_plan handle from a result dataclass before serialising."""
    import dataclasses
    d = dataclasses.asdict(r)
    if "runs" in d:
        for run in d.get("runs", []):
            run.pop("_replayed_plan", None)
    if "rows" in d:
        for row in d.get("rows", []):
            for run in row.get("runs", []):
                run.pop("_replayed_plan", None)
    d.pop("_replayed_plan", None)
    return d


def _validate_replay_mode(mode: str) -> None:
    if mode not in ("honest", "forward"):
        raise HTTPException(status_code=400, detail=f"mode must be 'honest' or 'forward', got {mode!r}")


@app.post("/api/v1/lp/replay")
async def lp_replay_endpoint(req: LpReplayRequest):
    """Replay a single past LP run on its frozen snapshot inputs.

    Provide either ``run_id`` (specific solve) or ``date`` (first run of that
    local day). See :func:`src.scheduler.lp_replay.replay_run` for the
    honest/forward mode semantics. Read-only — no DB / Fox / Daikin writes.
    """
    _validate_replay_mode(req.mode)
    if req.run_id is None and not req.date:
        raise HTTPException(status_code=400, detail="must provide run_id or date")
    if req.run_id is not None and req.date:
        raise HTTPException(status_code=400, detail="provide either run_id or date, not both")

    run_id = req.run_id
    if run_id is None:
        try:
            from datetime import date as _d
            _d.fromisoformat(req.date)  # type: ignore[arg-type]
        except ValueError:
            raise HTTPException(status_code=400, detail=f"date must be YYYY-MM-DD, got {req.date!r}")
        resolved = await asyncio.to_thread(lp_resolve_run_id_for_date, req.date)  # type: ignore[arg-type]
        if resolved is None:
            raise HTTPException(status_code=404, detail=f"no optimizer_log row for date={req.date}")
        run_id = resolved

    result = await asyncio.to_thread(lp_replay_run, run_id, mode=req.mode)
    return _replay_dict(result)


@app.post("/api/v1/lp/replay-day")
async def lp_replay_day_endpoint(req: LpReplayDayRequest):
    """Chain-replay all (or a subset of) the day's optimizer runs."""
    _validate_replay_mode(req.mode)
    try:
        from datetime import date as _d
        _d.fromisoformat(req.date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"date must be YYYY-MM-DD, got {req.date!r}")

    result = await asyncio.to_thread(lp_replay_day, req.date, cadence=req.cadence, mode=req.mode)
    return _replay_dict(result)


@app.post("/api/v1/lp/sweep-cadences")
async def lp_sweep_cadences_endpoint(req: LpSweepRequest):
    """Sweep multiple cadences across one local date and rank by savings vs SVT."""
    _validate_replay_mode(req.mode)
    try:
        from datetime import date as _d
        _d.fromisoformat(req.date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"date must be YYYY-MM-DD, got {req.date!r}")
    cadences = req.cadences if req.cadences else ["original", "first", "first:2", "stride:2"]
    result = await asyncio.to_thread(lp_sweep_cadences, req.date, cadences=cadences, mode=req.mode)
    return _replay_dict(result)


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


@app.post("/api/v1/optimization/rollback", response_model=RollbackResponse)
async def optimization_rollback(snapshot_id: str | None = None, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Restore a config snapshot (latest by default)."""
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
                "Re-run POST /api/v1/optimization/propose to refresh the plan."
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


@app.post("/api/v1/optimization/auto-approve/simulate")
async def optimization_auto_approve_simulate(req: SetAutoApproveRequest):
    return _register_diff(_diffs.diff_optimization_auto_approve(req.enabled))


# --- Settings + scheduler simulate routes -----------------------------------

@app.put("/api/v1/settings/{key}/simulate")
async def settings_simulate(key: str, body: dict):
    return _register_diff(_diffs.diff_setting_change(key, body.get("value")))


@app.post("/api/v1/settings/batch/simulate")
async def settings_batch_simulate(body: dict):
    """Simulate N settings changes in one diff. Body: ``{changes: {KEY: value, ...}}``."""
    changes = body.get("changes") or {}
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(status_code=400, detail="changes must be a non-empty object")
    return _register_diff(_diffs.diff_settings_batch(changes))


@app.post("/api/v1/settings/batch")
async def settings_batch_apply(
    body: dict,
    x_simulation_id: str | None = Header(None, alias="X-Simulation-Id"),
):
    """Apply N settings changes atomically (best-effort rollback on failure).

    Pair with ``/simulate`` above; pass ``X-Simulation-Id``. If any individual
    ``set_setting`` fails mid-batch, we re-apply the previous values for the
    keys that already succeeded, then surface a 409 with the per-key error.
    """
    _enforce_simulation_id("settings.batch", x_simulation_id)
    changes = body.get("changes") or {}
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(status_code=400, detail="changes must be a non-empty object")
    from .. import runtime_settings as rts

    applied: list[tuple[str, Any]] = []  # (key, prior_value) for rollback
    results: list[dict] = []
    for key, value in changes.items():
        try:
            prior = rts.get_setting(key)
        except Exception:
            prior = None
        try:
            canonical = rts.set_setting(key, value, actor="api_batch")
            applied.append((key, prior))
            results.append({"key": key, "ok": True, "value": canonical})
        except Exception as exc:
            # Rollback succeeded keys
            rollback_errors: list[dict] = []
            for ok_key, ok_prior in applied:
                try:
                    if ok_prior is not None:
                        rts.set_setting(ok_key, ok_prior, actor="api_batch_rollback")
                    else:
                        rts.delete_setting(ok_key, actor="api_batch_rollback")
                except Exception as rb_exc:
                    rollback_errors.append({"key": ok_key, "error": str(rb_exc)})
            results.append({"key": key, "ok": False, "error": str(exc)})
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "BatchPartialFailure",
                    "failed_at_key": key,
                    "results": results,
                    "rollback_errors": rollback_errors,
                },
            )
    return {"ok": True, "results": results}


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

@app.get("/api/v1/energy/freshness")
async def energy_freshness(start: str, end: str):
    """Per-date ``fetched_at`` metadata for cached Fox daily rows in the given range.

    The Insights UI reads this alongside ``/api/v1/energy/period`` to render the
    "data from X min ago · ⟳" badge and decide whether to offer a refresh button
    per date. Pure SQLite read; never hits Fox.

    Args:
        start: ISO date, e.g. ``2025-07-01``.
        end: ISO date, e.g. ``2025-07-31``.
    """
    from datetime import datetime as _dt
    from .. import db as _db

    rows = _db.get_fox_energy_daily_range(start, end)
    now = _dt.now(UTC)

    def _age_seconds(fetched_at: str | None) -> int | None:
        if not fetched_at:
            return None
        try:
            t = _dt.fromisoformat(fetched_at.replace("Z", "+00:00"))
            return int((now - t).total_seconds())
        except Exception:
            return None

    return {
        "start": start,
        "end": end,
        "now_utc": now.isoformat().replace("+00:00", "Z"),
        "rows": [
            {
                "date": r["date"],
                "fetched_at": r.get("fetched_at"),
                "age_seconds": _age_seconds(r.get("fetched_at")),
            }
            for r in rows
        ],
    }


@app.post("/api/v1/energy/refresh/simulate")
async def energy_refresh_simulate(body: dict):
    dates = body.get("dates") or []
    if not isinstance(dates, list):
        raise HTTPException(status_code=400, detail="dates must be a list")
    return _register_diff(_diffs.diff_energy_refresh(dates))


@app.post("/api/v1/energy/refresh")
async def energy_refresh(body: dict, x_simulation_id: str | None = Header(None, alias="X-Simulation-Id")):
    """Force a Fox cloud refetch for the listed dates. Burns Fox quota.

    Body: ``{"dates": ["YYYY-MM-DD", ...]}``. Gated by the simulate-confirm
    flow (``/simulate`` pair above). Groups requested dates by month to
    minimise cloud calls (one call per month regardless of how many dates).
    """
    _enforce_simulation_id("energy.refresh", x_simulation_id)
    dates = body.get("dates") or []
    if not isinstance(dates, list) or not dates:
        raise HTTPException(status_code=400, detail="dates must be a non-empty list")
    from datetime import date as _date
    from ..foxess import service as _fox_svc

    months: set[tuple[int, int]] = set()
    for d in dates:
        try:
            dt = _date.fromisoformat(d)
            months.add((dt.year, dt.month))
        except Exception:
            raise HTTPException(status_code=400, detail=f"invalid date: {d!r}")

    refreshed = []
    errors = []
    for (yr, mo) in sorted(months):
        try:
            _fox_svc.ensure_fox_month_cached(yr, mo, force=True)
            refreshed.append({"year": yr, "month": mo})
        except Exception as exc:
            errors.append({"year": yr, "month": mo, "error": str(exc)})
    return {
        "requested_dates": dates,
        "months_refreshed": refreshed,
        "errors": errors,
    }


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


def _classify_tariff_kinds(slots: list[dict]) -> None:
    """Mutate slots in-place adding a ``kind`` ∈ {negative, cheap, standard,
    peak, peak_export}. Pure percentile classification — no PV consideration —
    so it works for arbitrary historic days without solar context.
    """
    if not slots:
        return
    prices = sorted(float(s["p"]) for s in slots)
    n = len(prices)
    q25 = prices[max(0, n // 4 - 1)]
    q75 = prices[min(n - 1, (3 * n) // 4)]
    mean_p = sum(prices) / n
    cheap_thr = min(mean_p * 0.85, q25)
    peak_thr = max(q75, float(config.OPTIMIZATION_PEAK_THRESHOLD_PENCE))
    for s in slots:
        p = float(s["p"])
        if p <= 0:
            s["kind"] = "negative"
        elif p < cheap_thr:
            s["kind"] = "cheap"
        elif p > peak_thr:
            s["kind"] = "peak"
        else:
            s["kind"] = "standard"


@app.get("/api/v1/agile/day")
async def agile_day(date: str):
    """Tariff slots for an arbitrary local day (Europe/London) with kind labels.

    Powers the Insights Day view's tariff strip. Uses
    :func:`db.get_agile_rates_slots_for_local_day` which handles DST
    (returns 46/48/50 slots). Each slot gets a percentile-based ``kind``
    suitable for colour-coding.
    """
    try:
        from datetime import date as _date
        d = _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    from .. import db as _db
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return {"date": date, "tariff_code": None, "slots": []}
    tz = config.BULLETPROOF_TIMEZONE or "Europe/London"
    rows = _db.get_agile_rates_slots_for_local_day(tariff, d, tz_name=tz)
    slots = [
        {
            "valid_from": r["valid_from"],
            "valid_to": r["valid_to"],
            "p": float(r["value_inc_vat"]),
        }
        for r in rows
    ]
    _classify_tariff_kinds(slots)
    return {
        "date": date,
        "tariff_code": tariff,
        "tz": tz,
        "slots": slots,
    }


# --- v10.2 — pattern panels & ML-ready time-series (Insights browser) ---

def _validate_range(start: str, end: str) -> None:
    """Common range validator for /patterns/* and /timeseries endpoints."""
    from datetime import date as _date
    try:
        s = _date.fromisoformat(start)
        e = _date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail="start and end must be YYYY-MM-DD")
    if e < s:
        raise HTTPException(status_code=400, detail="end must be >= start")
    if (e - s).days > 366 * 3:
        raise HTTPException(status_code=400, detail="range too large (max ~3 years)")


@app.get("/api/v1/patterns/hourly")
async def patterns_hourly(start: str, end: str):
    _validate_range(start, end)
    from ..analytics import patterns as _pat
    return _pat.hourly_load_profile_for_range(start, end)


@app.get("/api/v1/patterns/dow")
async def patterns_dow(start: str, end: str):
    _validate_range(start, end)
    from ..analytics import patterns as _pat
    return _pat.dow_load_shape(start, end)


@app.get("/api/v1/patterns/price-distribution")
async def patterns_price_distribution(start: str, end: str):
    _validate_range(start, end)
    from ..analytics import patterns as _pat
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip() or None
    return _pat.cheap_peak_slot_frequency(tariff, start, end)


@app.get("/api/v1/patterns/pv-calibration")
async def patterns_pv_calibration(start: str, end: str):
    _validate_range(start, end)
    from ..analytics import patterns as _pat
    return _pat.pv_forecast_vs_actual(start, end)


_TIMESERIES_METRICS = {"load_kwh", "import_p", "solar_kwh", "daikin_kwh"}


@app.get("/api/v1/timeseries")
async def timeseries(
    metric: str,
    start: str,
    end: str,
    granularity: str = "hour",
):
    """Flat ``[{t, v}]`` for one metric. SQLite-only — never cloud.

    metric ∈ {load_kwh, import_p, solar_kwh, daikin_kwh}.
    granularity ∈ {slot, hour, day} — for daily metrics (solar_kwh, daikin_kwh)
    only ``day`` is supported; load_kwh and import_p support all three.
    """
    if metric not in _TIMESERIES_METRICS:
        raise HTTPException(status_code=400, detail=f"metric must be one of {sorted(_TIMESERIES_METRICS)}")
    if granularity not in ("slot", "hour", "day"):
        raise HTTPException(status_code=400, detail="granularity must be slot|hour|day")
    _validate_range(start, end)

    from datetime import UTC, date as _date, datetime as _dt, timedelta as _td
    from .. import db as _db

    s = _date.fromisoformat(start)
    e = _date.fromisoformat(end)
    s_iso = _dt(s.year, s.month, s.day, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    e_iso = (_dt(e.year, e.month, e.day, tzinfo=UTC) + _td(days=1)).isoformat().replace("+00:00", "Z")

    points: list[dict] = []

    if metric in ("load_kwh", "import_p"):
        # execution_log is the per-tick source for both load and import price
        with _db._lock:
            conn = _db.get_connection()
            try:
                col = "consumption_kwh" if metric == "load_kwh" else "agile_price_pence"
                cur = conn.execute(
                    f"""SELECT timestamp, {col} AS v
                        FROM execution_log
                        WHERE timestamp >= ? AND timestamp < ? AND {col} IS NOT NULL
                        ORDER BY timestamp""",
                    (s_iso, e_iso),
                )
                rows = cur.fetchall()
            finally:
                conn.close()

        if granularity == "slot":
            for r in rows:
                points.append({"t": r["timestamp"], "v": float(r["v"])})
        else:
            # Bucket into hour or day
            buckets: dict[str, list[float]] = {}
            for r in rows:
                ts = r["timestamp"]
                key = ts[:13] if granularity == "hour" else ts[:10]  # YYYY-MM-DDTHH or YYYY-MM-DD
                buckets.setdefault(key, []).append(float(r["v"]))
            for key in sorted(buckets):
                vals = buckets[key]
                if metric == "load_kwh":
                    v = sum(vals)  # kWh aggregates by sum
                else:
                    v = sum(vals) / len(vals)  # price aggregates by mean
                points.append({"t": key, "v": round(v, 4)})

    elif metric == "solar_kwh":
        if granularity != "day":
            raise HTTPException(status_code=400, detail="solar_kwh supports granularity=day only")
        for r in _db.get_fox_energy_daily_range(s.isoformat(), e.isoformat()):
            v = r.get("solar_kwh")
            if v is None:
                continue
            points.append({"t": r["date"], "v": round(float(v), 3)})

    elif metric == "daikin_kwh":
        if granularity != "day":
            raise HTTPException(status_code=400, detail="daikin_kwh supports granularity=day only")
        for r in _db.get_daikin_consumption_daily_range(s.isoformat(), e.isoformat()):
            v = r.get("kwh_total")
            if v is None:
                continue
            points.append({"t": r["date"], "v": round(float(v), 3)})

    return {
        "metric": metric,
        "granularity": granularity,
        "start": start,
        "end": end,
        "count": len(points),
        "points": points,
    }


@app.get("/api/v1/execution/today")
async def execution_today(date: str | None = None):
    """Per-30-min-slot realised cost data for plan-vs-actual view.

    The execution_log table is written by the heartbeat tick (~every 2 min),
    so we aggregate ticks within each 30-min slot boundary:
      - consumption_kwh: SUM of per-tick energy (real Fox load × interval)
      - agile_price_pence: same across the slot, take any
      - daikin_outdoor_temp: median across ticks
    Then derive cost = sum_kwh × price; Daikin share from physics
    (get_daikin_heating_kw at slot's outdoor temp).

    Quota-safe: SQLite reads only.

    Pass ``date=YYYY-MM-DD`` to view a past UTC day; default is today.
    Used by both the legacy Plan tab (today) and the v10.2 Insights Day view
    (arbitrary historic days).

    NOTE: pre-v10.1 historical rows used a self-referential constant for
    consumption_kwh — those slots will show flat values. New ticks (after
    the heartbeat fix shipped) record real Fox load × interval.
    """
    from datetime import UTC, datetime, timedelta
    from .. import db as _db

    if date:
        try:
            from datetime import date as _date
            d = _date.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        day_start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    else:
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start.replace(hour=23, minute=59, second=59, microsecond=999999)

    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT timestamp, consumption_kwh, agile_price_pence,
                          svt_shadow_price_pence,
                          soc_percent, fox_mode, daikin_lwt, daikin_outdoor_temp,
                          slot_kind
                   FROM execution_log
                   WHERE timestamp >= ? AND timestamp <= ?
                   ORDER BY timestamp""",
                (day_start.isoformat().replace("+00:00", "Z"),
                 day_end.isoformat().replace("+00:00", "Z")),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    # Bucket rows into 30-min slots
    def _slot_floor(iso: str) -> datetime:
        # Tolerate both Z and +00:00
        s = iso.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        d = d.astimezone(UTC)
        return d.replace(minute=(d.minute // 30) * 30, second=0, microsecond=0)

    buckets: dict[str, list[dict]] = {}
    for r in rows:
        slot = _slot_floor(r["timestamp"]).isoformat().replace("+00:00", "Z")
        buckets.setdefault(slot, []).append(r)

    from ..physics import get_daikin_heating_kw
    SLOT_HOURS = 0.5
    slots = []
    total_cost = 0.0
    total_svt = 0.0
    total_load = 0.0
    total_daikin_kwh = 0.0

    for slot_iso in sorted(buckets.keys()):
        ticks = buckets[slot_iso]
        # Sum consumption across ticks in the slot (real per-tick from heartbeat)
        load_kwh = sum(float(t.get("consumption_kwh") or 0.0) for t in ticks)
        # Tariff is constant across the slot; take from any tick (prefer non-null)
        agile_p = next((float(t["agile_price_pence"]) for t in ticks
                        if t.get("agile_price_pence") is not None), None)
        svt_rate = next((float(t["svt_shadow_price_pence"]) for t in ticks
                         if t.get("svt_shadow_price_pence") is not None), None)
        # Median outdoor temp across the slot
        out_vals = [float(t["daikin_outdoor_temp"]) for t in ticks
                    if t.get("daikin_outdoor_temp") is not None]
        outdoor = sorted(out_vals)[len(out_vals) // 2] if out_vals else None
        # Daikin physics estimate for this slot
        daikin_kw = float(get_daikin_heating_kw(outdoor)) if outdoor is not None else 0.0
        daikin_kwh = min(daikin_kw * SLOT_HOURS, load_kwh)  # can't exceed total load
        residual_kwh = max(0.0, load_kwh - daikin_kwh)
        # Costs: re-derive from the slot's totals (don't trust per-tick fakes)
        cost = load_kwh * agile_p if agile_p is not None else 0.0
        svt_cost = load_kwh * svt_rate if svt_rate is not None else 0.0
        cost_daikin = cost * (daikin_kwh / load_kwh) if load_kwh > 0 else 0.0
        cost_residual = cost - cost_daikin
        # slot_kind: take whatever the heartbeat decided last for this slot
        slot_kind = next((t["slot_kind"] for t in reversed(ticks) if t.get("slot_kind")), None)
        last = ticks[-1]
        slots.append({
            "slot_utc": slot_iso,
            "slot_kind": slot_kind,
            "agile_p": agile_p,
            "consumption_kwh": round(load_kwh, 3),
            "daikin_kwh_est": round(daikin_kwh, 3),
            "residual_kwh": round(residual_kwh, 3),
            "cost_realised_p": round(cost, 2),
            "cost_daikin_p": round(cost_daikin, 2),
            "cost_residual_p": round(cost_residual, 2),
            "cost_svt_p": round(svt_cost, 2),
            "delta_vs_svt_p": round(cost - svt_cost, 2),
            "soc_percent": last.get("soc_percent"),
            "fox_mode": last.get("fox_mode"),
            "daikin_outdoor_c": outdoor,
            "daikin_lwt_c": last.get("daikin_lwt"),
            "_tick_count": len(ticks),
        })
        total_cost += cost
        total_svt += svt_cost
        total_load += load_kwh
        total_daikin_kwh += daikin_kwh

    return {
        "date": day_start.date().isoformat(),
        "data_quality_note": (
            "Per-slot consumption is the sum of heartbeat-tick energy from real Fox "
            "load_power readings (v10.1+). Slots from before the v10.1 heartbeat fix "
            "show a self-referential constant — totals are still ~correct but per-slot "
            "values from the morning may look unnaturally smooth."
        ),
        "slots": slots,
        "totals": {
            "load_kwh": round(total_load, 2),
            "daikin_kwh_est": round(total_daikin_kwh, 2),
            "residual_kwh_est": round(max(0.0, total_load - total_daikin_kwh), 2),
            "cost_realised_p": round(total_cost, 2),
            "cost_svt_p": round(total_svt, 2),
            "delta_vs_svt_p": round(total_cost - total_svt, 2),
            "daikin_share_pct": round(100 * total_daikin_kwh / total_load, 1) if total_load else 0.0,
        },
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
    # Prefer Fox service cache (RealTimeData with .load_power). Pass a huge TTL
    # so the call NEVER triggers a cloud refresh — quota-safe by construction.
    try:
        from ..foxess import service as _fox_svc
        snap = _fox_svc.get_cached_realtime(max_age_seconds=86_400)
        if snap is not None:
            house_total_kw = float(getattr(snap, "load_power", None)) if getattr(snap, "load_power", None) is not None else None
        # Pull timestamp from refresh-stats (RealTimeData itself has no updated_at).
        try:
            stats = _fox_svc.get_refresh_stats()
            fox_captured_at = stats.get("last_updated_iso") or stats.get("last_updated_epoch")
        except Exception:
            pass
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
    # Daikin's outdoor_temp is fetched fresh by client.get_status() each call,
    # NOT cached on DaikinDevice. So we read from the SQLite daikin_telemetry
    # table — populated by the heartbeat tick. Quota-safe (no cloud call).
    try:
        tel = _db.get_latest_daikin_telemetry()
        if tel and tel.get("outdoor_temp_c") is not None:
            daikin_outdoor_c = float(tel["outdoor_temp_c"])
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
