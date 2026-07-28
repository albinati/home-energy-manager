#!/usr/bin/env python3
"""Fit the PV array's TWO-PLANE geometry against measured generation.

WHY
---
Over 2026-06-28..07-28 raw Quartz under-predicted total generation by ~20 %
(Sigma realised / Sigma forecast = 1.197), and the error is strongly shaped by
hour: on clear days the realised/forecast ratio runs ~0.60 at 05:00 UTC, climbs
to ~1.40 by 10:00 and then sits on a 1.21-1.44 plateau until 18:30. The per-hour
calibration table has been absorbing that with factors from 0.83 to 1.38, and
the closed-loop `pv_recent_bias` corrector is pinned against its clamp for five
midday hours. Both are symptoms of the same thing: the *physical* model handed
to Quartz does not describe this roof.

Prod currently declares::

    QUARTZ_OPEN_PLANES=[{"tilt": 35, "orientation": 225, "capacity_kwp": 2.25},
                        {"tilt": 10, "orientation": 180, "capacity_kwp": 2.25}]

i.e. south-west + south. The household describes the array as one FLAT group
and one group facing WEST. A plane modelled east of where it actually points
over-predicts the morning and under-predicts the afternoon — exactly the
observed signature. This script measures the geometry instead of assuming it.

WHAT IT DOES NOT DO
-------------------
It does not tune a per-hour correction. Fixing the geometry is upstream of the
calibration tables; if the geometry is right they should relax toward 1.0 on
their own. If after applying a winner they still carry +-30 %, the geometry is
still wrong and this script should be re-run with a wider grid — do not paper
over it downstream.

HONESTY RULES BAKED IN
----------------------
* **Out-of-sample split.** Candidates are ranked on FIT days and reported on
  HOLDOUT days. A geometry that only wins in-sample has learnt the weather, not
  the roof. The split is interleaved (every Nth day), not "most recent N", so
  the two sets see the same season.
* **The incumbent is scored the same way, on the same days, at the same forecast
  anchor.** A candidate that cannot beat the config already in prod is not a
  result. (A prior single-plane sweep concluded ~200 deg SSW — see
  ``project_quartz_site_level_killed``. Two planes is a different search and has
  to win on its own.)
* **Scale and shape are reported separately.** Free capacities can silently
  absorb a pure forecast bias and call it "a bigger array", so the run also
  reports the best geometry with total capacity PINNED to the declared value.
  If pinned and free disagree a lot, the win is scale, not orientation.

COST
----
Zero external API calls and zero vendor quota: the ``hem-quartz`` sidecar runs
on the same host (``QUARTZ_OPEN_URL``, internal bridge). Measured latency is
~0.1 s per call, and every (plane, day) response is cached to disk so re-runs
and grid widenings are nearly free.

NB Quartz is NOT linear in ``capacity_kwp`` (measured: doubling capacity moves
a slot by 1.35-4.8x, not 2x — it is an xgboost feature, not a scale factor), so
capacities must be searched, not solved for.

USAGE (prod: scripts/ is not in the image — copy to the state volume)
---------------------------------------------------------------------
    scp scripts/diag/pv_plane_fit.py root@<host>:/srv/hem/data/
    ssh root@<host> 'docker exec hem python /app/data/pv_plane_fit.py --days 60'

Read-only: it never writes to the database and never mutates config.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import defaultdict

UTC = dt.timezone.utc

DEFAULT_DB = os.getenv("DB_PATH", "/app/data/energy_state.db")
DEFAULT_QUARTZ = os.getenv("QUARTZ_OPEN_URL", "http://hem-quartz:8000")
DEFAULT_CACHE = "/app/data/.pv_plane_fit_cache.json"

# The declared array. Used for the capacity-pinned variant and as the sanity
# bound on a free-capacity winner.
DECLARED_TOTAL_KWP = 4.5

# The config in force — scored side by side with every candidate.
INCUMBENT = [
    {"tilt": 35.0, "orientation": 225.0, "capacity_kwp": 2.25},
    {"tilt": 10.0, "orientation": 180.0, "capacity_kwp": 2.25},
]

# Search grid, shaped by the household's description: one flat group, one west.
# Orientation is nearly irrelevant for a near-flat plane (cos of incidence is
# dominated by tilt), so the flat group only searches tilt.
FLAT_TILTS = [0.0, 5.0, 10.0, 15.0, 20.0, 25.0]
FLAT_ORIENT = 180.0
WEST_TILTS = [25.0, 30.0, 35.0, 40.0, 45.0, 50.0]
WEST_ORIENTS = [215.0, 230.0, 245.0, 255.0, 265.0, 275.0, 290.0]
# Capacity settled at 2.25/3.5 with a [1.5, 5.0] grid (winner not at an edge),
# so narrow it around that optimum and spend the search budget on tilt, which
# the edge guard flagged as truncated.
CAPACITIES = [2.0, 2.25, 2.5, 3.0, 3.5, 4.0]


# ---------------------------------------------------------------------------
# Measured generation
# ---------------------------------------------------------------------------


def load_realised(db_path: str, since: dt.date) -> dict[dt.datetime, float]:
    """Half-hourly realised kWh, keyed on slot start (UTC).

    ``pv_realtime_history.solar_power_kw`` is the inverter's PV power. Caveat
    worth remembering: issue #564 found Fox's *generation* field included
    battery discharge and inflated winter solar ~8x; that was fixed to use
    ``PVEnergyTotal``. If a fitted capacity comes back absurd, re-check this
    field before believing the array grew.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT captured_at, solar_power_kw FROM pv_realtime_history "
            "WHERE captured_at >= ? AND solar_power_kw IS NOT NULL",
            (since.isoformat(),),
        ).fetchall()
    finally:
        conn.close()

    samples: dict[dt.datetime, list[float]] = defaultdict(list)
    for captured_at, kw in rows:
        try:
            t = dt.datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        t = t.astimezone(UTC)
        slot = t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0)
        samples[slot].append(float(kw))
    # mean kW across the slot's samples, x 0.5 h -> kWh
    return {slot: sum(v) / len(v) * 0.5 for slot, v in samples.items()}


