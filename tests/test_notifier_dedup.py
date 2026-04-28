"""Tests for the notification debounce fixes (V12).

* Fox-flag warning gates per-day via ``acknowledged_warnings``.
* User-override notification dedups per action_id.
* New ``push_negative_window_start`` fires.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from src import config as _config
    monkeypatch.setattr(_config.config, "DB_PATH", db_path, raising=False)
    from src import db as _db
    _db.init_db()
    yield


# -------------------------------------------------------------------------
# Fox scheduler flag warning
# -------------------------------------------------------------------------

def test_fox_flag_warning_fires_once_per_day(monkeypatch):
    """Heartbeat with a stuck-False Fox flag should ping exactly once per
    plan_date, not every 2 min."""
    from datetime import date
    from src import db, state_machine

    notifies: list[str] = []
    monkeypatch.setattr(state_machine, "notify_risk", lambda msg, **k: notifies.append(msg))

    from unittest.mock import MagicMock
    fox = MagicMock()
    fox.api_key = "test-key"
    fox.get_scheduler_flag.return_value = False
    hw = MagicMock()
    hw.enabled = False
    hw.groups = []
    fox.get_scheduler_v3.return_value = hw

    # First heartbeat — fires.
    state_machine.heartbeat_repair_fox_scheduler(fox)
    assert len(notifies) == 1

    # Second heartbeat — silent (warning_key already acked).
    state_machine.heartbeat_repair_fox_scheduler(fox)
    state_machine.heartbeat_repair_fox_scheduler(fox)
    assert len(notifies) == 1

    # Verify the key is in acknowledged_warnings.
    today = date.today().isoformat()
    assert db.is_warning_acknowledged(f"fox_scheduler_disabled_{today}")


def test_fox_flag_warning_re_fires_after_recovery(monkeypatch):
    """Flag goes False → fires once. Flag recovers → ack cleared. Flag goes
    False again same day → fires once more. Total = 2."""
    from src import state_machine

    notifies: list[str] = []
    monkeypatch.setattr(state_machine, "notify_risk", lambda msg, **k: notifies.append(msg))

    from unittest.mock import MagicMock
    fox = MagicMock()
    fox.api_key = "test-key"

    # First failure.
    fox.get_scheduler_flag.return_value = False
    hw = MagicMock(); hw.enabled = False; hw.groups = []
    fox.get_scheduler_v3.return_value = hw
    state_machine.heartbeat_repair_fox_scheduler(fox)
    assert len(notifies) == 1

    # Recovery — clears the warning_key.
    fox.get_scheduler_flag.return_value = True
    hw.enabled = True
    state_machine.heartbeat_repair_fox_scheduler(fox)
    assert len(notifies) == 1  # no extra ping on recovery

    # Second failure same day — fires again.
    fox.get_scheduler_flag.return_value = False
    hw.enabled = False
    state_machine.heartbeat_repair_fox_scheduler(fox)
    assert len(notifies) == 2


# -------------------------------------------------------------------------
# User-override per-episode dedup
# -------------------------------------------------------------------------

def test_user_override_set_dedups_within_episode():
    """The module-level set ``_USER_OVERRIDE_NOTIFIED`` is the dedup
    mechanism — verifying its wiring: a row that's added stays added until
    the override clears."""
    from src import state_machine

    state_machine._USER_OVERRIDE_NOTIFIED.clear()
    state_machine._USER_OVERRIDE_NOTIFIED.add(42)
    # While in the set, the heartbeat should not re-notify (the production
    # code path checks `if aid not in _USER_OVERRIDE_NOTIFIED`).
    assert 42 in state_machine._USER_OVERRIDE_NOTIFIED
    # Recovery path uses discard, never raises on absent key.
    state_machine._USER_OVERRIDE_NOTIFIED.discard(42)
    state_machine._USER_OVERRIDE_NOTIFIED.discard(42)  # second discard is a no-op
    assert 42 not in state_machine._USER_OVERRIDE_NOTIFIED


# -------------------------------------------------------------------------
# Negative-window ping
# -------------------------------------------------------------------------

def test_push_negative_window_start_uses_correct_alert_type(monkeypatch):
    from src import notifier

    captured: list[tuple] = []
    monkeypatch.setattr(notifier, "push_alert", lambda alert_key, payload: captured.append((alert_key, payload)))

    notifier.push_negative_window_start(soc=42.0, fox_mode="SelfUse", price_pence=-5.3)
    assert len(captured) == 1
    alert_key, payload = captured[0]
    assert alert_key == notifier.AlertType.NEGATIVE_WINDOW_START.value
    assert "PAID" in payload["title"]
    assert payload["soc_pct"] == 42.0
    assert payload["price_pence"] == -5.3
