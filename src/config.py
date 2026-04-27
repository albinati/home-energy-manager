"""Config loader — reads from .env or environment variables."""
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def env_int_at_least(name: str, default: int, minimum: int) -> int:
    """Read an int env var, falling back to ``default``, then clamp to ``minimum``.
    Use this for values where going below the minimum creates a footgun (e.g. a
    0-second grace window that causes a self-DoS). Kept as a free function so
    tests can exercise the clamp without reloading the config module."""
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def parse_cop_curve_csv(s: str) -> list[tuple[float, float]]:
    """Parse ``"-7:1.8,2:2.6,7:3.1"`` into sorted ``[(T_C, COP), ...]``."""
    out: list[tuple[float, float]] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$", part)
        if m:
            out.append((float(m.group(1)), float(m.group(2))))
    out.sort(key=lambda x: x[0])
    return out


def cop_at_temperature(curve: list[tuple[float, float]], t_c: float) -> float:
    """Linear interpolation of COP vs outdoor temperature (°C)."""
    if not curve:
        return 3.0
    if t_c <= curve[0][0]:
        return curve[0][1]
    if t_c >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, c0 = curve[i]
        t1, c1 = curve[i + 1]
        if t0 <= t_c <= t1:
            if t1 == t0:
                return c0
            return c0 + (c1 - c0) * (t_c - t0) / (t1 - t0)
    return curve[-1][1]


