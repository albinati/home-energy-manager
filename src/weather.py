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
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .config import config, cop_at_temperature
from .physics import apply_cop_lift_multiplier, get_lwt_base_c

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
    limit_days: int = 250,
    min_solar_kwh: float = 1.0,
) -> float:
    """Compute rolling PV calibration factor: mean(actual / modelled) from Fox history.

    Compares fox_energy_daily.solar_kwh with Open-Meteo archive modelled solar for
    the same days. Returns a multiplier to scale the forward forecast. Clamps to [0.35, 1.05]
    (we never want to predict *more* than the best observed ratio, and never below 35%).
    - If ``PV_FORECAST_SCALE_FACTOR`` in config is > 0, that manual value is used directly.
    - If no Fox history is in the DB, falls back to 1.0.
    - Outlier days (ratio > 1.5) are dropped as they indicate archive data anomalies.
    """
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
            prev_pv = estimate_pv_kw(float(p.get("solar_w_m2") or 0.0))
            new_pv = estimate_pv_kw(float(n.get("solar_w_m2") or 0.0))
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


def forecast_to_lp_inputs(
    forecast: list[HourlyForecast],
    slot_starts_utc: list[datetime],
    pv_scale: float | None = None,
) -> WeatherLpSeries:
    """Build per-slot outdoor temperature, irradiance, PV kWh/slot, and COP arrays for the MILP.

    PV uses :func:`estimate_pv_kw` with ``PV_CAPACITY_KWP`` and ``PV_SYSTEM_EFFICIENCY``,
    scaled by ``pv_scale`` (default: ``PV_FORECAST_SCALE_FACTOR`` from config).
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

    # PV scale: explicit arg > config override > 1.0
    if pv_scale is None:
        pv_scale = float(getattr(config, "PV_FORECAST_SCALE_FACTOR", 1.0))
    # Hard ceiling: per-hour-of-day maximum actually observed from Fox history
    # (falls back to capacity × η × 0.5 kWh/slot if no history available)
    hourly_ceil = _build_pv_hourly_ceiling()

    for i in range(n):
        st = slot_starts_utc[i]
        temp_c = _interp_hourly_scalar(forecast, st, "temperature_c", 10.0)
        rad_wm2 = _interp_hourly_scalar(forecast, st, "shortwave_radiation_wm2", 0.0)
        cloud_pct = _interp_hourly_scalar(forecast, st, "cloud_cover_pct", 50.0)
        # Cloud attenuation on top of API irradiance
        att = max(0.0, min(1.0, 1.0 - 0.25 * (cloud_pct / 100.0)))
        rad_eff = max(0.0, rad_wm2 * att)
        kw_ac = estimate_pv_kw(rad_eff, capacity_kwp=cap, efficiency=eff)
        # Apply calibration scale and enforce per-hour physical ceiling
        slot_ceil = hourly_ceil.get(st.hour, cap * eff * 0.5)
        pv_kwh = min(slot_ceil, max(0.0, kw_ac * 0.5 * pv_scale))
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
