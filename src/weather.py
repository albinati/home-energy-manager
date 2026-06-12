"""Weather data: historical analytics (Open-Meteo Archive) and forecast for optimization control.

Historical API (archive-api.open-meteo.com): daily mean temps, no key required.
Forecast API (api.open-meteo.com): hourly temperature, cloud cover, solar radiation, no key required.

Forecast is used by the optimization solver to:
  - Estimate heating demand per half-hour slot (degree-hours below base temp)
  - Estimate PV generation (4.5kWp × radiation × system efficiency)
  - Pre-heat before cold spells in cheap windows
  - Skip grid battery charging when solar is expected to fill the battery
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from collections.abc import Callable
from typing import Any

from .config import config, cop_at_temperature
from .physics import apply_cop_lift_multiplier, get_lwt_base_c

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Historical (analytics only)
# --------------------------------------------------------------------------

def fetch_daily_temps(
    start_date: date,
    end_date: date,
    lat: str | None = None,
    lon: str | None = None,
) -> list[tuple[str, float]]:
    """
    Fetch daily mean temperature (°C) for date range. Returns list of (date_str, temp_c).
    Uses Open-Meteo Historical API. If lat/lon not configured, returns [].
    """
    lat = (lat or config.WEATHER_LAT or "").strip()
    lon = (lon or config.WEATHER_LON or "").strip()
    if not lat or not lon:
        return []
    try:
        start = start_date.isoformat()
        end = end_date.isoformat()
        url = (
            "https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat}&longitude={lon}&start_date={start}&end_date={end}"
            "&daily=temperature_2m_mean"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        daily = data.get("daily") or {}
        times = daily.get("time") or []
        temps = daily.get("temperature_2m_mean") or []
        out = []
        for i, t in enumerate(times):
            if i < len(temps) and temps[i] is not None:
                out.append((str(t), float(temps[i])))
        return out
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []


def fetch_daily_solar_kwh(
    start_date: date,
    end_date: date,
    lat: str | None = None,
    lon: str | None = None,
    capacity_kwp: float | None = None,
    efficiency: float | None = None,
) -> dict[str, float]:
    """Fetch Open-Meteo **archive** daily shortwave radiation and convert to kWh for the system.

    Returns dict of {date_str: modelled_solar_kwh}. Uses archive-api (past dates only).
    Applies the same PV model as the forecast path so ratios are consistent.
    """
    lat = (lat or config.WEATHER_LAT or "").strip()
    lon = (lon or config.WEATHER_LON or "").strip()
    if not lat or not lon:
        return {}
    cap = capacity_kwp if capacity_kwp is not None else float(config.PV_CAPACITY_KWP)
    eff = efficiency if efficiency is not None else float(config.PV_SYSTEM_EFFICIENCY)
    try:
        url = (
            "https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat}&longitude={lon}"
            f"&start_date={start_date.isoformat()}&end_date={end_date.isoformat()}"
            "&daily=shortwave_radiation_sum"
            "&timezone=UTC"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        daily = data.get("daily") or {}
        times = daily.get("time") or []
        radiation = daily.get("shortwave_radiation_sum") or []  # MJ/m²/day
        out: dict[str, float] = {}
        for i, t in enumerate(times):
            if i >= len(radiation) or radiation[i] is None:
                continue
            # Convert MJ/m²/day → Wh/m²/day (*277.78) then to kWh DC (*cap/1000*eff)
            kwh = float(radiation[i]) * 277.78 * cap * eff / 1000.0
            out[str(t)] = round(kwh, 3)
        return out
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}


def _build_pv_hourly_ceiling(limit_days: int = 250) -> dict[int, float]:
    """Return per-hour-of-day (0-23 UTC) maximum observed PV kWh/slot from Fox history.

    Used as a physical ceiling in forecast_to_lp_inputs so the LP never sees
    more PV than the system has ever actually produced at that hour.
    Falls back to capacity × efficiency × 0.5 when Fox data is unavailable.
    """
    default_ceil = float(config.PV_CAPACITY_KWP) * float(config.PV_SYSTEM_EFFICIENCY) * 0.5
    try:
        from . import db as _db

        rows = _db.get_fox_energy_daily(limit=limit_days)
        if not rows:
            return {h: default_ceil for h in range(24)}

        # We only have daily totals, not hourly — use a modelled solar curve to distribute
        # daily kWh across hours, then take per-hour maximum across all days.
        # Simple triangular distribution: solar generation peaks at solar noon (~13:00 UTC in UK)
        # with a roughly sinusoidal daily shape.  We scale by actual / theoretical_daily.
        import math
        hour_weights = {}
        for h in range(24):
            # sinusoidal: zero outside 5-21 UTC, peak at 13 UTC
            angle = math.pi * (h - 5) / (21 - 5)
            hour_weights[h] = max(0.0, math.sin(angle)) if 5 <= h <= 21 else 0.0
        weight_sum = sum(hour_weights.values())

        per_hour_max: dict[int, float] = {h: 0.0 for h in range(24)}
        for r in rows:
            solar = r.get("solar_kwh") or 0.0
            if solar < 0.5:
                continue
            # Distribute daily solar across hours proportionally
            for h in range(24):
                w = hour_weights[h] / weight_sum if weight_sum > 0 else 0.0
                kwh_slot = solar * w  # kWh in that hour (not per half-hour yet)
                # Each hour has 2 half-hour slots, so kWh per slot = kwh_slot / 2
                per_hour_max[h] = max(per_hour_max[h], kwh_slot / 2.0)

        # Apply a 20% safety margin above observed max so we don't over-clip good days
        return {h: min(v * 1.20, default_ceil) for h, v in per_hour_max.items()}
    except Exception:
        return {h: default_ceil for h in range(24)}


def compute_pv_calibration_factor(
    limit_days: int | None = None,
    min_solar_kwh: float = 1.0,
) -> float:
    """Compute rolling PV calibration factor: mean(actual / modelled) from Fox history.

    Compares fox_energy_daily.solar_kwh with Open-Meteo archive modelled solar for
    the same days. Returns a multiplier to scale the forward forecast. Clamps to [0.35, 1.05]
    (we never want to predict *more* than the best observed ratio, and never below 35%).
    - If ``PV_FORECAST_SCALE_FACTOR`` in config is > 0, that manual value is used directly.
    - If no Fox history is in the DB, falls back to 1.0.
    - Outlier days (ratio > 1.5) are dropped as they indicate archive data anomalies.
    - ``limit_days`` defaults to ``config.PV_CALIBRATION_WINDOW_DAYS`` (runtime-tunable).
      Shorter windows track seasonality faster; longer windows are more stable but
      slow to react to spring/autumn transitions.
    """
    if limit_days is None:
        limit_days = int(getattr(config, "PV_CALIBRATION_WINDOW_DAYS", 30))
    manual = float(getattr(config, "PV_FORECAST_SCALE_FACTOR", 0.0))
    if manual > 0:
        return manual
    default = 1.0
    try:
        from . import db as _db

        rows = _db.get_fox_energy_daily(limit=limit_days)
        if not rows:
            return default

        today = date.today()
        valid = [
            r for r in rows
            if (r.get("solar_kwh") or 0) >= min_solar_kwh
            and (r.get("date") or "") < today.isoformat()
        ]
        if not valid:
            return default

        dates = sorted(r["date"] for r in valid)
        start = date.fromisoformat(dates[0])
        end = date.fromisoformat(dates[-1])
        modelled = fetch_daily_solar_kwh(start, end)

        ratios: list[float] = []
        for r in valid:
            d = r["date"]
            mod = modelled.get(d)
            if not mod or mod <= 0:
                continue
            ratio = float(r["solar_kwh"]) / mod
            # Drop outlier days where archive data appears anomalous
            if ratio <= 1.5:
                ratios.append(ratio)

        if not ratios:
            return default

        factor = sum(ratios) / len(ratios)
        # Clamp: never predict more than best observed, never below 35%
        return round(max(0.35, min(1.05, factor)), 4)
    except Exception:
        return default


# --------------------------------------------------------------------------
# Forecast (optimization control)
# --------------------------------------------------------------------------

@dataclass
class HourlyForecast:
    """One hour of weather forecast data."""

    time_utc: datetime
    temperature_c: float
    cloud_cover_pct: float        # 0-100
    shortwave_radiation_wm2: float  # W/m² surface global tilted irradiance
    estimated_pv_kw: float        # estimated PV generation for 4.5kWp system
    heating_demand_factor: float  # 0-1: how much heating is needed relative to base temp
    pv_direct: bool = False       # True when provider supplies site PV directly.


@dataclass
class ForecastFetchResult:
    """Forecast plus provider metadata for canonical snapshot persistence."""

    forecast: list[HourlyForecast]
    source: str
    model_name: str | None = None
    model_version: str | None = None
    raw_payload_json: str | None = None


# PR L3 H5 — one-shot warning latch for astral-missing degradation.
_ASTRAL_WARNED = False

# System constants for PV estimate (London W4 system — 4.5 kWp DMEGC
# 450 W modules per MCS cert MCS-02470690-S, mounted "Above Roof" with
# Van der Valk Valkpitched rails. Aggregate empirical azimuth ~200° SSW
# from the 2026-05-24 orientation sweep. Production shows a late-PM
# cliff at 17:00 UTC consistent with a fixed obstruction roughly due
# west. Orientation/obstruction are NOT encoded here; the per-hour ×
# cloud × elevation calibration tables absorb them empirically.
_PV_CAPACITY_KWP = 4.5
_PV_SYSTEM_EFFICIENCY = 0.85  # accounts for inverter, wiring, temp de-rating
_IRRADIANCE_AT_STC_WM2 = 1000.0  # standard test conditions


def estimate_pv_kw(
    shortwave_radiation_wm2: float,
    capacity_kwp: float = _PV_CAPACITY_KWP,
    efficiency: float = _PV_SYSTEM_EFFICIENCY,
) -> float:
    """Estimate AC PV generation (kW) from surface solar irradiance (W/m²)."""
    if shortwave_radiation_wm2 <= 0:
        return 0.0
    return capacity_kwp * (shortwave_radiation_wm2 / _IRRADIANCE_AT_STC_WM2) * efficiency


def forecast_pv_kw_from_row(
    hour_utc: int,
    shortwave_radiation_wm2: float,
    cloud_cover_pct: float | None,
    *,
    direct_pv_kw: float | None = None,
    cloud_table: dict[tuple[int, int], float] | None = None,
    hourly_table: dict[int, float] | None = None,
    flat: float = 1.0,
    scale: float = 1.0,
    table_3d: dict[tuple[int, int, int], float] | None = None,
    slot_utc: datetime | None = None,
) -> float:
    """Apply the same PV forecast transform used by the LP and skill logger.

    **PR L1 (2026-05-24)** — Quartz direct-PV path now ALSO applies the
    calibration tables (was previously SKIPPED per PR #279's
    "Quartz self-calibrates" assumption). Prod telemetry showed GSP-level
    Quartz mispredicts our W4 1DZ array by ~35 % AM and ~20 % PM
    (`forecast_skill_log` 30-day mean ratios). Half-hourly evidence on 10
    clear days (2026-05-12 to -24) shows a consistent **late-PM cliff**
    between 16:30 and 17:00 UTC — production drops 50–72 % in 30 min on
    6 of 10 clear days, far steeper than geometry (max ~25 %) allows.
    Sun at that time sits at azimuth ~270°, elevation ~21°, so a fixed
    obstruction roughly due west of the panels is the only plausible
    cause. The calibration tables absorb the cliff empirically (hour 17 Z
    cell ratio 0.84 vs neighbouring 16 Z ~1.01) — they don't need to know
    the cause.

    **Known semantic gap (acknowledged):** the calibration tables are
    trained against ``actual / estimate_pv_kw(open_meteo_radiation)``,
    NOT against ``actual / quartz_prediction``. Applying them as a
    Quartz multiplier is empirically effective (the 30-day mean factors
    roughly match the observed Quartz bias because both correct for the
    same per-hour residual pattern), but the conversion is imperfect in
    partly-cloudy regimes where Quartz's blend model diverges from raw
    shortwave radiation. Phase 1.1 of the calibration epic adds a
    ``source`` column to segregate Quartz-vs-Open-Meteo residuals if
    post-deploy data shows over-correction. Set
    ``PV_QUARTZ_APPLY_CALIBRATION=false`` to restore the legacy bypass.

    Cloud attenuation is applied to the radiation path before the
    irradiance-to-kW conversion. The Quartz path skips attenuation
    (its prediction is already AC kW) but uses the cloud bucket to look
    up the matching calibration factor.
    """
    scale_f = max(0.0, float(scale))
    if direct_pv_kw is not None:
        try:
            base_kw = max(0.0, float(direct_pv_kw))
            if getattr(config, "PV_QUARTZ_APPLY_CALIBRATION", True):
                cloud_pct_f = (
                    float(cloud_cover_pct) if cloud_cover_pct is not None else 50.0
                )
                cal = get_pv_calibration_factor_for(
                    int(hour_utc),
                    cloud_pct_f,
                    cloud_table=cloud_table,
                    hourly_table=hourly_table,
                    flat=flat,
                    table_3d=table_3d,
                    slot_utc=slot_utc,
                )
                return base_kw * cal * scale_f
            return base_kw * float(flat) * scale_f
        except (TypeError, ValueError):
            pass
    cloud_pct_f = float(cloud_cover_pct) if cloud_cover_pct is not None else 50.0
    att = max(0.0, min(1.0, 1.0 - 0.25 * (cloud_pct_f / 100.0)))
    rad_eff = max(0.0, float(shortwave_radiation_wm2) * att)
    cal = get_pv_calibration_factor_for(
        int(hour_utc),
        cloud_pct_f,
        cloud_table=cloud_table,
        hourly_table=hourly_table,
        flat=flat,
        table_3d=table_3d,
        slot_utc=slot_utc,
    )
    return estimate_pv_kw(rad_eff) * cal * scale_f


def compute_heating_demand_factor(
    temperature_c: float,
    base_temp_c: float | None = None,
) -> float:
    """Return a 0-1 heating demand factor: 0 = no heating needed, 1 = maximum.

    Uses a simple degree-day model: demand is proportional to (base - outdoor)
    clamped to [0, 1] over a 20°C range.
    """
    base = base_temp_c if base_temp_c is not None else config.WEATHER_DEGREE_DAY_BASE_C
    delta = base - temperature_c
    if delta <= 0:
        return 0.0
    return min(1.0, delta / 20.0)


def fetch_forecast(
    lat: str | None = None,
    lon: str | None = None,
    hours: int = 48,
) -> list[HourlyForecast]:
    """Fetch forecast rows from the configured provider."""
    return fetch_forecast_snapshot(lat=lat, lon=lon, hours=hours).forecast


# In-process TTL cache for the forecast fetch — keyed on ``hours`` (lat/lon are
# config-pinned). Shared by /weather + /pv/today so the cockpit doesn't make two
# blocking Open-Meteo calls per load. Open-Meteo is hourly-deterministic, so a
# ~15 min cache is safe; the LP/heartbeat keep using ``fetch_forecast`` directly.
_forecast_cache: dict[int, tuple[float, list[HourlyForecast]]] = {}


def fetch_forecast_cached(
    lat: str | None = None,
    lon: str | None = None,
    hours: int = 48,
    ttl_seconds: int | None = None,
) -> list[HourlyForecast]:
    """TTL-cached :func:`fetch_forecast` for the cockpit API endpoints. Serves the
    last good value on a failed/empty refresh rather than an empty list."""
    import time as _t
    if ttl_seconds is None:
        ttl_seconds = int(getattr(config, "WEATHER_FORECAST_CACHE_TTL_SECONDS", 900))
    if ttl_seconds <= 0:
        return fetch_forecast(lat=lat, lon=lon, hours=hours)
    now = _t.monotonic()
    hit = _forecast_cache.get(hours)
    if hit and hit[1] and (now - hit[0]) < ttl_seconds:
        return hit[1]
    fresh = fetch_forecast(lat=lat, lon=lon, hours=hours)
    if fresh:
        _forecast_cache[hours] = (now, fresh)
        return fresh
    return hit[1] if hit else fresh  # serve stale on a failed fetch


# Separate TTL cache for the WEATHER PANEL forecast (open-meteo only — temp +
# cloud over a multi-day horizon). The planning fetch above is merged with
# Quartz, which caps the horizon to Quartz's ~2-day PV window; the panel only
# needs temp/cloud, so it uses open-meteo direct for a real 4-day forecast.
_weather_panel_cache: dict[int, tuple[float, list[HourlyForecast]]] = {}


def fetch_weather_panel_forecast_cached(
    lat: str | None = None,
    lon: str | None = None,
    hours: int = 96,
    ttl_seconds: int | None = None,
) -> list[HourlyForecast]:
    """TTL-cached open-meteo-only forecast for the cockpit weather panel (temp +
    cloud over ``hours``, default 96 h = 4 days). Bypasses the Quartz merge so
    the multi-day forecast isn't truncated to the PV-forecast horizon."""
    import time as _t
    if ttl_seconds is None:
        ttl_seconds = int(getattr(config, "WEATHER_FORECAST_CACHE_TTL_SECONDS", 900))
    now = _t.monotonic()
    hit = _weather_panel_cache.get(hours)
    if ttl_seconds > 0 and hit and hit[1] and (now - hit[0]) < ttl_seconds:
        return hit[1]
    try:
        fresh = _fetch_open_meteo_forecast(lat=lat, lon=lon, hours=hours)
    except Exception:  # pragma: no cover - degrade gracefully
        return hit[1] if hit else []
    if fresh:
        _weather_panel_cache[hours] = (now, fresh)
        return fresh
    return hit[1] if hit else fresh


