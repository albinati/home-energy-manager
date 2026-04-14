"""Weather data: historical analytics (Open-Meteo Archive) and forecast for optimization control.

Historical API (archive-api.open-meteo.com): daily mean temps, no key required.
Forecast API (api.open-meteo.com): hourly temperature, cloud cover, solar radiation, no key required.

Forecast is used by the optimization solver to:
  - Estimate heating demand per half-hour slot (degree-hours below base temp)
  - Estimate PV generation (4.5kWp × radiation × system efficiency)
  - Pre-heat before cold spells in cheap windows
  - Skip grid battery charging when solar is expected to fill the battery
"""
import urllib.request
import urllib.error
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from .config import config

# --------------------------------------------------------------------------
# Historical (analytics only)
# --------------------------------------------------------------------------

def fetch_daily_temps(
    start_date: date,
    end_date: date,
    lat: Optional[str] = None,
    lon: Optional[str] = None,
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


# System constants for PV estimate (London W4 system)
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


def compute_heating_demand_factor(
    temperature_c: float,
    base_temp_c: Optional[float] = None,
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
    lat: Optional[str] = None,
    lon: Optional[str] = None,
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

    now = datetime.now(timezone.utc)
    result: list[HourlyForecast] = []
    for i, t in enumerate(times):
        if len(result) >= hours:
            break
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                from datetime import timezone as _tz
                dt = dt.replace(tzinfo=_tz.utc)
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


def get_forecast_for_slot(
    slot_start_utc: datetime,
    forecast: list[HourlyForecast],
) -> Optional[HourlyForecast]:
    """Return the forecast entry closest to (but not after) slot_start_utc."""
    best: Optional[HourlyForecast] = None
    for f in forecast:
        if f.time_utc <= slot_start_utc:
            best = f
        elif best is not None:
            break
    return best
