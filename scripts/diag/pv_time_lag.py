#!/usr/bin/env python
"""Measure the forecast-vs-realised PV *time* lag from live data — read-only.

The cockpit "Today's plan" chart overlays the PV **forecast** line and the
**realised** PV on a shared half-hour slot axis. A user noticed what looked
like a small horizontal offset between them and asked whether UTC/BST handling
is wrong. A code audit found the timezone handling correct end-to-end (forecast,
import price, and realised PV are all joined on the *same* UTC half-hour key, and
the UI renders UTC→local consistently). The one residual nuance is a forecast
*sampling convention*: each slot's energy is `kw_at_slot_START × 0.5h` rather
than the slot centre/average, which on a ramping solar curve can shift the
forecast line ~15 min versus the realised (trapezoidal) energy.

This script quantifies that lag from the SAME data the chart consumes, by GETting
``/api/v1/pv/today`` for several recent completed UTC days (a viewer endpoint — no
token needed) and computing, per day, three sign-consistent lag estimates where
**positive = forecast is LATER than realised (forecast lags)**:

  * centroid shift  — energy-weighted mean slot time, forecast − realised, in
    minutes. Sub-slot resolution; the headline metric.
  * xcorr best lag  — the integer-slot shift of the forecast that best matches
    realised (Pearson), over ±2 slots (±60 min). Corroborates the centroid.
  * peak shift      — argmax(forecast) − argmax(realised), in minutes. Intuitive
    but noisy.

It then prints a per-day table and an aggregate verdict mapping to the decision
gate:
    |median centroid| ≲ 8 min            → aligned; no action needed.
    ~ +9..+22 min                        → slot-start sampling convention (~15 min,
                                           forecast late) — small fix is justified.
    > 22 min, or large negative          → unexpected; reopen the audit.

Read-only: only GETs ``/api/v1/pv/today``. No DB, no MCP, no writes, no hardware.

Run from any Tailscale-connected machine (default base = the prod UI origin that
proxies /api), or on the prod host against loopback:

    python scripts/diag/pv_time_lag.py --days 7
    python scripts/diag/pv_time_lag.py --base http://127.0.0.1:8000 --days 7
"""
from __future__ import annotations

import argparse
import json
import ssl
import statistics as st
import urllib.request
from datetime import UTC, datetime, timedelta

# Default to the Tailscale UI origin (nginx reverse-proxies /api → hem). On the
# prod host, pass --base http://127.0.0.1:8000 to hit the API directly.
DEFAULT_BASE = "https://openclaw-overbot.tail0dbf20.ts.net:8443"

SLOT_MIN = 30          # half-hour slots
FLOOR_KWH = 0.02       # per-slot dawn/dusk noise floor
MIN_DAY_KWH = 2.0      # skip days too small to infer a lag reliably
MAX_LAG_SLOTS = 2      # xcorr search window: ±2 slots = ±60 min


def _get_json(base: str, path: str, insecure: bool, timeout: float) -> dict:
    url = base.rstrip("/") + path
    ctx = ssl._create_unverified_context() if insecure else None
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _series(slots: list[dict]) -> tuple[list[float], list[float | None]]:
    """(forecast, realised) arrays aligned by slot index.

    Forecast prefers the committed plan (``pv_planned_kwh``) and falls back to the
    live forecast (``pv_forecast_kwh``); realised is ``pv_actual_kwh`` (None where
    not yet measured).
    """
    fc: list[float] = []
    act: list[float | None] = []
    for s in slots:
        p = s.get("pv_planned_kwh")
        f = p if p is not None else s.get("pv_forecast_kwh")
        fc.append(float(f) if f is not None else 0.0)
        a = s.get("pv_actual_kwh")
        act.append(float(a) if a is not None else None)
    return fc, act


def _daylight_window(fc: list[float], act_z: list[float]) -> tuple[int, int]:
    """Inclusive [lo, hi] index range where either series clears the floor."""
    idx = [i for i in range(len(fc)) if max(fc[i], act_z[i]) > FLOOR_KWH]
    if not idx:
        return (0, -1)
    return (min(idx), max(idx))


