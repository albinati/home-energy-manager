"""Planned-vs-actual PV history analysis — read-only.

Compares measured PV (pv_realtime_history) against TWO forecast bases over a
window, per half-hour slot:

  * latest    — the best nowcast available just before the slot (the MAX
                forecast fetch whose timestamp <= slot start).
  * dayahead  — what the forecast said around 00:05 UTC of the slot's day
                (the committed-plan basis the nightly push solves against).

Both forecast rows are converted to kW with the SAME canonical transform the
LP / endpoint use (weather.forecast_pv_kw_from_row + current calibration
tables, scale=1.0 — no intra-day "today factor", matching
evaluate_pv_forecast_accuracy). bias = actual - forecast (positive =
under-forecast).

Aggregates: overall, the latest-vs-dayahead delta (does intra-day
re-forecasting help?), per hour-of-day, AM/PM, per cloud bucket, per
solar-elevation bucket, and per month. Cross-checks the latest series against
weather.evaluate_pv_forecast_accuracy.

NOTE: stored forecast history is bounded by METEO_FORECAST_HISTORY_RETENTION_DAYS
(~30d) — the script reports the effective forecast coverage separately from the
actuals window.

Run on prod:
    scp scripts/analyse_planned_vs_actual_pv.py <prod>:/srv/hem/data/
    docker exec hem python /app/data/analyse_planned_vs_actual_pv.py [days]
"""
from __future__ import annotations

import math
import statistics as st
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from src import db, weather

MIN_KW = 0.05  # dawn/dusk noise floor, matches evaluate_pv_forecast_accuracy


def _norm(iso: str) -> str:
    """Common slot key 'YYYY-MM-DDTHH:MM' across Z / +00:00 forms."""
    return str(iso).replace("Z", "+00:00")[:16]


def _slot_dt(key: str) -> datetime:
    return datetime.fromisoformat(key + ":00+00:00")


def _calibration():
    cal_hourly = db.get_pv_calibration_hourly()
    cal_cloud = db.get_pv_calibration_hourly_cloud()
    cal_3d = db.get_pv_calibration_3d()
    flat = weather.compute_pv_calibration_factor() if not cal_hourly and not cal_cloud else 1.0
    return cal_hourly, cal_cloud, cal_3d, flat


def _row_to_kwh(row: dict, slot_dt: datetime, cal) -> float:
    cal_hourly, cal_cloud, cal_3d, flat = cal
    kw = weather.forecast_pv_kw_from_row(
        slot_dt.hour,
        row.get("solar_w_m2") or 0.0,
        row.get("cloud_cover_pct"),
        direct_pv_kw=row.get("direct_pv_kw"),
        cloud_table=cal_cloud,
        hourly_table=cal_hourly,
        flat=flat,
        scale=1.0,
        table_3d=cal_3d,
        slot_utc=slot_dt,
    )
    return max(0.0, kw) * 0.5


def _actuals(window_days: int) -> dict[str, float]:
    """Per-slot realised PV kWh keyed by 'YYYY-MM-DDTHH:MM' (UTC)."""
    out: dict[str, float] = {}
    today = datetime.now(UTC).date()
    for i in range(1, window_days + 1):
        d = today - timedelta(days=i)
        try:
            for k, v in db.half_hourly_solar_kwh_for_day(d).items():
                out[_norm(k)] = float(v)
        except Exception:
            continue
    return out


def _latest_forecast(start_iso: str, end_iso: str, cal) -> dict[str, float]:
    """Per slot, the kWh from the MAX fetch whose timestamp <= slot start."""
    out: dict[str, float] = {}
    with db._lock:
        conn = db.get_connection()
        try:
            cur = conn.execute(
                """SELECT v.slot_time, v.solar_w_m2, v.cloud_cover_pct, v.direct_pv_kw
                   FROM meteo_forecast_value v
                   WHERE v.slot_time BETWEEN ? AND ?
                     AND v.forecast_fetch_at_utc = (
                         SELECT MAX(v2.forecast_fetch_at_utc)
                         FROM meteo_forecast_value v2
                         WHERE v2.slot_time = v.slot_time
                           AND v2.forecast_fetch_at_utc <= v.slot_time
                     )""",
                (start_iso, end_iso),
            )
            for r in cur.fetchall():
                key = _norm(r["slot_time"])
                out[key] = _row_to_kwh(dict(r), _slot_dt(key), cal)
        finally:
            conn.close()
    return out


