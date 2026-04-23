"""v10.2 — pattern analytics for the Insights browser.

Pure SQLite reads. Each function takes a UTC date range
(``start_date``, ``end_date`` as ``YYYY-MM-DD``) and returns a small JSON-able
dict ready to render as inline sparklines/bars.

Designed to be cheap (one query per call) and ML-extension-friendly: every
returned shape is flat enough to feed a downstream model later without the
analytics module having to know what the model wants.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _local_tz() -> ZoneInfo:
    return ZoneInfo(config.BULLETPROOF_TIMEZONE or "Europe/London")


def hourly_load_profile_for_range(start_date: str, end_date: str) -> dict[str, Any]:
    """Mean consumption (kWh per 30-min slot) by hour-of-day across the range.

    Uses ``execution_log.consumption_kwh`` rows in the date range, bucketed
    into the local-time hour of the slot. Returns ``{hour: mean_kwh}`` plus
    sample counts for transparency.
    """
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    tz = _local_tz()
    start_iso = datetime(start.year, start.month, start.day, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    end_iso = (datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")

    with db._lock:
        conn = db.get_connection()
        try:
            cur = conn.execute(
                """SELECT timestamp, consumption_kwh
                   FROM execution_log
                   WHERE timestamp >= ? AND timestamp < ?
                     AND consumption_kwh IS NOT NULL""",
                (start_iso, end_iso),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            hour = ts.astimezone(tz).hour
        except (ValueError, TypeError, AttributeError):
            continue
        buckets[hour].append(float(r["consumption_kwh"] or 0.0))

    profile = {
        str(h): {
            "mean_kwh": (sum(b) / len(b)) if b else 0.0,
            "sample_count": len(b),
        }
        for h, b in buckets.items()
    }
    return {
        "start": start_date,
        "end": end_date,
        "tz": tz.key,
        "profile": profile,
        "total_samples": sum(len(b) for b in buckets.values()),
    }


def dow_load_shape(start_date: str, end_date: str) -> dict[str, Any]:
    """Mean daily import (kWh) by day-of-week (Mon=0..Sun=6) across the range.

    Reads ``fox_energy_daily.import_kwh`` (covered by the v10.2 read-through cache).
    """
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    rows = db.get_fox_energy_daily_range(start.isoformat(), end.isoformat())

    buckets: dict[int, list[float]] = {d: [] for d in range(7)}
    for r in rows:
        try:
            d = date.fromisoformat(r["date"])
        except (ValueError, TypeError):
            continue
        v = r.get("import_kwh")
        if v is None:
            continue
        buckets[d.weekday()].append(float(v))

    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    profile = {
        names[i]: {
            "mean_import_kwh": (sum(b) / len(b)) if b else 0.0,
            "sample_count": len(b),
        }
        for i, b in buckets.items()
    }
    return {
        "start": start_date,
        "end": end_date,
        "profile": profile,
        "total_days": sum(len(b) for b in buckets.values()),
    }


def cheap_peak_slot_frequency(
    tariff_code: str | None,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """% of slots in each kind across the date range using percentile thresholds.

    Reuses the same simple classification as ``/api/v1/agile/day``: percentile
    bins per-day (so a cold-day baseload that's pricey vs the day's own dist
    still reads as 'standard', not 'peak'). Returns aggregate counts.
    """
    if not tariff_code:
        return {"start": start_date, "end": end_date, "tariff_code": None, "kinds": {}}
    start = _parse_date(start_date)
    end = _parse_date(end_date)

    counts = {"negative": 0, "cheap": 0, "standard": 0, "peak": 0}
    sums_p = {"negative": 0.0, "cheap": 0.0, "standard": 0.0, "peak": 0.0}
    total = 0
    cur = start
    one = timedelta(days=1)
    tz = _local_tz()
    while cur <= end:
        slots = db.get_agile_rates_slots_for_local_day(tariff_code, cur, tz_name=tz.key)
        if slots:
            prices = sorted(float(s["value_inc_vat"]) for s in slots)
            n = len(prices)
            q25 = prices[max(0, n // 4 - 1)]
            q75 = prices[min(n - 1, (3 * n) // 4)]
            mean_p = sum(prices) / n
            cheap_thr = min(mean_p * 0.85, q25)
            peak_thr = max(q75, float(config.OPTIMIZATION_PEAK_THRESHOLD_PENCE))
            for s in slots:
                p = float(s["value_inc_vat"])
                if p <= 0:
                    k = "negative"
                elif p < cheap_thr:
                    k = "cheap"
                elif p > peak_thr:
                    k = "peak"
                else:
                    k = "standard"
                counts[k] += 1
                sums_p[k] += p
                total += 1
        cur += one

    return {
        "start": start_date,
        "end": end_date,
        "tariff_code": tariff_code,
        "total_slots": total,
        "kinds": {
            k: {
                "count": counts[k],
                "pct": round(100 * counts[k] / total, 2) if total else 0.0,
                "mean_p": round(sums_p[k] / counts[k], 3) if counts[k] else None,
            }
            for k in counts
        },
    }


def pv_forecast_vs_actual(start_date: str, end_date: str) -> dict[str, Any]:
    """Daily actual PV (Fox) vs forecast (cached weather), ratio per day.

    Returns a list of ``{date, actual_kwh, forecast_kwh, ratio}`` rows.
    Forecast uses ``weather.compute_pv_calibration_factor`` shape if present,
    else null forecast and a partial response with `forecast_unavailable: true`.
    """
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    actuals = {r["date"]: float(r.get("solar_kwh") or 0.0)
               for r in db.get_fox_energy_daily_range(start.isoformat(), end.isoformat())}

    series = []
    cur = start
    one = timedelta(days=1)
    while cur <= end:
        ds = cur.isoformat()
        a = actuals.get(ds)
        series.append({
            "date": ds,
            "actual_kwh": round(a, 2) if a is not None else None,
            "forecast_kwh": None,
            "ratio": None,
        })
        cur += one
    return {
        "start": start_date,
        "end": end_date,
        "series": series,
        "forecast_unavailable": True,
    }


__all__ = [
    "hourly_load_profile_for_range",
    "dow_load_shape",
    "cheap_peak_slot_frequency",
    "pv_forecast_vs_actual",
]