def _centroid(vals: list[float], lo: int, hi: int) -> float | None:
    """Energy-weighted mean slot index over [lo, hi]; None if no energy."""
    num = den = 0.0
    for i in range(lo, hi + 1):
        v = max(0.0, vals[i])
        num += i * v
        den += v
    return num / den if den > 0 else None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    try:
        sx, sy = st.pstdev(xs), st.pstdev(ys)
    except st.StatisticsError:
        return None
    if sx == 0 or sy == 0:
        return None
    mx, my = st.fmean(xs), st.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    return cov / (sx * sy)


def _xcorr_best_lag(fc: list[float], act_z: list[float], lo: int, hi: int) -> int | None:
    """Integer-slot shift S of forecast that best matches realised, S∈[-2,+2].

    We correlate ``act[i]`` against ``fc[i + S]``; the S maximising correlation
    means a forecast slot S positions ahead looks like the actual now — i.e. the
    forecast is S slots LATE. Positive = forecast lags (matches centroid sign).
    """
    best_s: int | None = None
    best_r = -2.0
    for s in range(-MAX_LAG_SLOTS, MAX_LAG_SLOTS + 1):
        a_pairs: list[float] = []
        f_pairs: list[float] = []
        for i in range(lo, hi + 1):
            j = i + s
            if lo <= j <= hi:
                a_pairs.append(act_z[i])
                f_pairs.append(fc[j])
        r = _pearson(a_pairs, f_pairs)
        if r is not None and r > best_r:
            best_r, best_s = r, s
    return best_s


def _peak_idx(vals: list[float], lo: int, hi: int) -> int | None:
    best_i: int | None = None
    best_v = FLOOR_KWH
    for i in range(lo, hi + 1):
        if vals[i] > best_v:
            best_v, best_i = vals[i], i
    return best_i


def analyse_day(day_iso: str, base: str, insecure: bool, timeout: float) -> dict | None:
    try:
        data = _get_json(base, f"/api/v1/pv/today?date={day_iso}", insecure, timeout)
    except Exception as e:  # noqa: BLE001 — diagnostic, surface and skip
        print(f"  {day_iso}: fetch failed ({e})")
        return None
    slots = data.get("slots") or []
    if not slots:
        print(f"  {day_iso}: no slots")
        return None
    fc, act = _series(slots)
    if all(a is None for a in act):
        print(f"  {day_iso}: no realised data yet (future/partial) — skipped")
        return None
    act_z = [a if a is not None else 0.0 for a in act]
    day_kwh = sum(act_z)
    if day_kwh < MIN_DAY_KWH:
        print(f"  {day_iso}: realised {day_kwh:.1f} kWh < {MIN_DAY_KWH} — too small, skipped")
        return None

    lo, hi = _daylight_window(fc, act_z)
    if hi <= lo:
        print(f"  {day_iso}: no daylight window — skipped")
        return None

    # Slot-CENTRE resample proxy: the forecast value at slot_start+15min is the
    # midpoint of the two adjacent slot-START samples (the underlying drivers are
    # linearly interpolated between hourly anchors, so the midpoint of F[i] and
    # F[i+1] approximates F sampled at the slot centre). The last slot has no
    # successor → keep it as-is.
    fc_centre = [(fc[i] + fc[i + 1]) / 2 if i + 1 < len(fc) else fc[i] for i in range(len(fc))]

    c_f = _centroid(fc, lo, hi)            # current convention (slot-start)
    c_fc = _centroid(fc_centre, lo, hi)    # slot-centre (what the fix would do)
    c_a = _centroid(act_z, lo, hi)
    have = c_f is not None and c_fc is not None and c_a is not None
    total_min = (c_f - c_a) * SLOT_MIN if have else None          # measured F_start − realised
    mechanical_min = (c_f - c_fc) * SLOT_MIN if have else None    # removed by the fix (~+15 theory)
    residual_min = (c_fc - c_a) * SLOT_MIN if have else None      # genuine forecast-skill timing error

    best_s = _xcorr_best_lag(fc, act_z, lo, hi)
    xcorr_min = best_s * SLOT_MIN if best_s is not None else None

    pf, pa = _peak_idx(fc, lo, hi), _peak_idx(act_z, lo, hi)
    peak_min = (pf - pa) * SLOT_MIN if (pf is not None and pa is not None) else None

    return {
        "day": day_iso,
        "kwh": day_kwh,
        "centroid_min": total_min,
        "mechanical_min": mechanical_min,
        "residual_min": residual_min,
        "xcorr_min": xcorr_min,
        "peak_min": peak_min,
    }


