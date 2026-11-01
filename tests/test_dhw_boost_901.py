"""#901 - "Banho extra hoje": the plan must RESPECT a one-tap tank boost.

Reconciler-level ("test the wire, not the planner"): the boost is applied through
``dhw_boost.apply`` and then the REAL ``_reconcile_daikin_actions`` runs over the
planned rows. We assert on what reaches ``apply_scheduled_daikin_params`` (the
only function that PATCHes the unit for plan rows), not on what a planner
emitted.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db, dhw_boost
from src import state_machine as sm
from src.daikin.models import DaikinDevice
from src.daikin_bulletproof import TANK_WRITE_LOCK


def _now() -> datetime:
    # 08:00 UTC: date-stable for the +4 h hop (same flake class as #383).
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


def _add(**kw) -> int:
    conn = db.get_connection()
    try:
        return _row(conn, **kw)
    finally:
        conn.close()


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
    powerful_writes: list[bool] = []
    monkeypatch.setattr(
        "src.dhw_boost.daikin_service.set_tank_temperature",
        lambda t, actor="api": writes.append(float(t)),
    )
    monkeypatch.setattr(
        "src.dhw_boost.daikin_service.set_tank_powerful",
        lambda on, actor="api": powerful_writes.append(bool(on)),
    )
    dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=47.0,
                       tank_temperature=44.0, lwt_offset=0.0)
    monkeypatch.setattr(dhw_boost, "_cached_device", lambda: dev)

    def _predict(target_c=None, powerful=None):  # the service helper, on this test's device
        if target_c is not None:
            dev.tank_target = float(target_c)
        if powerful is not None:
            dev.tank_powerful = bool(powerful)

    monkeypatch.setattr("src.dhw_boost.daikin_service.note_predicted_tank_state", _predict)
    monkeypatch.setattr(sm, "_device_read_wall", lambda: None)

    applied: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda d, c, params, trigger, skip_if_matches=True: applied.append(dict(params)) or True,
    )
    monkeypatch.setattr("src.state_machine.notify_user_override", lambda msg: None)
    monkeypatch.setattr("src.dhw_boost.DaikinClient", MagicMock)
    return {"dev": dev, "writes": writes, "powerful": powerful_writes, "applied": applied}


def _reconcile(now: datetime, dev) -> None:
    rows = db.get_actions_for_plan_date(now.date().isoformat(), device="daikin")
    sm._reconcile_daikin_actions(rows, MagicMock(), dev, now, trigger="test")


def _seed_plan(now: datetime) -> tuple[int, int]:
    """A warmup that covers now (47 C) and the 16:00-style setback (37 C) that
    would start inside the respect window."""
    warm = _add(atype="tank_warmup", start=now - timedelta(hours=1), end=now + timedelta(hours=1),
                params={"tank_power": True, "tank_temp": 47}, status="completed")
    setback = _add(atype="tank_setback", start=now + timedelta(hours=1), end=now + timedelta(hours=9),
                   params={"tank_power": True, "tank_temp": 37})
    return warm, setback


# ── the headline wire ────────────────────────────────────────────────────────

def test_boost_holds_the_planned_setback_then_resumes(env):
    now = _now()
    _, setback = _seed_plan(now)

    out = dhw_boost.apply(now)
    assert env["writes"] == [50.0]
    assert out["until_utc"].startswith((now + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"))
    boost = db.get_action_by_id(out["row_id"])
    assert boost["status"] == "active" and boost["overridden_by_user_at"]

    # The setback becomes due INSIDE the respect window: must NOT be written.
    _reconcile(now + timedelta(hours=1, minutes=1), env["dev"])
    assert env["applied"] == []
    sb = db.get_action_by_id(setback)
    assert sb["status"] in ("pending", "active") and not sb["overridden_by_user_at"]
    assert env["dev"].tank_target == 50.0

    # Window over (4 h): the hold is released and the plan's 37 C is written.
    _reconcile(now + timedelta(hours=4, minutes=1), env["dev"])
    assert {"tank_power": True, "tank_temp": 37} in env["applied"]
    done = db.get_action_by_id(out["row_id"])
    assert done["status"] == "completed" and done["overridden_by_user_at"] is None
    assert db.get_active_user_boost_rows() == []


# ── (2) the hold must not depend on a fresh device read ──────────────────────

def test_hold_does_not_depend_on_a_fresh_device_read(env):
    now = _now()
    out = dhw_boost.apply(now)
    env["dev"].tank_target = 37.0  # stale cache: the cloud still echoes the pre-boost target
    sb = _add(atype="tank_setback", start=now + timedelta(minutes=10), end=now + timedelta(hours=2),
              params={"tank_power": True, "tank_temp": 30})
    _reconcile(now + timedelta(minutes=11), env["dev"])
    assert env["applied"] == []
    assert db.get_action_by_id(sb)["status"] in ("pending", "active")
    assert db.get_action_by_id(out["row_id"])["status"] == "active"


def test_a_post_write_read_showing_another_target_releases_the_hold(env, monkeypatch):
    now = _now()
    dhw_boost.apply(now)
    env["dev"].tank_target = 41.0  # the user moved it by hand AFTER the boost
    monkeypatch.setattr(sm, "_device_read_wall", lambda: (now + timedelta(minutes=30)).timestamp())
    _add(atype="tank_setback", start=now + timedelta(minutes=10), end=now + timedelta(hours=2),
         params={"tank_power": True, "tank_temp": 37})
    _reconcile(now + timedelta(minutes=31), env["dev"])
    assert {"tank_power": True, "tank_temp": 37} in env["applied"]


def test_predicted_state_goes_through_the_service_helper(env):
    """apply() mirrors the write onto the CURRENT cache objects (the helper),
    not onto the pre-write reference."""
    seen = []
    import src.dhw_boost as mod
    mod.daikin_service.note_predicted_tank_state = lambda t=None, p=None: seen.append((t, p))
    dhw_boost.apply(_now())
    assert seen == [(50.0, True)]


# ── (3) paid negative-price boosts and Powerful rows still fire ──────────────

def test_negative_price_boost_rows_still_fire_during_a_user_boost(env):
    now = _now()
    dhw_boost.apply(now)
    _add(atype="tank_negative_boost", start=now + timedelta(minutes=5), end=now + timedelta(hours=1),
         params={"tank_power": True, "tank_temp": 60, "tank_powerful": True})
    _add(atype="tank_warmup", start=now + timedelta(minutes=5), end=now + timedelta(hours=1),
         params={"tank_power": True, "tank_temp": 50})
    _reconcile(now + timedelta(minutes=10), env["dev"])
    assert {"tank_power": True, "tank_temp": 60, "tank_powerful": True} in env["applied"]


def test_negative_boost_powerful_backstop_is_not_blocked_by_a_user_boost(env):
    now = _now()
    dhw_boost.apply(now)
    row = {
        "id": 99, "status": "completed", "action_type": "tank_negative_boost",
        "start_time": (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "end_time": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "params": {"tank_power": True, "tank_temp": 60, "tank_powerful": True},
    }
    sm._NEG_BOOST_POWERFUL_LAST_UTC = None
    sm._NEG_BOOST_STATE_LOADED = True
    client = MagicMock()
    env["dev"].tank_temperature = 51.0
    sm._check_negative_boost_powerful([row], client, env["dev"], now + timedelta(minutes=1), trigger="t")
    client.set_tank_powerful.assert_called_once()


# ── (14) tank restore rows ───────────────────────────────────────────────────

def test_tank_restore_rows_are_held_too(env):
    now = _now()
    dhw_boost.apply(now)
    rid = _add(atype="restore", start=now + timedelta(minutes=5), end=now + timedelta(hours=1),
               params={"tank_power": True, "tank_temp": 37})
    _reconcile(now + timedelta(minutes=10), env["dev"])
    assert env["applied"] == []
    assert db.get_action_by_id(rid)["status"] in ("pending", "active")


# ── (13) device-wide override side effects ───────────────────────────────────

def test_lwt_rows_are_not_held_by_a_tank_boost(env):
    now = _now()
    dhw_boost.apply(now)
    _add(atype="lwt_preheat", start=now + timedelta(minutes=5), end=now + timedelta(hours=1),
         params={"lwt_offset": 3})
    _reconcile(now + timedelta(minutes=10), env["dev"])
    assert {"lwt_offset": 3} in env["applied"]


def test_non_tank_override_lookup_can_look_past_the_boost(env):
    now = _now()
    dhw_boost.apply(now)
    t = now + timedelta(minutes=1)
    assert db.find_recent_user_override(
        "daikin", within_hours=4.0, now_utc=t, exclude_action_types=(db.USER_BOOST_ACTION_TYPE,)) is None
    assert db.find_recent_user_override("daikin", within_hours=4.0, now_utc=t) is not None


def test_tank_power_off_backstop_is_not_hidden_by_a_tank_boost(env, monkeypatch):
    """#382: a tank that is OFF with no plan slot asking for it must still be
    flagged while a boost row exists (the boost is about the TARGET, not power)."""
    monkeypatch.setattr("src.config.config.TANK_DRIFT_CHECK_ENABLED", True)
    monkeypatch.setattr("src.config.config.TANK_DRIFT_AUTO_RECOVER", False)
    now = _now()
    dhw_boost.apply(now)
    env["dev"].tank_on = False
    sm._TANK_DRIFT_NOTIFIED = False
    monkeypatch.setattr("src.notifier.notify_risk", lambda *a, **k: None, raising=False)
    sm._check_tank_power_drift([], MagicMock(), env["dev"], now + timedelta(minutes=5), trigger="t")
    assert any(a["action"] == "tank_drift_detected"
               for a in db.get_action_logs(device="daikin", limit=20))


# ── Powerful and the target ─────────────────────────────────────────────────

def test_powerful_only_inside_the_deadband(env):
    env["dev"].tank_temperature = 44.0   # gap 6 <= differential + margin
    dhw_boost.apply(_now())
    assert env["powerful"] == [True] and env["writes"] == [50.0]


def test_plain_target_when_the_gap_is_beyond_the_deadband(env):
    env["dev"].tank_temperature = 38.0   # gap 12: the heat pump starts on its own
    env["dev"].tank_target = 40.0
    dhw_boost.apply(_now())
    assert env["powerful"] == [] and env["writes"] == [50.0]


def test_target_is_capped_at_the_heat_pump_cliff(env, monkeypatch):
    monkeypatch.setattr("src.config.config.DHW_USER_BOOST_TARGET_C", 58.0)
    assert dhw_boost.target_c() == 50.0


# ── (4) bias decontamination ─────────────────────────────────────────────────

def test_bias_windows_include_finished_user_boosts(env):
    now = _now()
    out = dhw_boost.apply(now)
    dhw_boost.undo(now + timedelta(minutes=5))
    assert db.get_action_by_id(out["row_id"])["status"] == "completed"
    wins = db.get_dhw_boost_windows((now - timedelta(days=1)).date().isoformat(),
                                    (now + timedelta(days=1)).date().isoformat())
    assert any(w[0].startswith(now.strftime("%Y-%m-%dT%H:%M")) for w in wins)


# ── (5) a restore that cannot be written is not "done" ───────────────────────

def test_restore_waits_for_the_gate_and_retries_until_it_can_write(env, monkeypatch):
    now = _now()
    _add(atype="tank_setback", start=now - timedelta(minutes=1), end=now + timedelta(hours=9),
         params={"tank_power": True, "tank_temp": 37}, status="completed")
    out = dhw_boost.apply(now)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "passive")
    t = now + timedelta(hours=4, minutes=1)
    _reconcile(t, env["dev"])
    row = db.get_action_by_id(out["row_id"])
    assert row["status"] == "active" and row["overridden_by_user_at"] is None  # released, restore pending
    assert env["applied"] == []
    _reconcile(t + timedelta(minutes=2), env["dev"])
    assert db.get_action_by_id(out["row_id"])["status"] == "active"
    logs = [a for a in db.get_action_logs(device="daikin", limit=50) if a["action"] == "dhw_user_boost_end"]
    assert logs and all(entry["result"] != "success" for entry in logs)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "active")
    _reconcile(t + timedelta(minutes=4), env["dev"])
    assert any(p.get("tank_temp") == 37 for p in env["applied"])
    assert db.get_action_by_id(out["row_id"])["status"] == "completed"


def test_holiday_skip_at_expiry_is_not_success(env):
    now = _now()
    out = dhw_boost.apply(now)
    env["dev"].tank_holiday_active = True
    _reconcile(now + timedelta(hours=4, minutes=1), env["dev"])
    assert db.get_action_by_id(out["row_id"])["status"] == "active"
    logs = [a for a in db.get_action_logs(device="daikin", limit=50) if a["action"] == "dhw_user_boost_end"]
    assert logs and logs[0]["result"] == "skipped"


def test_undo_hands_the_tank_back_and_closes_the_row(env):
    now = _now()
    _add(atype="tank_setback", start=now - timedelta(minutes=1), end=now + timedelta(hours=8),
         params={"tank_power": True, "tank_temp": 37}, status="completed")
    out = dhw_boost.apply(now)
    res = dhw_boost.undo(now + timedelta(minutes=10))
    assert res["restored"] is True and res["target_c"] == 37.0
    assert {"tank_power": True, "tank_temp": 37.0, "tank_powerful": False} in env["applied"]
    done = db.get_action_by_id(out["row_id"])
    assert done["status"] == "completed" and done["overridden_by_user_at"] is None
    assert dhw_boost.active_boost(now + timedelta(minutes=11)) is None


def test_undo_that_cannot_write_returns_a_distinct_409(env, monkeypatch):
    now = _now()
    dhw_boost.apply(now)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "passive")
    with pytest.raises(dhw_boost.BoostBlocked) as ei:
        dhw_boost.undo(now + timedelta(minutes=5))
    assert ei.value.status == 409 and ei.value.code == "PassiveModeLocked"
    assert dhw_boost.active_boost(now + timedelta(minutes=6)) is not None  # still holding


# ── (6) atomicity ────────────────────────────────────────────────────────────

def test_one_active_boost_is_enforced_by_the_database(env):
    now = _now()
    dhw_boost.apply(now)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_user_boost_row(
            plan_date=now.date().isoformat(), start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-01T04:00:00Z", params={}, overridden_at=now.isoformat())


def test_boost_row_is_born_flagged(env):
    """One INSERT: there is no instant where the row is active but unflagged."""
    now = _now()
    rid = db.insert_user_boost_row(
        plan_date=now.date().isoformat(), start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T04:00:00Z", params={}, overridden_at=now.isoformat())
    row = db.get_action_by_id(rid)
    assert row["status"] == "active" and row["overridden_by_user_at"]


def test_boost_and_reconcile_share_one_tank_lock(env):
    got: list[bool] = []

    def other():
        got.append(TANK_WRITE_LOCK.acquire(timeout=0.2))

    with TANK_WRITE_LOCK:
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert got == [False]
    import inspect
    assert "TANK_WRITE_LOCK" in inspect.getsource(sm.reconcile_daikin_schedule_for_date)
    assert "TANK_WRITE_LOCK" in inspect.getsource(dhw_boost.apply)
    assert "TANK_WRITE_LOCK" in inspect.getsource(dhw_boost.undo)


# ── gates ───────────────────────────────────────────────────────────────────

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
        env["dev"].tank_temperature = 38.0  # beyond the deadband: nothing to add
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
    assert r.json()["uses_powerful"] is True and r.json()["target_c"] == 50.0
    sim = c.post("/api/v1/dhw/boost/simulate")
    assert sim.status_code == 200 and sim.json()["action"] == "dhw.boost"
    assert "Powerful" in sim.json()["human_summary"]
    ok = c.post("/api/v1/dhw/boost", headers={"X-Simulation-Id": sim.json()["simulation_id"]})
    assert ok.status_code == 200 and env["writes"] == [50.0]
    assert any(a["action"] == "dhw_user_boost" for a in db.get_action_logs(device="daikin", limit=20))
    st = c.get("/api/v1/dhw/boost").json()
    assert st["active"] is True and st["available"] is False
    # Rate limit: an immediate second apply is refused by the safeguard.
    again = c.post("/api/v1/dhw/boost")
    assert again.status_code == 429
    undo_sim = c.post("/api/v1/dhw/boost/undo/simulate")
    assert undo_sim.status_code == 200
    monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", True)
    blocked = c.post("/api/v1/dhw/boost/undo/simulate")
    assert blocked.status_code == 409 and blocked.json()["detail"]["error"] == "ReadOnly"
