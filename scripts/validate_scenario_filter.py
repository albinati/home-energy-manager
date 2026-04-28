#!/usr/bin/env python
"""Regression validator: would the scenario-LP filter have made or lost money historically?

For each LP run in the last N days that planned ``peak_export`` slots, this script:

1. Replays the LP on the snapshot inputs in **forward mode** (current code on
   past inputs) via :func:`src.scheduler.lp_replay.replay_run`.
2. Solves the optimistic + pessimistic scenarios on the replayed plan via
   :func:`src.scheduler.scenarios.solve_scenarios_with_nominal`.
3. Applies :func:`src.scheduler.lp_dispatch.filter_robust_peak_export`.
4. For each ``peak_export`` slot the filter would have **dropped**, computes
   a £ delta under a conservative proxy:

   ``delta_p = planned_export_kwh * (terminal_soc_value_p - actual_export_price_p)``

   Positive ``delta_p`` = the saved battery was worth more than the lost grid
   feed at this slot (filter helped).
   Negative ``delta_p`` = the filter cost us money (over-conservative).

5. Aggregates per-run + total. **Exits non-zero** when the 30-day total goes
   below ``--fail-below-pence`` (default −500 p / −£5), making this script a
   pre-merge gate against shipping a filter that hurts realised £.

The terminal-SoC proxy underestimates the true value of saved battery during
peak windows (where the kWh would actually have displaced peak imports), so
the validator errs on the side of NOT blocking false-positively. If you want
a stricter gate, raise ``LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH`` via env or
lower ``--fail-below-pence``.

Usage:

    DB_PATH=/tmp/hem-prod.db python scripts/validate_scenario_filter.py \\
        --days 30 --fail-below-pence -500 --json /tmp/filter_validation.json

    # Or in CI / pre-merge:
    DB_PATH=/srv/hem/data/energy_state.db python scripts/validate_scenario_filter.py
    echo "exit code: $?"   # 0 = filter neutral or favourable; 1 = filter regressed

Read-only — no Fox / Daikin / network touches. Safe to run against a prod DB
copy. Each scenario re-solve takes ~3 s; expect ~5 minutes for 30 days × 5
runs/day.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SlotDelta:
    slot_time_utc: str
    planned_export_kwh: float
    actual_export_price_p: float
    terminal_value_p: float
    saved_value_p: float
    lost_revenue_p: float
    net_delta_p: float
    decision_reason: str


@dataclass
class RunValidation:
    run_id: int
    plan_date: str
    run_at_utc: str
    peak_export_slots_total: int
    peak_export_slots_dropped: int
    peak_export_slots_committed: int
    aggregate_delta_p: float
    slot_deltas: list[SlotDelta] = field(default_factory=list)
    error: str | None = None


@dataclass
class ValidationReport:
    days_back: int
    runs_validated: int
    runs_skipped: int
    aggregate_delta_p: float
    total_slots_dropped: int
    total_slots_committed: int
    runs: list[RunValidation] = field(default_factory=list)
    fail_threshold_p: float = -500.0

    @property
    def passed(self) -> bool:
        return self.aggregate_delta_p >= self.fail_threshold_p


def _runs_with_peak_export(days_back: int) -> list[dict[str, Any]]:
    """Return optimizer_log rows from the last N days where the LP plan
    included at least one peak_export slot.

    A row qualifies if its ``lp_solution_snapshot`` has any slot with
    discharge_kwh>0 AND export_kwh>0 — that's the LP-level signature of
    peak_export, regardless of whether the dispatch path committed it.
    """
    from src import db

    cutoff = (datetime.now(UTC) - timedelta(days=int(days_back))).isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT o.id, o.run_at, i.plan_date
              FROM optimizer_log o
              JOIN lp_inputs_snapshot i ON i.run_id = o.id
              JOIN lp_solution_snapshot s ON s.run_id = o.id
             WHERE o.run_at >= ?
               AND s.discharge_kwh > 0.05
               AND s.export_kwh > 0.05
             ORDER BY o.run_at ASC
            """,
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _actual_export_price_p(slot_time_utc: str) -> float | None:
    """Lookup actual Octopus Outgoing rate for a given slot start.

    Falls back to the flat ``EXPORT_RATE_PENCE`` constant when the export
    tariff isn't configured / hadn't been fetched at the time. Returns None
    only when no fallback is sensible (shouldn't happen in practice).
    """
    from src import db
    from src.config import config

    rows = db.get_agile_export_rates_in_range(slot_time_utc, slot_time_utc)
    for r in rows:
        if str(r.get("valid_from", "")).startswith(slot_time_utc[:19]):
            try:
                return float(r["value_inc_vat"])
            except (KeyError, TypeError, ValueError):
                pass
    return float(config.EXPORT_RATE_PENCE)


