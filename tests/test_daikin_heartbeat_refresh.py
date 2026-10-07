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


def _cached(dev: DaikinDevice | None, source="fresh", age=0.0):
    return SimpleNamespace(devices=[dev] if dev else [], source=source, age_seconds=age, stale=False, fetched_at_wall=None)


def test_apply_schedules_verify_job(monkeypatch):
    from src import daikin_bulletproof as dbp

    sched = MagicMock()
    monkeypatch.setattr("src.scheduler.runner.get_background_scheduler", lambda: sched)
    job_id = dbp.schedule_post_write_verify({"lwt_offset": 3}, trigger="test")
    assert job_id and job_id.startswith("daikin_verify_")
    assert sched.add_job.call_count == 1
    kw = sched.add_job.call_args.kwargs
    assert kw["id"] == job_id and kw["kwargs"]["expected"] == {"lwt_offset": 3}


def test_apply_without_scheduler_or_disabled_is_noop(monkeypatch):
    from src import daikin_bulletproof as dbp

    monkeypatch.setattr("src.scheduler.runner.get_background_scheduler", lambda: None)
    assert dbp.schedule_post_write_verify({"lwt_offset": 3}, trigger="test") is None
    sched = MagicMock()
    monkeypatch.setattr("src.scheduler.runner.get_background_scheduler", lambda: sched)
    monkeypatch.setattr(config, "DAIKIN_POST_WRITE_VERIFY_ENABLED", False, raising=False)
    assert dbp.schedule_post_write_verify({"lwt_offset": 3}, trigger="test") is None
    assert sched.add_job.call_count == 0


def test_verify_logs_matched(monkeypatch):
    from src import daikin_bulletproof as dbp

    dev = DaikinDevice(id="gw", name="x", lwt_offset=3.0)
    calls: list[dict] = []
    monkeypatch.setattr("src.daikin.service.get_cached_devices", lambda **kw: calls.append(kw) or _cached(dev))
    logged: list[dict] = []
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: logged.append(kw))
    alerts: list = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: alerts.append(msg))
    out = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at="2026-10-07T12:00:00Z")
    assert out["matched"] is True
    assert calls[0]["allow_refresh"] is True and calls[0]["max_age_seconds"] == 0
    assert logged[0]["action"] == "daikin_write_verify" and logged[0]["result"] == "success"
    assert alerts == []


def test_verify_mismatch_notifies_once(monkeypatch):
    from src import daikin_bulletproof as dbp

    dbp._VERIFY_ALERTED.clear()
    dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)
    monkeypatch.setattr("src.daikin.service.get_cached_devices", lambda **kw: _cached(dev))
    logged: list[dict] = []
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: logged.append(kw))
    alerts: list = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: alerts.append((msg, extra)))
    key = "2026-10-07T12:00:00Z"
    out = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at=key)
    out2 = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at=key)
    assert out["matched"] is False and out2["matched"] is False
    assert logged[0]["result"] == "failure" and logged[0]["params"]["actual"]["lwt_offset"] == 0.0
    assert len(alerts) == 1 and alerts[0][1]["warning_key"] == f"daikin_write_verify_{key}"


def test_verify_tolerates_read_failure(monkeypatch):
    from src import daikin_bulletproof as dbp

    def _boom(**kw):
        raise RuntimeError("quota")

    monkeypatch.setattr("src.daikin.service.get_cached_devices", _boom)
    logged: list[dict] = []
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: logged.append(kw))
    out = dbp.post_write_verify_job(expected={"lwt_offset": 3}, trigger="test", written_at="2026-10-07T13:00:00Z")
    assert out["matched"] is None and "quota" in out["error"]
    assert logged[0]["result"] == "failure"


def test_apply_path_calls_schedule_on_success(monkeypatch):
    """The wire: a real apply_scheduled_daikin_params success schedules the verify."""
    from src import daikin_bulletproof as dbp

    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(dbp.db, "log_action", lambda **kw: None)
    scheduled: list = []
    monkeypatch.setattr(dbp, "schedule_post_write_verify", lambda p, *, trigger: scheduled.append((p, trigger)))
    dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0, is_on=True)
    client = MagicMock()
    client.set_lwt_offset = MagicMock()
    ok = dbp.apply_scheduled_daikin_params(dev, client, {"lwt_offset": 3}, trigger="test")
    assert ok is True and scheduled and scheduled[0][0]["lwt_offset"] == 3
