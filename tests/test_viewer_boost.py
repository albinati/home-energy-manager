"""Viewer-aware freshness boost: viewer_activity tracker + the 30 s runner job.

The job must be a strict no-op without a viewer, refresh Fox/Daikin with the
configured max-age targets while someone is watching, and stand down per-vendor
as soon as quota headroom falls to the reserve.
"""
import time

import pytest

from src import viewer_activity
from src.config import config
from src.scheduler import runner


@pytest.fixture(autouse=True)
def _reset_activity(monkeypatch):
    monkeypatch.setattr(viewer_activity, "_last_viewer_hit_monotonic", None)
    monkeypatch.setattr(config, "VIEWER_BOOST_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "VIEWER_ACTIVE_WINDOW_SECONDS", 90, raising=False)
    monkeypatch.setattr(config, "FOX_VIEWER_REFRESH_SECONDS", 60, raising=False)
    monkeypatch.setattr(config, "FOX_VIEWER_QUOTA_RESERVE", 300, raising=False)
    monkeypatch.setattr(config, "DAIKIN_VIEWER_REFRESH_SECONDS", 600, raising=False)
    monkeypatch.setattr(config, "DAIKIN_VIEWER_QUOTA_RESERVE", 80, raising=False)
    monkeypatch.setattr(runner, "get_scheduler_paused", lambda: False)


class _Calls:
    def __init__(self):
        self.fox: list[int] = []
        self.daikin: list[dict] = []


@pytest.fixture
def calls(monkeypatch):
    c = _Calls()
    monkeypatch.setattr(
        runner, "get_cached_realtime",
        lambda max_age_seconds=None: c.fox.append(max_age_seconds),
    )
    monkeypatch.setattr(
        runner.daikin_service, "get_cached_devices",
        lambda **kw: c.daikin.append(kw),
    )
    return c


def _quota(monkeypatch, *, fox=1000, daikin=150):
    import src.api_quota as api_quota
    monkeypatch.setattr(
        api_quota, "quota_remaining",
        lambda vendor: fox if vendor == "fox" else daikin,
    )


# --- viewer_activity tracker -------------------------------------------------

def test_tracker_inactive_before_first_hit():
    assert viewer_activity.viewer_active(90) is False
    assert viewer_activity.seconds_since_last_viewer() is None


def test_tracker_active_within_window_and_expires(monkeypatch):
    viewer_activity.mark_viewer_active()
    assert viewer_activity.viewer_active(90) is True
    # Simulate the hit having landed 91 s ago.
    monkeypatch.setattr(
        viewer_activity, "_last_viewer_hit_monotonic", time.monotonic() - 91
    )
    assert viewer_activity.viewer_active(90) is False
    assert viewer_activity.seconds_since_last_viewer() >= 91


# --- boost job ----------------------------------------------------------------

def test_noop_without_viewer(monkeypatch, calls):
    _quota(monkeypatch)
    runner.bulletproof_viewer_boost_job()
    assert calls.fox == [] and calls.daikin == []


def test_refreshes_both_vendors_while_viewing(monkeypatch, calls):
    _quota(monkeypatch)
    viewer_activity.mark_viewer_active()
    runner.bulletproof_viewer_boost_job()
    assert calls.fox == [60]
    assert len(calls.daikin) == 1
    kw = calls.daikin[0]
    assert kw["allow_refresh"] is True
    assert kw["max_age_seconds"] == 600
    assert kw["actor"] == "viewer_boost"


def test_quota_reserve_gates_per_vendor(monkeypatch, calls):
    # Fox below reserve, Daikin above: only Daikin refreshes.
    _quota(monkeypatch, fox=300, daikin=150)
    viewer_activity.mark_viewer_active()
    runner.bulletproof_viewer_boost_job()
    assert calls.fox == []
    assert len(calls.daikin) == 1

    # Daikin at reserve too: full stand-down.
    calls.daikin.clear()
    _quota(monkeypatch, fox=300, daikin=80)
    runner.bulletproof_viewer_boost_job()
    assert calls.fox == [] and calls.daikin == []


def test_zero_target_disables_that_vendor(monkeypatch, calls):
    _quota(monkeypatch)
    monkeypatch.setattr(config, "FOX_VIEWER_REFRESH_SECONDS", 0, raising=False)
    viewer_activity.mark_viewer_active()
    runner.bulletproof_viewer_boost_job()
    assert calls.fox == []
    assert len(calls.daikin) == 1


def test_master_switch_and_pause(monkeypatch, calls):
    _quota(monkeypatch)
    viewer_activity.mark_viewer_active()
    monkeypatch.setattr(config, "VIEWER_BOOST_ENABLED", False, raising=False)
    runner.bulletproof_viewer_boost_job()
    assert calls.fox == [] and calls.daikin == []

    monkeypatch.setattr(config, "VIEWER_BOOST_ENABLED", True, raising=False)
    monkeypatch.setattr(runner, "get_scheduler_paused", lambda: True)
    runner.bulletproof_viewer_boost_job()
    assert calls.fox == [] and calls.daikin == []


def test_vendor_failure_does_not_block_the_other(monkeypatch, calls):
    _quota(monkeypatch)

    def _boom(max_age_seconds=None):
        raise RuntimeError("fox down")

    monkeypatch.setattr(runner, "get_cached_realtime", _boom)
    viewer_activity.mark_viewer_active()
    runner.bulletproof_viewer_boost_job()  # must not raise
    assert len(calls.daikin) == 1
