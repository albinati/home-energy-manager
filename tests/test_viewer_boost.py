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
    """Counts vendor-cache interactions. The job probes the Daikin cache age
    with allow_refresh=False before spending quota, so only allow_refresh=True
    calls are recorded as refreshes."""

    def __init__(self):
        self.fox: list[int] = []
        self.daikin: list[dict] = []
        self.daikin_age_probes: int = 0
        self.daikin_cache_age: float = float("inf")

    def get_cached_devices(self, *, allow_refresh=False, max_age_seconds=None, actor=""):
        from types import SimpleNamespace
        if allow_refresh:
            self.daikin.append({
                "allow_refresh": allow_refresh,
                "max_age_seconds": max_age_seconds,
                "actor": actor,
            })
            return SimpleNamespace(age_seconds=0.0, devices=[], stale=False)
        self.daikin_age_probes += 1
        return SimpleNamespace(age_seconds=self.daikin_cache_age, devices=[], stale=True)


@pytest.fixture
def calls(monkeypatch):
    c = _Calls()
    monkeypatch.setattr(
        runner, "get_cached_realtime",
        lambda max_age_seconds=None: c.fox.append(max_age_seconds),
    )
    monkeypatch.setattr(
        runner.daikin_service, "get_cached_devices", c.get_cached_devices
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


def test_daikin_age_gate_ignores_write_invalidation(monkeypatch, calls):
    # A young cache (age < target) must NOT be refreshed even though the
    # service marks it stale after a control write — otherwise every
    # reconciler write becomes an extra boost read within 30 s.
    _quota(monkeypatch)
    calls.daikin_cache_age = 120.0  # young; _Calls reports stale=True
    viewer_activity.mark_viewer_active()
    runner.bulletproof_viewer_boost_job()
    assert calls.daikin == []
    assert calls.daikin_age_probes == 1

    # Past the age target the refresh fires.
    calls.daikin_cache_age = 601.0
    runner.bulletproof_viewer_boost_job()
    assert len(calls.daikin) == 1


# --- fox realtime single-flight -------------------------------------------------

def test_fox_realtime_fetch_is_single_flight(monkeypatch):
    """Two threads racing an expired cache must produce ONE Fox HTTP call —
    the coinciding 30 s boost tick + PV telemetry tick used to double-spend."""
    import threading

    from src.foxess import service as fox_service

    monkeypatch.setattr(fox_service, "_last_realtime", None)
    monkeypatch.setattr(fox_service, "_last_realtime_updated_monotonic", None)
    monkeypatch.setattr(fox_service, "should_block", lambda vendor: False)
    monkeypatch.setattr(fox_service, "_record_realtime_refresh", lambda: None)

    fetches = []
    entered = threading.Barrier(2, timeout=5)

    class _Client:
        def get_realtime(self):
            fetches.append(1)
            time.sleep(0.05)  # hold the fetch so the loser is waiting on the lock
            from types import SimpleNamespace
            return SimpleNamespace(soc=50.0, solar_power=1.0, grid_power=0.0)

    monkeypatch.setattr(fox_service, "_get_client", lambda: _Client())

    results = []

    def _race():
        entered.wait()
        results.append(fox_service.get_cached_realtime(max_age_seconds=60))

    threads = [threading.Thread(target=_race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(fetches) == 1
    assert len(results) == 2 and results[0] is results[1]
