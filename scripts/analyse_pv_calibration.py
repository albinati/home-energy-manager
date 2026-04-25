"""Compare ``pv_realtime_history`` (5-min Fox samples) against Open-Meteo Archive
modelled PV → identify systematic bias by hour-of-day and month.

Usage
-----
    python -m scripts.analyse_pv_calibration [--start 2026-03-04] [--end 2026-04-25]

Output is a structured text report (no DB writes). Decisions about adjusting
``compute_pv_calibration_factor`` (window length, sun-angle correction) are
human, made AFTER reading this report.

Methodology
-----------
For each calendar day in ``[start, end]``:
  1. Sum 5-min PV samples × (5/60) → measured kWh per UTC hour-of-day.
  2. Fetch Open-Meteo Archive ``shortwave_radiation_instant`` per UTC hour.
  3. Convert via project model: ``estimate_pv_kw(rad)`` × 1 h = kWh.
  4. Per-hour ratio ``actual / modelled`` (skip if either ≤ 0.05 to avoid
     division noise at dawn/dusk).

Aggregations:
- Bias per hour-of-day (median + p25/p75).
- Bias per ISO month (median + n samples).
- Daily totals comparison + total-period summary.

The script never modifies code — only reads + prints recommendations.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta

from src import db
from src.config import config
from src.weather import estimate_pv_kw


def _fetch_hourly_radiation(start: date, end: date, lat: str, lon: str) -> dict[datetime, float]:
    """Hit Open-Meteo Archive for hourly ``shortwave_radiation_instant`` (W/m²)."""
    url = (
        "https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={lat}&longitude={lon}"
        f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
        "&hourly=shortwave_radiation_instant"
        "&timezone=UTC"
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"open-meteo fetch failed: {e}", file=sys.stderr)
        return {}
    times = data.get("hourly", {}).get("time", [])
    rads = data.get("hourly", {}).get("shortwave_radiation_instant", [])
    out: dict[datetime, float] = {}
    for t, r in zip(times, rads):
        if r is None:
            continue
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            out[dt.replace(minute=0, second=0, microsecond=0)] = float(r)
        except (ValueError, TypeError):
            continue
    return out


def _measured_pv_kwh_per_hour(start: date, end: date) -> dict[datetime, float]:
    """Aggregate ``pv_realtime_history`` 5-min samples into kWh per UTC hour."""
    from src.db import _lock, get_connection
    out: dict[datetime, float] = {}
    with _lock:
        conn = get_connection()
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
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                hour_key = ts.replace(minute=0, second=0, microsecond=0)
                # 5-min sample × (5/60)h = kWh contribution
                out[hour_key] = out.get(hour_key, 0.0) + kw * (5.0 / 60.0)
        finally:
            conn.close()
    return out


def _format_pct(values: list[float]) -> str:
    if not values:
        return "n/a"
    s = sorted(values)
    return f"median={statistics.median(values):.2f}  p25={s[len(s)//4]:.2f}  p75={s[3*len(s)//4]:.2f}  n={len(s)}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default="2026-03-04", help="ISO date inclusive")
    p.add_argument("--end", default=None, help="ISO date inclusive (default: today)")
    args = p.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()

    print(f"=== PV calibration analysis ===")
    print(f"Period: {start} → {end}  ({(end - start).days + 1} days)")
    print(f"Location: lat={config.WEATHER_LAT} lon={config.WEATHER_LON}")
    print()

    # Fetch both sources
    measured = _measured_pv_kwh_per_hour(start, end)
    radiation = _fetch_hourly_radiation(start, end, config.WEATHER_LAT, config.WEATHER_LON)
    if not measured:
        print("error: pv_realtime_history is empty for the requested window.", file=sys.stderr)
        return 2
    if not radiation:
        print("error: Open-Meteo returned empty.", file=sys.stderr)
        return 2

    # Per-hour matched comparison
    common = sorted(h for h in measured if h in radiation)
    print(f"Matched hours: {len(common)}  (measured={len(measured)}, modelled={len(radiation)})")
    print()

    ratios: list[float] = []
    by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    by_month: dict[str, list[float]] = {}
    daily_actual: dict[str, float] = {}
    daily_model: dict[str, float] = {}

    for h in common:
        actual_kwh = measured[h]
        rad = radiation[h]
        modelled_kwh = estimate_pv_kw(rad) * 1.0  # 1-hour slot
        # Skip dawn/dusk noise
        if actual_kwh < 0.05 and modelled_kwh < 0.05:
            continue
        if modelled_kwh < 0.05:
            continue
        ratio = actual_kwh / modelled_kwh
        # Drop egregious outliers (model artefact / sensor stuck)
        if ratio > 5.0:
            continue
        ratios.append(ratio)
        by_hour[h.hour].append(ratio)
        ym = f"{h.year:04d}-{h.month:02d}"
        by_month.setdefault(ym, []).append(ratio)
        d = h.strftime("%Y-%m-%d")
        daily_actual[d] = daily_actual.get(d, 0.0) + actual_kwh
        daily_model[d] = daily_model.get(d, 0.0) + modelled_kwh

    print("--- Overall ratio (actual / modelled) ---")
    print(f"  {_format_pct(ratios)}")
    print(f"  mean={statistics.mean(ratios):.3f}")
    print()

    print("--- Per hour-of-day (UTC) ---")
    print(f"  {'hour':4} {'median':>7} {'p25':>6} {'p75':>6} {'n':>5}")
    for h in range(24):
        rs = by_hour[h]
        if not rs:
            continue
        s = sorted(rs)
        print(f"  {h:2d}   {statistics.median(rs):7.2f} {s[len(s)//4]:6.2f} {s[3*len(s)//4]:6.2f} {len(rs):5d}")
    print()

    print("--- Per month ---")
    for ym in sorted(by_month):
        rs = by_month[ym]
        s = sorted(rs)
        print(f"  {ym}: median={statistics.median(rs):.2f}  p25={s[len(s)//4]:.2f}  p75={s[3*len(s)//4]:.2f}  n={len(rs)}")
    print()

    print("--- Daily totals (top 20 by date desc) ---")
    print(f"  {'date':10} {'actual':>8} {'modelled':>9} {'ratio':>6}")
    days_sorted = sorted(daily_actual.keys(), reverse=True)[:20]
    for d in days_sorted:
        a = daily_actual[d]
        m = daily_model[d]
        if m > 0:
            print(f"  {d}  {a:8.2f} {m:9.2f}  {a / m:6.2f}")
    print()

    daily_ratios = [daily_actual[d] / daily_model[d] for d in daily_actual if daily_model.get(d, 0) > 0]
    if daily_ratios:
        print("--- Daily ratio distribution ---")
        s = sorted(daily_ratios)
        print(f"  median={statistics.median(daily_ratios):.3f}  mean={statistics.mean(daily_ratios):.3f}  p25={s[len(s)//4]:.3f}  p75={s[3*len(s)//4]:.3f}  n={len(s)}")
        print()

    # Recommendation
    print("=== Recommendation ===")
    overall_median = statistics.median(ratios)
    print(f"Overall median hourly ratio = {overall_median:.3f}")
    print(f"Recent system-reported `compute_pv_calibration_factor`: read via API or check at runtime.")
    if overall_median < 0.85:
        suggested = round(0.85 * overall_median, 3)  # bias towards conservative correction
        print(f"  → Model is OVERESTIMATING. Consider:")
        print(f"     a) Reduce `_PV_SYSTEM_EFFICIENCY` in src/weather.py from 0.85 to {0.85 * overall_median:.3f}.")
        print(f"     b) OR shorten window in `compute_pv_calibration_factor` (250 → 30 days) so seasonal bias responds faster.")
    elif overall_median > 1.15:
        print(f"  → Model is UNDERESTIMATING. Increase `_PV_SYSTEM_EFFICIENCY` or system capacity.")
    else:
        print(f"  → Model within ±15 % of reality. Probably fine; revisit after more data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