class Config:
    # Fox ESS — Option A: Open API key (API Management on foxesscloud.com)
    # FOX_API_KEY and FOXESS_API_KEY are both accepted per docs
    FOXESS_API_KEY: str = (
        (os.getenv("FOX_API_KEY") or os.getenv("FOXESS_API_KEY") or os.getenv("FOXESS_PRIVATE_TOKEN") or "").strip()
    )
    # Fox ESS — Option B: username/password (unofficial, works for endUser accounts)
    FOXESS_USERNAME: str = (os.getenv("FOXESS_USERNAME") or "").strip()
    FOXESS_PASSWORD: str = (os.getenv("FOXESS_PASSWORD") or "").strip()
    # Fox ESS — device serial (always required); INVERTER_SERIAL_NUMBER accepted as fallback
    FOXESS_DEVICE_SN: str = (
        (os.getenv("FOXESS_DEVICE_SN") or os.getenv("INVERTER_SERIAL_NUMBER") or "").strip()
    )
    # Datalogger / WiFi stick serial (often required as `sn` for Scheduler V3 and scheduler flag Open API).
    DATALOGGER_SERIAL_NUMBER: str = (os.getenv("DATALOGGER_SERIAL_NUMBER") or "").strip()
    # Optional override for scheduler endpoints only (defaults to DATALOGGER_SERIAL_NUMBER when set).
    FOXESS_SCHEDULER_SN: str = (os.getenv("FOXESS_SCHEDULER_SN") or "").strip()
    FOXESS_ALERT_LOW_SOC: int = int(os.getenv("FOXESS_ALERT_LOW_SOC", "10"))

    # Daikin
    DAIKIN_CLIENT_ID: str = os.getenv("DAIKIN_CLIENT_ID", "")
    DAIKIN_CLIENT_SECRET: str = os.getenv("DAIKIN_CLIENT_SECRET", "")
    DAIKIN_REDIRECT_URI: str = os.getenv("DAIKIN_REDIRECT_URI", "http://localhost:8080/callback")
    DAIKIN_TOKEN_FILE: Path = Path(os.getenv("DAIKIN_TOKEN_FILE", ".daikin-tokens.json"))
    # Proactively refresh the access token this many seconds before OAuth expiry (token endpoint only).
    DAIKIN_ACCESS_REFRESH_LEEWAY_SECONDS: int = int(
        os.getenv("DAIKIN_ACCESS_REFRESH_LEEWAY_SECONDS", "600")
    )
    # Minimum wall time between refresh_token HTTP calls (avoids bursts if many workers wake at once).
    DAIKIN_TOKEN_REFRESH_MIN_INTERVAL_SECONDS: int = int(
        os.getenv("DAIKIN_TOKEN_REFRESH_MIN_INTERVAL_SECONDS", "120")
    )
    # Retries for HTTP 429 from Onecta (respects Retry-After when present).
    DAIKIN_HTTP_429_MAX_RETRIES: int = int(os.getenv("DAIKIN_HTTP_429_MAX_RETRIES", "3"))
    # Circuit breaker for dead refresh tokens. After N consecutive failures
    # of refresh_tokens() we stop hammering the Onecta token endpoint for
    # the cooldown window; a single critical notification fires so the user
    # knows to re-auth. Reset on any successful refresh.
    DAIKIN_AUTH_CIRCUIT_THRESHOLD: int = int(os.getenv("DAIKIN_AUTH_CIRCUIT_THRESHOLD", "3"))
    DAIKIN_AUTH_CIRCUIT_COOLDOWN_SECONDS: int = int(
        os.getenv("DAIKIN_AUTH_CIRCUIT_COOLDOWN_SECONDS", "900")
    )
    DAIKIN_BASE_URL: str = "https://api.onecta.daikineurope.com/v1"
    # OIDC endpoints (docs: https://developer.cloud.daikineurope.com/docs/84e709f1-9d33-47e1-a93c-7f5cb8b8f12b)
    # Override via env if Daikin documents different URLs (e.g. via developer portal).
    DAIKIN_AUTH_URL: str = os.getenv(
        "DAIKIN_AUTH_URL", "https://idp.onecta.daikineurope.com/v1/oidc/authorize"
    )
    DAIKIN_TOKEN_URL: str = os.getenv(
        "DAIKIN_TOKEN_URL", "https://idp.onecta.daikineurope.com/v1/oidc/token"
    )
    DAIKIN_ALERT_TEMP_DEVIATION: float = float(os.getenv("DAIKIN_ALERT_TEMP_DEVIATION", "2"))

    # ── OpenClaw Gateway hooks (user-facing notifications) ───────────────────
    # All outbound notifications POST to OPENCLAW_HOOKS_URL (e.g. /hooks/agent).
    # Set OPENCLAW_NOTIFY_ENABLED=false to disable delivery (stdout + action_log still run).
    OPENCLAW_NOTIFY_ENABLED: bool = os.getenv("OPENCLAW_NOTIFY_ENABLED", "true").lower() in ("true", "1", "yes")

    # Default channel + target (fallback when severity-specific not set).
    # target: Telegram chat ID, @username, Discord channel/user, etc.
    OPENCLAW_NOTIFY_CHANNEL: str = os.getenv("OPENCLAW_NOTIFY_CHANNEL", "telegram")
    OPENCLAW_NOTIFY_TARGET: str = (os.getenv("OPENCLAW_NOTIFY_TARGET") or "").strip()

    # Optional severity-specific overrides — set these to route critical alerts and
    # daily reports to different destinations. Falls back to OPENCLAW_NOTIFY_TARGET.
    OPENCLAW_NOTIFY_TARGET_CRITICAL: str = (os.getenv("OPENCLAW_NOTIFY_TARGET_CRITICAL") or "").strip()
    OPENCLAW_NOTIFY_CHANNEL_CRITICAL: str = (os.getenv("OPENCLAW_NOTIFY_CHANNEL_CRITICAL") or "").strip()
    OPENCLAW_NOTIFY_TARGET_REPORTS: str = (os.getenv("OPENCLAW_NOTIFY_TARGET_REPORTS") or "").strip()
    OPENCLAW_NOTIFY_CHANNEL_REPORTS: str = (os.getenv("OPENCLAW_NOTIFY_CHANNEL_REPORTS") or "").strip()

    # Full URL e.g. http://127.0.0.1:18789/hooks/agent — see OpenClaw Webhooks docs.
    OPENCLAW_HOOKS_URL: str = (os.getenv("OPENCLAW_HOOKS_URL") or "").strip()
    OPENCLAW_HOOKS_TOKEN: str = (os.getenv("OPENCLAW_HOOKS_TOKEN") or "").strip()
    OPENCLAW_HOOKS_TIMEOUT_SECONDS: int = int(os.getenv("OPENCLAW_HOOKS_TIMEOUT_SECONDS", "30"))
    # Optional: route hook to a named agent (Gateway hooks.allowedAgentIds must allow it).
    OPENCLAW_HOOKS_AGENT_ID: str = (os.getenv("OPENCLAW_HOOKS_AGENT_ID") or "").strip()
    # Shown inside the hook ``message`` so the agent can fetch the full plan (avoid huge payloads).
    OPENCLAW_INTERNAL_API_BASE_URL: str = (
        os.getenv("OPENCLAW_INTERNAL_API_BASE_URL") or "http://127.0.0.1:8000"
    ).strip().rstrip("/")

    # Energy providers (for tariff tracking and cost analysis)
    OCTOPUS_API_KEY: str = os.getenv("OCTOPUS_API_KEY", "")
    OCTOPUS_ACCOUNT_NUMBER: str = os.getenv("OCTOPUS_ACCOUNT_NUMBER", "")
    # MPANs and meter serials — MPAN_1/MPAN_2 kept for backward compat.
    # MPAN_IMPORT / MPAN_EXPORT are set either explicitly or auto-detected from account endpoint.
    OCTOPUS_MPAN_1: str = (os.getenv("OCTOPUS_MPAN_1") or "").strip()
    OCTOPUS_MPAN_2: str = (os.getenv("OCTOPUS_MPAN_2") or "").strip()
    OCTOPUS_METER_SN_1: str = (os.getenv("OCTOPUS_METER_SN_1") or "").strip()
    OCTOPUS_METER_SN_2: str = (os.getenv("OCTOPUS_METER_SN_2") or "").strip()
    # Resolved import/export MPAN and serial — set explicitly or populated by auto_detect_mpan_roles()
    OCTOPUS_MPAN_IMPORT: str = (os.getenv("OCTOPUS_MPAN_IMPORT") or os.getenv("OCTOPUS_MPAN_1") or "").strip()
    OCTOPUS_MPAN_EXPORT: str = (os.getenv("OCTOPUS_MPAN_EXPORT") or os.getenv("OCTOPUS_MPAN_2") or "").strip()
    OCTOPUS_METER_SERIAL_IMPORT: str = (os.getenv("OCTOPUS_METER_SERIAL_IMPORT") or os.getenv("OCTOPUS_METER_SN_1") or "").strip()
    OCTOPUS_METER_SERIAL_EXPORT: str = (os.getenv("OCTOPUS_METER_SERIAL_EXPORT") or os.getenv("OCTOPUS_METER_SN_2") or "").strip()
    BRITISH_GAS_API_KEY: str = os.getenv("BRITISH_GAS_API_KEY", "")

    # API server
    # Default 127.0.0.1: in containerised deployments compose maps explicit interfaces
    # (loopback + Tailscale) to the container, so the in-container listener can stay
    # on loopback. Set API_HOST=0.0.0.0 only when running natively without a fronting
    # network namespace.
    API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))

    # OpenClaw: when True, POST /api/v1/openclaw/execute returns 403 (recommendation-only; apply via dashboard/CLI)
    OPENCLAW_READ_ONLY: bool = os.getenv("OPENCLAW_READ_ONLY", "true").lower() in ("true", "1", "yes")

    # OpenClaw MCP HTTP transport — bearer token guarding /mcp.
    # Resolution: env wins; otherwise the lifespan in src.api.main reads
    # HEM_OPENCLAW_TOKEN_FILE and writes it (urlsafe-32) on first boot.
    HEM_OPENCLAW_TOKEN_FILE: str = os.getenv(
        "HEM_OPENCLAW_TOKEN_FILE", "data/.openclaw-token"
    )
    HEM_OPENCLAW_TOKEN: str = os.getenv("HEM_OPENCLAW_TOKEN", "").strip()

    # AI Assistant (optional; if not set, rule-based suggestions only)
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Legacy OpenAI — kept for backward compat; not used by default
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    AI_ASSISTANT_PROVIDER: str = os.getenv("AI_ASSISTANT_PROVIDER", "anthropic")
    AI_ASSISTANT_MODEL: str = os.getenv("AI_ASSISTANT_MODEL", "claude-haiku-4-5")

    # Manual tariff for cost-aware suggestions (p/kWh) when no energy provider is connected
    MANUAL_TARIFF_IMPORT_PENCE: float = float(os.getenv("MANUAL_TARIFF_IMPORT_PENCE", "0"))
    MANUAL_TARIFF_EXPORT_PENCE: float = float(os.getenv("MANUAL_TARIFF_EXPORT_PENCE", "0"))
    MANUAL_STANDING_CHARGE_PENCE_PER_DAY: float = float(
        os.getenv("MANUAL_STANDING_CHARGE_PENCE_PER_DAY", "0")
    )

    # Gas comparison (solar + heat pump vs gas): gas price p/kWh, boiler efficiency (e.g. 0.9)
    GAS_PRICE_PENCE_PER_KWH: float = float(os.getenv("GAS_PRICE_PENCE_PER_KWH", "0"))
    GAS_BOILER_EFFICIENCY: float = float(os.getenv("GAS_BOILER_EFFICIENCY", "0.9"))
    HEAT_PUMP_COP_ESTIMATE: float = float(os.getenv("HEAT_PUMP_COP_ESTIMATE", "2.8"))
    # Heating estimate: share of total load assumed to be heating (e.g. 0.4 = 40%) in heating season
    HEATING_LOAD_SHARE: float = float(os.getenv("HEATING_LOAD_SHARE", "0.4"))

    # Weather (optional): for heating analytics — degree-days, cost per °C (Open-Meteo Historical, no key)
    # WEATHER_LAT + WEATHER_LON moved to runtime_settings (#52) — see @property
    # definitions below. Env vars still honored as the env_default fallback so
    # nothing breaks when the DB row is absent.
    # Degree-day base temp (°C): heating demand assumed proportional to (base - outdoor_mean)
    WEATHER_DEGREE_DAY_BASE_C: float = float(os.getenv("WEATHER_DEGREE_DAY_BASE_C", "18"))
    WEATHER_COLD_THRESHOLD_C: float = float(os.getenv("WEATHER_COLD_THRESHOLD_C", "5"))
    WEATHER_MILD_THRESHOLD_C: float = float(os.getenv("WEATHER_MILD_THRESHOLD_C", "15"))
    WEATHER_FROST_THRESHOLD_C: float = float(os.getenv("WEATHER_FROST_THRESHOLD_C", "2"))

    # Bulletproof engine (SQLite + Scheduler V3 + 2-min heartbeat)
    USE_BULLETPROOF_ENGINE: bool = os.getenv("USE_BULLETPROOF_ENGINE", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    DB_PATH: str = (os.getenv("DB_PATH") or "energy_state.db").strip()
    HEARTBEAT_INTERVAL_SECONDS: int = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "120"))
    SVT_RATE_PENCE: float = float(os.getenv("SVT_RATE_PENCE", "24.50"))
    OPTIMIZATION_PEAK_THRESHOLD_PENCE: float = float(
        os.getenv("OPTIMIZATION_PEAK_THRESHOLD_PENCE", "25")
    )
    OCTOPUS_FETCH_HOUR: int = int(os.getenv("OCTOPUS_FETCH_HOUR", "16"))
    OCTOPUS_FETCH_MINUTE: int = int(os.getenv("OCTOPUS_FETCH_MINUTE", "5"))
    DAILY_BRIEF_HOUR: int = int(os.getenv("DAILY_BRIEF_HOUR", "8"))
    DAILY_BRIEF_MINUTE: int = int(os.getenv("DAILY_BRIEF_MINUTE", "0"))
    BULLETPROOF_TIMEZONE: str = (os.getenv("BULLETPROOF_TIMEZONE") or "Europe/London").strip()

    DHW_TEMP_MAX_C: float = float(os.getenv("DHW_TEMP_MAX_C", "65"))
    # Plunge-only ceiling (≥ DHW_TEMP_COMFORT_C and ≤ DHW_TEMP_MAX_C is allowed only when price < 0).
    # DHW_TEMP_COMFORT_C + DHW_TEMP_NORMAL_C are runtime-tunable via
    # /api/v1/settings (#52) — see the @property definitions below.
    DHW_TEMP_CHEAP_C: float = float(os.getenv("DHW_TEMP_CHEAP_C", "60"))
    # NOTE: DHW_LEGIONELLA_* env vars removed in v10. The Daikin Onecta firmware
    # runs the weekly thermal-shock cycle autonomously (Sunday ~11:00 local) and
    # the LP/dispatch never enforced these bounds. Stale .env entries are silently
    # ignored — Python's os.getenv won't fail on unrecognised variables.

    BATTERY_CAPACITY_KWH: float = float(os.getenv("BATTERY_CAPACITY_KWH", "10"))

    # Fox Scheduler V3 power limits (Watts). Real values live in .env (immutable
    # hardware constraints). Defaults below match the deployed Fox H1-5.0-E-G2 +
    # G98 1φ install — see .env for the SNs and DNO ref.
    # FOX_FORCE_CHARGE_MAX_PWR: AC import ceiling for ForceCharge slots. Used by
    #   the LP as ``fuse_kwh`` source AND by the dispatcher as the per-group
    #   fdPwr clamp. Set to the inverter's nameplate AC rating, NOT the FoxESS
    #   app's configurable range (the app shows the H1 family's full range and
    #   the inverter clamps silently to the model's spec).
    # FOX_FORCE_CHARGE_NORMAL_PWR: fallback for the HEURISTIC backend only (LP
    #   path derives per-slot fdPwr from the MILP grid-import solution).
    # FOX_EXPORT_MAX_PWR: battery → grid ceiling for ForceDischarge slots. Bound
    #   by the DNO connection standard, NOT the inverter nameplate. UK G98 1φ =
    #   16 A × 230 V ≈ 3680 W; G98 3φ or G99 raise this. Decoupled from the
    #   charge ceiling so a model with higher AC import does not breach export
    #   compliance.
    FOX_FORCE_CHARGE_MAX_PWR: int = int(os.getenv("FOX_FORCE_CHARGE_MAX_PWR", "5000"))
    FOX_FORCE_CHARGE_NORMAL_PWR: int = int(os.getenv("FOX_FORCE_CHARGE_NORMAL_PWR", "3000"))
    FOX_EXPORT_MAX_PWR: int = int(os.getenv("FOX_EXPORT_MAX_PWR", "3680"))

    LWT_OFFSET_MAX: float = float(os.getenv("LWT_OFFSET_MAX", "5"))
    LWT_OFFSET_MIN: float = float(os.getenv("LWT_OFFSET_MIN", "-5"))
    LWT_OFFSET_PREHEAT_BOOST: float = float(
        os.getenv("LWT_OFFSET_PREHEAT_BOOST", os.getenv("SCHEDULER_PREHEAT_LWT_BOOST", "5"))
    )

    PV_CAPACITY_KWP: float = float(os.getenv("PV_CAPACITY_KWP", "4.5"))
    PV_SYSTEM_EFFICIENCY: float = float(os.getenv("PV_SYSTEM_EFFICIENCY", "0.85"))
    # Manual override for PV forecast scale (0 = auto-calibrate from Fox history).
    # Set e.g. 0.65 to permanently cap the forecast to 65% of Open-Meteo modelled output.
    # When 0 or unset, compute_pv_calibration_factor() derives it automatically from Fox history.
    PV_FORECAST_SCALE_FACTOR: float = float(os.getenv("PV_FORECAST_SCALE_FACTOR", "0"))

    # Agile scheduler (Daikin ASHP by price)
    SCHEDULER_ENABLED: bool = os.getenv("SCHEDULER_ENABLED", "false").lower() in ("true", "1", "yes")
    OCTOPUS_TARIFF_CODE: str = (os.getenv("OCTOPUS_TARIFF_CODE") or "").strip()
    # Optional: Octopus Agile Export tariff code for SEG export rate fetch.
    # E.g. E-1R-AGILE-OUTGOING-24-10-01-C. Leave blank if not on export tariff.
    OCTOPUS_EXPORT_TARIFF_CODE: str = (os.getenv("OCTOPUS_EXPORT_TARIFF_CODE") or "").strip()
    # Grid Supply Point (GSP) letter A-P for regional tariff lookup.
    # Default H = South East England (London W4). Auto-detected from account if not set.
    OCTOPUS_GSP: str = (os.getenv("OCTOPUS_GSP") or "H").strip().upper()
    # Current tariff product code for baseline comparison. Default: auto-detect from OCTOPUS_TARIFF_CODE
    # or fall back to latest Flexible Octopus. Set explicitly if you know your product code.
    CURRENT_TARIFF_PRODUCT: str = (os.getenv("CURRENT_TARIFF_PRODUCT") or "").strip()
    SCHEDULER_CHEAP_THRESHOLD_PENCE: float = float(os.getenv("SCHEDULER_CHEAP_THRESHOLD_PENCE", "12"))
    SCHEDULER_PEAK_START: str = os.getenv("SCHEDULER_PEAK_START", "16:00")
    SCHEDULER_PEAK_END: str = os.getenv("SCHEDULER_PEAK_END", "19:00")
    SCHEDULER_PREHEAT_LWT_BOOST: float = float(os.getenv("SCHEDULER_PREHEAT_LWT_BOOST", "2"))

    # OPTIMIZATION_PRESET + ENERGY_STRATEGY_MODE are runtime-tunable via
    # /api/v1/settings (#52) — see the @property definitions below.
    # savings_first (default): import/savings focus; peak grid export (force discharge)
    # is allowed whenever cached SoC >= EXPORT_DISCHARGE_MIN_SOC_PERCENT (i.e. the
    # battery is genuinely full from PV). The household-preset gate was removed in
    # favour of trusting the LP's predicted base load + Daikin draw.
    # strict_savings — never schedule peak export discharge (max self-use).
    EXPORT_DISCHARGE_MIN_SOC_PERCENT: float = float(
        os.getenv("EXPORT_DISCHARGE_MIN_SOC_PERCENT", "95")
    )
    EXPORT_DISCHARGE_FLOOR_SOC_PERCENT: int = int(
        os.getenv("EXPORT_DISCHARGE_FLOOR_SOC_PERCENT", "15")
    )
    TARGET_ROOM_TEMP_MIN_C: float = float(os.getenv("TARGET_ROOM_TEMP_MIN_C", "18.0"))
    TARGET_ROOM_TEMP_MAX_C: float = float(os.getenv("TARGET_ROOM_TEMP_MAX_C", "23.0"))
    TARGET_DHW_TEMP_MIN_NORMAL_C: float = float(os.getenv("TARGET_DHW_TEMP_MIN_NORMAL_C", "45.0"))
    TARGET_DHW_TEMP_MIN_GUESTS_C: float = float(os.getenv("TARGET_DHW_TEMP_MIN_GUESTS_C", "48.0"))
    TARGET_DHW_TEMP_MAX_C: float = float(os.getenv("TARGET_DHW_TEMP_MAX_C", "65.0"))
    MIN_SOC_RESERVE_PERCENT: float = float(os.getenv("MIN_SOC_RESERVE_PERCENT", "15"))
    OPTIMIZATION_WATCHDOG_HOUR_LOCAL: int = int(os.getenv("OPTIMIZATION_WATCHDOG_HOUR_LOCAL", "16"))
    OPTIMIZATION_WATCHDOG_MINUTE_LOCAL: int = int(os.getenv("OPTIMIZATION_WATCHDOG_MINUTE_LOCAL", "0"))
    OPTIMIZATION_TIMEZONE: str = (os.getenv("OPTIMIZATION_TIMEZONE") or "Europe/London").strip()
    OPTIMIZATION_CHEAP_THRESHOLD_PENCE: float = float(
        os.getenv("OPTIMIZATION_CHEAP_THRESHOLD_PENCE", os.getenv("SCHEDULER_CHEAP_THRESHOLD_PENCE", "12"))
    )
    OPTIMIZATION_PEAK_START: str = os.getenv("OPTIMIZATION_PEAK_START", os.getenv("SCHEDULER_PEAK_START", "16:00"))
    OPTIMIZATION_PEAK_END: str = os.getenv("OPTIMIZATION_PEAK_END", os.getenv("SCHEDULER_PEAK_END", "19:00"))
    OPTIMIZATION_PREHEAT_LWT_BOOST: float = float(
        os.getenv("OPTIMIZATION_PREHEAT_LWT_BOOST", os.getenv("SCHEDULER_PREHEAT_LWT_BOOST", "2"))
    )
    OPTIMIZATION_LWT_OFFSET_MIN: float = float(os.getenv("OPTIMIZATION_LWT_OFFSET_MIN", "-10"))
    OPTIMIZATION_LWT_OFFSET_MAX: float = float(os.getenv("OPTIMIZATION_LWT_OFFSET_MAX", "5"))
    OPTIMIZATION_DISABLE_WEATHER_REGULATION: bool = os.getenv(
        "OPTIMIZATION_DISABLE_WEATHER_REGULATION", "false"
    ).lower() in ("true", "1", "yes")

    # PuLP MILP optimizer (V8)
    OPTIMIZER_BACKEND: str = (os.getenv("OPTIMIZER_BACKEND") or "lp").strip().lower()
    BATTERY_RT_EFFICIENCY: float = float(os.getenv("BATTERY_RT_EFFICIENCY", "0.92"))
    MAX_INVERTER_KW: float = float(os.getenv("MAX_INVERTER_KW", "6.0"))
    EXPORT_RATE_PENCE: float = float(os.getenv("EXPORT_RATE_PENCE", "15.0"))
    # Rolling-window length for the LP/Fox V3 dispatch. Default 24 h so every
    # replan produces a "now → now + 24 h" schedule; truncated if Agile data
    # ends sooner (Octopus publishes tomorrow ~16:00 UTC).
    # S10.2 (#169): default 48 h. Octopus publishes D+1 prices ~16:00 BST; before
    # that the LP horizon extender (src/scheduler/optimizer.py:_resolve_plan_window)
    # fills the missing tail with median per-hour-of-day priors over the last 28 d.
    # Once real D+1 prices land the next MPC re-solve picks them up. Old default
    # was 24 — myopic to D+1 entirely until ~16:00.
    LP_HORIZON_HOURS: int = int(os.getenv("LP_HORIZON_HOURS", "48"))
    DAIKIN_POWER_BUCKETS_KW: str = (os.getenv("DAIKIN_POWER_BUCKETS_KW") or "0,0.5,1.0,1.5").strip()
    # Heat pump nameplate cap (kW) used in LP HP power bounds.
    DAIKIN_MAX_HP_KW: float = float(os.getenv("DAIKIN_MAX_HP_KW", "2.0"))
    LP_SHOWER_WINDOW_MINUTES: int = int(os.getenv("LP_SHOWER_WINDOW_MINUTES", "60"))
    # V8 LP — solver
    LP_SOLVER: str = (os.getenv("LP_SOLVER") or "highs").strip().lower()  # highs | cbc
    LP_CBC_TIME_LIMIT_SECONDS: int = int(os.getenv("LP_CBC_TIME_LIMIT_SECONDS", "30"))
    LP_HIGHS_TIME_LIMIT_SECONDS: int = int(os.getenv("LP_HIGHS_TIME_LIMIT_SECONDS", "30"))
    LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT: float = float(
        os.getenv("LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT", "100")
    )
    LP_CYCLE_PENALTY_PENCE_PER_KWH: float = float(os.getenv("LP_CYCLE_PENALTY_PENCE_PER_KWH", "0.0001"))
    # Inverter stress cost: piecewise-linear quadratic approximation on battery power per slot.
    # At nominal inverter power (MAX_INVERTER_KW), the penalty equals this value (p/kWh).
    # 0 = disabled. Recommended: 0.05–0.20. Works alongside or instead of TV penalties.
    LP_INVERTER_STRESS_COST_PENCE: float = float(os.getenv("LP_INVERTER_STRESS_COST_PENCE", "0.10"))
    # Segments for piecewise-linear stress approximation (higher = more accurate, slower; 6–10 is good)
    LP_INVERTER_STRESS_SEGMENTS: int = int(os.getenv("LP_INVERTER_STRESS_SEGMENTS", "8"))
    # HP on/off minimum ON time: minimum slots the heat-pump must run once started (anti-cycling)
    LP_HP_MIN_ON_SLOTS: int = int(os.getenv("LP_HP_MIN_ON_SLOTS", "2"))
    # Total-variation penalties (pence per 1 kWh step between adjacent half-hours): discourage rapid
    # battery / HP / import changes — trade a little money for fewer hardware mode switches. 0 = off.
    LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA: float = float(
        os.getenv("LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA", "0")
    )
    LP_HP_POWER_TV_PENALTY_PENCE_PER_KWH_DELTA: float = float(
        os.getenv("LP_HP_POWER_TV_PENALTY_PENCE_PER_KWH_DELTA", "0")
    )
    LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA: float = float(
        os.getenv("LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA", "0")
    )
    # Round each slot price to this grid (pence) before the MILP objective — ignores sub‑grid price noise.
    # 0 = use exact Agile prices. Try 1–3 for less “fidgety” schedules.
    LP_PRICE_QUANTIZE_PENCE: float = float(os.getenv("LP_PRICE_QUANTIZE_PENCE", "0"))
    # Legacy (unused): was “gap bridge” in lp_dispatch; removed in Phase 1 — keep keys so .env
    # does not break. Values are ignored.
    FOX_LP_BRIDGE_GAP_SLOTS: int = int(os.getenv("FOX_LP_BRIDGE_GAP_SLOTS", "2"))
    # Daikin: minimum consecutive slots (half-hours) for a non-standard window to be scheduled.
    # Cheap/negative windows shorter than this are merged forward or dropped to avoid rapid heat-pump
    # cycling.  2 = 1 hour minimum (recommended).  0 = disabled (legacy behaviour, any length).
    DAIKIN_MIN_WINDOW_SLOTS: int = int(os.getenv("DAIKIN_MIN_WINDOW_SLOTS", "2"))
    # Delay between critical Onecta writes (climate power, DHW) so the 3-way valve can settle (#18).
    # 0 = skip sleeps (tests).
    DAIKIN_VALVE_SETTLE_SECONDS: int = int(os.getenv("DAIKIN_VALVE_SETTLE_SECONDS", "10"))
    FOX_LP_BRIDGE_MAX_PRICE_PENCE: float = float(os.getenv("FOX_LP_BRIDGE_MAX_PRICE_PENCE", "0"))
    SOLAR_GAIN_FRACTION: float = float(os.getenv("SOLAR_GAIN_FRACTION", "0.15"))
    COP_DHW_PENALTY: float = float(os.getenv("COP_DHW_PENALTY", "0.5"))
    # Pre-PuLP COP lift (#29): scale COP_curve(T_out) by mult(LWT_supply − T_out). 0 = disabled.
    LP_COP_LIFT_PENALTY_PER_KELVIN: float = float(os.getenv("LP_COP_LIFT_PENALTY_PER_KELVIN", "0"))
    LP_COP_LIFT_REFERENCE_DELTA_K: float = float(os.getenv("LP_COP_LIFT_REFERENCE_DELTA_K", "25.0"))
    LP_COP_LIFT_MIN_MULTIPLIER: float = float(os.getenv("LP_COP_LIFT_MIN_MULTIPLIER", "0.5"))
    LP_COP_DHW_LIFT_SUPPLY_C: float = float(os.getenv("LP_COP_DHW_LIFT_SUPPLY_C", "45.0"))
    # Max leaving-water temp used when estimating space COP lift (aligns with physics LWT cap).
    LP_COP_SPACE_LWT_CEILING_C: float = float(os.getenv("LP_COP_SPACE_LWT_CEILING_C", "50.0"))
    DHW_TANK_LITRES: float = float(os.getenv("DHW_TANK_LITRES", "200"))
    DHW_WATER_CP: float = float(os.getenv("DHW_WATER_CP", "4186"))  # J/(kg·K)
    # CALIBRATION REQUIRED — building envelope + thermal mass for the LP single-zone model.
    # Tune from bills / heat-loss survey / co-heating test; defaults are placeholders.
    BUILDING_UA_W_PER_K: float = float(os.getenv("BUILDING_UA_W_PER_K", "180"))
    # CALIBRATION REQUIRED — effective thermal inertia (kWh/K) driving indoor temperature dynamics.
    BUILDING_THERMAL_MASS_KWH_PER_K: float = float(os.getenv("BUILDING_THERMAL_MASS_KWH_PER_K", "8.0"))
    # INDOOR_SETPOINT_C is runtime-tunable via /api/v1/settings (#52) — see @property below.
    INDOOR_COMFORT_BAND_C: float = float(os.getenv("INDOOR_COMFORT_BAND_C", "1.5"))
    RADIATOR_MAX_KW: float = float(os.getenv("RADIATOR_MAX_KW", "6.0"))
    # Physical climate (weather-compensation) curve from the Daikin panel.
    # Maps outdoor temperature to leaving-water temperature (LWT) via a straight line through
    # two configured points. The API does not expose these; set them from the physical display.
    DAIKIN_WEATHER_CURVE_HIGH_C: float = float(os.getenv("DAIKIN_WEATHER_CURVE_HIGH_C", "18.0"))
    DAIKIN_WEATHER_CURVE_HIGH_LWT_C: float = float(os.getenv("DAIKIN_WEATHER_CURVE_HIGH_LWT_C", "22.0"))
    DAIKIN_WEATHER_CURVE_LOW_C: float = float(os.getenv("DAIKIN_WEATHER_CURVE_LOW_C", "-5.0"))
    DAIKIN_WEATHER_CURVE_LOW_LWT_C: float = float(os.getenv("DAIKIN_WEATHER_CURVE_LOW_LWT_C", "45.0"))
    DAIKIN_WEATHER_CURVE_OFFSET_C: float = float(os.getenv("DAIKIN_WEATHER_CURVE_OFFSET_C", "0.0"))
    # Overnight (unoccupied) indoor temperature floor for the LP building model.
    # Raising this above the default 16 °C creates pre-heat pressure during cheap overnight
    # slots so the LP warms the thermal mass before morning occupancy begins.
    LP_OVERNIGHT_COMFORT_FLOOR_C: float = float(os.getenv("LP_OVERNIGHT_COMFORT_FLOOR_C", "18.0"))
    # Number of recent execution_log rows used to compute the micro-climate offset
    # (mean difference between Daikin outdoor sensor and Open-Meteo forecast).
    DAIKIN_MICRO_CLIMATE_LOOKBACK: int = int(os.getenv("DAIKIN_MICRO_CLIMATE_LOOKBACK", "96"))
    DHW_TANK_UA_W_PER_K: float = float(os.getenv("DHW_TANK_UA_W_PER_K", "2.5"))
    DAIKIN_COP_CURVE_STR: str = os.getenv(
        "DAIKIN_COP_CURVE",
        "-7:1.8,2:2.6,7:3.1,12:3.6,20:4.2",
    )
    LP_OCCUPIED_MORNING_START: str = (os.getenv("LP_OCCUPIED_MORNING_START") or "06:30").strip()
    LP_OCCUPIED_MORNING_END: str = (os.getenv("LP_OCCUPIED_MORNING_END") or "08:30").strip()
    LP_OCCUPIED_EVENING_START: str = (os.getenv("LP_OCCUPIED_EVENING_START") or "17:30").strip()
    LP_OCCUPIED_EVENING_END: str = (os.getenv("LP_OCCUPIED_EVENING_END") or "22:30").strip()
    # Empty string disables the morning-shower DHW floor entirely (family defaults to
    # evening showers — forcing a 43 °C floor at 07:00 triggers expensive morning heating).
    LP_SHOWER_MORNING_LOCAL: str = (os.getenv("LP_SHOWER_MORNING_LOCAL") or "").strip()
    LP_SHOWER_EVENING_LOCAL: str = (os.getenv("LP_SHOWER_EVENING_LOCAL") or "20:00").strip()

    # Terminal SoC constraint: now runtime-tunable via /api/v1/settings — see @property below.
    # Default = 25% of BATTERY_CAPACITY_KWH so each LP run honours an anti-myopia floor at
    # the end of its 24h rolling horizon. Without this, individual LP runs may plan to drain
    # the battery near the boundary, executing those decisions before the next MPC corrects.

    # Model Predictive Control: re-run the LP intra-day with refreshed SoC / tank / forecasts.
    # LP_MPC_HOURS is runtime-tunable via /api/v1/settings (#52); cron jobs are
    # re-registered without a process restart when it changes. See @property below.

    # Whether intra-day MPC re-runs (and the evening fetch) push the updated plan to Fox/Daikin.
    # Default false: compute + log only; the nightly push job dispatches at LP_PLAN_PUSH_HOUR:MINUTE.
    LP_MPC_WRITE_DEVICES: bool = os.getenv("LP_MPC_WRITE_DEVICES", "false").lower() in ("1", "true", "yes")

    # Nightly plan push: UTC wall-clock time to upload tomorrow's LP plan to Fox ESS + Daikin.
    # Anchored to UTC so it lands just after Daikin's daily 200-req quota rollover (midnight UTC).
    # The scheduler forces UTC for this cron regardless of BULLETPROOF_TIMEZONE.
    # LP_PLAN_PUSH_HOUR/MINUTE are runtime-tunable (#52) — see @property below.

    # Load-profile: rolling-window of execution_log slots used for per-hour-of-day load estimation.
    # The flat mean is used when fewer rows are available or when this is 0 (legacy).
    LP_LOAD_PROFILE_SLOTS: int = int(os.getenv("LP_LOAD_PROFILE_SLOTS", "2016"))  # 6 weeks × 48

    # Dynamic MPC replan: when the LP plan exceeds the Fox V3 8-group cap, dispatch
    # only the first 8 windows and schedule a one-shot MPC re-run shortly before the
    # 8th window ends, so the truncated tail is re-planned without precision loss.
    REPLAN_SAFETY_MARGIN_MINUTES: int = int(os.getenv("REPLAN_SAFETY_MARGIN_MINUTES", "15"))
    DYNAMIC_REPLAN_MIN_LEAD_MINUTES: int = int(os.getenv("DYNAMIC_REPLAN_MIN_LEAD_MINUTES", "120"))

    # Drop trivial SelfUse groups (work_mode=SelfUse AND min_soc_on_grid=MIN_SOC_RESERVE_PERCENT)
    # before uploading to the Fox scheduler. The inverter's "Remaining Time Work Mode" is
    # Self-use (confirmed in Fox app screen), so any time window with no scheduler group
    # naturally falls back to that — sending an explicit SelfUse group with the global floor
    # is wasted budget against the 8-group hardware cap. Setting this False reverts to the
    # original behaviour (every LP window becomes a Fox group, including SelfUse gaps).
    FOX_SKIP_TRIVIAL_SELFUSE_GROUPS: bool = os.getenv("FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", "true").lower() in ("1", "true", "yes")

    # Event-driven MPC ("Waze recalculating") — see Epic #73.
    # Kill switch: setting this False disables drift + forecast triggers (cron MPC continues).
    MPC_EVENT_DRIVEN_ENABLED: bool = os.getenv("MPC_EVENT_DRIVEN_ENABLED", "true").lower() in ("1", "true", "yes")
    # Cooldown applied at the entry of bulletproof_mpc_job — guards against rapid-fire
    # back-to-back runs when correlated triggers (drift + forecast + Octopus) arrive together.
    MPC_COOLDOWN_SECONDS: int = int(os.getenv("MPC_COOLDOWN_SECONDS", "300"))
    # SoC drift trigger: real SoC (%) vs the LP-predicted trajectory at "now". Threshold
    # calibrated against 7 d of execution_log: p95 of consecutive-snapshot delta = 14 %, so
    # 15 % filters most noise while catching real divergence. Hysteresis requires the drift
    # to persist across this many consecutive heartbeat ticks (heartbeat = 120 s, so 2 ticks
    # ≈ 4 min sustained — filters single-tick spikes from DHW boost cycles or load surges).
    MPC_DRIFT_SOC_THRESHOLD_PERCENT: float = float(os.getenv("MPC_DRIFT_SOC_THRESHOLD_PERCENT", "15"))
    MPC_DRIFT_HYSTERESIS_TICKS: int = int(os.getenv("MPC_DRIFT_HYSTERESIS_TICKS", "2"))
    # Plan-delta observability: how many hours of overlap between previous and new plan to
    # measure when logging the post-trigger delta. 6 h captures the immediate horizon.
    MPC_PLAN_DELTA_LOOKAHEAD_HOURS: int = int(os.getenv("MPC_PLAN_DELTA_LOOKAHEAD_HOURS", "6"))
    # Forecast revision trigger (Open-Meteo updates ~hourly): how often to re-fetch and
    # compare; how far ahead to compare; what delta is "material enough" to re-plan.
    # MPC_FORECAST_REFRESH_INTERVAL_MINUTES is runtime-tunable (cron_reload=True) — see @property below.
    MPC_FORECAST_DRIFT_LOOKAHEAD_HOURS: int = int(os.getenv("MPC_FORECAST_DRIFT_LOOKAHEAD_HOURS", "6"))
    MPC_FORECAST_DRIFT_SOLAR_KWH_THRESHOLD: float = float(os.getenv("MPC_FORECAST_DRIFT_SOLAR_KWH_THRESHOLD", "2.0"))
    MPC_FORECAST_DRIFT_TEMP_C_THRESHOLD: float = float(os.getenv("MPC_FORECAST_DRIFT_TEMP_C_THRESHOLD", "2.0"))

    @property
    def DAIKIN_COP_CURVE(self) -> list[tuple[float, float]]:
        return parse_cop_curve_csv(self.DAIKIN_COP_CURVE_STR)

    # -- Runtime-tunable knobs (#52). -----------------------------------------
    # Read through ``runtime_settings.get_setting`` so ``config.KNOB`` returns
    # the DB-backed override (or env default) without per-call-site churn.
    # Setters write to an in-memory override dict ``_overrides`` so legacy
    # call-sites that do ``setattr(config, name, value)`` (notably
    # ``simulate_plan`` and existing pytest ``monkeypatch.setattr`` calls)
    # keep working without polluting the DB. To *persist* a new value across
    # restarts, use ``runtime_settings.set_setting`` or ``PUT /api/v1/settings``.
    # Local import in each getter prevents a cycle at module load —
    # runtime_settings imports db which imports config during init.

    _overrides: dict[str, Any] = {}  # class-level: one dict for the singleton

    def _rt_get(self, key: str) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        from .runtime_settings import get_setting
        return get_setting(key)

    def _rt_set(self, key: str, value: Any) -> None:
        self._overrides[key] = value

    @property
    def WEATHER_LAT(self) -> str:
        return str(self._rt_get("WEATHER_LAT"))

    @WEATHER_LAT.setter
    def WEATHER_LAT(self, value: str) -> None:
        self._rt_set("WEATHER_LAT", str(value).strip())

    @property
    def WEATHER_LON(self) -> str:
        return str(self._rt_get("WEATHER_LON"))

    @WEATHER_LON.setter
    def WEATHER_LON(self, value: str) -> None:
        self._rt_set("WEATHER_LON", str(value).strip())

    @property
    def DHW_TEMP_COMFORT_C(self) -> float:
        return float(self._rt_get("DHW_TEMP_COMFORT_C"))

    @DHW_TEMP_COMFORT_C.setter
    def DHW_TEMP_COMFORT_C(self, value: float) -> None:
        self._rt_set("DHW_TEMP_COMFORT_C", float(value))

    @property
    def DHW_TEMP_NORMAL_C(self) -> float:
        return float(self._rt_get("DHW_TEMP_NORMAL_C"))

    @DHW_TEMP_NORMAL_C.setter
    def DHW_TEMP_NORMAL_C(self, value: float) -> None:
        self._rt_set("DHW_TEMP_NORMAL_C", float(value))

    @property
    def INDOOR_SETPOINT_C(self) -> float:
        return float(self._rt_get("INDOOR_SETPOINT_C"))

    @INDOOR_SETPOINT_C.setter
    def INDOOR_SETPOINT_C(self, value: float) -> None:
        self._rt_set("INDOOR_SETPOINT_C", float(value))

    @property
    def OPTIMIZATION_PRESET(self) -> str:
        return str(self._rt_get("OPTIMIZATION_PRESET"))

    @OPTIMIZATION_PRESET.setter
    def OPTIMIZATION_PRESET(self, value: str) -> None:
        self._rt_set("OPTIMIZATION_PRESET", str(value).strip().lower())

    @property
    def ENERGY_STRATEGY_MODE(self) -> str:
        return str(self._rt_get("ENERGY_STRATEGY_MODE"))

    @ENERGY_STRATEGY_MODE.setter
    def ENERGY_STRATEGY_MODE(self, value: str) -> None:
        self._rt_set("ENERGY_STRATEGY_MODE", str(value).strip().lower())

    @property
    def DAIKIN_CONTROL_MODE(self) -> str:
        return str(self._rt_get("DAIKIN_CONTROL_MODE"))

    @DAIKIN_CONTROL_MODE.setter
    def DAIKIN_CONTROL_MODE(self, value: str) -> None:
        self._rt_set("DAIKIN_CONTROL_MODE", str(value).strip().lower())

    @property
    def LP_SOC_FINAL_KWH(self) -> float:
        return float(self._rt_get("LP_SOC_FINAL_KWH"))

    @LP_SOC_FINAL_KWH.setter
    def LP_SOC_FINAL_KWH(self, value: float) -> None:
        self._rt_set("LP_SOC_FINAL_KWH", float(value))

    @property
    def LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH(self) -> float:
        return float(self._rt_get("LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH"))

    @LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH.setter
    def LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH(self, value: float) -> None:
        self._rt_set("LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", float(value))

    @property
    def MPC_FORECAST_REFRESH_INTERVAL_MINUTES(self) -> int:
        return int(self._rt_get("MPC_FORECAST_REFRESH_INTERVAL_MINUTES"))

    @MPC_FORECAST_REFRESH_INTERVAL_MINUTES.setter
    def MPC_FORECAST_REFRESH_INTERVAL_MINUTES(self, value: int) -> None:
        self._rt_set("MPC_FORECAST_REFRESH_INTERVAL_MINUTES", int(value))

    @property
    def PV_TELEMETRY_INTERVAL_MINUTES(self) -> int:
        return int(self._rt_get("PV_TELEMETRY_INTERVAL_MINUTES"))

    @PV_TELEMETRY_INTERVAL_MINUTES.setter
    def PV_TELEMETRY_INTERVAL_MINUTES(self, value: int) -> None:
        self._rt_set("PV_TELEMETRY_INTERVAL_MINUTES", int(value))

    @property
    def PV_CALIBRATION_WINDOW_DAYS(self) -> int:
        return int(self._rt_get("PV_CALIBRATION_WINDOW_DAYS"))

    @PV_CALIBRATION_WINDOW_DAYS.setter
    def PV_CALIBRATION_WINDOW_DAYS(self, value: int) -> None:
        self._rt_set("PV_CALIBRATION_WINDOW_DAYS", int(value))

    @property
    def LP_PLAN_PUSH_HOUR(self) -> int:
        return int(self._rt_get("LP_PLAN_PUSH_HOUR"))

    @LP_PLAN_PUSH_HOUR.setter
    def LP_PLAN_PUSH_HOUR(self, value: int) -> None:
        self._rt_set("LP_PLAN_PUSH_HOUR", int(value))

    @property
    def LP_PLAN_PUSH_MINUTE(self) -> int:
        return int(self._rt_get("LP_PLAN_PUSH_MINUTE"))

    @LP_PLAN_PUSH_MINUTE.setter
    def LP_PLAN_PUSH_MINUTE(self, value: int) -> None:
        self._rt_set("LP_PLAN_PUSH_MINUTE", int(value))

    @property
    def LP_MPC_HOURS(self) -> str:
        """Comma-separated string form — legacy callers (incl. pytest
        ``monkeypatch.setattr(config, 'LP_MPC_HOURS', '6,12,18')``) expect
        this setter shape. The canonical value is ``LP_MPC_HOURS_LIST``."""
        val = self._rt_get("LP_MPC_HOURS")
        if isinstance(val, str):
            return val
        if isinstance(val, (list, tuple)):
            return ",".join(str(int(h)) for h in val)
        return str(val or "")

    @LP_MPC_HOURS.setter
    def LP_MPC_HOURS(self, value: str) -> None:
        self._rt_set("LP_MPC_HOURS", str(value))

    @property
    def LP_MPC_HOURS_LIST(self) -> list[int]:
        """Parsed MPC re-solve hours (local). Runtime-tunable via settings PUT."""
        raw = self.LP_MPC_HOURS
        if not raw:
            return []
        return sorted({int(h.strip()) for h in raw.split(",") if h.strip()})

    # Directory for config snapshots (JSON). Snapshots are saved before any mode transition.
    CONFIG_SNAPSHOT_DIR: str = (os.getenv("CONFIG_SNAPSHOT_DIR") or "data/config_snapshots").strip()

    # Plan consent expiry in seconds (default 60 min). Proposed plans expire if not approved.
    PLAN_CONSENT_EXPIRY_SECONDS: int = int(os.getenv("PLAN_CONSENT_EXPIRY_SECONDS", "3600"))

    # Minimum cooldown between re-plan requests (seconds). Prevents rapid re-planning spam.
    # Set to 0 to disable. Default 300 s (5 min).
    PLAN_REGEN_COOLDOWN_SECONDS: int = int(os.getenv("PLAN_REGEN_COOLDOWN_SECONDS", "300"))

    # Minimum interval between plan_proposed notifications even when the plan hash changes.
    # The plan still upserts and auto-applies; only the Telegram/Discord hook is suppressed
    # inside the window. Default 3600 s (60 min) — cuts MPC re-plan noise. 0 disables.
    PLAN_NOTIFY_MIN_INTERVAL_SECONDS: int = int(
        os.getenv("PLAN_NOTIFY_MIN_INTERVAL_SECONDS", "3600")
    )

    # When True (default), every freshly proposed plan is auto-approved and applied.
    # Disable to require explicit per-plan consent via confirm_plan / reject_plan (MCP)
    # or the dashboard — in that case the PLAN_PROPOSED hook drives approval, and
    # PLAN_APPROVAL_TIMEOUT_SECONDS is the "no-answer → auto-accept" grace window
    # advertised to OpenClaw (Telegram/Discord interactive buttons).
    PLAN_AUTO_APPROVE: bool = os.getenv("PLAN_AUTO_APPROVE", "true").lower() in ("true", "1", "yes")
    PLAN_APPROVAL_TIMEOUT_SECONDS: int = int(os.getenv("PLAN_APPROVAL_TIMEOUT_SECONDS", "300"))

    # ── API Quota & Cache ────────────────────────────────────────────────────
    # Daikin Onecta: soft daily budget (real limit ≈200; we stop at 180 to preserve 10% headroom)
    DAIKIN_DAILY_BUDGET: int = int(os.getenv("DAIKIN_DAILY_BUDGET", "180"))
    # How long to serve device data from cache without refreshing (1800 s = 30 min)
    DAIKIN_DEVICES_CACHE_TTL_SECONDS: int = int(os.getenv("DAIKIN_DEVICES_CACHE_TTL_SECONDS", "1800"))
    # Minimum interval between explicit "force refresh" calls (UI refresh button, CLI --force-refresh)
    DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS: int = int(
        os.getenv("DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS", "1800")
    )
    # Width of the Octopus pre-slot window that allows automatic device refresh (seconds before HH:30/HH:00)
    DAIKIN_SLOT_TRANSITION_WINDOW_SECONDS: int = int(
        os.getenv("DAIKIN_SLOT_TRANSITION_WINDOW_SECONDS", "300")
    )
    # Phase 4.1 — per-caller cache staleness ceilings (seconds) so non-heartbeat paths reuse the cache.
    DAIKIN_LP_INIT_CACHE_MAX_AGE_SECONDS: int = int(
        os.getenv("DAIKIN_LP_INIT_CACHE_MAX_AGE_SECONDS", "600")
    )
    DAIKIN_LEGACY_TICK_CACHE_MAX_AGE_SECONDS: int = int(
        os.getenv("DAIKIN_LEGACY_TICK_CACHE_MAX_AGE_SECONDS", "1200")
    )
    # #55 — how old a live telemetry row can be before the LP wrapper falls back
    # to the physics estimator instead of returning the stale value.
    DAIKIN_TELEMETRY_MAX_STALENESS_SECONDS: int = int(
        os.getenv("DAIKIN_TELEMETRY_MAX_STALENESS_SECONDS", "1800")
    )
    # Phase 4.3 — user-override acceptance: how long after an action row becomes active
    # before divergence from live state counts as a user override (vs our own write echo).
    # Clamped to a 60 s minimum: values lower than a typical Onecta cloud-echo lag cause
    # every freshly-active row to be false-flagged as user-overridden on the first tick,
    # producing a self-DoS where no plan ever executes.
    DAIKIN_OVERRIDE_GRACE_SECONDS: int = env_int_at_least(
        "DAIKIN_OVERRIDE_GRACE_SECONDS", 600, 60
    )
    DAIKIN_OVERRIDE_TOLERANCE_TANK_C: float = float(
        os.getenv("DAIKIN_OVERRIDE_TOLERANCE_TANK_C", "0.6")
    )
    DAIKIN_OVERRIDE_TOLERANCE_LWT_C: float = float(
        os.getenv("DAIKIN_OVERRIDE_TOLERANCE_LWT_C", "0.35")
    )

    # Fox ESS: soft daily budget (real limit ≈1440; we stop at 1200 for 15% headroom)
    FOX_DAILY_BUDGET: int = int(os.getenv("FOX_DAILY_BUDGET", "1200"))
    # Default realtime data TTL — raised from 30 s to 300 s (5 min) to match heartbeat
    FOX_REALTIME_CACHE_TTL_SECONDS: int = int(os.getenv("FOX_REALTIME_CACHE_TTL_SECONDS", "300"))
    # Minimum interval between user-initiated "force refresh" calls
    FOX_FORCE_REFRESH_MIN_INTERVAL_SECONDS: int = int(
        os.getenv("FOX_FORCE_REFRESH_MIN_INTERVAL_SECONDS", "60")
    )
    # 429 retry — mirrors the Daikin pattern (see DAIKIN_HTTP_429_MAX_RETRIES).
    # Fox rate limits more than Daikin (1440/day soft vs Daikin 200/day) but
    # transient 429s happen on bursts (MPC re-solve + heartbeat overlap).
    # Default 2 = at most three attempts total. Cap sleep at
    # FOX_HTTP_429_MAX_SLEEP_SECONDS to avoid hanging (like Daikin's 86400 trap).
    FOX_HTTP_429_MAX_RETRIES: int = int(os.getenv("FOX_HTTP_429_MAX_RETRIES", "2"))
    FOX_HTTP_429_MAX_SLEEP_SECONDS: int = int(os.getenv("FOX_HTTP_429_MAX_SLEEP_SECONDS", "60"))
    # Inter-write delay between scheduler / charge-period / work-mode PATCHes
    # (mirrors TonyM1958/FoxESS-Cloud's 2s pattern). Prevents the 40257
    # "Parameters do not meet expectations" surprise on quick-succession writes.
    FOX_WRITE_INTER_DELAY_SECONDS: float = float(os.getenv("FOX_WRITE_INTER_DELAY_SECONDS", "2.0"))

    # Retention (days) for append-only history tables so the DB doesn't grow
    # unbounded. ADR-004 flagged daikin_telemetry specifically; the Phase 0
    # snapshot tables share the same concern. Pruning runs at startup plus
    # once per day via the scheduler.
    DAIKIN_TELEMETRY_RETENTION_DAYS: int = int(os.getenv("DAIKIN_TELEMETRY_RETENTION_DAYS", "30"))
    METEO_FORECAST_HISTORY_RETENTION_DAYS: int = int(
        os.getenv("METEO_FORECAST_HISTORY_RETENTION_DAYS", "30")
    )
    LP_SNAPSHOT_RETENTION_DAYS: int = int(os.getenv("LP_SNAPSHOT_RETENTION_DAYS", "90"))
    CONFIG_AUDIT_RETENTION_DAYS: int = int(os.getenv("CONFIG_AUDIT_RETENTION_DAYS", "365"))

    def foxess_client_kwargs(self) -> dict:
        """Return the right kwargs for FoxESSClient based on what's configured."""
        if not self.FOXESS_DEVICE_SN:
            raise ValueError("FOXESS_DEVICE_SN is required. Find it in foxesscloud.com → Devices.")
        kwargs = {"device_sn": self.FOXESS_DEVICE_SN}
        # Optional: only if your plant requires module SN in scheduler `deviceSN` (rare).
        if self.FOXESS_SCHEDULER_SN:
            kwargs["scheduler_sn"] = self.FOXESS_SCHEDULER_SN
        if self.FOXESS_API_KEY:
            kwargs["api_key"] = self.FOXESS_API_KEY
        elif self.FOXESS_USERNAME and self.FOXESS_PASSWORD:
            kwargs["username"] = self.FOXESS_USERNAME
            kwargs["password"] = self.FOXESS_PASSWORD
        else:
            raise ValueError(
                "Fox ESS auth not configured.\n"
                "Set either FOXESS_API_KEY (or FOXESS_PRIVATE_TOKEN) or FOXESS_USERNAME + FOXESS_PASSWORD in .env"
            )
        return kwargs


config = Config()
