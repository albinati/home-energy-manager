"""#872 review round: partial exit, quiet window end, signal loss, passive exit."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from src import db
from src.config import config
from src.daikin import service as daikin_service
from src.daikin.models import DaikinDevice
from tests.test_daikin_holiday_872 import (  # noqa: F401 - autouse fixture + helpers
    NOW,
    _db,
    _insert_row,
    _mk_dev,
    _patch_runner,
    _rows,
)


def test_reconciler_holiday_gate_blocks_wire_even_when_cache_says_on() -> None:
    """is_on=True (stale cache): without the holiday gate this PATCHes the offset."""
    from src import state_machine as sm
    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=0.0, holiday_active=True)
    rid = _insert_row("lwt_preheat", {"lwt_offset": 3.0})
    client = MagicMock()
    sm._reconcile_daikin_actions(_rows(), client, dev, NOW, trigger="heartbeat")
    assert client.method_calls == []
    assert db.get_action_by_id(rid)["status"] == "pending"


def test_row_whose_window_ended_in_holiday_completes_quietly(caplog) -> None:
    from src import state_machine as sm
    dev = DaikinDevice(id="gw", name="x", is_on=True, holiday_active=True)
    rid = _insert_row("lwt_preheat", {"lwt_offset": 3.0},
                      start=NOW - timedelta(hours=3), end=NOW - timedelta(hours=2))
    client = MagicMock()
    with caplog.at_level("WARNING"):
        sm._reconcile_daikin_actions(_rows(), client, dev, NOW, trigger="heartbeat")
    row = db.get_action_by_id(rid)
    assert row["status"] == "completed" and row["error_msg"] == "holiday_mode"
    assert not [r for r in caplog.records if "window missed" in r.message]
    assert client.method_calls == []


def test_partial_exit_climate_only_then_tank(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": True}))
    dev = DaikinDevice(id="gw", name="x", is_on=False, tank_on=False,
                       holiday_active=False, tank_holiday_active=True)
    assert runner._holiday_transition_tick([dev]) == "exited"
    assert calls[0] == ("set_power", True)
    assert ("set_tank_power", True) not in calls
    assert calls[-1][0] == "mpc" and calls[-1][1]["force_write_devices"] is True
    assert json.loads(db.get_kv("daikin_holiday_active")) == {"climate": False, "tank": True}
    calls.clear()
    dev.tank_holiday_active = False
    assert runner._holiday_transition_tick([dev]) == "exited"
    assert calls[0] == ("set_tank_power", True) and calls[-1][0] == "mpc"
    assert ("set_power", True) not in calls


def test_both_points_flip_replans_once(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": True}))
    runner._holiday_transition_tick([_mk_dev(climate_on=False, tank_on=False)])
    assert [c[0] for c in calls].count("mpc") == 1


def test_exit_while_passive_no_power_on_but_replans(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": True}))
    assert runner._holiday_transition_tick([_mk_dev(climate_on=False, tank_on=False)]) == "exited"
    assert [c[0] for c in calls] == ["mpc"]


def test_signal_loss_writes_exited_and_clears_state(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": True}))
    assert runner._holiday_transition_tick([DaikinDevice(id="gw", name="x")]) == "exited"
    assert not db.get_kv("daikin_holiday_active")
    logs = db.get_action_logs(device="daikin", action="holiday_mode")
    assert logs[0]["params"]["event"] == "exited" and logs[0]["params"]["reason"] == "no_signal"
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": False}))
    monkeypatch.setattr(config, "DAIKIN_HOLIDAY_DETECT_ENABLED", False)
    assert runner._holiday_transition_tick([]) == "exited"
    assert not db.get_kv("daikin_holiday_active")
    assert calls == []


def test_holiday_state_survives_cache_error(monkeypatch) -> None:
    def boom(**kw):
        raise RuntimeError("cache exploded")
    monkeypatch.setattr(daikin_service, "get_cached_devices", boom)
    st = daikin_service.holiday_state()
    assert st["climate"] is None and st["tank"] is None


def _holiday_event(ts: datetime, event: str) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO action_log (timestamp, device, action, params, result, trigger) "
            "VALUES (?, 'daikin', 'holiday_mode', ?, 'ok', 'heartbeat')",
            (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), json.dumps({"event": event})),
        )
        conn.commit()
    finally:
        conn.close()


def test_scorecard_holiday_day_parsing_and_kill_switch(monkeypatch) -> None:
    from src.analytics import cosy_scorecard as cs
    d = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    nxt = d + timedelta(days=1)
    _holiday_event(datetime(2026, 10, 28, 9, 0, tzinfo=UTC), "entered")
    assert cs._holiday_day(d, nxt) is True  # carried forward, no exit yet
    _holiday_event(datetime(2026, 10, 30, 8, 0, tzinfo=UTC), "exited")
    assert cs._holiday_day(d, nxt) is True  # exit inside the day
    assert cs._holiday_day(nxt, nxt + timedelta(days=1)) is False  # day after the exit
    monkeypatch.setattr(config, "DAIKIN_HOLIDAY_DETECT_ENABLED", False)
    assert cs._holiday_day(d, nxt) is False
