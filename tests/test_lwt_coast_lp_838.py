"""LWT coast mode lp, comfort backstop, learning log (#838)."""
from __future__ import annotations

import json
import math
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.scheduler import lwt_coast
from src.scheduler.lp_dispatch import (
    _lp_offsets,
    _write_lwt_preheat_actions,
    space_heating_gate_state,
)
from src.scheduler.lp_optimizer import LpPlan

TZ = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", -2)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_COMFORT_BAND_C", 0.5)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -10.0)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", 5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0)
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 21.0)
    monkeypatch.setattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 1)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5, raising=False)
    monkeypatch.setattr(config, "LP_W3_PEAK_COAST_DELTA_C", 1.0, raising=False)
    monkeypatch.setattr(config, "LP_W3_IMPLAUSIBLE_BELOW_FLOOR_C", 2.0, raising=False)
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "lp")
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "setback")
    monkeypatch.setattr("src.scheduler.lp_dispatch._space_heating_demand_present", lambda: True)
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 150)


@pytest.fixture()
def tmpdb(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        yield


def _plan(n=4, *, lwt=-8.0, space=0.0, start=None, bands=None, indoor=None):
    t0 = start or datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    p = LpPlan(
        ok=True, status="Optimal", objective_pence=0.0,
        slot_starts_utc=[t0 + timedelta(minutes=30 * i) for i in range(n)],
        price_pence=[25.4] * n, temp_outdoor_c=[5.0] * n,
        cheap_threshold_pence=18.97, peak_threshold_pence=31.81,
        tariff_structure_kind="banded", price_band=bands or ["peak"] * n,
    )
    p.lwt_offset_c = [lwt] * n
    p.space_electric_kwh = [space] * n
    p.indoor_temp_c = list(indoor) if indoor is not None else [20.5] * (n + 1)
    return p


def _curve(t):
    """Piecewise weather curve stand-in: 0C->35, 10C->27, 14C->24."""
    pts = [(0.0, 35.0), (10.0, 27.0), (14.0, 24.0)]
    if t <= pts[0][0]:
        return pts[0][1]
    for (a, la), (b, lb) in zip(pts, pts[1:]):
        if t <= b:
            return la + (lb - la) * (t - a) / (b - a)
    return pts[-1][1]


# ── coast mode ───────────────────────────────────────────────────────────────


def test_setback_mode_writes_minus_two(monkeypatch):
    assert set(_lp_offsets(_plan())) == {-2}


def test_lp_raw_mode_uses_lp_value_and_clamps(monkeypatch):
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp_raw")
    assert set(_lp_offsets(_plan(lwt=-8.0))) == {-5}  # default clamp +-5
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -10.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", 10.0, raising=False)
    assert set(_lp_offsets(_plan(lwt=-8.0))) == {-8}
    assert set(_lp_offsets(_plan(lwt=-14.0))) == {-10}
    # a heating slot keeps the old (tighter) rules, untouched by coast mode
    assert set(_lp_offsets(_plan(lwt=3.0, space=0.4))) == {3}


@pytest.mark.parametrize("outdoor,indoor,expected", [
    (10.0, 22.5, -3),     # 24.5 - 27 = -2.5 -> -3 (half away from zero)
    (0.0, 21.0, -10),     # 23 - 35 = -12 -> clamped to the -10 floor
    (14.0, 23.0, -1),     # 25 - 24 = +1 -> never positive on a coast slot
])
def test_lp_mode_physics_target(monkeypatch, outdoor, indoor, expected):
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp")
    monkeypatch.setattr("src.physics.get_lwt_base_c", _curve)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -10.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_COAST_DELTA_C", 2.0, raising=False)
    p = _plan(lwt=-9.0, indoor=[indoor] * 5)
    p.temp_outdoor_c = [outdoor] * 4
    got = set(_lp_offsets(p))
    if expected == -1:
        assert got <= {0, -1} and max(got) <= 0
    else:
        assert got == {expected}


def test_lp_mode_clamp_and_fallbacks(monkeypatch):
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp")
    monkeypatch.setattr("src.physics.get_lwt_base_c", _curve)
    p = _plan(indoor=[21.0] * 5)
    p.temp_outdoor_c = [0.0] * 4
    assert set(_lp_offsets(p)) == {-5}      # default LP_OFFSET_MIN -5
    # no predicted indoor and no live reading -> setback value
    p2 = _plan(indoor=[21.0] * 5)
    p2.temp_outdoor_c = [10.0] * 4
    from src.scheduler import lwt_coast
    p2.indoor_temp_c = []
    assert lwt_coast.coast_target(p2, 0)["offset"] is None
    # live indoor fallback
    assert lwt_coast.coast_target(p2, 0, 22.5)["offset"] == -3


def test_heating_slots_unchanged_in_lp_mode(monkeypatch):
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp")
    assert set(_lp_offsets(_plan(lwt=3.0, space=0.4))) == {3}


def test_lp_mode_respects_outdoor_cutoff(monkeypatch):
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp")
    p = _plan()
    p.temp_outdoor_c = [16.0] * 4
    assert _lp_offsets(p) == [None] * 4


def test_gate_state_reports_coast_mode(monkeypatch, tmpdb):
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp")
    assert space_heating_gate_state()["coast_mode"] == "lp"


def test_physics_inverse_honours_range(monkeypatch):
    from src.physics import lwt_offset_from_space_kw
    assert lwt_offset_from_space_kw(0.0, 5.0, lo=-10, hi=10) == -10
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -2.0)
    assert lwt_offset_from_space_kw(0.0, 5.0) == -2
    assert lwt_offset_from_space_kw(0.0, 5.0, lo=-8) == -8


