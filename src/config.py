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

    # ── Direct Telegram Bot API transport (preferred when configured) ───────
    # Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to bypass the OpenClaw
    # /hooks/agent path. The OpenClaw hook fans out to an LLM that re-shapes
    # already-formatted Markdown — that LLM call costs Anthropic tokens on
    # every brief/plan-revision/tier-boundary/appliance ping. With Telegram
    # configured, ``src/notifier.py`` POSTs straight to api.telegram.org
    # using HTML parse mode and the OpenClaw hook is skipped entirely.
    # OPENCLAW_NOTIFY_ENABLED still acts as the master mute (stdout +
    # action_log keep running regardless).
    TELEGRAM_BOT_TOKEN: str = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    TELEGRAM_CHAT_ID: str = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    TELEGRAM_API_BASE_URL: str = (
        os.getenv("TELEGRAM_API_BASE_URL") or "https://api.telegram.org"
    ).strip().rstrip("/")
    TELEGRAM_TIMEOUT_SECONDS: int = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10"))
    # Extra Telegram chat IDs (CSV) that receive a copy of every appliance
    # lifecycle notification (armed / starting / finished / cancelled). The
    # primary chat at TELEGRAM_CHAT_ID always gets the message; entries here
    # are fanned out *in addition*. De-duplicated against the primary chat
    # so listing the same ID twice is harmless. Empty = no fanout.
    TELEGRAM_APPLIANCE_FANOUT_CHAT_IDS: str = (
        os.getenv("TELEGRAM_APPLIANCE_FANOUT_CHAT_IDS") or ""
    ).strip()

    # ── Google Family Calendar publisher (Octopus rate windows) ─────────────
    # Side feature: after every Octopus fetch, classify each 30-min slot into
    # a 6-tier price bucket (day-relative + absolute floors) and publish merged
    # windows as events on a shared Google Calendar so the family knows when
    # to run laundry/dishwasher/etc. Idempotent: existing events for the same
    # slot are updated in place; obsolete events are deleted.
    GOOGLE_CALENDAR_ENABLED: bool = os.getenv("GOOGLE_CALENDAR_ENABLED", "false").lower() in ("true", "1", "yes")
    GOOGLE_CALENDAR_ID: str = (os.getenv("GOOGLE_CALENDAR_ID") or "").strip()
    # Service-account credentials (preferred path — no refresh tokens, no
    # browser dance). When set, the publisher uses this and ignores the OAuth
    # token/secret files below. Share the family calendar with the SA email
    # ("Make changes to events") for write access.
    GOOGLE_CALENDAR_SA_FILE: Path = Path(
        os.getenv("GOOGLE_CALENDAR_SA_FILE", "data/.google-sa.json")
    )
    # Installed-app OAuth fallback files (only used when SA credentials are
    # absent). Bootstrapped via `python -m src.google_calendar` (interactive).
    GOOGLE_CALENDAR_TOKEN_FILE: Path = Path(
        os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", "data/.google-tokens.json")
    )
    GOOGLE_CALENDAR_CLIENT_SECRET_FILE: Path = Path(
        os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET_FILE", "data/.google-client-secret.json")
    )
    GOOGLE_CALENDAR_TIMEZONE: str = (os.getenv("GOOGLE_CALENDAR_TIMEZONE") or "Europe/London").strip()
    # Local OAuth callback port for the one-shot bootstrap container (mirror of Daikin :8080).
    GOOGLE_CALENDAR_OAUTH_PORT: int = int(os.getenv("GOOGLE_CALENDAR_OAUTH_PORT", "8080"))
    # Daily publish cron in UTC. Octopus releases tomorrow's Agile rates around
    # 16:00 UTC; 16:30 UTC catches the new prices with margin. The job is also
    # called once on service startup so first-deploy / service-was-down-at-cron
    # cases recover automatically. Idempotent — re-running with unchanged prices
    # makes zero Google API calls.
    GOOGLE_CALENDAR_PUBLISH_HOUR: int = int(os.getenv("GOOGLE_CALENDAR_PUBLISH_HOUR", "16"))
    GOOGLE_CALENDAR_PUBLISH_MINUTE: int = int(os.getenv("GOOGLE_CALENDAR_PUBLISH_MINUTE", "30"))

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

    # ── Epic 13b — UI container bearer token + CORS ─────────────────────────
    # The SPA container POSTs to /api/v1/* with `Authorization: Bearer
    # <HEM_UI_TOKEN>`. Same mint pattern as the OpenClaw token: env wins,
    # otherwise the lifespan reads the file or generates a fresh token on
    # first boot (urlsafe-32). Mounted via `ApiV1BearerAuth` middleware,
    # gated by HEM_UI_AUTH_REQUIRED below so B1 can land before B6 cutover.
    HEM_UI_TOKEN_FILE: str = os.getenv(
        "HEM_UI_TOKEN_FILE", "data/.hem-ui-token"
    )
    HEM_UI_TOKEN: str = os.getenv("HEM_UI_TOKEN", "").strip()
    # ── Viewer/Admin role model ─────────────────────────────────────────────
    # The UI defaults to a passive VIEWER (read-only, no token) so it can be
    # shared (even outside Tailscale) without exposing controls. Mutating the
    # system (writes) and the Settings/Journal admin reads require the ADMIN
    # secret below — the user types it once in the UI ("unlock"), it's stored
    # in the browser and sent as the bearer. HEM_OPENCLAW_TOKEN is also
    # admin-level so server-to-server flows keep working.
    # IMPORTANT: HEM_UI_TOKEN is NOT admin — it is baked into the UI's
    # config.js (readable by any viewer), so granting it write power would
    # defeat the model. Only HEM_ADMIN_TOKEN + HEM_OPENCLAW_TOKEN are admin.
    HEM_ADMIN_TOKEN: str = os.getenv("HEM_ADMIN_TOKEN", "").strip()
    # ── Scoped sensor-ingest token (#540 W1) ────────────────────────────────
    # A NON-admin write credential that unlocks ONLY an exact POST to
    # /api/v1/sensors/indoor (see ApiV1RoleAuth.ingest_tokens / _ingest_allowed).
    # This is what an internet-exposed device (an ESPHome room sensor, pushing
    # through the existing hem-ui Tailscale funnel at :8443) carries — so a
    # firmware/network leak can only post fake temperatures to that one route,
    # never touch admin. Empty → feature off (only admin tokens work, as
    # before). Rotate to revoke a device.
    HEM_SENSOR_INGEST_TOKEN: str = os.getenv("HEM_SENSOR_INGEST_TOKEN", "").strip()
    # Default False — middleware is a no-op (dev: everything open). When True,
    # the role model is enforced: safe reads open to viewers, writes +
    # Settings/Journal gated on an admin token. Set True in prod.
    HEM_UI_AUTH_REQUIRED: bool = os.getenv(
        "HEM_UI_AUTH_REQUIRED", "false"
    ).lower() in ("true", "1", "yes")
    # CSV of origins allowed by the FastAPI CORSMiddleware. The SPA's nginx
    # container reverse-proxies /api/v1 → HEM, so the browser sees both
    # under one origin in compose; the entries here matter mainly for the
    # Tailnet-direct case where the SPA host differs from HEM.
    HEM_UI_CORS_ORIGINS: str = os.getenv(
        "HEM_UI_CORS_ORIGINS", "http://localhost,http://localhost:8080"
    )

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

    # PnL comparison shadow: previous fixed tariff (e.g. British Gas PeakSave Fixed v58).
    # Set both to surface a "vs <FIXED_TARIFF_LABEL>" line in the daily brief; leave
    # either at 0 to suppress the line. Used by ``compute_daily_pnl`` to compute the
    # alternative-cost shadow at apples-to-apples (kWh × rate + standing charge).
    FIXED_TARIFF_LABEL: str = os.getenv("FIXED_TARIFF_LABEL", "")
    FIXED_TARIFF_RATE_PENCE: float = float(os.getenv("FIXED_TARIFF_RATE_PENCE", "0"))
    FIXED_TARIFF_STANDING_PENCE_PER_DAY: float = float(
        os.getenv("FIXED_TARIFF_STANDING_PENCE_PER_DAY", "0")
    )

    # Window (in days) for the morning-vs-afternoon PV forecast bias report
    # surfaced in the morning brief and via the ``get_pv_forecast_bias`` MCP
    # tool. Reads ``pv_calibration_hourly`` and ``pv_realtime_history`` via
    # ``weather.evaluate_pv_forecast_accuracy``.
    PV_BIAS_REPORT_WINDOW_DAYS: int = int(os.getenv("PV_BIAS_REPORT_WINDOW_DAYS", "14"))

    # Effective start of the Octopus Agile tariff. Period aggregations
    # (weekly/monthly/MTD/YTD) clamp their start date upward to this value
    # so pre-switch days don't pollute the realised cost or shadow comparisons
    # (the household was on a different tariff back then). ISO date string
    # ``YYYY-MM-DD``; empty disables the clamp (= include all history).
    AGILE_TARIFF_START_DATE: str = os.getenv("AGILE_TARIFF_START_DATE", "")

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
    # 300s default (#306 follow-up): heartbeat no longer calls Daikin API,
    # so cadence is bounded by Fox cache freshness + MPC drift detection latency.
    # 5-min latency on SoC drift / low-SoC alerts is well within battery safety
    # margin. Override via env to tighten.
    HEARTBEAT_INTERVAL_SECONDS: int = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "300"))
    SVT_RATE_PENCE: float = float(os.getenv("SVT_RATE_PENCE", "24.50"))
    # SVT daily standing charge for the shadow comparison. Defaults to 0 →
    # callers fall back to MANUAL_STANDING_CHARGE_PENCE_PER_DAY. Set when the SVT
    # standing differs from your Agile standing so the comparison is per-tariff.
    SVT_STANDING_PENCE_PER_DAY: float = float(
        os.getenv("SVT_STANDING_PENCE_PER_DAY", "0")
    )
    OPTIMIZATION_PEAK_THRESHOLD_PENCE: float = float(
        os.getenv("OPTIMIZATION_PEAK_THRESHOLD_PENCE", "25")
    )
    OCTOPUS_FETCH_HOUR: int = int(os.getenv("OCTOPUS_FETCH_HOUR", "16"))
    OCTOPUS_FETCH_MINUTE: int = int(os.getenv("OCTOPUS_FETCH_MINUTE", "5"))
    # Twice-daily digest (V12). DAILY_BRIEF_* are kept as aliases so existing
    # /srv/hem/.env files still work without an edit; BRIEF_MORNING_* take
    # precedence when both are set.
    DAILY_BRIEF_HOUR: int = int(os.getenv("DAILY_BRIEF_HOUR", "8"))
    DAILY_BRIEF_MINUTE: int = int(os.getenv("DAILY_BRIEF_MINUTE", "0"))
    BRIEF_MORNING_HOUR: int = int(
        os.getenv("BRIEF_MORNING_HOUR", os.getenv("DAILY_BRIEF_HOUR", "8"))
    )
    BRIEF_MORNING_MINUTE: int = int(
        os.getenv("BRIEF_MORNING_MINUTE", os.getenv("DAILY_BRIEF_MINUTE", "0"))
    )
    BRIEF_NIGHT_HOUR: int = int(os.getenv("BRIEF_NIGHT_HOUR", "22"))
    BRIEF_NIGHT_MINUTE: int = int(os.getenv("BRIEF_NIGHT_MINUTE", "0"))

    # Heartbeat tariff-transition pings — muted by default in V12; the
    # morning brief lists today's windows. The ``negative`` (🔵 PAID) tier
    # is a separate path (always pings, see push_negative_window_start).
    NOTIFY_TARIFF_TRANSITIONS: bool = os.getenv(
        "NOTIFY_TARIFF_TRANSITIONS", "false"
    ).lower() in ("1", "true", "yes")

    # Tier-boundary MPC trigger — fire this many minutes BEFORE a window's
    # local start time. Reuses dynamic_replan one-shot scheduling. The 5-min
    # default covers the worst-case LP-solve + Fox-V3 upload + retry budget
    # AND aligns with MPC_COOLDOWN_SECONDS=300 so back-to-back triggers
    # don't suppress each other.
    TIER_BOUNDARY_LEAD_MINUTES: int = int(
        os.getenv("TIER_BOUNDARY_LEAD_MINUTES", "5")
    )
    # Minimum lead time from "now" before we'll schedule a tier-boundary fire.
    # Keeps us from scheduling a job whose fire-time is already in the past
    # (or so close that solve+upload can't complete in time). DIFFERENT from
    # ``DYNAMIC_REPLAN_MIN_LEAD_MINUTES=120`` which is the much larger floor
    # for the LP-truncation-tail recovery path. Set to 1 min by default —
    # 30 s solve + 30 s Fox cloud worst case fits comfortably.
    TIER_BOUNDARY_MIN_LEAD_MINUTES: int = int(
        os.getenv("TIER_BOUNDARY_MIN_LEAD_MINUTES", "1")
    )

    # Consumption backfill — when the nightly post-hoc reconciliation pulls
    # yesterday's actual half-hourly consumption from Octopus and rewrites
    # ``execution_log`` rows. Fires in BULLETPROOF_TIMEZONE local. Octopus
    # consumption data lands ~24 h after the slot; 04:00 local is safe.
    CONSUMPTION_BACKFILL_HOUR: int = int(os.getenv("CONSUMPTION_BACKFILL_HOUR", "4"))
    CONSUMPTION_BACKFILL_MINUTE: int = int(os.getenv("CONSUMPTION_BACKFILL_MINUTE", "0"))

    # --- Guests-mode suggestion (2026-06-30) ---
    # A nightly detector flags SUSTAINED base-load elevation (possible visitors)
    # and PROMPTS the user to enable the guests preset (never auto-applies — the
    # household knows if there are guests; auto-applying would chase the noisy
    # load like the rejected recency corrector). Fires at most once per episode.
    GUESTS_DETECT_ENABLED: bool = os.getenv(
        "GUESTS_DETECT_ENABLED", "true"
    ).strip().lower() in ("1", "true", "yes", "on")
    GUESTS_DETECT_WINDOW_DAYS: int = int(os.getenv("GUESTS_DETECT_WINDOW_DAYS", "4"))
    GUESTS_DETECT_RATIO: float = float(os.getenv("GUESTS_DETECT_RATIO", "1.15"))
    GUESTS_DETECT_MIN_DAYS: int = int(os.getenv("GUESTS_DETECT_MIN_DAYS", "3"))
    GUESTS_DETECT_DAY_OVER: float = float(os.getenv("GUESTS_DETECT_DAY_OVER", "1.10"))
    # Re-arm only after the signal falls back to ~normal (ratio < this) — stops
    # nightly re-prompts during the same elevated episode.
    GUESTS_DETECT_REARM_RATIO: float = float(os.getenv("GUESTS_DETECT_REARM_RATIO", "1.05"))
    GUESTS_SUGGESTION_HOUR: int = int(os.getenv("GUESTS_SUGGESTION_HOUR", "8"))
    GUESTS_SUGGESTION_MINUTE: int = int(os.getenv("GUESTS_SUGGESTION_MINUTE", "30"))

    # --- LP health monitor (2026-06-30) ---
    # A nightly self-check that flags PERFORMANCE REGRESSIONS from recent dispatch
    # changes (#607/#608/#609/#610) and alerts via Telegram — ONLY on regression
    # (silent when healthy). Lives in HEM (has the data); a cloud routine can't
    # reach the Tailscale-only prod box. Checks: (1) LP infeasible spike in the
    # last 24h; (2) on a day that HAD negative-price slots, did the battery
    # DISCHARGE during them (the #607 Backup-discharge bug regressing → it must be
    # ForceCharge/hold, discharge ≈ 0). Deduped one alert per signature per day.
    LP_HEALTH_MONITOR_ENABLED: bool = os.getenv(
        "LP_HEALTH_MONITOR_ENABLED", "true"
    ).strip().lower() in ("1", "true", "yes", "on")
    # infeasibles/24h above this = regression (historical rate ~0.8/day; recent 0).
    LP_HEALTH_MAX_INFEASIBLE_24H: int = int(os.getenv("LP_HEALTH_MAX_INFEASIBLE_24H", "5"))
    # battery discharge (kWh) summed over negative-price slots above this = the
    # #607 bug regressed (battery should hold/charge, not self-discharge into load).
    LP_HEALTH_NEG_DISCHARGE_KWH: float = float(os.getenv("LP_HEALTH_NEG_DISCHARGE_KWH", "0.5"))
    # PR B floor observability: alert when the pessimistic charge floor costs
    # more insurance than plausible savings in 24h, or when its slack shows the
    # floor is unreachable (pess/nominal inputs diverging).
    LP_HEALTH_FLOOR_INSURANCE_24H_PENCE: float = float(
        os.getenv("LP_HEALTH_FLOOR_INSURANCE_24H_PENCE", "150.0")
    )
    LP_HEALTH_FLOOR_SLACK_KWH: float = float(
        os.getenv("LP_HEALTH_FLOOR_SLACK_KWH", "0.3")
    )
    LP_HEALTH_MONITOR_HOUR: int = int(os.getenv("LP_HEALTH_MONITOR_HOUR", "9"))
    LP_HEALTH_MONITOR_MINUTE: int = int(os.getenv("LP_HEALTH_MONITOR_MINUTE", "0"))
    # Octopus regularly publishes later than the ~24 h the single-shot design
    # assumed (observed 2026-05/06: whole weeks landed days late after a
    # meter-comms outage). The cron therefore sweeps a trailing window and
    # re-attempts any local day that still lacks a daily-meter row or whose
    # execution_log metered coverage is below the slot floor. Re-runs are
    # idempotent (update_execution_log_metered upserts by half-hour bucket).
    CONSUMPTION_BACKFILL_SWEEP_DAYS: int = int(
        os.getenv("CONSUMPTION_BACKFILL_SWEEP_DAYS", "7")
    )
    CONSUMPTION_BACKFILL_MIN_METERED_SLOTS: int = int(
        os.getenv("CONSUMPTION_BACKFILL_MIN_METERED_SLOTS", "40")
    )
    # Daily-brief warning when the newest plausible metered day is older than
    # this — silence here previously hid a month of Fox-only PnL (#533).
    CONSUMPTION_METER_STALE_DAYS: int = int(
        os.getenv("CONSUMPTION_METER_STALE_DAYS", "3")
    )
    # Actuation-health alert (2026-06-14). A healthy system refreshes the Fox V3
    # upload timestamp at least daily — the 00:05 plan push always re-uploads
    # (an unchanged schedule still saves state via skip_if_equal), plus the
    # force-write triggers (drift / forecast_revision / octopus_fetch /
    # tier_boundary) — and fires tank rows (dhw_policy warmup+setback) ~2×/day,
    # so >~30h without one means actuation is wedged. Tank age is suppressed in
    # vacation mode (no tank rows by design). LWT is demand-gated (summer-
    # dormant) → failure-count only. Set *_STALE_HOURS to 0 to disable the age
    # check; the failed-count threshold clamps to ≥1.
    FOX_UPLOAD_STALE_HOURS: float = float(os.getenv("FOX_UPLOAD_STALE_HOURS", "30"))
    DAIKIN_TANK_STALE_HOURS: float = float(os.getenv("DAIKIN_TANK_STALE_HOURS", "30"))
    DAIKIN_FAILED_ALERT_THRESHOLD: int = int(os.getenv("DAIKIN_FAILED_ALERT_THRESHOLD", "3"))
    BULLETPROOF_TIMEZONE: str = (os.getenv("BULLETPROOF_TIMEZONE") or "Europe/London").strip()

    # ── SmartThings smart-appliance scheduling — OAuth 2.0 (mirrors Daikin) ──
    # Tokens live at SMARTTHINGS_TOKEN_FILE (JSON, 0600, mirrors .daikin-tokens.json
    # shape: access_token + refresh_token + expires_in + obtained_at + scope).
    # Bootstrap via the one-shot ``deploy/compose.smartthings-auth.yaml`` container.
    SMARTTHINGS_CLIENT_ID: str = (os.getenv("SMARTTHINGS_CLIENT_ID") or "").strip()
    SMARTTHINGS_CLIENT_SECRET: str = (os.getenv("SMARTTHINGS_CLIENT_SECRET") or "").strip()
    SMARTTHINGS_REDIRECT_URI: str = (
        os.getenv("SMARTTHINGS_REDIRECT_URI")
        or "http://localhost:8080/oauth/smartthings/callback"
    ).strip()
    SMARTTHINGS_AUTHORIZE_URL: str = (
        os.getenv("SMARTTHINGS_AUTHORIZE_URL") or "https://api.smartthings.com/oauth/authorize"
    ).strip().rstrip("/")
    SMARTTHINGS_TOKEN_URL: str = (
        os.getenv("SMARTTHINGS_TOKEN_URL")
        or "https://auth-global.api.smartthings.com/oauth/token"
    ).strip().rstrip("/")
    SMARTTHINGS_OAUTH_SCOPES: str = (
        os.getenv("SMARTTHINGS_OAUTH_SCOPES") or "r:devices:* x:devices:*"
    ).strip()
    SMARTTHINGS_TOKEN_FILE: Path = Path(
        os.getenv("SMARTTHINGS_TOKEN_FILE", "data/.smartthings-tokens.json")
    )
    SMARTTHINGS_API_BASE: str = (
        os.getenv("SMARTTHINGS_API_BASE") or "https://api.smartthings.com/v1"
    ).strip().rstrip("/")
    # Refresh leeway: get a new access token if it expires within this many seconds.
    SMARTTHINGS_ACCESS_REFRESH_LEEWAY_SECONDS: int = int(
        os.getenv("SMARTTHINGS_ACCESS_REFRESH_LEEWAY_SECONDS", "300")
    )
    SMARTTHINGS_TOKEN_REFRESH_MIN_INTERVAL_SECONDS: int = int(
        os.getenv("SMARTTHINGS_TOKEN_REFRESH_MIN_INTERVAL_SECONDS", "60")
    )
    APPLIANCE_DISPATCH_ENABLED: bool = os.getenv(
        "APPLIANCE_DISPATCH_ENABLED", "true"
    ).lower() in ("true", "1", "yes")
    # Fallback start window (HH:MM-HH:MM, local TZ) when agile_rates is empty
    # for the planning horizon (cold start, fetch failure).
    APPLIANCE_FALLBACK_WINDOW_LOCAL: str = (
        os.getenv("APPLIANCE_FALLBACK_WINDOW_LOCAL") or "02:00-05:00"
    ).strip()
    # Default deadline when an appliance is registered without an explicit
    # deadline_local_time. HH:MM in local TZ — the next occurrence is used.
    APPLIANCE_DEFAULT_DEADLINE_LOCAL: str = (
        os.getenv("APPLIANCE_DEFAULT_DEADLINE_LOCAL") or "07:00"
    ).strip()
    APPLIANCE_RECONCILE_ERROR_PING_THRESHOLD: int = int(
        os.getenv("APPLIANCE_RECONCILE_ERROR_PING_THRESHOLD", "3")
    )
    # Notification verbosity (2026-06-28). The user wants only two pings per
    # cycle: the arm confirmation (the LP picked a window) and the finished
    # summary. The "starting" and "re-armed (window shifted)" pings are noise
    # under the pull-based notification policy — default OFF, flip to true to
    # observe them again. Failure pings (notify_risk) are never gated by these.
    APPLIANCE_NOTIFY_STARTING: bool = (
        os.getenv("APPLIANCE_NOTIFY_STARTING", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )
    APPLIANCE_NOTIFY_REPLAN: bool = (
        os.getenv("APPLIANCE_NOTIFY_REPLAN", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )

    # ------------------------------------------------------------------
    # PR K3 (2026-05-23) — battery-aware appliance scheduling.
    # ------------------------------------------------------------------
    # When True, ``find_battery_aware_window`` uses the LP's predicted SoC
    # trajectory to identify slots where the battery can safely cover
    # the appliance load. This lets the dispatcher pick EARLIER windows
    # (user convenience) at no extra grid cost — the LP naturally
    # plans a force-charge refill later via cheap-slot imports.
    # Falls back to legacy ``find_cheapest_window`` when no LP plan is
    # available or when no candidate window passes the safety reserve.
    APPLIANCE_BATTERY_AWARE_ENABLED: bool = (
        os.getenv("APPLIANCE_BATTERY_AWARE_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # k_σ multiplier on historical (actual − estimated) kWh stddev. 2σ
    # ≈ 95% confidence the actual load won't exceed the budget.
    APPLIANCE_VARIANCE_SIGMA: float = float(
        os.getenv("APPLIANCE_VARIANCE_SIGMA", "2.0")
    )
    # Minimum completed jobs before trusting the empirical variance.
    # Until then, the static fallback margin (next setting) is used.
    APPLIANCE_VARIANCE_MIN_SAMPLES: int = int(
        os.getenv("APPLIANCE_VARIANCE_MIN_SAMPLES", "3")
    )
    # Lookback depth (most-recent completed jobs) for variance calculation.
    APPLIANCE_VARIANCE_LOOKBACK_JOBS: int = int(
        os.getenv("APPLIANCE_VARIANCE_LOOKBACK_JOBS", "20")
    )
    # Static safety margin (kWh) used when not enough history exists for
    # a reliable σ. Conservative default ≈ a small spike on top of the
    # typical_kW × duration estimate.
    APPLIANCE_FALLBACK_SAFETY_MARGIN_KWH: float = float(
        os.getenv("APPLIANCE_FALLBACK_SAFETY_MARGIN_KWH", "0.3")
    )
    # #222 — learn an appliance's typical power from measured `actual_kwh`
    # history (SmartThings energy counter) instead of the static registration
    # default. The central cycle-energy estimate uses the rolling mean of the
    # last N completed runs once ≥ MIN_SAMPLES exist; otherwise it falls back to
    # `appliances.typical_kw`. (The σ-based safety margin is separate, #235.)
    APPLIANCE_LEARNED_KW_LOOKBACK: int = int(
        os.getenv("APPLIANCE_LEARNED_KW_LOOKBACK", "10")
    )
    APPLIANCE_LEARNED_KW_MIN_SAMPLES: int = int(
        os.getenv("APPLIANCE_LEARNED_KW_MIN_SAMPLES", "3")
    )
    # Inverter grid-charge rate (kWh per 30-min slot) used to size the
    # refill-window search in the battery-aware picker. Fox EP11 ≈ 1.5;
    # smaller inverters under-fill / larger over-fill if hardcoded.
    APPLIANCE_INVERTER_GRID_CHARGE_KWH_PER_SLOT: float = float(
        os.getenv("APPLIANCE_INVERTER_GRID_CHARGE_KWH_PER_SLOT", "1.5")
    )
    # AC-DC-AC round-trip efficiency of the battery. ~92% typical for
    # Fox EP11; applied as a penalty on battery-covered effective price
    # so the picker correctly accounts for losses vs grid-direct.
    APPLIANCE_BATTERY_ROUND_TRIP_EFF: float = float(
        os.getenv("APPLIANCE_BATTERY_ROUND_TRIP_EFF", "0.92")
    )
    # Max age (hours) of the LP solution before the battery-aware picker
    # treats it as stale and falls back to cheapest-grid. 2 h covers a
    # typical LP cadence; older forecasts encode outdated tariff/weather.
    APPLIANCE_LP_MAX_AGE_HOURS: float = float(
        os.getenv("APPLIANCE_LP_MAX_AGE_HOURS", "2.0")
    )
    # PV-aware appliance window dispatch (PR #219). When True (default), the
    # cheapest-window picker scores candidate windows by marginal cost
    # (= forgone export revenue + grid import for the appliance load) instead
    # of raw Agile import price. Captures free-PV opportunities the legacy
    # path misses on sunny days. Set False to revert to import-only picker.
    APPLIANCE_PV_AWARE_DISPATCH: bool = os.getenv(
        "APPLIANCE_PV_AWARE_DISPATCH", "true"
    ).strip().lower() in ("true", "1", "yes")
    # Static default for the household residual base load when the per-hour-of-day
    # profile has no row for a given slot (cold start / sparse history).
    APPLIANCE_DEFAULT_BASE_LOAD_KW: float = float(
        os.getenv("APPLIANCE_DEFAULT_BASE_LOAD_KW", "0.4")
    )
    # --- Proactive appliance load nudge (2026-06-07) ---
    # HEM can't load the machine (the physical Smart-Control button is the consent
    # gate), so when a notably-cheap / NEGATIVE Agile window is upcoming and a
    # registered appliance is idle, it pushes ONE high-signal Telegram nudge to
    # load it + Smart-Control, with a recommended run window. Fires when day-ahead
    # rates land (octopus_fetch); debounced once per appliance per window.
    APPLIANCE_WINDOW_NUDGE_ENABLED: bool = (
        os.getenv("APPLIANCE_WINDOW_NUDGE_ENABLED", "true").strip().lower()
        in ("true", "1", "yes", "on")
    )
    # Push threshold: empty → push on NEGATIVE windows only (high signal, aligns
    # with the "negative always pings" rule). Set a number (pence) to also push
    # on windows whose mean ≤ that — more nudges, more push load.
    APPLIANCE_WINDOW_NUDGE_PRICE_THRESHOLD_P: str = (
        os.getenv("APPLIANCE_WINDOW_NUDGE_PRICE_THRESHOLD_P") or ""
    )
    # How far ahead (hours) to scan for a candidate window.
    APPLIANCE_WINDOW_NUDGE_HORIZON_HOURS: float = float(
        os.getenv("APPLIANCE_WINDOW_NUDGE_HORIZON_HOURS", "24")
    )
    # Cheap-window threshold (pence) for the PULL morning/night brief suggestion
    # line — independent of the push threshold; covers cheap-but-not-negative.
    APPLIANCE_WINDOW_NUDGE_BRIEF_THRESHOLD_P: float = float(
        os.getenv("APPLIANCE_WINDOW_NUDGE_BRIEF_THRESHOLD_P", "8.0")
    )

    # Max tank temperature (°C) — the physical ceiling for EVERYTHING: the LP
    # tank-variable bound, the soft per-slot ceiling, every setpoint we command,
    # and the cap on the firmware legionella lift. The heat pump AND the
    # firmware legionella cycle both top out here; Onecta rejects any setpoint
    # above it ("Max tank temperature is 60°C", client.py:306), which made the
    # old 65 °C negative-price boost silently FAIL (tank got no extra heat). The
    # app's "powerful"/immersion button can momentarily push higher, but we
    # never command that. Was 65 → now 60 to match the hardware.
    DHW_TEMP_MAX_C: float = float(os.getenv("DHW_TEMP_MAX_C", "60"))
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
    # FOX_FORCE_CHARGE_MAX_PWR: the dispatcher's per-group ForceCharge fdPwr clamp
    #   = the inverter's BATTERY-charge-from-grid rate. Set to the inverter's
    #   nameplate AC rating, NOT the FoxESS app's configurable range (the app
    #   shows the H1 family's full range and the inverter clamps silently to the
    #   model's spec). 2026-06-29: this NO LONGER feeds the LP's total grid-import
    #   cap — see LP_GRID_IMPORT_MAX_KW below.
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
    # LP_GRID_IMPORT_MAX_KW (2026-06-29): the LP's cap on TOTAL grid import per
    # slot (load + battery charge + heat-pump), i.e. what the house MAIN SERVICE
    # FUSE can carry — NOT the inverter rating. Previously the LP derived this
    # from FOX_FORCE_CHARGE_MAX_PWR (5 kW), conflating "total import" with the
    # inverter's battery-charge rate. But total import = direct AC load (heat
    # pump / DHW boost, fed straight from the grid) + battery charge (via the
    # inverter, ≤ MAX_INVERTER_KW). Prod actuals show ~7.5 kW import in deep
    # ForceCharge slots (≈ 5 kW charge + ~2.5 kW load) — above the old 5 kW cap.
    # The old cap artificially stopped the LP from planning battery-charge AND a
    # concurrent grid-fed load at the paid negative price (relevant in winter when
    # the heat pump runs through negative windows). There is no fuse-trip risk
    # from raising this: the LP can never plan import beyond (forecast load +
    # chg≤MAX_INVERTER_KW + e_hp), so a higher cap only removes an artificial
    # limit. Default 10 kW comfortably covers battery(5) + heat pump(~3) + base,
    # and sits below any plausible heat-pump-home main fuse (≥60 A ≈ 13.8 kW).
    # Set to the install's real main-fuse rating in .env when known.
    LP_GRID_IMPORT_MAX_KW: float = float(os.getenv("LP_GRID_IMPORT_MAX_KW", "10.0"))
    # 2026-06-07: pin maxSoc to the reserve floor on `negative_hold` (Backup)
    # slots so charging is blocked during the hold. DEFAULT FLIPPED to false on
    # 2026-07-04 (owner decision): during a negative window every kWh the
    # firmware tops up — from PV or from the PAID grid (Backup with maxSoc
    # unset imported ~1.2 kW avg in prod samples) — is free/paid money, and
    # "maximize grid usage inside the negative window" is the household
    # policy. The old concern (PV creep eating headroom for the deep-slot
    # paid fill) predates the hold/fill class-aware merge (#616) that keeps
    # fills anchored to the deepest slots. Set true to restore the pin.
    LP_NEGATIVE_HOLD_PIN_MAXSOC: bool = (
        os.getenv("LP_NEGATIVE_HOLD_PIN_MAXSOC", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # 2026-07-04 (owner decision) — Fox mode for `negative_hold` slots:
    #   "backup"      (default) Fox's native reserve mode. Empirically 0
    #                 discharges in 413 prod samples; with maxSoc unset it
    #                 also tops the battery up from the PAID grid during the
    #                 window. Holds stay outside the ForceCharge merge, so
    #                 fills remain anchored to the deepest-priced slots.
    #   "forcecharge" the #607/#630 interim: ForceCharge at ~LP-import power
    #                 (~200 W) with fdSoc = LP target. Also 0 discharges in
    #                 354 samples. Kept as fallback.
    # (Replaces the boolean LP_NEGATIVE_HOLD_NO_DISCHARGE, which chose
    # between forcecharge and a maxSoc-pinned Backup.)
    LP_NEGATIVE_HOLD_FOX_MODE: str = (
        os.getenv("LP_NEGATIVE_HOLD_FOX_MODE", "backup").strip().lower()
    )

    # 2026-07-04 — in the slot labeller, `price <= 0` outranks the PV-only
    # (grid_import ~= 0) solar_charge check. Negative-price slots with a
    # PV-sourced planned charge used to be labelled solar_charge ->
    # SelfUse(minSocOnGrid=100), and the H1 firmware does not honour that
    # floor as a discharge freeze (battery discharged into the DHW boost on
    # 06-28 and 07-04 instead of the paid grid). With this on they become
    # `negative` -> ForceCharge, the discharge-proof mode. Vacation preset is
    # unaffected (its LP forbids grid->battery entirely). false = legacy
    # labelling for instant rollback.
    # Fox dispatch surface in hours (< 24). V3 groups are daily-cyclic
    # (hour:minute only), so a 24 h surface's tail slot lands on TODAY's
    # in-flight hour-of-day — the 06-28/07-04 leak class. Hard-capped at
    # 23.5; default 23.0 keeps a spare slot of margin + covers the DST
    # fall-back day. Re-solves re-dispatch the dropped tail continuously.
    FOX_DISPATCH_HORIZON_HOURS: float = float(
        os.getenv("FOX_DISPATCH_HORIZON_HOURS", "23.0")
    )

    LP_NEGATIVE_BEATS_SOLAR_CHARGE: bool = (
        os.getenv("LP_NEGATIVE_BEATS_SOLAR_CHARGE", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )

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
    FORECAST_SOURCE: str = (os.getenv("FORECAST_SOURCE") or "open_meteo").strip().lower()
    QUARTZ_AUTH_URL: str = (
        os.getenv("QUARTZ_AUTH_URL") or "https://nowcasting-pro.eu.auth0.com/oauth/token"
    ).strip()
    # Default to the documented HTTPS endpoint at api.quartz.solar. Some
    # development networks see a Cloudflare 1010 from that hostname; in that
    # case set ``QUARTZ_API_BASE_URL`` explicitly in your local ``.env`` to
    # the upstream Elastic Beanstalk URL. Production must use HTTPS so the
    # Authorization bearer token is not sent over plaintext.
    QUARTZ_API_BASE_URL: str = (
        os.getenv("QUARTZ_API_BASE_URL") or "https://api.quartz.solar"
    ).strip().rstrip("/")
    # Vendor-issued OAuth client_id. Empty default — operators must supply it
    # via ``.env`` so the value is not committed to OSS.
    QUARTZ_CLIENT_ID: str = (os.getenv("QUARTZ_CLIENT_ID") or "").strip()
    QUARTZ_AUDIENCE: str = (
        os.getenv("QUARTZ_AUDIENCE") or "https://api.nowcasting.io/"
    ).strip()
    QUARTZ_USERNAME: str = (
        os.getenv("QUARTZ_USERNAME") or os.getenv("QUARTZ_USER") or ""
    ).strip()
    QUARTZ_PASSWORD: str = os.getenv("QUARTZ_PASSWORD") or os.getenv("QUARTZ_PASS") or ""
    QUARTZ_GSP_ID: str = (os.getenv("QUARTZ_GSP_ID") or "").strip()
    QUARTZ_MODEL_NAME: str = (os.getenv("QUARTZ_MODEL_NAME") or "blend").strip()
    QUARTZ_TREND_ADJUSTER_ON: bool = (
        os.getenv("QUARTZ_TREND_ADJUSTER_ON", "true").lower() in ("1", "true", "yes")
    )
    QUARTZ_INSTALLED_CAPACITY_MW: float = float(
        os.getenv("QUARTZ_INSTALLED_CAPACITY_MW", "0") or "0"
    )
    # --- #542 — site-level open Quartz provider --------------------------
    # The hosted api.quartz.solar token is the COMMERCIAL national/GSP
    # product (access expiring); the open-source SITE-level model is free in
    # two interchangeable forms behind the same schema:
    #   * the hem-quartz sidecar container (deploy/compose.yaml) at
    #     http://hem-quartz:8000 — self-hosted, no external dependency;
    #   * the hosted twin https://open.quartz.solar — unauthenticated.
    # QUARTZ_PROVIDER selects the client: "open" (new default) talks the
    # open schema at QUARTZ_OPEN_URL; "hosted" preserves the legacy
    # token-based national/GSP client for rollback while the token lives.
    QUARTZ_PROVIDER: str = (os.getenv("QUARTZ_PROVIDER") or "open").strip().lower()
    QUARTZ_OPEN_URL: str = (
        os.getenv("QUARTZ_OPEN_URL") or "https://open.quartz.solar"
    ).strip().rstrip("/")
    # JSON list of panel planes, e.g. (this house: 6 SW-pitched + 6 flat-rack
    # south — see project_quartz_site_level_killed for the sweep evidence):
    #   [{"tilt": 35, "orientation": 225, "capacity_kwp": 2.25},
    #    {"tilt": 10, "orientation": 180, "capacity_kwp": 2.25}]
    # Empty → single aggregate plane (tilt 30, orientation 200,
    # capacity PV_CAPACITY_KWP); the L3 calibration absorbs the residual.
    QUARTZ_OPEN_PLANES: str = (os.getenv("QUARTZ_OPEN_PLANES") or "").strip()
    QUARTZ_OPEN_TIMEOUT_SECONDS: int = int(os.getenv("QUARTZ_OPEN_TIMEOUT_SECONDS", "60"))
    # Send recent measured generation with the request. NOTE (#544 review):
    # today BOTH the sidecar and the hosted twin accept-and-ignore it (the
    # upstream model only anchors on live data via its inverter
    # integrations) — kept on as future-proof plumbing, harmless either way.
    QUARTZ_OPEN_SEND_LIVE: bool = os.getenv(
        "QUARTZ_OPEN_SEND_LIVE", "true"
    ).lower() in ("true", "1", "yes")
    # PR L1 (2026-05-24) — apply the per-hour + per-(hour, cloud) calibration
    # tables on top of Quartz's direct PV output. Previously SKIPPED (PR #279)
    # under the assumption "Quartz self-calibrates", but observed AM 0.65 /
    # PM 1.11 bias in `forecast_skill_log` confirms the GSP-level Quartz
    # endpoint does NOT capture our W4 1DZ site (split array: SW-pitched +
    # flat-rack, aggregate ~200° SSW, with non-ideal tilt mix). The
    # calibration tables (populated daily 04:30 UTC from `pv_realtime_history`
    # actuals vs Quartz forecasts) already encode the residual empirically —
    # we just weren't applying it. Set to false to restore legacy bypass
    # (for rollback if double-correction issues surface).
    PV_QUARTZ_APPLY_CALIBRATION: bool = (
        os.getenv("PV_QUARTZ_APPLY_CALIBRATION", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )

    # --- Adaptive PV recent-bias corrector (#486) ----------------------------
    # Closed feedback loop on the COMMITTED forecast's own error: per UTC hour,
    # the recency-weighted mean of actual/forecast from ``pv_error_log`` nudges
    # the day-ahead PV forecast. Because it's driven by REALISED error (not
    # clear-sky potential), genuine morning shade stays low (actual low there →
    # factor ≈ 1) while systematic under-forecast (e.g. clear mornings 2× low)
    # gets corrected. Damped + clamped + recomputed daily after the error
    # rebuild → stable, self-converging. Off by default (observe first).
    PV_RECENT_BIAS_ENABLED: bool = (
        os.getenv("PV_RECENT_BIAS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    )
    PV_RECENT_BIAS_WINDOW_DAYS: int = int(os.getenv("PV_RECENT_BIAS_WINDOW_DAYS", "14"))
    PV_RECENT_BIAS_HALFLIFE_DAYS: float = float(os.getenv("PV_RECENT_BIAS_HALFLIFE_DAYS", "5"))
    # The corrector ACCUMULATES on its previous factor each refresh, nudged by
    # the RESIDUAL error (ratio of actual to the already-corrected forecast):
    # ``new = old × (1 + damping·(ratio−1))``. So it ramps to FULL correction
    # over a few days (while the corrected forecast still under-shoots, ratio>1
    # keeps pushing the factor up; once it matches, ratio≈1 and it settles) and
    # self-stabilises if it over-shoots (ratio<1 pulls it back). Damping sets the
    # ramp speed. Clamp is a hard safety rail, wide enough to fully correct the
    # observed ~2× morning bias.
    PV_RECENT_BIAS_DAMPING: float = float(os.getenv("PV_RECENT_BIAS_DAMPING", "0.5"))
    PV_RECENT_BIAS_MIN: float = float(os.getenv("PV_RECENT_BIAS_MIN", "0.4"))
    PV_RECENT_BIAS_MAX: float = float(os.getenv("PV_RECENT_BIAS_MAX", "2.5"))
    # A slot needs at least this forecast+actual kWh to contribute (drop noise).
    PV_RECENT_BIAS_MIN_KWH: float = float(os.getenv("PV_RECENT_BIAS_MIN_KWH", "0.05"))
    # Slot-CENTRE forecast sampling. A 30-min slot's energy is `kw × 0.5h`; the
    # honest representative power is the value at the slot CENTRE (start+15min),
    # not the start instant. Sampling at the start systematically attributed each
    # slot's energy ~15 min too late versus the realised (trapezoidal) PV — a
    # deterministic +15 min lag confirmed on 21 days of prod data
    # (scripts/diag/pv_time_lag.py). True → interpolate the weather drivers at the
    # slot centre. Set false to restore the legacy slot-start sampling.
    PV_FORECAST_SLOT_CENTRE_SAMPLING: bool = (
        os.getenv("PV_FORECAST_SLOT_CENTRE_SAMPLING", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )

    # --- Load recent-bias corrector (Phase 2; analog of the PV one) -----------
    # ADDITIVE per-LOCAL-hour correction on the residual base-load forecast.
    # Load is occupancy-driven (local hour) and the bias is a level offset from
    # seasonal/occupancy regime shift (the 120d median lags), so it's ADDITIVE,
    # not multiplicative like PV. Closed loop: ``new = old + damping·raw_bias``
    # where raw_bias is the recency-weighted mean (actual − committed_forecast)
    # — i.e. the RESIDUAL error of the already-corrected forecast — so it ramps
    # to full correction and settles at raw_bias≈0. DEFAULT OFF — the table is
    # still refreshed nightly (cheap, observable) but never touches the LP until
    # this is flipped on after the backtest confirms it helps.
    LOAD_RECENT_BIAS_ENABLED: bool = (
        os.getenv("LOAD_RECENT_BIAS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    )
    LOAD_RECENT_BIAS_WINDOW_DAYS: int = int(os.getenv("LOAD_RECENT_BIAS_WINDOW_DAYS", "21"))
    LOAD_RECENT_BIAS_HALFLIFE_DAYS: float = float(os.getenv("LOAD_RECENT_BIAS_HALFLIFE_DAYS", "7"))
    LOAD_RECENT_BIAS_DAMPING: float = float(os.getenv("LOAD_RECENT_BIAS_DAMPING", "0.5"))
    # Hard safety rail on the |additive correction| (kWh/slot). The observed
    # diurnal swing is ~0.35 kWh/slot; 0.3 covers it without letting a noisy
    # hour run away.
    LOAD_RECENT_BIAS_MAX_KWH: float = float(os.getenv("LOAD_RECENT_BIAS_MAX_KWH", "0.3"))
    # A slot needs at least this forecast+actual kWh to contribute (drop noise).
    LOAD_RECENT_BIAS_MIN_KWH: float = float(os.getenv("LOAD_RECENT_BIAS_MIN_KWH", "0.05"))
    # Min DISTINCT DAYS of evidence for a local hour before it gets a correction.
    LOAD_RECENT_BIAS_MIN_SAMPLES: int = int(os.getenv("LOAD_RECENT_BIAS_MIN_SAMPLES", "3"))
    # Per-slot we can only measure TOTAL load (no per-slot Daikin meter). To
    # isolate the BASE (residual) bias — the only thing this corrector should
    # touch — we only LEARN from slots where the committed heat-pump load
    # (forecast_kwh − forecast_base_kwh) is below this, so on the learning slots
    # total ≈ base and the heat-pump timing error doesn't pollute the base
    # correction. Hours that are always heat-pump-heavy (e.g. the 13–14h warmup)
    # get no clean sample → no correction, which is the honest outcome.
    LOAD_RECENT_BIAS_MAX_DAIKIN_KWH: float = float(os.getenv("LOAD_RECENT_BIAS_MAX_DAIKIN_KWH", "0.1"))

    # Agile scheduler (Daikin ASHP by price)
    SCHEDULER_ENABLED: bool = os.getenv("SCHEDULER_ENABLED", "false").lower() in ("true", "1", "yes")
    OCTOPUS_TARIFF_CODE: str = (os.getenv("OCTOPUS_TARIFF_CODE") or "").strip()
    # Optional: Octopus Agile Export tariff code for SEG export rate fetch.
    # E.g. E-1R-AGILE-OUTGOING-24-10-01-C. Leave blank if not on export tariff.
    OCTOPUS_EXPORT_TARIFF_CODE: str = (os.getenv("OCTOPUS_EXPORT_TARIFF_CODE") or "").strip()
    # Which export tariff the household is ACTUALLY paid on (drives realised £ +
    # the LP's export valuation). "seg_flat" = a flat SEG at EXPORT_SEG_RATE_PENCE
    # (the real bill — confirmed via the Octopus statement); "outgoing_agile" =
    # the per-slot Outgoing Agile curve from agile_export_rates. The other one is
    # always computed too, for the side-by-side comparison in the Insights tab.
    EXPORT_TARIFF_MODE: str = (os.getenv("EXPORT_TARIFF_MODE") or "seg_flat").strip().lower()
    # Flat Smart Export Guarantee rate (p/kWh) actually paid when on the SEG.
    EXPORT_SEG_RATE_PENCE: float = float(os.getenv("EXPORT_SEG_RATE_PENCE", "4.10"))
    # Export meter activation date (ISO). Export before this earns nothing (the
    # export MPAN wasn't live yet). Blank = credit all export.
    EXPORT_METER_START_DATE: str = (os.getenv("EXPORT_METER_START_DATE") or "").strip()
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
    # savings_first (default): trust the LP entirely. Peak grid export (force
    # discharge) is decided by the MILP objective using per-slot Octopus
    # Outgoing pricing and battery round-trip efficiency. Robustness against
    # forecast error is enforced by scenario LP at dispatch time
    # (src/scheduler/scenarios.py + filter_robust_peak_export).
    # strict_savings — never schedule peak export discharge (max self-use);
    # the dispatch filter drops every peak_export slot regardless of scenarios.
    # ``EXPORT_DISCHARGE_FLOOR_SOC_PERCENT`` is the ``fdSoC`` parameter sent
    # to Fox in the ForceDischarge group (separate from the removed live-SoC gate).
    EXPORT_DISCHARGE_FLOOR_SOC_PERCENT: int = int(
        os.getenv("EXPORT_DISCHARGE_FLOOR_SOC_PERCENT", "15")
    )

    # Scenario LP — three-pass robustness check on peak-export commits.
    # See docs/DISPATCH_DECISIONS.md for the design rationale.
    LP_SCENARIO_OPTIMISTIC_TEMP_DELTA_C: float = float(
        os.getenv("LP_SCENARIO_OPTIMISTIC_TEMP_DELTA_C", "1.0")
    )
    LP_SCENARIO_OPTIMISTIC_LOAD_FACTOR: float = float(
        os.getenv("LP_SCENARIO_OPTIMISTIC_LOAD_FACTOR", "0.90")
    )
    LP_SCENARIO_PESSIMISTIC_TEMP_DELTA_C: float = float(
        os.getenv("LP_SCENARIO_PESSIMISTIC_TEMP_DELTA_C", "-1.5")
    )
    LP_SCENARIO_PESSIMISTIC_LOAD_FACTOR: float = float(
        os.getenv("LP_SCENARIO_PESSIMISTIC_LOAD_FACTOR", "1.15")
    )
    # 2026-07-02 LP audit — the pessimistic scenario perturbed load + temperature
    # but kept NOMINAL PV, so a cloud surprise could breach the very floor the
    # peak-export gate (and the PR-B charge floor) trusts. These scale
    # pv_kwh_per_slot in the side scenarios. Defaults calibrated from 27 days of
    # pv_error_log daily Σactual/Σforecast ratios: p25 = 0.883, p10 = 0.758 →
    # pessimistic 0.85; p75 = 1.04 → optimistic 1.05. 1.0 = legacy behaviour
    # (no PV perturbation); nominal is always 1.0.
    LP_SCENARIO_OPTIMISTIC_PV_FACTOR: float = float(
        os.getenv("LP_SCENARIO_OPTIMISTIC_PV_FACTOR", "1.05")
    )
    LP_SCENARIO_PESSIMISTIC_PV_FACTOR: float = float(
        os.getenv("LP_SCENARIO_PESSIMISTIC_PV_FACTOR", "0.85")
    )
    # 2026-07-02 LP audit (PR B) — pessimistic-scenario charge floor. The June
    # hindsight audit measured 14 evenings where the battery hit empty at
    # above-median prices (£2.36/mo; winter amplifies): the LP sizes overnight/
    # pre-peak charge for the MEDIAN load while the cost of under-charging
    # (peak import ~30p) dwarfs over-charging (cheap ~7p) — the newsvendor
    # asymmetry says charge for a high quantile, and the pessimistic scenario
    # (p75 load, −1.5°C, PV ×0.85) IS that quantile, already solved and then
    # discarded. When enabled, scenario-bearing triggers re-solve the nominal
    # plan with a SOFT floor at the pessimistic SoC trajectory; the floored
    # plan is committed. Instant rollback: set false (no redeploy via .env).
    LP_PESS_CHARGE_FLOOR_ENABLED: bool = os.getenv(
        "LP_PESS_CHARGE_FLOOR_ENABLED", "true"
    ).strip().lower() in ("1", "true", "yes", "on")
    # Subtracted from the pessimistic SoC before flooring — keeps the floor
    # from binding on noise-level differences (kWh).
    LP_PESS_CHARGE_FLOOR_TOLERANCE_KWH: float = float(
        os.getenv("LP_PESS_CHARGE_FLOOR_TOLERANCE_KWH", "0.2")
    )
    # Floor only the first N hours of the horizon — the far half is replanned
    # many times before it executes; flooring it would just re-anchor noise.
    LP_PESS_CHARGE_FLOOR_HOURS: float = float(
        os.getenv("LP_PESS_CHARGE_FLOOR_HOURS", "24")
    )
    # Penalty per kWh of slack below the floor. Far above any realistic price
    # spread so the floor behaves hard, but can never make the solve Infeasible.
    LP_PESS_CHARGE_FLOOR_SLACK_PENALTY_PENCE: float = float(
        os.getenv("LP_PESS_CHARGE_FLOOR_SLACK_PENALTY_PENCE", "50.0")
    )
    # PR D (2026-07-02 audit) — adjacent ForceCharge Fox rows merge only within
    # the same intent class: HOLD (fdSoc <= this threshold, i.e. "hold at
    # reserve, don't fill") vs FILL (higher targets). A negative_hold merged
    # into a negative used to take max(fdSoc)=100 for the whole window → Fox
    # front-loaded the fill at the shallow price instead of the deepest slots.
    # Tapered fill runs (70→100) still merge into one group. -1 = legacy
    # always-merge rollback.
    LP_FC_MERGE_HOLD_FDSOC_MAX: float = float(
        os.getenv("LP_FC_MERGE_HOLD_FDSOC_MAX", "35.0")
    )
    # #477 Stage 2 — when true, the scenario LP perturbs base load per-slot by
    # the LEARNED p75 spread (median ± (p75−median)) from residual_load_profile_v2
    # instead of the flat factors above. Sharper protection where the variance is
    # actually high (e.g. variable evenings), gentler where load is steady. Falls
    # back to the flat factors per-slot when no spread is known for a slot, or
    # globally when set false (rollback).
    LP_SCENARIO_USE_SPREAD: bool = os.getenv(
        "LP_SCENARIO_USE_SPREAD", "true"
    ).lower() in ("true", "1", "yes")
    # #477 kill-switch for the LP-critical residual_load_profile_v2 behaviours.
    # True (default): day-of-week buckets + measured-split calibration + away-day
    # exclusion. False: emit only the per-(h,m) median tier with pure-physics
    # subtraction (≈ the legacy half_hourly_residual profile) so the LP plan can
    # be rolled back to the prior shape WITHOUT a redeploy if a regression shows.
    LP_RESIDUAL_PROFILE_V2: bool = os.getenv(
        "LP_RESIDUAL_PROFILE_V2", "true"
    ).lower() in ("true", "1", "yes")
    # Drop negative-price half-hour slots from the residual load-profile sample.
    # During plunges the system DELIBERATELY boosts consumption (battery charge,
    # appliance dispatch, tank to 65 °C), so those slots aren't the organic
    # at-home pattern — keeping them biases the LP base-load forecast upward for
    # whichever (dow,hour) the plunge landed on. Set false to roll back to the
    # old behaviour (include them).
    LP_LOAD_EXCLUDE_NEGATIVE_SLOTS: bool = os.getenv(
        "LP_LOAD_EXCLUDE_NEGATIVE_SLOTS", "true"
    ).lower() in ("true", "1", "yes")
    # Drop half-hour slots inside a HEM nonzero LWT-offset window (+ the
    # thermal-lag tail) from the residual load-profile sample (Tracked by #540).
    # A positive LWT offset can WAKE the compressor (the June-2026 phantom-heat
    # self-loop) — that heat is HEM-induced, not the household's organic load,
    # so learning the residual / heat-pump heatmaps from it pollutes them (and
    # the Insights Heating heatmap). Same rationale as the negative-slot drop
    # above. Tail length reuses DAIKIN_LWT_PREHEAT_DECONTAM_TAIL_BUCKETS. Set
    # false to roll back to the old behaviour (include them).
    LP_LOAD_EXCLUDE_LWT_OFFSET_SLOTS: bool = os.getenv(
        "LP_LOAD_EXCLUDE_LWT_OFFSET_SLOTS", "true"
    ).lower() in ("true", "1", "yes")
    LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH: float = float(
        os.getenv("LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH", "0.30")
    )
    # Additional dispatch-layer guard on ``peak_export``: even when the
    # pessimistic scenario still exports, only commit the slot if the export
    # price beats the best future refill / saved-energy shadow value by at
    # least this much pence/kWh. Default 0.0 = observation-only — the margin
    # value is recorded in ``dispatch_decisions`` for audit but no slots are
    # actually dropped. Raise this AFTER ≥14 days of dispatch_decisions data
    # confirms the implied dropped-slot rate would not have hurt PnL on
    # historical days. Caveat: the refill shadow uses ``min(future_prices)``
    # which is trajectory-blind — multiple peak_export slots crediting the
    # same future-cheap slot all see the same shadow, so the calculation is
    # optimistic about refill availability.
    LP_PEAK_EXPORT_MIN_MARGIN_PENCE_PER_KWH: float = float(
        os.getenv("LP_PEAK_EXPORT_MIN_MARGIN_PENCE_PER_KWH", "0.0")
    )
    # Battery-wear shadow cost (p/kWh throughput). Folded into the
    # peak_export margin check as ``(1 + 1/η) × wear_cost`` so a marginal
    # export must cover both the future refill cost and the extra cycle
    # wear it causes. Default 0 = no wear penalty applied. Story #185
    # tracks the proper wear-tracking + EOL-projection work that should
    # inform setting this.
    LP_BATTERY_WEAR_COST_PENCE_PER_KWH: float = float(
        os.getenv("LP_BATTERY_WEAR_COST_PENCE_PER_KWH", "0.0")
    )
    # Rank-based export-timing bonus (#274). On flat Outgoing-rate days the
    # spread between top-quartile and median is small (~1–2 p/kWh), so the
    # objective term ``-exp[i] × export_rate[i]`` doesn't strongly prefer
    # top-quartile slots when they happen to coincide with low PV — the LP
    # picks based on PV availability instead. This bonus adds a small extra
    # reward for exporting in the horizon's top quartile, breaking ties on
    # flat days while staying well below any genuinely profitable absolute
    # spread (so it never causes curtailment when prices are uniformly low).
    # Default 0.5 p/kWh = ~30 % of a typical flat-day spread.
    LP_PEAK_EXPORT_RANK_BONUS_PENCE_PER_KWH: float = float(
        os.getenv("LP_PEAK_EXPORT_RANK_BONUS_PENCE_PER_KWH", "0.5")
    )
    # Percentile cutoff for the rank bonus above. 25.0 = top quartile.
    # Use a smaller number (e.g. 10) for a tighter "top decile" bonus.
    LP_PEAK_EXPORT_TOP_QUARTILE_PERCENT: float = float(
        os.getenv("LP_PEAK_EXPORT_TOP_QUARTILE_PERCENT", "25")
    )
    # PV-abundance DHW preheat (PR 3 of plan; tank ceiling lift). When per-slot
    # (pv_avail − base_load − battery-charge headroom) exceeds this threshold,
    # the LP lifts the tank ceiling to ``DHW_TEMP_MAX_C`` (same surface as the
    # negative-price lift) and adds a small reward to e_dhw[i] so otherwise-
    # curtailed PV gets stored as hot water for evening showers. Default 0.5
    # kWh/slot — matches the user's empirical "afternoon set-point lift" pattern.
    DHW_PV_ABUNDANCE_THRESHOLD_KWH: float = float(
        os.getenv("DHW_PV_ABUNDANCE_THRESHOLD_KWH", "0.5")
    )
    # Reward magnitude for PV-abundance DHW heating. Per user (2026-05-09):
    # prefer tank-store over export when at home — household will use the
    # stored hot water. Default 10 p/kWh × cop_dhw 3 ≈ 30 p/kWh equivalent
    # stored value, well above 15 p export rate → tank wins. Zeroed at solve
    # time when OPTIMIZATION_PRESET == vacation (legacy travel/away values
    # translate to vacation) — household isn't there to use stored hot water;
    # revert to export-priority economics.
    LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH: float = float(
        os.getenv("LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", "10.0")
    )
    # PR I (2026-05-22) — dynamic floor on the PV-abundance tank reward.
    # Without this, when Outgoing Agile export rate exceeds the static
    # reward (e.g. 15 p > 10 p), the LP picks export over tank → PV is
    # not stored thermally. Formula applied per slot in lp_optimizer:
    # ``slot_reward = max(static, export_rate[i] + buffer)``. Battery
    # charging still wins by future-value calculation in the energy
    # balance (peak discharge ≈ 25-30 p/kWh, well above any plausible
    # export+buffer). So priority order: battery → tank → export.
    LP_PV_ABUNDANCE_TANK_BEAT_EXPORT_BUFFER_PENCE: float = float(
        os.getenv("LP_PV_ABUNDANCE_TANK_BEAT_EXPORT_BUFFER_PENCE", "2.0")
    )
    # Tank target ceiling on PV-abundance slots, distinct from
    # ``DHW_TEMP_MAX_C`` (65 °C) used on negative-price slots. The user's
    # empirical manual schedule lifts to 45 °C on solar afternoons; this
    # default 55 °C gives margin for guests + minor forecast error while
    # avoiding the heavy standing losses of holding 65 °C through the day.
    # Lower this toward 45 °C if standing-loss bleed-back becomes a concern.
    # DHW_TEMP_PV_ABUNDANCE_TARGET_C is now runtime-tunable via runtime_settings
    # (see @property below). Default lowered from 55 → 45 (= DHW_TEMP_NORMAL_C);
    # override per household occupancy at runtime without restart.
    # NOTE: ``DHW_PEAK_TANK_STRATEGY`` was removed 2026-05-21 (Epic 14, #386).
    # Prod telemetry showed the SHUTDOWN branch failed 27% of the time with
    # READ_ONLY_CHARACTERISTIC errors and produced no measurable kWh savings
    # vs the IDLE behaviour for this well-insulated tank (median decay
    # 0.00 °C/h across 8 May peak windows). The dispatch layer always uses
    # the IDLE behaviour now (tank_power=True, tank_temp=DHW_TEMP_NORMAL_C).
    # The env line in ``/srv/hem/.env`` becomes a harmless unknown; remove it
    # opportunistically on the next ``.env`` touch.
    # Post-shower overnight tank idle (LP-driven). After the LAST shower window
    # of the day, the LP has no DHW need until next-day's PV abundance — the
    # tank is set to a low BACKUP target so firmware doesn't reheat overnight,
    # but stays slightly above cold for unexpected morning showers. Reset to
    # NORMAL when LP plans the next productive slot (cheap / negative /
    # solar_charge — i.e. "next day's economics are better").
    #   38 °C default — backup buffer for unplanned morning shower.
    #   30 °C for full overnight cooling (no morning backup).
    #   45 °C effectively disables the override.
    DHW_TANK_OVERNIGHT_TARGET_C: float = float(
        os.getenv("DHW_TANK_OVERNIGHT_TARGET_C", "38.0")
    )

    # ------------------------------------------------------------------
    # PR K (2026-05-23) — DHW fixed schedule (replaces LP-driven tank).
    # ------------------------------------------------------------------
    # When True, the LP no longer emits tank-write actions. Instead,
    # ``src.dhw_policy`` writes a deterministic 2-row schedule per day:
    #   - tank_warmup at DHW_WARMUP_START_HOUR_LOCAL → DHW_SETBACK_START_HOUR_LOCAL
    #     with tank_temp = DHW_TEMP_NORMAL_C
    #   - tank_setback for the rest of the day, tank_temp = DHW_TEMP_SETBACK_C
    # Guests preset collapses to a single 24h warmup row. Vacation skips
    # the schedule entirely (Daikin firmware owns).
    # User constraint: "battery first, don't drain it overnight; no DHW
    # tariff arb beyond negative-price events." Trades ~£20-50/year of
    # DHW arbitrage savings for ZERO operational tank drama.
    DHW_FIXED_SCHEDULE_ENABLED: bool = (
        os.getenv("DHW_FIXED_SCHEDULE_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # Local-time hour the daily warmup window starts (tank → NORMAL).
    DHW_WARMUP_START_HOUR_LOCAL: int = int(
        os.getenv("DHW_WARMUP_START_HOUR_LOCAL", "13")
    )
    # Local-time hour the daily setback window starts (tank → SETBACK).
    DHW_SETBACK_START_HOUR_LOCAL: int = int(
        os.getenv("DHW_SETBACK_START_HOUR_LOCAL", "22")
    )
    # Tank setback temperature during overnight window (°C).
    # 37 °C = enough for emergency morning shower without battery drain.
    DHW_TEMP_SETBACK_C: float = float(
        os.getenv("DHW_TEMP_SETBACK_C", "37")
    )
    # Early setback on evening shower drawdown: when the tank drops ≥
    # TRIGGER_DELTA_C below its evening peak inside the armed window
    # ([ARM_HOUR, DHW_SETBACK_START_HOUR_LOCAL) local), pull the setback
    # forward to NOW instead of letting the firmware reheat the freshly-
    # drawn tank at peak price from the battery (measured ~1.0-1.6 kWh on
    # shower-heavy evenings, e.g. 38→45 °C finishing 21:53 on 2026-07-10 —
    # 7 min before the static 22:00 setback). The K2 pin already models the
    # reheat as deferred to the next warmup, so this aligns hardware with
    # the plan. Fires at most once per day (persist-once runtime_settings
    # key); heartbeat detector in state_machine._check_dhw_shower_drawdown.
    # False = instant rollback to the static setback hour only.
    DHW_EARLY_SETBACK_ENABLED: bool = (
        os.getenv("DHW_EARLY_SETBACK_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # Drop below the evening running max that counts as "the showers
    # happened" (°C). Standing loss is ~0.5 °C/h, so a fast ≥4 °C drop is
    # unambiguously a draw; sink/kitchen draws measure <2 °C.
    DHW_EARLY_SETBACK_TRIGGER_DELTA_C: float = float(
        os.getenv("DHW_EARLY_SETBACK_TRIGGER_DELTA_C", "4.0")
    )
    # Local hour the detector arms (default = evening shower window start).
    # Before this hour a drawdown is ignored — an early-evening bath must
    # not cancel the pre-shower hold.
    DHW_EARLY_SETBACK_ARM_HOUR_LOCAL: int = int(
        os.getenv("DHW_EARLY_SETBACK_ARM_HOUR_LOCAL", "20")
    )
    # Pre-cool the tank toward the device minimum during the setback that runs
    # INTO a negative-price window, so the paid boost (cold → 60 °C) absorbs the
    # most kWh while we're paid — and zero positive-price reheat happens just
    # before. Only applied when no shower window falls between setback start and
    # the boost (the boost reheats to 60 before showers). NOTE: the gain is
    # bounded by standing loss (~0.5 °C/h) — the tank can't be force-cooled, only
    # left to coast — so expect ~0.5–1 kWh, not a step change.
    DHW_TANK_PRECOOL_ENABLED: bool = (
        os.getenv("DHW_TANK_PRECOOL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    )
    DHW_TANK_PRECOOL_TARGET_C: int = int(os.getenv("DHW_TANK_PRECOOL_TARGET_C", "30"))
    # Commandable SETPOINT during negative-price slots (import price < 0 p/kWh):
    # the tank target we WRITE to the heat pump. Grid is paying us to consume, so
    # we drive the setpoint to its max. MUST stay ≤ the device's heat-pump
    # setpoint ceiling (60 °C) — Onecta rejects anything higher
    # ("Max tank temperature is 60°C", client.py:306), which made every boost
    # write FAIL (the tank got NO extra heat). Was 65 → now 60. The tank can
    # still reach 65 physically via the app's "powerful"/immersion button or the
    # legionella cycle (that's DHW_TEMP_MAX_C), but those are not commanded here.
    # The LP also BUDGETS this heat-up energy (forecast_dhw_load_per_slot ramp).
    DHW_NEGATIVE_PRICE_BOOST_C: float = float(
        os.getenv("DHW_NEGATIVE_PRICE_BOOST_C", "60")
    )
    # Sustain DHW "Powerful" through a negative-price boost window (2026-06-28).
    # Daikin Powerful is a one-shot the firmware auto-clears (timeout / on
    # reaching setpoint), so the once-at-window-start boost write leaves the
    # tank coasting for the rest of a multi-hour negative window — confirmed in
    # prod: tank 51 °C, target 60, powerful=off during −2..−5 p slots. We're
    # PAID to import then, so a heartbeat backstop (_check_negative_boost_powerful)
    # re-asserts Powerful on a bounded cadence while a tank_negative_boost slot
    # is active and the tank is still below target. Min-interval bounds the
    # Daikin 200/day quota cost (~one write per interval on negative-window days).
    # 2026-07-04 — apply the LWT outdoor cutoff to the PEAK SETBACK too (the
    # positive-boost cutoff is #540). Summer peak windows wrote a -2 offset +
    # restore daily with the heat pump not space-heating at all. false =
    # pre-2026-07-04 behaviour (setback year-round).
    DAIKIN_LWT_SETBACK_OUTDOOR_GATE: bool = (
        os.getenv("DAIKIN_LWT_SETBACK_OUTDOOR_GATE", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )

    DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_ENABLED: bool = (
        os.getenv("DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_MIN_INTERVAL_MINUTES: int = int(
        os.getenv("DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_MIN_INTERVAL_MINUTES", "15")
    )
    # No-progress backoff (2026-07-02): after STALL_LIMIT consecutive
    # re-asserts without the tank rising ≥0.5 °C (unit arbitrating Powerful
    # away — e.g. compressor DHW ceiling on hot days), stretch the interval
    # ×STALL_BACKOFF. Progress or a new episode (colder tank / 6h gap) resets.
    DHW_NEGATIVE_BOOST_REASSERT_STALL_LIMIT: int = int(
        os.getenv("DHW_NEGATIVE_BOOST_REASSERT_STALL_LIMIT", "4")
    )
    DHW_NEGATIVE_BOOST_REASSERT_STALL_BACKOFF: float = float(
        os.getenv("DHW_NEGATIVE_BOOST_REASSERT_STALL_BACKOFF", "4.0")
    )
    # --- DHW forecast auto-scale (#534) ---
    # The per-slot draw constants in forecast_dhw_load_per_slot are static and
    # drift seasonally (May 2026 measured ~3.0 kWh/day vs ~2.4 in June: warmer
    # inlet water + lower standing loss). The auto-scale multiplies the
    # schedule constants by clamp(median(measured kwh_dhw, window) / nominal
    # mode total). Median is robust to negative-price boost days. Kill switch
    # = false → factor 1.0 (raw constants).
    DHW_FORECAST_AUTOSCALE_ENABLED: bool = os.getenv(
        "DHW_FORECAST_AUTOSCALE_ENABLED", "true"
    ).lower() in ("true", "1", "yes")
    DHW_FORECAST_AUTOSCALE_WINDOW_DAYS: int = int(
        os.getenv("DHW_FORECAST_AUTOSCALE_WINDOW_DAYS", "14")
    )
    DHW_FORECAST_AUTOSCALE_MIN_DAYS: int = int(
        os.getenv("DHW_FORECAST_AUTOSCALE_MIN_DAYS", "5")
    )
    DHW_FORECAST_AUTOSCALE_MIN: float = float(
        os.getenv("DHW_FORECAST_AUTOSCALE_MIN", "0.5")
    )
    DHW_FORECAST_AUTOSCALE_MAX: float = float(
        os.getenv("DHW_FORECAST_AUTOSCALE_MAX", "1.6")
    )
    # --- DHW bucket-bias shape corrector ---
    # Multiplicative per-LOCAL-2h-bucket factor on the pinned DHW forecast,
    # learned nightly from dhw_error_log by an OPEN-LOOP estimator: each row
    # records the factor in force when its forecast was committed, learning
    # de-biases (raw = forecast/factor) and takes the recency-weighted
    # ratio-of-sums directly — NO prior-state accumulation (the first,
    # PV-style damped-accumulation cut compounded geometrically while
    # disabled and limit-cycled while enabled; killed in adversarial review).
    # SHAPE-only: application normalizes against the nominal bucket shares so
    # the daily total is preserved — the auto-scale above owns the level and
    # is open-loop w.r.t. the committed forecast. NORMAL mode only (guests is
    # comfort-critical, vacation ~0). Default OFF (house convention for
    # correctors feeding the LP; here the value becomes a HARD K2 equality):
    # the table refreshes and is observable regardless; enable after the
    # backtest endpoint (/api/v1/dhw/error-log/backtest) shows out-of-sample
    # MAE reduction over ~7 days — with open-loop learning the backtest
    # evaluates exactly the factors production would apply.
    DHW_BUCKET_BIAS_ENABLED: bool = os.getenv(
        "DHW_BUCKET_BIAS_ENABLED", "false"
    ).lower() in ("true", "1", "yes")
    DHW_BUCKET_BIAS_WINDOW_DAYS: int = int(
        os.getenv("DHW_BUCKET_BIAS_WINDOW_DAYS", "14")
    )
    DHW_BUCKET_BIAS_HALFLIFE_DAYS: float = float(
        os.getenv("DHW_BUCKET_BIAS_HALFLIFE_DAYS", "5")
    )
    # Clamp: daytime summer over-forecast needs ~0.25-0.33; the observed 4x
    # warmup under-forecast is mostly the flip side inside a total-preserving
    # shape (shrinking daytime buckets boosts warmup via normalization), so
    # 3.0 suffices pre-normalization. Factors AT a clamp are logged WARNING
    # by the refresh (true ratio beyond the clamp — inspect before enabling).
    DHW_BUCKET_BIAS_MIN: float = float(os.getenv("DHW_BUCKET_BIAS_MIN", "0.25"))
    DHW_BUCKET_BIAS_MAX: float = float(os.getenv("DHW_BUCKET_BIAS_MAX", "3.0"))
    # RAW-forecast-side floor (ratio denominator; raw so a shrunk bucket can't
    # starve its own learning). Asymmetric on purpose: low/zero ACTUAL rows
    # are kept — they ARE the over-forecast signal. NULL actuals are dropped.
    DHW_BUCKET_BIAS_MIN_FORECAST_KWH: float = float(
        os.getenv("DHW_BUCKET_BIAS_MIN_FORECAST_KWH", "0.05")
    )
    DHW_BUCKET_BIAS_MIN_DAYS: int = int(os.getenv("DHW_BUCKET_BIAS_MIN_DAYS", "3"))
    # A stored table staler than this is treated as absent at application (a
    # vacation / Daikin outage must not leave a fossil correction applying).
    DHW_BUCKET_BIAS_MAX_AGE_DAYS: int = int(
        os.getenv("DHW_BUCKET_BIAS_MAX_AGE_DAYS", "7")
    )
    # One-shot Telegram ping when the enable gate is met (still disabled +
    # >= MIN_DAYS distinct usable days + out-of-sample MAE improvement).
    # Never auto-enables; re-arm by clearing the `dhw_bias_enable_suggested_at`
    # runtime setting.
    DHW_BUCKET_BIAS_SUGGEST_ENABLED: bool = os.getenv(
        "DHW_BUCKET_BIAS_SUGGEST_ENABLED", "true"
    ).lower() in ("true", "1", "yes")
    DHW_BUCKET_BIAS_SUGGEST_MIN_DAYS: int = int(
        os.getenv("DHW_BUCKET_BIAS_SUGGEST_MIN_DAYS", "7")
    )
    # Out-of-sample MAE improvement must clear this % (and 0.01 kWh absolute)
    # before the ping fires — a one-shot prompt burned on a rounding artefact
    # never re-arms.
    DHW_BUCKET_BIAS_SUGGEST_MIN_PCT: float = float(
        os.getenv("DHW_BUCKET_BIAS_SUGGEST_MIN_PCT", "5.0")
    )
    # --- Legionella heat-up BUDGET (#643, 2026-07-05) ---
    # The firmware's weekly thermal-shock cycle draws real energy the LP must
    # plan for (measured ~3-3.5 kWh electric: 37→60 °C on 200 L + hold at the
    # poor COP of a 60 °C lift). Budgeted evenly across the standoff window in
    # forecast_dhw_load_per_slot; 0 (or the flag) disables and restores the
    # pre-#643 behavior where the battery discharged into the un-budgeted
    # cycle and hit the SoC floor mid-heat-up.
    DHW_LEGIONELLA_BUDGET_ENABLED: bool = os.getenv(
        "DHW_LEGIONELLA_BUDGET_ENABLED", "true"
    ).lower() in ("true", "1", "yes")
    DHW_LEGIONELLA_BUDGET_KWH: float = float(
        os.getenv("DHW_LEGIONELLA_BUDGET_KWH", "3.5")
    )
    # Intraday Daikin consumption rollups (10:05/14:05/18:05 UTC) so today's
    # heat-pump split reaches the Consumption panel same-day. 1 quota call per
    # run (3/day); false = nightly-only (pre-2026-07-05 behavior).
    DAIKIN_CONSUMPTION_INTRADAY_ENABLED: bool = os.getenv(
        "DAIKIN_CONSUMPTION_INTRADAY_ENABLED", "true"
    ).lower() in ("true", "1", "yes")
    # --- Legionella thermal-shock STAND-OFF (2026-06-07) ---
    # The Onecta firmware owns the DHW tank during its weekly thermal-shock
    # cycle. Any tank PATCH HEM sends in that window is arbitrated/overridden by
    # the firmware → wasted Daikin quota + churn + READ_ONLY. So the reconciler
    # SKIPS tank-device writes during a configured window (LWT / space heating
    # are NOT affected — legionella is a DHW-tank cycle only). This does NOT
    # schedule the cycle (firmware does) and is unrelated to the removed
    # DHW_LEGIONELLA_* scheduling vars — it only tells HEM when to stand off the
    # tank. Window is defined in UTC on one weekday; must not cross midnight.
    # Default: Sunday (weekday 6) 11:00 UTC for 120 min — covers the ramp from
    # the overnight setback (~37 °C) up to ~60 °C plus the firmware's ~1 h hold.
    DHW_LEGIONELLA_STANDOFF_ENABLED: bool = (
        os.getenv("DHW_LEGIONELLA_STANDOFF_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    DHW_LEGIONELLA_STANDOFF_DOW: int = int(  # Mon=0 .. Sun=6 (datetime.weekday())
        os.getenv("DHW_LEGIONELLA_STANDOFF_DOW", "6")
    )
    DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC: int = int(
        os.getenv("DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", "11")
    )
    DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC: int = int(
        os.getenv("DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", "0")
    )
    DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES: int = int(
        os.getenv("DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", "120")
    )
    # NOTE: the leading-warmup defer (a boost SUPERSEDES the warmup that would
    # otherwise pre-heat at a positive price right before it) is driven by
    # LP_PRE_NEGATIVE_PRECOOL_HOURS below — the SAME window the LP's energy
    # forecast (forecast_dhw_load_per_slot) uses to pre-cool. Sharing one knob
    # keeps the fired tank actions and the LP's budgeted DHW import consistent
    # by construction (no separate DHW_WARMUP_BOOST_OVERRIDE_LEAD_MINUTES).
    # Forecast night-temperature bias (#324). DEFAULT 0.0 = OFF — do not arm
    # this without fresh evidence.
    #
    # The original -3.0 was calibrated on ONE cold observation (2026-05-12:
    # pred 8.1 °C / actual 5.0 °C at 23 UTC). It was disabled on prod
    # 2026-06-12 because the learned per-hour microclimate offset
    # (``get_micro_climate_offset_by_hour_c``, fed by ``forecast_skill_log``)
    # already corrects the sensor-vs-forecast gap ADAPTIVELY — so a static -3
    # DOUBLE-CORRECTS. By June the raw night residual was only +0.2..+0.7 °C.
    # A LP that budgets every night ~3 °C colder than reality over-heats all
    # winter.
    #
    # The default stayed -3.0 in code long after prod pinned 0 in .env, so
    # every dev/test/fresh deploy silently ran the double-correction. Default
    # is now 0.0: prod behaviour is unchanged, everything else is fixed.
    FORECAST_NIGHT_TEMP_BIAS_C: float = float(
        os.getenv("FORECAST_NIGHT_TEMP_BIAS_C", "0.0")
    )
    FORECAST_NIGHT_START_HOUR_UTC: int = int(
        os.getenv("FORECAST_NIGHT_START_HOUR_UTC", "21")
    )
    FORECAST_NIGHT_END_HOUR_UTC: int = int(
        os.getenv("FORECAST_NIGHT_END_HOUR_UTC", "6")
    )
    # Toggle the post-shower overnight override. Default true. Set to false
    # to let overnight slots fall back to plain "standard" (no Daikin write,
    # firmware does whatever it wants based on its own schedule).
    DHW_TANK_OVERNIGHT_IDLE_ENABLED: str = os.getenv("DHW_TANK_OVERNIGHT_IDLE_ENABLED", "true")
    # Static-physics DHW draw model (precursor to V11-C #196 learned prior).
    # Total litres of hot water drawn per day, distributed evenly across
    # configured shower-window slots. The LP subtracts the corresponding
    # thermal energy from the tank balance so it sees realistic post-shower
    # temperature drops instead of just standing-loss.
    #
    # Default 144 L = household of 4 (2 adults + 2 children sharing one
    # shower together) × 8 min/shower × 6 L/min (low-flow eco showerhead).
    # Adjust if your shower flow is higher (e.g. 8-10 L/min standard) — at
    # 8 L/min the same usage = 192 L/day.
    #
    # Set to 0 to disable (LP reverts to previous behavior — only standing
    # loss modeled).
    DHW_DAILY_SHOWER_LITRES: float = float(os.getenv("DHW_DAILY_SHOWER_LITRES", "144"))
    # Cold-mains inlet temp (°C). UK average ~10°C; varies seasonally.
    DHW_COLD_INLET_TEMP_C: float = float(os.getenv("DHW_COLD_INLET_TEMP_C", "10.0"))
    # Use temperature at the tap (°C) — typical mixer-tap shower temp.
    # Used to compute hot-water litres drawn from tank: hot = mix × (use - cold) / (tank - cold).
    DHW_USAGE_TEMP_C: float = float(os.getenv("DHW_USAGE_TEMP_C", "40.0"))
    # Slack penalty on the soft DHW ceiling (#225 item 1, was hardcoded 0.01).
    # Operators who want stricter comfort vs cost can tune this. Default 0.01
    # p/°C-slot preserves prior behaviour.
    LP_TANK_HI_SLACK_PENCE_PER_DEGC_SLOT: float = float(
        os.getenv("LP_TANK_HI_SLACK_PENCE_PER_DEGC_SLOT", "0.01")
    )
    # Shower-floor slack penalty (pence per °C-slot of breach). Heavy default
    # (50 p / K-slot) so the LP only violates when physically forced — see
    # PR #344. The slack becomes the "by how many K did the tank miss the
    # shower floor" diagnostic. Lowering it weakens the comfort guarantee;
    # raising it risks Infeasible if other constraints already strain.
    LP_SHOWER_LO_PENALTY_PENCE_PER_DEGC_SLOT: float = float(
        os.getenv("LP_SHOWER_LO_PENALTY_PENCE_PER_DEGC_SLOT", "50.0")
    )
    # Comma-separated trigger reasons for which scenario LP runs. Triggers
    # not in this list use only the nominal solve.
    # ``octopus_fetch`` is the natural pre-peak fire (default 16:05 local,
    # right after Octopus publishes the next-day rates). Including it here
    # means the run that sees fresh prices ALSO does the 3-pass scenario
    # robustness check, ~55 min before the typical 17:00 BST peak window.
    # ``tier_boundary`` (V12) fires N minutes before any tariff tier change
    # computed by tiers.classify_day — these are high-stakes pre-transition
    # decisions where the scenario robustness check earns its keep.
    # The legacy ``cron`` trigger reason was removed in V12 (fixed-hour
    # cron is gone); the system is fully event-driven.
    # #668: all mid-day event-driven re-solves (soc_drift, import_overshoot,
    # pv_upside/pv_downside, load_upside, forecast_revision, dynamic_replan,
    # appliance_armed) are included by default too — otherwise a drift-
    # triggered afternoon replan ran a single nominal solve with NO
    # pessimistic charge floor and could under-charge vs what the overnight
    # plan guaranteed for the evening peak (under-charging costs ~4× over-
    # charging per the 2026-07 LP audit).
    # Cost: solve_scenarios_with_nominal reuses the nominal plan and runs
    # only the 2 side scenarios, in parallel worker threads (~one extra
    # solve of wall-clock, 3-4s typical), plus a possible charge-floor
    # re-solve — a soc_drift replan goes from ~4s to ~10s typical; worst
    # case ~90-100s, bounded by LP_CBC_TIME_LIMIT_SECONDS=30 per solve
    # (nominal ≤30s + parallel sides ≤30s wall + floor re-solve ≤30s).
    # This runs on the heartbeat thread / APScheduler workers — never the
    # asyncio event loop — and stays under the drift triggers'
    # MPC_COOLDOWN_SECONDS=300 (stamped at solve completion, so slower
    # solves can't cause replan thrash). appliance_armed bypasses that
    # cooldown and is instead rate-bounded by the heartbeat's remote-mode
    # transition detector.
    # ``manual`` is deliberately EXCLUDED: it is an interactive request
    # (MCP/web propose) where latency matters, not a drift context where
    # the charge floor earns its keep.
    LP_SCENARIOS_ON_TRIGGER_REASONS: str = os.getenv(
        "LP_SCENARIOS_ON_TRIGGER_REASONS",
        "plan_push,octopus_fetch,tier_boundary,"
        "soc_drift,import_overshoot,pv_upside,pv_downside,load_upside,"
        "forecast_revision,dynamic_replan,appliance_armed",
    )
    TARGET_ROOM_TEMP_MIN_C: float = float(os.getenv("TARGET_ROOM_TEMP_MIN_C", "18.0"))
    TARGET_ROOM_TEMP_MAX_C: float = float(os.getenv("TARGET_ROOM_TEMP_MAX_C", "23.0"))
    TARGET_DHW_TEMP_MIN_NORMAL_C: float = float(os.getenv("TARGET_DHW_TEMP_MIN_NORMAL_C", "45.0"))
    TARGET_DHW_TEMP_MIN_GUESTS_C: float = float(os.getenv("TARGET_DHW_TEMP_MIN_GUESTS_C", "48.0"))
    TARGET_DHW_TEMP_MAX_C: float = float(os.getenv("TARGET_DHW_TEMP_MAX_C", "65.0"))
    # PR 4 of plan: time-of-day shower schedule (LP-side hard floor only on
    # slots inside a configured shower window). Multi-window comma-separated
    # list, e.g. ``"19:00-22:00"`` (default; user has no morning showers
    # normally) or ``"07:00-09:00,19:00-22:00"``. When empty, the LP falls
    # back to the legacy ``LP_SHOWER_MORNING_LOCAL`` / ``LP_SHOWER_EVENING_LOCAL``
    # scalars + ``LP_SHOWER_WINDOW_MINUTES`` for backward-compatibility.
    DHW_SHOWER_SCHEDULE: str = os.getenv("DHW_SHOWER_SCHEDULE", "19:00-22:00")
    # When ``PLAN_GUESTS_TODAY=true`` is active (manual toggle, eventually
    # auto-detected via #197), the LP uses this schedule instead — re-enables
    # morning showers + keeps evening.
    DHW_SHOWER_SCHEDULE_GUESTS: str = os.getenv(
        "DHW_SHOWER_SCHEDULE_GUESTS", "07:00-09:00,19:00-22:00"
    )
    # Tank temperature floor used at horizon-end when the last slot is NOT
    # inside any shower window. Lets the LP let the tank cool overnight
    # without forcing an expensive late-night reheat just to satisfy a 48 h
    # horizon terminal constraint. User OK'd 30 °C as the aggressive default;
    # raise toward 37 °C if cold-spot reheat times become a comfort issue.
    DHW_TEMP_MIN_FLOOR_C: float = float(os.getenv("DHW_TEMP_MIN_FLOOR_C", "30.0"))
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
    # Device range is ±10. Raised 5→10 (2026-06-06): the Daikin firmware applies
    # the offset relative to its OWN weather curve and decides whether to heat,
    # so a high offset doesn't force pointless heating — it lets paid windows
    # push the radiator water to the top of the operating range.
    OPTIMIZATION_LWT_OFFSET_MAX: float = float(os.getenv("OPTIMIZATION_LWT_OFFSET_MAX", "10"))
    # --- Heuristic LWT pre-heat (#481) — active space-heating control ---------
    # When enabled, HEM drives the Daikin leaving-water-temperature OFFSET by a
    # simple price-tier rule: boost in cheap slots (pre-heat the house) and set
    # back in peak slots (coast on thermal mass). Offset-only — the firmware's
    # weather curve still decides WHEN to heat; we only nudge the water temp when
    # it's already heating. Open-loop (no room sensor yet) — the offset is
    # clamped to OPTIMIZATION_LWT_OFFSET_MIN/MAX and written as an integer; the
    # firmware's own curve is the comfort floor. Default OFF (climate hands-off).
    DAIKIN_LWT_PREHEAT_ENABLED: bool = os.getenv(
        "DAIKIN_LWT_PREHEAT_ENABLED", "false"
    ).lower() in ("true", "1", "yes")
    DAIKIN_LWT_PREHEAT_BOOST_C: int = int(os.getenv("DAIKIN_LWT_PREHEAT_BOOST_C", "3"))
    DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C: int = int(os.getenv("DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", "-2"))
    # NEGATIVE (paid-to-import) slots: push the space-heating offset to the TOP
    # of the operating range instead of the modest cheap-slot boost — bank the
    # most thermal mass into the building while the grid pays us, alongside the
    # already-maxed tank. Bounded above by the firmware heating cutoff
    # (DAIKIN_WEATHER_CURVE_HIGH_C — no heat when it's mild out) and below by the
    # OPTIMIZATION_LWT_OFFSET_MAX clamp; the comfort guard suppresses it once a
    # room sensor exists. Default = the current offset ceiling (+5). Raise
    # OPTIMIZATION_LWT_OFFSET_MAX (and this) for an even wider paid-window range.
    DAIKIN_LWT_PREHEAT_NEGATIVE_BOOST_C: int = int(os.getenv("DAIKIN_LWT_PREHEAT_NEGATIVE_BOOST_C", "10"))
    # Comfort dead-band (°C) used by the sensor-ready guard: once a real room
    # temperature is available, suppress boost above SETPOINT+band and suppress
    # setback below SETPOINT-band. No-op while indoor_temp telemetry is absent.
    DAIKIN_LWT_PREHEAT_COMFORT_BAND_C: float = float(
        os.getenv("DAIKIN_LWT_PREHEAT_COMFORT_BAND_C", "0.5")
    )
    # Thermal coherence: a building's thermal mass has a multi-hour time
    # constant, so toggling the LWT offset for short price wiggles is both
    # ineffective and wasteful of the Daikin quota. Smooth the per-slot offset
    # sequence so it changes only in sustained blocks: short 0-gaps between two
    # equal non-zero blocks are bridged, and any boost/setback block shorter
    # than this many half-hour slots is dropped to neutral. Default 4 = 2 h.
    DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS: int = int(
        os.getenv("DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", "4")
    )
    # Demand gate (#540 quick win, June-2026 incident): a positive LWT offset
    # can WAKE the compressor the firmware would have left off — measured
    # space heating went 0 → 3-8 kWh/day in June within days of enabling the
    # pre-heat. There is no point pre-heating thermal mass the house is not
    # draining, so offset action rows are only written when the trailing
    # window shows real measured space-heating demand (Onecta split). When a
    # cold snap starts, the firmware heats naturally within hours and the
    # gate opens on the next plan. Set the kWh floor to 0 to disable.
    DAIKIN_LWT_PREHEAT_MIN_TRAILING_HEATING_KWH: float = float(
        os.getenv("DAIKIN_LWT_PREHEAT_MIN_TRAILING_HEATING_KWH", "0.5")
    )
    DAIKIN_LWT_PREHEAT_DEMAND_LOOKBACK_HOURS: int = int(
        os.getenv("DAIKIN_LWT_PREHEAT_DEMAND_LOOKBACK_HOURS", "48")
    )
    # Outdoor-temperature cutoff for POSITIVE LWT offsets (Tracked by #540).
    # The demand gate above is endogenous (trailing measured heating) and can
    # be fooled by its own output: a positive offset (cheap +BOOST or
    # negative-price +NEGATIVE_BOOST) WAKES the compressor, and some of that
    # HEM-induced heat bleeds into 2-h buckets just AFTER the offset window
    # (thermal lag) → counted as natural demand → gate latches open (the live
    # June-2026 self-loop: heating 0 → ~4.6 kWh/day once active LWT was on).
    # Outdoor temperature is an EXOGENOUS signal the loop cannot fake: above a
    # heating-degree-day base (~15.5 °C, CIBSE) a UK heat-pump house needs
    # little/no space heat, so positive offsets are suppressed PER SLOT against
    # the micro-climate-calibrated forecast temp. The NEGATIVE peak setback
    # (-2) is never cut — it cannot wake the compressor. Default 15 sits just
    # below the firmware's own 18 °C ambient heating cutoff
    # (DAIKIN_WEATHER_CURVE_HIGH_C) on purpose. Set high (e.g. 99) to disable.
    DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C: float = float(
        os.getenv("DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", "15.0")
    )
    # Thermal-lag tail exclusion for the demand-gate decontamination: also drop
    # this many 2-h buckets AFTER each offset window from the measured-heating
    # read, so offset-induced heat that bleeds past the window close cannot
    # re-open the gate during convergence. Default 1 (~2 h). 0 = old behaviour.
    DAIKIN_LWT_PREHEAT_DECONTAM_TAIL_BUCKETS: int = int(
        os.getenv("DAIKIN_LWT_PREHEAT_DECONTAM_TAIL_BUCKETS", "1")
    )
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
    # V8 LP — solver. We standardize on CBC; the HiGHS branch was implemented
    # but never used in this system and was removed to avoid dead code.
    # Any non-cbc value here logs an info line and falls back to CBC.
    LP_SOLVER: str = (os.getenv("LP_SOLVER") or "cbc").strip().lower()
    LP_CBC_TIME_LIMIT_SECONDS: int = int(os.getenv("LP_CBC_TIME_LIMIT_SECONDS", "30"))
    LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT: float = float(
        os.getenv("LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT", "100")
    )
    LP_CYCLE_PENALTY_PENCE_PER_KWH: float = float(os.getenv("LP_CYCLE_PENALTY_PENCE_PER_KWH", "0.0001"))
    # PV curtailment penalty: pence per kWh of solar the LP throws away. Without this, ``pv_curt``
    # has zero objective coefficient — the LP happily curtails PV during deep-negative ForceCharge
    # slots because grid imp at -7p ties or beats PV's zero direct value (battery is fungible
    # given the chg cap). Setting the penalty to ``EXPORT_RATE_PENCE`` makes curtailment
    # cost-equivalent to "would have exported," which restores the right ranking: prefer PV→battery
    # over grid→battery when both compete for the same chg cap. 0 = legacy behaviour (no penalty).
    LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH: float = float(
        os.getenv("LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", os.getenv("EXPORT_RATE_PENCE", "15.0"))
    )
    # 2026-06-29: make the PV-curtailment penalty export-aware on NEGATIVE-price
    # slots. The flat penalty (= EXPORT_RATE_PENCE, 15p) assumes PV "would have
    # exported at 15p", but deep-negative windows are solar oversupply where the
    # Outgoing rate is usually <= 0 (export impossible/unprofitable) AND we're PAID
    # to import. With the flat penalty (15p > any realistic |neg price|) the LP
    # never curtails PV in negatives → self-consumes it instead of importing from
    # the paid grid (prod 2026-06-12: ~£0.38 paid import forgone in one −8.79p
    # window; solver A/B paid-import £0.25→£0.70). When true, the penalty on a
    # price<0 slot becomes max(0, that slot's Outgoing rate): → 0 when export is
    # unprofitable (curtail + grid-charge), = the rate when export is valuable (so
    # the LP EXPORTS the PV instead of throwing it away — adversarially tested).
    # NB the within-slot PV/grid split is largely set by the inverter (Fox
    # prioritises PV→battery), so the real-world gain is mostly an honest
    # negative-window model + better ForceCharge timing, not a large £/yr. Set
    # false to revert to the uniform flat penalty.
    LP_NEG_SLOT_NO_CURTAIL_PENALTY: bool = (
        os.getenv("LP_NEG_SLOT_NO_CURTAIL_PENALTY", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # Pre-plunge discipline lookahead (hours): when a negative-price slot is
    # within this window AHEAD of a positive-price slot, forbid grid→battery
    # charge on that positive slot (PV→battery still allowed). Reserves
    # import capacity for the negative window so the LP can max out its 3680 W
    # ForceCharge there. Capped to the LP horizon implicitly. 0 disables.
    # Bounded to 12 h by default after the 2026-05-02 LP audit found that an
    # unbounded look-ahead (the previous behaviour) starved the battery on
    # days where the negative slot was >24 h ahead — only 33% of charge slots
    # were in the cheap quartile on those days.
    LP_PLUNGE_PREP_HOURS: int = int(os.getenv("LP_PLUNGE_PREP_HOURS", "12"))

    # Pre-negative ACTIVE pre-positioning: within LP_PLUNGE_PREP_HOURS of a
    # negative window, ALLOW the battery to drain to the grid (force export,
    # selling high) so it has maximum headroom to absorb paid import during the
    # negative window. This only sets *eligibility* — the LP objective decides
    # whether and how much to drain (export revenue net of cycle penalty and
    # buy-back cost); there is no economic threshold gate. This DELIBERATELY
    # reverses the standard "no battery export in normal/guests" rule (PR D)
    # for the pre-negative window only — set false to disable.
    # NOTE: LP_PRE_NEGATIVE_EXPORT_MARGIN_PENCE was REMOVED — it was an arbitrary
    # 2p cliff that could toggle drain availability on small export-rate moves.
    LP_PRE_NEGATIVE_PREP_ENABLED: bool = (
        os.getenv("LP_PRE_NEGATIVE_PREP_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # Pre-cool the DHW tank for this many hours before a negative window: don't
    # re-warm it (let it coast to setback) so it has maximum headroom to absorb
    # the boost-to-max during the negative window. Short + shower-safe (never
    # overrides a shower-reheat slot). 0 disables active pre-cool.
    LP_PRE_NEGATIVE_PRECOOL_HOURS: float = float(
        os.getenv("LP_PRE_NEGATIVE_PRECOOL_HOURS", "3")
    )

    # --- PV-sufficiency guard rail (incident 2026-05-15; docs/PV_TRUST_GUARDRAIL.md)
    # In ``strict_savings`` mode, block grid → battery for every today-slot
    # strictly before the first peak-tariff slot when ``Σ forecast PV today ×
    # MARGIN ≥ (battery headroom + Σ load today)``. ``MARGIN < 1`` demands
    # extra cushion before the rail fires (more grid-charging allowed);
    # ``> 1`` is more aggressive. Inert under ``savings_first``.
    LP_PV_SUFFICIENCY_GUARD: bool = (
        os.getenv("LP_PV_SUFFICIENCY_GUARD", "true").strip().lower() in ("1", "true", "yes")
    )
    LP_PV_SUFFICIENCY_MARGIN: float = float(os.getenv("LP_PV_SUFFICIENCY_MARGIN", "1.0"))

    # Inverter stress cost: piecewise-linear quadratic approximation on battery power per slot.
    # At nominal inverter power (MAX_INVERTER_KW), the penalty equals this value (p/kWh).
    # 0 = disabled. Recommended: 0.05–0.20. Works alongside or instead of TV penalties.
    LP_INVERTER_STRESS_COST_PENCE: float = float(os.getenv("LP_INVERTER_STRESS_COST_PENCE", "0.10"))
    # Segments for piecewise-linear stress approximation (higher = more accurate, slower; 6–10 is good)
    LP_INVERTER_STRESS_SEGMENTS: int = int(os.getenv("LP_INVERTER_STRESS_SEGMENTS", "8"))
    # HP on/off minimum ON time: minimum slots the heat-pump must run once
    # started (anti-cycling). Default lowered from 2 → 1 on 2026-05-20 (audit
    # finding): the Daikin Altherma firmware enforces its own minimum-cycle
    # protection on the compressor, so the LP's per-startup binary
    # (startup_i + sum constraints, ~N extra integers per solve) is
    # redundant and inflates the MILP. Set to 2 again only if observed
    # plan-vs-reality divergence on short HP bursts.
    LP_HP_MIN_ON_SLOTS: int = int(os.getenv("LP_HP_MIN_ON_SLOTS", "1"))
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
    # Daikin: minimum consecutive slots (half-hours) for a non-standard window to be scheduled.
    # Cheap/negative windows shorter than this are merged forward or dropped to avoid rapid heat-pump
    # cycling.  2 = 1 hour minimum (recommended).  0 = disabled (legacy behaviour, any length).
    DAIKIN_MIN_WINDOW_SLOTS: int = int(os.getenv("DAIKIN_MIN_WINDOW_SLOTS", "2"))
    # Delay between critical Onecta writes (climate power, DHW) so the 3-way valve can settle (#18).
    # 0 = skip sleeps (tests).
    DAIKIN_VALVE_SETTLE_SECONDS: int = int(os.getenv("DAIKIN_VALVE_SETTLE_SECONDS", "10"))
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
    # --- DHW calibration (#714 rewrite) -------------------------------------
    # The tank's UA and effective ambient, learned from the THERMOMETER only
    # (never the energy counter — that is quantised and half-synthesised, #719).
    # false → every solve uses the databook TankParams; instant rollback of the
    # learned values without touching the nightly fit.
    DHW_CALIBRATION_ENABLED: bool = os.getenv(
        "DHW_CALIBRATION_ENABLED", "true"
    ).lower() == "true"
    DHW_CALIBRATION_WINDOW_DAYS: int = int(os.getenv("DHW_CALIBRATION_WINDOW_DAYS", "21"))
    # A summer-fitted ambient must not steer a winter plan: past this age the reader
    # falls back to the databook and logs it.
    DHW_CALIBRATION_MAX_AGE_DAYS: float = float(
        os.getenv("DHW_CALIBRATION_MAX_AGE_DAYS", "45")
    )
    # The LP times the tank itself (src/dhw), instead of following dhw_policy's fixed
    # 13:00→45 / 22:00→37 clock. Ships OFF: it must first be PROVEN better by the
    # economic shadow. Precedence passive > LP-owned > K1 pin; flag off is
    # byte-identical, and flipping it back restores the fixed schedule on the next
    # re-plan (dhw_policy stays intact as the kill switch).
    DHW_LP_OWNED_ENABLED: bool = os.getenv(
        "DHW_LP_OWNED_ENABLED", "false"
    ).lower() == "true"
    # The economic shadow runs the LP-owned regime on every committed solve (while the
    # flag above is off) and logs what it WOULD have cost + whether it kept comfort. It
    # feeds the enable gate; nothing it plans is dispatched. Backtest over 20 real days:
    # LP-owned cheaper on 18/20, median −10.8 p/day, zero comfort breaches.
    DHW_LP_OWNED_SHADOW_ENABLED: bool = os.getenv(
        "DHW_LP_OWNED_SHADOW_ENABLED", "true"
    ).lower() == "true"
    DHW_LP_OWNED_SHADOW_MAX_PER_DAY: int = int(
        os.getenv("DHW_LP_OWNED_SHADOW_MAX_PER_DAY", "4")
    )
    # The gate: enable is only SUGGESTED (one-shot ping, never automatic) after this
    # many shadow days that are BOTH cheaper by the median saving AND comfort-clean.
    DHW_LP_OWNED_GATE_MIN_DAYS: int = int(os.getenv("DHW_LP_OWNED_GATE_MIN_DAYS", "14"))
    DHW_LP_OWNED_GATE_MIN_SAVING_PENCE: float = float(
        os.getenv("DHW_LP_OWNED_GATE_MIN_SAVING_PENCE", "3.0")
    )
    DHW_LP_OWNED_GATE_MAX_ROWS: int = int(os.getenv("DHW_LP_OWNED_GATE_MAX_ROWS", "6"))
    DHW_WATER_CP: float = float(os.getenv("DHW_WATER_CP", "4186"))  # J/(kg·K)
    # Building envelope + thermal mass for the single-zone model (estimator
    # fallback today; the LP t_in restore in #540 will consume these too).
    # MEASURED 2026-06-12 (docs/WINTER_THERMAL_MODEL.md §2.1): 120-day
    # regression of fox daily load vs heating degree-days gives
    # UA_eff ≈ 520-730 W/K (≈630 @ COP 3, R²=0.66). The old 180 W/K
    # placeholder made the estimator ~3.5× too optimistic about heat
    # retention. Thermal mass 12 kWh/K encodes a τ ≈ 20 h prior for UK
    # masonry at this UA; both get replaced by the W2 thermal learner once
    # the indoor sensors land (#540).
    BUILDING_UA_W_PER_K: float = float(os.getenv("BUILDING_UA_W_PER_K", "600"))
    BUILDING_THERMAL_MASS_KWH_PER_K: float = float(os.getenv("BUILDING_THERMAL_MASS_KWH_PER_K", "12.0"))
    # --- W2 thermal learner (#540) ---
    # Learns τ from unheated overnight indoor decay + re-fits UA from the HDD
    # regression with MEASURED indoor daily means; C = τ·UA. No-op (quality
    # gates report 'skipped') while room_temperature_history is empty.
    # THERMAL_LEARNING_ENABLED stops the 05:30 UTC cron;
    # THERMAL_LEARNED_VALUES_ENABLED=false makes every reader fall back to the
    # env constants above instantly (the consumer-side kill switch).
    THERMAL_LEARNING_ENABLED: bool = os.getenv(
        "THERMAL_LEARNING_ENABLED", "true"
    ).lower() in ("true", "1", "yes")
    THERMAL_LEARNED_VALUES_ENABLED: bool = os.getenv(
        "THERMAL_LEARNED_VALUES_ENABLED", "true"
    ).lower() in ("true", "1", "yes")
    THERMAL_TAU_WINDOW_DAYS: int = int(os.getenv("THERMAL_TAU_WINDOW_DAYS", "21"))
    THERMAL_TAU_MIN_EPISODES: int = int(os.getenv("THERMAL_TAU_MIN_EPISODES", "5"))
    THERMAL_TAU_MIN_EPISODE_HOURS: float = float(
        os.getenv("THERMAL_TAU_MIN_EPISODE_HOURS", "4")
    )
    # Radiators + the hydronic loop keep emitting after the compressor stops
    # (thermal inertia of the emitters/pipework) — a decay episode may only
    # START this many hours after the last heating activity.
    THERMAL_TAU_SETTLE_HOURS: float = float(os.getenv("THERMAL_TAU_SETTLE_HOURS", "2.0"))
    # ΔT(indoor − outdoor) below this makes τ unidentifiable (decay signal
    # drowns in sensor noise) — warm summer nights are skipped honestly.
    THERMAL_TAU_MIN_DELTA_T_C: float = float(os.getenv("THERMAL_TAU_MIN_DELTA_T_C", "5.0"))
    THERMAL_TAU_MIN_R2: float = float(os.getenv("THERMAL_TAU_MIN_R2", "0.8"))
    THERMAL_TAU_MIN_HOURS: float = float(os.getenv("THERMAL_TAU_MIN_HOURS", "5"))
    THERMAL_TAU_MAX_HOURS: float = float(os.getenv("THERMAL_TAU_MAX_HOURS", "100"))
    THERMAL_TAU_NIGHT_START_HOUR_LOCAL: int = int(
        os.getenv("THERMAL_TAU_NIGHT_START_HOUR_LOCAL", "21")
    )
    THERMAL_TAU_NIGHT_END_HOUR_LOCAL: int = int(
        os.getenv("THERMAL_TAU_NIGHT_END_HOUR_LOCAL", "8")
    )
    # A 2h bucket with measured space heating above this contaminates a decay
    # episode; DHW gets a much higher floor (separate circuit — only heavy
    # boosts leak meaningful heat into the envelope).
    THERMAL_HEATING_CONTAM_KWH: float = float(os.getenv("THERMAL_HEATING_CONTAM_KWH", "0.1"))
    THERMAL_DHW_CONTAM_KWH: float = float(os.getenv("THERMAL_DHW_CONTAM_KWH", "0.8"))
    # Bounded by the meteo retention (METEO_FORECAST_HISTORY_RETENTION_DAYS,
    # ~30): a larger window would silently fit on whatever subset has outdoor
    # coverage while recording the configured window as provenance. 30 winter
    # days with HDD spread satisfy the min-days gate; the doc's 120-day
    # regression remains the offline reference.
    THERMAL_UA_WINDOW_DAYS: int = int(os.getenv("THERMAL_UA_WINDOW_DAYS", "30"))
    THERMAL_UA_MIN_HDD_DAYS: int = int(os.getenv("THERMAL_UA_MIN_HDD_DAYS", "20"))
    THERMAL_UA_ASSUMED_COP: float = float(os.getenv("THERMAL_UA_ASSUMED_COP", "3.0"))
    THERMAL_UA_MIN_R2: float = float(os.getenv("THERMAL_UA_MIN_R2", "0.5"))
    # INDOOR_SETPOINT_C is runtime-tunable via /api/v1/settings (#52) — see @property below.
    INDOOR_COMFORT_BAND_C: float = float(os.getenv("INDOOR_COMFORT_BAND_C", "1.5"))
    # #540 W1 — a room-sensor reading older than this is treated as ABSENT (the
    # comfort guard / LP fall back to the estimator), so a dead sensor can't
    # freeze the LP on a stale indoor temperature.
    INDOOR_SENSOR_STALE_MINUTES: int = int(os.getenv("INDOOR_SENSOR_STALE_MINUTES", "30"))
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

    # ── W3 (#540) — LP indoor-temperature state + comfort optimisation ──────
    # Restores the t_in decision variable + RC dynamics (τ/UA/C from the W2
    # learner or env fallback) so the LP can pre-heat in cheap slots and coast
    # through peaks while holding a comfort floor. OFF by default → the solver
    # is byte-identical to today; flip true only after the regression replay
    # (scripts/check_lp_regression.py) confirms it doesn't worsen cost.
    LP_W3_TIN_ENABLED: bool = os.getenv("LP_W3_TIN_ENABLED", "false").lower() in ("true", "1", "yes")
    # Comfort floor: night value (blankets OK) vs the day setpoint
    # (INDOOR_SETPOINT_C). Night window is local hours [start, end).
    LP_W3_NIGHT_FLOOR_C: float = float(os.getenv("LP_W3_NIGHT_FLOOR_C", "17.5"))
    LP_W3_NIGHT_START_HOUR_LOCAL: int = int(os.getenv("LP_W3_NIGHT_START_HOUR_LOCAL", "22"))
    LP_W3_NIGHT_END_HOUR_LOCAL: int = int(os.getenv("LP_W3_NIGHT_END_HOUR_LOCAL", "7"))
    # Anti-heat-pump-spike: cap the modelled indoor rise per 30-min slot so the
    # LP never plans an unrealistic morning blast (the user's "no cold-morning
    # recovery delta" preference).
    LP_W3_MAX_RECOVERY_C_PER_SLOT: float = float(os.getenv("LP_W3_MAX_RECOVERY_C_PER_SLOT", "0.5"))
    # Comfort-floor slack penalty (p per °C-slot below floor). Deliberately LOW
    # so comfort only nudges the plan — the regression gate defends cost.
    LP_W3_COMFORT_PEN_PENCE_PER_DEGC_SLOT: float = float(
        os.getenv("LP_W3_COMFORT_PEN_PENCE_PER_DEGC_SLOT", "15")
    )

    # Number of recent execution_log rows used to compute the micro-climate offset
    # (mean difference between Daikin outdoor sensor and Open-Meteo forecast).
    DAIKIN_MICRO_CLIMATE_LOOKBACK: int = int(os.getenv("DAIKIN_MICRO_CLIMATE_LOOKBACK", "96"))
    DHW_TANK_UA_W_PER_K: float = float(os.getenv("DHW_TANK_UA_W_PER_K", "2.5"))
    # Daikin Altherma 3 R EDLA11DA3 (11 kW R-32 split, low-temp).
    # Values from manufacturer datasheet at W35 LWT (the operating range
    # the user's weather curve produces — DAIKIN_WEATHER_CURVE_HIGH_LWT_C=22°C
    # at A18°C, DAIKIN_WEATHER_CURVE_LOW_LWT_C=45°C at A-5°C).
    # Source datasheet rows (A-T/W35):
    #   A-7°C → COP 2.55,  A2°C → COP 3.54,  A7°C → COP 4.62,
    #   A12°C → COP 4.98,  A20°C extrapolated → ~5.65
    # Conservative ~5% derate applied to absorb real-world degradation +
    # occasional DHW LWT lift (DHW penalty already covers W55 separately
    # via ``COP_DHW_PENALTY``).
    # Issue #312: prior placeholder curve was ~30% pessimistic at warm
    # temps, biasing the LP toward over-provisioning electrical heating.
    DAIKIN_COP_CURVE_STR: str = os.getenv(
        "DAIKIN_COP_CURVE",
        "-7:2.4,2:3.4,7:4.3,12:4.7,20:5.2",
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

    # Model Predictive Control re-runs the LP intra-day. As of V12 the fixed-
    # hour cron is GONE — the MPC is fully event-driven: tier_boundary (before
    # every tariff transition), octopus_fetch (when new rates land), soc_drift,
    # forecast_revision, dynamic_replan, plan_push (nightly). Manual re-runs
    # via the MCP propose_optimization_plan tool always work.

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
    # On a mid-slot re-upload, bridge the in-progress half-hour slot with the
    # previously-uploaded work mode so a live ForceCharge/ForceDischarge isn't
    # dropped to the firmware's SelfUse default (the plan horizon starts at the
    # NEXT boundary for Daikin quota integrity, but a Fox upload replaces the
    # whole schedule). Fixes force-charge silently stopping mid negative-price
    # slot. Set false to roll back to the bare next-boundary schedule.
    FOX_PRESERVE_INFLIGHT_GROUP: bool = os.getenv("FOX_PRESERVE_INFLIGHT_GROUP", "true").lower() in ("1", "true", "yes")

    # #693 — the in-flight bridge sees through a boot-time safe-defaults wipe
    # (schedule state saved as disabled/empty WITHIN the current slot) to the
    # schedule that was actually in force at the slot's start. When NO
    # authoritative schedule exists at all (fresh install, or scheduler off
    # since before the slot started), a bridge is synthesized from the plan's
    # next-boundary group ONLY when it is ForceCharge/Backup AND the current
    # slot's Agile import price is NEGATIVE — the one regime where blind
    # charging/holding strictly beats the firmware SelfUse default (which
    # discharges to avoid PAID import). At positive prices SelfUse stays the
    # blind default (the LP never priced the current slot; extending a
    # cheap-window ForceCharge into a peak slot could cost ~80p in 29 min).
    # Set false to disable this no-authority fallback only;
    # FOX_PRESERVE_INFLIGHT_GROUP=false disables bridge + fallback together.
    FOX_INFLIGHT_EXTEND_FIRST_GROUP: bool = os.getenv("FOX_INFLIGHT_EXTEND_FIRST_GROUP", "true").lower() in ("1", "true", "yes")

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
    # V12: bumped down from 2 → 1 (i.e. fire on the first heartbeat with
    # drift ≥ threshold). The 2026-04-28 23:00 incident showed a 2-tick
    # window (4 min @ 2-min cadence) is too slow for a heating ramp; the
    # tier-boundary trigger does most of the catching now, this is just the
    # belt-and-braces for ramps that happen MID-window.
    MPC_DRIFT_HYSTERESIS_TICKS: int = int(os.getenv("MPC_DRIFT_HYSTERESIS_TICKS", "1"))
    # Directional gate (2026-07-02 window audit): live SoC running AHEAD of
    # the prediction while the committed plan reaches that level within the
    # look-ahead is early arrival on the planned trajectory (Fox ForceCharge
    # fills faster than the LP taper), not drift — re-solving produced 5-min
    # upload bursts mid-fill. SoC BELOW prediction always fires.
    MPC_DRIFT_AHEAD_SUPPRESS_ENABLED: bool = (
        os.getenv("MPC_DRIFT_AHEAD_SUPPRESS_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    MPC_DRIFT_AHEAD_LOOKAHEAD_HOURS: float = float(
        os.getenv("MPC_DRIFT_AHEAD_LOOKAHEAD_HOURS", "3.0")
    )
    # Staleness bound: max consecutive heartbeats the ahead gate may suppress
    # (24 × 300 s ≈ 2 h) — a plan plateauing at exactly soc − threshold can
    # otherwise hold the suppression indefinitely; past the cap drift fires.
    MPC_DRIFT_AHEAD_MAX_SUPPRESSED_TICKS: int = int(
        os.getenv("MPC_DRIFT_AHEAD_MAX_SUPPRESSED_TICKS", "24")
    )
    # Plan-delta observability: how many hours of overlap between previous and new plan to
    # measure when logging the post-trigger delta. 6 h captures the immediate horizon.
    MPC_PLAN_DELTA_LOOKAHEAD_HOURS: int = int(os.getenv("MPC_PLAN_DELTA_LOOKAHEAD_HOURS", "6"))
    # Forecast revision trigger (Open-Meteo updates ~hourly): how often to re-fetch and
    # compare; how far ahead to compare; what delta is "material enough" to re-plan.
    # MPC_FORECAST_REFRESH_INTERVAL_MINUTES is runtime-tunable (cron_reload=True) — see @property below.
    MPC_FORECAST_DRIFT_LOOKAHEAD_HOURS: int = int(os.getenv("MPC_FORECAST_DRIFT_LOOKAHEAD_HOURS", "6"))
    MPC_FORECAST_DRIFT_SOLAR_KWH_THRESHOLD: float = float(os.getenv("MPC_FORECAST_DRIFT_SOLAR_KWH_THRESHOLD", "2.0"))
    MPC_FORECAST_DRIFT_TEMP_C_THRESHOLD: float = float(os.getenv("MPC_FORECAST_DRIFT_TEMP_C_THRESHOLD", "2.0"))
    # Live deviation triggers (Fox realtime vs LP/forecast expectations).
    # Complement ``forecast_revision`` (forecast-vs-forecast) with
    # actual-vs-expected checks. Default thresholds are intentionally
    # conservative so the trigger only fires on substantial deviations —
    # raise the hysteresis tick count to disable entirely during
    # bring-up while we validate against measured CT-placement
    # assumptions in ``_lp_predicted_load_kw_at``.
    MPC_LIVE_PV_KW_THRESHOLD: float = float(os.getenv("MPC_LIVE_PV_KW_THRESHOLD", "1.5"))
    MPC_LIVE_LOAD_KW_THRESHOLD: float = float(os.getenv("MPC_LIVE_LOAD_KW_THRESHOLD", "1.5"))
    MPC_LIVE_DEVIATION_HYSTERESIS_TICKS: int = int(os.getenv("MPC_LIVE_DEVIATION_HYSTERESIS_TICKS", "3"))

    # ``import_overshoot`` MPC trigger — fires when actual grid import in the
    # last completed half-hour slot exceeds the LP-planned import for the
    # same slot by more than this many kWh. Catches the failure mode where
    # Fox V3 ForceCharge runs hot relative to the LP's tapered schedule
    # (2026-05-08 incident: planned 7.49 kWh / 4 h, actual 10.18 kWh = +36 %).
    # Single-shot — no hysteresis — because by the time we know a slot
    # overshot, it's already over and we want to re-plan the remaining
    # ForceCharge window NOW. Set to 0 to disable.
    MPC_IMPORT_OVERSHOOT_KWH_THRESHOLD: float = float(
        os.getenv("MPC_IMPORT_OVERSHOOT_KWH_THRESHOLD", "0.5")
    )

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
    def DHW_TEMP_PV_ABUNDANCE_TARGET_C(self) -> float:
        return float(self._rt_get("DHW_TEMP_PV_ABUNDANCE_TARGET_C"))

    @DHW_TEMP_PV_ABUNDANCE_TARGET_C.setter
    def DHW_TEMP_PV_ABUNDANCE_TARGET_C(self, value: float) -> None:
        self._rt_set("DHW_TEMP_PV_ABUNDANCE_TARGET_C", float(value))

    # --- Price-aware DHW warmup start hour (#681, 2026-07-10) -----------------
    # Default OFF + shadow-log first (observe the deltas ~1 week before
    # enabling). When enabled, dhw_policy.resolve_warmup_hour_local picks the
    # cheapest warmup start within [WINDOW_START, WINDOW_END) once per plan-date
    # and persists it (runtime_settings ``dhw_warmup_hour_<date>``). Setback
    # stays fixed at DHW_SETBACK_START_HOUR_LOCAL (the restore covenant).
    @property
    def DHW_WARMUP_PRICE_AWARE_ENABLED(self) -> bool:
        v = self._rt_get("DHW_WARMUP_PRICE_AWARE_ENABLED")
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    @DHW_WARMUP_PRICE_AWARE_ENABLED.setter
    def DHW_WARMUP_PRICE_AWARE_ENABLED(self, value: Any) -> None:
        if isinstance(value, bool):
            self._rt_set("DHW_WARMUP_PRICE_AWARE_ENABLED", "true" if value else "false")
        else:
            self._rt_set(
                "DHW_WARMUP_PRICE_AWARE_ENABLED", str(value).strip().lower()
            )

    @property
    def DHW_WARMUP_WINDOW_START_LOCAL(self) -> int:
        return int(self._rt_get("DHW_WARMUP_WINDOW_START_LOCAL"))

    @DHW_WARMUP_WINDOW_START_LOCAL.setter
    def DHW_WARMUP_WINDOW_START_LOCAL(self, value: int) -> None:
        self._rt_set("DHW_WARMUP_WINDOW_START_LOCAL", int(value))

    @property
    def DHW_WARMUP_WINDOW_END_LOCAL(self) -> int:
        return int(self._rt_get("DHW_WARMUP_WINDOW_END_LOCAL"))

    @DHW_WARMUP_WINDOW_END_LOCAL.setter
    def DHW_WARMUP_WINDOW_END_LOCAL(self, value: int) -> None:
        self._rt_set("DHW_WARMUP_WINDOW_END_LOCAL", int(value))

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

    # PR C — ``ENERGY_STRATEGY_MODE`` property removed. Was the
    # 2-value dispatch policy (savings_first / strict_savings). The
    # household never wanted strict_savings (per user 2026-05-22), and
    # the scenario-LP filter is now the sole peak-export gate. Leftover
    # entries in /srv/hem/.env are harmless (Python ignores them).

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
    def LP_GUESTS_BASE_LOAD_SCALE(self) -> float:
        return float(self._rt_get("LP_GUESTS_BASE_LOAD_SCALE"))

    @LP_GUESTS_BASE_LOAD_SCALE.setter
    def LP_GUESTS_BASE_LOAD_SCALE(self, value: float) -> None:
        self._rt_set("LP_GUESTS_BASE_LOAD_SCALE", float(value))

    @property
    def LP_LOAD_SCALE_FACTOR(self) -> float:
        return float(self._rt_get("LP_LOAD_SCALE_FACTOR"))

    @LP_LOAD_SCALE_FACTOR.setter
    def LP_LOAD_SCALE_FACTOR(self, value: float) -> None:
        self._rt_set("LP_LOAD_SCALE_FACTOR", float(value))

    # --- PR B — shower demand model -------------------------------------
    @property
    def DHW_SHOWER_DURATION_MIN(self) -> float:
        return float(self._rt_get("DHW_SHOWER_DURATION_MIN"))

    @DHW_SHOWER_DURATION_MIN.setter
    def DHW_SHOWER_DURATION_MIN(self, value: float) -> None:
        self._rt_set("DHW_SHOWER_DURATION_MIN", float(value))

    @property
    def DHW_SHOWER_FLOW_LPM(self) -> float:
        return float(self._rt_get("DHW_SHOWER_FLOW_LPM"))

    @DHW_SHOWER_FLOW_LPM.setter
    def DHW_SHOWER_FLOW_LPM(self, value: float) -> None:
        self._rt_set("DHW_SHOWER_FLOW_LPM", float(value))

    @property
    def DHW_SHOWER_MIXER_TEMP_C(self) -> float:
        return float(self._rt_get("DHW_SHOWER_MIXER_TEMP_C"))

    @DHW_SHOWER_MIXER_TEMP_C.setter
    def DHW_SHOWER_MIXER_TEMP_C(self, value: float) -> None:
        self._rt_set("DHW_SHOWER_MIXER_TEMP_C", float(value))

    @property
    def DHW_SHOWER_COLD_INLET_TEMP_C(self) -> float:
        return float(self._rt_get("DHW_SHOWER_COLD_INLET_TEMP_C"))

    @DHW_SHOWER_COLD_INLET_TEMP_C.setter
    def DHW_SHOWER_COLD_INLET_TEMP_C(self, value: float) -> None:
        self._rt_set("DHW_SHOWER_COLD_INLET_TEMP_C", float(value))

    @property
    def DHW_SHOWERS_NORMAL_EVENING(self) -> int:
        return int(self._rt_get("DHW_SHOWERS_NORMAL_EVENING"))

    @DHW_SHOWERS_NORMAL_EVENING.setter
    def DHW_SHOWERS_NORMAL_EVENING(self, value: int) -> None:
        self._rt_set("DHW_SHOWERS_NORMAL_EVENING", int(value))

    @property
    def DHW_SHOWERS_NORMAL_MORNING_RESERVE(self) -> int:
        return int(self._rt_get("DHW_SHOWERS_NORMAL_MORNING_RESERVE"))

    @DHW_SHOWERS_NORMAL_MORNING_RESERVE.setter
    def DHW_SHOWERS_NORMAL_MORNING_RESERVE(self, value: int) -> None:
        self._rt_set("DHW_SHOWERS_NORMAL_MORNING_RESERVE", int(value))

    @property
    def DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST(self) -> int:
        return int(self._rt_get("DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST"))

    @DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST.setter
    def DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST(self, value: int) -> None:
        self._rt_set("DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST", int(value))

    @property
    def DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST(self) -> int:
        return int(self._rt_get("DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST"))

    @DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST.setter
    def DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST(self, value: int) -> None:
        self._rt_set("DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST", int(value))

    @property
    def DHW_GUEST_COUNT(self) -> int:
        return int(self._rt_get("DHW_GUEST_COUNT"))

    @DHW_GUEST_COUNT.setter
    def DHW_GUEST_COUNT(self, value: int) -> None:
        self._rt_set("DHW_GUEST_COUNT", int(value))

    @property
    def DHW_SHOWERS_EVENING_CAP(self) -> int:
        return int(self._rt_get("DHW_SHOWERS_EVENING_CAP"))

    @DHW_SHOWERS_EVENING_CAP.setter
    def DHW_SHOWERS_EVENING_CAP(self, value: int) -> None:
        self._rt_set("DHW_SHOWERS_EVENING_CAP", int(value))

    @property
    def DHW_TANK_USABLE_FRACTION(self) -> float:
        return float(self._rt_get("DHW_TANK_USABLE_FRACTION"))

    @DHW_TANK_USABLE_FRACTION.setter
    def DHW_TANK_USABLE_FRACTION(self, value: float) -> None:
        self._rt_set("DHW_TANK_USABLE_FRACTION", float(value))

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

    # --- Positive-price battery hold + solar_charge Fox mode (#679) ---------
    # Runtime-tunable (DB-backed); see SCHEMA in runtime_settings.py for the
    # 2026-07-10 incident + A0 finding that motivated these.
    @property
    def LP_POSITIVE_HOLD_ENABLED(self) -> bool:
        return str(self._rt_get("LP_POSITIVE_HOLD_ENABLED")).strip().lower() in (
            "1", "true", "yes", "on"
        )

    @LP_POSITIVE_HOLD_ENABLED.setter
    def LP_POSITIVE_HOLD_ENABLED(self, value: Any) -> None:
        self._rt_set(
            "LP_POSITIVE_HOLD_ENABLED",
            "true" if (value is True or str(value).strip().lower() in ("1", "true", "yes", "on")) else "false",
        )

    @property
    def LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE(self) -> float:
        return float(self._rt_get("LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE"))

    @LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE.setter
    def LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE(self, value: float) -> None:
        self._rt_set("LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE", float(value))

    @property
    def LP_POSITIVE_HOLD_MAX_GROUPS(self) -> int:
        return int(self._rt_get("LP_POSITIVE_HOLD_MAX_GROUPS"))

    @LP_POSITIVE_HOLD_MAX_GROUPS.setter
    def LP_POSITIVE_HOLD_MAX_GROUPS(self, value: int) -> None:
        self._rt_set("LP_POSITIVE_HOLD_MAX_GROUPS", int(value))

    @property
    def LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT(self) -> float:
        return float(self._rt_get("LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT"))

    @LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT.setter
    def LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT(self, value: float) -> None:
        self._rt_set("LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT", float(value))

    @property
    def LP_SOLAR_CHARGE_FOX_MODE(self) -> str:
        return str(self._rt_get("LP_SOLAR_CHARGE_FOX_MODE")).strip().lower()

    @LP_SOLAR_CHARGE_FOX_MODE.setter
    def LP_SOLAR_CHARGE_FOX_MODE(self, value: str) -> None:
        self._rt_set("LP_SOLAR_CHARGE_FOX_MODE", str(value).strip().lower())

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

    # ── Daikin action_schedule windows ───────────────────────────────────────
    # Width of the "restore" window written by lp_dispatch after each LP-driven
    # window (shutdown / max_heat / pre_heat / normal). The state machine
    # silently marks an action `completed` (no fire) when ``now > end_time`` and
    # status is still pending — so a window narrower than the heartbeat tick is
    # racy. Production heartbeat is 120 s; 5 min gives 2.5× safety margin so
    # restores always fire even when ticks land slightly off-grid. 2026-04-30
    # active-mode rollout hit this with the legacy 1-min window: action 607
    # (off) succeeded, action 606 (restore) was silently skipped, device stayed
    # off until manual rollback to passive.
    LP_RESTORE_WINDOW_MINUTES: int = max(
        2,
        int(os.getenv("LP_RESTORE_WINDOW_MINUTES", "5")),
    )

    # ── API Quota & Cache ────────────────────────────────────────────────────
    # Daikin Onecta: soft daily budget (real limit ≈200; we stop at 180 to preserve 10% headroom)
    DAIKIN_DAILY_BUDGET: int = int(os.getenv("DAIKIN_DAILY_BUDGET", "180"))
    # When DAIKIN_CONTROL_MODE=active, cap daily writes to this value during the soak window
    # (first DAIKIN_ACTIVE_SOAK_DAYS days after the toggle). 0 = no soak cap. Belt-and-braces
    # against an active-mode misbehaviour burning the whole 200/day Onecta limit before we
    # notice. Soak start is recorded in runtime_settings ``daikin_active_mode_started_at``
    # the first time the api_quota module sees DAIKIN_CONTROL_MODE=active.
    DAIKIN_ACTIVE_SOAK_DAILY_BUDGET: int = int(os.getenv("DAIKIN_ACTIVE_SOAK_DAILY_BUDGET", "100"))
    DAIKIN_ACTIVE_SOAK_DAYS: int = int(os.getenv("DAIKIN_ACTIVE_SOAK_DAYS", "3"))
    # PR 5 of plan: write-budget reservation. Each LP plan push, the budget
    # guard subtracts this from quota_remaining("daikin") before deciding how
    # many action_schedule pairs to upsert. Reserves headroom for the
    # heartbeat's safe-default reconciles + telemetry reads (~30 calls/day on
    # current prod). Lower if Daikin call volume drops; raise if heartbeat
    # reconciles are starving for headroom.
    DAIKIN_RESERVE_FOR_HEARTBEAT: int = int(os.getenv("DAIKIN_RESERVE_FOR_HEARTBEAT", "30"))
    # Circuit breaker: pause Daikin writes after N consecutive failures within W minutes,
    # cooldown for C minutes, reset on next successful write. Defends against an Onecta
    # outage burning quota with retries. 0 fails = breaker disabled.
    DAIKIN_CIRCUIT_BREAKER_FAILS: int = int(os.getenv("DAIKIN_CIRCUIT_BREAKER_FAILS", "3"))
    DAIKIN_CIRCUIT_BREAKER_WINDOW_MINUTES: int = int(os.getenv("DAIKIN_CIRCUIT_BREAKER_WINDOW_MINUTES", "15"))
    DAIKIN_CIRCUIT_BREAKER_COOLDOWN_MINUTES: int = int(os.getenv("DAIKIN_CIRCUIT_BREAKER_COOLDOWN_MINUTES", "30"))
    # How long to serve device data from cache without refreshing (1800 s = 30 min)
    DAIKIN_DEVICES_CACHE_TTL_SECONDS: int = int(os.getenv("DAIKIN_DEVICES_CACHE_TTL_SECONDS", "1800"))
    # Hard anti-burst floor between REAL Daikin device reads (any caller). Stops
    # the read-storm sawtooth (#423 follow-up) — a tight loop gets the warm cache
    # instead of hitting the wire. Distinct from the 30-min cache TTL and the
    # force-refresh cooldown; this is the low-level _do_refresh guard.
    DAIKIN_REFRESH_MIN_INTERVAL_SECONDS: int = int(
        os.getenv("DAIKIN_REFRESH_MIN_INTERVAL_SECONDS", "90")
    )
    # Minimum interval between explicit "force refresh" calls (UI refresh button, CLI --force-refresh).
    # 5 min: long enough to stop button-mashing through the ~200/day quota, short
    # enough that an operator who genuinely needs fresh state isn't stuck waiting.
    DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS: int = int(
        os.getenv("DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS", "300")
    )
    # Width of the Octopus pre-slot window that allows automatic device refresh (seconds before HH:30/HH:00)
    DAIKIN_SLOT_TRANSITION_WINDOW_SECONDS: int = int(
        os.getenv("DAIKIN_SLOT_TRANSITION_WINDOW_SECONDS", "300")
    )
    # Local-time calibration windows where Daikin refreshes are allowed if the cache is stale.
    # Format: comma-separated HH:MM-HH:MM ranges in BULLETPROOF_TIMEZONE.
    DAIKIN_CALIBRATION_WINDOWS_LOCAL: str = (
        os.getenv("DAIKIN_CALIBRATION_WINDOWS_LOCAL", "06:00-08:00,14:30-16:30")
    )
    # 2 h-aligned Daikin refresh window. Onecta caches consumption data in
    # 2-hour buckets that rotate at fixed UTC times (00, 02, …, 22). When
    # enabled, the heartbeat fires one refresh in the first few minutes
    # past each even hour UTC, capturing the freshest 2 h bucket as soon
    # as it lands. 12 calls/day (well under the 200/day Daikin budget).
    # See issue #267 (Daikin observation strategy epic).
    DAIKIN_2H_REFRESH_ENABLED: bool = (
        os.getenv("DAIKIN_2H_REFRESH_ENABLED", "true").strip().lower()
        in ("1", "true", "yes")
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
    # Epic 14 (#386) — pre-fire reconcile knobs.
    #
    # When the user has overridden a recent action_schedule row (Onecta app /
    # physical button) and the next LP replan inserts a NEW row covering the
    # overlapping window with identical params, that fresh row would normally
    # fire and undo the user's gesture. Within this window after an override
    # was recorded, the pre-fire reconcile inherits the override onto any
    # would-be reversion row. Aged past the window → normal scheduling resumes.
    USER_OVERRIDE_RESPECT_HOURS: float = float(
        os.getenv("USER_OVERRIDE_RESPECT_HOURS", "4.0")
    )
    # 2026-06-07: extend respect of a manual LWT/tank gesture to the END of the
    # planned window it was made in (the overridden row's end_time), not just
    # the fixed USER_OVERRIDE_RESPECT_HOURS grace. Matters for windows longer
    # than that grace — e.g. a multi-hour negative-price tank boost: nudge the
    # tank by hand and HEM leaves it alone until the boost window closes. The
    # live ``user_gesture_still_in_effect`` check is still the safety gate
    # (revert the manual change → HEM resumes at once), so a long window can
    # never wedge the schedule. Set false to revert to the fixed-hours grace.
    USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END: bool = (
        os.getenv("USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # Feature flag for the pre-fire state-match dedupe. When True (default),
    # the heartbeat compares the live device state against a pending row's
    # params before firing — already-matching rows are marked completed with
    # no API call. Kills the READ_ONLY_CHARACTERISTIC failures and the
    # redundant writes from overlapping replan rows. Flip to "false" for an
    # instant rollback if the comparator misbehaves.
    PREFIRE_STATE_MATCH_ENABLED: bool = (
        os.getenv("PREFIRE_STATE_MATCH_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # Issue #382 — preserve pending restore rows whose start_time is within
    # this many minutes of "now" from the LP-replan clear sweep. The 2026-05-21
    # incident left a tank at 25 °C because a 17:55 MPC re-plan deleted the
    # paired restore (start 18:00) before it could fire — the parent shutdown
    # had failed READ_ONLY at 16:36, so the existing "active parent → preserve
    # its restore" rule didn't apply. Restores within this window have no
    # safe alternative path (the next LP solve may not re-emit one), so we
    # treat them as in-flight regardless of parent state. Set to 0 to disable.
    RESTORE_PRESERVE_LEAD_MINUTES: float = float(
        os.getenv("RESTORE_PRESERVE_LEAD_MINUTES", "10.0")
    )
    # Issue #382 follow-on — heartbeat sanity check. When the live tank is
    # OFF and no current plan slot intends it to be OFF, treat this as state
    # drift: alert and force-restore via apply_comfort_restore (tank_power=True,
    # tank_temp=DHW_TEMP_NORMAL_C). Skipped when the device was overridden by
    # the user within USER_OVERRIDE_RESPECT_HOURS. Set to false to alert only
    # (no auto-recover) or disable entirely.
    TANK_DRIFT_AUTO_RECOVER: bool = (
        os.getenv("TANK_DRIFT_AUTO_RECOVER", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    TANK_DRIFT_CHECK_ENABLED: bool = (
        os.getenv("TANK_DRIFT_CHECK_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # #461 — LWT-offset drift backstop. When HEM owns the offset (pre-heat
    # enabled) and the live offset is non-zero but no active lwt_preheat slot
    # justifies it, reset it to 0 — after the user's grace window
    # (USER_OVERRIDE_RESPECT_HOURS) so a manual override gets respected then
    # cleared. Catches a manual offset that has no paired restore.
    LWT_OFFSET_DRIFT_CHECK_ENABLED: bool = (
        os.getenv("LWT_OFFSET_DRIFT_CHECK_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    LWT_OFFSET_DRIFT_AUTO_RECOVER: bool = (
        os.getenv("LWT_OFFSET_DRIFT_AUTO_RECOVER", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # Fox ESS: soft daily budget (real limit ≈1440; we stop at 1200 for 15% headroom)
    FOX_DAILY_BUDGET: int = int(os.getenv("FOX_DAILY_BUDGET", "1200"))
    # Default realtime data TTL — raised from 30 s to 300 s (5 min) to match heartbeat
    FOX_REALTIME_CACHE_TTL_SECONDS: int = int(os.getenv("FOX_REALTIME_CACHE_TTL_SECONDS", "300"))
    # 2026-06-07: the pv-telemetry background job is the canonical Fox-snapshot
    # refresher; it forces a read older than this so the cockpit serves a fresh
    # snapshot at the telemetry cadence (PV_TELEMETRY_INTERVAL_MINUTES). Keeps the
    # cockpit/API reads cache-only (they never fetch on the request path).
    FOX_SNAPSHOT_REFRESH_MAX_AGE_SECONDS: int = int(
        os.getenv("FOX_SNAPSHOT_REFRESH_MAX_AGE_SECONDS", "60")
    )
    # --- Viewer-aware freshness boost -------------------------------------
    # While someone has the cockpit open (detected via the /cockpit/now poll
    # stream), a 30 s background job refreshes the Fox/Daikin caches faster
    # than their idle baselines (Fox: PV_TELEMETRY_INTERVAL_MINUTES; Daikin:
    # opportunistic only). Both vendors keep a quota reserve so the boost can
    # never starve control writes / LP reads — when quota_remaining drops
    # below the reserve, the boost silently stops and the baselines take over.
    VIEWER_BOOST_ENABLED: bool = (
        os.getenv("VIEWER_BOOST_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # How long after the last /cockpit/now hit a viewer counts as "watching".
    # Must comfortably exceed the SPA's 10 s poll so one missed tick doesn't
    # flap the boost off.
    VIEWER_ACTIVE_WINDOW_SECONDS: int = int(
        os.getenv("VIEWER_ACTIVE_WINDOW_SECONDS", "90")
    )
    # Target Fox realtime staleness while a viewer is watching (idle baseline
    # is the PV telemetry cadence, default 3 min). 0 disables the Fox boost.
    # 30 s is the effective floor — the boost job itself ticks every 30 s, so a
    # smaller target just refreshes on every tick. Quota: ≤120 Fox calls/hour
    # of watching (budget 1200/day, reserve FOX_VIEWER_QUOTA_RESERVE).
    FOX_VIEWER_REFRESH_SECONDS: int = int(
        os.getenv("FOX_VIEWER_REFRESH_SECONDS", "30")
    )
    # Don't boost Fox when fewer than this many calls remain of the daily
    # budget — plan pushes, MPC reads and the telemetry baseline come first.
    FOX_VIEWER_QUOTA_RESERVE: int = int(
        os.getenv("FOX_VIEWER_QUOTA_RESERVE", "300")
    )
    # Target Daikin device-cache staleness while a viewer is watching (idle
    # baseline: refreshed only opportunistically by LP/reconciler reads, so
    # tank/indoor temps could previously sit 30-60+ min stale on the cockpit).
    # 0 disables the Daikin boost. The DAIKIN_REFRESH_MIN_INTERVAL_SECONDS
    # anti-burst floor still applies underneath.
    DAIKIN_VIEWER_REFRESH_SECONDS: int = int(
        os.getenv("DAIKIN_VIEWER_REFRESH_SECONDS", "600")
    )
    # Daikin quota reserve (of DAIKIN_DAILY_BUDGET=180): reconciler writes and
    # LP-init reads must never queue behind cosmetic freshness.
    DAIKIN_VIEWER_QUOTA_RESERVE: int = int(
        os.getenv("DAIKIN_VIEWER_QUOTA_RESERVE", "80")
    )
    # In-process TTL cache for the Open-Meteo forecast fetch (shared by /weather +
    # /pv/today). Open-Meteo is hourly-deterministic, so a short cache turns the
    # 0.5–2 s blocking HTTP call into ~0 ms for the cockpit. 0 disables.
    WEATHER_FORECAST_CACHE_TTL_SECONDS: int = int(
        os.getenv("WEATHER_FORECAST_CACHE_TTL_SECONDS", "900")
    )
    # In-process TTL cache for /energy/period day & week (Fox ESS HTTP). Month
    # already has a 1 h cache; this mirrors it so period-nav clicks are instant.
    ENERGY_PERIOD_CACHE_TTL_SECONDS: int = int(
        os.getenv("ENERGY_PERIOD_CACHE_TTL_SECONDS", "1200")
    )
    # In-process TTL cache for /tariffs/fair-compare — the per-slot replay of
    # the whole period against every tariff card took ~9.6 s uncached in prod
    # (month × 14 tariffs) and the Insights page paid it on every visit.
    # 0 disables (always recompute).
    FAIR_COMPARE_CACHE_TTL_SECONDS: int = int(
        os.getenv("FAIR_COMPARE_CACHE_TTL_SECONDS", "900")
    )
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
    # Per-day-keyed warning ack rows accumulate forever otherwise (issue #200);
    # the keys are useless once the date passes, so 30 d is plenty.
    ACKNOWLEDGED_WARNINGS_RETENTION_DAYS: int = int(
        os.getenv("ACKNOWLEDGED_WARNINGS_RETENTION_DAYS", "30")
    )

    # ── Sensor data lifecycle (#540) — tiered hot/cold, never lose data ──────
    # The room-sensor tables (room_temperature_history, device_reading_log) are
    # append-only and would grow unbounded on the storage-constrained box. We
    # keep a RECENT raw window hot in SQLite (LP + W2 read it), roll it up to a
    # permanent tiny 15-min table for long-term UI, and ARCHIVE the full-res raw
    # to monthly gzip files before pruning — nothing is deleted without a
    # compressed copy first (kept for future ML training).
    INDOOR_SENSOR_RAW_RETENTION_DAYS: int = int(
        os.getenv("INDOOR_SENSOR_RAW_RETENTION_DAYS", "90")
    )
    DEVICE_LOG_RETENTION_DAYS: int = int(os.getenv("DEVICE_LOG_RETENTION_DAYS", "30"))
    DATA_ARCHIVE_ENABLED: bool = os.getenv("DATA_ARCHIVE_ENABLED", "true").lower() in (
        "true", "1", "yes",
    )
    # Empty → derived as <dir of DB_PATH>/archive. Absolute path inside the
    # container (state volume) so archives survive image swaps.
    DATA_ARCHIVE_DIR: str = os.getenv("DATA_ARCHIVE_DIR", "")

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
