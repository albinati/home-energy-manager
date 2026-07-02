"""Tests for the negative-price boost Powerful re-assert backstop.

Daikin Powerful is a one-shot the firmware auto-clears, so the once-at-window
boost write leaves the tank coasting for the rest of a paid negative window.
``_check_negative_boost_powerful`` re-asserts Powerful on a bounded cadence
while a ``tank_negative_boost`` window covers now and the tank is below the
boost target. It targets ``completed`` boost rows because the #386 pre-fire
idempotency marks the boost row completed at window start — that's the gap.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db
from src import state_machine as sm
from src.config import config


@dataclass
class _FakeDev:
    id: str = "dev-1"
    name: str = "Altherma"
    tank_on: bool | None = True
    tank_target: float | None = 60.0
    tank_temperature: float | None = 51.0
    tank_powerful: bool | None = False
    is_on: bool | None = True
    lwt_offset: float | None = 0.0


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_MIN_INTERVAL_MINUTES", 15, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "USER_OVERRIDE_RESPECT_HOURS", 4.0, raising=False)
    monkeypatch.setattr(config, "USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END", True, raising=False)
    db.init_db()
    sm._NEG_BOOST_POWERFUL_LAST_UTC = None
    sm._NEG_BOOST_STALL_COUNT = 0
    sm._NEG_BOOST_LAST_TEMP = None
    yield
    sm._NEG_BOOST_POWERFUL_LAST_UTC = None
    sm._NEG_BOOST_STALL_COUNT = 0
    sm._NEG_BOOST_LAST_TEMP = None


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _boost_row(now: datetime, *, status: str = "completed", target: int = 60) -> dict:
    return {
        "id": 1,
        "start_time": _iso(now - timedelta(hours=1)),
        "end_time": _iso(now + timedelta(hours=2)),
        "status": status,
        "action_type": "tank_negative_boost",
        "params": {"tank_power": True, "tank_temp": target, "tank_powerful": True},
    }


NOW = datetime(2026, 6, 28, 11, 0, tzinfo=UTC)


def test_reasserts_powerful_for_completed_boost_below_target():
    """The prod scenario: boost row completed (pre-fire noop'd), tank 51 < 60,
    Powerful off → re-assert."""
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_called_once_with(dev, True)
    assert dev.tank_powerful is True


def test_active_boost_row_left_to_per_row_loop():
    """An active (still-firing) boost row is owned by the per-row apply loop;
    the backstop must NOT double-write."""
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW, status="active")], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_noop_when_no_boost_slot():
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    row = _boost_row(NOW)
    row["action_type"] = "tank_warmup"
    row["params"] = {"tank_power": True, "tank_temp": 45, "tank_powerful": False}
    sm._check_negative_boost_powerful([row], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_solar_charge_row_not_treated_as_negative_boost():
    """solar_charge rows also carry tank_powerful=True but are PV-abundance, not
    paid import — must be excluded by the action_type filter."""
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    row = _boost_row(NOW)
    row["action_type"] = "solar_charge"
    sm._check_negative_boost_powerful([row], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_uses_boost_row_target_not_live_device_target():
    """An overlapping tank_setback pulls live dev.tank_target to 45; the
    headroom check must use the boost row's own 60 °C target."""
    dev = _FakeDev(tank_temperature=51.0, tank_target=45.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW, target=60)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_called_once_with(dev, True)


def test_noop_when_tank_at_boost_target():
    dev = _FakeDev(tank_temperature=60.0, tank_target=45.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW, target=60)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_noop_when_tank_off():
    dev = _FakeDev(tank_on=False, tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_noop_when_tank_temp_unknown():
    dev = _FakeDev(tank_temperature=None, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_disabled_flag_noop(monkeypatch):
    monkeypatch.setattr(config, "DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_ENABLED", False, raising=False)
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_read_only_noop(monkeypatch):
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True, raising=False)
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_vacation_noop(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_cadence_gate_blocks_then_allows():
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    sm._check_negative_boost_powerful(
        [_boost_row(NOW + timedelta(minutes=10))], client, dev, NOW + timedelta(minutes=10), trigger="hb"
    )
    assert client.set_tank_powerful.call_count == 1  # within 15-min interval
    sm._check_negative_boost_powerful(
        [_boost_row(NOW + timedelta(minutes=16))], client, dev, NOW + timedelta(minutes=16), trigger="hb"
    )
    assert client.set_tank_powerful.call_count == 2  # interval elapsed


def test_failure_is_rate_limited_not_retried_every_tick():
    """H2: a failing PATCH must still advance the cadence gate so it's not
    retried (and re-charged quota) every heartbeat."""
    from src.daikin.client import DaikinError
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    client.set_tank_powerful.side_effect = DaikinError("read_only", "boom")
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    # 5 min later (within interval) → must NOT retry despite the failure.
    sm._check_negative_boost_powerful(
        [_boost_row(NOW + timedelta(minutes=5))], client, dev, NOW + timedelta(minutes=5), trigger="hb"
    )
    assert client.set_tank_powerful.call_count == 1


def test_respects_user_override(monkeypatch):
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    monkeypatch.setattr(db, "find_recent_user_override", lambda **kw: {"params": {"tank_power": False}})
    monkeypatch.setattr(sm, "user_gesture_still_in_effect", lambda dev, p: True)
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def _tick(client, dev, at, temp):
    dev.tank_temperature = temp
    sm._check_negative_boost_powerful([_boost_row(at)], client, dev, at, trigger="hb")


def test_stall_backoff_stretches_interval(monkeypatch):
    """2026-07-02: tank pinned at 50-51 °C for 5h while Powerful was re-written
    every 15 min (24 writes, zero gain). After STALL_LIMIT no-progress
    re-asserts the cadence must stretch ×STALL_BACKOFF."""
    monkeypatch.setattr(config, "DHW_NEGATIVE_BOOST_REASSERT_STALL_LIMIT", 3, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_BOOST_REASSERT_STALL_BACKOFF", 4.0, raising=False)
    dev = _FakeDev(tank_powerful=False)
    client = MagicMock()
    # 3 stalled re-asserts at the base 15-min cadence (temp frozen at 51).
    for k in range(3):
        _tick(client, dev, NOW + timedelta(minutes=16 * k), 51.0)
    assert client.set_tank_powerful.call_count == 3
    assert sm._NEG_BOOST_STALL_COUNT == 3
    # Next base-interval attempt is now blocked (backoff = 60 min)...
    _tick(client, dev, NOW + timedelta(minutes=16 * 3), 51.0)
    assert client.set_tank_powerful.call_count == 3
    # ...but allowed once the stretched interval elapses.
    _tick(client, dev, NOW + timedelta(minutes=16 * 2 + 61), 51.0)
    assert client.set_tank_powerful.call_count == 4


def test_progress_resets_stall_count():
    dev = _FakeDev(tank_powerful=False)
    client = MagicMock()
    _tick(client, dev, NOW, 45.0)
    assert sm._NEG_BOOST_STALL_COUNT == 1  # first tick: no baseline yet
    _tick(client, dev, NOW + timedelta(minutes=16), 47.0)  # +2 °C → progress
    assert sm._NEG_BOOST_STALL_COUNT == 0
    assert client.set_tank_powerful.call_count == 2


def test_new_episode_resets_stall_state(monkeypatch):
    """A clearly colder tank (fresh window after the overnight setback) must
    not inherit yesterday's stall verdict."""
    monkeypatch.setattr(config, "DHW_NEGATIVE_BOOST_REASSERT_STALL_LIMIT", 2, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_BOOST_REASSERT_STALL_BACKOFF", 8.0, raising=False)
    dev = _FakeDev(tank_powerful=False)
    client = MagicMock()
    for k in range(2):
        _tick(client, dev, NOW + timedelta(minutes=16 * k), 51.0)
    assert sm._NEG_BOOST_STALL_COUNT == 2  # stalled
    # Next day, tank restarts from 39 °C — 16 min after the (stale) last write
    # would be blocked under backoff, but the colder tank resets the episode…
    later = NOW + timedelta(hours=20)
    _tick(client, dev, later, 39.0)
    # …and the 6h gap alone would too; either way the write goes through.
    assert client.set_tank_powerful.call_count == 3
    assert sm._NEG_BOOST_STALL_COUNT <= 1


def test_audit_row_written():
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    rows = db.get_connection().execute(
        "SELECT result FROM action_log WHERE action='negative_boost_powerful_reassert'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "success"
