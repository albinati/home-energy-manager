"""Optional weather data for heating analytics (Open-Meteo Historical API, no key required)."""
import urllib.request
import urllib.error
import json
from datetime import date
from typing import Optional

from .config import config


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
