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
from collections.abc import Callable
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


def forecast_pv_kw_from_row(
    hour_utc: int,
    shortwave_radiation_wm2: float,
    cloud_cover_pct: float | None,
    *,
    cloud_table: dict[tuple[int, int], float] | None = None,
    hourly_table: dict[int, float] | None = None,
    flat: float = 1.0,
) -> float:
    """Apply the same PV forecast transform used by the LP and live triggers.

    Cloud attenuation is applied before the irradiance-to-kW conversion and the
    calibration lookup follows the same cloud → hour → flat fallback chain as
    ``forecast_to_lp_inputs``.
    """
    cloud_pct_f = float(cloud_cover_pct) if cloud_cover_pct is not None else 50.0
    att = max(0.0, min(1.0, 1.0 - 0.25 * (cloud_pct_f / 100.0)))
    rad_eff = max(0.0, float(shortwave_radiation_wm2) * att)
    cal = get_pv_calibration_factor_for(
        int(hour_utc),
        cloud_pct_f,
        cloud_table=cloud_table,
        hourly_table=hourly_table,
        flat=flat,
    )
    return estimate_pv_kw(rad_eff) * cal


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
            prev_pv = forecast_pv_kw_from_row(
                slot.hour,
                float(p.get("solar_w_m2") or 0.0),
                p.get("cloud_cover_pct"),
            )
            new_pv = forecast_pv_kw_from_row(
                slot.hour,
                float(n.get("solar_w_m2") or 0.0),
                n.get("cloud_cover_pct"),
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
    """Recompute the per-hour-of-day PV calibration table from ``pv_realtime_history``.

    For each UTC hour-of-day, sums measured PV (5-min samples × 1/12 → kWh per
    hour) and compares against Open-Meteo Archive ``shortwave_radiation_instant``
    converted via :func:`estimate_pv_kw`. The median ratio per hour becomes the
    factor stored in ``pv_calibration_hourly``.

    Hours with fewer than ``min_samples_per_hour`` samples in the window are
    skipped (low statistical confidence). Returns a status dict so the caller
    can log how the recompute went.

    Failure modes (no Fox data, Open-Meteo down, etc.) return a dict with
    ``status='skipped'`` and never raise — the LP keeps the flat fallback.
    """
    from . import db as _db
    from datetime import date as _date, timedelta as _td

    if window_days is None:
        window_days = int(getattr(config, "PV_CALIBRATION_WINDOW_DAYS", 30))
    end = _date.today()
    start = end - _td(days=window_days)

    # Pull measured PV per UTC hour from pv_realtime_history.
    # NOTE: average kW × 1h, NOT sum × (5/60). The previous "kw × (5/60)" formula
    # assumed 12 samples/hour (5-min cadence). In practice the heartbeat writes
    # samples sparsely (often 1-3/hour), so the sum-based formula collapses
    # by ~10× — the resulting "ratio" was artificially low and made the per-hour
    # calibration table over-correct against forecast (audit 2026-05-02:
    # factors at the 0.10 floor when reality should give 0.5-0.7).
    measured_per_hour: dict[int, list[float]] = {h: [] for h in range(24)}
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
    # mean kW × 1h = kWh per hour (sample-density-independent)
    measured_per_day_hour: dict[tuple[str, int], float] = {
        k: (sum(samples) / len(samples)) for k, samples in measured_kw_samples.items() if samples
    }

    if not measured_per_day_hour:
        return {"status": "skipped", "reason": "no pv_realtime_history in window"}

    # Pull modelled PV per hour from Open-Meteo Archive — same conversion the LP uses.
    lat = (config.WEATHER_LAT or "").strip()
    lon = (config.WEATHER_LON or "").strip()
    if not lat or not lon:
        return {"status": "skipped", "reason": "no lat/lon configured"}
    try:
        url = (
            "https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat}&longitude={lon}"
            f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
            "&hourly=shortwave_radiation_instant"
            "&timezone=UTC"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            api_data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError) as e:
        return {"status": "skipped", "reason": f"open-meteo fetch failed: {e}"}

    times = api_data.get("hourly", {}).get("time", [])
    rads = api_data.get("hourly", {}).get("shortwave_radiation_instant", [])
    modelled_per_day_hour: dict[tuple[str, int], float] = {}
    for t, r in zip(times, rads):
        if r is None:
            continue
        try:
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            day = dt.date().isoformat()
            hour = dt.hour
            modelled_per_day_hour[(day, hour)] = estimate_pv_kw(float(r))  # 1-hour slot → kWh
        except (ValueError, TypeError):
            continue

    # Build per-hour ratio lists, skipping dawn/dusk noise (both < 0.05).
    ratios_per_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    for (day, hour), measured_kwh in measured_per_day_hour.items():
        modelled_kwh = modelled_per_day_hour.get((day, hour))
        if modelled_kwh is None or modelled_kwh < 0.05:
            continue
        if measured_kwh < 0.05 and modelled_kwh < 0.05:
            continue
        ratio = measured_kwh / modelled_kwh
        # Drop egregious outliers (sensor spike or archive anomaly)
        if ratio > 5.0:
            continue
        ratios_per_hour[hour].append(ratio)

    factors: dict[int, float] = {}
    samples: dict[int, int] = {}
    for hour, rs in ratios_per_hour.items():
        if len(rs) < min_samples_per_hour:
            continue
        # Use median (robust to single bad day) and clamp to safe range
        rs_sorted = sorted(rs)
        median = rs_sorted[len(rs_sorted) // 2]
        clamped = max(0.10, min(1.10, median))
        factors[hour] = round(clamped, 4)
        samples[hour] = len(rs)

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

    # 2. Pull today's forecast PV per UTC hour from slot-time keyed rows. This
    # is what the LP/heartbeat should reason about, regardless of when the rows
    # were last upserted.
    try:
        forecast_rows = _db.get_meteo_forecast_for_slot_date(today)
    except Exception:  # noqa: BLE001
        forecast_rows = []
    cal_cloud = _db.get_pv_calibration_hourly_cloud()
    cal_hour = _db.get_pv_calibration_hourly()
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
                cloud_table=cal_cloud,
                hourly_table=cal_hour,
                flat=flat_cal,
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

    # 4. Median ratio (robust to one bad hour) + safety clamp
    sorted_r = sorted(r for _, r in ratios)
    median = sorted_r[len(sorted_r) // 2]
    lo, hi = safety_clamp
    factor = max(lo, min(hi, median))

    return round(factor, 4), {
        "n_hours": len(ratios),
        "median_ratio": round(median, 4),
        "applied_factor": round(factor, 4),
        "clamped": factor != median,
        "ratios_per_hour": {h: round(r, 4) for h, r in ratios},
    }


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
) -> float:
    """Resolve the calibration factor with the cloud → hour → flat fallback chain.

    Lookup priority:
        1. ``pv_calibration_hourly_cloud[(hour, bucket(cloud))]`` — per-hour × per-cloud
        2. ``pv_calibration_hourly[hour]``                       — per-hour only
        3. ``flat`` (caller's flat fallback, typically from compute_pv_calibration_factor)

    Pass pre-fetched tables to avoid hitting the DB inside per-slot loops.
    """
    from . import db as _db

    if cloud_table is None:
        cloud_table = _db.get_pv_calibration_hourly_cloud()
    if hourly_table is None:
        hourly_table = _db.get_pv_calibration_hourly()

    bucket = cloud_bucket(cloud_cover_pct)
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

    # 2. Pull historical forecasts WITH cloud_cover via Open-Meteo Archive
    lat = (config.WEATHER_LAT or "").strip()
    lon = (config.WEATHER_LON or "").strip()
    if not lat or not lon:
        return {"status": "skipped", "reason": "no lat/lon configured"}
    try:
        url = (
            "https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat}&longitude={lon}"
            f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
            "&hourly=shortwave_radiation_instant,cloud_cover"
            "&timezone=UTC"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            api_data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError) as e:
        return {"status": "skipped", "reason": f"open-meteo archive fetch failed: {e}"}

    times = api_data.get("hourly", {}).get("time", [])
    rads = api_data.get("hourly", {}).get("shortwave_radiation_instant", [])
    clouds = api_data.get("hourly", {}).get("cloud_cover", [])
    archive: dict[tuple[str, int], tuple[float, float | None]] = {}
    for i, t in enumerate(times):
        try:
            dt = _dt.fromisoformat(str(t).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_UTC)
            day = dt.date().isoformat()
            hour = dt.hour
            r = rads[i] if i < len(rads) else None
            c = clouds[i] if i < len(clouds) else None
            if r is None:
                continue
            archive[(day, hour)] = (float(r), float(c) if c is not None else None)
        except (ValueError, TypeError, IndexError):
            continue

    # 3. Build per-(hour, bucket) ratio lists
    ratios_per_cell: dict[tuple[int, int], list[float]] = defaultdict(list)
    for (day, hour), measured_kw in measured_per_day_hour.items():
        forecast = archive.get((day, hour))
        if forecast is None:
            continue
        rad, cloud = forecast
        modelled_kw = forecast_pv_kw_from_row(
            hour,
            rad,
            cloud,
            cloud_table=cal_cloud,
            hourly_table=cal_hourly,
            flat=flat_cal,
        )
        if modelled_kw < 0.05 or measured_kw < 0.05:
            continue                          # dawn/dusk noise
        ratio = measured_kw / modelled_kw
        if ratio > 5.0:
            continue                          # outlier
        bucket = cloud_bucket(cloud)
        ratios_per_cell[(hour, bucket)].append(ratio)

    # 4. Median per cell, clamp to [0.05, 1.20] (slightly wider than per-hour
    #    table because clear-sky bucket can legitimately reach 1.0+)
    factors: dict[tuple[int, int], float] = {}
    samples: dict[tuple[int, int], int] = {}
    for cell, rs in ratios_per_cell.items():
        if len(rs) < min_samples_per_cell:
            continue
        rs_sorted = sorted(rs)
        median = rs_sorted[len(rs_sorted) // 2]
        clamped = max(0.05, min(1.20, median))
        factors[cell] = round(clamped, 4)
        samples[cell] = len(rs)

    if not factors:
        return {"status": "skipped", "reason": "insufficient samples per (hour, bucket)"}

    n = _db.upsert_pv_calibration_hourly_cloud(factors, samples, window_days)
    return {
        "status": "ok",
        "rows": n,
        "window_days": window_days,
        "cells_calibrated": sorted(factors.keys()),
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
    flat_cal = compute_pv_calibration_factor() if not cal_hourly and not cal_cloud else 1.0

    # Pull all measurements + forecasts in window
    actual_kw_samples: dict[tuple[str, int], list[float]] = defaultdict(list)
    forecast_radiation: dict[tuple[str, int], float] = {}
    forecast_cloud: dict[tuple[str, int], float | None] = {}

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

            # cloud_cover_pct may not exist on older meteo_forecast rows; coalesce.
            cur = conn.execute(
                """SELECT slot_time, solar_w_m2,
                          COALESCE(cloud_cover_pct, NULL) AS cloud_pct
                     FROM meteo_forecast
                    WHERE substr(slot_time, 1, 10) BETWEEN ? AND ?""",
                (start.isoformat(), end.isoformat()),
            )
            for ts_raw, rad, cpct in cur.fetchall():
                try:
                    ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_UTC)
                key = (ts.date().isoformat(), ts.hour)
                forecast_radiation[key] = float(rad or 0.0)
                forecast_cloud[key] = float(cpct) if cpct is not None else None
        finally:
            conn.close()

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
        predicted_kw = forecast_pv_kw_from_row(
            key[1],
            rad,
            forecast_cloud.get(key),
            cloud_table=cal_cloud,
            hourly_table=cal_hourly,
            flat=flat_cal,
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
    scaled by ``pv_scale``:
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

    for i in range(n):
        st = slot_starts_utc[i]
        temp_c = _interp_hourly_scalar(forecast, st, "temperature_c", 10.0)
        rad_wm2 = _interp_hourly_scalar(forecast, st, "shortwave_radiation_wm2", 0.0)
        cloud_pct = _interp_hourly_scalar(forecast, st, "cloud_cover_pct", 50.0)
        # Cloud attenuation on top of API irradiance
        att = max(0.0, min(1.0, 1.0 - 0.25 * (cloud_pct / 100.0)))
        rad_eff = max(0.0, rad_wm2 * att)
        kw_ac = estimate_pv_kw(rad_eff, capacity_kwp=cap, efficiency=eff)
        # Resolve the per-slot scale factor.
        if callable(pv_scale):
            try:
                scale_for_slot = float(pv_scale(st.hour, cloud_pct))
            except Exception:
                scale_for_slot = 1.0
        elif isinstance(pv_scale, dict):
            scale_for_slot = pv_scale.get(st.hour, pv_scale_fallback)
        else:
            scale_for_slot = float(pv_scale)
        # Apply calibration scale and enforce per-hour physical ceiling
        slot_ceil = hourly_ceil.get(st.hour, cap * eff * 0.5)
        pv_kwh = min(slot_ceil, max(0.0, kw_ac * 0.5 * scale_for_slot))
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
