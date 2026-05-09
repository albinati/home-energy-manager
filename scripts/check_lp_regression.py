#!/usr/bin/env python
"""Pre-merge regression gate: did this LP-touching commit make the planner worse?

For each historical day in the last ``--days`` (default 14), replays the LP via
:func:`src.scheduler.lp_replay.replay_day` in **forward mode** (current code on
the day's snapshot inputs) and scores the planned dispatch against the actually-
published Agile rates. Compares the **total replayed cost** across the window
against a frozen baseline pinned in
``tests/fixtures/lp_regression_baseline.json``.

**Exit code semantics:**

* 0 — total replayed cost ≤ baseline + ``--fail-above-pence`` threshold (default
  0 p over the comparable window). The new code is at least as good as the
  baseline overall. Individual moments may be worse; aggregate cost must not be.
* 1 — the new code is worse than the comparable baseline, the baseline is
  missing, or baseline replay coverage is incomplete. **Do not merge** until
  the regression or replay data is fixed.

**Usage:**

```bash
# Pre-merge gate (against your locally-mounted prod DB snapshot):
DB_PATH=/path/to/prod-snapshot.db .venv/bin/python scripts/check_lp_regression.py

# After proving a new LP strategy is better or equal on an agreed replay set:
DB_PATH=/path/to/prod-snapshot.db .venv/bin/python scripts/check_lp_regression.py \
    --refresh-baseline
git add tests/fixtures/lp_regression_baseline.json && git commit
```

**What the script does NOT cover:** dispatch-layer regressions (the scenario LP
robustness filter dropping too many slots) — that's the job of
``validate_scenario_filter.py``. Solver-level regressions are this script's
domain.

**Read-only.** No Fox / Daikin / network touches. Safe to run against a prod DB
copy. Each day's replay takes ~1-3 s (one to ~five LP solves per day depending
on how many MPC re-runs the day had); expect 1–3 min for 14 days.

**Cost source — independent of PnL bug #306.** The replayed cost is
``plan.import_kwh × agile_p − plan.export_kwh × export_p`` where ``import_kwh``
and ``export_kwh`` come from the LP's own energy-balance decision variables
(``src/scheduler/lp_replay.py``). It does NOT call ``compute_daily_pnl`` /
``compute_period_pnl`` — so the load-vs-import bug in those analytics functions
never affected the baseline. The frozen JSON pre-#306 values remain valid.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "tests" / "fixtures" / "lp_regression_baseline.json"


@dataclass
class DayResult:
    date: str
    ok: bool
    error: str | None
    recalc_count: int
    total_original_cost_p: float
    total_replayed_cost_p: float
    total_delta_cost_p: float


@dataclass
class RegressionReport:
    days_back: int
    fail_threshold_p: float
    baseline_path: str
    baseline_total_replayed_cost_p: float | None
    new_total_replayed_cost_p: float
    delta_vs_baseline_p: float    # new - baseline; positive = regression
    days: list[DayResult] = field(default_factory=list)
    refreshed_baseline: bool = False
    no_baseline_yet: bool = False  # first run, baseline file empty/missing
    baseline_missing_dates: list[str] = field(default_factory=list)
    compared_dates: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if self.refreshed_baseline:
            return True
        if self.no_baseline_yet:
            return False
        if self.baseline_missing_dates:
            return False
        return self.delta_vs_baseline_p <= self.fail_threshold_p


# --------------------------------------------------------------------------
# Replay orchestration
# --------------------------------------------------------------------------

def _enumerate_dates_with_runs(days_back: int) -> list[date]:
    """Return the local dates over the last ``days_back`` that have at least
    one optimizer_log run on file. Skips dates with no LP activity (e.g.
    service was down, cold-start period)."""
    from src import db
    from src.config import config

    cutoff = (datetime.now(UTC) - timedelta(days=int(days_back))).isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """SELECT DISTINCT plan_date
                 FROM lp_inputs_snapshot
                WHERE run_at_utc >= ?
                ORDER BY plan_date ASC""",
            (cutoff,),
        )
        rows = [str(r["plan_date"]) for r in cur.fetchall() if r["plan_date"]]
    finally:
        conn.close()
    out: list[date] = []
    for s in rows:
        try:
            out.append(date.fromisoformat(s))
        except ValueError:
            continue
    return out


def _replay_one_day(target_date: date, *, mode: str = "forward") -> DayResult:
    """Replay one day with the given mode; return per-day cost totals.

    ``mode`` is passed through to :func:`replay_day`:
      - ``forward`` — snapshot weather + current config (catches behaviour change)
      - ``honest`` — snapshot weather + snapshot config (catches solver / framework
        regressions). V11-A (#194) made this meaningful by storing cloud cover
        in the forecast history; before that, ``honest`` and ``forward`` both
        used 0% cloud and were indistinguishable on the PV side.
    """
    from src.scheduler.lp_replay import replay_day

    iso = target_date.isoformat()
    try:
        r = replay_day(iso, cadence="original", mode=mode)
    except Exception as e:
        return DayResult(
            date=iso, ok=False, error=f"replay raised: {e}",
            recalc_count=0, total_original_cost_p=0.0,
            total_replayed_cost_p=0.0, total_delta_cost_p=0.0,
        )
    if not r.ok:
        return DayResult(
            date=iso, ok=False, error=r.error or "replay failed",
            recalc_count=0, total_original_cost_p=0.0,
            total_replayed_cost_p=0.0, total_delta_cost_p=0.0,
        )
    return DayResult(
        date=iso,
        ok=True,
        error=None,
        recalc_count=len(r.recalc_run_ids),
        total_original_cost_p=float(r.total_original_cost_p),
        total_replayed_cost_p=float(r.total_replayed_cost_p),
        total_delta_cost_p=float(r.total_delta_cost_p),
    )


# --------------------------------------------------------------------------
# Baseline IO
# --------------------------------------------------------------------------

def _load_baseline(path: Path) -> dict | None:
    """Return the parsed baseline dict, or None if the file is empty / missing."""
    if not path.exists():
        return None
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Baseline file at %s is malformed: %s — treating as empty", path, e)
        return None


def _write_baseline(path: Path, report: RegressionReport, sha: str | None) -> None:
    payload = {
        "frozen_at_sha": sha or "",
        "frozen_at_iso": datetime.now(UTC).isoformat(),
        "days_back": report.days_back,
        "total_replayed_cost_p": report.new_total_replayed_cost_p,
        "per_date": {
            d.date: {
                "total_original_cost_p": d.total_original_cost_p,
                "total_replayed_cost_p": d.total_replayed_cost_p,
                "recalc_count": d.recalc_count,
            }
            for d in report.days if d.ok
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _detect_git_sha() -> str | None:
    """Best-effort repo-relative HEAD lookup so the baseline records *which
    commit* it was frozen at."""
    try:
        import subprocess

        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Top-level orchestration
# --------------------------------------------------------------------------

def run_check(
    *,
    days_back: int,
    baseline_path: Path,
    fail_threshold_p: float,
    refresh_baseline: bool,
    mode: str = "forward",
) -> RegressionReport:
    dates = _enumerate_dates_with_runs(days_back)
    report = RegressionReport(
        days_back=days_back,
        fail_threshold_p=fail_threshold_p,
        baseline_path=str(baseline_path),
        baseline_total_replayed_cost_p=None,
        new_total_replayed_cost_p=0.0,
        delta_vs_baseline_p=0.0,
    )

    ok_by_date: dict[str, DayResult] = {}
    for d in dates:
        result = _replay_one_day(d, mode=mode)
        report.days.append(result)
        if result.ok:
            ok_by_date[result.date] = result

    baseline = _load_baseline(baseline_path)
    if baseline is None:
        report.no_baseline_yet = True
        report.new_total_replayed_cost_p = sum(
            d.total_replayed_cost_p for d in report.days if d.ok
        )
    else:
        per_date = baseline.get("per_date") if isinstance(baseline, dict) else None
        if isinstance(per_date, dict) and per_date:
            baseline_total = 0.0
            current_total = 0.0
            missing: list[str] = []
            compared: list[str] = []
            for day in sorted(str(k) for k in per_date.keys()):
                row = per_date.get(day) or {}
                try:
                    baseline_day_cost = float(row.get("total_replayed_cost_p", 0.0))
                except (AttributeError, TypeError, ValueError):
                    baseline_day_cost = 0.0
                current = ok_by_date.get(day)
                if current is None:
                    missing.append(day)
                    continue
                baseline_total += baseline_day_cost
                current_total += current.total_replayed_cost_p
                compared.append(day)
            report.baseline_missing_dates = missing
            report.compared_dates = compared
            report.baseline_total_replayed_cost_p = baseline_total
            report.new_total_replayed_cost_p = current_total
            report.delta_vs_baseline_p = current_total - baseline_total
        else:
            report.new_total_replayed_cost_p = sum(
                d.total_replayed_cost_p for d in report.days if d.ok
            )
            report.baseline_total_replayed_cost_p = float(baseline.get("total_replayed_cost_p", 0.0))
            report.delta_vs_baseline_p = (
                report.new_total_replayed_cost_p - report.baseline_total_replayed_cost_p
            )

    if refresh_baseline:
        _write_baseline(baseline_path, report, _detect_git_sha())
        report.refreshed_baseline = True

    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_wins_losses(
    report: RegressionReport,
    baseline_per_date: dict[str, dict[str, float]] | None,
) -> list[dict[str, float | str]]:
    """Per-day deltas of (current − baseline) replayed cost in pence.

    Returns a list of ``{date, baseline_p, current_p, delta_p, classification}``
    rows sorted by ``|delta_p|`` desc. ``classification`` is ``win`` when the
    new code beats baseline (delta < 0), ``loss`` when worse, ``flat`` when
    within 1 p, and ``no-baseline`` when the day isn't in the baseline.
    """
    rows: list[dict[str, float | str]] = []
    by_date_current = {d.date: d for d in report.days if d.ok}
    bd = baseline_per_date or {}
    seen: set[str] = set()
    for date_str, day in by_date_current.items():
        seen.add(date_str)
        bl = bd.get(date_str) or {}
        try:
            baseline_p = float(bl.get("total_replayed_cost_p", 0.0)) if bl else None
        except (TypeError, ValueError):
            baseline_p = None
        if baseline_p is None:
            classification = "no-baseline"
            delta_p = 0.0
            baseline_p_out = 0.0
        else:
            delta_p = day.total_replayed_cost_p - baseline_p
            if delta_p < -1.0:
                classification = "win"
            elif delta_p > 1.0:
                classification = "loss"
            else:
                classification = "flat"
            baseline_p_out = baseline_p
        rows.append({
            "date": date_str,
            "baseline_p": baseline_p_out,
            "current_p": day.total_replayed_cost_p,
            "delta_p": delta_p,
            "classification": classification,
        })
    # Surface baseline-only days (regressed enough to fail to replay) too.
    for date_str, bl in bd.items():
        if date_str in seen:
            continue
        try:
            baseline_p = float(bl.get("total_replayed_cost_p", 0.0))
        except (TypeError, ValueError):
            baseline_p = 0.0
        rows.append({
            "date": date_str,
            "baseline_p": baseline_p,
            "current_p": 0.0,
            "delta_p": 0.0,
            "classification": "missing-current",
        })
    rows.sort(key=lambda r: abs(float(r["delta_p"])), reverse=True)
    return rows


def _print_wins_losses(rows: list[dict[str, float | str]], top_k: int) -> None:
    print()
    print("=" * 80)
    print(f"WINS / LOSSES vs baseline (top {top_k} by |Δ|)")
    print("=" * 80)
    print(f"{'date':>12}  {'baseline £':>11}  {'current £':>10}  {'Δ £':>9}  class")
    for r in rows[: max(0, int(top_k))]:
        print(
            f"{str(r['date']):>12}  "
            f"{float(r['baseline_p']) / 100:>+10.2f}  "
            f"{float(r['current_p']) / 100:>+9.2f}  "
            f"{float(r['delta_p']) / 100:>+8.2f}  "
            f"{r['classification']}"
        )
    wins = [r for r in rows if r["classification"] == "win"]
    losses = [r for r in rows if r["classification"] == "loss"]
    flats = [r for r in rows if r["classification"] == "flat"]
    win_total_p = sum(float(r["delta_p"]) for r in wins)
    loss_total_p = sum(float(r["delta_p"]) for r in losses)
    print("-" * 80)
    print(
        f"  Wins  : {len(wins):>3}  total Δ {win_total_p / 100:>+8.2f} £   "
        f"Losses: {len(losses):>3}  total Δ {loss_total_p / 100:>+8.2f} £   "
        f"Flat: {len(flats):>3}"
    )
    print()


def _inspect_day(target: str, *, mode: str = "forward") -> int:
    """Replay one day and dump per-slot LP outputs.

    Reuses the existing ``replay_day`` machinery; surfaces ``slot_diffs`` for
    the LAST run of the day (fully-resolved horizon) so the user can see the
    plan that would have shipped if the day's recalc chain ran on current code.
    """
    from src.scheduler.lp_replay import replay_day

    try:
        r = replay_day(target, cadence="original", mode=mode)
    except Exception as e:
        print(f"[inspect-day] replay raised: {e}", file=sys.stderr)
        return 1
    if not r.ok:
        print(f"[inspect-day] replay failed: {r.error}", file=sys.stderr)
        return 1
    runs = list(r.runs or [])
    if not runs:
        print(f"[inspect-day] no LP runs on {target}", file=sys.stderr)
        return 1
    last = runs[-1]
    slots = last.slot_diffs or []
    print()
    print("=" * 100)
    print(f"INSPECT-DAY {target} — last run (mode={mode}, run_id={last.run_id})")
    print(f"  replayed cost (full day)  : {r.total_replayed_cost_p / 100:>+8.2f} £")
    print(f"  original  cost (full day) : {r.total_original_cost_p / 100:>+8.2f} £")
    print(f"  Δ                         : {r.total_delta_cost_p / 100:>+8.2f} £")
    print("=" * 100)
    print(
        f"{'slot_utc':>20}  {'p':>6}  "
        f"{'imp':>5}  {'exp':>5}  {'chg':>5}  {'dis':>5}  "
        f"{'dhw':>5}  {'spc':>5}  {'soc':>6}  {'tank':>5}"
    )
    for s in slots:
        print(
            f"{str(s.get('slot_time_utc', ''))[-20:]:>20}  "
            f"{float(s.get('price_p', 0.0)):>6.2f}  "
            f"{float(s.get('new_import_kwh', 0.0)):>5.2f}  "
            f"{float(s.get('new_export_kwh', 0.0)):>5.2f}  "
            f"{float(s.get('new_charge_kwh', 0.0)):>5.2f}  "
            f"{float(s.get('new_discharge_kwh', 0.0)):>5.2f}  "
            f"{float(s.get('new_dhw_kwh', 0.0)):>5.2f}  "
            f"{float(s.get('new_space_kwh', 0.0)):>5.2f}  "
            f"{float(s.get('new_soc_kwh', 0.0)):>6.2f}  "
            f"{float(s.get('new_tank_temp_c', 0.0)):>5.1f}"
        )
    print()
    return 0


def _print_report(report: RegressionReport) -> None:
    print()
    print("=" * 80)
    print(f"LP regression check — last {report.days_back} days, {len(report.days)} days replayed")
    print("=" * 80)
    print(f"{'date':>12}  {'recalcs':>7}  {'orig £':>10}  {'replay £':>10}  {'Δ £':>10}  status")
    for d in report.days:
        status = "ok" if d.ok else f"SKIP ({d.error})"
        print(
            f"{d.date:>12}  {d.recalc_count:>7d}  "
            f"{d.total_original_cost_p / 100:>+9.2f}  "
            f"{d.total_replayed_cost_p / 100:>+9.2f}  "
            f"{d.total_delta_cost_p / 100:>+9.2f}  "
            f"{status}"
        )
    print("-" * 80)
    print(f"NEW total replayed cost      : {report.new_total_replayed_cost_p / 100:>+9.2f} £")
    if report.compared_dates:
        print(f"Compared baseline dates      : {len(report.compared_dates)}")
    if report.baseline_missing_dates:
        print("Missing baseline dates       : " + ", ".join(report.baseline_missing_dates))
    if report.baseline_total_replayed_cost_p is None:
        if report.refreshed_baseline:
            print("Baseline                     : refreshed and written to "
                  f"{report.baseline_path}")
        else:
            print(f"Baseline                     : NOT FOUND at {report.baseline_path}")
            print("                               Approval requires a comparable baseline. "
                  "Refresh only after proving the strategy is better or equal.")
    else:
        print(f"BASELINE total replayed cost : "
              f"{report.baseline_total_replayed_cost_p / 100:>+9.2f} £   "
              f"(allowed worse +{report.fail_threshold_p / 100:.2f} £)")
        print(f"Δ vs baseline                : "
              f"{report.delta_vs_baseline_p / 100:>+9.2f} £")
    if report.passed:
        print("VERDICT: PASS — aggregate cost is no worse than baseline.")
    else:
        print("VERDICT: FAIL — aggregate cost regressed, baseline is missing, "
              "or baseline coverage is incomplete; "
              "investigate before merging.")
    print()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Pre-merge LP regression gate.")
    parser.add_argument(
        "--days", type=int, default=14,
        help="Replay window in days (default 14).",
    )
    parser.add_argument(
        "--baseline", type=Path, default=DEFAULT_BASELINE,
        help="Path to the JSON baseline file (default: tests/fixtures/lp_regression_baseline.json).",
    )
    parser.add_argument(
        "--fail-above-pence", type=float, default=0.0,
        help=(
            "Fail if (new total replayed cost − baseline) exceeds this many pence. "
            "Default 0: individual moments may worsen, but aggregate replayed cost "
            "must be better than or equal to the comparable baseline window."
        ),
    )
    parser.add_argument(
        "--refresh-baseline", action="store_true",
        help=(
            "Overwrite the baseline file with the current totals. Use this only after "
            "proving the new strategy is better or equal on an agreed replay set; "
            "commit the updated JSON in the same PR."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("forward", "honest", "both"),
        default="forward",
        help=(
            "V11-A (#194). 'forward' (default): snapshot weather + CURRENT config — "
            "catches behaviour changes. 'honest': snapshot weather + SNAPSHOT config — "
            "catches solver/framework regressions. 'both': run both and require both "
            "to pass; baseline tracks the larger of the two costs (most stringent gate). "
            "Use 'both' for any LP-touching PR."
        ),
    )
    parser.add_argument(
        "--json", type=Path, default=None,
        help="Optional path to write the full report as JSON.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress the human-readable table; useful in CI when --json is set.",
    )
    parser.add_argument(
        "--wins-losses", action="store_true",
        help=(
            "After the standard report, print a per-day wins/losses table "
            "sorted by |Δ| vs baseline. Useful to localise WHICH days the "
            "new code helps or hurts before refreshing the baseline."
        ),
    )
    parser.add_argument(
        "--wins-losses-top-k", type=int, default=14,
        help="When --wins-losses is set, print up to this many rows (default 14).",
    )
    parser.add_argument(
        "--inspect-day", type=str, default=None,
        help=(
            "Replay ONE local date (YYYY-MM-DD) under --mode and dump per-slot "
            "LP outputs (price, kWh per pillar, SoC, tank temp) for the last "
            "run of the day. Mutually exclusive with the regression-gate flow; "
            "exits 0/1 based on whether the replay succeeded."
        ),
    )
    args = parser.parse_args(argv[1:])

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.inspect_day:
        # Single-day inspection short-circuits the regression-gate flow —
        # the gate only makes sense as an aggregate over the window.
        single_mode = "forward" if args.mode == "both" else args.mode
        return _inspect_day(args.inspect_day, mode=single_mode)

    if args.mode == "both":
        # Run forward + honest sequentially, fail on EITHER regressing.
        # Each mode keeps its own baseline section ("forward" / "honest")
        # in the JSON file so they can drift independently when warranted.
        from copy import deepcopy

        modes_to_run = ("forward", "honest")
        per_mode_reports: dict[str, RegressionReport] = {}
        passed_overall = True
        for m in modes_to_run:
            # Each mode reads its own subkey of the baseline file to avoid mixing
            # forward/honest totals on first refresh.
            mode_baseline_path = args.baseline.with_name(
                args.baseline.stem + f".{m}" + args.baseline.suffix
            )
            r = run_check(
                days_back=args.days,
                baseline_path=mode_baseline_path,
                fail_threshold_p=args.fail_above_pence,
                refresh_baseline=args.refresh_baseline,
                mode=m,
            )
            per_mode_reports[m] = r
            if not r.passed:
                passed_overall = False
            if not args.quiet:
                print(f"\n### MODE = {m.upper()} ###")
                _print_report(r)
                if args.wins_losses:
                    bl = _load_baseline(mode_baseline_path) or {}
                    per_date = bl.get("per_date") if isinstance(bl, dict) else None
                    rows = _build_wins_losses(r, per_date if isinstance(per_date, dict) else None)
                    _print_wins_losses(rows, args.wins_losses_top_k)

        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps({
                "mode": "both",
                "passed": passed_overall,
                "per_mode": {
                    m: {
                        **{k: v for k, v in asdict(r).items() if k != "days"},
                        "passed": r.passed,
                        "days": [asdict(d) for d in r.days],
                    }
                    for m, r in per_mode_reports.items()
                },
            }, indent=2))

        if not args.quiet:
            verdict = "PASS" if passed_overall else "FAIL"
            print(f"\n=== BOTH-MODE VERDICT: {verdict} ===")
            print("Both forward and honest replay must pass for an LP-touching PR.")
        return 0 if passed_overall else 1

    report = run_check(
        days_back=args.days,
        baseline_path=args.baseline,
        fail_threshold_p=args.fail_above_pence,
        refresh_baseline=args.refresh_baseline,
        mode=args.mode,
    )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            **{k: v for k, v in asdict(report).items() if k != "days"},
            "passed": report.passed,
            "mode": args.mode,
            "days": [asdict(d) for d in report.days],
        }, indent=2))

    if not args.quiet:
        _print_report(report)
        if args.wins_losses:
            bl = _load_baseline(args.baseline) or {}
            per_date = bl.get("per_date") if isinstance(bl, dict) else None
            rows = _build_wins_losses(report, per_date if isinstance(per_date, dict) else None)
            _print_wins_losses(rows, args.wins_losses_top_k)

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
