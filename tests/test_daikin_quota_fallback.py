"""Daikin quota-exhausted → physics estimator fallback (#55).

Validates the ``get_lp_state_cached_or_estimated`` wrapper and the LP's
resilience when the Onecta daily quota is exhausted.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src import db
from src.daikin import service as daikin_service


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets a fresh on-disk SQLite file — avoids pollution from any
    ambient DB at project root. ``get_connection`` reads ``config.DB_PATH`` per
    call, so monkeypatching that is enough to redirect all writes."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "DB_PATH", str(db_path))
    db.init_db()
    yield db_path


@pytest.fixture(autouse=True)
def reset_service_state(monkeypatch):
    """Reset module-level cache/flag between tests."""
    monkeypatch.setattr(daikin_service, "_devices_cache", None, raising=False)
    monkeypatch.setattr(daikin_service, "_devices_fetched_monotonic", None, raising=False)
    monkeypatch.setattr(daikin_service, "_devices_fetched_wall", None, raising=False)
    monkeypatch.setattr(daikin_service, "_devices_stale", False, raising=False)
    monkeypatch.setattr(daikin_service, "_cold_start_quota_logged", False, raising=False)
    yield


def _seed_live_row(age_seconds: float, *, tank: float = 50.0, indoor: float = 21.0) -> None:
    now = datetime.now(UTC)
    db.insert_daikin_telemetry({
        "fetched_at": now.timestamp() - age_seconds,
        "source": "live",
        "tank_temp_c": tank,
        "indoor_temp_c": indoor,
        "outdoor_temp_c": 8.0,
    })


def test_fresh_live_row_returned_without_fetch(monkeypatch):
    """A recent live row short-circuits before anything touches the client."""
    _seed_live_row(age_seconds=60, tank=49.0, indoor=20.5)

    def _boom(*a, **kw):
        raise AssertionError("get_cached_devices must not be called on cache hit")

    monkeypatch.setattr(daikin_service, "get_cached_devices", _boom)
    state = daikin_service.get_lp_state_cached_or_estimated()
    assert state["source"] == "live"
    assert state["tank_temp_c"] == 49.0
    assert state["indoor_temp_c"] == 20.5


def test_quota_exhausted_falls_back_to_estimator(monkeypatch):
    """Stale live seed + quota gone → estimator walks from the seed and the
    LP still gets a sensible tank/indoor, no crash."""
    _seed_live_row(age_seconds=4 * 3600, tank=52.0, indoor=21.0)  # 4 h stale

    monkeypatch.setattr(daikin_service, "should_block", lambda vendor: True)

    def _forbidden(*a, **kw):
        raise AssertionError("must not fetch when quota is blocked")

    monkeypatch.setattr(daikin_service, "get_cached_devices", _forbidden)
    state = daikin_service.get_lp_state_cached_or_estimated()

    assert state["source"] == "estimate"
    assert state["tank_temp_c"] is not None
    assert state["indoor_temp_c"] is not None
    # Tank should have decayed a bit toward indoor but not crossed it.
    assert 21.0 < state["tank_temp_c"] < 52.0

    # Estimate row must have been persisted so dashboards can see the fallback.
    latest = db.get_latest_daikin_telemetry()
    assert latest is not None
    assert latest["source"] == "estimate"


def test_quota_exhausted_and_no_seed_returns_degraded(monkeypatch):
    """First boot under quota-exhaustion: nothing to walk from. Return
    ``source='degraded'`` with None temps so the LP uses its config defaults."""
    monkeypatch.setattr(daikin_service, "should_block", lambda vendor: True)
    state = daikin_service.get_lp_state_cached_or_estimated()
    assert state["source"] == "degraded"
    assert state["tank_temp_c"] is None
    assert state["indoor_temp_c"] is None


def test_quota_has_headroom_does_live_fetch(monkeypatch):
    """When stale but quota OK: go to the live path via get_cached_devices."""
    _seed_live_row(age_seconds=2 * 3600, tank=45.0, indoor=19.5)  # stale by default

    monkeypatch.setattr(daikin_service, "should_block", lambda vendor: False)

    class _FakeTemp:
        room_temperature = 20.8
        outdoor_temperature = 9.0

    class _FakeDevice:
        tank_temperature = 50.5
        temperature = _FakeTemp()

    class _FakeResult:
        devices = [_FakeDevice()]
        age_seconds = 0.0

    calls = {"n": 0}

    def _fake_get_cached_devices(*a, **kw):
        calls["n"] += 1
        return _FakeResult()

    monkeypatch.setattr(daikin_service, "get_cached_devices", _fake_get_cached_devices)
    state = daikin_service.get_lp_state_cached_or_estimated()

    assert calls["n"] == 1
    assert state["source"] == "live"
    assert state["tank_temp_c"] == 50.5
    assert state["indoor_temp_c"] == 20.8


def test_cold_start_quota_log_fires_once_then_suppressed(monkeypatch, caplog):
    """Two successive cold-start attempts under a failing client must only
    emit the WARNING once — no more 2-minute log spam loop (#55)."""
    import logging as py_logging

    def _failing_refresh(actor):
        raise RuntimeError("HTTP 429 daikin daily quota")

    monkeypatch.setattr(daikin_service, "_do_refresh", _failing_refresh)
    caplog.set_level(py_logging.WARNING, logger="src.daikin.service")

    daikin_service.get_cached_devices(actor="heartbeat")
    daikin_service.get_cached_devices(actor="heartbeat")
    daikin_service.get_cached_devices(actor="heartbeat")

    msg_count = sum(
        1 for r in caplog.records if "Daikin cold-start fetch failed" in r.getMessage()
    )
    assert msg_count == 1, (
        f"expected exactly one WARN, got {msg_count} — the suppression flag is leaking"
    )
