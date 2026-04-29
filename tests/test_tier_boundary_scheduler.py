"""Tests for ``_register_tier_boundary_triggers`` in src/scheduler/runner.py.

Mocks the APScheduler instance + the rate fetch so we exercise the schedule-
ordering and dedup logic without spinning a real scheduler.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest


def _fake_rates_for_day(local_date, *, prices):
    """Return 48 half-hour rate rows shaped like db.get_agile_rates_slots_for_local_day."""
    rows = []
    for i, p in enumerate(prices):
        start = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=UTC) + timedelta(minutes=30 * i)
        rows.append({
            "valid_from": start.isoformat().replace("+00:00", "Z"),
            "valid_to": (start + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": float(p),
        })
    return rows


@pytest.fixture
def stub_scheduler(monkeypatch):
    """Replace runner._background_scheduler with a MagicMock so we can inspect add_job calls."""
    from src.scheduler import runner

    sched = MagicMock()
    sched.get_jobs.return_value = []
    monkeypatch.setattr(runner, "_background_scheduler", sched)
    monkeypatch.setattr(runner, "_scheduler_paused", False, raising=False)
    return sched


def test_register_returns_inactive_when_scheduler_missing(monkeypatch):
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_background_scheduler", None)
    out = runner._register_tier_boundary_triggers()
    assert out["status"] == "inactive"
    assert out["scheduled"] == []


def test_register_returns_no_tariff_when_octopus_unset(monkeypatch, stub_scheduler):
    from src.scheduler import runner

    monkeypatch.setattr(runner.config, "OCTOPUS_TARIFF_CODE", "", raising=False)
    out = runner._register_tier_boundary_triggers()
    assert out["status"] == "no_tariff"


def test_register_schedules_one_job_per_window(monkeypatch, stub_scheduler):
    """A simple two-tier day (12 cheap slots + 12 moderate + ...) yields the
    same number of windows as classify_day produces; each gets a unique-id
    job scheduled with the configured lead time."""
    from src.scheduler import runner

    monkeypatch.setattr(runner.config, "OCTOPUS_TARIFF_CODE", "TEST-TARIFF", raising=False)
    monkeypatch.setattr(runner.config, "TIER_BOUNDARY_LEAD_MINUTES", 5, raising=False)
    monkeypatch.setattr(runner.config, "DYNAMIC_REPLAN_MIN_LEAD_MINUTES", 0, raising=False)

    # Construct a day with very far-future timestamps so the lead-time gate
    # never trips (test-stable across clock skew).
    far_future = datetime.now(UTC).date() + timedelta(days=400)
    today_prices = [10.0] * 24 + [25.0] * 24    # cheap → expensive at noon
    tomorrow_prices = [10.0] * 24 + [25.0] * 24

    seen: list[str] = []

    def _fake_get_rates(tariff, local_date, tz_name="Europe/London"):
        # Only return rates for far-future dates so we know the lead is safely positive.
        if local_date >= far_future:
            return _fake_rates_for_day(local_date, prices=today_prices if local_date == far_future else tomorrow_prices)
        return []

    monkeypatch.setattr(runner.db, "get_agile_rates_slots_for_local_day", _fake_get_rates)
    # Force "today" to match our fixture so the +0/+1 day sweep hits the fake data.
    class _FakeDate:
        @staticmethod
        def today_local(tz):
            return far_future
    real_dt = runner.datetime
    class _FakeDateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime as _dt
            return _dt(far_future.year, far_future.month, far_future.day, 0, 0, tzinfo=tz or UTC)
    monkeypatch.setattr(runner, "datetime", _FakeDateTime)

    out = runner._register_tier_boundary_triggers()
    assert out["status"] == "ok"
    assert len(out["scheduled"]) >= 1
    # Each job_id must be unique.
    ids = [j["job_id"] for j in out["scheduled"]]
    assert len(ids) == len(set(ids)), "duplicate job ids"
    # add_job must have been called the same number of times as scheduled.
    assert stub_scheduler.add_job.call_count == len(out["scheduled"])
    # All add_job kwargs carry the tier_boundary trigger reason.
    for call in stub_scheduler.add_job.call_args_list:
        kwargs = call.kwargs.get("kwargs") or {}
        assert kwargs.get("trigger_reason") == "tier_boundary"
        assert kwargs.get("force_write_devices") is True


def test_register_skips_past_windows(monkeypatch, stub_scheduler):
    """Windows whose fire-time is already past must not be scheduled."""
    from src.scheduler import runner

    monkeypatch.setattr(runner.config, "OCTOPUS_TARIFF_CODE", "TEST-TARIFF", raising=False)
    monkeypatch.setattr(runner.config, "TIER_BOUNDARY_LEAD_MINUTES", 5, raising=False)
    monkeypatch.setattr(runner.config, "DYNAMIC_REPLAN_MIN_LEAD_MINUTES", 30, raising=False)

    # All slots are in the past for "today" — the loop should skip them.
    yesterday = datetime.now(UTC).date() - timedelta(days=2)

    def _fake_get_rates(tariff, local_date, tz_name="Europe/London"):
        if local_date == yesterday:
            return _fake_rates_for_day(yesterday, prices=[10.0] * 48)
        return []
    monkeypatch.setattr(runner.db, "get_agile_rates_slots_for_local_day", _fake_get_rates)

    real_dt = runner.datetime
    class _FakeDateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime as _dt
            return _dt(yesterday.year, yesterday.month, yesterday.day, 23, 0, tzinfo=tz or UTC)
    monkeypatch.setattr(runner, "datetime", _FakeDateTime)

    out = runner._register_tier_boundary_triggers()
    # All windows would be in the past relative to "now=23:00 of yesterday".
    assert out["status"] == "ok"
    # No scheduled jobs because every fire_at < now + min_lead.
    assert len(out["scheduled"]) == 0


def test_lp_mpc_hours_setting_removed():
    """V12 architectural shift: the fixed-hour MPC cron is GONE entirely.
    LP_MPC_HOURS is no longer in the runtime-settings SCHEMA, and the
    config Config dataclass no longer exposes LP_MPC_HOURS / LP_MPC_HOURS_LIST.
    """
    from src import runtime_settings
    from src.config import config

    assert "LP_MPC_HOURS" not in runtime_settings.SCHEMA
    assert not hasattr(config, "LP_MPC_HOURS")
    assert not hasattr(config, "LP_MPC_HOURS_LIST")


def test_register_clears_stale_jobs_on_rerun(monkeypatch, stub_scheduler):
    """Re-running the registration must remove any previous tier_boundary jobs
    so a new tariff cycle doesn't leak yesterday's fires."""
    from src.scheduler import runner

    # Pretend two stale jobs exist with the prefix.
    stale_a = MagicMock(); stale_a.id = "tier_boundary_111"
    stale_b = MagicMock(); stale_b.id = "tier_boundary_222"
    other = MagicMock(); other.id = "bulletproof_morning_brief"
    stub_scheduler.get_jobs.return_value = [stale_a, stale_b, other]

    monkeypatch.setattr(runner.config, "OCTOPUS_TARIFF_CODE", "TEST-TARIFF", raising=False)
    monkeypatch.setattr(runner.db, "get_agile_rates_slots_for_local_day", lambda *a, **k: [])

    runner._register_tier_boundary_triggers()
    # Both stale tier_boundary_* jobs were removed; the unrelated brief job was not.
    removed_ids = [c.args[0] for c in stub_scheduler.remove_job.call_args_list]
    assert "tier_boundary_111" in removed_ids
    assert "tier_boundary_222" in removed_ids
    assert "bulletproof_morning_brief" not in removed_ids
