"""Warm-side LWT backstop (#855): mirror of the #838 cold backstop."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import db
from src.config import config
from src.scheduler import lwt_coast
from src.scheduler.lp_dispatch import _lp_offsets, _tier_offsets, space_heating_gate_state
from src.scheduler.lp_optimizer import LpPlan


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "LWT_WARM_BACKSTOP_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LWT_WARM_BACKSTOP_TICKS", 2, raising=False)
    monkeypatch.setattr(config, "LWT_WARM_BACKSTOP_HOLD_MINUTES", 60, raising=False)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_ENABLED", True)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_TICKS", 2)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_COMFORT_BAND_C", 0.5)
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 21.0)
    monkeypatch.setattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30, raising=False)
    monkeypatch.setitem(config._overrides, "LP_W3_CEILING_C", 23.0)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -10.0)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", 5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_BOOST_C", 3, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", -2)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True)
    lwt_coast.reset_backstop()


@pytest.fixture()
def tmpdb(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        yield


def _now():
    return datetime.now(UTC).replace(second=0, microsecond=0)


def _z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def wb(monkeypatch, tmpdb):
    now = _now()
    plan_date = now.date().isoformat()
    rid = db.upsert_action(
        plan_date=plan_date, start_time=_z(now - timedelta(minutes=30)),
        end_time=_z(now + timedelta(hours=2)),
        device="daikin", action_type="lwt_preheat", params={"lwt_offset": 10, "lp_optimizer": True},
        status="active",
    )
    applied = []
    monkeypatch.setattr("src.daikin_bulletproof.apply_scheduled_daikin_params",
                        lambda dev, client, params, trigger, **kw: applied.append((params, trigger, kw)) or True)
    replan = MagicMock(return_value=True)

    def run(indoor, now_=now):
        monkeypatch.setattr(db, "get_latest_indoor_reading",
                            lambda max_age_minutes=30: None if indoor is None else {"temp_c": indoor})
        return lwt_coast.warm_backstop_tick(now_utc=now_, plan_date=plan_date, dev=MagicMock(),
                                            client=MagicMock(), replan_fn=replan)

    return run, rid, applied, replan, now, plan_date


def test_two_ticks_fire(wb):
    run, rid, applied, replan, now, _ = wb
    assert run(22.6)["fired"] is False
    assert applied == []
    out = run(22.6)
    assert out["fired"] is True
    assert applied[0][0] == {"lwt_offset": 0} and applied[0][2]["skip_if_matches"] is False
    row = db.get_action_by_id(rid)
    assert row["status"] == "completed" and row["error_msg"] == "warm_backstop"
    hold = lwt_coast.get_warm_hold_until()
    assert hold is not None and abs((hold - now).total_seconds() - 3600) < 5
    assert lwt_coast.get_hold_until() is None          # cold hold untouched
    replan.assert_called_once_with(force_write_devices=True, trigger_reason="lwt_warm_backstop",
                                   bypass_cooldown=True)
    logs = db.get_action_logs(device="daikin", action="lwt_warm_backstop")
    assert len(logs) == 1 and logs[0]["result"] == "ok"
    assert space_heating_gate_state()["warm_backstop_hold_until"] is not None
    run(23.0)
    run(23.0)                                           # row no longer active
    assert len(applied) == 1


def test_one_tick_and_below_threshold_do_nothing(wb):
    run, rid, applied, replan, *_ = wb
    run(22.6)                                           # one tick only
    assert applied == []
    run(22.4)                                           # below 23 - 0.5: resets
    run(22.6)
    assert applied == []
    assert db.get_action_by_id(rid)["status"] == "active"
    replan.assert_not_called()


def test_stale_sensor_holds_counter(wb):
    run, *_ = wb
    run(22.6)
    assert lwt_coast._warm_ticks == 1
    run(None)
    assert lwt_coast._warm_ticks == 1
    assert run(22.6)["fired"]


def test_negative_row_untouched_by_warm(monkeypatch, tmpdb):
    now = _now()
    plan_date = now.date().isoformat()
    rid = db.upsert_action(
        plan_date=plan_date, start_time=_z(now - timedelta(minutes=30)), end_time=_z(now + timedelta(hours=2)),
        device="daikin", action_type="lwt_preheat", params={"lwt_offset": -2}, status="active",
    )
    applied = []
    monkeypatch.setattr("src.daikin_bulletproof.apply_scheduled_daikin_params",
                        lambda *a, **k: applied.append(1) or True)
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: {"temp_c": 24.0})
    for _ in range(3):
        out = lwt_coast.warm_backstop_tick(now_utc=now, plan_date=plan_date, dev=MagicMock(), client=MagicMock())
    assert not out["fired"] and applied == []
    assert db.get_action_by_id(rid)["status"] == "active"


def test_cold_backstop_ignores_positive_row(wb):
    _, rid, applied, _, now, plan_date = wb
    monkeypatch_reading = {"temp_c": 10.0}
    db_get = db.get_latest_indoor_reading
    db.get_latest_indoor_reading = lambda max_age_minutes=30: monkeypatch_reading
    try:
        for _ in range(3):
            out = lwt_coast.backstop_tick(now_utc=now, plan_date=plan_date, dev=MagicMock(),
                                          client=MagicMock(), in_peak=False)
    finally:
        db.get_latest_indoor_reading = db_get
    assert not out["fired"] and applied == []


def test_read_only_and_passive_never_fire(wb, monkeypatch):
    run, rid, applied, *_ = wb
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    run(24.0); run(24.0)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    run(24.0); run(24.0)
    assert applied == [] and db.get_action_by_id(rid)["status"] == "active"


def test_apply_not_written_keeps_row_and_counter(wb, monkeypatch):
    run, rid, applied, replan, *_ = wb
    monkeypatch.setattr("src.daikin_bulletproof.apply_scheduled_daikin_params", lambda *a, **k: False)
    run(23.0)
    out = run(23.0)
    assert not out["fired"] and lwt_coast._warm_ticks >= 2
    assert db.get_action_by_id(rid)["status"] == "active"
    assert lwt_coast.get_warm_hold_until() is None
    replan.assert_not_called()
    assert db.get_action_logs(device="daikin", action="lwt_warm_backstop")[0]["result"] == "skipped"


def _plan(n, start, lwt=3.0, space=0.4):
    p = LpPlan(
        ok=True, status="Optimal", objective_pence=0.0,
        slot_starts_utc=[start + timedelta(minutes=30 * i) for i in range(n)],
        price_pence=[12.49] * n, temp_outdoor_c=[5.0] * n,
        cheap_threshold_pence=18.97, peak_threshold_pence=31.81,
        tariff_structure_kind="banded", price_band=["cheap"] * n,
    )
    p.lwt_offset_c = [lwt] * n
    p.space_electric_kwh = [space] * n
    p.indoor_temp_c = [21.5] * (n + 1)
    return p


def test_hold_zeroes_positive_offsets_lp_and_tier(wb, monkeypatch):
    run, rid, applied, replan, now, plan_date = wb
    start = now.replace(minute=(now.minute // 30) * 30)
    plan = _plan(8, start)
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    before = _lp_offsets(plan, None, now_utc=now)
    assert before[0] == 3 and before[-1] == 3
    run(23.0); run(23.0)
    hold = lwt_coast.get_warm_hold_until()
    guards = {}
    after = _lp_offsets(plan, None, now_utc=now, guards=guards)
    for i, st in enumerate(plan.slot_starts_utc):
        assert after[i] == (0 if st < hold else 3), (st, hold)
    assert guards.get("warm_backstop_hold", 0) >= 1
    # negative offsets are never touched by the warm hold
    cold_plan = _plan(4, start, lwt=-8.0, space=0.0)
    cold_plan.price_band = ["peak"] * 4
    assert set(_lp_offsets(cold_plan, None, now_utc=now)) == {-2}
    # tier rule: cheap-band boost zeroed inside the hold
    t = _tier_offsets(plan, [], None)
    for i, st in enumerate(plan.slot_starts_utc):
        if st < hold:
            assert not t[i]
    # expired hold: boost returns
    db.set_kv("lwt_warm_backstop_hold_until", _z(now - timedelta(minutes=1)))
    assert _lp_offsets(plan, None, now_utc=now)[0] == 3
    assert space_heating_gate_state()["warm_backstop_hold_until"] is None


def test_wire_through_apply_scheduled_daikin_params(monkeypatch, tmpdb):
    from src.daikin.models import DaikinDevice

    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "DAIKIN_VALVE_SETTLE_SECONDS", 0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_POST_WRITE_VERIFY_ENABLED", False, raising=False)
    now = _now()
    plan_date = now.date().isoformat()
    rid = db.upsert_action(
        plan_date=plan_date, start_time=_z(now - timedelta(minutes=30)), end_time=_z(now + timedelta(hours=2)),
        device="daikin", action_type="lwt_preheat", params={"lwt_offset": 5}, status="active",
    )
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: {"temp_c": 22.8})
    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=5.0)
    client = MagicMock()
    replan = MagicMock(return_value=True)
    for _ in range(2):
        out = lwt_coast.warm_backstop_tick(now_utc=now, plan_date=plan_date, dev=dev, client=client,
                                           replan_fn=replan)
    client.set_lwt_offset.assert_called_once()
    assert client.set_lwt_offset.call_args[0][1] == 0
    assert out["fired"] and db.get_action_by_id(rid)["error_msg"] == "warm_backstop"
