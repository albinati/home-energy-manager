"""#901 round 2: terminating restore, hold margins, pending gate, divergence, Powerful."""
from __future__ import annotations

import inspect
from datetime import timedelta

import pytest

from src import db, dhw_boost
from src import state_machine as sm
from tests.test_dhw_boost_901 import _add, _now, _reconcile, env  # noqa: F401  (env is a fixture)


def _released_boost(env, monkeypatch):
    now = _now()
    _add(atype="tank_setback", start=now - timedelta(minutes=1), end=now + timedelta(hours=20),
         params={"tank_power": True, "tank_temp": 37}, status="completed")
    out = dhw_boost.apply(now)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "passive")
    _reconcile(now + timedelta(hours=4, minutes=1), env["dev"])  # released, restore pending
    return now, out


def test_restore_gives_up_after_12h_closes_the_row_and_notifies_once(env, monkeypatch):
    now, out = _released_boost(env, monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr("src.state_machine.notify_risk", lambda m, extra=None: sent.append(m))
    released_end = db.get_action_by_id(out["row_id"])["end_time"]
    _reconcile(now + timedelta(hours=4, minutes=30), env["dev"])
    assert db.get_action_by_id(out["row_id"])["end_time"] == released_end  # clock never moves
    _reconcile(now + timedelta(hours=17), env["dev"])
    assert db.get_action_by_id(out["row_id"])["status"] == "completed"
    _reconcile(now + timedelta(hours=18), env["dev"])
    assert len(sent) == 1 and "shower boost" in sent[0] and "tank" in sent[0]


def test_restore_attempts_are_bounded_with_backoff(env, monkeypatch):
    now, out = _released_boost(env, monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "active")

    def _boom(*a, **k):
        raise sm.DaikinError("write failed")

    monkeypatch.setattr("src.state_machine.apply_scheduled_daikin_params", _boom)
    sent: list[str] = []
    monkeypatch.setattr("src.state_machine.notify_risk", lambda m, extra=None: sent.append(m))
    t0 = now + timedelta(hours=4, minutes=1)
    for minute in range(1, 300):  # 5 h of 1-minute ticks, still passive
        _reconcile(t0 + timedelta(minutes=minute), env["dev"])
    row = db.get_action_by_id(out["row_id"])
    assert row["status"] == "completed"           # gave up after 6 attempts
    assert row["params"]["restore_attempts"] == 6
    assert len(sent) == 1


def test_no_restore_write_while_the_quota_blocks(env, monkeypatch):
    now, out = _released_boost(env, monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.api_quota.should_block", lambda vendor: True)
    for m in (3, 10, 30, 70):
        _reconcile(now + timedelta(hours=4, minutes=1 + m), env["dev"])
    assert env["applied"] == []
    assert db.get_action_by_id(out["row_id"])["status"] in ("active", "completed")


def test_no_same_value_powerful_off_when_hem_never_turned_it_on(env):
    env["dev"].tank_temperature = 38.0   # beyond the deadband: plain target, no Powerful
    env["dev"].tank_target = 40.0
    now = _now()
    _add(atype="tank_setback", start=now - timedelta(minutes=1), end=now + timedelta(hours=8),
         params={"tank_power": True, "tank_temp": 37, "tank_powerful": False}, status="completed")
    dhw_boost.apply(now)
    dhw_boost.undo(now + timedelta(minutes=5))
    assert env["applied"] and all("tank_powerful" not in p for p in env["applied"])


def test_undo_with_a_user_override_still_clears_the_powerful_hem_set(env):
    now = _now()
    out = dhw_boost.apply(now)             # inside the deadband: Powerful ON
    assert env["powerful"] == [True]
    sid = _add(atype="tank_setback", start=now - timedelta(minutes=1), end=now + timedelta(hours=8),
               params={"tank_power": True, "tank_temp": 37}, status="completed")
    db.mark_action_user_overridden(sid, overridden_at=now.isoformat())  # the user owns the target
    res = dhw_boost.undo(now + timedelta(minutes=5))
    assert {"tank_powerful": False} in env["applied"]
    assert res["restored"] is True
    assert db.get_action_by_id(out["row_id"])["status"] == "completed"


def test_hold_release_margin_uses_the_override_grace(env, monkeypatch):
    now = _now()
    dhw_boost.apply(now)
    monkeypatch.setattr("src.config.config.DAIKIN_OVERRIDE_GRACE_SECONDS", 900)
    env["dev"].tank_target = 41.0
    monkeypatch.setattr(sm, "_device_read_wall", lambda: (now + timedelta(minutes=5)).timestamp())
    _add(atype="tank_setback", start=now + timedelta(minutes=6), end=now + timedelta(hours=2),
         params={"tank_power": True, "tank_temp": 37})
    _reconcile(now + timedelta(minutes=7), env["dev"])
    assert env["applied"] == []          # read inside the 900 s grace: still held
    monkeypatch.setattr(sm, "_device_read_wall", lambda: (now + timedelta(minutes=20)).timestamp())
    _reconcile(now + timedelta(minutes=21), env["dev"])
    assert {"tank_power": True, "tank_temp": 37} in env["applied"]


def test_hold_continues_when_the_user_raised_the_target_further(env, monkeypatch):
    now = _now()
    dhw_boost.apply(now)
    env["dev"].tank_target = 55.0
    monkeypatch.setattr(sm, "_device_read_wall", lambda: (now + timedelta(minutes=30)).timestamp())
    _add(atype="tank_setback", start=now + timedelta(minutes=10), end=now + timedelta(hours=2),
         params={"tank_power": True, "tank_temp": 37})
    _reconcile(now + timedelta(minutes=31), env["dev"])
    assert env["applied"] == []


def test_a_pending_restore_blocks_a_new_boost_with_a_distinct_code(env, monkeypatch):
    now, _out = _released_boost(env, monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "active")
    with pytest.raises(dhw_boost.BoostBlocked) as ei:
        dhw_boost.apply(now + timedelta(hours=4, minutes=2))
    assert ei.value.code == "RestorePending"
    assert dhw_boost.status(now + timedelta(hours=4, minutes=2))["blocked"]["error"] == "RestorePending"


def test_target_divergence_alert_is_skipped_while_a_boost_row_is_pending(env, monkeypatch):
    now, _out = _released_boost(env, monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.config.config.TANK_TARGET_DIVERGENCE_CHECK_ENABLED", True)
    called: list[int] = []
    monkeypatch.setattr(sm, "_scheduled_tank_state", lambda n: called.append(1) or (None, "none", None))
    sm._check_tank_target_divergence(env["dev"], now + timedelta(hours=4, minutes=5), trigger="t")
    assert called == []   # returned before even reading the plan


def test_boot_paths_take_the_tank_lock():
    assert "TANK_WRITE_LOCK" in inspect.getsource(sm.recover_on_boot)
    assert "TANK_WRITE_LOCK" in inspect.getsource(sm.apply_safe_defaults)


def test_gated_ticks_do_not_consume_restore_attempts(env, monkeypatch):
    """Hold expires inside a 3 h legionella stand-off: every skipped tick is free,
    the restore happens right after the stand-off and nothing is abandoned."""
    now = _now()
    _add(atype="tank_setback", start=now - timedelta(minutes=1), end=now + timedelta(hours=20),
         params={"tank_power": True, "tank_temp": 37}, status="completed")
    out = dhw_boost.apply(now)
    sent: list[str] = []
    monkeypatch.setattr("src.state_machine.notify_risk", lambda m, extra=None: sent.append(m))
    s0 = now + timedelta(hours=3, minutes=30)
    monkeypatch.setattr(
        "src.state_machine.in_legionella_standoff",
        lambda t: s0 <= t < s0 + timedelta(hours=3),
    )
    t0 = now + timedelta(hours=4, minutes=1)
    for minute in range(0, 145):  # ticks all inside the stand-off (ends at +6h30)
        _reconcile(t0 + timedelta(minutes=minute), env["dev"])
    row = db.get_action_by_id(out["row_id"])
    assert row["status"] == "active", row.get("error_msg")
    assert int(row["params"].get("restore_attempts", 0)) == 0
    assert env["applied"] == []
    _reconcile(s0 + timedelta(hours=3, minutes=1), env["dev"])
    row = db.get_action_by_id(out["row_id"])
    assert row["status"] == "completed"
    assert env["applied"], "restore must be written once the stand-off ends"
    assert sent == []