def _fmt(v: float | None, suffix: str = "") -> str:
    return "  n/a" if v is None else f"{v:+5.0f}{suffix}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"API base URL (default: {DEFAULT_BASE})")
    ap.add_argument("--days", type=int, default=7, help="number of recent completed UTC days (default 7)")
    ap.add_argument("--date", action="append", help="explicit YYYY-MM-DD (repeatable); overrides --days")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    ap.add_argument("--timeout", type=float, default=20.0, help="per-request timeout seconds")
    args = ap.parse_args()

    if args.date:
        days = list(args.date)
    else:
        today = datetime.now(UTC).date()
        days = [(today - timedelta(days=n)).isoformat() for n in range(1, args.days + 1)]

    print(f"PV forecast-vs-realised time lag  (base={args.base})")
    print("Sign convention: positive = forecast LATER than realised (forecast lags)")
    print("total = mechanical (removed by slot-centre fix) + residual (genuine forecast skill)\n")
    print(f"  {'day':<12} {'kWh':>6}  {'total':>6}  {'mech':>6}  {'resid':>6}  {'xcorr':>7}  {'peak':>6}")
    print(f"  {'-'*12} {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*6}")

    rows: list[dict] = []
    for d in days:
        r = analyse_day(d, args.base, args.insecure, args.timeout)
        if r is None:
            continue
        rows.append(r)
        print(
            f"  {r['day']:<12} {r['kwh']:6.1f}  "
            f"{_fmt(r['centroid_min'],'m'):>6}  {_fmt(r['mechanical_min'],'m'):>6}  "
            f"{_fmt(r['residual_min'],'m'):>6}  {_fmt(r['xcorr_min'],'m'):>7}  {_fmt(r['peak_min'],'m'):>6}"
        )

    if not rows:
        print("\nNo qualifying days (need realised PV ≥ "
              f"{MIN_DAY_KWH} kWh). Try more --days or explicit --date.")
        return

    def _med(field: str) -> float | None:
        vals = [r[field] for r in rows if r[field] is not None]
        return st.median(vals) if vals else None

    med_total = _med("centroid_min")
    med_mech = _med("mechanical_min")
    med_resid = _med("residual_min")
    med_x = _med("xcorr_min")

    print(f"\n  Days analysed: {len(rows)}")
    print(f"  Median total centroid shift:  {_fmt(med_total, ' min')}")
    print(f"    ├─ mechanical (slot-start):  {_fmt(med_mech, ' min')}   ← removed by the fix")
    print(f"    └─ residual (forecast skill):{_fmt(med_resid, ' min')}   ← NOT addressed by the fix")
    print(f"  Median xcorr lag (coarse):    {_fmt(med_x, ' min')}")

    # Verdict gate — judged on the RESIDUAL (what survives the slot-centre fix).
    if med_total is None or med_resid is None or med_mech is None:
        verdict = "Inconclusive — no centroid signal."
    elif abs(med_total) <= 8:
        verdict = ("ALIGNED — forecast and realised are time-coincident within ~one quarter-slot. "
                   "No timezone or sampling shift to fix.")
    elif abs(med_resid) <= 8:
        verdict = (f"MECHANICAL — the {med_total:+.0f} min lag is almost entirely the slot-start "
                   f"sampling convention ({med_mech:+.0f} min); residual forecast-skill timing "
                   f"({med_resid:+.0f} min) is negligible. The slot-centre fix resolves it cleanly.")
    else:
        verdict = (f"MIXED — of the {med_total:+.0f} min lag, ~{med_mech:+.0f} min is the mechanical "
                   f"sampling convention (fixable) but ~{med_resid:+.0f} min is genuine forecast-skill "
                   "timing error the slot-centre fix won't remove. Fix is still worthwhile, but the "
                   "residual points at the forecast model/calibration (Quartz/Open-Meteo) separately.")
    print(f"\n  Verdict: {verdict}")


if __name__ == "__main__":
    main()
