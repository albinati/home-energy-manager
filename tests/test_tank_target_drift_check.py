"""Tests for the PR F tank-target drift heartbeat check.

Mirrors `tests/test_tank_drift_check.py` for the power-drift check. The
target-drift check catches "commanded tank_temp > NORMAL with no upcoming
heating action" — typically a stale solar_preheat target after PV
forecast collapsed mid-window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db as _db
from src import state_machine as sm
from src.config import config


@dataclass
class _FakeDev:
    id: str = "dev-1"
    name: str = "Altherma"
    tank_on: bool | None = True
    tank_target: float | None = None
    tank_powerful: bool | None = None
    is_on: bool | None = None
    lwt_offset: float | None = None


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    _db.init_db()
    monkeypatch.setattr(config, "TANK_TARGET_DRIFT_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TANK_TARGET_DRIFT_TOLERANCE_C", 1.0, raising=False)
    monkeypatch.setattr(config, "TANK_TARGET_DRIFT_LOOKAHEAD_MIN", 30, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "USER_OVERRIDE_RESPECT_HOURS", 4.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    sm._TANK_TARGET_DRIFT_NOTIFIED = False
    yield
    sm._TANK_TARGET_DRIFT_NOTIFIED = False


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


def test_disabled_flag_skips_check(monkeypatch):
    monkeypatch.setattr(config, "TANK_TARGET_DRIFT_CHECK_ENABLED", False, raising=False)
    dev = _FakeDev(tank_target=55.0)
    client = MagicMock()
    apply_mock = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    sm._check_tank_target_drift([], client, dev, datetime(2026, 6, 1, 14, 0, tzinfo=UTC), trigger="hb")
    apply_mock.assert_not_called()


def test_vacation_mode_skips_check(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    dev = _FakeDev(tank_target=55.0)
    client = MagicMock()
    apply_mock = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    sm._check_tank_target_drift([], client, dev, datetime(2026, 6, 1, 14, 0, tzinfo=UTC), trigger="hb")
    apply_mock.assert_not_called()


def test_passive_mode_skips_check(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    dev = _FakeDev(tank_target=55.0)
    client = MagicMock()
    apply_mock = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    sm._check_tank_target_drift([], client, dev, datetime(2026, 6, 1, 14, 0, tzinfo=UTC), trigger="hb")
    apply_mock.assert_not_called()


def test_unknown_target_skips_check(monkeypatch):
    dev = _FakeDev(tank_target=None)
    client = MagicMock()
    apply_mock = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    sm._check_tank_target_drift([], client, dev, datetime(2026, 6, 1, 14, 0, tzinfo=UTC), trigger="hb")
    apply_mock.assert_not_called()


def test_target_at_normal_skips_check(monkeypatch):
    dev = _FakeDev(tank_target=45.5)  # within tolerance
    client = MagicMock()
    apply_mock = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    sm._check_tank_target_drift([], client, dev, datetime(2026, 6, 1, 14, 0, tzinfo=UTC), trigger="hb")
    apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Heating-intent gate
# ---------------------------------------------------------------------------


def test_upcoming_solar_preheat_is_not_drift(monkeypatch):
    """When the next 30 min has a pending action with tank_temp > NORMAL,
    the current high target is intentional — no drift."""
    dev = _FakeDev(tank_target=50.0)
    client = MagicMock()
    apply_mock = MagicMock()
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", MagicMock())
    now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    actions = [{
        "id": 1,
        "start_time": _iso(now + timedelta(minutes=15)),
        "end_time": _iso(now + timedelta(hours=1)),
        "status": "pending",
        "action_type": "solar_preheat",
        "params": {"tank_power": True, "tank_temp": 50},
    }]
    sm._check_tank_target_drift(actions, client, dev, now, trigger="hb")
    apply_mock.assert_not_called()
    notify_risk.assert_not_called()


def test_in_flight_solar_preheat_is_not_drift(monkeypatch):
    """Active action with end > now and tank_temp high → not drift."""
    dev = _FakeDev(tank_target=50.0)
    client = MagicMock()
    apply_mock = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    actions = [{
        "id": 1,
        "start_time": _iso(now - timedelta(minutes=30)),
        "end_time": _iso(now + timedelta(minutes=30)),
        "status": "active",
        "action_type": "solar_preheat",
        "params": {"tank_power": True, "tank_temp": 50},
    }]
    sm._check_tank_target_drift(actions, client, dev, now, trigger="hb")
    apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Drift detected → recovery
# ---------------------------------------------------------------------------


def test_drift_with_no_upcoming_heating_recovers(monkeypatch):
    """Tank target=50, no heating planned in next 30 min → recover to 45."""
    dev = _FakeDev(tank_target=50.0)
    client = MagicMock()
    apply_mock = MagicMock()
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", MagicMock())
    sm._check_tank_target_drift(
        [], client, dev, datetime(2026, 6, 1, 14, 0, tzinfo=UTC), trigger="hb",
    )
    apply_mock.assert_called_once()
    call_args = apply_mock.call_args
    assert call_args.kwargs["params"]["tank_temp"] == 45
    assert call_args.kwargs["params"]["tank_power"] is True
    notify_risk.assert_called_once()
    assert sm._TANK_TARGET_DRIFT_NOTIFIED is True


def test_drift_dedup_within_episode(monkeypatch):
    """Multiple consecutive heartbeats with drift → only one notification."""
    dev = _FakeDev(tank_target=52.0)
    client = MagicMock()
    apply_mock = MagicMock()
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", MagicMock())
    now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    sm._check_tank_target_drift([], client, dev, now, trigger="hb1")
    sm._check_tank_target_drift([], client, dev, now + timedelta(minutes=2), trigger="hb2")
    sm._check_tank_target_drift([], client, dev, now + timedelta(minutes=4), trigger="hb3")
    assert notify_risk.call_count == 1


def test_drift_dedup_resets_when_target_drops(monkeypatch):
    """After tank target drops back to NORMAL, dedup clears so a new
    drift episode re-notifies."""
    dev = _FakeDev(tank_target=52.0)
    client = MagicMock()
    apply_mock = MagicMock()
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", MagicMock())
    now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    sm._check_tank_target_drift([], client, dev, now, trigger="hb1")
    assert notify_risk.call_count == 1

    # Recovery happens (apply_mock would set tank to 45). Simulate by
    # updating dev.tank_target.
    dev.tank_target = 45.0
    sm._check_tank_target_drift([], client, dev, now + timedelta(minutes=10), trigger="hb2")
    assert sm._TANK_TARGET_DRIFT_NOTIFIED is False

    # Fresh drift episode
    dev.tank_target = 53.0
    sm._check_tank_target_drift([], client, dev, now + timedelta(minutes=20), trigger="hb3")
    assert notify_risk.call_count == 2


# ---------------------------------------------------------------------------
# User-override respect
# ---------------------------------------------------------------------------


def test_user_override_lifted_target_respected(monkeypatch):
    """If a recent user override is still in effect (live state matches
    the overridden row's params), the check defers to the user."""
    dev = _FakeDev(tank_target=55.0)
    client = MagicMock()
    apply_mock = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", MagicMock())
    monkeypatch.setattr(sm, "notify_critical", MagicMock())
    now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    aid = _db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(now - timedelta(minutes=30)),
        end_time=_iso(now + timedelta(hours=1)),
        device="daikin", action_type="solar_preheat",
        # Original LP-planned target was NORMAL (45); user lifted to 55.
        # `user_gesture_still_in_effect`: live tank_target (55) differs
        # from override row's tank_temp (45) → gesture is still in effect.
        params={"tank_power": True, "tank_temp": 45}, status="active",
    )
    _db.mark_action_user_overridden(
        aid, overridden_at=(now - timedelta(minutes=20)).isoformat(),
    )
    sm._check_tank_target_drift([], client, dev, now, trigger="hb")
    apply_mock.assert_not_called()
