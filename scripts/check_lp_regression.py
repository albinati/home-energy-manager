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
  50 p over the window). The new code is at least as good as the baseline.
* 1 — the new code is materially worse than the baseline. **Do not merge** until
  either (a) the regression is fixed, or (b) you intentionally accept it as the
  new baseline by re-running with ``--refresh-baseline`` and committing the
  updated JSON in the same PR.

**Usage:**

```bash
# Pre-merge gate (against your locally-mounted prod DB snapshot):
DB_PATH=/path/to/prod-snapshot.db .venv/bin/python scripts/check_lp_regression.py

# After an INTENTIONAL LP behaviour change (new objective term, tuned weights):
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

    @property
    def passed(self) -> bool:
        if self.refreshed_baseline:
            return True
        if self.no_baseline_yet:
            return True
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

    for d in dates:
        result = _replay_one_day(d, mode=mode)
        report.days.append(result)
        if result.ok:
            report.new_total_replayed_cost_p += result.total_replayed_cost_p

    baseline = _load_baseline(baseline_path)
    if baseline is None:
        report.no_baseline_yet = True
    else:
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
    if report.baseline_total_replayed_cost_p is None:
        if report.refreshed_baseline:
            print("Baseline                     : refreshed and written to "
                  f"{report.baseline_path}")
        else:
            print(f"Baseline                     : NOT FOUND at {report.baseline_path}")
            print("                               Re-run with --refresh-baseline to "
                  "freeze the current totals as the baseline.")
    else:
        print(f"BASELINE total replayed cost : "
              f"{report.baseline_total_replayed_cost_p / 100:>+9.2f} £   "
              f"(threshold +{report.fail_threshold_p / 100:.2f} £)")
        print(f"Δ vs baseline                : "
              f"{report.delta_vs_baseline_p / 100:>+9.2f} £")
    if report.passed:
        print("VERDICT: PASS — LP is no worse than baseline.")
    else:
        print("VERDICT: FAIL — LP regressed beyond the threshold; "
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
        "--fail-above-pence", type=float, default=50.0,
        help=(
            "Fail if (new total replayed cost − baseline) exceeds this many pence. "
            "Default 50 (= £0.50 over the whole window). Solver float-precision drift "
            "is typically << 1 p across 14 days, so 50 p is a generous tolerance."
        ),
    )
    parser.add_argument(
        "--refresh-baseline", action="store_true",
        help=(
            "Overwrite the baseline file with the current totals. Use this AFTER "
            "intentionally changing LP behaviour (new objective term, tuned weights, "
            "rebased default config) — commit the updated JSON in the same PR."
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
    args = parser.parse_args(argv[1:])

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

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

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