@pytest.mark.parametrize("mode,expected", [("setback", -2), ("lp_raw", -8), ("lp", -3)])
def test_wire_coast_slot_reaches_set_lwt_offset(monkeypatch, tmpdb, mode, expected):
    import src.state_machine as sm
    from src.daikin.models import DaikinDevice

    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", mode)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -10.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", 10.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.physics.get_lwt_base_c", _curve)
    monkeypatch.setattr(config, "DAIKIN_VALVE_SETTLE_SECONDS", 0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_POST_WRITE_VERIFY_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "PREFIRE_STATE_MATCH_ENABLED", True)
    sm._FIRST_APPLIED_SESSION.clear()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    plan = _plan(n=4, start=now - timedelta(minutes=10), indoor=[22.5] * 5)
    plan.temp_outdoor_c = [10.0] * 4
    plan_date = now.date().isoformat()
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    assert _write_lwt_preheat_actions(plan_date, plan, []) >= 1
    rows = db.get_actions_for_plan_date(plan_date, device="daikin")
    client = MagicMock()
    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=0.0)
    sm._reconcile_daikin_actions(rows, client, dev, now, trigger="test")
    client.set_lwt_offset.assert_called_once()
    assert client.set_lwt_offset.call_args[0][1] == expected


# ── comfort backstop ─────────────────────────────────────────────────────────


@pytest.fixture()
def bs(monkeypatch, tmpdb):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_ENABLED", True)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_MARGIN_C", 0.5)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_TICKS", 2)
    lwt_coast.reset_backstop()
    # 14:00 UTC in November = 14:00 local, daytime (setpoint floor 21)
    now = datetime(2026, 11, 4, 14, 10, tzinfo=UTC)
    plan_date = "2026-11-04"
    rid = db.upsert_action(
        plan_date=plan_date, start_time="2026-11-04T14:00:00Z", end_time="2026-11-04T16:00:00Z",
        device="daikin", action_type="lwt_preheat", params={"lwt_offset": -8, "lp_optimizer": True},
        status="active",
    )
    applied = []
    notified = []
    monkeypatch.setattr("src.daikin_bulletproof.apply_scheduled_daikin_params",
                        lambda dev, client, params, trigger, **kw: applied.append((params, trigger)) or True)
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: notified.append(extra))
    replan = MagicMock(return_value=True)

    def run(indoor, in_peak=False, now_=now):
        monkeypatch.setattr(db, "get_latest_indoor_reading",
                            lambda max_age_minutes=30: None if indoor is None else {"temp_c": indoor})
        return lwt_coast.backstop_tick(now_utc=now_, plan_date=plan_date, dev=MagicMock(),
                                       client=MagicMock(), in_peak=in_peak, replan_fn=replan)

    return run, rid, applied, notified, replan, plan_date


def test_backstop_two_ticks_fire_once(bs):
    run, rid, applied, notified, replan, _ = bs
    assert run(20.3)["fired"] is False          # tick 1 (floor 21 - 0.5 = 20.5)
    assert applied == []
    assert run(20.3)["fired"] is True           # tick 2
    assert applied == [({"lwt_offset": 0}, "lwt_backstop")]
    row = db.get_action_by_id(rid)
    assert row["status"] == "completed" and row["error_msg"] == "comfort_backstop"
    assert len(notified) == 1
    replan.assert_called_once_with(force_write_devices=True, trigger_reason="lwt_backstop", bypass_cooldown=True)
    run(20.0)
    run(20.0)                                    # row no longer active: nothing more
    assert len(applied) == 1 and len(notified) == 1
    logs = db.get_action_logs(device="daikin", action="lwt_comfort_backstop")
    assert len(logs) == 1


