"""PV realtime telemetry: helper, idempotency, presence_periods, cron job behaviour."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """Each test runs against a throwaway DB."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    # Force module-level path cache to refresh
    from src import db
    monkeypatch.setattr(db, "_DB_PATH", str(db_path), raising=False)
    db.init_db()
    yield


def test_save_pv_realtime_sample_inserts_row():
    from src import db
    ok = db.save_pv_realtime_sample(
        "2026-05-01T12:00:00+00:00",
        solar_power_kw=2.5,
        soc_pct=80.0,
        load_power_kw=0.5,
        grid_import_kw=0.0,
        grid_export_kw=2.0,
        battery_charge_kw=0.0,
        battery_discharge_kw=0.0,
        source="test",
    )
    assert ok is True


def test_save_pv_realtime_sample_idempotent_on_duplicate_timestamp():
    from src import db
    ts = "2026-05-01T13:00:00+00:00"
    first = db.save_pv_realtime_sample(ts, solar_power_kw=1.0, source="test")
    second = db.save_pv_realtime_sample(ts, solar_power_kw=999.0, source="test")
    assert first is True
    assert second is False  # duplicate silently ignored


def test_presence_period_lookup_returns_kind_when_in_range():
    from src import db
    db.add_presence_period(
        "2026-04-01T00:00:00+00:00",
        "2026-04-15T23:59:59+00:00",
        "travel",
        "test",
    )
    assert db.get_presence_at("2026-04-10T12:00:00+00:00") == "travel"
    assert db.get_presence_at("2026-04-20T12:00:00+00:00") == "home"  # outside range
    assert db.get_presence_at("2026-03-01T12:00:00+00:00") == "home"  # before range


def test_presence_period_rejects_invalid_kind():
    from src import db
    with pytest.raises(ValueError):
        db.add_presence_period("2026-04-01T00:00:00+00:00", "2026-04-15T23:59:59+00:00", "vacation", "")


def test_pv_telemetry_job_persists_when_realtime_available(monkeypatch):
    from src.scheduler import runner
    from src import db

    fake_rt = MagicMock(soc=75.0, solar_power=2.0, load_power=0.5, grid_power=-1.5, battery_power=1.0, work_mode="SelfUse")
    monkeypatch.setattr(runner, "get_cached_realtime", lambda: fake_rt)
    monkeypatch.setattr(runner, "_scheduler_paused", False)

    runner.bulletproof_pv_telemetry_job()

    # Confirm a row landed in the table
    from src.db import _lock, get_connection
    with _lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT solar_power_kw, soc_pct, load_power_kw, grid_export_kw, source FROM pv_realtime_history"
            ).fetchone()
        finally:
            conn.close()
    assert row is not None
    assert row[0] == 2.0
    assert row[1] == 75.0
    assert row[2] == 0.5
    assert row[3] == 1.5  # negative grid_power → export side
    assert row[4] == "heartbeat"


def test_pv_telemetry_job_silent_when_realtime_empty(monkeypatch):
    from src.scheduler import runner

    fake_rt = MagicMock(soc=None, solar_power=None, load_power=None, grid_power=None, battery_power=None, work_mode="unknown")
    monkeypatch.setattr(runner, "get_cached_realtime", lambda: fake_rt)
    monkeypatch.setattr(runner, "_scheduler_paused", False)

    # Should not raise; should not write
    runner.bulletproof_pv_telemetry_job()

    from src.db import _lock, get_connection
    with _lock:
        conn = get_connection()
        try:
            n = conn.execute("SELECT COUNT(*) FROM pv_realtime_history").fetchone()[0]
        finally:
            conn.close()
    assert n == 0


def test_pv_telemetry_job_skipped_when_scheduler_paused(monkeypatch):
    from src.scheduler import runner

    fake_rt = MagicMock(soc=75.0, solar_power=2.0, load_power=0.5, grid_power=0.0, battery_power=0.0, work_mode="SelfUse")
    spy = MagicMock(side_effect=AssertionError("must not call get_cached_realtime when paused"))
    monkeypatch.setattr(runner, "get_cached_realtime", spy)
    monkeypatch.setattr(runner, "_scheduler_paused", True)

    runner.bulletproof_pv_telemetry_job()
    spy.assert_not_called()
