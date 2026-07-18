"""Deadband-aware warmup escalation (#735, follow-up to #732).

The firmware only reheats when tank ≤ target − differential (~6-7 °C measured),
so a warm-tank day turns the commanded warmup into a silent no-op. Measured
2026-07-17: commanded 47, tank 42 — nothing happened, showers at ~40.5 °C
against the family's declared 45. The declared dial is the spec: force the
lift ONLY when the coast projection misses the next shower window's floor.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.config import config
from src.state_machine import _warmup_deadband_force_reason


@pytest.fixture(autouse=True)
def _fixed_env(monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "normal")
    # Pin the measured physics so projections are deterministic.
    monkeypatch.setattr(config, "DHW_REHEAT_DIFFERENTIAL_FALLBACK_C", 6.0, raising=False)
    yield


def _dev(tank: float, target: float | None = None):
    return SimpleNamespace(tank_temperature=tank, tank_target=target)


# 2026-07-17 12:05 UTC = 13:05 BST — the real incident's warmup fire time.
_FIRE = datetime(2026, 7, 17, 12, 5, tzinfo=UTC)


def test_incident_case_lifts_the_heat_pump_not_powerful():
    """Tank 42, target 47 (inside the deadband) and ~7 h coast to the 20:00
    window → projected < 45. 47 is below the 50 °C cliff, so the heat pump can
    do the lift: command the cliff, NOT Powerful (#737)."""
    r = _warmup_deadband_force_reason(_dev(42.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["window"] == "evening_showers"
    assert r["projected_c"] < r["floor_c"]
    assert r["mechanism"] == "hp_target_lift"
    # Command the cliff — stable across the heat-up; clears the deadband from
    # tank 42 (Δ = 50 − 42 = 8 > differential) and stays sub-resistance.
    assert r["lift_target_c"] == int(r["cliff_c"])
    assert r["lift_target_c"] - r["tank_c"] > r["differential_c"]


def test_powerful_is_the_fallback_only_when_tank_cannot_clear_sub_cliff():
    """Tank 44: cliff − tank (6) ≤ differential — no sub-cliff target can
    trigger the heat pump, so resistance (Powerful) is the only way to add
    heat, and the missed floor demands it."""
    r = _warmup_deadband_force_reason(_dev(44.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["mechanism"] == "powerful"
    assert r["lift_target_c"] is None


def test_mechanism_stays_hp_mid_lift_when_tank_warms_into_the_powerful_band():
    """Review #737 finding 3: once the device is lifted to the cliff, a tick
    where the tank has warmed into the would-be-Powerful band (44) must NOT
    flip to resistance — the heat pump is already doing the lift. ``already_
    lifted`` (device target at the cliff) keeps it on the HP."""
    r = _warmup_deadband_force_reason(
        _dev(44.0, target=50.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["already_lifted"] is True
    assert r["mechanism"] == "hp_target_lift"
    assert r["lift_target_c"] == 50


def test_not_yet_lifted_tank_in_powerful_band_still_falls_back():
    """The same tank 44 but device NOT yet lifted (target 47) is the genuine
    fallback corner — resistance, since the HP can't be triggered sub-cliff."""
    r = _warmup_deadband_force_reason(
        _dev(44.0, target=47.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["already_lifted"] is False
    assert r["mechanism"] == "powerful"


def test_firmware_will_heat_unaided_no_force():
    """Δ9 is beyond the deadband — the plain command heats; no Powerful."""
    assert _warmup_deadband_force_reason(
        _dev(38.0), {"tank_temp": 47, "tank_power": True}, _FIRE) is None


def test_coast_clearing_the_floor_keeps_the_free_skip():
    """Tank 46 an hour before the window: inside the deadband, but the short
    coast stays above the 45 floor — the firmware skip is deliberate and
    cheaper. (At 13:00 the same 46 °C would NOT clear: τ=95 h drops it to
    ~44.3 by 20:00, which is exactly why the V3 schedule heats to 47.)"""
    late_fire = datetime(2026, 7, 17, 18, 5, tzinfo=UTC)  # 19:05 BST
    assert _warmup_deadband_force_reason(
        _dev(46.0), {"tank_temp": 47, "tank_power": True}, late_fire) is None


def test_already_at_target_no_force():
    assert _warmup_deadband_force_reason(
        _dev(47.5), {"tank_temp": 47, "tank_power": True}, _FIRE) is None


def test_missing_telemetry_fails_open_to_plain_command():
    assert _warmup_deadband_force_reason(
        _dev(None), {"tank_temp": 47, "tank_power": True}, _FIRE) is None
    assert _warmup_deadband_force_reason(
        SimpleNamespace(), {"tank_power": True}, _FIRE) is None


def test_vacation_preset_never_forces(monkeypatch):
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "vacation")
    assert _warmup_deadband_force_reason(
        _dev(42.0), {"tank_temp": 47, "tank_power": True}, _FIRE) is None


def test_early_fire_judges_every_window_not_just_the_soonest():
    """Review case: a cheap-night warmup fires at 04:05 BST. The soonest window
    is the morning reserve (floor 40) which a warm tank clears — but the
    EVENING 45 floor, 16 h out, does not survive the coast. Must force."""
    early = datetime(2026, 7, 17, 3, 5, tzinfo=UTC)
    r = _warmup_deadband_force_reason(_dev(44.5), {"tank_temp": 47, "tank_power": True}, early)
    assert r is not None
    assert r["window"] == "evening_showers"


def test_mid_window_fire_is_judged_against_the_current_floor():
    """Review case: a backstop row firing INSIDE the shower window used to be
    scored against tomorrow. The floor is owed NOW (hours = 0)."""
    mid = datetime(2026, 7, 17, 19, 30, tzinfo=UTC)  # 20:30 BST, inside 20-21h
    r = _warmup_deadband_force_reason(_dev(43.0), {"tank_temp": 45, "tank_power": True}, mid)
    assert r is not None
    assert r["window"] == "evening_showers"


def _fake_windows(monkeypatch, *windows):
    from src.dhw.comfort import ShowerComfortWindow

    made = tuple(ShowerComfortWindow(*w) for w in windows)
    monkeypatch.setattr("src.dhw.comfort.shower_windows", lambda **kw: made)


def test_end_hour_24_window_is_inside_not_tomorrow(monkeypatch):
    """#748: a declared x-24.0 window put its end at 00:00 of the SAME day, so
    "inside the window" was unsatisfiable and a fire inside it projected the
    coast to TOMORROW's start (~21 h of phantom cooling → inflated shortfall).
    Inside the window the floor is owed at hours = 0."""
    _fake_windows(monkeypatch, (20.0, 24.0, 45.0, "evening_showers"))
    inside = datetime(2026, 7, 17, 20, 30, tzinfo=UTC)  # 21:30 BST
    r = _warmup_deadband_force_reason(_dev(43.0), {"tank_temp": 47, "tank_power": True}, inside)
    assert r is not None
    assert r["hours_to_window"] == 0.0
    # projected at hours=0 is the live tank temp, not a phantom overnight coast
    assert r["projected_c"] == pytest.approx(43.0, abs=0.1)


def test_cross_midnight_window_tail_is_judged_as_current(monkeypatch):
    """#748: at 00:30 inside a 22:00-01:00 window, yesterday's instance is the
    live one — the floor is owed NOW, not in ~21.5 h."""
    _fake_windows(monkeypatch, (22.0, 1.0, 45.0, "evening_showers"))
    tail = datetime(2026, 7, 17, 23, 30, tzinfo=UTC)  # 00:30 BST next day
    r = _warmup_deadband_force_reason(_dev(43.0), {"tank_temp": 47, "tank_power": True}, tail)
    assert r is not None
    assert r["hours_to_window"] == 0.0


def test_zero_length_window_is_disabled_not_always(monkeypatch):
    """#750 review: start == end is the intuitive way to disable a window; the
    end<=start normalisation must not promote it to a 24 h always-owed floor."""
    _fake_windows(monkeypatch, (20.0, 20.0, 45.0, "evening_showers"))
    assert _warmup_deadband_force_reason(
        _dev(42.0), {"tank_temp": 47, "tank_power": True}, _FIRE) is None


def test_all_day_window_is_still_always_inside(monkeypatch):
    """0.0-24.0 is genuinely all-day (raw hours differ) — floor owed now."""
    _fake_windows(monkeypatch, (0.0, 24.0, 45.0, "evening_showers"))
    r = _warmup_deadband_force_reason(_dev(42.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["hours_to_window"] == 0.0


def test_cross_midnight_window_before_entry_projects_to_tonight(monkeypatch):
    """#748: outside a cross-midnight window, entry is TONIGHT's start (a few
    hours out), not treated as an inverted/empty window."""
    _fake_windows(monkeypatch, (22.0, 1.0, 45.0, "evening_showers"))
    noon = datetime(2026, 7, 17, 11, 0, tzinfo=UTC)  # 12:00 BST
    r = _warmup_deadband_force_reason(_dev(42.0), {"tank_temp": 47, "tank_power": True}, noon)
    assert r is not None
    assert r["hours_to_window"] == pytest.approx(10.0, abs=0.1)


# ---------------------------------------------------------------------------
# #741 review — the audit row is the #739 fit-exclusion window, so it must
# exist whenever Powerful may be live, not only on the happy apply path.
# ---------------------------------------------------------------------------


def _reconcile_env(monkeypatch, tmp_path, *, dev):
    import src.state_machine as sm
    from src import db

    path = tmp_path / "t.db"
    monkeypatch.setattr("src.config.config.DB_PATH", str(path))
    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._DEADBAND_FORCE_LOGGED.clear()
    db.init_db()
    now = datetime.now(UTC)
    conn = db.get_connection()
    try:
        from datetime import timedelta
        import json as _json
        conn.execute(
            """INSERT INTO action_schedule
               (date, start_time, end_time, device, action_type, params, status, created_at)
               VALUES (?, ?, ?, 'daikin', 'tank_warmup', ?, 'active', ?)""",
            (now.date().isoformat(),
             (now - timedelta(seconds=60)).isoformat(),
             (now + timedelta(seconds=1800)).isoformat(),
             _json.dumps({"tank_power": True, "tank_temp": 47, "tank_powerful": False}),
             now.isoformat()),
        )
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    rows = db.get_actions_for_plan_date(now.date().isoformat(), device="daikin")
    return sm, db, rows, rid, now


def _force_log_rows(db):
    conn = db.get_connection()
    try:
        return conn.execute(
            "SELECT params, result FROM action_log WHERE action='warmup_deadband_force'"
        ).fetchall()
    finally:
        conn.close()


def test_failed_apply_still_writes_the_audit_row(monkeypatch, tmp_path):
    """A timed-out set_tank_powerful can apply cloud-side, and the failed row is
    never reconciled again — the exclusion window must be written anyway."""
    from unittest.mock import MagicMock

    from src.daikin.client import DaikinError
    from src.daikin.models import DaikinDevice

    # Tank 44: Δ3 inside the deadband, cliff − 44 = 6 ≤ 6.5 → Powerful corner;
    # 44 < the 45 evening floor, so the coast misses at ANY time of day.
    dev = DaikinDevice(id="gw", name="x", tank_temperature=44.0,
                       tank_target=37.0, tank_on=True, tank_powerful=False)
    sm, db, rows, rid, now = _reconcile_env(monkeypatch, tmp_path, dev=dev)

    def _boom(*a, **k):
        raise DaikinError("timeout")
    monkeypatch.setattr("src.state_machine.apply_scheduled_daikin_params", _boom)

    sm._reconcile_daikin_actions(rows, MagicMock(), dev, now, trigger="test")

    row = db.get_action_by_id(rid)
    assert row["status"] == "failed"
    logged = _force_log_rows(db)
    assert len(logged) == 1
    assert logged[0][1] == "failed"
    assert '"mechanism": "powerful"' in logged[0][0]
    # And the db accessor (the fit's window source) sees it.
    times = db.get_deadband_force_powerful_times("2000-01-01T00:00:00", "2099-01-01T00:00:00")
    assert len(times) == 1


def test_prefire_match_confirms_and_writes_the_audit_row_once(monkeypatch, tmp_path):
    """Device already holds the escalated state (timeout-then-completed, or the
    user's own Powerful): the idempotency completion must still produce the
    exclusion window — exactly once."""
    from unittest.mock import MagicMock

    from src.daikin.models import DaikinDevice

    dev = DaikinDevice(id="gw", name="x", tank_temperature=44.0,
                       tank_target=47.0, tank_on=True, tank_powerful=True)
    sm, db, rows, rid, now = _reconcile_env(monkeypatch, tmp_path, dev=dev)

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda d, c, p, trigger: apply_calls.append(p) or True,
    )

    sm._reconcile_daikin_actions(rows, MagicMock(), dev, now, trigger="test")

    row = db.get_action_by_id(rid)
    assert row["status"] == "completed"
    assert len(apply_calls) == 0  # completed via idempotency, no PATCH
    logged = _force_log_rows(db)
    assert len(logged) == 1
    assert logged[0][1] == "confirmed"
    assert '"via": "prefire_match"' in logged[0][0]


# ---------------------------------------------------------------------------
# #742 — heartbeat settle: finish the HP lift at the GOAL, not the cliff
# ---------------------------------------------------------------------------


def _settle_env(monkeypatch, tmp_path, *, audit_mechanism="hp_target_lift",
                row_goal=47, row_status="completed"):
    """Seed a lifted-warmup world: row covering now + its audit log row."""
    import json as _json
    from datetime import timedelta

    import src.state_machine as sm
    from src import db

    path = tmp_path / "t.db"
    monkeypatch.setattr("src.config.config.DB_PATH", str(path))
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", False)
    # #743 review — without this the settle tests fail every Sunday
    # 11:00-13:00 UTC (the legionella stand-off guard, enabled by default).
    monkeypatch.setattr("src.config.config.DHW_LEGIONELLA_STANDOFF_ENABLED", False)
    sm._LIFT_SETTLE_ATTEMPTS.clear()
    db.init_db()
    now = datetime.now(UTC)
    actions = [{
        "id": 90, "action_type": "tank_warmup", "status": row_status,
        "start_time": (now - timedelta(minutes=40)).isoformat(),
        "end_time": (now + timedelta(minutes=80)).isoformat(),
        "params": _json.dumps({"tank_power": True, "tank_temp": row_goal,
                               "tank_powerful": False}),
    }]
    if audit_mechanism:
        db.log_action(device="daikin", action="warmup_deadband_force",
                      params={"row_id": 90, "mechanism": audit_mechanism,
                              "lift_target_c": 50},
                      result="applied", trigger="test")
    applied: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda d, c, p, trigger: applied.append({"params": p, "trigger": trigger}) or True,
    )
    return sm, db, actions, now, applied


def _settle_logs(db):
    conn = db.get_connection()
    try:
        return conn.execute(
            "SELECT params FROM action_log WHERE action='warmup_lift_settle'"
        ).fetchall()
    finally:
        conn.close()


def test_settle_recommands_the_goal_once_the_tank_reaches_it(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    dev = SimpleNamespace(tank_temperature=47.0, tank_target=50.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert len(applied) == 1
    assert applied[0]["params"] == {"tank_power": True, "tank_temp": 47.0}
    assert applied[0]["trigger"].startswith("warmup_lift_settle:")
    assert len(_settle_logs(db)) == 1


def test_settle_waits_while_the_pump_is_still_lifting(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    dev = SimpleNamespace(tank_temperature=45.5, tank_target=50.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert applied == []


def test_settle_noop_after_it_already_settled(monkeypatch, tmp_path):
    """Natural dedup: once the device target reads the goal, nothing to do."""
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    dev = SimpleNamespace(tank_temperature=47.4, tank_target=47.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert applied == []


def test_settle_respects_a_cliff_the_user_set_themselves(monkeypatch, tmp_path):
    """Device at 50 with NO hp_target_lift audit row = the user's own gesture."""
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path,
                                                audit_mechanism=None)
    dev = SimpleNamespace(tank_temperature=48.0, tank_target=50.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert applied == []


def test_settle_bails_inside_a_negative_boost_window(monkeypatch, tmp_path):
    """#745: a tank_negative_boost row covering now owns the tank — settling
    down to the warmup goal would forfeit the paid window, and nothing would
    re-raise the target (the boost row is completed; #619 re-asserts only
    Powerful). Even with the device still at OUR lift target (50), bail."""
    import json as _json
    from datetime import timedelta
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    actions.append({
        "id": 91, "action_type": "tank_negative_boost", "status": "completed",
        "start_time": (now - timedelta(minutes=10)).isoformat(),
        "end_time": (now + timedelta(minutes=50)).isoformat(),
        "params": _json.dumps({"tank_power": True, "tank_temp": 60,
                               "tank_powerful": True}),
    })
    dev = SimpleNamespace(tank_temperature=47.5, tank_target=50.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert applied == []


def test_settle_leaves_a_target_above_our_lift_alone(monkeypatch, tmp_path):
    """#745: the audit row says we lifted to 50; the device reads 60. That is
    an intent HEM never commanded (user gesture mid-lift, or a boost the row
    scan missed) — the old ">= cliff - 0.6" gate would have clobbered it."""
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    dev = SimpleNamespace(tank_temperature=47.5, tank_target=60.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert applied == []


def test_settle_halts_the_residue_of_our_own_finished_boost(monkeypatch, tmp_path):
    """#745 review: a FINISHED tank_negative_boost left the device at 60 —
    that 60 is HEM's own command, not a user gesture, and above the cliff the
    firmware grinds on at COP-1 resistance at now-positive prices. Settle must
    fire (pre-#745 behaviour) and re-command the warmup goal."""
    import json as _json
    from datetime import timedelta
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    actions.append({
        "id": 91, "action_type": "tank_negative_boost", "status": "completed",
        "start_time": (now - timedelta(minutes=90)).isoformat(),
        "end_time": (now - timedelta(minutes=5)).isoformat(),
        "params": _json.dumps({"tank_power": True, "tank_temp": 60,
                               "tank_powerful": True}),
    })
    dev = SimpleNamespace(tank_temperature=52.0, tank_target=60.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert len(applied) == 1
    assert applied[0]["params"] == {"tank_power": True, "tank_temp": 47.0}


def test_settle_skips_cliff_goal_rows(monkeypatch, tmp_path):
    """A PV-abundance row whose GOAL is the cliff wants the cliff — no settle."""
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path, row_goal=50)
    dev = SimpleNamespace(tank_temperature=50.0, tank_target=50.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert applied == []


def test_settle_kill_switch_and_read_only(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    dev = SimpleNamespace(tank_temperature=47.0, tank_target=50.0, tank_on=True)
    monkeypatch.setattr("src.config.config.DHW_WARMUP_LIFT_SETTLE_ENABLED", False)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert applied == []
    monkeypatch.setattr("src.config.config.DHW_WARMUP_LIFT_SETTLE_ENABLED", True)
    monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert applied == []


def test_settle_needs_a_row_covering_now(monkeypatch, tmp_path):
    """After the warmup window the setback owns the tank — never settle then."""
    from unittest.mock import MagicMock
    from datetime import timedelta

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    dev = SimpleNamespace(tank_temperature=47.0, tank_target=50.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev,
                                 now + timedelta(hours=3), trigger="heartbeat")
    assert applied == []


def test_settle_runs_once_per_episode_then_respects_the_users_cliff(monkeypatch, tmp_path):
    """#743 review real-bug: after a successful settle, a cliff-level target
    re-appearing inside the same window is the USER's gesture — HEM must not
    revert it (the old row-scoped ownership check clobbered it in a loop)."""
    from unittest.mock import MagicMock

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    dev = SimpleNamespace(tank_temperature=47.0, tank_target=50.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert len(applied) == 1  # first settle fires

    # The user sets the tank back to 50 mid-window; a fresh device read.
    dev2 = SimpleNamespace(tank_temperature=48.0, tank_target=50.0, tank_on=True)
    sm._check_warmup_lift_settle(actions, MagicMock(), dev2, now, trigger="heartbeat")
    assert len(applied) == 1  # respected — no second settle

    # Survives a restart (persistent audit row, not process state).
    sm._LIFT_SETTLE_ATTEMPTS.clear()
    sm._check_warmup_lift_settle(actions, MagicMock(), dev2, now, trigger="heartbeat")
    assert len(applied) == 1


def test_settle_gives_up_after_bounded_failed_attempts(monkeypatch, tmp_path):
    """#743 review risk: if Onecta refuses the lowered setpoint, retry a few
    times and then fall back to the safe cliff coast — not ~40 writes."""
    from unittest.mock import MagicMock

    from src.daikin.client import DaikinError

    sm, db, actions, now, applied = _settle_env(monkeypatch, tmp_path)
    calls: list[int] = []

    def _refuse(d, c, p, trigger):
        calls.append(1)
        raise DaikinError("READ_ONLY_CHARACTERISTIC")
    monkeypatch.setattr("src.state_machine.apply_scheduled_daikin_params", _refuse)

    dev = SimpleNamespace(tank_temperature=47.0, tank_target=50.0, tank_on=True)
    for _ in range(6):
        sm._check_warmup_lift_settle(actions, MagicMock(), dev, now, trigger="heartbeat")
    assert len(calls) == sm._LIFT_SETTLE_MAX_ATTEMPTS
    assert _settle_logs(db) == []  # no false 'applied' audit rows