def fetch_forecast_snapshot(
    lat: str | None = None,
    lon: str | None = None,
    hours: int = 48,
) -> ForecastFetchResult:
    """Fetch forecast rows plus source metadata for canonical persistence."""
    source = (getattr(config, "FORECAST_SOURCE", "open_meteo") or "open_meteo").strip().lower()
    if source in ("quartz", "quartz_solar"):
        base = _fetch_open_meteo_forecast(lat=lat, lon=lon, hours=hours)
        provider = (getattr(config, "QUARTZ_PROVIDER", "open") or "open").strip().lower()
        if provider == "open":
            quartz = _fetch_quartz_open_forecast(
                hours=hours, base_weather=base, lat=lat, lon=lon
            )
        else:
            quartz = _fetch_quartz_forecast(hours=hours, base_weather=base)
        if quartz.forecast:
            return quartz
        logger.warning("Quartz forecast unavailable; falling back to Open-Meteo")
        return ForecastFetchResult(forecast=base, source="open-meteo")
    return ForecastFetchResult(
        forecast=_fetch_open_meteo_forecast(lat=lat, lon=lon, hours=hours),
        source="open-meteo",
    )


def _fetch_open_meteo_forecast(
    lat: str | None = None,
    lon: str | None = None,
    hours: int = 48,
) -> list[HourlyForecast]:
    """Fetch hourly forecast for the next `hours` hours.

    Uses Open-Meteo Forecast API (free, no key). Returns [] if lat/lon not configured
    or the fetch fails (graceful degradation — solver works without forecast).

    Fields: temperature_2m, cloud_cover, shortwave_radiation_instant.
    """
    lat = (lat or config.WEATHER_LAT or "").strip()
    lon = (lon or config.WEATHER_LON or "").strip()
    if not lat or not lon:
        return []

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m,cloud_cover,shortwave_radiation_instant"
            f"&forecast_days={max(2, (hours // 24) + 1)}"
            "&timezone=UTC"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    clouds = hourly.get("cloud_cover") or []
    radiation = hourly.get("shortwave_radiation_instant") or []

    now = datetime.now(UTC)
    result: list[HourlyForecast] = []
    for i, t in enumerate(times):
        if len(result) >= hours:
            break
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if dt < now:
                continue
            temp_c = float(temps[i]) if i < len(temps) and temps[i] is not None else 10.0
            cloud_pct = float(clouds[i]) if i < len(clouds) and clouds[i] is not None else 50.0
            rad_wm2 = float(radiation[i]) if i < len(radiation) and radiation[i] is not None else 0.0
            result.append(
                HourlyForecast(
                    time_utc=dt,
                    temperature_c=temp_c,
                    cloud_cover_pct=cloud_pct,
                    shortwave_radiation_wm2=rad_wm2,
                    estimated_pv_kw=estimate_pv_kw(rad_wm2),
                    heating_demand_factor=compute_heating_demand_factor(temp_c),
                )
            )
        except (ValueError, IndexError, TypeError):
            continue

    return result


# Cached Auth0 access token + epoch second when it expires. We fetch a fresh
# token slightly before the upstream ``expires_in`` mark so the next forecast
# request never races a 401 from the API.
_QUARTZ_TOKEN: str | None = None
_QUARTZ_TOKEN_EXPIRES_AT: float = 0.0
_QUARTZ_TOKEN_REFRESH_LEEWAY_SECONDS = 60


