"""Config loader — reads from .env or environment variables."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


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

    # Alerts — OpenClaw webhook (leave ALERT_CHANNEL blank for stdout-only)
    ALERT_OPENCLAW_URL: str = os.getenv("ALERT_OPENCLAW_URL", "http://127.0.0.1:18789/api/send")
    ALERT_CHANNEL: str = (os.getenv("ALERT_CHANNEL") or "").strip()

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
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))

    # OpenClaw: when True, POST /api/v1/openclaw/execute returns 403 (recommendation-only; apply via dashboard/CLI)
    OPENCLAW_READ_ONLY: bool = os.getenv("OPENCLAW_READ_ONLY", "true").lower() in ("true", "1", "yes")

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
    # Defaults: Chiswick W4 approximate (London)
    WEATHER_LAT: str = (os.getenv("WEATHER_LAT") or "51.4927").strip()
    WEATHER_LON: str = (os.getenv("WEATHER_LON") or "-0.2628").strip()
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
    DHW_TEMP_CHEAP_C: float = float(os.getenv("DHW_TEMP_CHEAP_C", "60"))
    DHW_TEMP_NORMAL_C: float = float(os.getenv("DHW_TEMP_NORMAL_C", "50"))
    DHW_LEGIONELLA_TEMP_C: float = float(os.getenv("DHW_LEGIONELLA_TEMP_C", "60"))
    DHW_LEGIONELLA_DAY: int = int(os.getenv("DHW_LEGIONELLA_DAY", "6"))  # Sunday
    DHW_LEGIONELLA_HOUR_START: int = int(os.getenv("DHW_LEGIONELLA_HOUR_START", "11"))
    DHW_LEGIONELLA_HOUR_END: int = int(os.getenv("DHW_LEGIONELLA_HOUR_END", "13"))

    BATTERY_CAPACITY_KWH: float = float(os.getenv("BATTERY_CAPACITY_KWH", "10"))
    FOX_FORCE_CHARGE_MAX_PWR: int = int(os.getenv("FOX_FORCE_CHARGE_MAX_PWR", "6000"))
    FOX_FORCE_CHARGE_NORMAL_PWR: int = int(os.getenv("FOX_FORCE_CHARGE_NORMAL_PWR", "3000"))

    LWT_OFFSET_MAX: float = float(os.getenv("LWT_OFFSET_MAX", "10"))
    LWT_OFFSET_MIN: float = float(os.getenv("LWT_OFFSET_MIN", "-5"))
    LWT_OFFSET_PREHEAT_BOOST: float = float(
        os.getenv("LWT_OFFSET_PREHEAT_BOOST", os.getenv("SCHEDULER_PREHEAT_LWT_BOOST", "5"))
    )

    PV_CAPACITY_KWP: float = float(os.getenv("PV_CAPACITY_KWP", "4.5"))
    PV_SYSTEM_EFFICIENCY: float = float(os.getenv("PV_SYSTEM_EFFICIENCY", "0.85"))

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

    # V7 Optimization engine (Agile watchdog + 48-block solver + dispatch hints)
    OPTIMIZATION_ENGINE_ENABLED: bool = os.getenv(
        "OPTIMIZATION_ENGINE_ENABLED", "false"
    ).lower() in ("true", "1", "yes")
    OPTIMIZATION_PRESET: str = (os.getenv("OPTIMIZATION_PRESET") or "normal").strip().lower()
    # savings_first (default): import/savings focus; peak grid export (force discharge) only when
    # OPTIMIZATION_PRESET is travel/away AND cached SoC >= EXPORT_DISCHARGE_MIN_SOC_PERCENT.
    # strict_savings — never schedule peak export discharge (max self-use).
    ENERGY_STRATEGY_MODE: str = (os.getenv("ENERGY_STRATEGY_MODE") or "savings_first").strip().lower()
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
    OPTIMIZATION_LWT_OFFSET_MAX: float = float(os.getenv("OPTIMIZATION_LWT_OFFSET_MAX", "10"))
    OPTIMIZATION_DISABLE_WEATHER_REGULATION: bool = os.getenv(
        "OPTIMIZATION_DISABLE_WEATHER_REGULATION", "false"
    ).lower() in ("true", "1", "yes")

    # Operation mode: simulation (default) or operational.
    # In simulation, the engine computes and logs what it would do but never writes to hardware.
    # Switch to operational only after reviewing simulation output and explicitly activating.
    OPERATION_MODE: str = (os.getenv("OPERATION_MODE") or "simulation").strip().lower()

    # User's target average cost (p/kWh). The solver ranks 48 slots and exploits cheap windows
    # aggressively enough to bring the weighted average below this target.
    TARGET_PRICE_PENCE: float = float(os.getenv("TARGET_PRICE_PENCE", "0"))

    # Directory for config snapshots (JSON). Snapshots are saved before any mode transition.
    CONFIG_SNAPSHOT_DIR: str = (os.getenv("CONFIG_SNAPSHOT_DIR") or "data/config_snapshots").strip()

    # Plan consent expiry in seconds (default 60 min). Proposed plans expire if not approved.
    PLAN_CONSENT_EXPIRY_SECONDS: int = int(os.getenv("PLAN_CONSENT_EXPIRY_SECONDS", "3600"))

    # When True, every freshly proposed plan is immediately auto-approved.
    # Only meaningful when OPERATION_MODE=operational; in simulation mode the effect is
    # the same but harmless. Disable this to return to explicit per-plan consent.
    PLAN_AUTO_APPROVE: bool = os.getenv("PLAN_AUTO_APPROVE", "false").lower() in ("true", "1", "yes")

    def foxess_client_kwargs(self) -> dict:
        """Return the right kwargs for FoxESSClient based on what's configured."""
        if not self.FOXESS_DEVICE_SN:
            raise ValueError("FOXESS_DEVICE_SN is required. Find it in foxesscloud.com → Devices.")
        kwargs = {"device_sn": self.FOXESS_DEVICE_SN}
        sched_sn = self.FOXESS_SCHEDULER_SN or self.DATALOGGER_SERIAL_NUMBER
        if sched_sn:
            kwargs["scheduler_sn"] = sched_sn
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
