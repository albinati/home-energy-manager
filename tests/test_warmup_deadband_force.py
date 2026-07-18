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
    assert r["hours_to_window"] == 0.0
    assert r["projected_c"] == pytest.approx(43.0)


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
    assert logged[0][1] == "applied"
    assert '"via": "prefire_match"' in logged[0][0]