def test_backstop_single_tick_and_recovery_do_nothing(bs):
    run, rid, applied, notified, replan, _ = bs
    run(20.0)
    run(21.0)       # recovered -> counter resets
    run(20.0)
    assert applied == [] and replan.call_count == 0
    assert db.get_action_by_id(rid)["status"] == "active"


def test_backstop_above_floor_and_stale_sensor_do_nothing(bs):
    run, _, applied, *_ = bs
    for _i in range(4):
        run(20.6)
    for _i in range(4):
        run(None)
    assert applied == []


def test_backstop_ignores_non_negative_row(monkeypatch, bs):
    run, rid, applied, *_ = bs
    with db._lock:
        c = db.get_connection()
        c.execute("UPDATE action_schedule SET params = ? WHERE id = ?", (json.dumps({"lwt_offset": 3}), rid))
        c.commit()
        c.close()
    run(15.0)
    run(15.0)
    assert applied == []


def test_backstop_respects_read_only_and_passive(monkeypatch, bs):
    run, _, applied, *_ = bs
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    run(15.0)
    run(15.0)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    run(15.0)
    run(15.0)
    assert applied == []


def test_floor_selection_night_peak_day():
    night = datetime(2026, 11, 4, 2, 0, tzinfo=UTC)
    day = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert lwt_coast.comfort_floor_c(night, False) == 17.5
    assert lwt_coast.comfort_floor_c(night, True) == 17.5
    assert lwt_coast.comfort_floor_c(day, True) == 20.0
    assert lwt_coast.comfort_floor_c(day, False) == 21.0


def test_backstop_night_floor_used(monkeypatch, bs):
    run, rid, applied, *_ = bs
    night = datetime(2026, 11, 4, 14, 10, tzinfo=UTC)
    # at 14:10 it is day: 19.0 < 20.5 -> fires; confirm via night time instead
    with db._lock:
        c = db.get_connection()
        c.execute("UPDATE action_schedule SET start_time='2026-11-04T01:00:00Z', end_time='2026-11-04T03:00:00Z' WHERE id=?", (rid,))
        c.commit()
        c.close()
    n = datetime(2026, 11, 4, 2, 0, tzinfo=UTC)
    run(18.0, now_=n)
    run(18.0, now_=n)
    assert applied == []          # 18.0 >= 17.5 - 0.5
    run(16.9, now_=n)
    run(16.9, now_=n)
    assert len(applied) == 1


# ── learning log ─────────────────────────────────────────────────────────────


def test_planned_rows_upserted_at_dispatch(monkeypatch, tmpdb):
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp_raw")
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    monkeypatch.setattr("src.physics.get_lwt_base_c", _curve)
    t0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    plan = _plan(n=4, start=t0, lwt=-4.0)
    _write_lwt_preheat_actions("2026-11-04", plan, [])
    rows = db.get_lwt_learning_rows("2026-11-04T10:00:00Z", "2026-11-04T12:00:00Z")
    assert len(rows) == 4
    r = rows[0]
    assert r["coast_mode"] == "lp_raw" and r["source"] == "lp"
    assert r["curve_lwt_c"] == _curve(5.0) and r["coast_delta_c"] == 2.0
    assert abs(r["coast_target_lwt_c"] - 22.5) < 0.6
    assert r["offset_lp_raw"] == -4.0 and r["offset_written"] == -4.0
    assert r["floor_c"] == 20.0 and r["price_band"] == "peak" and r["cop_space"] > 1
    first_written = r["written_at_utc"]
    # re-dispatch with a new plan overwrites the planned values, keeps written_at
    _write_lwt_preheat_actions("2026-11-04", _plan(n=4, start=t0, lwt=-6.0), [])
    r2 = db.get_lwt_learning_rows("2026-11-04T10:00:00Z", "2026-11-04T10:30:00Z")[0]
    assert r2["offset_lp_raw"] == -6.0 and r2["written_at_utc"] == first_written


