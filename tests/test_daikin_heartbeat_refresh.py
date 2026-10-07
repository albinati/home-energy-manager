"""Daikin cadence (#803 / #809): bounded heartbeat refresh + post-write verify."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import db
from src.config import config
from src.daikin.models import DaikinDevice


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_RESERVE_FOR_HEARTBEAT", 30, raising=False)
    monkeypatch.setattr(config, "DAIKIN_HEARTBEAT_REFRESH_MIN_HEADROOM", 40, raising=False)
    monkeypatch.setattr(config, "DAIKIN_HEARTBEAT_REFRESH_SECONDS", 1800, raising=False)
    monkeypatch.setattr(config, "DAIKIN_POST_WRITE_VERIFY_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_POST_WRITE_VERIFY_SECONDS", 120, raising=False)
    db.init_db()


# ── heartbeat refresh gate ───────────────────────────────────────────────────


def test_heartbeat_refresh_respects_flag_and_headroom(monkeypatch):
    from src.scheduler import runner

    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 150)
    monkeypatch.setattr(config, "DAIKIN_HEARTBEAT_REFRESH_ENABLED", False, raising=False)
    assert runner._heartbeat_daikin_refresh_allowed() is False  # kill switch / Phase A default
    monkeypatch.setattr(config, "DAIKIN_HEARTBEAT_REFRESH_ENABLED", True, raising=False)
    assert runner._heartbeat_daikin_refresh_allowed() is True


def test_heartbeat_refresh_blocked_when_quota_low(monkeypatch):
    from src.scheduler import runner

    monkeypatch.setattr(config, "DAIKIN_HEARTBEAT_REFRESH_ENABLED", True, raising=False)
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 70)   # == reserve + headroom → no
    assert runner._heartbeat_daikin_refresh_allowed() is False
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 71)
    assert runner._heartbeat_daikin_refresh_allowed() is True


def test_daily_budget_fits_projected_reads_and_writes():
    """Documented arithmetic (#809): the bounded cadence fits the 180/day
    budget with the reserve intact."""
    heartbeat = 86400 // int(config.DAIKIN_HEARTBEAT_REFRESH_SECONDS)   # 48
    verify, rollups, lp_init, viewer, writes = 10, 4, 2, 10, 20
    total = heartbeat + verify + rollups + lp_init + viewer + writes
    assert heartbeat == 48
    assert total <= int(config.DAIKIN_DAILY_BUDGET) - int(config.DAIKIN_RESERVE_FOR_HEARTBEAT) - 40


# ── post-write verification ──────────────────────────────────────────────────


def _cached(dev: DaikinDevice | None, source="fresh", age=0.0, wall=100.0):
    return SimpleNamespace(devices=[dev] if dev else [], source=source, age_seconds=age, stale=False, fetched_at_wall=wall)


def _service_reads(monkeypatch, dev: DaikinDevice, *, fresh: bool = True):
    """First call (allow_refresh=False) reports the pre-read cache wall; the
    refresh call reports a LATER wall when fresh, the same wall otherwise."""
    calls: list[dict] = []

    def _get(**kw):
        calls.append(kw)
        if not kw.get("allow_refresh"):
            return _cached(dev, source="cache", wall=100.0)
        if fresh:
            return _cached(dev, source="fresh", wall=160.0)
        return _cached(dev, source="cache_throttled", age=30.0, wall=100.0)

    monkeypatch.setattr("src.daikin.service.get_cached_devices", _get)
    return calls


def test_apply_schedules_verify_job_with_written_keys_only(monkeypatch):
    from src import daikin_bulletproof as dbp

    sched = MagicMock()
    sched.get_job.return_value = None
    monkeypatch.setattr("src.scheduler.runner.get_background_scheduler", lambda: sched)
    job_id = dbp.schedule_post_write_verify({"lwt_offset": 3}, trigger="test")
    assert job_id == dbp._VERIFY_JOB_ID
    kw = sched.add_job.call_args.kwargs
    assert kw["id"] == job_id and kw["replace_existing"] is True and kw["misfire_grace_time"] == 120
    assert kw["kwargs"]["expected"] == {"lwt_offset": 3} and kw["kwargs"]["attempt"] == 1
    # delay floored above the service's 90 s anti-burst interval
    fire_at = sched.add_job.call_args.args[1].run_date
    from datetime import UTC, datetime
    assert (fire_at - datetime.now(UTC)).total_seconds() >= 115


def test_second_write_merges_into_pending_job(monkeypatch):
    from src import daikin_bulletproof as dbp

    sched = MagicMock()
    sched.get_job.return_value = SimpleNamespace(kwargs={"expected": {"lwt_offset": 3}})
    monkeypatch.setattr("src.scheduler.runner.get_background_scheduler", lambda: sched)
    dbp.schedule_post_write_verify({"tank_temp": 45}, trigger="test")
    assert sched.add_job.call_args.kwargs["kwargs"]["expected"] == {"lwt_offset": 3, "tank_temp": 45}


def test_apply_without_scheduler_or_disabled_or_nothing_written_is_noop(monkeypatch):
    from src import daikin_bulletproof as dbp

    monkeypatch.setattr("src.scheduler.runner.get_background_scheduler", lambda: None)
    assert dbp.schedule_post_write_verify({"lwt_offset": 3}, trigger="test") is None
    sched = MagicMock()
    monkeypatch.setattr("src.scheduler.runner.get_background_scheduler", lambda: sched)
    assert dbp.schedule_post_write_verify({}, trigger="test") is None
    monkeypatch.setattr(config, "DAIKIN_POST_WRITE_VERIFY_ENABLED", False, raising=False)
    assert dbp.schedule_post_write_verify({"lwt_offset": 3}, trigger="test") is None
    assert sched.add_job.call_count == 0


def test_verify_logs_matched_on_fresh_read(monkeypatch):
    from src import daikin_bulletproof as dbp

    dev = DaikinDevice(id="gw", name="x", lwt_offset=3.0)
    calls = _service_reads(monkeypatch, dev, fresh=True)
    logged: list[dict] = []
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: logged.append(kw))
    alerts: list = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: alerts.append(msg))
    out = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at="2026-10-07T12:00:00Z")
    assert out["matched"] is True and out["fresh"] is True
    assert any(c.get("allow_refresh") and c.get("max_age_seconds") == 0 for c in calls)
    assert logged[0]["action"] == "daikin_write_verify" and logged[0]["result"] == "success"
    assert alerts == []


def test_verify_throttled_read_is_unverified_not_success(monkeypatch):
    """A read inside the 90 s floor returns the in-place-mutated cache: it
    must never count as a verified match."""
    from src import daikin_bulletproof as dbp

    dev = DaikinDevice(id="gw", name="x", lwt_offset=3.0)
    _service_reads(monkeypatch, dev, fresh=False)
    logged: list[dict] = []
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: logged.append(kw))
    out = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at="2026-10-07T12:01:00Z")
    assert out["matched"] is None and out["fresh"] is False and out["cache_source"] == "cache_throttled"
    assert logged[0]["result"] == "unverified"


def test_verify_mismatch_retries_once_then_alerts_once(monkeypatch):
    from src import daikin_bulletproof as dbp

    db.init_db()
    dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)
    _service_reads(monkeypatch, dev, fresh=True)
    logged: list[dict] = []
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: logged.append(kw))
    alerts: list = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: alerts.append((msg, extra)))
    rescheduled: list[dict] = []
    monkeypatch.setattr(dbp, "schedule_post_write_verify", lambda w, **kw: rescheduled.append({"written": w, **kw}))
    key = "2026-10-07T12:02:00Z"
    out1 = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at=key, attempt=1)
    assert out1["matched"] is False and alerts == []
    assert rescheduled and rescheduled[0]["attempt"] == 2 and rescheduled[0]["delay_s"] == 180
    out2 = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at=key, attempt=2)
    out3 = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at=key, attempt=2)
    assert out2["matched"] is False and out3["matched"] is False
    assert len(alerts) == 1 and alerts[0][1]["warning_key"] == f"daikin_write_verify_{key}"
    assert logged[-1]["result"] == "failure" and logged[-1]["params"]["actual"]["lwt_offset"] == 0.0


def test_verify_tolerates_read_failure(monkeypatch):
    from src import daikin_bulletproof as dbp

    def _boom(**kw):
        raise RuntimeError("quota")

    monkeypatch.setattr("src.daikin.service.get_cached_devices", _boom)
    logged: list[dict] = []
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: logged.append(kw))
    out = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at="2026-10-07T13:00:00Z")
    assert out["matched"] is None and "quota" in out["error"]
    assert logged[0]["result"] == "unverified"


def test_apply_path_schedules_only_written_keys(monkeypatch):
    """The wire: a real apply writes lwt_offset, SKIPS a tank_temp the device
    already holds, and schedules the verify with the written key only."""
    from src import daikin_bulletproof as dbp

    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: None)
    scheduled: list = []
    monkeypatch.setattr(dbp, "schedule_post_write_verify", lambda w, *, trigger, **kw: scheduled.append((w, trigger)))
    dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0, is_on=True, tank_on=True, tank_target=45.0)
    client = MagicMock()
    ok = dbp.apply_scheduled_daikin_params(dev, client, {"lwt_offset": 3, "tank_temp": 45}, trigger="test")
    assert ok is True and scheduled == [({"lwt_offset": 3}, "test")]


def test_apply_lwt_skipped_with_zone_off_schedules_nothing(monkeypatch):
    from src import daikin_bulletproof as dbp

    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: None)
    scheduled: list = []
    monkeypatch.setattr(dbp, "schedule_post_write_verify", lambda w, *, trigger, **kw: scheduled.append(w))
    dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0, is_on=False)
    client = MagicMock()
    dbp.apply_scheduled_daikin_params(dev, client, {"lwt_offset": 3}, trigger="test")
    assert scheduled == []  # lwt_offset not writable with the zone off → nothing to verify


def test_service_labels_throttled_refresh_honestly(tmp_path, monkeypatch):
    """get_cached_devices(allow_refresh=True, max_age_seconds=0) inside the
    90 s floor must not report source=fresh/age 0 for pre-existing data."""
    import importlib
    import time

    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import src.daikin.service as svc
    importlib.reload(svc)
    monkeypatch.setattr(svc, "should_block", lambda vendor: False)
    dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)
    svc._devices_cache = [dev]
    svc._devices_fetched_wall = time.time() - 30
    svc._last_refresh_monotonic = time.monotonic() - 30   # last REAL read 30 s ago (< 90 s floor)
    svc._devices_stale = False
    res = svc.get_cached_devices(allow_refresh=True, max_age_seconds=0, actor="post_write_verify")
    assert res.source == "cache_throttled" and res.age_seconds >= 25