def _dayahead_forecast(window_days: int, cal) -> dict[str, float]:
    """Per slot, the kWh from the fetch nearest 00:05 UTC of the slot's day."""
    out: dict[str, float] = {}
    today = datetime.now(UTC).date()
    for i in range(1, window_days + 1):
        d = today - timedelta(days=i)
        cutoff = f"{d.isoformat()}T00:05:00+00:00"
        with db._lock:
            conn = db.get_connection()
            try:
                row = conn.execute(
                    "SELECT MAX(forecast_fetch_at_utc) f FROM meteo_forecast_value "
                    "WHERE forecast_fetch_at_utc <= ?", (cutoff,)
                ).fetchone()
            finally:
                conn.close()
        fetch = row["f"] if row else None
        if not fetch:
            continue
        for r in db.get_meteo_forecast_at(fetch):
            key = _norm(r["slot_time"])
            if key[:10] == d.isoformat():
                out[key] = _row_to_kwh(dict(r), _slot_dt(key), cal)
    return out


def _stats(pairs: list[tuple[float, float]]) -> dict:
    """pairs = [(actual, forecast)]. Returns n/mae/bias/rmse/mape."""
    pairs = [(a, f) for a, f in pairs if a >= MIN_KW or f >= MIN_KW]
    if not pairs:
        return {"n": 0, "mae": 0.0, "bias": 0.0, "rmse": 0.0, "mape": 0.0}
    errs = [a - f for a, f in pairs]
    mae = sum(abs(e) for e in errs) / len(errs)
    bias = sum(errs) / len(errs)
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
    denom = sum(a for a, _ in pairs)
    mape = (sum(abs(a - f) for a, f in pairs) / denom * 100) if denom > 0 else 0.0
    return {"n": len(pairs), "mae": mae, "bias": bias, "rmse": rmse, "mape": mape}


def _agg(rows: list[tuple], key_fn) -> dict:
    buckets: dict = defaultdict(list)
    for r in rows:
        buckets[key_fn(r)].append((r[1], r[2]))  # (actual, forecast)
    return {k: _stats(v) for k, v in buckets.items()}


def _table(title: str, agg: dict, order=None) -> str:
    keys = order or sorted(agg.keys())
    out = [f"\n### {title}", "key | n | MAE | bias | RMSE", "--- | --- | --- | --- | ---"]
    for k in keys:
        s = agg.get(k)
        if not s or s["n"] == 0:
            continue
        out.append(f"{k} | {s['n']} | {s['mae']:.3f} | {s['bias']:+.3f} | {s['rmse']:.3f}")
    return "\n".join(out)