def _validate_run(run_id: int, plan_date: str, run_at_utc: str) -> RunValidation:
    """Replay one LP run, run scenarios, apply filter, score per-slot deltas."""
    from src.config import config
    from src.scheduler.lp_dispatch import filter_robust_peak_export
    from src.scheduler.lp_optimizer import LpInitialState
    from src.scheduler.lp_replay import (
        _build_export_prices,
        _reconstruct_weather,
        replay_run,
    )
    from src.scheduler.scenarios import solve_scenarios_with_nominal
    from src import db
    from zoneinfo import ZoneInfo

    rv = RunValidation(
        run_id=run_id,
        plan_date=plan_date,
        run_at_utc=run_at_utc,
        peak_export_slots_total=0,
        peak_export_slots_dropped=0,
        peak_export_slots_committed=0,
        aggregate_delta_p=0.0,
    )

    replay = replay_run(run_id, mode="forward")
    if not replay.ok:
        rv.error = f"replay failed: {replay.error}"
        return rv
    plan = replay._replayed_plan
    if plan is None:
        rv.error = "replay missing _replayed_plan"
        return rv

    # Reconstruct the inputs needed for the scenario re-solves. The replay
    # already loaded these once; re-derive via the same helpers so we don't
    # leak any private replay state.
    inputs = db.get_lp_inputs(run_id) or {}
    snap_slots = db.get_lp_solution_slots(run_id)
    if not snap_slots:
        rv.error = "no lp_solution_snapshot rows"
        return rv

    slot_starts = [
        datetime.fromisoformat(str(s["slot_time_utc"]).replace("Z", "+00:00"))
        for s in snap_slots
    ]
    price_pence = [float(s.get("price_p") or 0.0) for s in snap_slots]
    try:
        base_load = [float(x) for x in json.loads(inputs.get("base_load_json") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        base_load = []
    if len(base_load) != len(slot_starts):
        rv.error = "base_load length mismatch"
        return rv

    weather, weather_fidelity = _reconstruct_weather(str(inputs.get("run_at_utc") or ""), slot_starts)
    if weather_fidelity == "missing":
        rv.error = "weather snapshot missing"
        return rv

    initial = LpInitialState(
        soc_kwh=float(inputs.get("soc_initial_kwh") or 0.0),
        tank_temp_c=float(inputs.get("tank_initial_c") or 45.0),
        indoor_temp_c=float(inputs.get("indoor_initial_c") or 20.0),
        soc_source=str(inputs.get("soc_source") or "snapshot"),
        tank_source=str(inputs.get("tank_source") or "snapshot"),
        indoor_source=str(inputs.get("indoor_source") or "snapshot"),
    )
    export_prices = _build_export_prices(slot_starts)
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    mco = float(inputs.get("micro_climate_offset_c") or 0.0)

    # Scenarios on the replayed plan.
    scenarios_dict = solve_scenarios_with_nominal(
        nominal=plan,
        slot_starts_utc=slot_starts,
        price_pence=price_pence,
        base_load_kwh=base_load,
        weather=weather,
        initial=initial,
        tz=tz,
        micro_climate_offset_c=mco,
        export_price_pence=export_prices,
    )

    # Apply the filter.
    _slots, decisions = filter_robust_peak_export(plan, dict(scenarios_dict))

    terminal_value_p = float(getattr(config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 5.0))

    for d in decisions:
        if d["lp_kind"] != "peak_export":
            continue
        rv.peak_export_slots_total += 1
        if d["committed"]:
            rv.peak_export_slots_committed += 1
            continue
        # Slot was dropped — compute the £ delta under the proxy.
        rv.peak_export_slots_dropped += 1
        slot_iso = d["slot_time_utc"]
        # Find planned export from the replayed plan.
        try:
            idx = slot_starts.index(
                datetime.fromisoformat(slot_iso.replace("Z", "+00:00"))
            )
            planned_export = float(plan.export_kwh[idx])
        except (ValueError, IndexError):
            continue
        actual_price_p = _actual_export_price_p(slot_iso) or float(config.EXPORT_RATE_PENCE)
        saved_p = planned_export * terminal_value_p
        lost_p = planned_export * actual_price_p
        net_p = saved_p - lost_p
        rv.aggregate_delta_p += net_p
        rv.slot_deltas.append(
            SlotDelta(
                slot_time_utc=slot_iso,
                planned_export_kwh=round(planned_export, 3),
                actual_export_price_p=round(actual_price_p, 2),
                terminal_value_p=terminal_value_p,
                saved_value_p=round(saved_p, 2),
                lost_revenue_p=round(lost_p, 2),
                net_delta_p=round(net_p, 2),
                decision_reason=d["reason"],
            )
        )

    return rv


def validate(days_back: int = 30, fail_threshold_p: float = -500.0) -> ValidationReport:
    rows = _runs_with_peak_export(days_back)
    report = ValidationReport(
        days_back=days_back,
        runs_validated=0,
        runs_skipped=0,
        aggregate_delta_p=0.0,
        total_slots_dropped=0,
        total_slots_committed=0,
        fail_threshold_p=fail_threshold_p,
    )
    for row in rows:
        rv = _validate_run(int(row["id"]), str(row["plan_date"]), str(row["run_at"]))
        if rv.error:
            report.runs_skipped += 1
            logger.info("run_id=%d skipped: %s", rv.run_id, rv.error)
            continue
        report.runs_validated += 1
        report.aggregate_delta_p += rv.aggregate_delta_p
        report.total_slots_dropped += rv.peak_export_slots_dropped
        report.total_slots_committed += rv.peak_export_slots_committed
        report.runs.append(rv)
    return report


def _print_report(report: ValidationReport) -> None:
    print()
    print("=" * 70)
    print(f"Scenario-filter validation — last {report.days_back} days")
    print("=" * 70)
    print(f"Runs validated:        {report.runs_validated}")
    print(f"Runs skipped:          {report.runs_skipped}")
    print(f"peak_export committed: {report.total_slots_committed}")
    print(f"peak_export dropped:   {report.total_slots_dropped}")
    print()
    print("Per-run delta (negative = filter cost £; positive = filter saved £):")
    print(f"{'run_id':>8}  {'plan_date':>10}  {'dropped':>7}  {'delta £':>10}")
    for rv in report.runs:
        if rv.peak_export_slots_dropped == 0:
            continue
        print(
            f"{rv.run_id:>8}  {rv.plan_date:>10}  "
            f"{rv.peak_export_slots_dropped:>7d}  "
            f"{rv.aggregate_delta_p / 100:>+9.2f}"
        )
    print("-" * 70)
    print(
        f"AGGREGATE: {report.aggregate_delta_p / 100:+.2f} £   "
        f"(threshold: {report.fail_threshold_p / 100:+.2f} £)"
    )
    if report.passed:
        print(f"VERDICT: PASS — filter is within tolerance.")
    else:
        print(f"VERDICT: FAIL — filter regressed beyond threshold; investigate.")
    print()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Regression-validate the scenario-LP filter against past data.",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Replay window in days (default 30).",
    )
    parser.add_argument(
        "--fail-below-pence", type=float, default=-500.0,
        help=(
            "Aggregate Δpence below which the script exits non-zero. "
            "Default −500 (= −£5). Set to a more negative value to relax the "
            "gate; set to 0 for strict 'never lose money' enforcement."
        ),
    )
    parser.add_argument(
        "--json", type=str, default=None,
        help="Optional path to write the full report as JSON.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress the human-readable table; useful in CI when --json is set.",
    )
    args = parser.parse_args(argv[1:])

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    report = validate(days_back=args.days, fail_threshold_p=args.fail_below_pence)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(
                {
                    "days_back": report.days_back,
                    "runs_validated": report.runs_validated,
                    "runs_skipped": report.runs_skipped,
                    "aggregate_delta_p": report.aggregate_delta_p,
                    "total_slots_dropped": report.total_slots_dropped,
                    "total_slots_committed": report.total_slots_committed,
                    "fail_threshold_p": report.fail_threshold_p,
                    "passed": report.passed,
                    "runs": [
                        {
                            **{k: v for k, v in asdict(rv).items() if k != "slot_deltas"},
                            "slot_deltas": [asdict(s) for s in rv.slot_deltas],
                        }
                        for rv in report.runs
                    ],
                },
                f,
                indent=2,
            )

    if not args.quiet:
        _print_report(report)

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
