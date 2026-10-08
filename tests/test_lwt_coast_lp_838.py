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
    (10.0, 22.5, -2),     # 24.5 - 27 = -2.5 -> floor(x + 0.5) = -2 (pipeline convention)
    (0.0, 21.0, -10),     # 23 - 35 = -12 -> clamped to the -10 floor
    (14.0, 23.0, 0),      # 25 - 24 = +1 -> clamped to 0: a coast slot never boosts
])
def test_lp_mode_physics_target(monkeypatch, outdoor, indoor, expected):
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp")
    monkeypatch.setattr("src.physics.get_lwt_base_c", _curve)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -10.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_COAST_DELTA_C", 2.0, raising=False)
    p = _plan(lwt=-9.0, indoor=[indoor] * 5)
    p.temp_outdoor_c = [outdoor] * 4
    got = set(_lp_offsets(p))
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
    assert lwt_coast.coast_target(p2, 0, 22.5)["offset"] == -2


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


@pytest.mark.parametrize("mode,expected", [("setback", -2), ("lp_raw", -8), ("lp", -2)])
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


def _bucket(kwh, lwt, n=8, dt=900.0, t0=1_000_000.0):
    return {"kwh": kwh, "samples": [(t0 + k * dt, lwt) for k in range(n)]}


def test_k_estimate_per_bucket_integrates_telemetry():
    from src.analytics.lwt_learning import estimate_k_kw_per_c

    # 8 samples x 15 min at LWT 33 (=15 K lift): 2 h x 15 K x k = kWh -> k = 1/15
    # (the last sample reuses the previous gap, so the integral covers 8 x 0.25 h)
    kwh = 15.0 * 2.0 * (1.0 / 15.0)
    k, n = estimate_k_kw_per_c([_bucket(kwh, 33.0) for _ in range(3)])
    assert n == 3 and abs(k - 1.0 / 15.0) < 1e-3


def test_k_estimate_requires_samples_and_hot_water():
    from src.analytics.lwt_learning import estimate_k_kw_per_c

    assert estimate_k_kw_per_c([_bucket(1.0, 33.0, n=2)]) == (None, 0)        # < 3 samples
    assert estimate_k_kw_per_c([_bucket(1.0, 19.5)]) == (None, 0)              # lwt <= 20
    assert estimate_k_kw_per_c([_bucket(0.0, 33.0)]) == (None, 0)              # bucket did not heat
    assert estimate_k_kw_per_c([{"kwh": None, "samples": []}]) == (None, 0)


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


# ── #839 review: H1 backstop <-> replan oscillation ─────────────────────────


def _real_now():
    return datetime.now(UTC).replace(second=0, microsecond=0)


@pytest.fixture()
def osc(monkeypatch, tmpdb):
    """Real-clock backstop fixture (the hold is evaluated against wall time)."""
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_ENABLED", True)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_TICKS", 2)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_MARGIN_C", 0.5)
    monkeypatch.setattr(config, "LWT_COMFORT_BACKSTOP_HOLD_MINUTES", 90, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 4)   # the real default
    lwt_coast.reset_backstop()
    now = _real_now()
    plan_date = now.date().isoformat()
    rid = db.upsert_action(
        plan_date=plan_date, start_time=(now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_time=(now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        device="daikin", action_type="lwt_preheat", params={"lwt_offset": -2, "lp_optimizer": True},
        status="active",
    )
    calls = []
    monkeypatch.setattr("src.daikin_bulletproof.apply_scheduled_daikin_params",
                        lambda dev, client, params, trigger, **kw: calls.append(kw) or True)
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: None)
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: {"temp_c": 10.0})
    return now, plan_date, rid, calls


def _fire(now, plan_date):
    for _ in range(2):
        out = lwt_coast.backstop_tick(now_utc=now, plan_date=plan_date, dev=MagicMock(),
                                      client=MagicMock(), in_peak=False, replan_fn=None)
    return out


def _neg_rows(plan_date):
    return [a for a in db.get_actions_for_plan_date(plan_date, device="daikin")
            if a["action_type"] == "lwt_preheat" and (a.get("params") or {}).get("lwt_offset", 0) < 0
            and a["status"] == "pending"]


