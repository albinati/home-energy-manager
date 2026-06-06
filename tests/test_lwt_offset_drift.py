"""#461 — LWT-offset drift backstop: reset a stray non-zero offset to 0 when no
plan slot justifies it, after the user's grace window. Mirrors the tank-power
drift backstop."""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from src.config import config
from src.daikin.models import DaikinDevice


def _common(monkeypatch, *, enabled=True):
    import src.state_machine as sm
    monkeypatch.setattr("src.config.config.DAIKIN_LWT_PREHEAT_ENABLED", enabled)
    monkeypatch.setattr("src.config.config.LWT_OFFSET_DRIFT_CHECK_ENABLED", True)
    monkeypatch.setattr("src.config.config.LWT_OFFSET_DRIFT_AUTO_RECOVER", True)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.config.config.OPTIMIZATION_PRESET", "normal")
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.state_machine.notify_risk", lambda *a, **k: None)
    sm._LWT_DRIFT_NOTIFIED = False
    applied: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: applied.append(params) or True,
    )
    return sm, applied


def _slot(action_type, offset, *, status="active"):
    now = datetime.now(UTC)
    return {
        "action_type": action_type,
        "start_time": (now - timedelta(minutes=10)).isoformat(),
        "end_time": (now + timedelta(minutes=20)).isoformat(),
        "status": status,
        "params": json.dumps({"lwt_offset": offset, "lp_optimizer": True}),
    }


def _run(monkeypatch, sm, actions, dev):
    with tempfile.TemporaryDirectory() as td:
        from src import db
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        sm._check_lwt_offset_drift(actions, MagicMock(), dev, datetime.now(UTC), trigger="test")


def test_resets_stray_offset(monkeypatch):
    sm, applied = _common(monkeypatch)
    dev = DaikinDevice(id="gw", name="x", lwt_offset=3.0)
    _run(monkeypatch, sm, [], dev)  # no slot justifies the +3
    assert len(applied) == 1
    assert applied[0].get("lwt_offset") == 0


def test_justified_by_preheat_slot(monkeypatch):
    sm, applied = _common(monkeypatch)
    dev = DaikinDevice(id="gw", name="x", lwt_offset=3.0)
    _run(monkeypatch, sm, [_slot("lwt_preheat", 3)], dev)  # window wants +3
    assert applied == []


def test_zero_offset_is_noop(monkeypatch):
    sm, applied = _common(monkeypatch)
    dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)
    _run(monkeypatch, sm, [], dev)
    assert applied == []


def test_disabled_feature_hands_off(monkeypatch):
    # Pre-heat off → climate hands-off → never touch a non-zero offset.
    sm, applied = _common(monkeypatch, enabled=False)
    dev = DaikinDevice(id="gw", name="x", lwt_offset=4.0)
    _run(monkeypatch, sm, [], dev)
    assert applied == []


def test_respects_recent_user_override(monkeypatch):
    sm, applied = _common(monkeypatch)
    dev = DaikinDevice(id="gw", name="x", lwt_offset=3.0)
    # Override ROW holds what HEM scheduled (offset 0); the device still shows
    # the user's manual 3 → the gesture contradicts the schedule → in effect.
    monkeypatch.setattr(
        "src.state_machine.db.find_recent_user_override",
        lambda **kw: {"id": 7, "params": {"lwt_offset": 0}},
    )
    _run(monkeypatch, sm, [], dev)
    assert applied == []  # respect the grace window
