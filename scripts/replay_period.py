#!/usr/bin/env python
"""One-shot replay across a date range against a DB snapshot.

Usage:
    DB_PATH=/tmp/hem-prod.db python scripts/replay_period.py \\
        2026-04-22 2026-04-23 2026-04-24 2026-04-25 2026-04-26 2026-04-27

Runs ``replay_lp_day(date, cadence='original', mode='honest')`` for each date
and prints a per-date table plus aggregate totals for "prior" (first half) vs
"recent" (second half) periods. ``mode='honest'`` keeps each day's snapshotted
config so we test today's solver code against that day's settings — answering
"do today's changes outperform what actually ran on day D?".

Negative ``delta_cost_at_actual_p`` means today's code would have saved more.
Positive means today's code would have cost more (regression candidate).

Read-only — no Fox / Daikin / network touches. Does not write to the DB.
"""
from __future__ import annotations

import os
import sys
from typing import Sequence


def _fmt_p(pence: float) -> str:
    return f"{pence/100:>+8.2f} £"


def _fmt_p_nosign(pence: float) -> str:
    return f"{pence/100:>8.2f} £"


def main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    dates = list(argv[1:])

    # Late imports — config reads DB_PATH at import time.
    from src.scheduler.lp_replay import replay_day

    rows: list[dict] = []
    for d in dates:
        result = replay_day(d, cadence="original", mode="honest")
        rows.append({
            "date": d,
            "ok": result.ok,
            "error": result.error,
            "n_recalcs": len(result.recalc_run_ids),
            "orig_cost_p": result.total_original_cost_p,
            "replayed_cost_p": result.total_replayed_cost_p,
            "delta_cost_p": result.total_delta_cost_p,
            "svt_p": result.total_svt_shadow_p,
            "orig_savings_p": result.total_original_savings_vs_svt_p,
            "replayed_savings_p": result.total_replayed_savings_vs_svt_p,
            "n_active_slots": len(result.active_slots),
        })

    print()
    print("Per-day replay (today's solver code, honest mode):")
    print(
        f"  {'Date':<12} {'recalcs':>8} {'slots':>6} "
        f"{'orig £':>10} {'replay £':>10} {'Δ £':>10} "
        f"{'orig vs SVT':>12} {'replay vs SVT':>14} {'status':<10}"
    )
    print("  " + "-" * 110)
    for r in rows:
        if not r["ok"]:
            print(f"  {r['date']:<12} {'-':>8} {'-':>6} {'-':>10} {'-':>10} {'-':>10} "
                  f"{'-':>12} {'-':>14} {'fail':<10}  ({r['error']})")
            continue
        print(
            f"  {r['date']:<12} {r['n_recalcs']:>8} {r['n_active_slots']:>6} "
            f"{_fmt_p_nosign(r['orig_cost_p']):>10} "
            f"{_fmt_p_nosign(r['replayed_cost_p']):>10} "
            f"{_fmt_p(r['delta_cost_p']):>10} "
            f"{_fmt_p(r['orig_savings_p']):>12} "
            f"{_fmt_p(r['replayed_savings_p']):>14}  ok"
        )

    ok = [r for r in rows if r["ok"]]
    if len(ok) >= 2:
        half = len(ok) // 2
        prior = ok[:half]
        recent = ok[half:]
        prior_delta = sum(r["delta_cost_p"] for r in prior)
        recent_delta = sum(r["delta_cost_p"] for r in recent)

        prior_orig_savings = sum(r["orig_savings_p"] for r in prior)
        prior_replay_savings = sum(r["replayed_savings_p"] for r in prior)
        recent_orig_savings = sum(r["orig_savings_p"] for r in recent)
        recent_replay_savings = sum(r["replayed_savings_p"] for r in recent)

        print()
        print(f"Aggregate — prior period ({prior[0]['date']}–{prior[-1]['date']}):")
        print(f"  orig savings vs SVT:    {_fmt_p(prior_orig_savings)}")
        print(f"  replay savings vs SVT:  {_fmt_p(prior_replay_savings)}")
        print(f"  Δ cost vs original:     {_fmt_p(prior_delta)}  "
              "(negative = today's code saves more)")

        print()
        print(f"Aggregate — recent period ({recent[0]['date']}–{recent[-1]['date']}):")
        print(f"  orig savings vs SVT:    {_fmt_p(recent_orig_savings)}")
        print(f"  replay savings vs SVT:  {_fmt_p(recent_replay_savings)}")
        print(f"  Δ cost vs original:     {_fmt_p(recent_delta)}  "
              "(negative = today's code saves more)")

        print()
        print("Verdict on 'did our recent changes outperform the previous setup?':")
        # If today's code beats original on BOTH periods (negative deltas), it's
        # a generic improvement. If it only beats on one, the answer is mixed.
        n_better = sum(1 for r in ok if r["delta_cost_p"] < 0)
        n_worse = sum(1 for r in ok if r["delta_cost_p"] > 0)
        print(f"  days today's code saves more:  {n_better}/{len(ok)}")
        print(f"  days today's code costs more:  {n_worse}/{len(ok)}")
        net = sum(r["delta_cost_p"] for r in ok)
        if net < 0:
            print(f"  net £ across all days:         {_fmt_p(net)}  → today's code wins")
        elif net > 0:
            print(f"  net £ across all days:         {_fmt_p(net)}  → today's code loses")
        else:
            print(f"  net £ across all days:         {_fmt_p(net)}  → tie")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