def _seed_synthetic_day(day: date, ua_w_per_k: float, c_kwh_per_k: float):
    """Indoor cools exponentially during a coast window, 1 reading / 10 min."""
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ).astimezone(UTC)
    to = 5.0
    t = 20.0
    k = ua_w_per_k / 1000.0 / c_kwh_per_k  # 1/h
    step_h = 10 / 60
    with db._lock:
        conn = db.get_connection()
        for i in range(0, 24 * 6):
            ts = start + timedelta(minutes=10 * i)
            conn.execute("INSERT OR REPLACE INTO room_temperature_history (captured_at, room, temp_c) VALUES (?,?,?)",
                         (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "lounge", t))
            conn.execute(
                "INSERT INTO daikin_telemetry (fetched_at, source, outdoor_temp_c, lwt_actual_c) VALUES (?,?,?,?)",
                (ts.timestamp(), "live", to, 30.0))
            conn.execute("INSERT INTO execution_log (timestamp, daikin_lwt_offset) VALUES (?,?)",
                         (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), -8))
            t = to + (t - to) * math.exp(-k * step_h)
        for b in range(12):
            conn.execute(
                "INSERT OR REPLACE INTO daikin_consumption_2hourly (date,bucket_idx,kwh_total,kwh_heating,kwh_dhw,source,fetched_at) "
                "VALUES (?,?,?,?,?,?,?)", (day.isoformat(), b, 0.0, 0.0, 0.0, "t", "x"))
        conn.commit()
        conn.close()


def test_nightly_job_fills_and_estimates_ua(monkeypatch, tmpdb):
    from src.analytics import lwt_learning

    day = date(2026, 11, 3)
    ua, c = 200.0, 16.5
    _seed_synthetic_day(day, ua, c)
    monkeypatch.setattr("src.analytics.thermal_learning.get_building_thermal_mass_kwh_per_k", lambda: c)
    # a planned row so the pred error is computed
    st = datetime(2026, 11, 3, 6, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.upsert_lwt_learning_planned([{"slot_time_utc": st, "indoor_pred_c": 18.0, "offset_written": -8.0}])
    row = lwt_learning.run_for_day(day, TZ)
    assert row["n_coast_slots"] > 40
    assert abs(row["ua_est_w_per_k"] - ua) / ua < 0.10
    slot = db.get_lwt_learning_rows(st, "2026-11-03T06:30:00Z")[0]
    assert slot["indoor_real_c"] is not None and slot["outdoor_real_c"] == 5.0
    assert slot["device_offset"] == -8 and slot["filled_at_utc"]
    assert slot["indoor_pred_c"] == 18.0           # planned field preserved
    assert row["pred_err_mean_c"] is not None
    daily = db.get_lwt_learning_daily(5)
    assert daily[0]["date"] == "2026-11-03"
    assert db.get_action_logs(device="system", action="lwt_learning_summary")


def test_pump_off_delta():
    from src.analytics.lwt_learning import pump_off_delta

    rows = [{"lwt_actual_c": 24.0, "indoor_real_c": 22.0, "heating_kwh": 0.0}] * 3 + \
           [{"lwt_actual_c": 33.0, "indoor_real_c": 21.0, "heating_kwh": 0.4}] * 2
    d = pump_off_delta(rows)
    assert d["pump_off_delta_median_c"] == 2.0 and d["pump_on_delta_p10_c"] == 12.0
    assert d["pump_off_n"] == 3 and d["pump_on_n"] == 2


def test_k_estimate_from_heating_slots():
    from src.analytics.lwt_learning import estimate_k_kw_per_c

    rows = [{"heating_kwh": 0.5, "lwt_actual_c": 33.0} for _ in range(6)]  # 1 kW / 15 K
    k, n = estimate_k_kw_per_c(rows)
    assert n == 6 and abs(k - 1.0 / 15.0) < 1e-3


def test_prune_lwt_learning_log(monkeypatch, tmpdb):
    monkeypatch.setattr(config, "LWT_LEARNING_RETENTION_DAYS", 120)
    old = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.upsert_lwt_learning_planned([{"slot_time_utc": old}, {"slot_time_utc": new}])
    res = db.prune_history_tables()
    assert res["lwt_learning_log"] == 1
    left = db.get_lwt_learning_rows("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z")
    assert [r["slot_time_utc"] for r in left] == [new]


# ── API ──────────────────────────────────────────────────────────────────────


def test_api_shape(monkeypatch, tmpdb):
    from fastapi.testclient import TestClient

    from src.api.main import app

    db.upsert_lwt_learning_daily({"date": "2026-11-03", "n_coast_slots": 10, "n_heat_slots": 5,
                                  "ua_est_w_per_k": 190.0, "k_est_kw_per_c": 0.06,
                                  "pred_err_mean_c": 0.1, "pred_err_p90_c": 0.4,
                                  "payload": {"ua_pinned_w_per_k": 200.0}})
    r = TestClient(app).get("/api/v1/thermal/lwt-learning?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["daily"][0]["ua_est_w_per_k"] == 190.0
    assert body["daily"][0]["ua_pinned_w_per_k"] == 200.0
    assert "slots" in body["yesterday"] and "coast_mode" in body
