"""Octopus fetch backoff / retry helpers."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.scheduler.octopus_fetch import next_retry_seconds, should_run_retry_fetch


def test_next_retry_seconds_staircase() -> None:
    assert next_retry_seconds(1) == 600
    assert next_retry_seconds(2) == 1800
    assert next_retry_seconds(3) == 3600
    assert next_retry_seconds(99) == 3600


def test_should_run_retry_false_without_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        assert should_run_retry_fetch() is False


def test_should_run_retry_true_after_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        now = datetime.now(UTC)
        db.update_octopus_fetch_state(
            consecutive_failures=2,
            failure_streak_started_at=(now - timedelta(hours=2)).isoformat(),
            last_attempt_at=(now - timedelta(seconds=1900)).isoformat(),
        )
        assert should_run_retry_fetch() is True


# --- #691: export-coverage gap detection + export-only retry ---


@pytest.fixture()
def _gap_env(monkeypatch: pytest.MonkeyPatch):
    """Fresh DB + outgoing_agile mode + reset module retry state."""
    from src.scheduler import octopus_fetch

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        monkeypatch.setattr("src.config.config.EXPORT_TARIFF_MODE", "outgoing_agile")
        monkeypatch.setattr("src.config.config.OCTOPUS_TARIFF_CODE", "IMP")
        monkeypatch.setattr("src.config.config.OCTOPUS_EXPORT_TARIFF_CODE", "EXP")
        monkeypatch.setattr(octopus_fetch, "_export_resolve_pending", False)
        monkeypatch.setattr(octopus_fetch, "_export_gap_last_warn_at", None)
        yield


def _seed_import(through: str, tariff: str = "IMP") -> None:
    db.save_agile_rates(
        [{"valid_from": through, "valid_to": through, "value_inc_vat": 10.0}],
        tariff,
    )


def _seed_export(through: str, tariff: str = "EXP") -> None:
    db.save_agile_export_rates(
        [{"valid_from": through, "valid_to": through, "value_inc_vat": 2.0}],
        tariff,
    )


def test_export_gap_false_without_export_tariff(_gap_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.scheduler.octopus_fetch import export_rates_coverage_gap

    monkeypatch.setattr("src.config.config.OCTOPUS_EXPORT_TARIFF_CODE", "")
    _seed_import("2026-07-12T21:30:00Z")
    assert export_rates_coverage_gap() is False


def test_export_gap_false_in_seg_flat_mode(_gap_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """seg_flat prices exports at the flat SEG rate — a stale Outgoing table
    must not force device-writing re-solves."""
    from src.scheduler.octopus_fetch import export_rates_coverage_gap

    monkeypatch.setattr("src.config.config.EXPORT_TARIFF_MODE", "seg_flat")
    _seed_import("2026-07-12T21:30:00Z")
    assert export_rates_coverage_gap() is False


def test_export_gap_true_when_export_lags(_gap_env) -> None:
    from src.scheduler.octopus_fetch import export_rates_coverage_gap

    _seed_import("2026-07-12T21:30:00Z")
    # No export rows at all → gap.
    assert export_rates_coverage_gap() is True
    # Export rows ending a day earlier → still a gap.
    _seed_export("2026-07-11T21:30:00Z")
    assert export_rates_coverage_gap() is True
    # Export coverage caught up → no gap.
    _seed_export("2026-07-12T21:30:00Z")
    assert export_rates_coverage_gap() is False


def test_export_gap_scoped_to_configured_tariffs(_gap_env) -> None:
    """Upsert-only tables keep rows for previously configured codes — those
    must not mask a real gap after a tariff switch."""
    from src.scheduler.octopus_fetch import export_rates_coverage_gap

    _seed_import("2026-07-12T21:30:00Z")
    # Full coverage, but under the OLD export tariff code → still a gap.
    _seed_export("2026-07-12T21:30:00Z", tariff="OLD-EXP")
    assert export_rates_coverage_gap() is True
    _seed_export("2026-07-12T21:30:00Z", tariff="EXP")
    assert export_rates_coverage_gap() is False


def test_export_gap_false_when_no_import_rates(_gap_env) -> None:
    from src.scheduler.octopus_fetch import export_rates_coverage_gap

    assert export_rates_coverage_gap() is False


def test_retry_export_noop_without_gap(_gap_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.scheduler import octopus_fetch

    def _boom(**kwargs):  # must not be called
        raise AssertionError("fetch should not run when there is no gap")

    monkeypatch.setattr(octopus_fetch, "fetch_agile_export_rates", _boom)
    out = octopus_fetch.retry_export_rates_if_gap()
    assert out["ok"] is True and out["gap"] is False and out["resolve_pending"] is False


def test_retry_export_closes_gap_and_resolves(_gap_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.scheduler import octopus_fetch, runner

    monkeypatch.setattr("src.config.config.USE_BULLETPROOF_ENGINE", True)
    _seed_import("2026-07-12T21:30:00Z")

    monkeypatch.setattr(
        octopus_fetch,
        "fetch_agile_export_rates",
        lambda export_tariff_code=None: [
            {"valid_from": "2026-07-12T21:30:00Z", "valid_to": "2026-07-12T22:00:00Z", "value_inc_vat": 2.0}
        ],
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        runner, "bulletproof_mpc_job", lambda **kw: calls.append(kw) or True
    )

    out = octopus_fetch.retry_export_rates_if_gap()
    assert out["ok"] is True and out["gap"] is False and out["export_rows"] == 1
    assert out["resolve_pending"] is False
    assert calls == [{"force_write_devices": True, "trigger_reason": "octopus_fetch"}]
    # Gap closed and solve ran — a second tick is a full no-op.
    monkeypatch.setattr(
        octopus_fetch,
        "fetch_agile_export_rates",
        lambda export_tariff_code=None: pytest.fail("no refetch once coverage caught up"),
    )
    out2 = octopus_fetch.retry_export_rates_if_gap()
    assert out2["gap"] is False and out2["resolve_pending"] is False
    assert len(calls) == 1


def test_retry_export_rearms_when_mpc_skipped(_gap_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cooldown/lock-skipped MPC (returns False) must keep the re-solve armed:
    the next tick re-solves WITHOUT needing the gap to reopen (#691 blocker)."""
    from src.scheduler import octopus_fetch, runner

    monkeypatch.setattr("src.config.config.USE_BULLETPROOF_ENGINE", True)
    _seed_import("2026-07-12T21:30:00Z")
    monkeypatch.setattr(
        octopus_fetch,
        "fetch_agile_export_rates",
        lambda export_tariff_code=None: [
            {"valid_from": "2026-07-12T21:30:00Z", "valid_to": "2026-07-12T22:00:00Z", "value_inc_vat": 2.0}
        ],
    )
    mpc_results = iter([False, True])  # first call skipped (cooldown), second runs
    calls: list[dict] = []
    monkeypatch.setattr(
        runner,
        "bulletproof_mpc_job",
        lambda **kw: calls.append(kw) or next(mpc_results),
    )

    out = octopus_fetch.retry_export_rates_if_gap()
    assert out["gap"] is False
    assert out["resolve_pending"] is True  # solve was swallowed → stays armed
    assert len(calls) == 1

    # Next tick: no gap, no fetch — but the pending re-solve fires and clears.
    monkeypatch.setattr(
        octopus_fetch,
        "fetch_agile_export_rates",
        lambda export_tariff_code=None: pytest.fail("no refetch once coverage caught up"),
    )
    out2 = octopus_fetch.retry_export_rates_if_gap()
    assert out2["resolve_pending"] is False
    assert len(calls) == 2


