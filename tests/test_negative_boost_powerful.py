"""Tests for the negative-price boost Powerful re-assert backstop.

Daikin Powerful is a one-shot the firmware auto-clears, so the once-at-window
boost write leaves the tank coasting for the rest of a paid negative window.
``_check_negative_boost_powerful`` re-asserts Powerful on a bounded cadence
while a ``tank_negative_boost`` slot is active and the tank is below target.
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
    yield
    sm._NEG_BOOST_POWERFUL_LAST_UTC = None


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _boost_row(now: datetime, *, status: str = "active") -> dict:
    return {
        "id": 1,
        "start_time": _iso(now - timedelta(hours=1)),
        "end_time": _iso(now + timedelta(hours=2)),
        "status": status,
        "action_type": "tank_negative_boost",
        "params": {"tank_power": True, "tank_temp": 60, "tank_powerful": True},
    }


NOW = datetime(2026, 6, 28, 11, 0, tzinfo=UTC)


def test_reasserts_powerful_in_active_boost_below_target():
    dev = _FakeDev(tank_temperature=51.0, tank_target=60.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_called_once_with(dev, True)
    assert dev.tank_powerful is True


def test_noop_when_no_boost_slot_active():
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    # A non-boost row (warmup) → no re-assert.
    row = _boost_row(NOW)
    row["action_type"] = "tank_warmup"
    row["params"] = {"tank_power": True, "tank_temp": 45, "tank_powerful": False}
    sm._check_negative_boost_powerful([row], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_noop_when_tank_at_target():
    dev = _FakeDev(tank_temperature=60.0, tank_target=60.0, tank_powerful=False)
    client = MagicMock()
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
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
    # 10 min later → still within 15-min interval → no second write.
    sm._check_negative_boost_powerful(
        [_boost_row(NOW + timedelta(minutes=10))], client, dev, NOW + timedelta(minutes=10), trigger="hb"
    )
    assert client.set_tank_powerful.call_count == 1
    # 16 min after the first → interval elapsed → re-assert again.
    sm._check_negative_boost_powerful(
        [_boost_row(NOW + timedelta(minutes=16))], client, dev, NOW + timedelta(minutes=16), trigger="hb"
    )
    assert client.set_tank_powerful.call_count == 2


def test_respects_user_override(monkeypatch):
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    monkeypatch.setattr(
        db, "find_recent_user_override",
        lambda **kw: {"params": {"tank_power": False}},
    )
    monkeypatch.setattr(sm, "user_gesture_still_in_effect", lambda dev, p: True)
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_not_called()


def test_failure_is_logged_not_raised(monkeypatch):
    from src.daikin.client import DaikinError
    dev = _FakeDev(tank_temperature=51.0, tank_powerful=False)
    client = MagicMock()
    client.set_tank_powerful.side_effect = DaikinError("read_only", "boom")
    # Must not raise.
    sm._check_negative_boost_powerful([_boost_row(NOW)], client, dev, NOW, trigger="hb")
    client.set_tank_powerful.assert_called_once()
