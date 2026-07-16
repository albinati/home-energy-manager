"""Import-rates gap retry (#726) — the import-side twin of the #691 export gap.

The daily fetch races Octopus's ~16:00-local publication: a fetch minutes
early records SUCCESS with only today's curve, and nothing retried until the
next day's cron (observed 2026-07-16: calendar empty for tomorrow, nightly
plan push would have solved rateless).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.config import config
from src.scheduler import octopus_fetch as of


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(of, "_import_gap_last_attempt_at", None)
    monkeypatch.setattr(of, "_import_gap_last_warn_at", None)
    monkeypatch.setattr(of, "_import_resolve_pending", False)
    # Plain dataclass attrs (NOT runtime properties) → setattr is safe here;
    # config._overrides would be a silent no-op for these.
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-TEST-C")
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "OCTOPUS_FETCH_HOUR", 16)
    monkeypatch.setattr(config, "OCTOPUS_FETCH_MINUTE", 0)
    yield


def _coverage(monkeypatch, max_valid_from: str | None):
    monkeypatch.setattr(
        of.db, "get_agile_rates_coverage_max", lambda table, tariff_code=None: max_valid_from
    )


# 2026-07-16 is BST (UTC+1): 16:15 local fetch-due = 15:15 UTC.
_BEFORE_DUE = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)   # 15:00 local
_AFTER_DUE = datetime(2026, 7, 16, 17, 40, tzinfo=UTC)   # 18:40 local — the incident moment
_AFTER_MIDNIGHT = datetime(2026, 7, 16, 23, 30, tzinfo=UTC)  # 17/07 00:30 local
_TODAY_ONLY = "2026-07-16T21:30:00Z"                      # coverage ends today
_TOMORROW_OK = "2026-07-17T21:30:00Z"                     # tomorrow landed


# --- import_rates_coverage_gap ------------------------------------------------

def test_no_gap_before_fetch_due(monkeypatch):
    _coverage(monkeypatch, _TODAY_ONLY)
    assert of.import_rates_coverage_gap(now_utc=_BEFORE_DUE) is False


def test_gap_after_fetch_due_when_tomorrow_missing(monkeypatch):
    _coverage(monkeypatch, _TODAY_ONLY)
    assert of.import_rates_coverage_gap(now_utc=_AFTER_DUE) is True


def test_no_gap_when_tomorrow_covered(monkeypatch):
    _coverage(monkeypatch, _TOMORROW_OK)
    assert of.import_rates_coverage_gap(now_utc=_AFTER_DUE) is False


def test_gap_survives_local_midnight(monkeypatch):
    # Review finding: at 00:30 local the missing day IS today — a fetch-due
    # test alone would disarm and the house runs rateless until 16:05. The
    # today-evening clause must keep the gap armed at any hour.
    _coverage(monkeypatch, _TODAY_ONLY)  # coverage ends 16/07 22:30 local
    assert of.import_rates_coverage_gap(now_utc=_AFTER_MIDNIGHT) is True


def test_no_gap_overnight_when_today_covered(monkeypatch):
    # Normal overnight state: yesterday's publication covers today through
    # ~23:00 local → quiet until this afternoon's fetch-due.
    _coverage(monkeypatch, _TODAY_ONLY)
    early = datetime(2026, 7, 16, 6, 0, tzinfo=UTC)  # 07:00 local, same day
    assert of.import_rates_coverage_gap(now_utc=early) is False


def test_no_gap_when_table_empty_failure_streak_owns_it(monkeypatch):
    _coverage(monkeypatch, None)
    assert of.import_rates_coverage_gap(now_utc=_AFTER_DUE) is False


def test_no_gap_when_tariff_unset(monkeypatch):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "")
    _coverage(monkeypatch, _TODAY_ONLY)
    assert of.import_rates_coverage_gap(now_utc=_AFTER_DUE) is False


# --- retry_import_rates_if_gap ------------------------------------------------

def _probe(monkeypatch, valid_froms: list[str]):
    monkeypatch.setattr(
        of, "fetch_agile_rates",
        lambda tariff_code=None: [{"valid_from": v} for v in valid_froms],
    )


def test_retry_probe_skips_full_fetch_when_nothing_new(monkeypatch):
    # Octopus still late: probe returns the same curve we already hold →
    # no calibrations, no forced solve, just one cheap GET per 30 min.
    monkeypatch.setattr(of, "import_rates_coverage_gap", lambda now_utc=None: True)
    _coverage(monkeypatch, _TODAY_ONLY)
    _probe(monkeypatch, [_TODAY_ONLY])
    monkeypatch.setattr(
        of, "fetch_and_store_rates",
        lambda fox=None: pytest.fail("full fetch must not run when probe shows nothing new"),
    )
    res = of.retry_import_rates_if_gap()
    assert res["fetched"] is False and res["gap"] is True and res["advanced"] is False


def test_retry_runs_full_fetch_when_probe_advances(monkeypatch):
    gaps = iter([True, False])  # gap before fetch, closed after
    monkeypatch.setattr(of, "import_rates_coverage_gap", lambda now_utc=None: next(gaps))
    _coverage(monkeypatch, _TODAY_ONLY)
    _probe(monkeypatch, [_TOMORROW_OK])
    calls = []
    monkeypatch.setattr(
        of, "fetch_and_store_rates",
        lambda fox=None: calls.append(fox) or {"ok": True, "optimizer": {"ok": True}},
    )
    res = of.retry_import_rates_if_gap()
    assert calls == [None]
    assert res["ok"] is True and res["gap"] is False and res["fetched"] is True
    assert res["resolve_pending"] is False


def test_retry_arms_pending_resolve_when_solve_skipped(monkeypatch):
    # Fetch stored the new curve but its internal re-solve hit the MPC
    # cooldown → pending must arm, and clear on a later tick once a solve
    # actually completes (mirror of _export_resolve_pending, #691).
    gaps = iter([True, False, False])
    monkeypatch.setattr(of, "import_rates_coverage_gap", lambda now_utc=None: next(gaps))
    _coverage(monkeypatch, _TODAY_ONLY)
    _probe(monkeypatch, [_TOMORROW_OK])
    monkeypatch.setattr(
        of, "fetch_and_store_rates",
        lambda fox=None: {"ok": True, "optimizer": {"ok": False}},
    )
    resolves = []
    monkeypatch.setattr(
        of, "_resolve_after_export_rates", lambda: resolves.append(1) or True
    )
    res = of.retry_import_rates_if_gap()
    assert res["resolve_pending"] is True and of._import_resolve_pending is True
    res2 = of.retry_import_rates_if_gap()  # next tick: solve completes → cleared
    assert resolves == [1]
    assert res2["resolve_pending"] is False and of._import_resolve_pending is False


def test_retry_noop_when_no_gap(monkeypatch):
    monkeypatch.setattr(of, "import_rates_coverage_gap", lambda now_utc=None: False)
    monkeypatch.setattr(
        of, "fetch_and_store_rates",
        lambda fox=None: pytest.fail("must not fetch without a gap"),
    )
    res = of.retry_import_rates_if_gap()
    assert res["ok"] is True and res["gap"] is False and res["fetched"] is False


def test_retry_throttles_to_30_minutes(monkeypatch):
    monkeypatch.setattr(of, "import_rates_coverage_gap", lambda now_utc=None: True)
    _coverage(monkeypatch, _TODAY_ONLY)
    _probe(monkeypatch, [_TOMORROW_OK])
    calls = []
    monkeypatch.setattr(
        of, "fetch_and_store_rates",
        lambda fox=None: calls.append(1) or {"ok": True, "optimizer": {"ok": True}},
    )
    first = of.retry_import_rates_if_gap()
    second = of.retry_import_rates_if_gap()  # immediately after → throttled
    assert first["fetched"] is True and len(calls) == 1
    assert second == {"ok": False, "gap": True, "fetched": False, "throttled": True}


# --- fetch serialization (review finding 4) ------------------------------------

def test_concurrent_fetch_skips_instead_of_overlapping(monkeypatch):
    assert of._fetch_in_flight.acquire(blocking=False)
    try:
        res = of.fetch_and_store_rates()
        assert res["ok"] is False and "in flight" in res["error"]
    finally:
        of._fetch_in_flight.release()