def test_backstop_then_replan_emits_no_negative_inside_hold(monkeypatch, osc):
    now, plan_date, rid, calls = osc
    out = _fire(now, plan_date)
    assert out["fired"] and calls and calls[0].get("skip_if_matches") is False
    hold = lwt_coast.get_hold_until()
    assert hold is not None and abs((hold - now).total_seconds() - 90 * 60) < 5
    assert space_heating_gate_state()["backstop_hold_until"] is not None
    # replan: the LP still wants to coast across the whole horizon (live sensor now fresh/warm)
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    t0 = now.replace(minute=(now.minute // 30) * 30)
    plan = _plan(n=12, start=t0, lwt=-8.0)
    assert _write_lwt_preheat_actions(plan_date, plan, []) >= 1
    rows = _neg_rows(plan_date)
    assert rows, "negative coast must return after the hold"
    for r in rows:
        st = datetime.fromisoformat(r["start_time"].replace("Z", "+00:00"))
        assert st >= hold - timedelta(minutes=30), (st, hold)
        assert st + timedelta(minutes=30) > hold - timedelta(minutes=30)
    first = min(datetime.fromisoformat(r["start_time"].replace("Z", "+00:00")) for r in rows)
    assert first >= hold - timedelta(minutes=29)   # slot grid: first slot starting at/after the hold
    # once the hold has expired the coast comes straight back at the first slot
    db.set_kv("lwt_backstop_hold_until", (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    db.clear_actions_in_range("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z", device="daikin")
    assert _write_lwt_preheat_actions(plan_date, plan, []) >= 1
    first2 = min(datetime.fromisoformat(r["start_time"].replace("Z", "+00:00")) for r in _neg_rows(plan_date))
    assert first2 == t0
    assert space_heating_gate_state()["backstop_hold_until"] is None


def test_hold_survives_restart_via_kv(osc):
    now, plan_date, *_ = osc
    _fire(now, plan_date)
    lwt_coast.reset_backstop()          # simulated restart: process state gone, kv remains
    assert lwt_coast.active_hold_until() is not None


def test_hold_applies_to_tier_source_too(monkeypatch, osc):
    from src.scheduler.lp_dispatch import _tier_offsets

    now, plan_date, *_ = osc
    _fire(now, plan_date)
    t0 = now.replace(minute=(now.minute // 30) * 30)
    plan = _plan(n=12, start=t0, bands=["peak"] * 12)
    offs = _tier_offsets(plan, [], None)
    hold = lwt_coast.get_hold_until()
    for st, o in zip(plan.slot_starts_utc, offs):
        if st < hold:
            assert not o or o >= 0
    assert any(o and o < 0 for st, o in zip(plan.slot_starts_utc, offs) if st >= hold)


def test_dedupe_key_is_hold_start_not_row_start(monkeypatch, osc):
    now, plan_date, rid, _ = osc
    seen = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: seen.append(extra["warning_key"]))
    _fire(now, plan_date)
    assert seen == [f"lwt_backstop_{now.astimezone(ZoneInfo('Europe/London')):%Y-%m-%d_%H%M}"]


def test_live_cold_guard_zeroes_negative_on_near_now_slot(monkeypatch, tmpdb):
    now = _real_now()
    plan = _plan(n=6, start=now.replace(minute=(now.minute // 30) * 30), lwt=-8.0)
    guards = {}
    cold = _lp_offsets(plan, 10.0, now_utc=now, guards=guards)     # far under any floor
    assert cold[0] == 0 and guards.get("live_cold_guard", 0) >= 1
    assert cold[-1] == -2                                           # far-future slot: untouched
    warm = _lp_offsets(plan, 22.0, now_utc=now)
    assert warm[0] == -2
    assert _lp_offsets(plan, None, now_utc=now)[0] == -2            # no reading -> no guard


def test_live_cold_guard_diff_telemetry(monkeypatch, tmpdb):
    now = _real_now()
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: {"temp_c": 10.0})
    plan = _plan(n=6, start=now.replace(minute=(now.minute // 30) * 30), lwt=-8.0)
    _write_lwt_preheat_actions(now.date().isoformat(), plan, [])
    log = db.get_action_logs(device="daikin", action="lwt_source_diff")[0]
    params = log["params"] if isinstance(log["params"], dict) else json.loads(log["params"])
    assert params["guards"]["live_cold_guard"] >= 1


# ── M1 / L6 backstop write semantics ─────────────────────────────────────────


def test_backstop_apply_not_written_keeps_row_and_counter(monkeypatch, osc):
    now, plan_date, rid, calls = osc
    monkeypatch.setattr("src.daikin_bulletproof.apply_scheduled_daikin_params",
                        lambda dev, client, params, trigger, **kw: False)
    notified = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: notified.append(1))
    out = _fire(now, plan_date)
    assert not out["fired"] and lwt_coast._backstop_ticks >= 2          # still armed
    assert db.get_action_by_id(rid)["status"] == "active"
    assert notified == [] and lwt_coast.get_hold_until() is None
    logs = db.get_action_logs(device="daikin", action="lwt_comfort_backstop")
    assert logs and logs[0]["result"] == "skipped"


def test_stale_reading_holds_the_counter(monkeypatch, osc):
    now, plan_date, rid, calls = osc
    lwt_coast.backstop_tick(now_utc=now, plan_date=plan_date, dev=MagicMock(), client=MagicMock(), in_peak=False)
    assert lwt_coast._backstop_ticks == 1
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    lwt_coast.backstop_tick(now_utc=now, plan_date=plan_date, dev=MagicMock(), client=MagicMock(), in_peak=False)
    assert lwt_coast._backstop_ticks == 1                               # held, not reset
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: {"temp_c": 10.0})
    assert lwt_coast.backstop_tick(now_utc=now, plan_date=plan_date, dev=MagicMock(),
                                   client=MagicMock(), in_peak=False)["fired"]


# ── H2 smoothing ─────────────────────────────────────────────────────────────


def test_smoothing_bounds_spread_and_keeps_depth():
    from src.scheduler.lp_dispatch import smooth_lp_offsets

    out = smooth_lp_offsets([-3, -4, -5, -6, -7], 4)
    assert len(set(out)) > 1 or out == [0] * 5    # never ONE mean block (-5 x5)
    assert out != [-5] * 5
    # heating -3 (cheap band) next to coast -7: never averaged, each keeps its value
    seq = [-3] * 4 + [-7] * 4
    heating = [True] * 4 + [False] * 4
    assert smooth_lp_offsets(seq, 4, heating=heating, bands=["cheap"] * 4 + ["peak"] * 4) == seq
    # same values, same flag, but a price-band change still ends the heating block
    assert smooth_lp_offsets([2, 2, 2, 2, 3, 3, 3, 3], 4, heating=[True] * 8,
                             bands=["cheap"] * 4 + ["standard"] * 4) == [2, 2, 2, 2, 3, 3, 3, 3]


def test_coast_run_keeps_depth_with_real_min_block(monkeypatch):
    """Forecast-driven coast depth (-3 early, -7 late) survives the real
    MIN_BLOCK=4 filter instead of collapsing to a flat mean."""
    from src.scheduler.lp_dispatch import _pairs_from_offsets, _smoothed_offsets

    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 4)
    plan = _plan(n=10, space=0.0)
    seq = [-3] * 4 + [-5] * 1 + [-7] * 5
    sm = _smoothed_offsets(seq, "lp", plan)
    assert sm[:4] == [-3] * 4 and sm[5:] == [-7] * 5
    assert sm[4] in (-3, -7)                      # the 1-slot -5 merged into a neighbour, not dropped
    pairs = _pairs_from_offsets(plan, seq, source="lp")
    assert sorted({a["params"]["lwt_offset"] for _r, a in pairs}) == [-7, -3]
    # a coast run shorter than MIN_BLOCK is still dropped
    assert _smoothed_offsets([-3, -3, -4], "lp", _plan(n=3)) == [0, 0, 0]


# ── M5 absolute ceiling ──────────────────────────────────────────────────────


def test_abs_max_ceiling_blocks_lift_on_heating_path(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_LWT_ABS_MAX_C", 45.0, raising=False)
    monkeypatch.setattr("src.physics.get_lwt_base_c", lambda t: 45.0 if t <= -5 else 36.0)
    p = _plan(lwt=4.0, space=0.4)
    p.temp_outdoor_c = [-5.0] * 4
    assert set(_lp_offsets(p)) == {0}                 # curve already 45: no lift at all
    p.temp_outdoor_c = [5.0] * 4
    assert set(_lp_offsets(p)) == {4}                 # 36 + 4 = 40 < 45
    p.lwt_offset_c = [5.0] * 4
    monkeypatch.setattr("src.physics.get_lwt_base_c", lambda t: 42.0)
    assert set(_lp_offsets(p)) == {3}                 # capped at 45 - 42


def test_lp_optimizer_ceiling_mirrors_abs_max(monkeypatch):
    from src.physics import get_daikin_heating_kw, get_lwt_base_c

    monkeypatch.setattr(config, "DAIKIN_LWT_ABS_MAX_C", 30.0, raising=False)
    t = 0.0
    base = get_lwt_base_c(t)
    lift = max(0, math.floor(30.0 - base + 0.5))
    assert lift < 5   # the cap actually bites for the default curve at 0 C
    # the dispatch rule and the plan's ceiling use the same arithmetic
    assert get_daikin_heating_kw(t, lwt_offset_delta=lift) <= get_daikin_heating_kw(t, lwt_offset_delta=5)


def test_heating_clamp_reaches_ten_when_configured(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", 10.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_ABS_MAX_C", 60.0, raising=False)
    monkeypatch.setattr("src.physics.get_lwt_base_c", _curve)
    p = _plan(lwt=14.0, space=0.4)
    assert set(_lp_offsets(p)) == {10}
    p.lwt_offset_c = [-14.0] * 4
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -10.0, raising=False)
    assert set(_lp_offsets(p)) == {-10}


# ── L10 misc ─────────────────────────────────────────────────────────────────


def test_coast_target_reads_the_configured_weather_curve(monkeypatch):
    p = _plan(indoor=[21.0] * 5)
    p.temp_outdoor_c = [5.0] * 4
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -10.0, raising=False)
    a = lwt_coast.coast_target(p, 0)
    monkeypatch.setattr(config, "DAIKIN_WEATHER_CURVE_HIGH_LWT_C", config.DAIKIN_WEATHER_CURVE_HIGH_LWT_C + 6)
    monkeypatch.setattr(config, "DAIKIN_WEATHER_CURVE_LOW_LWT_C", config.DAIKIN_WEATHER_CURVE_LOW_LWT_C + 6)
    b = lwt_coast.coast_target(p, 0)
    assert b["curve_lwt_c"] == a["curve_lwt_c"] + 6
    assert b["offset"] < a["offset"] or a["offset"] == -10


# ── M2 / L4 / L5 learning log ────────────────────────────────────────────────


def test_run_id_is_stamped_with_the_producing_run(monkeypatch, tmpdb):
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    t0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    # a PREVIOUS run already in optimizer_log must not be what the rows point at
    prev = db.log_optimizer_run({"run_at": "2026-11-04T09:00:00+00:00"})
    plan = _plan(n=4, start=t0, lwt=-4.0)
    _write_lwt_preheat_actions("2026-11-04", plan, [])
    rows = db.get_lwt_learning_rows("2026-11-04T10:00:00Z", "2026-11-04T12:00:00Z")
    assert all(r["run_id"] is None for r in rows)       # not the previous run's id
    new = db.log_optimizer_run({"run_at": "2026-11-04T10:01:00+00:00"})
    assert new != prev
    assert lwt_coast.stamp_run_id(plan, new) == 4
    rows = db.get_lwt_learning_rows("2026-11-04T10:00:00Z", "2026-11-04T12:00:00Z")
    assert {r["run_id"] for r in rows} == {new}
    # a later plan overwrites the rows and gets ITS run id
    plan2 = _plan(n=4, start=t0, lwt=-6.0)
    _write_lwt_preheat_actions("2026-11-04", plan2, [])
    new2 = db.log_optimizer_run({"run_at": "2026-11-04T10:31:00+00:00"})
    lwt_coast.stamp_run_id(plan2, new2)
    assert {r["run_id"] for r in db.get_lwt_learning_rows("2026-11-04T10:00:00Z", "2026-11-04T12:00:00Z")} == {new2}


def test_optimizer_stamps_run_id_after_logging():
    import inspect

    from src.scheduler import optimizer
    src = inspect.getsource(optimizer)
    assert src.index("run_id = db.log_optimizer_run(") < src.index("stamp_run_id(plan, run_id)")


def test_plan_updated_at_moves_but_written_at_stays(monkeypatch, tmpdb):
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    t0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    _write_lwt_preheat_actions("2026-11-04", _plan(n=4, start=t0, lwt=-4.0), [])
    r1 = db.get_lwt_learning_rows("2026-11-04T10:00:00Z", "2026-11-04T10:30:00Z")[0]
    _write_lwt_preheat_actions("2026-11-04", _plan(n=4, start=t0, lwt=-6.0), [])
    r2 = db.get_lwt_learning_rows("2026-11-04T10:00:00Z", "2026-11-04T10:30:00Z")[0]
    assert r1["plan_updated_at_utc"] and r2["plan_updated_at_utc"] > r1["plan_updated_at_utc"]
    assert r2["written_at_utc"] == r1["written_at_utc"]


def test_offset_written_is_null_for_quota_dropped_slots(monkeypatch, tmpdb):
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 2)
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_COAST_MODE", "lp_raw")
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 32)   # reserve 30 -> 1 pair
    t0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    plan = _plan(n=8, start=t0, lwt=-4.0)
    plan.lwt_offset_c = [-4.0, -4.0, 0.0, 0.0, -3.0, -3.0, -3.0, -3.0]
    plan.space_electric_kwh = [0.0, 0.0, 0.4, 0.4, 0.0, 0.0, 0.0, 0.0]
    _write_lwt_preheat_actions("2026-11-04", plan, [])
    rows = db.get_lwt_learning_rows("2026-11-04T10:00:00Z", "2026-11-04T14:00:00Z")
    got = [r["offset_written"] for r in rows]
    assert got[:2] == [-4.0, -4.0]                       # kept pair
    assert got[4:] == [None, None, None, None]           # second window dropped by the cap


# ── M3 UA ────────────────────────────────────────────────────────────────────


def _coast_rows(n, hk, off=-8, start=datetime(2026, 11, 3, 23, 0, tzinfo=UTC), t0=20.0, drop=0.1):
    return [{"slot_time_utc": (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "indoor_real_c": t0 - drop * i, "outdoor_real_c": 5.0, "device_offset": off,
             "heating_kwh": hk} for i in range(n)]


def test_coast_requires_no_measured_heating():
    from src.analytics.lwt_learning import estimate_ua_w_per_k

    ua, n = estimate_ua_w_per_k(_coast_rows(8, 0.0), 16.5, tz=TZ)
    assert n == 8 and ua and ua > 0
    # negative offset but the pump drew heat -> NOT a coast slot
    assert estimate_ua_w_per_k(_coast_rows(8, 0.3), 16.5, tz=TZ) == (None, 0)
    # unmeasured heating (None) with a negative offset still counts
    assert estimate_ua_w_per_k(_coast_rows(8, None), 16.5, tz=TZ)[1] == 8


def test_night_ua_excludes_daytime_coasts():
    from src.analytics.lwt_learning import estimate_ua_w_per_k

    day = _coast_rows(8, 0.0, start=datetime(2026, 11, 3, 11, 0, tzinfo=UTC))
    assert estimate_ua_w_per_k(day, 16.5, tz=TZ)[0] is not None
    assert estimate_ua_w_per_k(day, 16.5, night_only=True, tz=TZ) == (None, 0)
    night = _coast_rows(8, 0.0, start=datetime(2026, 11, 3, 23, 0, tzinfo=UTC))
    assert estimate_ua_w_per_k(night, 16.5, night_only=True, tz=TZ)[1] == 8


def test_daily_payload_carries_night_ua(monkeypatch, tmpdb):
    from src.analytics import lwt_learning

    day = date(2026, 11, 3)
    _seed_synthetic_day(day, 200.0, 16.5)
    monkeypatch.setattr("src.analytics.thermal_learning.get_building_thermal_mass_kwh_per_k", lambda: 16.5)
    row = lwt_learning.run_for_day(day, TZ)
    assert row["ua_est_night_w_per_k"] is not None and abs(row["ua_est_night_w_per_k"] - 200.0) < 25
    assert db.get_lwt_learning_daily(2)[0]["payload"]["ua_est_night_w_per_k"] == row["ua_est_night_w_per_k"]


def test_realised_fill_covers_the_last_local_slot_of_a_bst_day(monkeypatch, tmpdb):
    from src.analytics import lwt_learning

    day = date(2026, 7, 10)                               # BST: local 23:30 = 22:30Z
    slot = datetime(2026, 7, 10, 22, 30, tzinfo=UTC)
    with db._lock:
        c = db.get_connection()
        c.execute("INSERT INTO room_temperature_history (captured_at, room, temp_c) VALUES (?,?,?)",
                  ("2026-07-10T22:40:00Z", "lounge", 21.3))
        c.execute("INSERT INTO daikin_telemetry (fetched_at, source, outdoor_temp_c, lwt_actual_c) VALUES (?,?,?,?)",
                  (slot.timestamp() + 600, "live", 12.0, 26.0))
        c.execute("INSERT OR REPLACE INTO daikin_consumption_2hourly (date,bucket_idx,kwh_total,kwh_heating,kwh_dhw,source,fetched_at) "
                  "VALUES (?,?,?,?,?,?,?)", (day.isoformat(), 11, 0.4, 0.4, 0.0, "t", "x"))
        c.commit()
        c.close()
    lwt_learning.fill_realised(day, TZ)
    r = db.get_lwt_learning_rows("2026-07-10T22:30:00Z", "2026-07-10T23:00:00Z")[0]
    assert r["indoor_real_c"] == 21.3 and r["outdoor_real_c"] == 12.0 and r["lwt_actual_c"] == 26.0
    assert abs(r["heating_kwh"] - 0.1) < 1e-6              # 0.4 kWh over the bucket's 4 slots
    # and the slot after local midnight belongs to the NEXT day, untouched
    assert db.get_lwt_learning_rows("2026-07-10T23:00:00Z", "2026-07-10T23:30:00Z") == []