def _quartz_token(*, force_refresh: bool = False) -> str | None:
    """Return a cached Quartz bearer token, refreshing when expired or forced.

    Auth0 access tokens issued for the password grant typically expire after
    24 h. The original implementation cached the token for the lifetime of
    the process and silently 401'd thereafter. This version tracks
    ``expires_in`` and re-authenticates when the token is within the leeway
    window or explicitly forced (e.g. on a 401 from the forecast endpoint).
    """
    global _QUARTZ_TOKEN, _QUARTZ_TOKEN_EXPIRES_AT
    import time as _time

    if (
        not force_refresh
        and _QUARTZ_TOKEN
        and (_QUARTZ_TOKEN_EXPIRES_AT - _QUARTZ_TOKEN_REFRESH_LEEWAY_SECONDS) > _time.time()
    ):
        return _QUARTZ_TOKEN

    username = (getattr(config, "QUARTZ_USERNAME", "") or "").strip()
    password = getattr(config, "QUARTZ_PASSWORD", "") or ""
    client_id = (getattr(config, "QUARTZ_CLIENT_ID", "") or "").strip()
    if not username or not password or not client_id:
        logger.warning(
            "Quartz credentials are not configured (need QUARTZ_USERNAME, "
            "QUARTZ_PASSWORD, QUARTZ_CLIENT_ID)"
        )
        return None
    body = {
        "client_id": client_id,
        "audience": getattr(config, "QUARTZ_AUDIENCE", "https://api.nowcasting.io/"),
        "grant_type": "password",
        "username": username,
        "password": password,
    }
    try:
        req = urllib.request.Request(
            getattr(config, "QUARTZ_AUTH_URL", "https://nowcasting-pro.eu.auth0.com/oauth/token"),
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        token = str(payload.get("access_token") or "")
        if not token:
            logger.warning("Quartz auth response did not include access_token")
            return None
        try:
            expires_in = float(payload.get("expires_in") or 0.0)
        except (TypeError, ValueError):
            expires_in = 0.0
        _QUARTZ_TOKEN = token
        # Default to a one-hour TTL when the upstream omits ``expires_in`` so
        # we still re-authenticate periodically rather than caching forever.
        _QUARTZ_TOKEN_EXPIRES_AT = _time.time() + (expires_in if expires_in > 0 else 3600.0)
        return token
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.warning("Quartz auth failed: %s", e)
        return None


def _quartz_site_kw(
    row: dict[str, Any],
    *,
    installed_capacity_mw: float | None = None,
) -> float | None:
    normalized = row.get("expectedPowerGenerationNormalized")
    if normalized is not None:
        try:
            frac = float(normalized)
        except (TypeError, ValueError):
            frac = 0.0
        if frac > 1.0:
            frac /= 100.0
        return max(0.0, frac * float(config.PV_CAPACITY_KWP))

    capacity_mw = (
        float(installed_capacity_mw)
        if installed_capacity_mw is not None and installed_capacity_mw > 0
        else float(getattr(config, "QUARTZ_INSTALLED_CAPACITY_MW", 0.0) or 0.0)
    )
    if capacity_mw <= 0:
        return None
    try:
        mw = float(row.get("expectedPowerGenerationMegawatts") or 0.0)
    except (TypeError, ValueError):
        return None
    frac = mw / capacity_mw
    return max(0.0, frac * float(config.PV_CAPACITY_KWP))


def _quartz_open_planes() -> list[dict[str, float]]:
    """Panel planes for the site-level open Quartz model (#542).

    ``QUARTZ_OPEN_PLANES`` JSON when set; otherwise a single aggregate plane
    (tilt 30, orientation 200 = the measured SSW aggregate of this split
    array, capacity ``PV_CAPACITY_KWP``). Bad JSON → aggregate fallback with
    a warning, never a crash (forecast fetch must degrade gracefully).
    """
    raw = (getattr(config, "QUARTZ_OPEN_PLANES", "") or "").strip()
    fallback = [{
        "tilt": 30.0,
        "orientation": 200.0,
        "capacity_kwp": float(getattr(config, "PV_CAPACITY_KWP", 4.5)),
    }]
    if not raw:
        return fallback
    try:
        planes = json.loads(raw)
        ok = [
            {
                "tilt": float(p.get("tilt", 30)),
                "orientation": float(p.get("orientation", 200)),
                "capacity_kwp": float(p["capacity_kwp"]),
            }
            for p in planes
            if isinstance(p, dict) and p.get("capacity_kwp")
        ]
        if ok:
            return ok
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as e:
        logger.warning("QUARTZ_OPEN_PLANES unparseable (%s) — using aggregate plane", e)
    return fallback


def _quartz_open_live_generation() -> list[dict[str, Any]]:
    """Recent measured generation samples for nowcast anchoring (#542).

    The hosted open.quartz.solar twin uses these to anchor the first forecast
    hours to reality; the local sidecar accepts-and-ignores. Best-effort —
    any DB trouble returns [] and the request goes out without them.
    """
    if not bool(getattr(config, "QUARTZ_OPEN_SEND_LIVE", True)):
        return []
    try:
        from . import db as _db
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, solar_power_kw FROM pv_realtime_history
                   WHERE solar_power_kw IS NOT NULL
                     AND captured_at >= datetime('now', '-2 hours')
                   ORDER BY captured_at DESC LIMIT 8"""
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            ts = str(r["captured_at"])
            if not ts.endswith("Z") and "+" not in ts:
                ts = ts + "Z"
            out.append({"timestamp": ts, "generation": float(r["solar_power_kw"])})
        return out
    except Exception:  # pragma: no cover — live anchor is optional sugar
        logger.debug("quartz-open: live-generation read failed", exc_info=True)
        return []


def _fetch_quartz_open_forecast(
    *,
    hours: int,
    base_weather: list[HourlyForecast],
    lat: str | None = None,
    lon: str | None = None,
) -> ForecastFetchResult:
    """Site-level PV forecast via the open Quartz schema (#542).

    Talks ``POST {QUARTZ_OPEN_URL}/forecast/`` — served either by the
    hem-quartz sidecar container or by the hosted open.quartz.solar twin
    (identical schema, no auth). One call per configured panel plane; the
    per-timestamp kW are summed, the 15-min cadence is averaged into
    half-hour slot starts, and the result is merged with Open-Meteo weather
    context exactly like the legacy hosted client so everything downstream
    (calibration chain, snapshots, pv_error_log) is provider-agnostic.
    """
    lat_s = (lat or config.WEATHER_LAT or "").strip()
    lon_s = (lon or config.WEATHER_LON or "").strip()
    if not lat_s or not lon_s:
        return ForecastFetchResult(forecast=[], source="quartz-open")
    base_url = (getattr(config, "QUARTZ_OPEN_URL", "https://open.quartz.solar") or "").rstrip("/")
    timeout = int(getattr(config, "QUARTZ_OPEN_TIMEOUT_SECONDS", 60))
    live = _quartz_open_live_generation()

    # kW summed across planes per raw model timestamp.
    kw_by_ts: dict[datetime, float] = {}
    planes = _quartz_open_planes()
    for plane in planes:
        body = {
            "site": {
                "latitude": float(lat_s),
                "longitude": float(lon_s),
                "capacity_kwp": plane["capacity_kwp"],
                "tilt": plane["tilt"],
                "orientation": plane["orientation"],
            },
        }
        if live:
            body["live_generation"] = live
        req = urllib.request.Request(
            f"{base_url}/forecast/",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            preds = (payload.get("predictions") or {}).get("power_kw") or {}
        except (urllib.error.URLError, json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning(
                "quartz-open fetch failed for plane %s@%s°: %s",
                plane["orientation"], plane["tilt"], e,
            )
            return ForecastFetchResult(forecast=[], source="quartz-open")
        for ts_raw, kw in preds.items():
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            # Model timestamps are naive UTC with per-REQUEST sub-minute
            # offsets (live probe: '09:45:00.533720', different on every
            # call). Floor to the 15-min grid so the planes' keys COLLIDE —
            # without this, plane A and plane B land on disjoint keys and the
            # half-hour bucket averages the planes instead of summing them
            # (half the real forecast for a 2-plane site).
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            ts = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
            try:
                kw_by_ts[ts] = kw_by_ts.get(ts, 0.0) + max(0.0, float(kw))
            except (TypeError, ValueError):
                continue

    if not kw_by_ts:
        return ForecastFetchResult(forecast=[], source="quartz-open")

    # 15-min cadence → half-hour slot starts (mean kW inside each bucket).
    bucket_sum: dict[datetime, float] = {}
    bucket_n: dict[datetime, int] = {}
    for ts, kw in kw_by_ts.items():
        slot = ts.replace(minute=(ts.minute // 30) * 30, second=0)
        bucket_sum[slot] = bucket_sum.get(slot, 0.0) + kw
        bucket_n[slot] = bucket_n.get(slot, 0) + 1

    quartz_rows: list[HourlyForecast] = []
    horizon_end = datetime.now(UTC) + timedelta(hours=max(1, hours))
    for slot in sorted(bucket_sum):
        if slot > horizon_end:
            continue
        site_kw = bucket_sum[slot] / max(1, bucket_n[slot])
        if base_weather:
            temp_c = _interp_hourly_scalar(base_weather, slot, "temperature_c", 10.0)
            cloud_pct = _interp_hourly_scalar(base_weather, slot, "cloud_cover_pct", 50.0)
        else:
            temp_c = 10.0
            cloud_pct = 50.0
        equivalent_rad = site_kw / max(0.001, float(config.PV_CAPACITY_KWP) * float(config.PV_SYSTEM_EFFICIENCY)) * 1000.0
        quartz_rows.append(
            HourlyForecast(
                time_utc=slot,
                temperature_c=temp_c,
                cloud_cover_pct=cloud_pct,
                shortwave_radiation_wm2=max(0.0, equivalent_rad),
                estimated_pv_kw=site_kw,
                heating_demand_factor=compute_heating_demand_factor(temp_c),
                pv_direct=True,
            )
        )

    quartz_by_time = {f.time_utc for f in quartz_rows}
    first_q = min(quartz_by_time)
    last_q = max(quartz_by_time)
    merged = list(quartz_rows)
    for f in base_weather:
        if first_q <= f.time_utc <= last_q:
            continue
        merged.append(f)
    merged.sort(key=lambda f: f.time_utc)
    compact = json.dumps(
        {"planes": planes, "n_points": len(kw_by_ts), "url": base_url},
        separators=(",", ":"),
    )
    return ForecastFetchResult(
        forecast=merged[: max(hours, len(quartz_rows))],
        source="quartz",
        model_name="quartz-open-site",
        model_version=None,
        raw_payload_json=compact,
    )


def _fetch_quartz_forecast(
    *,
    hours: int,
    base_weather: list[HourlyForecast],
) -> ForecastFetchResult:
    """Fetch hosted Quartz PV nowcast and merge it with weather context.

    Hosted Quartz supplies PV generation, not temperature. Until site-level
    Quartz is available here, Open-Meteo remains the temperature/cloud source.
    """
    token = _quartz_token()
    if not token:
        return ForecastFetchResult(forecast=[], source="quartz")

    start = datetime.now(UTC).replace(second=0, microsecond=0)
    end = start + timedelta(hours=max(1, hours))
    gsp_id = (getattr(config, "QUARTZ_GSP_ID", "") or "").strip()
    base_url = getattr(config, "QUARTZ_API_BASE_URL", "https://api.quartz.solar").rstrip("/")
    if gsp_id:
        path = f"/v0/solar/GB/gsp/{urllib.parse.quote(gsp_id)}/forecast"
        params = {
            "start_datetime_utc": start.isoformat().replace("+00:00", "Z"),
            "end_datetime_utc": end.isoformat().replace("+00:00", "Z"),
        }
        model_name = None
    else:
        path = "/v0/solar/GB/national/forecast"
        params = {
            "start_datetime_utc": start.isoformat().replace("+00:00", "Z"),
            "end_datetime_utc": end.isoformat().replace("+00:00", "Z"),
            "include_metadata": "true",
            "model_name": getattr(config, "QUARTZ_MODEL_NAME", "blend"),
            "trend_adjuster_on": str(bool(getattr(config, "QUARTZ_TREND_ADJUSTER_ON", True))).lower(),
        }
        model_name = getattr(config, "QUARTZ_MODEL_NAME", "blend")
    url = f"{base_url}{path}?{urllib.parse.urlencode(params)}"

    def _do_fetch(bearer: str) -> dict[str, Any] | list[Any]:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {bearer}"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    try:
        try:
            payload = _do_fetch(token)
        except urllib.error.HTTPError as http_err:
            # 401 → cached token expired; force a fresh auth and retry once.
            if http_err.code == 401:
                logger.info("Quartz forecast 401; refreshing access token")
                token = _quartz_token(force_refresh=True)
                if not token:
                    return ForecastFetchResult(forecast=[], source="quartz")
                payload = _do_fetch(token)
            else:
                raise
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.warning("Quartz forecast fetch failed: %s", e)
        return ForecastFetchResult(forecast=[], source="quartz")

    values: list[dict[str, Any]]
    model_version = None
    installed_capacity_mw: float | None = None
    if isinstance(payload, dict):
        values = list(payload.get("forecastValues") or [])
        model = payload.get("model") or {}
        model_name = str(model.get("name") or model_name or "quartz")
        model_version = str(model.get("version") or "") or None
        location = payload.get("location") or {}
        try:
            installed_capacity_mw = float(location.get("installedCapacityMw"))
        except (AttributeError, TypeError, ValueError):
            installed_capacity_mw = None
    elif isinstance(payload, list):
        values = list(payload)
    else:
        values = []

    quartz_rows: list[HourlyForecast] = []
    for row in values:
        if not isinstance(row, dict):
            continue
        site_kw = _quartz_site_kw(row, installed_capacity_mw=installed_capacity_mw)
        if site_kw is None:
            continue
        try:
            target = datetime.fromisoformat(str(row["targetTime"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        # Quartz targetTime is the period end. Use the period start so it lines
        # up with our half-hour slot starts.
        slot_time = target - timedelta(minutes=30)
        # Interpolate Open-Meteo's hourly temp/cloud at the (possibly half-hour)
        # Quartz slot. Exact-match lookup misses the :30 slots (and any slot
        # outside the hourly grid) and yields the placeholder 10.0/50.0.
        if base_weather:
            temp_c = _interp_hourly_scalar(base_weather, slot_time, "temperature_c", 10.0)
            cloud_pct = _interp_hourly_scalar(base_weather, slot_time, "cloud_cover_pct", 50.0)
        else:
            temp_c = 10.0
            cloud_pct = 50.0
        equivalent_rad = site_kw / max(0.001, float(config.PV_CAPACITY_KWP) * float(config.PV_SYSTEM_EFFICIENCY)) * 1000.0
        quartz_rows.append(
            HourlyForecast(
                time_utc=slot_time,
                temperature_c=temp_c,
                cloud_cover_pct=cloud_pct,
                shortwave_radiation_wm2=max(0.0, equivalent_rad),
                estimated_pv_kw=site_kw,
                heating_demand_factor=compute_heating_demand_factor(temp_c),
                pv_direct=True,
            )
        )

    if not quartz_rows:
        return ForecastFetchResult(forecast=[], source="quartz", raw_payload_json=json.dumps(payload))

    quartz_by_time = {f.time_utc: f for f in quartz_rows}
    first_q = min(quartz_by_time)
    last_q = max(quartz_by_time)
    merged = list(quartz_rows)
    for f in base_weather:
        if first_q <= f.time_utc <= last_q:
            continue
        merged.append(f)
    merged.sort(key=lambda f: f.time_utc)
    return ForecastFetchResult(
        forecast=merged[: max(hours, len(quartz_rows))],
        source="quartz",
        model_name=model_name or ("quartz-gsp" if gsp_id else "quartz-national"),
        model_version=model_version,
        raw_payload_json=json.dumps(payload, separators=(",", ":")),
    )


def _forecast_delta(
    prev_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    *,
    lookahead_hours: int,
    horizon_start_utc: datetime | None = None,
) -> tuple[float, float]:
    """Compare two forecast snapshots over the next ``lookahead_hours``.

    Returns ``(delta_pv_kwh_total, delta_temp_c_avg)`` summed/averaged across
    slots present in **both** snapshots within the lookahead window. Used by
    the Waze MPC forecast-revision trigger (Epic #73 — story #144) to decide
    whether a re-plan is justified.

    Each ``slot_time`` row covers 1 hour (Open-Meteo hourly), so PV (W/m²)
    converts to kWh via ``estimate_pv_kw(rad) * 1.0 h``. Temp is averaged
    across overlap.
    """
    if not prev_rows or not new_rows:
        return (0.0, 0.0)
    horizon_start = horizon_start_utc or datetime.now(UTC)
    horizon_end = horizon_start + timedelta(hours=lookahead_hours)

    def _index(rows: list[dict[str, Any]]) -> dict[datetime, dict[str, Any]]:
        out: dict[datetime, dict[str, Any]] = {}
        for r in rows:
            st_raw = r.get("slot_time")
            if not st_raw:
                continue
            try:
                st = datetime.fromisoformat(str(st_raw).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if st.tzinfo is None:
                st = st.replace(tzinfo=UTC)
            if horizon_start <= st < horizon_end:
                out[st] = r
        return out

    prev_idx = _index(prev_rows)
    new_idx = _index(new_rows)
    common = sorted(prev_idx.keys() & new_idx.keys())
    if not common:
        return (0.0, 0.0)

    delta_pv_kwh_total = 0.0
    temp_deltas: list[float] = []
    for slot in common:
        p = prev_idx[slot]
        n = new_idx[slot]
        try:
            prev_pv = forecast_pv_kw_from_row(
                slot.hour,
                float(p.get("solar_w_m2") or 0.0),
                p.get("cloud_cover_pct"),
                direct_pv_kw=p.get("direct_pv_kw"),
            )
            new_pv = forecast_pv_kw_from_row(
                slot.hour,
                float(n.get("solar_w_m2") or 0.0),
                n.get("cloud_cover_pct"),
                direct_pv_kw=n.get("direct_pv_kw"),
            )
            delta_pv_kwh_total += abs(new_pv - prev_pv)  # 1h slot → kW * 1h = kWh
        except (ValueError, TypeError):
            pass
        try:
            prev_t = float(p.get("temp_c") or 0.0)
            new_t = float(n.get("temp_c") or 0.0)
            temp_deltas.append(abs(new_t - prev_t))
        except (ValueError, TypeError):
            pass
    delta_temp_c_avg = sum(temp_deltas) / len(temp_deltas) if temp_deltas else 0.0
    return (delta_pv_kwh_total, delta_temp_c_avg)


def get_forecast_for_slot(
    slot_start_utc: datetime,
    forecast: list[HourlyForecast],
) -> HourlyForecast | None:
    """Return the forecast entry closest to (but not after) slot_start_utc."""
    best: HourlyForecast | None = None
    for f in forecast:
        if f.time_utc <= slot_start_utc:
            best = f
        elif best is not None:
            break
    return best


# --------------------------------------------------------------------------
# Half-hour series for PuLP LP (V8)
# --------------------------------------------------------------------------


@dataclass
class WeatherLpSeries:
    """Per half-hour slot inputs aligned to the MILP horizon."""

    slot_starts_utc: list[datetime]
    temperature_outdoor_c: list[float]
    shortwave_radiation_wm2: list[float]
    cloud_cover_pct: list[float]
    pv_kwh_per_slot: list[float]
    cop_space: list[float]
    cop_dhw: list[float]


def _interp_hourly_scalar(
    forecast: list[HourlyForecast],
    slot_start: datetime,
    attr: str,
    default: float,
) -> float:
    """Linear interpolation of an hourly scalar at ``slot_start`` (UTC)."""
    if not forecast:
        return default
    if len(forecast) == 1:
        return float(getattr(forecast[0], attr, default) or default)
    # Find segment [a, b] with a.time <= slot_start < b.time (or extrapolate flat)
    before: HourlyForecast | None = None
    after: HourlyForecast | None = None
    for f in forecast:
        if f.time_utc <= slot_start:
            before = f
        elif after is None and f.time_utc > slot_start:
            after = f
            break
    if before is None:
        v = getattr(forecast[0], attr, default)
        return float(v if v is not None else default)
    if after is None:
        v = getattr(before, attr, default)
        return float(v if v is not None else default)
    va = getattr(before, attr, default)
    vb = getattr(after, attr, default)
    va = float(va if va is not None else default)
    vb = float(vb if vb is not None else default)
    ta = before.time_utc.timestamp()
    tb = after.time_utc.timestamp()
    ts = slot_start.timestamp()
    if tb <= ta:
        return va
    w = (ts - ta) / (tb - ta)
    return va + w * (vb - va)


def compute_pv_calibration_hourly_table(
    window_days: int | None = None,
    min_samples_per_hour: int = 7,
) -> dict[str, Any]:
    """Recompute the per-hour-of-day PV calibration table.

    **PR L1.1 (2026-05-24)** — Re-baselined from Quartz ``direct_pv_kw``
    (the same prediction the LP consumes) instead of
    ``estimate_pv_kw(open_meteo_radiation)``. Open-Meteo's radiation-derived
    PV assumes a south-facing array; the W4 1DZ install is a split array
    (aggregate ~200° SSW) plus a physical west obstruction past ~16:30 UTC.
    The resulting "actual / Open-Meteo prediction" ratios were
    structurally low (0.2-0.5) and would have driven Quartz forecasts
    severely down when applied as multipliers (PR L1 over-correction bug).

    The new baseline is the latest pre-slot Quartz forecast from
    ``meteo_forecast_value.direct_pv_kw``. The ratio becomes
    ``actual / Quartz_raw`` — exactly the correction needed to align
    LP's Quartz-driven plan with reality.

    Hours with fewer than ``min_samples_per_hour`` Quartz samples in the
    window are skipped (low statistical confidence). Returns a status dict.
    Failure modes return ``status='skipped'`` and never raise — LP keeps
    the flat fallback.
    """
    from . import db as _db
    from datetime import date as _date, timedelta as _td

    if window_days is None:
        window_days = int(getattr(config, "PV_CALIBRATION_WINDOW_DAYS", 30))
    end = _date.today()
    start = end - _td(days=window_days)

    # Pull measured PV per UTC hour from pv_realtime_history.
    # mean kW × 1h = kWh per hour (sample-density-independent).
    measured_kw_samples: dict[tuple[str, int], list[float]] = {}
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, solar_power_kw
                   FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) BETWEEN ? AND ?
                     AND solar_power_kw IS NOT NULL""",
                (start.isoformat(), end.isoformat()),
            )
            for row in cur.fetchall():
                ts_raw = row[0]
                kw = float(row[1] or 0.0)
                try:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                day = ts.date().isoformat()
                hour = ts.hour
                measured_kw_samples.setdefault((day, hour), []).append(kw)
        finally:
            conn.close()
    measured_per_day_hour: dict[tuple[str, int], float] = {
        k: (sum(samples) / len(samples)) for k, samples in measured_kw_samples.items() if samples
    }

    if not measured_per_day_hour:
        return {"status": "skipped", "reason": "no pv_realtime_history in window"}

    # PR L1.1 — pull modelled PV from Quartz forecasts (the actual
    # baseline the LP uses). Quartz writes HALF-HOUR slots (slot_time
    # at :00 + :30 per hour, each = avg kW for that 30-min window).
    # For each half-hour, pick the LATEST pre-slot fetch (calibration
    # trains against what the LP saw, not post-hoc revisions). Then
    # aggregate half-hours into hourly kWh for ratio against measured
    # mean-kW-over-hour × 1h.
    modelled_per_half: dict[tuple[str, int, int], float] = {}
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT mv.slot_time, mv.direct_pv_kw, mv.forecast_fetch_at_utc
                   FROM meteo_forecast_value mv
                   WHERE mv.direct_pv_kw IS NOT NULL
                     AND substr(mv.slot_time, 1, 10) BETWEEN ? AND ?
                     AND mv.forecast_fetch_at_utc < mv.slot_time
                   ORDER BY mv.slot_time ASC, mv.forecast_fetch_at_utc ASC""",
                (start.isoformat(), end.isoformat()),
            )
            # Iterate in order; later rows overwrite earlier → latest pre-slot wins
            # per (day, hour, half_of_hour)
            for row in cur.fetchall():
                slot_time_str = row[0]
                direct_pv = float(row[1] or 0.0)
                try:
                    slot_dt = datetime.fromisoformat(str(slot_time_str).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if slot_dt.tzinfo is None:
                    slot_dt = slot_dt.replace(tzinfo=UTC)
                day = slot_dt.date().isoformat()
                hour = slot_dt.hour
                half = 0 if slot_dt.minute < 30 else 1
                modelled_per_half[(day, hour, half)] = direct_pv
        finally:
            conn.close()

    if not modelled_per_half:
        return {
            "status": "skipped",
            "reason": "no Quartz direct_pv_kw in meteo_forecast_value window",
        }

    # Aggregate half-hour kW values to hourly kWh.
    # hourly kWh = sum_over_halves(direct_pv_kw × 0.5h). When only one
    # half is present (rare; usually edge of horizon), we extrapolate
    # by doubling rather than under-counting — keeps the ratio sane.
    modelled_per_day_hour: dict[tuple[str, int], float] = {}
    for (day, hour, half), direct_pv in modelled_per_half.items():
        key = (day, hour)
        modelled_per_day_hour[key] = modelled_per_day_hour.get(key, 0.0) + direct_pv * 0.5
    # For hours with only ONE half-hour present, double up so we approximate
    # the full hour rather than reporting half. This avoids a 2x under-count
    # on hours that lack their second sample.
    halves_count: dict[tuple[str, int], int] = {}
    for (day, hour, _half) in modelled_per_half:
        halves_count[(day, hour)] = halves_count.get((day, hour), 0) + 1
    for key, cnt in halves_count.items():
        if cnt == 1:
            modelled_per_day_hour[key] = modelled_per_day_hour[key] * 2.0

    # Build per-hour (measured, modelled) pairs, skipping dawn/dusk noise.
    pairs_per_hour: dict[int, list[tuple[float, float]]] = {h: [] for h in range(24)}
    for (day, hour), measured_kwh in measured_per_day_hour.items():
        modelled_kwh = modelled_per_day_hour.get((day, hour))
        if modelled_kwh is None or modelled_kwh < 0.05:
            continue
        if measured_kwh < 0.05 and modelled_kwh < 0.05:
            continue
        # Drop egregious outliers (sensor spike or forecast anomaly)
        if measured_kwh / modelled_kwh > 5.0:
            continue
        pairs_per_hour[hour].append((measured_kwh, modelled_kwh))

    factors: dict[int, float] = {}
    samples: dict[int, int] = {}
    for hour, ps in pairs_per_hour.items():
        if len(ps) < min_samples_per_hour:
            continue
        # Ratio-of-sums (magnitude-weighted), not median-of-ratios. Median
        # ignores error magnitude and left a systematic over-forecast at
        # high-generation hours (10 UTC / high sun); Σactual/Σmodelled zeroes
        # the per-hour kWh bias. Clamp [0.10, 2.0]: Quartz can legitimately
        # UNDER-predict PM (split SSW + flat surfaces peak PM vs GSP aggregate).
        sum_m = sum(m for m, _ in ps)
        sum_f = sum(f for _, f in ps)
        factor = (sum_m / sum_f) if sum_f > 0 else 1.0
        factors[hour] = round(max(0.10, min(2.0, factor)), 4)
        samples[hour] = len(ps)

    if not factors:
        return {"status": "skipped", "reason": "insufficient samples per hour"}

    n = _db.upsert_pv_calibration_hourly(factors, samples, window_days)
    return {
        "status": "ok",
        "rows": n,
        "window_days": window_days,
        "hours_calibrated": sorted(factors.keys()),
    }


def compute_today_pv_correction_factor(
    *,
    min_hours: int = 2,
    min_kwh: float = 0.05,
    safety_clamp: tuple[float, float] = (0.30, 2.0),
) -> tuple[float, dict[str, Any]]:
    """OCF-style "today-aware" PV adjuster on top of the per-hour calibration table.

    Compares **today's observed PV** against **today's forecast PV** for the
    daylight hours where we already have data. Returns a multiplicative
    correction factor + diagnostics.

    Use case: cloudy morning vs forecast → factor < 1 → afternoon forecast
    scaled down. Sunny morning → factor > 1 → afternoon scaled up. The
    per-hour calibration table (``pv_calibration_hourly``) captures the
    long-run shape, but is dominated by the rolling-window mix of conditions
    — when today is unusually cloudy or sunny, the table over- or under-
    corrects. This adjuster anchors corrections to TODAY's reality.

    Inspired by Open Climate Fix's Quartz Solar Forecast adjuster pattern
    (https://github.com/openclimatefix/open-source-quartz-solar-forecast).

    Returns ``(1.0, {"reason": "..."})`` when not enough data is available.
    Caller should multiply this factor on top of the per-hour calibration.
    """
    from . import db as _db

    today = datetime.now(UTC).date().isoformat()

    # 1. Pull today's measured PV per UTC hour from pv_realtime_history.
    # Use mean kW × 1h (sample-density-independent), not sum × (5/60).
    # Sparse heartbeat (1-3 samples/hour, not 12) made the old formula collapse
    # the ratio by ~10× — fixed in this commit alongside the per-hour table.
    actual_kw_samples: dict[int, list[float]] = {}
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, solar_power_kw
                   FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) = ?
                     AND solar_power_kw IS NOT NULL""",
                (today,),
            )
            for ts_raw, kw in cur.fetchall():
                try:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                actual_kw_samples.setdefault(ts.hour, []).append(float(kw or 0.0))
        finally:
            conn.close()
    actual_per_hour: dict[int, float] = {
        h: (sum(s) / len(s)) for h, s in actual_kw_samples.items() if s
    }

    if not actual_per_hour:
        return 1.0, {"reason": "no pv_realtime_history yet today"}

    # 2. Pull today's forecast PV per UTC hour (use the latest meteo_forecast row
    #    per slot — this is what the LP saw when it last solved).
    try:
        forecast_rows = _db.get_meteo_forecast(today)
    except Exception:  # noqa: BLE001
        forecast_rows = []
    cal_cloud = _db.get_pv_calibration_hourly_cloud()
    cal_hour = _db.get_pv_calibration_hourly()
    cal_3d = _db.get_pv_calibration_3d()
    flat_cal = compute_pv_calibration_factor() if not cal_cloud and not cal_hour else 1.0
    forecast_per_hour: dict[int, float] = {}
    for r in forecast_rows:
        try:
            ts = datetime.fromisoformat(str(r["slot_time"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts.date().isoformat() != today:
                continue
            forecast_per_hour[ts.hour] = forecast_per_hour.get(ts.hour, 0.0) + forecast_pv_kw_from_row(
                ts.hour,
                float(r.get("solar_w_m2") or 0.0),
                r.get("cloud_cover_pct"),
                direct_pv_kw=r.get("direct_pv_kw"),
                cloud_table=cal_cloud,
                hourly_table=cal_hour,
                flat=flat_cal,
                table_3d=cal_3d,
                slot_utc=ts,
            )
        except (ValueError, TypeError, KeyError):
            continue

    if not forecast_per_hour:
        return 1.0, {"reason": "no meteo_forecast saved for today"}

    # 3. Per-hour ratio (only daylight + meaningful hours)
    ratios: list[tuple[int, float]] = []
    for h in sorted(set(actual_per_hour.keys()) & set(forecast_per_hour.keys())):
        a = actual_per_hour[h]
        f = forecast_per_hour[h]
        if f >= min_kwh and a >= min_kwh:
            ratios.append((h, a / f))

    if len(ratios) < min_hours:
        return 1.0, {
            "reason": f"only {len(ratios)} daylight hour(s) with both actual+forecast data today",
            "min_hours_required": min_hours,
        }

    # 4. Statistical median (mean of two middle values for even-length lists,
    # not ``sorted[len//2]`` which is the *upper* of the two middles).
    sorted_r = sorted(r for _, r in ratios)
    n_r = len(sorted_r)
    if n_r % 2 == 1:
        median = sorted_r[n_r // 2]
    else:
        median = (sorted_r[n_r // 2 - 1] + sorted_r[n_r // 2]) / 2.0
    lo, hi = safety_clamp
    factor = max(lo, min(hi, median))

    return round(factor, 4), {
        "n_hours": len(ratios),
        "median_ratio": round(median, 4),
        "applied_factor": round(factor, 4),
        "clamped": factor != median,
        "ratios_per_hour": {h: round(r, 4) for h, r in ratios},
    }


def compute_today_pv_correction_factor_by_hour(
    *,
    min_hours: int = 2,
    min_kwh: float = 0.05,
    safety_clamp: tuple[float, float] = (0.30, 2.0),
) -> tuple[dict[int, float], dict[str, Any]]:
    """Per-hour today-aware PV adjuster (the per-hour cousin of the scalar version).

    Same data sources as :func:`compute_today_pv_correction_factor` —
    today's measured PV per UTC hour (``pv_realtime_history``) vs today's
    calibrated forecast PV per UTC hour (``meteo_forecast`` x calibration
    chain). The difference: instead of returning **one** scalar applied
    uniformly to every hour of the day, this returns a **dict** mapping
    each future hour to its own correction factor.

    Why we need this:
      - The afternoon-bias diagnosis on 2026-05-06 (14-day window) showed
        Open-Meteo + the per-hour calibration table systematically
        under-forecasts by ~17 % in the afternoon (12-18 UTC) but only
        by ~7 % in the morning (06-12 UTC). The scalar adjuster averages
        these and applies the same correction at every hour, so
        afternoons stay under-forecast even after the adjuster runs.
      - With per-hour ratios, an observation at hour 9 (e.g. actual/cal
        = 1.20) lifts the prediction at hour 9 specifically.

    Imputation policy (2026-05-15 fix):
      - Hours WITHOUT today's observation are set to ``1.0`` (no
        intraday adjustment). The legitimate multi-day deviation pattern
        is already in ``pv_calibration_hourly`` (the ``cal`` term that
        is stacked separately at apply-time). Imputing yesterday's
        factor or today's observed median for unobserved hours pulls a
        SINGLE noisy sample on top of the statistical baseline, which is
        wrong: outliers cascade into the next day's plan. Setting
        ``tf=1.0`` for unobserved hours defers cleanly to the 30-day
        statistical pattern. AM hours that ALREADY happened today (and
        underperformed) still pull AM down via observed; PM hours that
        haven't happened yet trust the long-term per-hour shape, which
        is precisely what we want for a site with structural AM-over /
        PM-under asymmetry.

    Returns ``(factor_by_hour, diag)``:
      - ``factor_by_hour`` covers all 24 UTC hours, populated for hours
        that had both forecast + actual today (clamped per-hour) and
        ``1.0`` for the rest. Returns ``{}`` when not enough data exists.
      - ``diag`` includes ``observed_hours``, ``imputed_hours``,
        ``median_ratio``, ``ratios_per_hour``, ``n_observed``,
        ``clamped_hours``.
    """
    from . import db as _db

    today = datetime.now(UTC).date().isoformat()

    # Reuse the same data-pull path as the scalar function so behaviour
    # stays consistent.
    actual_kw_samples: dict[int, list[float]] = {}
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, solar_power_kw
                   FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) = ?
                     AND solar_power_kw IS NOT NULL""",
                (today,),
            )
            for ts_raw, kw in cur.fetchall():
                try:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                actual_kw_samples.setdefault(ts.hour, []).append(float(kw or 0.0))
        finally:
            conn.close()
    actual_per_hour: dict[int, float] = {
        h: (sum(s) / len(s)) for h, s in actual_kw_samples.items() if s
    }
    if not actual_per_hour:
        return {}, {"reason": "no pv_realtime_history yet today"}

    try:
        forecast_rows = _db.get_meteo_forecast(today)
    except Exception:  # noqa: BLE001
        forecast_rows = []
    cal_cloud = _db.get_pv_calibration_hourly_cloud()
    cal_hour = _db.get_pv_calibration_hourly()
    cal_3d = _db.get_pv_calibration_3d()
    flat_cal = compute_pv_calibration_factor() if not cal_cloud and not cal_hour else 1.0
    forecast_per_hour: dict[int, float] = {}
    for r in forecast_rows:
        try:
            ts = datetime.fromisoformat(str(r["slot_time"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts.date().isoformat() != today:
                continue
            forecast_per_hour[ts.hour] = forecast_per_hour.get(ts.hour, 0.0) + forecast_pv_kw_from_row(
                ts.hour,
                float(r.get("solar_w_m2") or 0.0),
                r.get("cloud_cover_pct"),
                direct_pv_kw=r.get("direct_pv_kw"),
                cloud_table=cal_cloud,
                hourly_table=cal_hour,
                flat=flat_cal,
                table_3d=cal_3d,
                slot_utc=ts,
            )
        except (ValueError, TypeError, KeyError):
            continue
    if not forecast_per_hour:
        return {}, {"reason": "no meteo_forecast saved for today"}

    lo, hi = safety_clamp
    observed: dict[int, float] = {}
    clamped_hours: list[int] = []
    for h in sorted(set(actual_per_hour) & set(forecast_per_hour)):
        a = actual_per_hour[h]
        f = forecast_per_hour[h]
        if f < min_kwh and a < min_kwh:
            continue
        if f < min_kwh:
            # Forecast was effectively zero but actual was non-trivial:
            # the ratio explodes, so cap to the upper clamp directly.
            observed[h] = hi
            clamped_hours.append(h)
            continue
        ratio = a / f
        clamped = max(lo, min(hi, ratio))
        observed[h] = clamped
        if clamped != ratio:
            clamped_hours.append(h)

    if len(observed) < min_hours:
        return {}, {
            "reason": f"only {len(observed)} hour(s) with both actual+forecast data today",
            "min_hours_required": min_hours,
        }

    # Compute median for the unobserved-hour fallback. Two corrections vs the
    # original implementation:
    #   1. Exclude hours that were *clamped* — by construction they're at the
    #      ``[lo, hi]`` boundary because either actual or forecast was tiny;
    #      the ratio is unreliable as a representative value for the rest of
    #      the day. Including them pulls the median toward the clamp bounds
    #      and biases the projection wrongly. Concrete repro on 2026-05-06:
    #      observed = {5: 2.0(clamp), 6: 0.67, 7: 1.10, 9: 0.30(clamp)} →
    #      old median = 1.10, true unclamped median = 0.89. The 1.10 made
    #      the LP scale UP an already-too-optimistic forecast on a heavy-
    #      cloud day.
    #   2. ``sorted[len//2]`` is the *upper* of two middle values for even-
    #      length lists, not the statistical median. Use the mean of the
    #      two middle values for proper even-length median.
    unclamped = [r for h, r in observed.items() if h not in clamped_hours]
    median_source = unclamped if len(unclamped) >= max(2, min_hours) else list(observed.values())
    sorted_obs = sorted(median_source)
    n_obs = len(sorted_obs)
    if n_obs % 2 == 1:
        median = sorted_obs[n_obs // 2]
    else:
        median = (sorted_obs[n_obs // 2 - 1] + sorted_obs[n_obs // 2]) / 2.0

    # Imputation policy: unobserved hours get factor 1.0 (no intraday
    # adjustment), NOT yesterday's value or today's observed median.
    # The 30-day per-hour deviation pattern is in ``pv_calibration_hourly``
    # (the ``cal`` term in the LP's _pv_scale_callable). Stacking a
    # single-day noisy sample on top would propagate outliers — see
    # 2026-05-15 incident follow-up. Observed hours still pull today's
    # weather in; unobserved hours defer cleanly to the multi-day baseline.
    factor_by_hour: dict[int, float] = {h: 1.0 for h in range(24)}
    imputed_hours: list[int] = []
    for h in range(24):
        if h in observed:
            factor_by_hour[h] = observed[h]
        else:
            # factor_by_hour[h] stays at the init value 1.0 — no override.
            imputed_hours.append(h)

    return factor_by_hour, {
        "n_observed": len(observed),
        "n_imputed": len(imputed_hours),
        "median_ratio": round(median, 4),
        "median_source": "unclamped_only" if median_source is unclamped else "all_observed",
        "imputation_policy": "neutral_1.0",  # was "median_of_observed" pre 2026-05-15
        "ratios_per_hour": {h: round(v, 4) for h, v in observed.items()},
        "imputed_hours": imputed_hours,
        "clamped_hours": clamped_hours,
    }


def compute_pv_recent_bias_by_hour() -> tuple[dict[int, float], dict[int, float], dict[int, int], dict[str, Any]]:
    """Adaptive closed-loop PV bias corrector (#486).

    Per UTC hour, the **recency-weighted mean** of ``actual/forecast`` from
    ``pv_error_log`` — i.e. how wrong the COMMITTED forecast was, weighted so
    recent days dominate (half-life decay). Returns
    ``(applied_factors, raw_ratios, samples, diag)``:

    * ``raw_ratios[h]`` — the measured weighted actual/forecast (the RESIDUAL
      error of the already-corrected forecast).
    * ``applied_factors[h]`` — the PREVIOUS factor accumulated by the residual:
      ``old × (1 + damping·(ratio−1))``, clamped to ``[MIN, MAX]``. Ramps to
      full correction over a few refreshes and self-stabilises on over-shoot;
      the loop's fixed point is ratio ≈ 1 (zero residual error).

    Because it's keyed on realised error, genuine morning shade (actual low →
    ratio ≈ 1) is left alone while systematic under-forecast (ratio > 1) is
    corrected. Slots below ``PV_RECENT_BIAS_MIN_KWH`` on either side are dropped.
    """
    from . import db as _db
    window = int(getattr(config, "PV_RECENT_BIAS_WINDOW_DAYS", 14))
    half_life = float(getattr(config, "PV_RECENT_BIAS_HALFLIFE_DAYS", 5))
    damping = float(getattr(config, "PV_RECENT_BIAS_DAMPING", 0.5))
    lo = float(getattr(config, "PV_RECENT_BIAS_MIN", 0.4))
    hi = float(getattr(config, "PV_RECENT_BIAS_MAX", 2.5))
    min_kwh = float(getattr(config, "PV_RECENT_BIAS_MIN_KWH", 0.05))
    # Accumulate on the previous factor → the loop ramps to FULL correction
    # (measuring residual error against the already-corrected forecast).
    try:
        prev = _db.get_pv_recent_bias()
    except Exception:  # pragma: no cover — cold start / missing table
        prev = {}

    now = datetime.now(UTC)
    start = now - timedelta(days=window)
    rows = _db.get_pv_error_log_range(
        start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    acc: dict[int, list[float]] = {}  # hour -> [sum_w_ratio, sum_w, n]
    for r in rows:
        f = r.get("forecast_kwh") or 0.0
        a = r.get("actual_kwh")
        if a is None or f < min_kwh or a < min_kwh:
            continue
        try:
            ts = datetime.fromisoformat(str(r["slot_time_utc"]).replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            continue
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        w = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
        d = acc.setdefault(ts.hour, [0.0, 0.0, 0.0])
        d[0] += w * (a / f)
        d[1] += w
        d[2] += 1

    factors: dict[int, float] = {}
    raw: dict[int, float] = {}
    samples: dict[int, int] = {}
    for h, (sw, w, n) in acc.items():
        if w <= 0 or n < 2:
            continue
        ratio = sw / w
        if h in prev:
            # Have a prior factor → damped accumulation (stable ongoing tracking,
            # avoids overshoot on a noisy new day).
            applied = float(prev[h]) * (1.0 + damping * (ratio - 1.0))
        else:
            # WARM START (#486 Q2): we already have the historical error — jump
            # straight to the measured correction instead of crawling from 1.0
            # over days. The window mean is itself a smoothed estimate.
            applied = ratio
        applied = max(lo, min(hi, applied))
        raw[h] = round(ratio, 4)
        factors[h] = round(applied, 4)
        samples[h] = int(n)
    diag = {"window_days": window, "half_life_days": half_life, "damping": damping,
            "min_kwh": min_kwh, "clamp": [lo, hi], "n_hours": len(factors)}
    return factors, raw, samples, diag


def refresh_pv_recent_bias() -> int:
    """Recompute + persist the adaptive PV bias table (cron after error rebuild)."""
    from . import db as _db
    factors, raw, samples, diag = compute_pv_recent_bias_by_hour()
    if not factors:
        logger.info("pv_recent_bias: nothing to compute (%s)", diag)
        return 0
    ca = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = _db.upsert_pv_recent_bias(factors, raw, samples, ca)
    logger.info(
        "pv_recent_bias refreshed: %d hours; applied=%s raw=%s",
        n, {h: factors[h] for h in sorted(factors)}, {h: raw[h] for h in sorted(raw)},
    )
    return n


def compute_solar_elevation_deg(
    slot_utc: datetime,
    lat: float | None = None,
    lon: float | None = None,
) -> float:
    """Solar elevation angle (degrees above horizon) at ``slot_utc``.

    PR L3 (2026-05-24) — used by the 3D calibration table to separate
    same-UTC-hour samples by sun position (winter low / summer high).
    The W4 1DZ install has fundamentally different physics when the sun
    is at 10° vs 50° elevation — same hour, different physics, different
    obstruction-shadow geometry → different correction factor.

    Defaults to ``config.WEATHER_LAT`` / ``WEATHER_LON`` when omitted.

    Raises ImportError if ``astral`` is unavailable — the caller should
    handle it explicitly (we DON'T silently return 0.0, because that
    would collapse every slot into bucket 0 and silently poison the
    calibration table).
    """
    if lat is None:
        try:
            lat = float(config.WEATHER_LAT or "0")
        except (TypeError, ValueError):
            return 0.0
    if lon is None:
        try:
            lon = float(config.WEATHER_LON or "0")
        except (TypeError, ValueError):
            return 0.0
    from astral import Observer  # noqa: imported for the side effect of failing loudly
    from astral.sun import elevation
    try:
        obs = Observer(latitude=lat, longitude=lon)
        return float(elevation(observer=obs, dateandtime=slot_utc))
    except (ValueError, TypeError):
        return 0.0


def elevation_bucket(elev_deg: float) -> int:
    """Map solar elevation (degrees) to a calibration bucket.

    Buckets chosen to separate the physically-distinct PV regimes:
        0 = very-low  (<10°)   night-edge, dawn/dusk; PV negligible
        1 = low       (10-25°) winter midday, summer dawn/dusk
        2 = mid       (25-40°) spring/autumn midday, shoulder
        3 = high      (40-55°) summer midday outside solstice
        4 = very-high (>55°)   peak summer solstice noon

    A 5-bucket split keeps the cells dense enough to populate from
    30 days of data while still separating the worst-bias regimes
    (low elevation = obstruction shadow + sub-optimal angle-of-incidence
    on the W4 1DZ split array).
    """
    if elev_deg < 10.0:
        return 0
    if elev_deg < 25.0:
        return 1
    if elev_deg < 40.0:
        return 2
    if elev_deg < 55.0:
        return 3
    return 4


def cloud_bucket(cloud_cover_pct: float | None) -> int:
    """Map a cloud-cover % (0-100) to the calibration bucket index.

    Convention (matches db.upsert_pv_calibration_hourly_cloud + the schema):
        0 = clear     (0-25%)
        1 = partly    (25-50%)
        2 = mostly    (50-75%)
        3 = overcast  (75-100%)

    None or out-of-range values default to bucket 1 (partly) — middle of
    the distribution, neutral choice.
    """
    if cloud_cover_pct is None:
        return 1
    p = float(cloud_cover_pct)
    if p < 25.0:
        return 0
    if p < 50.0:
        return 1
    if p < 75.0:
        return 2
    return 3


def get_pv_calibration_factor_for(
    hour_utc: int,
    cloud_cover_pct: float | None,
    *,
    cloud_table: dict[tuple[int, int], float] | None = None,
    hourly_table: dict[int, float] | None = None,
    flat: float = 1.0,
    table_3d: dict[tuple[int, int, int], float] | None = None,
    slot_utc: datetime | None = None,
) -> float:
    """Resolve calibration factor with the 3d → 2d → 1d → flat fallback chain.

    Lookup priority (PR L3 extends with 3D):
        1. ``pv_calibration_3d[(hour, cloud_bucket, elev_bucket)]`` — full 3D
        2. ``pv_calibration_hourly_cloud[(hour, cloud_bucket)]``    — 2D
        3. ``pv_calibration_hourly[hour]``                           — 1D
        4. ``flat`` (caller's flat fallback)

    The 3D lookup requires ``slot_utc`` to compute solar elevation. When
    omitted (back-compat with callers that don't have slot_utc, e.g.
    aggregation helpers), the 3D layer is skipped and we fall through
    to the 2D lookup.

    Pass pre-fetched tables to avoid hitting the DB inside per-slot loops.
    """
    import sqlite3

    from . import db as _db

    # Tolerate missing DB tables (cold-start, pure-function tests).
    if table_3d is None and slot_utc is not None:
        try:
            table_3d = _db.get_pv_calibration_3d()
        except sqlite3.OperationalError:
            table_3d = {}
    if cloud_table is None:
        try:
            cloud_table = _db.get_pv_calibration_hourly_cloud()
        except sqlite3.OperationalError:
            cloud_table = {}
    if hourly_table is None:
        try:
            hourly_table = _db.get_pv_calibration_hourly()
        except sqlite3.OperationalError:
            hourly_table = {}

    bucket = cloud_bucket(cloud_cover_pct)

    # 3D — only attempted when slot_utc is available so we can compute elevation
    if table_3d and slot_utc is not None:
        # PR L3 H2 fix — normalize lookup timestamp to mid-hour so it
        # matches what compute_pv_calibration_3d_table used during training.
        # Without this, :00 slots would compute elevation 30 min earlier
        # than the training reference and could land in a different bucket
        # at dawn/dusk transitions → miss the populated cell.
        midhour_dt = slot_utc.replace(minute=30, second=0, microsecond=0)
        # PR L3 H5 fix — catch ImportError here (astral may be missing in
        # minimal-install envs) so we degrade cleanly to the 2D path rather
        # than crashing every LP solve when ``pv_calibration_3d`` is also
        # populated. astral is in requirements.txt — this is defence in
        # depth for partial deploys.
        try:
            elev = compute_solar_elevation_deg(midhour_dt)
            elev_b = elevation_bucket(elev)
            f = table_3d.get((hour_utc, bucket, elev_b))
            if f is not None:
                return float(f)
        except ImportError:
            global _ASTRAL_WARNED
            if not _ASTRAL_WARNED:
                import logging
                logging.getLogger(__name__).warning(
                    "astral not installed — 3D calibration disabled; "
                    "falling back to 2D cloud table"
                )
                _ASTRAL_WARNED = True

    if cloud_table:
        f = cloud_table.get((hour_utc, bucket))
        if f is not None:
            return float(f)
    if hourly_table:
        f = hourly_table.get(hour_utc)
        if f is not None:
            return float(f)
    return float(flat)


def compute_pv_calibration_hourly_cloud_table(
    window_days: int | None = None,
    min_samples_per_cell: int = 4,
) -> dict[str, Any]:
    """Recompute the per-(hour, cloud-bucket) calibration table.

    For each historical (day, hour) we have:
      * measured kWh/h = mean of pv_realtime_history.solar_power_kw samples (PR #230 fix)
      * forecast kWh/h = estimate_pv_kw(meteo_forecast.solar_w_m2 at that hour)
      * cloud_pct      = meteo_forecast.cloud_cover_pct at that hour
      * ratio          = measured / forecast

    Group by (hour_of_day, cloud_bucket); take the median ratio per cell.
    Cells with < ``min_samples_per_cell`` samples are skipped (low confidence).

    Buckets that don't appear in the result fall back to the per-hour table
    via :func:`get_pv_calibration_factor_for` at apply time.
    """
    from . import db as _db
    from collections import defaultdict
    from datetime import UTC as _UTC, date as _date, datetime as _dt, timedelta as _td

    if window_days is None:
        window_days = int(getattr(config, "PV_CALIBRATION_WINDOW_DAYS", 30))
    end = _date.today()
    start = end - _td(days=window_days)

    # 1. Pull measurements (mean kW per hour, sparse-fix from PR #230)
    actual_kw_samples: dict[tuple[str, int], list[float]] = defaultdict(list)
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, solar_power_kw FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) BETWEEN ? AND ?
                     AND solar_power_kw IS NOT NULL""",
                (start.isoformat(), end.isoformat()),
            )
            for ts_raw, kw in cur.fetchall():
                try:
                    ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_UTC)
                actual_kw_samples[(ts.date().isoformat(), ts.hour)].append(float(kw or 0.0))
        finally:
            conn.close()

    measured_per_day_hour: dict[tuple[str, int], float] = {
        k: sum(s) / len(s) for k, s in actual_kw_samples.items() if s
    }

    # 2. PR L1.1 — pull Quartz half-hour forecasts (with Open-Meteo
    # cloud_cover for bucketing) from local snapshot store. Latest
    # pre-slot fetch wins per half-hour, then aggregate to hourly kWh
    # (matching the measured side's mean kW × 1h units).
    archive_per_half: dict[tuple[str, int, int], tuple[float, float | None]] = {}
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT mv.slot_time, mv.direct_pv_kw, mv.cloud_cover_pct,
                          mv.forecast_fetch_at_utc
                   FROM meteo_forecast_value mv
                   WHERE mv.direct_pv_kw IS NOT NULL
                     AND substr(mv.slot_time, 1, 10) BETWEEN ? AND ?
                     AND mv.forecast_fetch_at_utc < mv.slot_time
                   ORDER BY mv.slot_time ASC, mv.forecast_fetch_at_utc ASC""",
                (start.isoformat(), end.isoformat()),
            )
            for row in cur.fetchall():
                slot_time_str, direct_pv, cloud_pct, _fetch_at = row
                try:
                    slot_dt = _dt.fromisoformat(str(slot_time_str).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if slot_dt.tzinfo is None:
                    slot_dt = slot_dt.replace(tzinfo=_UTC)
                day = slot_dt.date().isoformat()
                hour = slot_dt.hour
                half = 0 if slot_dt.minute < 30 else 1
                cloud_f = float(cloud_pct) if cloud_pct is not None else None
                archive_per_half[(day, hour, half)] = (float(direct_pv or 0.0), cloud_f)
        finally:
            conn.close()

    if not archive_per_half:
        return {
            "status": "skipped",
            "reason": "no Quartz direct_pv_kw in meteo_forecast_value window",
        }

    # Aggregate half-hour kW into hourly kWh; pick the dominant cloud
    # bucket per hour (when halves differ in cloud cover, the average is
    # close enough — clouds drift slowly). Single-half-only hours
    # extrapolate by ×2.
    archive: dict[tuple[str, int], tuple[float, float | None]] = {}
    halves_seen: dict[tuple[str, int], list[tuple[float, float | None]]] = defaultdict(list)
    for (day, hour, _half), data in archive_per_half.items():
        halves_seen[(day, hour)].append(data)
    for key, datas in halves_seen.items():
        total_kwh = sum(pv * 0.5 for pv, _c in datas)
        if len(datas) == 1:
            total_kwh *= 2.0
        # Average cloud over the halves (ignore Nones).
        clouds = [c for _pv, c in datas if c is not None]
        avg_cloud = (sum(clouds) / len(clouds)) if clouds else None
        archive[key] = (total_kwh, avg_cloud)

    # 3. Build per-(hour, bucket) ratio lists.
    # measured_per_day_hour value is mean kW over hour → numerically = kWh
    # for that hour. archive value is hourly kWh from above aggregation.
    pairs_per_cell: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for (day, hour), measured_kwh in measured_per_day_hour.items():
        forecast = archive.get((day, hour))
        if forecast is None:
            continue
        modelled_kwh, cloud = forecast
        if modelled_kwh < 0.05 or measured_kwh < 0.05:
            continue                          # dawn/dusk noise
        if measured_kwh / modelled_kwh > 5.0:
            continue                          # outlier
        bucket = cloud_bucket(cloud)
        pairs_per_cell[(hour, bucket)].append((measured_kwh, modelled_kwh))

    # 4. Ratio-of-sums per cell (magnitude-weighted), clamp [0.05, 2.0].
    # Σactual/Σmodelled zeroes the per-cell kWh bias; median-of-ratios left a
    # high-generation over-forecast. Upper bound 2.0: Quartz can legitimately
    # under-predict PM (split SW-pitched + flat-rack array peaks PM vs GSP).
    factors: dict[tuple[int, int], float] = {}
    samples: dict[tuple[int, int], int] = {}
    for cell, ps in pairs_per_cell.items():
        if len(ps) < min_samples_per_cell:
            continue
        sum_m = sum(m for m, _ in ps)
        sum_f = sum(f for _, f in ps)
        factor = (sum_m / sum_f) if sum_f > 0 else 1.0
        factors[cell] = round(max(0.05, min(2.0, factor)), 4)
        samples[cell] = len(ps)

    if not factors:
        return {"status": "skipped", "reason": "insufficient samples per (hour, bucket)"}

    n = _db.upsert_pv_calibration_hourly_cloud(factors, samples, window_days)
    return {
        "status": "ok",
        "rows": n,
        "window_days": window_days,
        "cells_calibrated": sorted(factors.keys()),
    }


def compute_pv_calibration_3d_table(
    window_days: int | None = None,
    min_samples_per_cell: int = 3,
) -> dict[str, Any]:
    """PR L3 (2026-05-24) — 3D calibration table:
    (hour_utc, cloud_bucket, elevation_bucket) → factor.

    Same input as the 2D cloud-aware version (Quartz forecast vs actual
    PV from ``pv_realtime_history``) but adds solar elevation as a 3rd
    binning dimension. Separates winter-low-sun from summer-high-sun at
    the same UTC hour — fundamentally different physics for an east-
    facing obstructed array.

    Sample density: 5 elevation buckets × 4 cloud buckets × 14 daylight
    hours = up to 280 cells. With 30 days of data, each cell averages
    only a handful of samples → ``min_samples_per_cell=3`` is set lower
    than the 2D table's 4 to keep coverage acceptable; sparse cells
    fall through to the 2D table via the lookup chain.
    """
    from collections import defaultdict
    from datetime import UTC as _UTC
    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from . import db as _db

    if window_days is None:
        window_days = int(getattr(config, "PV_CALIBRATION_WINDOW_DAYS", 30))
    end = _date.today()
    start = end - _td(days=window_days)

    # 1. Actual PV per (day, hour) — same pattern as 2D compute
    actual_kw_samples: dict[tuple[str, int], list[float]] = defaultdict(list)
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, solar_power_kw FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) BETWEEN ? AND ?
                     AND solar_power_kw IS NOT NULL""",
                (start.isoformat(), end.isoformat()),
            )
            for ts_raw, kw in cur.fetchall():
                try:
                    ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_UTC)
                actual_kw_samples[(ts.date().isoformat(), ts.hour)].append(float(kw or 0.0))
        finally:
            conn.close()
    measured_per_day_hour: dict[tuple[str, int], float] = {
        k: sum(s) / len(s) for k, s in actual_kw_samples.items() if s
    }
    if not measured_per_day_hour:
        return {"status": "skipped", "reason": "no pv_realtime_history in window"}

    # 2. Quartz forecast per (day, hour, half) + cloud + elevation
    archive_per_half: dict[tuple[str, int, int], tuple[float, float | None, _dt]] = {}
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT mv.slot_time, mv.direct_pv_kw, mv.cloud_cover_pct
                   FROM meteo_forecast_value mv
                   WHERE mv.direct_pv_kw IS NOT NULL
                     AND substr(mv.slot_time, 1, 10) BETWEEN ? AND ?
                     AND mv.forecast_fetch_at_utc < mv.slot_time
                   ORDER BY mv.slot_time ASC, mv.forecast_fetch_at_utc ASC""",
                (start.isoformat(), end.isoformat()),
            )
            for row in cur.fetchall():
                slot_time_str, direct_pv, cloud_pct = row
                try:
                    slot_dt = _dt.fromisoformat(str(slot_time_str).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if slot_dt.tzinfo is None:
                    slot_dt = slot_dt.replace(tzinfo=_UTC)
                day = slot_dt.date().isoformat()
                hour = slot_dt.hour
                half = 0 if slot_dt.minute < 30 else 1
                cloud_f = float(cloud_pct) if cloud_pct is not None else None
                archive_per_half[(day, hour, half)] = (
                    float(direct_pv or 0.0), cloud_f, slot_dt,
                )
        finally:
            conn.close()

    if not archive_per_half:
        return {
            "status": "skipped",
            "reason": "no Quartz direct_pv_kw in meteo_forecast_value window",
        }

    # 3. Aggregate half-hour to hourly kWh + bucketize cloud + elevation.
    # Solar elevation taken from the MID-HOUR moment (slot_time + 30 min)
    # so it represents the average elevation across the hour.
    halves_seen: dict[tuple[str, int], list[tuple[float, float | None]]] = defaultdict(list)
    midhour_dts: dict[tuple[str, int], _dt] = {}
    for (day, hour, _half), (direct_pv, cloud, slot_dt) in archive_per_half.items():
        halves_seen[(day, hour)].append((direct_pv, cloud))
        # Mid-hour timestamp (used for elevation calc)
        midhour_dts[(day, hour)] = _dt(
            slot_dt.year, slot_dt.month, slot_dt.day, hour, 30, tzinfo=_UTC,
        )

    archive: dict[tuple[str, int], tuple[float, float | None, _dt]] = {}
    for key, datas in halves_seen.items():
        total_kwh = sum(pv * 0.5 for pv, _c in datas)
        if len(datas) == 1:
            total_kwh *= 2.0
        clouds = [c for _pv, c in datas if c is not None]
        avg_cloud = (sum(clouds) / len(clouds)) if clouds else None
        archive[key] = (total_kwh, avg_cloud, midhour_dts[key])

    # 4. Build per-cell (measured, modelled) pairs (hour, cloud_b, elev_b)
    pairs_per_cell: dict[tuple[int, int, int], list[tuple[float, float]]] = defaultdict(list)
    for (day, hour), measured_kwh in measured_per_day_hour.items():
        forecast = archive.get((day, hour))
        if forecast is None:
            continue
        modelled_kwh, cloud, midhour = forecast
        if modelled_kwh < 0.05 or measured_kwh < 0.05:
            continue
        if measured_kwh / modelled_kwh > 5.0:
            continue
        cloud_b = cloud_bucket(cloud)
        elev = compute_solar_elevation_deg(midhour)
        elev_b = elevation_bucket(elev)
        pairs_per_cell[(hour, cloud_b, elev_b)].append((measured_kwh, modelled_kwh))

    # 5. Ratio-of-sums per cell (magnitude-weighted), clamp [0.05, 2.0].
    # Σactual/Σmodelled zeroes the per-cell kWh bias — median-of-ratios left a
    # systematic over-forecast at high-generation (10 UTC / high-elevation) cells.
    factors: dict[tuple[int, int, int], float] = {}
    samples: dict[tuple[int, int, int], int] = {}
    for cell, ps in pairs_per_cell.items():
        if len(ps) < min_samples_per_cell:
            continue
        sum_m = sum(m for m, _ in ps)
        sum_f = sum(f for _, f in ps)
        factor = (sum_m / sum_f) if sum_f > 0 else 1.0
        factors[cell] = round(max(0.05, min(2.0, factor)), 4)
        samples[cell] = len(ps)

    if not factors:
        return {
            "status": "skipped",
            "reason": "insufficient samples per (hour, cloud, elevation) cell",
        }

    n = _db.upsert_pv_calibration_3d(factors, samples, window_days)
    return {
        "status": "ok",
        "rows": n,
        "window_days": window_days,
        "cells_calibrated": len(factors),
    }


def evaluate_pv_forecast_accuracy(
    window_days: int = 30,
    *,
    min_kw: float = 0.05,
) -> dict[str, Any]:
    """Compare predicted vs realized PV across the last N days.

    Used as a **regression baseline** for any forecast-accuracy work. Output
    captures MAE / RMSE / bias / sample count, both per-hour-of-day and
    overall. After shipping a calibration improvement, re-run and compare:
    a real improvement reduces MAE/RMSE; "improvement" that doesn't
    measurably move these metrics is noise.

    Pipeline replicated for each (day, hour):
        prediction = estimate_pv_kw(forecast_irradiance) × calibration[hour]
        actual = mean(pv_realtime_history.solar_power_kw for that hour)
        error = actual - prediction (positive = under-prediction)

    Hours/days where prediction OR actual < ``min_kw`` are excluded as
    dawn/dusk noise (default 0.05 kW).

    The today-aware adjuster is NOT applied for historical days — we use the
    current per-hour table only. This gives "what would today's calibration
    have predicted historically" — apples-to-apples vs any future
    calibration change.

    Returns a dict with:

        {
            "window_days": 30,
            "n_paired": 245,
            "overall": {
                "mae_kw": 0.42, "rmse_kw": 0.61,
                "bias_kw": -0.08,    # negative = system over-predicts
                "mean_actual_kw": 0.78, "mean_pred_kw": 0.86,
                "mape_pct": 38.4,
            },
            "per_hour": {
                7:  {"mae_kw": 0.21, "rmse_kw": 0.32, "bias_kw": +0.04, "n": 18},
                ...
            },
        }
    """
    from . import db as _db
    from collections import defaultdict
    from datetime import UTC as _UTC, date as _date, datetime as _dt, timedelta as _td

    end = _date.today()
    start = end - _td(days=window_days)

    cal_hourly = _db.get_pv_calibration_hourly()
    cal_cloud = _db.get_pv_calibration_hourly_cloud()
    cal_3d = _db.get_pv_calibration_3d()
    flat_cal = compute_pv_calibration_factor() if not cal_hourly and not cal_cloud else 1.0

    # Pull all measurements in window.
    actual_kw_samples: dict[tuple[str, int], list[float]] = defaultdict(list)
    forecast_radiation: dict[tuple[str, int], float] = {}
    forecast_cloud: dict[tuple[str, int], float | None] = {}
    forecast_direct_pv: dict[tuple[str, int], float | None] = {}

    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, solar_power_kw FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) BETWEEN ? AND ?
                     AND solar_power_kw IS NOT NULL""",
                (start.isoformat(), end.isoformat()),
            )
            for ts_raw, kw in cur.fetchall():
                try:
                    ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_UTC)
                actual_kw_samples[(ts.date().isoformat(), ts.hour)].append(float(kw or 0.0))
        finally:
            conn.close()

    current_day = start
    while current_day <= end:
        try:
            forecast_rows = _db.get_meteo_forecast_for_slot_date(current_day.isoformat())
        except Exception:  # noqa: BLE001
            forecast_rows = []
        for r in forecast_rows:
            try:
                ts = _dt.fromisoformat(str(r["slot_time"]).replace("Z", "+00:00"))
            except (ValueError, TypeError, KeyError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_UTC)
            key = (ts.date().isoformat(), ts.hour)
            forecast_radiation[key] = float(r.get("solar_w_m2") or 0.0)
            forecast_cloud[key] = float(r.get("cloud_cover_pct")) if r.get("cloud_cover_pct") is not None else None
            forecast_direct_pv[key] = (
                float(r.get("direct_pv_kw")) if r.get("direct_pv_kw") is not None else None
            )
        current_day += _td(days=1)

    # Build paired (predicted_kw, actual_kw) tuples
    pairs_per_hour: dict[int, list[tuple[float, float]]] = defaultdict(list)
    all_pairs: list[tuple[float, float]] = []
    for key, samples in actual_kw_samples.items():
        if not samples:
            continue
        actual_kw = sum(samples) / len(samples)
        rad = forecast_radiation.get(key)
        if rad is None:
            continue
        try:
            slot_utc_key = _dt.fromisoformat(key[0]).replace(
                hour=key[1], tzinfo=_UTC,
            )
        except (ValueError, TypeError):
            slot_utc_key = None
        predicted_kw = forecast_pv_kw_from_row(
            key[1],
            rad,
            forecast_cloud.get(key),
            direct_pv_kw=forecast_direct_pv.get(key),
            cloud_table=cal_cloud,
            hourly_table=cal_hourly,
            flat=flat_cal,
            table_3d=cal_3d,
            slot_utc=slot_utc_key,
        )
        if predicted_kw < min_kw and actual_kw < min_kw:
            continue                      # both negligible — skip dawn/dusk
        pairs_per_hour[key[1]].append((predicted_kw, actual_kw))
        all_pairs.append((predicted_kw, actual_kw))

    def _stats(pairs: list[tuple[float, float]]) -> dict[str, float]:
        if not pairs:
            return {"mae_kw": 0.0, "rmse_kw": 0.0, "bias_kw": 0.0,
                    "mean_actual_kw": 0.0, "mean_pred_kw": 0.0, "mape_pct": 0.0, "n": 0}
        n = len(pairs)
        errs = [a - p for p, a in pairs]
        mae = sum(abs(e) for e in errs) / n
        rmse = (sum(e * e for e in errs) / n) ** 0.5
        bias = sum(errs) / n
        mean_actual = sum(a for _, a in pairs) / n
        mean_pred = sum(p for p, _ in pairs) / n
        # MAPE: skip points where actual ~ 0 to avoid div-by-zero
        ape = [abs(a - p) / a * 100 for p, a in pairs if a > 0.1]
        mape = sum(ape) / len(ape) if ape else 0.0
        return {
            "mae_kw": round(mae, 4),
            "rmse_kw": round(rmse, 4),
            "bias_kw": round(bias, 4),
            "mean_actual_kw": round(mean_actual, 4),
            "mean_pred_kw": round(mean_pred, 4),
            "mape_pct": round(mape, 2),
            "n": n,
        }

    return {
        "window_days": window_days,
        "n_paired": len(all_pairs),
        "calibration_method": "per_hour_table" if cal_hourly else f"flat({flat_cal:.4f})",
        "overall": _stats(all_pairs),
        "per_hour": {h: _stats(p) for h, p in sorted(pairs_per_hour.items())},
    }


def forecast_to_lp_inputs(
    forecast: list[HourlyForecast],
    slot_starts_utc: list[datetime],
    pv_scale: float | dict[int, float] | Callable[[int, float], float] | None = None,
) -> WeatherLpSeries:
    """Build per-slot outdoor temperature, irradiance, PV kWh/slot, and COP arrays for the MILP.

    PV uses :func:`estimate_pv_kw` with ``PV_CAPACITY_KWP`` and ``PV_SYSTEM_EFFICIENCY``,
    scaled by ``pv_scale`` and corrected through the site calibration tables.
    Quartz direct PV follows the same calibration chain so the solver still
    sees local shading/orientation bias instead of raw vendor output.
    ``pv_scale`` can be:
      - ``float``: single multiplier applied to every slot (legacy behaviour).
      - ``dict[int, float]``: per-hour-of-day UTC factor (richer calibration that
        captures shading / sun-angle bias). Missing hours fall back to the median
        of provided values.
      - ``Callable[[hour_utc, cloud_pct], factor]``: cloud-aware calibration (PR #232).
        Receives the slot's hour-of-day + interpolated cloud_cover_pct, returns the
        per-slot factor. Best-of-class for capturing day-specific conditions.
      - ``None``: defaults to ``PV_FORECAST_SCALE_FACTOR`` from config.

    Half-hour energy = kW × 0.5 h. When forecast is empty, PV and COP use safe defaults.
    A hard ceiling of ``PV_CAPACITY_KWP × PV_SYSTEM_EFFICIENCY × 0.5`` kWh/slot is enforced.
    """
    n = len(slot_starts_utc)
    t_out: list[float] = []
    rad: list[float] = []
    cloud: list[float] = []
    pv: list[float] = []
    c_space: list[float] = []
    c_dhw: list[float] = []
    curve = config.DAIKIN_COP_CURVE
    dhw_pen = float(config.COP_DHW_PENALTY)
    cap = float(config.PV_CAPACITY_KWP)
    eff = float(config.PV_SYSTEM_EFFICIENCY)
    import sqlite3

    from . import db as _db

    # Same DB-tolerance shape as ``get_pv_calibration_factor_for`` — pure
    # callers may invoke this without ever running ``init_db()``.
    try:
        cal_cloud = _db.get_pv_calibration_hourly_cloud()
    except sqlite3.OperationalError:
        cal_cloud = {}
    try:
        cal_hourly = _db.get_pv_calibration_hourly()
    except sqlite3.OperationalError:
        cal_hourly = {}
    # PR L3 — pull 3D table once; passed through to forecast_pv_kw_from_row
    # so each slot can dispatch on its own (hour, cloud, elevation) cell.
    try:
        cal_3d = _db.get_pv_calibration_3d()
    except sqlite3.OperationalError:
        cal_3d = {}
    try:
        flat_cal = compute_pv_calibration_factor() if not cal_cloud and not cal_hourly else 1.0
    except sqlite3.OperationalError:
        flat_cal = 1.0
    # #486 — adaptive recent-bias corrector: a per-hour final nudge from the
    # committed forecast's own realised error (pv_error_log). Separate from the
    # calibration tables (which train on raw data), so applying it here closes a
    # convergent loop without contaminating training. Off → empty dict → no-op.
    recent_bias: dict[int, float] = {}
    if getattr(config, "PV_RECENT_BIAS_ENABLED", False):
        try:
            recent_bias = _db.get_pv_recent_bias()
        except sqlite3.OperationalError:
            recent_bias = {}

    # PV scale: explicit arg > config override > 1.0. Accept float OR per-hour dict.
    if pv_scale is None:
        pv_scale = float(getattr(config, "PV_FORECAST_SCALE_FACTOR", 1.0))
    # When dict is supplied, pre-compute fallback for missing hours (median of provided).
    pv_scale_fallback: float = 1.0
    if isinstance(pv_scale, dict) and pv_scale:
        sorted_vals = sorted(pv_scale.values())
        pv_scale_fallback = sorted_vals[len(sorted_vals) // 2]
    # Hard ceiling: per-hour-of-day maximum actually observed from Fox history
    # (falls back to capacity × η × 0.5 kWh/slot if no history available)
    hourly_ceil = _build_pv_hourly_ceiling()

    # #324: night-temperature bias. Open Meteo under-estimates how cold the
    # local microclimate gets overnight; without this correction the LP plans
    # too-warm scenarios and the battery depletes faster than budgeted
    # (observed 2026-05-12: pred 8 °C vs sensor 5 °C overnight). The bias is
    # applied ONLY when the LP reads the forecast — the actual heat pump
    # weather curve reacts to its own sensor, so this is a planning-side
    # correction with no comfort impact. Set bias=0 to disable.
    _night_bias = float(getattr(config, "FORECAST_NIGHT_TEMP_BIAS_C", 0.0))
    _night_start = int(getattr(config, "FORECAST_NIGHT_START_HOUR_UTC", 21))
    _night_end = int(getattr(config, "FORECAST_NIGHT_END_HOUR_UTC", 6))
    _night_wraps_midnight = _night_start > _night_end

    def _is_night_hour(h: int) -> bool:
        if _night_wraps_midnight:
            return h >= _night_start or h < _night_end
        return _night_start <= h < _night_end

    for i in range(n):
        st = slot_starts_utc[i]
        temp_c = _interp_hourly_scalar(forecast, st, "temperature_c", 10.0)
        if _night_bias != 0.0 and _is_night_hour(st.hour):
            temp_c += _night_bias
        rad_wm2 = _interp_hourly_scalar(forecast, st, "shortwave_radiation_wm2", 0.0)
        cloud_pct = _interp_hourly_scalar(forecast, st, "cloud_cover_pct", 50.0)
        nearest = get_forecast_for_slot(st, forecast)
        direct_pv = bool(nearest and nearest.pv_direct)
        if direct_pv:
            rad_eff = max(0.0, rad_wm2)
        else:
            # Cloud attenuation on top of API irradiance.
            att = max(0.0, min(1.0, 1.0 - 0.25 * (cloud_pct / 100.0)))
            rad_eff = max(0.0, rad_wm2 * att)
        if callable(pv_scale):
            try:
                # New signature: (hour_utc, cloud_pct, slot_start_utc) — lets
                # the closure distinguish today vs tomorrow slots so a
                # today-only PV bias factor doesn't leak across the date
                # boundary. Fall back to the legacy 2-arg signature for
                # callers that don't yet declare slot_start_utc.
                try:
                    scale_for_slot = float(pv_scale(st.hour, cloud_pct, st))
                except TypeError:
                    scale_for_slot = float(pv_scale(st.hour, cloud_pct))
            except Exception:
                scale_for_slot = 1.0
        elif isinstance(pv_scale, dict):
            scale_for_slot = pv_scale.get(st.hour, pv_scale_fallback)
        else:
            scale_for_slot = float(pv_scale)
        # The provider-specific PV signal can still be locally corrected: Quartz
        # direct PV is site-level, but it still benefits from the same calibration
        # chain that the irradiance-based path uses.
        kw_ac = forecast_pv_kw_from_row(
            st.hour,
            rad_wm2,
            cloud_pct,
            direct_pv_kw=_interp_hourly_scalar(forecast, st, "estimated_pv_kw", 0.0) if direct_pv else None,
            cloud_table=cal_cloud,
            hourly_table=cal_hourly,
            flat=flat_cal,
            scale=scale_for_slot,
            table_3d=cal_3d,
            slot_utc=st,
        )
        # #486 — adaptive recent-bias nudge (no-op when disabled / no data).
        if recent_bias:
            kw_ac *= recent_bias.get(st.hour, 1.0)
        # Apply calibration scale and enforce per-hour physical ceiling
        slot_ceil = hourly_ceil.get(st.hour, cap * eff * 0.5)
        pv_kwh = min(slot_ceil, max(0.0, kw_ac * 0.5))
        base_cop = max(1.0, cop_at_temperature(curve, temp_c))
        cop_s = base_cop
        lift_pen = float(getattr(config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.0))
        if lift_pen > 0.0:
            lwt_off_max = float(getattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0))
            lwt_ceiling = float(getattr(config, "LP_COP_SPACE_LWT_CEILING_C", 50.0))
            lwt_space = min(lwt_ceiling, get_lwt_base_c(temp_c) + lwt_off_max)
            lwt_dhw = float(getattr(config, "LP_COP_DHW_LIFT_SUPPLY_C", 45.0))
            ref_k = float(getattr(config, "LP_COP_LIFT_REFERENCE_DELTA_K", 25.0))
            min_m = float(getattr(config, "LP_COP_LIFT_MIN_MULTIPLIER", 0.5))
            cop_s = apply_cop_lift_multiplier(
                cop_s,
                temp_c,
                lwt_space,
                penalty_per_k=lift_pen,
                reference_delta_k=ref_k,
                min_mult=min_m,
            )
            cop_d = max(
                1.0,
                apply_cop_lift_multiplier(
                    max(1.0, base_cop - dhw_pen),
                    temp_c,
                    lwt_dhw,
                    penalty_per_k=lift_pen,
                    reference_delta_k=ref_k,
                    min_mult=min_m,
                ),
            )
        else:
            cop_d = max(1.0, cop_s - dhw_pen)
        t_out.append(temp_c)
        rad.append(rad_eff)
        cloud.append(cloud_pct)
        pv.append(pv_kwh)
        c_space.append(cop_s)
        c_dhw.append(cop_d)

    return WeatherLpSeries(
        slot_starts_utc=list(slot_starts_utc),
        temperature_outdoor_c=t_out,
        shortwave_radiation_wm2=rad,
        cloud_cover_pct=cloud,
        pv_kwh_per_slot=pv,
        cop_space=c_space,
        cop_dhw=c_dhw,
    )
