"""What does the pessimistic charge floor COST? (read-only diagnostic)

The #684 audit concluded "leave the charge floor as-is" and named a monthly
`floor_worth_it.py` tripwire as the winter check. It was never written. This is it.

READ THIS BEFORE QUOTING A NUMBER
---------------------------------
`insurance_cost_pence` (logged by ``_apply_pessimistic_charge_floor``) is the LP's
**objective delta**, not money:

  * it includes NON-CASH penalties — cycle cost, the piecewise inverter-stress
    term, TV smoothing, terminal-SoC value;
  * it spans the whole ``LP_HORIZON_HOURS`` window (default **48 h**), so it is
    not a per-day figure.

An earlier version of this script treated it as a daily cash premium and
annualised it ("£7.89/yr premium, £4.93/yr wasted"). Those figures were wrong and
are retracted — see #684. It is now reported honestly: per solve, scaled to a
day-equivalent, and labelled an objective delta.

WHAT THIS SCRIPT CAN AND CANNOT TELL YOU
----------------------------------------
It CAN tell you how often the floor binds and how large its objective delta is —
enough to answer *"is the floor cheap?"*, which is the question that decided #684.

It CANNOT tell you whether the floor SAVED money. That needs a counterfactual
replay of the same day without the floor (``scripts/research/winter_floor_value.py``
— and read its caveats, its settlement layer is biased toward the floor).

In particular, the old **"CANNOT PAY OFF"** classifier (battery full pre-peak while
PV was still exporting ⇒ "it would have filled from free PV anyway") had **reverse
causality**: the floor grid-charges the battery overnight, so a battery that is
full at midday and spilling PV is *exactly what you observe when the floor
pre-filled it and left no headroom*. The export is plausibly an EFFECT of the
floor, not evidence against it. That classifier is gone. The observation it was
built on remains — reported as an observation, not a verdict.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import config  # noqa: E402


def _open_ro(db_path: str) -> sqlite3.Connection:
    """Read-only connection — this runs against PROD. A plain ``connect()`` takes
    write locks and creates ``-wal``/``-shm`` files on the live DB."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(description="Charge-floor cost tripwire (read-only).")
    ap.add_argument("--db", default=config.DB_PATH, help="path to energy_state.db")
    ap.add_argument("--days", type=int, default=40)
    args = ap.parse_args()

    c = _open_ro(args.db)

    # Read config, don't guess. The old script hardcoded a 3-min telemetry cadence
    # (real default 5) and a 15:00-20:00 UTC peak (config: 16:00-19:00 LOCAL), so
    # its kWh figures ran ~40% low and its peak window was 2 h too wide.
    peak_start = str(config.SCHEDULER_PEAK_START)
    peak_end = str(config.SCHEDULER_PEAK_END)
    horizon_h = float(config.LP_HORIZON_HOURS)
    reserve = float(config.MIN_SOC_RESERVE_PERCENT)
    near_empty = reserve + 10.0

    print(f"db={args.db}   peak={peak_start}-{peak_end} local   "
          f"horizon={horizon_h:.0f} h   reserve={reserve:.0f}%")
    print("NB the floor's `objective delta` is NOT cash — see the docstring.\n")

    # Keyed by plan_date (the day the plan is FOR), not the run's own UTC date: a
    # late-evening replan's 48 h horizon is mostly about TOMORROW's peak, so
    # scoring it against today's telemetry misattributes it.
    per_day: dict[str, list[float]] = {}
    binding: dict[str, list[int]] = {}
    solves: dict[str, int] = {}
    for r in c.execute(
        """SELECT plan_date, exogenous_snapshot_json
           FROM lp_inputs_snapshot
           WHERE run_at_utc >= date('now', ?) AND plan_date IS NOT NULL
           ORDER BY run_id""",
        (f"-{args.days} days",),
    ):
        d = str(r["plan_date"])
        solves[d] = solves.get(d, 0) + 1
        try:
            cf = (json.loads(r["exogenous_snapshot_json"] or "{}") or {}).get("pess_charge_floor")
        except Exception:
            cf = None
        if not cf:
            continue        # the key is only written when the floor actually bound
        per_day.setdefault(d, []).append(float(cf.get("insurance_cost_pence") or 0.0))
        binding.setdefault(d, []).append(int(cf.get("binding_slots") or 0))

    if not per_day:
        print("the floor never bound in this window — nothing to report")
        return 0

    day_equiv = 24.0 / horizon_h        # a 48 h objective delta -> day-equivalent

    print(f"{'plan_date':12} {'solves':>7} {'bound':>6} {'obj_delta/solve':>16} "
          f"{'day-equiv':>10} {'binding_slots':>14}")
    equivs: list[float] = []
    for d in sorted(per_day):
        mean_delta = statistics.mean(per_day[d])
        eq = mean_delta * day_equiv
        equivs.append(eq)
        print(f"{d:12} {solves.get(d, 0):>7} {len(per_day[d]):>6} "
              f"{mean_delta:>15.2f}p {eq:>9.2f}p {statistics.mean(binding[d]):>14.1f}")

    total_solves = sum(solves.values())
    bound_solves = sum(len(v) for v in per_day.values())
    print(f"\n--- floor bound on {len(per_day)} of {len(solves)} planned day(s) "
          f"({bound_solves}/{total_solves} solves) ---")
    print(f"mean objective delta : {statistics.mean(equivs):.2f}p / day-equivalent")
    print(f"median               : {statistics.median(equivs):.2f}p")
    print(f"max                  : {max(equivs):.2f}p")
    print("\nThis is the number that decided #684: the floor is CHEAP.")
    print("It is NOT cash and NOT a saving — do not annualise it as money.")

    # --- Observation, deliberately NOT a verdict.
    print(f"\n=== observed SoC around the peak ({peak_start}-{peak_end} local) ===")
    print("A battery that is full pre-peak while PV still exports is NOT proof the")
    print("floor was useless: the floor grid-charges overnight, so that is also")
    print("exactly what a floor-FILLED battery looks like. Causality runs both ways;")
    print("only a counterfactual replay can separate them.")
    print(f"\n{'plan_date':12} {'minSoC@peak':>12} {'maxSoC pre-peak':>16}")
    for d in sorted(per_day):
        mn = c.execute(
            """SELECT MIN(soc_pct) v FROM pv_realtime_history
               WHERE date(captured_at) = ? AND time(captured_at) BETWEEN ? AND ?""",
            (d, f"{peak_start}:00", f"{peak_end}:00"),
        ).fetchone()["v"]
        mx = c.execute(
            """SELECT MAX(soc_pct) v FROM pv_realtime_history
               WHERE date(captured_at) = ? AND time(captured_at) < ?""",
            (d, f"{peak_start}:00"),
        ).fetchone()["v"]
        flag = "   <-- ran near empty at peak: the floor earned its keep here" \
            if (mn is not None and mn <= near_empty) else ""
        print(f"{d:12} {(f'{mn:.0f}%' if mn is not None else 'n/a'):>12} "
              f"{(f'{mx:.0f}%' if mx is not None else 'n/a'):>16}{flag}")

    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
