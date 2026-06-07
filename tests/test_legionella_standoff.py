"""Legionella thermal-shock STAND-OFF (2026-06-07).

The Onecta firmware owns the DHW tank during its weekly thermal-shock cycle, so
HEM must not write to the tank in that window (any PATCH is arbitrated away).
The reconciler skips TANK rows inside the window and leaves them pending;
LWT / space-heating rows are unaffected.
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import config
from src.daikin.models import DaikinDevice

# 2026-06-07 is a Sunday (weekday()==6) — the default stand-off DOW.
_SUN = datetime(2026, 6, 7, tzinfo=UTC)


def _cfg(monkeypatch, *, enabled=True, dow=6, hour=11, minute=0, dur=120):
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", enabled, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", dow, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", hour, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", minute, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", dur, raising=False)


# --- pure helpers -----------------------------------------------------------

def test_in_window_true(monkeypatch):
    from src.state_machine import in_legionella_standoff
    _cfg(monkeypatch)
    assert in_legionella_standoff(_SUN.replace(hour=11, minute=30)) is True
    assert in_legionella_standoff(_SUN.replace(hour=11, minute=0)) is True   # inclusive start
    assert in_legionella_standoff(_SUN.replace(hour=12, minute=59)) is True


def test_out_of_window(monkeypatch):
    from src.state_machine import in_legionella_standoff
    _cfg(monkeypatch)
    assert in_legionella_standoff(_SUN.replace(hour=10, minute=59)) is False  # before start
    assert in_legionella_standoff(_SUN.replace(hour=13, minute=0)) is False   # exclusive end
    assert in_legionella_standoff(_SUN.replace(hour=13, minute=1)) is False


def test_wrong_weekday(monkeypatch):
    from src.state_machine import in_legionella_standoff
    _cfg(monkeypatch)
    sat = _SUN - timedelta(days=1)  # Saturday, same time
    assert in_legionella_standoff(sat.replace(hour=11, minute=30)) is False


def test_disabled(monkeypatch):
    from src.state_machine import in_legionella_standoff
    _cfg(monkeypatch, enabled=False)
    assert in_legionella_standoff(_SUN.replace(hour=11, minute=30)) is False


def test_is_tank_action():
    from src.state_machine import _is_tank_action
    assert _is_tank_action({"tank_temp": 60, "tank_power": True}) is True
    assert _is_tank_action({"tank_powerful": True}) is True
    assert _is_tank_action({"lwt_offset": 10}) is False
    assert _is_tank_action({"climate_on": True}) is False
    assert _is_tank_action({}) is False


# --- reconcile integration --------------------------------------------------

def _seed(conn, *, action_type, params):
    """Insert an active daikin row whose window covers the Sunday 11:30 tick."""
    start = _SUN.replace(hour=4, minute=0).isoformat()
    end = _SUN.replace(hour=12, minute=0).isoformat()
    conn.execute(
        """INSERT INTO action_schedule
           (date, start_time, end_time, device, action_type, params, status, created_at)
           VALUES (?, ?, ?, 'daikin', ?, ?, 'pending', ?)""",
        (_SUN.date().isoformat(), start, end, action_type, json.dumps(params),
         _SUN.isoformat()),
    )
    rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return rid


def _reconcile_env(monkeypatch):
    import src.state_machine as sm
    _cfg(monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr("src.config.config.DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", False, raising=False)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False, raising=False)
    sm._LEGIONELLA_STANDOFF_LOGGED.clear()
    sm._FIRST_APPLIED_SESSION.clear()
    applied: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: applied.append(params) or True,
    )
    return sm, applied


def test_tank_row_skipped_in_window(monkeypatch):
    sm, applied = _reconcile_env(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        from src import db
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        conn = db.get_connection()
        try:
            rid = _seed(conn, action_type="tank_negative_boost",
                        params={"tank_temp": 60, "tank_power": True, "tank_powerful": True})
        finally:
            conn.close()
        # Live device diverges (45 ≠ 60) so WITHOUT the guard it would fire.
        dev = DaikinDevice(id="gw", name="x", tank_target=45.0, tank_on=True)
        now = _SUN.replace(hour=11, minute=30)
        rows = db.get_actions_for_plan_date(now.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, MagicMock(), dev, now, trigger="test")
        assert applied == [], "tank write must be suppressed during legionella"
        # Row stays pending (not completed) so it resumes after the window.
        row = db.get_action_by_id(rid)
        assert row["status"] == "pending"


def test_tank_row_fires_outside_window(monkeypatch):
    sm, applied = _reconcile_env(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        from src import db
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        conn = db.get_connection()
        try:
            _seed(conn, action_type="tank_negative_boost",
                  params={"tank_temp": 60, "tank_power": True, "tank_powerful": True})
        finally:
            conn.close()
        dev = DaikinDevice(id="gw", name="x", tank_target=45.0, tank_on=True)
        now = _SUN.replace(hour=9, minute=30)  # before the 11:00 window
        rows = db.get_actions_for_plan_date(now.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, MagicMock(), dev, now, trigger="test")
        assert len(applied) == 1 and applied[0].get("tank_temp") == 60


def test_lwt_row_still_fires_in_window(monkeypatch):
    sm, applied = _reconcile_env(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        from src import db
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        conn = db.get_connection()
        try:
            _seed(conn, action_type="lwt_preheat", params={"lwt_offset": 10})
        finally:
            conn.close()
        dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)  # diverged → would fire
        now = _SUN.replace(hour=11, minute=30)
        rows = db.get_actions_for_plan_date(now.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, MagicMock(), dev, now, trigger="test")
        assert len(applied) == 1 and applied[0].get("lwt_offset") == 10, \
            "LWT/space-heating must NOT be suppressed by the tank stand-off"
