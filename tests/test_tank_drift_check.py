"""Tests for the issue-#382 heartbeat tank-power drift check.

The check sits at the end of ``_reconcile_daikin_actions``. It alerts and
(optionally) force-restores when the live tank is OFF and no plan slot
intends it to be OFF — the belt-and-braces backstop for any future class
of bugs that strands the tank in a shutdown state without a recovery row.
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
    tank_on: bool | None = None
    tank_target: float | None = None
    tank_powerful: bool | None = None
    is_on: bool | None = None
    lwt_offset: float | None = None


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(config, "TANK_DRIFT_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TANK_DRIFT_AUTO_RECOVER", True, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "USER_OVERRIDE_RESPECT_HOURS", 4.0, raising=False)
    db.init_db()
    # Reset the module-level dedup token so each test starts fresh.
    sm._TANK_DRIFT_NOTIFIED = False
    yield


def _drift_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def test_no_drift_when_tank_on() -> None:
    dev = _FakeDev(tank_on=True)
    client = MagicMock()
    sm._check_tank_power_drift([], client, dev, datetime(2026, 6, 1, 18, 0, tzinfo=UTC), trigger="hb")
    client.assert_not_called()
    assert sm._TANK_DRIFT_NOTIFIED is False


def test_no_drift_when_tank_state_unknown() -> None:
    dev = _FakeDev(tank_on=None)
    client = MagicMock()
    sm._check_tank_power_drift([], client, dev, datetime(2026, 6, 1, 18, 0, tzinfo=UTC), trigger="hb")
    client.assert_not_called()


def test_no_drift_when_planned_shutdown_active(monkeypatch) -> None:
    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    now = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
    actions = [{
        "id": 1,
        "start_time": _drift_iso(datetime(2026, 6, 1, 16, 30, tzinfo=UTC)),
        "end_time": _drift_iso(datetime(2026, 6, 1, 18, 0, tzinfo=UTC)),
        "status": "active",
        "action_type": "shutdown",
        "params": {"tank_power": False},
    }]
    notify = MagicMock()
    monkeypatch.setattr(sm, "notify_critical", notify)
    monkeypatch.setattr(sm, "notify_risk", notify)
    sm._check_tank_power_drift(actions, client, dev, now, trigger="hb")
    client.assert_not_called()
    notify.assert_not_called()


def test_drift_detected_alerts_and_recovers(monkeypatch) -> None:
    """Tank OFF, no planned shutdown, no user gesture → alert + comfort restore."""
    dev = _FakeDev(tank_on=False, tank_target=37.0)
    client = MagicMock()
    notify_risk = MagicMock()
    notify_critical = MagicMock()
    apply_restore = MagicMock()
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", notify_critical)
    monkeypatch.setattr(sm, "apply_comfort_restore", apply_restore)

    now = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)
    sm._check_tank_power_drift([], client, dev, now, trigger="hb")

    apply_restore.assert_called_once()
    notify_risk.assert_called_once()
    notify_critical.assert_not_called()
    assert sm._TANK_DRIFT_NOTIFIED is True


def test_drift_dedup_within_episode(monkeypatch) -> None:
    """Sustained drift across multiple heartbeat ticks → notify only once."""
    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "apply_comfort_restore", MagicMock())

    now = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)
    sm._check_tank_power_drift([], client, dev, now, trigger="hb1")
    sm._check_tank_power_drift([], client, dev, now + timedelta(minutes=2), trigger="hb2")
    sm._check_tank_power_drift([], client, dev, now + timedelta(minutes=4), trigger="hb3")

    assert notify_risk.call_count == 1


def test_drift_dedup_resets_when_tank_returns(monkeypatch) -> None:
    """After tank turns on (drift cleared), the next drift episode re-pings."""
    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "apply_comfort_restore", MagicMock())

    now = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)
    sm._check_tank_power_drift([], client, dev, now, trigger="hb1")
    assert notify_risk.call_count == 1

    # Recovery — tank back on, dedup token clears.
    dev.tank_on = True
    sm._check_tank_power_drift([], client, dev, now + timedelta(minutes=10), trigger="hb2")
    assert sm._TANK_DRIFT_NOTIFIED is False

    # Fresh drift episode — should ping again.
    dev.tank_on = False
    sm._check_tank_power_drift([], client, dev, now + timedelta(minutes=20), trigger="hb3")
    assert notify_risk.call_count == 2


def test_drift_respects_user_override(monkeypatch) -> None:
    """User turned the tank off via Onecta → respect the gesture; no alert."""
    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    notify_risk = MagicMock()
    apply_restore = MagicMock()
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "apply_comfort_restore", apply_restore)

    now = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)
    # Seed a user-overridden row from 30 min ago whose intent was tank_power=True
    aid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_drift_iso(now - timedelta(minutes=30)),
        end_time=_drift_iso(now + timedelta(minutes=30)),
        device="daikin", action_type="tank_idle_overnight",
        params={"tank_power": True, "tank_temp": 45}, status="active",
    )
    # Pin the override timestamp into the fake-now timeline so
    # find_recent_user_override's "within_hours" filter sees it as recent.
    db.mark_action_user_overridden(
        aid, overridden_at=(now - timedelta(minutes=30)).isoformat()
    )

    sm._check_tank_power_drift([], client, dev, now, trigger="hb")

    apply_restore.assert_not_called()
    notify_risk.assert_not_called()


def test_drift_alert_only_when_auto_recover_disabled(monkeypatch) -> None:
    monkeypatch.setattr(config, "TANK_DRIFT_AUTO_RECOVER", False, raising=False)
    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    apply_restore = MagicMock()
    notify_critical = MagicMock()
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "apply_comfort_restore", apply_restore)
    monkeypatch.setattr(sm, "notify_critical", notify_critical)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)

    sm._check_tank_power_drift([], client, dev, datetime(2026, 6, 1, 19, 0, tzinfo=UTC), trigger="hb")

    apply_restore.assert_not_called()
    notify_critical.assert_called_once()
    notify_risk.assert_not_called()


def test_drift_disabled_when_flag_false(monkeypatch) -> None:
    monkeypatch.setattr(config, "TANK_DRIFT_CHECK_ENABLED", False, raising=False)
    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    apply_restore = MagicMock()
    monkeypatch.setattr(sm, "apply_comfort_restore", apply_restore)

    sm._check_tank_power_drift([], client, dev, datetime(2026, 6, 1, 19, 0, tzinfo=UTC), trigger="hb")

    apply_restore.assert_not_called()


def test_drift_no_recover_in_read_only(monkeypatch) -> None:
    """OPENCLAW_READ_ONLY=true should alert but not write."""
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True, raising=False)
    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    apply_restore = MagicMock()
    notify_critical = MagicMock()
    monkeypatch.setattr(sm, "apply_comfort_restore", apply_restore)
    monkeypatch.setattr(sm, "notify_critical", notify_critical)
    monkeypatch.setattr(sm, "notify_risk", MagicMock())

    sm._check_tank_power_drift([], client, dev, datetime(2026, 6, 1, 19, 0, tzinfo=UTC), trigger="hb")

    apply_restore.assert_not_called()
    notify_critical.assert_called_once()


def test_drift_ignores_overridden_row_in_window(monkeypatch) -> None:
    """A planned ``tank_power=False`` slot that's been marked user-overridden
    no longer counts as the intent — the user reverted it, drift checking
    should treat it as no-planned-off."""
    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    notify_risk = MagicMock()
    apply_restore = MagicMock()
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "apply_comfort_restore", apply_restore)

    now = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
    actions = [{
        "id": 1,
        "start_time": _drift_iso(datetime(2026, 6, 1, 16, 30, tzinfo=UTC)),
        "end_time": _drift_iso(datetime(2026, 6, 1, 18, 0, tzinfo=UTC)),
        "status": "active",
        "action_type": "shutdown",
        "params": {"tank_power": False},
        "overridden_by_user_at": "2026-06-01T16:45:00Z",
    }]
    sm._check_tank_power_drift(actions, client, dev, now, trigger="hb")

    apply_restore.assert_called_once()
    notify_risk.assert_called_once()