def test_retry_export_partial_advance_still_resolves(_gap_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real rates landing beat priors immediately — re-solve on coverage
    advance even when the export tail is still short of import coverage."""
    from src.scheduler import octopus_fetch, runner

    monkeypatch.setattr("src.config.config.USE_BULLETPROOF_ENGINE", True)
    _seed_import("2026-07-12T21:30:00Z")
    _seed_export("2026-07-11T21:30:00Z")

    # Fetch advances coverage to mid-day but not to the import max.
    monkeypatch.setattr(
        octopus_fetch,
        "fetch_agile_export_rates",
        lambda export_tariff_code=None: [
            {"valid_from": "2026-07-12T11:30:00Z", "valid_to": "2026-07-12T12:00:00Z", "value_inc_vat": 2.0}
        ],
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        runner, "bulletproof_mpc_job", lambda **kw: calls.append(kw) or True
    )
    out = octopus_fetch.retry_export_rates_if_gap()
    assert out["gap"] is True  # tail still missing → keeps retrying
    assert out["resolve_pending"] is False  # but the fresh rates got a solve
    assert len(calls) == 1


def test_retry_export_no_advance_no_resolve(_gap_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outgoing not published yet: the fetch returns only rows we already
    have — no coverage advance, no re-solve, gap stays open."""
    from src.scheduler import octopus_fetch, runner

    monkeypatch.setattr("src.config.config.USE_BULLETPROOF_ENGINE", True)
    _seed_import("2026-07-12T21:30:00Z")
    _seed_export("2026-07-11T21:30:00Z")

    monkeypatch.setattr(
        octopus_fetch,
        "fetch_agile_export_rates",
        lambda export_tariff_code=None: [
            {"valid_from": "2026-07-11T21:30:00Z", "valid_to": "2026-07-11T22:00:00Z", "value_inc_vat": 2.0}
        ],
    )
    monkeypatch.setattr(
        runner,
        "bulletproof_mpc_job",
        lambda **kw: pytest.fail("must not re-solve when nothing new landed"),
    )
    out = octopus_fetch.retry_export_rates_if_gap()
    assert out["ok"] is False and out["gap"] is True and out["resolve_pending"] is False
