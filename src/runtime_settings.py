"""Runtime-tunable settings layer (#52).

Callers read settings via ``get_setting(key)`` which returns the coerced value
(float/int/str/list) from a 30-sec TTL + version-counter cache. Cache misses
hit SQLite (``runtime_settings`` table); absent rows fall back to the
env-derived default declared in :data:`SCHEMA`. Writes happen only via
``set_setting`` — validation, persistence, and cache invalidation are atomic.

Design choices:
  * **Schema-driven**: every tunable has an entry in :data:`SCHEMA` — no silent
    extension. A PUT for an unknown key returns 400.
  * **Env defaults via lambda**: the ``env_default`` callable is re-evaluated
    only on first read (or after a ``delete_setting``), so env changes after
    process start do **not** retroactively shift the default. Matches the
    "zero-risk rollback" behavior in the issue — delete the row, env reasserts.
  * **TTL + version**: single-process hot paths short-circuit on version match
    (O(1) dict lookup). The 30-sec TTL is a belt-and-braces floor so
    out-of-band writes (a human typing ``UPDATE runtime_settings ...``) get
    picked up without a restart.
  * **Cron hot-reload side effect**: settings whose change requires
    APScheduler job re-registration are tagged ``cron_reload=True``; the PUT
    handler calls ``scheduler.runner.reregister_cron_jobs(reason)`` after
    persistence. All other keys take effect on the next cache-miss read.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import db

logger = logging.getLogger(__name__)


class SettingValidationError(ValueError):
    """Raised by :func:`set_setting` when the new value fails schema validation."""


# Per-key legacy-value translators. Applied during ``get_setting`` after the
# raw read but before caching, so a stored value like
# ``OPTIMIZATION_PRESET="travel"`` (from before PR A) transparently reads as
# ``"vacation"`` for every caller. The translator is allowed to violate the
# spec's ``enum``: it runs after ``_coerce`` and before the caller sees the
# value, while ``set_setting``'s validator only sees fresh writes.
_LEGACY_VALUE_TRANSLATORS: dict[str, dict[str, str]] = {
    "OPTIMIZATION_PRESET": {"travel": "vacation", "away": "vacation"},
}
_LEGACY_TRANSLATION_LOGGED: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type_name: str  # "float" | "int" | "str" | "list[int]"
    env_default: Callable[[], Any]
    min_value: float | None = None
    max_value: float | None = None
    enum: tuple[str, ...] | None = None
    cron_reload: bool = False
    description: str = ""


def _float_env(name: str, default: str) -> Callable[[], float]:
    return lambda: float(os.getenv(name, default))


def _int_env(name: str, default: str) -> Callable[[], int]:
    return lambda: int(os.getenv(name, default))


def _str_env(name: str, default: str) -> Callable[[], str]:
    return lambda: (os.getenv(name) or default).strip().lower()


def _lp_soc_final_kwh_default() -> float:
    """LP terminal SoC floor: explicit env override wins; otherwise 25 % of BATTERY_CAPACITY_KWH."""
    env = os.getenv("LP_SOC_FINAL_KWH")
    if env is not None and env.strip() != "":
        return float(env)
    cap = float(os.getenv("BATTERY_CAPACITY_KWH", "10"))
    return round(cap * 0.25, 2)


SCHEMA: dict[str, SettingSpec] = {
    # DHW comfort knobs — user-tunable per season / presence.
    "DHW_TEMP_COMFORT_C": SettingSpec(
        key="DHW_TEMP_COMFORT_C",
        type_name="float",
        env_default=_float_env("DHW_TEMP_COMFORT_C", "48"),
        min_value=40.0,
        max_value=65.0,
        description="Tank target when negative-price plunge fills headroom (°C).",
    ),
    "DHW_TEMP_NORMAL_C": SettingSpec(
        key="DHW_TEMP_NORMAL_C",
        type_name="float",
        env_default=_float_env("DHW_TEMP_NORMAL_C", "45"),
        min_value=40.0,
        max_value=65.0,
        description=(
            "Restore / safe-default tank target (°C). PR G (2026-05-22): "
            "lowered default 50 → 45 to match the user's empirical reality "
            "— 45 °C delivers 4 daily showers comfortably; even 43 works "
            "in practice. Matches the value already documented in CLAUDE.md "
            "and used in /srv/hem/.env on prod."
        ),
    ),
    "INDOOR_SETPOINT_C": SettingSpec(
        key="INDOOR_SETPOINT_C",
        type_name="float",
        env_default=_float_env("INDOOR_SETPOINT_C", "21"),
        min_value=16.0,
        max_value=26.0,
        description="Indoor comfort setpoint (°C).",
    ),
    # Strategy switches.
    "OPTIMIZATION_PRESET": SettingSpec(
        key="OPTIMIZATION_PRESET",
        type_name="str",
        env_default=_str_env("OPTIMIZATION_PRESET", "normal"),
        enum=("normal", "guests", "vacation"),
        description=(
            "Household mode — drives DHW demand model, battery dispatch, and "
            "peak-export aggressiveness. normal = family at home; guests = "
            "extra showers (DHW_GUEST_COUNT); vacation = DHW off + max "
            "arbitrage, PV-only charging. Legacy values 'travel' and 'away' "
            "are silently translated to 'vacation' on read."
        ),
    ),
    # --- PR B — explicit shower-demand model -------------------------------
    # Formalises DHW demand as count × duration × flow × mixer-temp instead of
    # the legacy DHW_DAILY_SHOWER_LITRES aggregate. Each setting is runtime-
    # tunable via /api/v1/settings or MCP set_setting; defaults match the
    # household spec captured 2026-05-22 (4 evening showers, 5 min, ~9 L/min
    # UK low-flow head, mixer-out 38 °C).
    "DHW_SHOWER_DURATION_MIN": SettingSpec(
        key="DHW_SHOWER_DURATION_MIN",
        type_name="float",
        env_default=_float_env("DHW_SHOWER_DURATION_MIN", "5.0"),
        min_value=1.0,
        max_value=30.0,
        description="Average shower duration (minutes per shower).",
    ),
    "DHW_SHOWER_FLOW_LPM": SettingSpec(
        key="DHW_SHOWER_FLOW_LPM",
        type_name="float",
        env_default=_float_env("DHW_SHOWER_FLOW_LPM", "7.0"),
        min_value=4.0,
        max_value=20.0,
        description=(
            "Mixer-out flow rate (litres per minute). PR G (2026-05-22): "
            "lowered default 9 → 7 to match the user's actual UK low-flow "
            "shower head. Reverse-engineered from empirical: with 7 L/min "
            "and DHW_TANK_USABLE_FRACTION=0.85, the model's "
            "required_tank_temp matches observed tank deliveries (40 °C "
            "for 4 daily showers; 46 °C for 6 guest showers)."
        ),
    ),
    "DHW_SHOWER_MIXER_TEMP_C": SettingSpec(
        key="DHW_SHOWER_MIXER_TEMP_C",
        type_name="float",
        env_default=_float_env("DHW_SHOWER_MIXER_TEMP_C", "38.0"),
        min_value=30.0,
        max_value=45.0,
        description="Target mixer-out temperature (°C); typical comfortable shower.",
    ),
    "DHW_SHOWER_COLD_INLET_TEMP_C": SettingSpec(
        key="DHW_SHOWER_COLD_INLET_TEMP_C",
        type_name="float",
        env_default=_float_env("DHW_SHOWER_COLD_INLET_TEMP_C",
                                _str_env("DHW_COLD_INLET_TEMP_C", "10.0")()),
        min_value=4.0,
        max_value=20.0,
        description=(
            "Mains cold-water inlet temperature (°C). Drives mixer math. "
            "Default lifts the legacy DHW_COLD_INLET_TEMP_C env if set."
        ),
    ),
    "DHW_SHOWERS_NORMAL_EVENING": SettingSpec(
        key="DHW_SHOWERS_NORMAL_EVENING",
        type_name="int",
        env_default=_int_env("DHW_SHOWERS_NORMAL_EVENING", "4"),
        min_value=0,
        max_value=10,
        description="Evening showers planned in normal mode (typical family count).",
    ),
    "DHW_SHOWERS_NORMAL_MORNING_RESERVE": SettingSpec(
        key="DHW_SHOWERS_NORMAL_MORNING_RESERVE",
        type_name="int",
        env_default=_int_env("DHW_SHOWERS_NORMAL_MORNING_RESERVE", "1"),
        min_value=0,
        max_value=5,
        description=(
            "Morning reserve in normal mode: tank must be warm enough for N "
            "showers but not necessarily consumed. Models as a soft floor "
            "at the configured morning hour, NOT as drawn litres."
        ),
    ),
    "DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST": SettingSpec(
        key="DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST",
        type_name="int",
        env_default=_int_env("DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST", "1"),
        min_value=0,
        max_value=3,
        description="Extra evening showers per visitor when mode=guests.",
    ),
    "DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST": SettingSpec(
        key="DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST",
        type_name="int",
        env_default=_int_env("DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST", "1"),
        min_value=0,
        max_value=3,
        description="Extra morning showers per visitor when mode=guests.",
    ),
    "DHW_GUEST_COUNT": SettingSpec(
        key="DHW_GUEST_COUNT",
        type_name="int",
        env_default=_int_env("DHW_GUEST_COUNT", "2"),
        min_value=0,
        max_value=8,
        description=(
            "Number of visitors when mode=guests. Multiplies the per-guest "
            "extras. Default 2 matches the assumption when the operator "
            "switches to guests without specifying a count."
        ),
    ),
    "DHW_SHOWERS_EVENING_CAP": SettingSpec(
        key="DHW_SHOWERS_EVENING_CAP",
        type_name="int",
        env_default=_int_env("DHW_SHOWERS_EVENING_CAP", "6"),
        min_value=1,
        max_value=12,
        description=(
            "Maximum showers the LP plans into a single evening shift "
            "(PR G — user empirical: 6 is the practical limit per evening; "
            "more spills to next-day morning). When "
            "``base + guests × extras`` exceeds this cap, the surplus "
            "rolls over into the morning count, and the morning required "
            "tank temp is itself capped at ``DHW_TEMP_NORMAL_C`` so the "
            "LP doesn't over-heat overnight just to satisfy spill-over "
            "demand."
        ),
    ),
    # --- Price-aware DHW warmup start hour (#681, 2026-07-10) ----------------
    # Default OFF + shadow-log first: dhw_policy shadow-logs the price-aware
    # pick vs the static 13:00 warmup for ~1 week so the deltas can be observed
    # before flipping this on. When true, the warmup START hour is chosen once
    # per plan-date within [START, END) to minimise the mean Agile import price
    # of its two 30-min transition slots, then persisted verbatim
    # (runtime_settings ``dhw_warmup_hour_<date>``) so re-plans stay coherent
    # with the K2 pin + the restore covenant. Setback stays fixed at 22:00.
    "DHW_WARMUP_PRICE_AWARE_ENABLED": SettingSpec(
        key="DHW_WARMUP_PRICE_AWARE_ENABLED",
        type_name="str",  # "true" / "false" — mirrors REQUIRE_SIMULATION_ID
        env_default=_str_env("DHW_WARMUP_PRICE_AWARE_ENABLED", "false"),
        enum=("true", "false"),
        description=(
            "Price-aware DHW warmup start hour (#681). false (default) = fixed "
            "DHW_WARMUP_START_HOUR_LOCAL (13:00), byte-identical to legacy but "
            "with a shadow-log of the would-pick delta. true = pick the cheapest "
            "warmup start within [DHW_WARMUP_WINDOW_START_LOCAL, "
            "DHW_WARMUP_WINDOW_END_LOCAL) once per plan-date and persist it. "
            "Setback stays fixed at DHW_SETBACK_START_HOUR_LOCAL."
        ),
    ),
    "DHW_WARMUP_WINDOW_START_LOCAL": SettingSpec(
        key="DHW_WARMUP_WINDOW_START_LOCAL",
        type_name="int",
        env_default=_int_env("DHW_WARMUP_WINDOW_START_LOCAL", "11"),
        min_value=0,
        max_value=23,
        description=(
            "Earliest LOCAL hour the price-aware warmup may start (inclusive). "
            "Only used when DHW_WARMUP_PRICE_AWARE_ENABLED=true. Bounded so the "
            "tank is still warm before evening showers."
        ),
    ),
    "DHW_WARMUP_WINDOW_END_LOCAL": SettingSpec(
        key="DHW_WARMUP_WINDOW_END_LOCAL",
        type_name="int",
        env_default=_int_env("DHW_WARMUP_WINDOW_END_LOCAL", "16"),
        min_value=1,
        max_value=24,
        description=(
            "Exclusive upper bound (LOCAL hour) for the price-aware warmup "
            "start window. Candidates are [START, END). Only used when "
            "DHW_WARMUP_PRICE_AWARE_ENABLED=true; must be > START."
        ),
    ),
    "DHW_TANK_USABLE_FRACTION": SettingSpec(
        key="DHW_TANK_USABLE_FRACTION",
        type_name="float",
        env_default=_float_env("DHW_TANK_USABLE_FRACTION", "0.85"),
        min_value=0.4,
        max_value=1.0,
        description=(
            "Stratification fraction: portion of the nominal tank volume "
            "that delivers hot water at the storage temperature before the "
            "remaining cold inlet dilutes the draw. PR G (2026-05-22): "
            "bumped 0.7 → 0.85 based on this household's empirical "
            "observation that 45 °C tank delivers 4 family showers and "
            "48 °C delivers 6 guest showers. Daikin Altherma HPSU tanks "
            "with their immersed coil show better stratification than the "
            "conservative 0.7 default. Lower this if a future tank with "
            "weaker stratification is installed."
        ),
    ),
    "DHW_MORNING_RESERVE_HOUR_LOCAL": SettingSpec(
        key="DHW_MORNING_RESERVE_HOUR_LOCAL",
        type_name="int",
        env_default=_int_env("DHW_MORNING_RESERVE_HOUR_LOCAL", "7"),
        min_value=4,
        max_value=12,
        description=(
            "Local hour for the morning-reserve soft floor (slot whose start "
            "matches this hour). Defaults to 07:00 — the first plausible "
            "shower hour the morning after."
        ),
    ),
    # ENERGY_STRATEGY_MODE removed in PR C (mode-collapse stack 3/3).
    # Was a 2-value dispatch policy (savings_first / strict_savings) that
    # gated the drop-peak-export branch. Replaced by household-mode-derived
    # behaviour: vacation = max arbitrage; normal/guests use the scenario-LP
    # filter. Anything stored under this key reads via the legacy alias path
    # below as a no-op deprecation log; remove from /srv/hem/.env opportunistically.
    "DAIKIN_CONTROL_MODE": SettingSpec(
        key="DAIKIN_CONTROL_MODE",
        type_name="str",
        env_default=_str_env("DAIKIN_CONTROL_MODE", "passive"),
        enum=("passive", "active"),
        description=(
            "passive = service never writes to Daikin (firmware autonomous; "
            "treated as fixed thermal load by LP). active = legacy v9 control."
        ),
    ),
    "REQUIRE_SIMULATION_ID": SettingSpec(
        key="REQUIRE_SIMULATION_ID",
        type_name="str",  # "true" / "false" — kept as str so PUT payloads stay simple
        env_default=_str_env("REQUIRE_SIMULATION_ID", "false"),
        enum=("true", "false"),
        description=(
            "v10.1 cockpit: when 'true', every state-changing API route requires a "
            "valid X-Simulation-Id header from a paired /simulate call. Default 'false' "
            "so legacy dashboard + scripts keep working until the new cockpit ships."
        ),
    ),
    # Schedule cadence — require cron hot-reload when changed.
    "LP_PLAN_PUSH_HOUR": SettingSpec(
        key="LP_PLAN_PUSH_HOUR",
        type_name="int",
        env_default=_int_env("LP_PLAN_PUSH_HOUR", "0"),
        min_value=0,
        max_value=23,
        cron_reload=True,
        description="UTC hour for the nightly plan-push cron.",
    ),
    "LP_PLAN_PUSH_MINUTE": SettingSpec(
        key="LP_PLAN_PUSH_MINUTE",
        type_name="int",
        env_default=_int_env("LP_PLAN_PUSH_MINUTE", "5"),
        min_value=0,
        max_value=59,
        cron_reload=True,
        description="UTC minute for the nightly plan-push cron.",
    ),
    "MPC_FORECAST_REFRESH_INTERVAL_MINUTES": SettingSpec(
        key="MPC_FORECAST_REFRESH_INTERVAL_MINUTES",
        type_name="int",
        # Default 30 min — aligned with Quartz's ``blend`` model refresh
        # cadence (the underlying ``pvnet_v2`` nowcast updates every
        # ~30 min). Open-Meteo's hourly model rolls every 60 min so OM-only
        # deployments could still set 60 in ``.env`` without losing signal,
        # but 30 is cheap (24 free OM calls/day vs 12) and catches mid-hour
        # nowcast adjustments when Quartz is active.
        env_default=_int_env("MPC_FORECAST_REFRESH_INTERVAL_MINUTES", "30"),
        min_value=10,
        max_value=720,
        cron_reload=True,
        description=(
            "Interval (minutes) for the forecast refresh + revision-trigger detector. "
            "Each tick re-fetches the active forecast source (Open-Meteo or Quartz per "
            "FORECAST_SOURCE) and fires an MPC re-plan if the next 6 h of solar/temp "
            "diverged materially from the previous fetch. Lower = quicker reaction to "
            "intra-hour nowcast updates (especially relevant for Quartz which refreshes "
            "every ~30 min); higher = less network traffic but missed nowcast cycles."
        ),
    ),
    "PV_TELEMETRY_INTERVAL_MINUTES": SettingSpec(
        key="PV_TELEMETRY_INTERVAL_MINUTES",
        type_name="int",
        # 5 min default — gives 6 samples per LP slot (30 min) so the
        # half-hour aggregations used by ``forecast_skill_log`` rebuild,
        # the today-aware adjuster, and the bias diagnostics are based on
        # a proper slot mean rather than a single point. Zero Fox quota
        # cost since this reads the heartbeat-cached realtime, not the
        # paid /raw API.
        env_default=_int_env("PV_TELEMETRY_INTERVAL_MINUTES", "5"),
        min_value=5,
        max_value=120,
        cron_reload=True,
        description=(
            "Interval (minutes) for the PV-realtime telemetry job. Each tick reads the "
            "Fox cached realtime (SoC%, solar/load/grid/battery kW) and persists in "
            "pv_realtime_history for offline calibration analysis. Zero Fox quota cost "
            "(reads heartbeat-cached values). 5 min = 6 samples/slot for proper "
            "slot-mean aggregations."
        ),
    ),
    "PV_CALIBRATION_WINDOW_DAYS": SettingSpec(
        key="PV_CALIBRATION_WINDOW_DAYS",
        type_name="int",
        # Default 14d (was 30d). S10.5 (#172) A/B test against today's data showed
        # 30d severely lags the spring→summer transition: afternoon factors at
        # BST 14-17 were under-calibrated by 17-49% vs 14d. Median + Open-Meteo
        # Archive method (production code) handles outliers fine; the smaller
        # window's faster seasonal response wins.
        env_default=_int_env("PV_CALIBRATION_WINDOW_DAYS", "14"),
        min_value=7,
        max_value=365,
        cron_reload=False,
        description=(
            "Rolling window (days) used by compute_pv_calibration_factor to learn the "
            "Fox-actual / Open-Meteo-modelled PV ratio. Shorter = faster response to "
            "seasonality (spring → summer transitions); longer = more stable but slow "
            "to react. Default 30 d after analysis showed the legacy 250 d masked a "
            "0.83 vs 0.67 (recent) overestimate bias."
        ),
    ),
    # Site location — installation-specific, not a credential. Drives
    # Open-Meteo forecast fetches (LP weather inputs) and degree-day analytics.
    # Defaults: Chiswick W4 (London) — same as the legacy env defaults so the
    # post-cutover env-empty case is unchanged.
    "WEATHER_LAT": SettingSpec(
        key="WEATHER_LAT",
        type_name="str",
        env_default=lambda: (os.getenv("WEATHER_LAT") or "51.4927").strip(),
        description="Latitude (decimal degrees). Drives Open-Meteo forecast for LP weather inputs.",
    ),
    "WEATHER_LON": SettingSpec(
        key="WEATHER_LON",
        type_name="str",
        env_default=lambda: (os.getenv("WEATHER_LON") or "-0.2628").strip(),
        description="Longitude (decimal degrees). Drives Open-Meteo forecast for LP weather inputs.",
    ),
    # Terminal SoC floor — anti-myopia. Each LP run must end its 24h horizon with
    # SoC ≥ this value (kWh). Without it, individual runs may plan to drain the
    # battery near the boundary before the next MPC corrects. Default = 25 % of
    # BATTERY_CAPACITY_KWH so it scales with the installed battery.
    # Terminal SoC valuation — addresses the "drain to floor + refill from grid"
    # myopia by making each kWh above the floor worth N pence in the objective.
    # Default 5 p/kWh: nudges away from MARGINAL arbitrage (where the
    # export-vs-overnight-import spread is small) while still allowing STRONG
    # arbitrage to cycle the battery. 0 = legacy hard-floor-only behaviour.
    "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH": SettingSpec(
        key="LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH",
        type_name="float",
        env_default=_float_env("LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", "5.0"),
        min_value=0.0,
        max_value=30.0,
        description=(
            "Soft-cost per kWh of terminal SoC above LP_SOC_FINAL_KWH (pence). "
            "Each kWh kept in the battery at horizon end is worth this much in "
            "the LP objective — represents the avoided import cost of NOT having "
            "to refill from grid in the next horizon. 0 disables (legacy)."
        ),
    ),
    "LP_SOC_FINAL_KWH": SettingSpec(
        key="LP_SOC_FINAL_KWH",
        type_name="float",
        env_default=_lp_soc_final_kwh_default,
        min_value=0.0,
        max_value=50.0,  # generous upper bound (any battery 200 kWh stays safe)
        description=(
            "Hard SoC floor (kWh) the LP must hit at the end of its 24h horizon. "
            "Anti-myopia constraint that prevents end-of-window battery drains. "
            "0 = disabled (legacy soft-cost-only behaviour). Default = 25 % of "
            "BATTERY_CAPACITY_KWH."
        ),
    ),
    "LP_LOAD_SCALE_FACTOR": SettingSpec(
        key="LP_LOAD_SCALE_FACTOR",
        type_name="float",
        env_default=_float_env("LP_LOAD_SCALE_FACTOR", "1.0"),
        min_value=0.5,
        max_value=2.0,
        description=(
            "Operator multiplier on the residual house-load forecast the LP "
            "plans against. 1.0 = as-measured (default, no-op). Nudge down "
            "(e.g. 0.7 when away) or up (e.g. 1.3 for guests) to shift the "
            "plan's demand assumption without re-learning the profile. Applies "
            "to the residual profile only — explicit appliance loads are "
            "unaffected."
        ),
    ),
    "LP_GUESTS_BASE_LOAD_SCALE": SettingSpec(
        key="LP_GUESTS_BASE_LOAD_SCALE",
        type_name="float",
        env_default=_float_env("LP_GUESTS_BASE_LOAD_SCALE", "1.3"),
        min_value=1.0,
        max_value=1.6,
        description=(
            "Multiplier applied to the residual house-load forecast ONLY when "
            "mode=guests (on top of LP_LOAD_SCALE_FACTOR). Visitors raise base "
            "load (cooking, lights, devices), not just DHW showers, so the "
            "battery must be provisioned for it. 1.0 = guests changes DHW only "
            "(legacy). Default 1.3 mirrors the documented 'guests -> 1.3' intent. "
            "Auto-reverts to no-op when mode returns to normal."
        ),
    ),
    # --- Positive-price battery hold + solar_charge Fox mode (#679) ---------
    # Incident (prod 2026-07-10 UTC): PV under-delivered; battery hit the 10%
    # floor at 08:01, then at 14:01-14:32 discharged ~1 kWh into a midday load
    # spike at 18.1p while the 33-39p evening peak was minutes away — because
    # those slots dispatched as SelfUse(minSoc=100) and the H1 IGNORES a
    # per-group minSoc floor as a discharge freeze. A0 finding (40,369 prod
    # samples): SelfUse(min=100) discharged below floor in 40.6% of samples;
    # Backup 0.0% in 441 samples. So the ONLY reliable hold primitive is
    # Backup — not an elevated SelfUse floor. These knobs make the dispatcher
    # honour the LP's "hold the battery for the peak" decision at positive
    # prices (A1) and move solar_charge off the broken SelfUse(100,100) (A2).
    # Instant rollback: PUT /api/v1/settings (no restart).
    "LP_POSITIVE_HOLD_ENABLED": SettingSpec(
        key="LP_POSITIVE_HOLD_ENABLED",
        type_name="str",  # "true" / "false" — kept as str so PUT payloads stay simple
        env_default=_str_env("LP_POSITIVE_HOLD_ENABLED", "true"),
        enum=("true", "false"),
        description=(
            "A1 (#679): when 'true', a positive-price slot where the LP holds "
            "the battery (dis=0, chg=0, imp>0) ahead of a forecast peak maps to "
            "pinned Backup (the proven 0%-discharge hold) instead of SelfUse "
            "(whose floor the H1 ignores — the 2026-07-10 incident). 'false' = "
            "byte-identical to legacy (soc_floor_pct never set)."
        ),
    ),
    "LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE": SettingSpec(
        key="LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE",
        type_name="float",
        env_default=_float_env("LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE", "5.0"),
        min_value=0.0,
        max_value=50.0,
        description=(
            "A1 (#679): minimum price uplift (pence) between a hold slot and the "
            "highest later slot in the horizon before the hold is worth "
            "protecting. Below this the battery may as well cover the current "
            "load. Default 5.0p."
        ),
    ),
    "LP_POSITIVE_HOLD_MAX_GROUPS": SettingSpec(
        key="LP_POSITIVE_HOLD_MAX_GROUPS",
        type_name="int",
        env_default=_int_env("LP_POSITIVE_HOLD_MAX_GROUPS", "2"),
        min_value=0,
        max_value=6,
        description=(
            "A1 (#679): keep at most this many contiguous hold RUNS (each merges "
            "to one Fox V3 group), ranked by protected value. Protects the "
            "8-group scheduler cap. Default 2; 0 disables A1 holds."
        ),
    ),
    "LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT": SettingSpec(
        key="LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT",
        type_name="float",
        env_default=_float_env("LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT", "2.0"),
        min_value=0.0,
        max_value=50.0,
        description=(
            "A1 (#679): a hold is only labelled when the planned end-of-slot "
            "SoC exceeds MIN_SOC_RESERVE_PERCENT + this margin (%). Near the "
            "reserve there is nothing to protect (Backup would just track the "
            "floor). Default 2.0%."
        ),
    ),
    # A2 (#679) — final owner decision 2026-07-11, CORRECTED after adversarial
    # verification against our OWN 35-day truth table (docs/FOXESS/
    # WORK_MODES_AND_SOC.md). The earlier "backup_fill is safe" reasoning was
    # WRONG. Established facts on our H1 firmware 1.51:
    #   * Backup is a STRICT no-discharge hold (Fox fixed discharge-in-Backup in
    #     master V1.39; we run 1.51) — that part holds, and it is why A1 pre-peak
    #     holds use Backup(reserve, reserve).
    #   * BUT Backup grid-import is driven by **maxSoc** (the ceiling), NOT
    #     minSoc. Truth-table row `Backup(minSoc=10, maxSoc unset/high)` shows
    #     ~1.2 kW grid top-up even with SoC ABOVE the minSoc floor. The "won't
    #     import from grid" behaviour is a v1.55 fix; we are on 1.51. So
    #     `backup_fill = Backup(minSoc=reserve, maxSoc=LP_target)` with
    #     target > current SoC would GRID-IMPORT at ~18p and curtail PV on sunny
    #     solar_charge slots — the exact footgun.
    # DEFAULT is therefore 'selfuse': plain SelfUse(reserve) lets PV fill and
    # NEVER auto-imports (respects "charging = the LP's decision"); the rare
    # discharge leak is accepted (empty-at-peak ~1/30 days, handled at the LP
    # level). 'backup_fill' is retained as a NON-default, FIRMWARE-GATED option
    # (safe only on fw >= 1.55). A structural guard (_guard_nonneg_backup_maxsoc)
    # additionally clamps any Backup maxSoc > live SoC at a positive price, so
    # the footgun cannot be armed by a mode mistake.
    "LP_SOLAR_CHARGE_FOX_MODE": SettingSpec(
        key="LP_SOLAR_CHARGE_FOX_MODE",
        type_name="str",
        env_default=_str_env("LP_SOLAR_CHARGE_FOX_MODE", "selfuse"),
        enum=("selfuse", "backup_hold", "backup_fill"),
        description=(
            "A2 (#679): Fox mode for solar_charge slots. 'selfuse' (DEFAULT) = "
            "plain SelfUse at reserve — PV fills the battery and the inverter "
            "NEVER auto-imports from grid (respects 'charging = the LP's "
            "decision'); the rare discharge leak is accepted (empty-at-peak "
            "~1/30 days, handled at the LP level). 'backup_hold' = "
            "Backup(reserve, reserve): a strict no-discharge hold that also "
            "BLOCKS the PV fill (exports midday surplus) — same tuple as A1 "
            "pre-peak holds. 'backup_fill' = Backup(minSoc=reserve, "
            "maxSoc=planned-SoC): lets PV fill toward the LP target BUT "
            "**grid-imports toward maxSoc on firmware < 1.55 (our H1 is 1.51) — "
            "do NOT enable until firmware >= 1.55 is confirmed** (the "
            "_guard_nonneg_backup_maxsoc guard will clamp it to reserve at "
            "positive prices meanwhile). The retired SelfUse(100,100) shape is "
            "never emittable. Vacation preset always keeps plain SelfUse."
        ),
    ),
    # Legionella thermal-shock awareness. The Daikin Onecta firmware fires the
    # cycle autonomously on a user-configured day/hour (set in the Onecta app);
    # the LP cannot command it. These knobs let the LP *predict* the resulting
    # DHW pulse so it allocates PV/battery/grid correctly that hour. Set
    # DHW_LEGIONELLA_DAY=-1 to disable the prediction (no uplift; firmware still
    # fires whenever it pleases — LP just won't see it coming).
    "DHW_LEGIONELLA_DAY": SettingSpec(
        key="DHW_LEGIONELLA_DAY",
        type_name="int",
        env_default=_int_env("DHW_LEGIONELLA_DAY", "-1"),
        min_value=-1,
        max_value=6,
        description=(
            "Local weekday for predicted legionella thermal-shock cycle. "
            "-1 disabled, 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun. "
            "Must match what is configured in the Daikin Onecta app — the LP "
            "uses this only to inject a predicted DHW pulse into passive-mode "
            "load forecast; it does not command the firmware."
        ),
    ),
    "DHW_LEGIONELLA_HOUR_LOCAL": SettingSpec(
        key="DHW_LEGIONELLA_HOUR_LOCAL",
        type_name="int",
        env_default=_int_env("DHW_LEGIONELLA_HOUR_LOCAL", "13"),
        min_value=0,
        max_value=23,
        description=(
            "Local hour at which the legionella cycle starts (0–23). Must match "
            "the Daikin Onecta schedule. Used only with DHW_LEGIONELLA_DAY ≥ 0."
        ),
    ),
    "DHW_LEGIONELLA_DURATION_MIN": SettingSpec(
        key="DHW_LEGIONELLA_DURATION_MIN",
        type_name="int",
        env_default=_int_env("DHW_LEGIONELLA_DURATION_MIN", "60"),
        min_value=30,
        max_value=240,
        description=(
            "Estimated cycle duration in minutes (rounded up to whole 30-min slots). "
            "60 min ≈ 200 L tank from 50 → 60 °C at typical Altherma DHW power."
        ),
    ),
    "DHW_LEGIONELLA_TANK_TARGET_C": SettingSpec(
        key="DHW_LEGIONELLA_TANK_TARGET_C",
        type_name="float",
        env_default=_float_env("DHW_LEGIONELLA_TANK_TARGET_C", "60"),
        min_value=50.0,
        max_value=70.0,
        description=(
            "Tank temperature reached during the cycle (°C). The LP uses "
            "(target − DHW_TEMP_NORMAL_C) to size the predicted electric pulse."
        ),
    ),
    # Post-shower overnight tank target sent to Daikin firmware. Lives in
    # runtime_settings so the user can tune it without restart — mirrors the
    # household's empirical "set tank low at bedtime, let it cool" pattern.
    # Below this value the firmware reheats; above it, firmware idles. The
    # default 38 °C is a safety backup for unexpected morning showers; users
    # who confirm no morning shower demand can drop to 37 (matches the user's
    # manual habit on this installation) or even 30 for full overnight cool.
    "DHW_TANK_OVERNIGHT_TARGET_C": SettingSpec(
        key="DHW_TANK_OVERNIGHT_TARGET_C",
        type_name="float",
        env_default=_float_env("DHW_TANK_OVERNIGHT_TARGET_C", "38"),
        min_value=30.0,
        max_value=55.0,
        description=(
            "Daikin tank target (°C) sent during tank_idle_overnight slots — "
            "post-evening-shower until next-day PV abundance. Lower = more "
            "overnight standing-loss savings; firmware won't reheat the tank "
            "from 50+°C down to this value, only triggers if tank actually "
            "drops to it. Min 30 (still well above pipe-freeze risk; weekly "
            "legionella thermal-shock cycle handles pasteurisation)."
        ),
    ),
    # PV abundance target — applied during ``solar_charge`` slots when free
    # PV would otherwise be exported. Runtime-tunable per household occupancy:
    # a 4-person household with evening shower load benefits from 50 °C; a
    # solo dweller might drop to 42 to minimise standing loss. Default 45
    # matches DHW_TEMP_NORMAL_C — assume "no extra °C unless household demands
    # it". Bumped from a previous env-only default of 55, since 14-day Daikin
    # consumption telemetry showed overnight DHW reheat = 0 even when tank
    # exited PV-abundance windows at 45-48 °C.
    "DHW_TEMP_PV_ABUNDANCE_TARGET_C": SettingSpec(
        key="DHW_TEMP_PV_ABUNDANCE_TARGET_C",
        type_name="float",
        env_default=_float_env("DHW_TEMP_PV_ABUNDANCE_TARGET_C", "60"),
        min_value=40.0,
        max_value=60.0,
        description=(
            "Daikin tank target (°C) during solar_charge / solar_preheat "
            "slots. This is a STORAGE target, NOT a comfort target — "
            "the tank is lifted as high as possible WHILE PV is excess "
            "(free energy), then a paired 'restore' action at the end of "
            "the solar window drops the target back to "
            "``DHW_TEMP_NORMAL_C`` (45 °C). Outside the solar window the "
            "tank decays naturally via standing loss until evening "
            "showers consume the stored thermal energy. "
            "\n\n"
            "Default 60 (PR H, 2026-05-22): matches the user's manual "
            "preference after they observed PR G's 46 °C (a comfort-"
            "level setting wrongly applied as the storage ceiling) was "
            "exporting PV instead of storing thermal. "
            "\n\n"
            "Economics: lifting 45 → 60 °C stores ~3.5 kWh thermal "
            "(200 L × 4186 × 15 / 3.6e6). After ~5 h decay (standing "
            "loss 97 W at indoor 21 °C → 0.5 kWh thermal lost), evening "
            "showers can draw ~3 kWh thermal ≈ 1 kWh elec equivalent "
            "(vs grid import at 12-30 p/kWh). Vs exporting that same PV "
            "at 5-15 p/kWh export rate, storage wins by ~6-12 p/day on "
            "PV-rich days. "
            "\n\n"
            "Capped at 60 (= legionella target floor) to protect tank "
            "longevity. Lower this if you don't want to push the tank "
            "that high (e.g. 50 = ~2 kWh thermal stored, less aggressive)."
        ),
    ),
}


# Version counter: bumped on every set_setting() so get() can short-circuit
# without a DB round-trip when the cache entry is current. A TTL-based fallback
# catches out-of-band writes (e.g. manual UPDATE from sqlite3 shell).
_lock = threading.RLock()
_version: int = 0
_cache: dict[str, tuple[Any, int, float]] = {}  # key -> (value, version, monotonic_at)
_TTL_SECONDS: float = 30.0


def _coerce(spec: SettingSpec, raw: str) -> Any:
    if spec.type_name == "float":
        return float(raw)
    if spec.type_name == "int":
        return int(raw)
    if spec.type_name == "str":
        return raw.strip().lower()
    if spec.type_name == "list[int]":
        return sorted({int(p.strip()) for p in raw.split(",") if p.strip()})
    raise SettingValidationError(f"unknown type {spec.type_name!r}")


def _validate(spec: SettingSpec, value: Any) -> Any:
    """Coerce and range/enum-check. Returns the canonical in-memory value.

    Raises :class:`SettingValidationError` with a human-readable message that
    becomes the 400 response body.
    """
    try:
        if spec.type_name == "float":
            v = float(value)
        elif spec.type_name == "int":
            v = int(value)
        elif spec.type_name == "str":
            v = str(value).strip().lower()
        elif spec.type_name == "list[int]":
            if isinstance(value, str):
                v = sorted({int(p.strip()) for p in value.split(",") if p.strip()})
            else:
                v = sorted({int(p) for p in value})
        else:
            raise SettingValidationError(f"unknown type {spec.type_name!r}")
    except (TypeError, ValueError) as e:
        raise SettingValidationError(
            f"{spec.key}: cannot coerce {value!r} to {spec.type_name}: {e}"
        ) from e

    if spec.enum is not None and v not in spec.enum:
        raise SettingValidationError(
            f"{spec.key}: {v!r} not in {list(spec.enum)}"
        )
    if spec.min_value is not None and isinstance(v, (int, float)):
        if v < spec.min_value:
            raise SettingValidationError(
                f"{spec.key}: {v} < min {spec.min_value}"
            )
    if spec.max_value is not None and isinstance(v, (int, float)):
        if v > spec.max_value:
            raise SettingValidationError(
                f"{spec.key}: {v} > max {spec.max_value}"
            )
    return v


def _serialize(spec: SettingSpec, value: Any) -> str:
    if spec.type_name == "list[int]":
        return ",".join(str(int(x)) for x in value)
    return str(value)


def get_setting(key: str) -> Any:
    """Return the current value for *key*.

    Reads hit an in-memory cache that is valid for the current ``_version`` and
    up to ``_TTL_SECONDS``. On miss: read the DB; if absent, call the spec's
    ``env_default`` once and cache it.
    """
    spec = SCHEMA.get(key)
    if spec is None:
        raise KeyError(f"unknown runtime setting: {key!r}")
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None:
            value, version, cached_at = entry
            if version == _version and (now - cached_at) < _TTL_SECONDS:
                return value

        raw = db.get_runtime_setting(key)
        if raw is None:
            try:
                value = spec.env_default()
            except Exception as e:
                logger.warning(
                    "runtime_setting %s: env_default failed (%s); using 0", key, e
                )
                value = 0
        else:
            try:
                value = _coerce(spec, raw)
            except Exception as e:
                logger.warning(
                    "runtime_setting %s: stored value %r failed to coerce (%s); "
                    "falling back to env default",
                    key,
                    raw,
                    e,
                )
                value = spec.env_default()
        translators = _LEGACY_VALUE_TRANSLATORS.get(key)
        if translators and isinstance(value, str) and value in translators:
            translated = translators[value]
            token = (key, value)
            if token not in _LEGACY_TRANSLATION_LOGGED:
                _LEGACY_TRANSLATION_LOGGED.add(token)
                logger.warning(
                    "runtime_setting %s: legacy value %r translated to %r "
                    "(deprecated; update stored value when convenient).",
                    key, value, translated,
                )
            value = translated
        _cache[key] = (value, _version, now)
        return value


def set_setting(key: str, value: Any, *, actor: str = "api") -> Any:
    """Validate + persist + invalidate cache. Returns the canonical value stored.

    Side effect: when the spec has ``cron_reload=True`` the caller (API/MCP
    handler) must invoke ``scheduler.runner.reregister_cron_jobs`` after this
    function returns. We do **not** import the scheduler here to avoid a
    circular dependency (scheduler reads config which will soon read this).
    """
    spec = SCHEMA.get(key)
    if spec is None:
        raise SettingValidationError(f"unknown runtime setting: {key!r}")

    canonical = _validate(spec, value)
    serialized = _serialize(spec, canonical)
    db.set_runtime_setting(key, serialized)
    # V11: append-only audit trail so a past LP run can be explained even
    # after a knob is changed. Non-fatal — never block the setting write.
    try:
        db.log_config_change(key, serialized, op="set", actor=actor)
    except Exception as e:
        logger.debug("config_audit insert failed (non-fatal): %s", e)
    global _version
    with _lock:
        _version += 1
        _cache.pop(key, None)
    logger.info(
        "runtime_setting updated: %s=%r (actor=%s, cron_reload=%s)",
        key,
        canonical,
        actor,
        spec.cron_reload,
    )
    return canonical


def delete_setting(key: str, *, actor: str = "api") -> bool:
    """Drop the override row so the next read returns the env default.

    Returns True when a row was removed.
    """
    spec = SCHEMA.get(key)
    if spec is None:
        raise SettingValidationError(f"unknown runtime setting: {key!r}")
    removed = db.delete_runtime_setting(key)
    if removed:
        try:
            db.log_config_change(key, None, op="delete", actor=actor)
        except Exception as e:
            logger.debug("config_audit delete insert failed (non-fatal): %s", e)
    global _version
    with _lock:
        _version += 1
        _cache.pop(key, None)
    logger.info(
        "runtime_setting cleared: %s (actor=%s, removed=%s)", key, actor, removed
    )
    return removed


def list_settings() -> list[dict[str, Any]]:
    """Return current state of every known key, with default and updated_at."""
    rows = {r["key"]: r for r in db.list_runtime_settings()}
    out: list[dict[str, Any]] = []
    for key, spec in SCHEMA.items():
        row = rows.get(key)
        try:
            current = get_setting(key)
        except Exception:
            current = None
        try:
            default = spec.env_default()
        except Exception:
            default = None
        out.append({
            "key": key,
            "value": current,
            "default": default,
            "updated_at": row["updated_at"] if row else None,
            "overridden": row is not None,
            "type": spec.type_name,
            "min": spec.min_value,
            "max": spec.max_value,
            "enum": list(spec.enum) if spec.enum else None,
            "cron_reload": spec.cron_reload,
            "description": spec.description,
        })
    return out


def clear_cache() -> None:
    """Invalidate the entire cache (used by tests and by the cron-reload path)."""
    global _version
    with _lock:
        _version += 1
        _cache.clear()
