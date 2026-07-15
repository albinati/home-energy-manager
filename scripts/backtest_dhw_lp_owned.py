#!/usr/bin/env python3
"""Backtest the LP-owned DHW regime (#714) against the committed pinned plan.

The offline half of the economic gate. For each recent optimizer run we replay the
committed inputs TWICE on the frozen snapshot and score both against the actual
published rates:

* the BASELINE arm pins the tank to a SIMULATION of what the fixed schedule actually
  does under the measured physics (src/dhw/baseline.py) — NOT the dhw_policy forecast,
  which plans ~2.4x the household's real DHW energy and would credit the LP-owned arm
  with phantom savings;
* the LP-OWNED arm lets the LP time the tank (force_dhw_lp_owned).

Both arms carry the same declared draw, the same physics and the same legionella
budget, so the delta is pure allocation value: WHEN each regime bought the heat.

It is NOT a promise of live savings (the replay re-solves rather than re-dispatching),
but it is the strongest read available before enabling anything.

REQUIRED ENVIRONMENT — both of these, or the result silently degrades:
    DAIKIN_CONTROL_MODE=active          # passive force-disables the LP-owned regime
                                        # -> every delta reads ~0 (false negative)
    OCTOPUS_EXPORT_TARIFF_CODE=E-1R-AGILE-OUTGOING-19-05-13-H
                                        # without it export prices are FLAT and the
                                        # LP cannot see WHEN exporting pays, which
                                        # distorts every timing decision
The script warns when either symptom is detected.

Usage (in the prod container, where the snapshots live):
    docker exec hem python -m scripts.backtest_dhw_lp_owned --days 21 --cadence first

``--cadence first`` scores one run per day (the first solve), which is the cleanest
day-level comparison. ``--cadence original`` scores every run.
"""
from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from zoneinfo import ZoneInfo

from src.config import config
from src.dhw import comfort as dhw_comfort
from src.scheduler.lp_replay import (
    list_run_ids_for_date,
    replay_run,
    resolve_run_id_for_date,
)


def _comfort_deficit_c(plan) -> float:
    """The single honest comfort signal: how many °C below its floor the LP-owned tank
    sits at any shower-window boundary. Zero means every shower was delivered; anything
    above means a cheaper plan that skimped — and does not count as a saving.

    Read straight off the plan's own tank trajectory and floors, so it is exactly what
    the household would feel, not a proxy for the objective's slack (which also carries
    the harmless over-temperature coast-down)."""
    if not getattr(plan, "dhw_lp_owned", False) or not plan.slot_starts_utc:
        return 0.0
    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    preset = (config.OPTIMIZATION_PRESET or "normal").strip().lower()
    floors = dhw_comfort.comfort_floors_for_slots(
        list(plan.slot_starts_utc), tz, preset=preset)
    worst = 0.0
    for i, floor in enumerate(floors):
        if floor is not None and i < len(plan.tank_temp_c):
            worst = max(worst, floor - plan.tank_temp_c[i])
    return worst


def _daterange(days: int) -> list[str]:
    today = datetime.now(UTC).date()
    return [(today - timedelta(days=d)).isoformat() for d in range(1, days + 1)]