def main(window_days: int = 30) -> int:
    cal = _calibration()
    actual = _actuals(window_days)
    if not actual:
        print("No actuals in window.")
        return 1
    keys = sorted(actual.keys())
    start_iso = _slot_dt(keys[0]).isoformat()
    end_iso = _slot_dt(keys[-1]).isoformat()

    latest = _latest_forecast(start_iso, end_iso, cal)
    dayahead = _dayahead_forecast(window_days, cal)

    fc_keys = set(latest) | set(dayahead)
    fc_days = sorted({k[:10] for k in fc_keys})
    print(f"# Planned-vs-actual PV — last {window_days}d")
    print(f"\nactuals slots: {len(actual)} ({keys[0][:10]} .. {keys[-1][:10]})")
    print(f"forecast coverage: {len(fc_days)} days "
          f"({fc_days[0] if fc_days else '-'} .. {fc_days[-1] if fc_days else '-'}) "
          f"— bounded by METEO_FORECAST_HISTORY_RETENTION_DAYS")

    # Rows for each basis: (slot_dt, actual, forecast, cloud, elev)
    def _rows(fc: dict) -> list[tuple]:
        rows = []
        for k, f in fc.items():
            a = actual.get(k)
            if a is None:
                continue
            sd = _slot_dt(k)
            rows.append((sd, a, f))
        return rows

    rows_latest = _rows(latest)
    rows_da = _rows(dayahead)

    print("\n## Overall")
    sl = _stats([(a, f) for _, a, f in rows_latest])
    sd = _stats([(a, f) for _, a, f in rows_da])
    print(f"latest   : n={sl['n']} MAE={sl['mae']:.3f} bias={sl['bias']:+.3f} RMSE={sl['rmse']:.3f} MAPE={sl['mape']:.0f}%")
    print(f"day-ahead: n={sd['n']} MAE={sd['mae']:.3f} bias={sd['bias']:+.3f} RMSE={sd['rmse']:.3f} MAPE={sd['mape']:.0f}%")

    # Headline: latest-vs-dayahead on the SHARED slot set.
    shared = [k for k in latest if k in dayahead and k in actual]
    pl = [(actual[k], latest[k]) for k in shared]
    pd = [(actual[k], dayahead[k]) for k in shared]
    spl, spd = _stats(pl), _stats(pd)
    print("\n## Headline — does intra-day re-forecasting help? (shared slots)")
    print(f"shared slots: {spl['n']}")
    print(f"  day-ahead MAE {spd['mae']:.3f}  →  latest MAE {spl['mae']:.3f}  "
          f"(Δ {spl['mae']-spd['mae']:+.3f} kWh, {'-' if spl['mae']<spd['mae'] else '+'}{abs(spl['mae']-spd['mae'])/spd['mae']*100 if spd['mae'] else 0:.0f}%)")
    print(f"  day-ahead bias {spd['bias']:+.3f}  →  latest bias {spl['bias']:+.3f}")

    # Pattern tables on the LATEST basis (best forecast skill).
    print("\n## Patterns (latest forecast vs actual)")
    print(_table("By hour of day (UTC)", _agg(rows_latest, lambda r: f"{r[0].hour:02d}"),
                 order=[f"{h:02d}" for h in range(24)]))
    # AM/PM
    am = _stats([(a, f) for sd_, a, f in rows_latest if 4 <= sd_.hour < 12])
    pm = _stats([(a, f) for sd_, a, f in rows_latest if 12 <= sd_.hour < 20])
    print("\n### AM (04-12) vs PM (12-20)")
    print(f"AM: n={am['n']} MAE={am['mae']:.3f} bias={am['bias']:+.3f}")
    print(f"PM: n={pm['n']} MAE={pm['mae']:.3f} bias={pm['bias']:+.3f}")
    # cloud bucket (needs the forecast cloud — recompute from latest fetch rows)
    print(_table("By solar-elevation bucket (0=<10° .. 4=>55°)",
                 _agg(rows_latest, lambda r: str(weather.elevation_bucket(
                     weather.compute_solar_elevation_deg(r[0].replace(minute=30)))))))
    print(_table("By month", _agg(rows_latest, lambda r: r[0].strftime("%Y-%m"))))

    # Cross-check against the existing tool.
    print("\n## Cross-check vs evaluate_pv_forecast_accuracy (latest, per-hour MAE kW)")
    try:
        ev = weather.evaluate_pv_forecast_accuracy(window_days=window_days)
        ov = ev.get("overall", {})
        print(f"  tool overall: MAE={ov.get('mae_kw'):.3f} kW  bias={ov.get('bias_kw'):+.3f} kW  n={ov.get('n')}")
        print("  (script works in kWh/half-slot; expect tool kW ≈ 2× script kWh at matching hours)")
    except Exception as e:
        print(f"  (cross-check unavailable: {e})")
    return 0


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    raise SystemExit(main(days))
