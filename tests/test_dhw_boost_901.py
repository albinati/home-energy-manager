"""#901 - "Banho extra hoje": the plan must RESPECT a one-tap tank boost.

Reconciler-level ("test the wire, not the planner"): the boost is applied through
``dhw_boost.apply`` and then the REAL ``_reconcile_daikin_actions`` runs over the
planned rows. We assert on what reaches ``apply_scheduled_daikin_params`` (the
only function that PATCHes the unit for plan rows), not on what a planner
emitted:

* inside the respect window the planned 16:00 setback (37 C) is NOT written;
* once the window ends the tank is handed back to the plan;
* read-only / passive / holiday / vacation / hot tank each refuse the write.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db, dhw_boost
from src import state_machine as sm
from src.daikin.models import DaikinDevice


def _now() -> datetime:
    # Midday UTC: date-stable for the +4 h hop (same flake class as #383).
    return datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0)


def _row(conn, *, atype, start, end, params, status="pending") -> int:
    conn.execute(
        """INSERT INTO action_schedule
           (date, start_time, end_time, device, action_type, params, status, created_at)
           VALUES (?, ?, ?, 'daikin', ?, ?, ?, ?)""",
        (start.date().isoformat(), start.isoformat(), end.isoformat(), atype,
         json.dumps(params), status, start.isoformat()),
    )
    rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return rid


@pytest.fixture
def env(monkeypatch):
    db.init_db()
    cfg = "src.config.config."
    monkeypatch.setattr(cfg + "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(cfg + "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(cfg + "OPTIMIZATION_PRESET", "normal")
    monkeypatch.setattr(cfg + "DHW_LEGIONELLA_STANDOFF_ENABLED", False)
    monkeypatch.setattr(cfg + "DAIKIN_HOLIDAY_DETECT_ENABLED", True)
    monkeypatch.setattr(cfg + "PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setattr(cfg + "USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr(cfg + "USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END", True)
    monkeypatch.setattr(cfg + "DAIKIN_LWT_PREHEAT_ENABLED", True)
    monkeypatch.setattr(cfg + "DHW_USER_BOOST_TARGET_C", 50.0)
    monkeypatch.setattr(cfg + "DHW_TEMP_MAX_C", 60.0)
    monkeypatch.setattr("src.api_quota.should_block", lambda vendor: False)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._BOOST_HOLD_LOGGED.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()

    writes: list[float] = []
    monkeypatch.setattr(
        "src.dhw_boost.daikin_service.set_tank_temperature",
        lambda t, actor="api": writes.append(float(t)),
    )
    dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=47.0, tank_temperature=44.0, lwt_offset=0.0)
    monkeypatch.setattr(dhw_boost, "_cached_device", lambda: dev)

    applied: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda d, c, params, trigger, skip_if_matches=True: applied.append(dict(params)) or True,
    )
    monkeypatch.setattr("src.state_machine.notify_user_override", lambda msg: None)
    return {"dev": dev, "writes": writes, "applied": applied}


def _reconcile(now: datetime, dev) -> None:
    rows = db.get_actions_for_plan_date(now.date().isoformat(), device="daikin")
    sm._reconcile_daikin_actions(rows, MagicMock(), dev, now, trigger="test")


def _seed_plan(now: datetime) -> tuple[int, int]:
    """A warmup that covers now (47 C) and the 16:00-style setback (37 C) that
    would start inside the respect window."""
    conn = db.get_connection()
    try:
        warm = _row(conn, atype="tank_warmup", start=now - timedelta(hours=1),
                    end=now + timedelta(hours=1),
                    params={"tank_power": True, "tank_temp": 47}, status="completed")
        setback = _row(conn, atype="tank_setback", start=now + timedelta(hours=1),
                       end=now + timedelta(hours=9),
                       params={"tank_power": True, "tank_temp": 37})
    finally:
        conn.close()
    return warm, setback


def test_boost_holds_the_planned_setback_then_resumes(env):
    now = _now()
    warm, setback = _seed_plan(now)

    out = dhw_boost.apply(now)
    assert env["writes"] == [50.0]
    assert out["until_utc"].startswith((now + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"))
    boost = db.get_action_by_id(out["row_id"])
    assert boost["status"] == "active" and boost["overridden_by_user_at"]

    # The setback becomes due INSIDE the respect window: must NOT be written.
    t = now + timedelta(hours=1, minutes=1)
    _reconcile(t, env["dev"])
    assert env["applied"] == []
    sb = db.get_action_by_id(setback)
    # Held, not consumed: still fireable once the boost ends (no chained override).
    assert sb["status"] in ("pending", "active") and not sb["overridden_by_user_at"]
    assert env["dev"].tank_target == 50.0

    # Window over (4 h): boost closes and the plan's 37 C is written again.
    t2 = now + timedelta(hours=4, minutes=1)
    _reconcile(t2, env["dev"])
    assert {"tank_power": True, "tank_temp": 37} in env["applied"]
    done = db.get_action_by_id(out["row_id"])
    assert done["status"] == "completed" and done["overridden_by_user_at"] is None
    assert db.get_active_user_boost_rows() == []


def test_lwt_rows_are_not_held_by_a_tank_boost(env):
    now = _now()
    dhw_boost.apply(now)
    conn = db.get_connection()
    try:
        _row(conn, atype="lwt_preheat", start=now + timedelta(minutes=5),
             end=now + timedelta(hours=1), params={"lwt_offset": 3})
    finally:
        conn.close()
    _reconcile(now + timedelta(minutes=10), env["dev"])
    assert {"lwt_offset": 3} in env["applied"]


def test_stale_cloud_snapshot_right_after_the_write_does_not_let_the_plan_overwrite(env):
    now = _now()
    _, setback = _seed_plan(now)
    dhw_boost.apply(now)
    env["dev"].tank_target = 37.0  # cloud still echoing the pre-boost target
    # A planned row due 2 min after the click (inside the fresh-write grace).
    conn = db.get_connection()
    try:
        _row(conn, atype="tank_setback", start=now + timedelta(minutes=1),
             end=now + timedelta(hours=2), params={"tank_power": True, "tank_temp": 30})
    finally:
        conn.close()
    _reconcile(now + timedelta(minutes=2), env["dev"])
    assert env["applied"] == []


def test_undo_hands_the_tank_back_and_the_plan_fires(env, monkeypatch):
    now = _now()
    conn = db.get_connection()
    try:
        _row(conn, atype="tank_setback", start=now - timedelta(minutes=1),
             end=now + timedelta(hours=8), params={"tank_power": True, "tank_temp": 37},
             status="completed")
    finally:
        conn.close()
    out = dhw_boost.apply(now)
    monkeypatch.setattr("src.dhw_boost.DaikinClient", MagicMock)
    res = dhw_boost.undo(now + timedelta(minutes=10))
    assert res["restored"] is True and res["target_c"] == 37.0
    assert {"tank_power": True, "tank_temp": 37.0, "tank_powerful": False} in env["applied"]
    done = db.get_action_by_id(out["row_id"])
    assert done["status"] == "completed" and done["overridden_by_user_at"] is None
    assert dhw_boost.active_boost(now + timedelta(minutes=11)) is None


def test_expiry_under_passive_closes_the_row_without_writing(env, monkeypatch):
    now = _now()
    out = dhw_boost.apply(now)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "passive")
    _reconcile(now + timedelta(hours=4, minutes=1), env["dev"])
    assert env["applied"] == []
    assert db.get_action_by_id(out["row_id"])["status"] == "completed"


@pytest.mark.parametrize("gate,code,status", [
    ("read_only", "ReadOnly", 409),
    ("passive", "PassiveModeLocked", 409),
    ("holiday", "HolidayMode", 409),
    ("vacation", "VacationMode", 409),
    ("tank_hot", "TankAlreadyHot", 422),
    ("target_high", "TargetAlreadyHigh", 422),
    ("tank_off", "TankOff", 422),
    ("quota", "QuotaExhausted", 409),
])
def test_gates_block_the_write(env, monkeypatch, gate, code, status):
    if gate == "read_only":
        monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", True)
    elif gate == "passive":
        monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "passive")
    elif gate == "holiday":
        env["dev"].tank_holiday_active = True
    elif gate == "vacation":
        monkeypatch.setattr("src.config.config.OPTIMIZATION_PRESET", "vacation")
    elif gate == "tank_hot":
        env["dev"].tank_temperature = 51.0
    elif gate == "target_high":
        env["dev"].tank_target = 50.0
    elif gate == "tank_off":
        env["dev"].tank_on = False
    elif gate == "quota":
        monkeypatch.setattr("src.api_quota.should_block", lambda vendor: True)
    with pytest.raises(dhw_boost.BoostBlocked) as ei:
        dhw_boost.apply(_now())
    assert ei.value.code == code and ei.value.status == status
    assert env["writes"] == []
    assert db.get_active_user_boost_rows() == []
    with pytest.raises(dhw_boost.BoostBlocked):
        dhw_boost.simulate(_now())


def test_second_boost_is_refused(env):
    now = _now()
    dhw_boost.apply(now)
    env["dev"].tank_target = 47.0
    with pytest.raises(dhw_boost.BoostBlocked) as ei:
        dhw_boost.apply(now + timedelta(minutes=1))
    assert ei.value.code == "AlreadyBoosted"


def test_failed_write_leaves_no_override_behind(env, monkeypatch):
    from src.daikin.client import DaikinError

    def boom(t, actor="api"):
        raise DaikinError("cloud down")

    monkeypatch.setattr("src.dhw_boost.daikin_service.set_tank_temperature", boom)
    with pytest.raises(DaikinError):
        dhw_boost.apply(_now())
    assert db.get_active_user_boost_rows() == []
    assert db.find_recent_user_override("daikin", within_hours=4.0, now_utc=_now()) is None


def test_target_is_clamped_to_the_tank_ceiling(env, monkeypatch):
    monkeypatch.setattr("src.config.config.DHW_USER_BOOST_TARGET_C", 75.0)
    assert dhw_boost.target_c() == 60.0


def test_boot_recovery_does_not_overwrite_a_running_boost(env):
    now = _now()
    _seed_plan(now)
    dhw_boost.apply(now)
    planned, source, _ = sm._scheduled_tank_state(now + timedelta(minutes=5))
    assert planned is None and source == "user_override"


def test_http_surface_is_gated_and_logged(env, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.main import app

    monkeypatch.setenv("REQUIRE_SIMULATION_ID", "false")
    c = TestClient(app)
    r = c.get("/api/v1/dhw/boost")
    assert r.status_code == 200 and r.json()["available"] is True
    sim = c.post("/api/v1/dhw/boost/simulate")
    assert sim.status_code == 200 and sim.json()["action"] == "dhw.boost"
    ok = c.post("/api/v1/dhw/boost", headers={"X-Simulation-Id": sim.json()["simulation_id"]})
    assert ok.status_code == 200 and env["writes"] == [50.0]
    assert any(a["action"] == "dhw_user_boost" for a in db.get_action_logs(device="daikin", limit=20))
    st = c.get("/api/v1/dhw/boost").json()
    assert st["active"] is True and st["available"] is False
    monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", True)
    blocked = c.post("/api/v1/dhw/boost/simulate")
    assert blocked.status_code == 409
    # Reason is a distinct code, not a generic error.
    monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", False)
    assert c.post("/api/v1/dhw/boost/simulate").json()["detail"]["error"] == "AlreadyBoosted"