def _run_ids_for(date: str, cadence: str) -> list[int]:
    if cadence == "first":
        rid = resolve_run_id_for_date(date, which="first")
        return [rid] if rid else []
    return [rid for rid, _run_at in list_run_ids_for_date(date)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--cadence", choices=["first", "original"], default="first")
    ap.add_argument("--mode", default="honest",
                    help="replay config mode (honest = snapshotted config)")
    args = ap.parse_args()

    per_day: dict[str, list[float]] = defaultdict(list)
    comfort_breaches = 0
    n_scored = 0
    n_skipped = 0
    both_costs: list[tuple[str, float, float]] = []

    from src.scheduler.lp_replay import _build_export_prices  # noqa: PLC0415

    for date in sorted(_daterange(args.days)):
        for rid in _run_ids_for(date, args.cadence):
            base = replay_run(rid, mode=args.mode, dhw_fixed_baseline=True)
            lp = replay_run(rid, mode=args.mode, force_dhw_lp_owned=True)
            if not base.ok or not lp.ok:
                n_skipped += 1
                continue
            # Cost of each plan priced at the actual published rates.
            delta_p = lp.replayed_cost_at_actual_p - base.replayed_cost_at_actual_p
            e_base = sum(base._replayed_plan.dhw_electric_kwh) if base._replayed_plan else 0.0
            e_lp = sum(lp._replayed_plan.dhw_electric_kwh) if lp._replayed_plan else 0.0
            deficit = _comfort_deficit_c(lp._replayed_plan)
            # A cheaper plan that skimped on a shower does NOT count. Exclude the day
            # from the £ tally AND flag it — comfort is not for sale.
            if deficit > 0.5:
                comfort_breaches += 1
                both_costs.append((date, base.replayed_cost_at_actual_p,
                                   lp.replayed_cost_at_actual_p, deficit, e_base, e_lp))
                continue
            per_day[date].append(delta_p)
            both_costs.append((date, base.replayed_cost_at_actual_p,
                               lp.replayed_cost_at_actual_p, 0.0, e_base, e_lp))
            n_scored += 1

    if not per_day:
        print("no scorable runs in window — no snapshots, or all Infeasible")
        return 1

    day_deltas = [statistics.mean(v) for v in per_day.values() if v]
    day_deltas.sort()
    total = sum(day_deltas)
    median = statistics.median(day_deltas)
    mean = statistics.mean(day_deltas)

    print(f"\nDHW LP-owned backtest — {len(day_deltas)} days, {n_scored} runs "
          f"({n_skipped} skipped)\n")
    print(f"{'date':12} {'fixed p':>9} {'lp p':>8} {'delta p':>9} {'fx kWh':>7} {'lp kWh':>7}  flags")
    for date in sorted({c[0] for c in both_costs}):
        costs = [c for c in both_costs if c[0] == date]
        b = statistics.mean(c[1] for c in costs)
        l = statistics.mean(c[2] for c in costs)
        worst_def = max(c[3] for c in costs)
        eb = statistics.mean(c[4] for c in costs)
        el = statistics.mean(c[5] for c in costs)
        flags = ""
        if worst_def > 0.5:
            flags += f"  COLD −{worst_def:.0f}°C (excluded)"
        # Heat parity: the LP may spend MORE (pre-heat + hold loss is a legitimate
        # strategy) but a big divergence means the arms are not comparable.
        if eb > 0.1 and not (0.5 <= (el / eb if eb else 1.0) <= 2.0):
            flags += "  ENERGY-DIVERGED"
        print(f"{date:12} {b:9.1f} {l:8.1f} {l - b:+9.1f} {eb:7.2f} {el:7.2f}{flags}")

    print(f"\nper-day delta (lp-owned − pinned), pence:")
    print(f"  median {median:+.1f}  mean {mean:+.1f}  total {total:+.1f} over {len(day_deltas)} days")
    print(f"  best day {day_deltas[0]:+.1f}  worst day {day_deltas[-1]:+.1f}")
    saved_days = sum(1 for d in day_deltas if d < -0.5)
    print(f"  LP-owned cheaper on {saved_days}/{len(day_deltas)} days")
    if comfort_breaches:
        print(f"  ⚠ {comfort_breaches} days EXCLUDED for a cold tank at a shower boundary "
              f"(>0.5 °C below floor). Comfort is not for sale — these are not savings.")
    annual = -median * 365 / 100.0
    print(f"\n  extrapolated (median × 365): £{annual:+.0f}/yr "
          f"(a rough read — winter differs; the live shadow is the real gate)")

    if abs(median) < 0.5 and abs(mean) < 0.5:
        print("\n  ⚠ every delta is ~0. Two known causes: DAIKIN_CONTROL_MODE is not "
              "'active' in the env (the LP-owned regime silently disables in passive), "
              "or the two arms are identical for another reason. Check the env before "
              "reading this as 'no value'.")
    probe = None
    try:
        from datetime import datetime as _dt, timedelta as _td

        # Slot-aligned (:00) — export rates key on exact slot starts.
        base_day = (_dt.now(UTC) - _td(days=3)).replace(
            minute=0, second=0, microsecond=0)
        probe = _build_export_prices(
            [base_day + i * _td(minutes=30) for i in range(8)])
    except Exception:
        probe = None
    if probe is None:
        print("  ⚠ export prices resolved FLAT (no OCTOPUS_EXPORT_TARIFF_CODE?). "
              "Timing conclusions are unreliable without per-slot export prices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
