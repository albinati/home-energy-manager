"""Tests for the LP regression gate (``scripts/check_lp_regression.py``).

Scope: the comparison logic and CLI wiring. The actual replay (``replay_day``)
is monkey-patched out — that's exercised end-to-end by
``scripts/replay_period.py`` against a real prod DB snapshot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest


# Tiny shim so the script's "replay one day" call returns deterministic values.
@dataclass
class _FakeReplay:
    ok: bool = True
    error: str | None = None
    plan_date: str = ""
    recalc_run_ids: list = None  # type: ignore[assignment]
    total_original_cost_p: float = 0.0
    total_replayed_cost_p: float = 0.0
    total_delta_cost_p: float = 0.0

    def __post_init__(self):
        if self.recalc_run_ids is None:
            self.recalc_run_ids = [1, 2]


@pytest.fixture
def patched_replay(monkeypatch):
    """Replace replay_day + the date enumerator with deterministic stubs."""
    from scripts import check_lp_regression as cs
    from datetime import date

    # Three fake days with controllable replayed cost.
    by_date = {
        date(2026, 4, 25): _FakeReplay(plan_date="2026-04-25", total_original_cost_p=100.0, total_replayed_cost_p=95.0, total_delta_cost_p=-5.0),
        date(2026, 4, 26): _FakeReplay(plan_date="2026-04-26", total_original_cost_p=200.0, total_replayed_cost_p=200.0, total_delta_cost_p=0.0),
        date(2026, 4, 27): _FakeReplay(plan_date="2026-04-27", total_original_cost_p=150.0, total_replayed_cost_p=140.0, total_delta_cost_p=-10.0),
    }
    # Patch the date-enumerator to return our fixed list.
    monkeypatch.setattr(cs, "_enumerate_dates_with_runs", lambda days_back: list(by_date.keys()))
    # Patch the per-day replay to return our stubs. Accept mode kwarg
    # (V11-A added it) but ignore it — the stubs are mode-agnostic.
    def _fake_replay(target_date, *, mode: str = "forward"):
        r = by_date[target_date]
        return cs.DayResult(
            date=r.plan_date, ok=r.ok, error=r.error,
            recalc_count=len(r.recalc_run_ids),
            total_original_cost_p=r.total_original_cost_p,
            total_replayed_cost_p=r.total_replayed_cost_p,
            total_delta_cost_p=r.total_delta_cost_p,
        )
    monkeypatch.setattr(cs, "_replay_one_day", _fake_replay)
    return cs, by_date


def test_no_baseline_yet_passes_with_message(tmp_path, patched_replay):
    cs, _ = patched_replay
    baseline_path = tmp_path / "missing.json"

    report = cs.run_check(
        days_back=14,
        baseline_path=baseline_path,
        fail_threshold_p=50.0,
        refresh_baseline=False,
    )
    assert report.passed
    assert report.no_baseline_yet
    # Aggregate equals sum of replayed costs.
    assert report.new_total_replayed_cost_p == pytest.approx(95.0 + 200.0 + 140.0)


def test_pass_when_total_at_or_below_baseline(tmp_path, patched_replay):
    cs, _ = patched_replay
    baseline_path = tmp_path / "baseline.json"
    # Pretend last run also totalled 435 p — current run matches exactly.
    baseline_path.write_text(json.dumps({
        "frozen_at_sha": "abc",
        "total_replayed_cost_p": 435.0,
        "per_date": {},
    }))
    report = cs.run_check(
        days_back=14, baseline_path=baseline_path,
        fail_threshold_p=50.0, refresh_baseline=False,
    )
    assert report.passed
    assert report.delta_vs_baseline_p == pytest.approx(0.0)


def test_pass_when_under_baseline(tmp_path, patched_replay):
    """New code is BETTER (saves more) — definitely a pass."""
    cs, _ = patched_replay
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"total_replayed_cost_p": 600.0}))
    report = cs.run_check(
        days_back=14, baseline_path=baseline_path,
        fail_threshold_p=50.0, refresh_baseline=False,
    )
    assert report.passed
    # 435 (new) - 600 (baseline) = -165 → improvement.
    assert report.delta_vs_baseline_p == pytest.approx(-165.0)


def test_fail_when_total_exceeds_baseline_plus_threshold(tmp_path, patched_replay):
    cs, _ = patched_replay
    baseline_path = tmp_path / "baseline.json"
    # Baseline 100 p, new = 435 p. Δ = +335 p, threshold 50 → FAIL.
    baseline_path.write_text(json.dumps({"total_replayed_cost_p": 100.0}))
    report = cs.run_check(
        days_back=14, baseline_path=baseline_path,
        fail_threshold_p=50.0, refresh_baseline=False,
    )
    assert not report.passed
    assert report.delta_vs_baseline_p == pytest.approx(335.0)


def test_within_threshold_still_passes(tmp_path, patched_replay):
    cs, _ = patched_replay
    baseline_path = tmp_path / "baseline.json"
    # Baseline 400 p, new 435 p. Δ = +35 p. Threshold 50 p → still pass.
    baseline_path.write_text(json.dumps({"total_replayed_cost_p": 400.0}))
    report = cs.run_check(
        days_back=14, baseline_path=baseline_path,
        fail_threshold_p=50.0, refresh_baseline=False,
    )
    assert report.passed


def test_refresh_baseline_writes_file_and_returns_pass(tmp_path, patched_replay):
    cs, _ = patched_replay
    baseline_path = tmp_path / "baseline.json"
    report = cs.run_check(
        days_back=14, baseline_path=baseline_path,
        fail_threshold_p=50.0, refresh_baseline=True,
    )
    assert report.passed
    assert report.refreshed_baseline
    # File exists and contains the new totals.
    payload = json.loads(baseline_path.read_text())
    assert payload["total_replayed_cost_p"] == pytest.approx(435.0)
    assert "2026-04-25" in payload["per_date"]
    assert "2026-04-27" in payload["per_date"]


def test_malformed_baseline_treated_as_no_baseline(tmp_path, patched_replay):
    cs, _ = patched_replay
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text("not valid json {{{")
    report = cs.run_check(
        days_back=14, baseline_path=baseline_path,
        fail_threshold_p=50.0, refresh_baseline=False,
    )
    assert report.passed  # vacuously
    assert report.no_baseline_yet


def test_failed_replay_skipped_from_total(tmp_path, monkeypatch):
    """A day whose replay fails (no inputs snapshot, weather missing, etc.)
    should be reported with ok=False but NOT contribute to the new total."""
    from scripts import check_lp_regression as cs
    from datetime import date

    monkeypatch.setattr(cs, "_enumerate_dates_with_runs", lambda d: [date(2026, 4, 25), date(2026, 4, 26)])

    def _fake(target, *, mode: str = "forward"):
        if target == date(2026, 4, 25):
            return cs.DayResult(date=str(target), ok=True, error=None, recalc_count=2,
                               total_original_cost_p=100.0, total_replayed_cost_p=80.0, total_delta_cost_p=-20.0)
        return cs.DayResult(date=str(target), ok=False, error="weather missing",
                           recalc_count=0, total_original_cost_p=0.0, total_replayed_cost_p=0.0, total_delta_cost_p=0.0)

    monkeypatch.setattr(cs, "_replay_one_day", _fake)
    baseline_path = tmp_path / "missing.json"
    report = cs.run_check(
        days_back=14, baseline_path=baseline_path,
        fail_threshold_p=50.0, refresh_baseline=False,
    )
    # Only the OK day contributes.
    assert report.new_total_replayed_cost_p == pytest.approx(80.0)
    assert any(not d.ok for d in report.days)
    assert any(d.ok for d in report.days)


# ---------------------------------------------------------------------------
# V11-A (#194) — --mode flag wiring
# ---------------------------------------------------------------------------

def test_run_check_threads_mode_through_to_replay(tmp_path, monkeypatch):
    """V11-A: ``mode`` kwarg flows through ``run_check`` → ``_replay_one_day`` →
    ``replay_day``. We verify by capturing what ``_replay_one_day`` is called with."""
    from scripts import check_lp_regression as cs
    from datetime import date

    monkeypatch.setattr(cs, "_enumerate_dates_with_runs", lambda d: [date(2026, 4, 25)])
    captured: list[str] = []

    def _fake(target, *, mode: str = "forward"):
        captured.append(mode)
        return cs.DayResult(
            date=str(target), ok=True, error=None, recalc_count=1,
            total_original_cost_p=10.0, total_replayed_cost_p=10.0, total_delta_cost_p=0.0,
        )
    monkeypatch.setattr(cs, "_replay_one_day", _fake)

    cs.run_check(
        days_back=14, baseline_path=tmp_path / "x.json",
        fail_threshold_p=50.0, refresh_baseline=False, mode="honest",
    )
    assert captured == ["honest"]

    captured.clear()
    cs.run_check(
        days_back=14, baseline_path=tmp_path / "x2.json",
        fail_threshold_p=50.0, refresh_baseline=False, mode="forward",
    )
    assert captured == ["forward"]