# ---------------------------------------------------------------------------
# Quartz replay (cached)
# ---------------------------------------------------------------------------


class Quartz:
    """Half-hourly kWh for one plane on one day, memoised on disk."""

    def __init__(self, base_url: str, cache_path: str, lat: float, lon: float) -> None:
        self.base = base_url.rstrip("/")
        self.cache_path = cache_path
        self.lat = lat
        self.lon = lon
        self.calls = 0
        try:
            with open(cache_path) as fh:
                self._cache: dict[str, dict[str, float]] = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self._cache = {}

    def save(self) -> None:
        try:
            with open(self.cache_path, "w") as fh:
                json.dump(self._cache, fh)
        except OSError as exc:
            print(f"  ! cache write failed ({exc}) — re-runs will be slow", file=sys.stderr)

    @staticmethod
    def _key(day: dt.date, plane: dict[str, float]) -> str:
        return (
            f"{day.isoformat()}|{plane['tilt']:.1f}|"
            f"{plane['orientation']:.1f}|{plane['capacity_kwp']:.4f}"
        )

    def plane_day(self, day: dt.date, plane: dict[str, float]) -> dict[str, float]:
        """``{slot_iso: kWh}`` for this plane over this UTC day."""
        key = self._key(day, plane)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        anchor = dt.datetime(day.year, day.month, day.day, 0, 0)
        body = {
            "site": {
                "latitude": self.lat,
                "longitude": self.lon,
                "capacity_kwp": plane["capacity_kwp"],
                "tilt": plane["tilt"],
                "orientation": plane["orientation"],
            },
            # Naive UTC, quarter-aligned: the sidecar anchors its 15-min grid
            # here, and every plane must share it or the buckets below would
            # average the planes instead of summing them (the #544 bug).
            "timestamp": anchor.isoformat(),
        }
        req = urllib.request.Request(
            f"{self.base}/forecast/",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = json.loads(resp.read().decode())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise RuntimeError(f"quartz call failed for {key}: {exc}") from exc
        self.calls += 1

        preds = (payload.get("predictions") or {}).get("power_kw") or {}
        # 15-min kW -> half-hour kWh (mean kW in the bucket x 0.5 h)
        bucket: dict[str, list[float]] = defaultdict(list)
        for ts_raw, kw in preds.items():
            try:
                ts = dt.datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            ts = ts.astimezone(UTC)
            if ts.date() != day:
                continue  # the 48 h horizon spills into tomorrow
            slot = ts.replace(minute=(ts.minute // 30) * 30, second=0, microsecond=0)
            bucket[slot.isoformat()].append(max(0.0, float(kw)))
        out = {k: sum(v) / len(v) * 0.5 for k, v in bucket.items()}
        self._cache[key] = out
        return out


def candidate_day(q: Quartz, day: dt.date, planes: list[dict[str, float]]) -> dict[str, float]:
    """Summed half-hourly kWh across all planes for one day."""
    total: dict[str, float] = defaultdict(float)
    for plane in planes:
        for slot, kwh in q.plane_day(day, plane).items():
            total[slot] += kwh
    return dict(total)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score(
    q: Quartz,
    planes: list[dict[str, float]],
    days: list[dt.date],
    realised: dict[dt.datetime, float],
) -> dict[str, float]:
    """Slot MAE (kWh) plus the daily-total bias, over ``days``."""
    abs_err = 0.0
    n = 0
    sum_act = 0.0
    sum_fc = 0.0
    for day in days:
        fc = candidate_day(q, day, planes)
        for slot_iso, kwh in fc.items():
            slot = dt.datetime.fromisoformat(slot_iso)
            act = realised.get(slot)
            if act is None:
                continue
            abs_err += abs(act - kwh)
            n += 1
            sum_act += act
            sum_fc += kwh
    if not n:
        return {"mae": float("inf"), "ratio": 0.0, "n": 0}
    return {
        "mae": abs_err / n,
        "ratio": (sum_act / sum_fc) if sum_fc > 0 else 0.0,
        "n": n,
        "sum_act": sum_act,
        "sum_fc": sum_fc,
    }


def hourly_shape(
    q: Quartz,
    planes: list[dict[str, float]],
    days: list[dt.date],
    realised: dict[dt.datetime, float],
) -> dict[int, tuple[float, float]]:
    """Per-UTC-hour (realised, forecast) kWh sums — the residual's shape.

    A flat ratio across hours means the geometry is right and any remaining gap
    is pure scale. A sloped ratio means the orientation is still off, and no
    capacity change can fix it.
    """
    agg: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for day in days:
        for slot_iso, kwh in candidate_day(q, day, planes).items():
            slot = dt.datetime.fromisoformat(slot_iso)
            act = realised.get(slot)
            if act is None:
                continue
            agg[slot.hour][0] += act
            agg[slot.hour][1] += kwh
    return {h: (v[0], v[1]) for h, v in sorted(agg.items())}


def fmt_planes(planes: list[dict[str, float]]) -> str:
    return json.dumps(
        [
            {
                "tilt": p["tilt"],
                "orientation": p["orientation"],
                "capacity_kwp": p["capacity_kwp"],
            }
            for p in planes
        ]
    )


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60, help="lookback window (days)")
    ap.add_argument("--holdout-every", type=int, default=3,
                    help="every Nth day is held out for out-of-sample scoring")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--quartz", default=DEFAULT_QUARTZ)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--lat", type=float, default=float(os.getenv("WEATHER_LAT", "51.4927")))
    ap.add_argument("--lon", type=float, default=float(os.getenv("WEATHER_LON", "-0.2628")))
    ap.add_argument("--top", type=int, default=8, help="candidates to print")
    args = ap.parse_args()

    today = dt.datetime.now(UTC).date()
    since = today - dt.timedelta(days=args.days)
    realised = load_realised(args.db, since)
    if not realised:
        print("No realised PV in pv_realtime_history — nothing to fit.", file=sys.stderr)
        return 1

    # Days with enough daylight coverage to be worth scoring.
    per_day: dict[dt.date, float] = defaultdict(float)
    per_day_n: dict[dt.date, int] = defaultdict(int)
    for slot, kwh in realised.items():
        per_day[slot.date()] += kwh
        per_day_n[slot.date()] += 1
    days = sorted(
        d for d in per_day
        if d < today and per_day_n[d] >= 20 and per_day[d] > 1.0
    )
    if len(days) < 8:
        print(f"Only {len(days)} usable days — too few to split. Widen --days.", file=sys.stderr)
        return 1

    holdout = [d for i, d in enumerate(days) if i % args.holdout_every == 0]
    fit = [d for d in days if d not in set(holdout)]

    print(f"Window      : {days[0]} .. {days[-1]}  ({len(days)} usable days)")
    print(f"Fit / holdout: {len(fit)} / {len(holdout)} days "
          f"(interleaved every {args.holdout_every}th)")
    print(f"Quartz      : {args.quartz}   cache={args.cache}")
    print()

    q = Quartz(args.quartz, args.cache, args.lat, args.lon)

    # ---- incumbent, scored on exactly the same days and anchor ----
    inc_fit = score(q, INCUMBENT, fit, realised)
    inc_hold = score(q, INCUMBENT, holdout, realised)
    print("INCUMBENT (currently in /srv/hem/.env)")
    print(f"  {fmt_planes(INCUMBENT)}")
    print(f"  fit    : MAE {inc_fit['mae']:.4f} kWh/slot   act/fc {inc_fit['ratio']:.3f}")
    print(f"  holdout: MAE {inc_hold['mae']:.4f} kWh/slot   act/fc {inc_hold['ratio']:.3f}")
    q.save()
    print()

    # ---- grid ----
    combos = []
    for ft in FLAT_TILTS:
        for wt in WEST_TILTS:
            for wo in WEST_ORIENTS:
                for fc_kwp in CAPACITIES:
                    for wc_kwp in CAPACITIES:
                        combos.append((ft, wt, wo, fc_kwp, wc_kwp))
    print(f"Searching {len(combos)} candidates "
          f"({len(FLAT_TILTS)}x{len(WEST_TILTS)}x{len(WEST_ORIENTS)} geometries "
          f"x {len(CAPACITIES)}^2 capacities)...")

    results = []
    for i, (ft, wt, wo, fc_kwp, wc_kwp) in enumerate(combos):
        planes = [
            {"tilt": ft, "orientation": FLAT_ORIENT, "capacity_kwp": fc_kwp},
            {"tilt": wt, "orientation": wo, "capacity_kwp": wc_kwp},
        ]
        try:
            s = score(q, planes, fit, realised)
        except RuntimeError as exc:
            print(f"  ! {exc}", file=sys.stderr)
            q.save()
            return 2
        results.append((s["mae"], s["ratio"], planes))
        if (i + 1) % 200 == 0:
            print(f"  ...{i + 1}/{len(combos)}  ({q.calls} quartz calls so far)")
            q.save()
    q.save()
    results.sort(key=lambda r: r[0])
    print(f"  done — {q.calls} quartz calls this run\n")

    # ---- report ----
    print(f"TOP {args.top} BY FIT MAE (holdout re-scored independently)")
    print(f"  {'MAE_fit':>8} {'MAE_hold':>9} {'act/fc_h':>9}  planes")
    for mae, _ratio, planes in results[: args.top]:
        h = score(q, planes, holdout, realised)
        print(f"  {mae:8.4f} {h['mae']:9.4f} {h['ratio']:9.3f}  {fmt_planes(planes)}")
    q.save()

    best_mae, _, best = results[0]
    best_hold = score(q, best, holdout, realised)

    # ---- capacity-pinned variant: separates shape from scale ----
    pinned = [
        r for r in results
        if abs(sum(p["capacity_kwp"] for p in r[2]) - DECLARED_TOTAL_KWP) < 1e-6
    ]
    print()
    if pinned:
        p_mae, _, p_planes = pinned[0]
        p_hold = score(q, p_planes, holdout, realised)
        print(f"BEST WITH TOTAL CAPACITY PINNED TO {DECLARED_TOTAL_KWP} kWp")
        print(f"  {fmt_planes(p_planes)}")
        print(f"  fit MAE {p_mae:.4f}   holdout MAE {p_hold['mae']:.4f}   "
              f"act/fc {p_hold['ratio']:.3f}")
        print("  If this is close to the free-capacity winner, the win is SHAPE")
        print("  (orientation). If the free winner is much better, the win is")
        print("  SCALE — and a capacity above nameplate deserves suspicion of the")
        print("  measurement (see #564) before it is believed.")
        q.save()

    print()
    total_kwp = sum(p["capacity_kwp"] for p in best)
    print("WINNER (free capacity)")
    print(f"  {fmt_planes(best)}")
    print(f"  total capacity {total_kwp:.2f} kWp (declared {DECLARED_TOTAL_KWP})")
    print(f"  holdout MAE {best_hold['mae']:.4f} vs incumbent {inc_hold['mae']:.4f}  "
          f"({(1 - best_hold['mae'] / inc_hold['mae']) * 100:+.1f} %)")
    print(f"  holdout act/fc {best_hold['ratio']:.3f} vs incumbent {inc_hold['ratio']:.3f} "
          "(1.000 is unbiased)")
    if best_hold["mae"] >= inc_hold["mae"]:
        print("  >> DOES NOT BEAT THE INCUMBENT OUT OF SAMPLE. Do not apply it.")
    if total_kwp > DECLARED_TOTAL_KWP * 1.25:
        print(f"  >> Fitted capacity is {total_kwp / DECLARED_TOTAL_KWP:.2f}x nameplate — "
              "check the realised field before believing this.")

    # A winner sitting on a grid boundary means the search was TRUNCATED, not
    # that the optimum was found: the true best may lie outside. Saying so is
    # the difference between a measurement and a number that merely looks like
    # one — widen the grid and re-run (the cache makes that cheap).
    edges = []
    for i, p in enumerate(best):
        if p["capacity_kwp"] in (min(CAPACITIES), max(CAPACITIES)):
            edges.append(f"plane{i} capacity={p['capacity_kwp']}")
    if best[0]["tilt"] in (min(FLAT_TILTS), max(FLAT_TILTS)):
        edges.append(f"flat tilt={best[0]['tilt']}")
    if best[1]["tilt"] in (min(WEST_TILTS), max(WEST_TILTS)):
        edges.append(f"west tilt={best[1]['tilt']}")
    if best[1]["orientation"] in (min(WEST_ORIENTS), max(WEST_ORIENTS)):
        edges.append(f"west orientation={best[1]['orientation']}")
    if edges:
        print(f"  >> WINNER SITS ON A GRID EDGE ({', '.join(edges)}) — the search was")
        print("     truncated. Widen that axis and re-run before believing this is")
        print("     the optimum.")

    print()
    print("HOLDOUT RESIDUAL SHAPE (per UTC hour; flat ratio = geometry is right)")
    print(f"  {'hour':>5} {'act':>8} {'fc':>8} {'ratio':>7}   incumbent_ratio")
    win_shape = hourly_shape(q, best, holdout, realised)
    inc_shape = hourly_shape(q, INCUMBENT, holdout, realised)
    for h in sorted(win_shape):
        a, f = win_shape[h]
        ia, if_ = inc_shape.get(h, (0.0, 0.0))
        if f <= 0.05:
            continue
        print(f"  {h:5d} {a:8.2f} {f:8.2f} {a / f:7.3f}   "
              f"{(ia / if_ if if_ > 0 else 0):.3f}")
    q.save()

    print()
    print("To apply (no code change — QUARTZ_OPEN_PLANES is already read and summed):")
    print(f"  QUARTZ_OPEN_PLANES={fmt_planes(best)}")
    print("  then: systemctl restart hem")
    print("  then RETRAIN the calibration tables and CLEAR pv_recent_bias — both")
    print("  were trained against the old geometry and are poisoned by it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
