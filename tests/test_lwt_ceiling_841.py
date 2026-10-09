"""#841 — bank heat in cheap bands: C consistent with UA, comfort ceiling,
boost guard vs the ceiling, telemetry."""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.analytics import thermal_learning as tl
from src.config import config
from src.runtime_settings import SettingValidationError, set_setting
from src.scheduler.lp_dispatch import (
    _lp_offsets,
    _tier_offsets,
    _write_lwt_preheat_actions,
    w3_trajectory_plausible,
)
from src.scheduler.lp_optimizer import LpInitialState, LpPlan, solve_lp
from src.weather import WeatherLpSeries

TZ = ZoneInfo("Europe/London")
COSY_CHEAP, COSY_DAY, COSY_PEAK = 12.4868, 25.4461, 38.174


@pytest.fixture
def tmpdb(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        yield


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_BOOST_C", 3)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", -2)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_COMFORT_BAND_C", 0.5)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -10.0)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", 5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0)
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 21.0)
    monkeypatch.setattr(config, "LP_W3_CEILING_C", 23.0)
    monkeypatch.setattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 1)
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5, raising=False)
    monkeypatch.setattr(config, "LP_W3_IMPLAUSIBLE_BELOW_FLOOR_C", 2.0, raising=False)
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "tier")


def _plan(n=8, *, lwt=None, space=None, indoor=None, bands=None, start=None):
    t0 = start or datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    p = LpPlan(
        ok=True, status="Optimal", objective_pence=0.0,
        slot_starts_utc=[t0 + timedelta(minutes=30 * i) for i in range(n)],
        price_pence=[COSY_DAY] * n, temp_outdoor_c=[5.0] * n,
        cheap_threshold_pence=18.97, peak_threshold_pence=31.81,
        tariff_structure_kind="banded", price_band=bands or ["cheap"] * n,
    )
    p.lwt_offset_c = list(lwt) if lwt is not None else [4.0] * n
    p.space_electric_kwh = list(space) if space is not None else [0.3] * n
    if indoor is not None:
        p.indoor_temp_c = list(indoor)
    return p


# ── C consistent with UA ─────────────────────────────────────────────────────


def _cal(monkeypatch, **row):
    base = {"tau_hours": 82.7, "c_kwh_per_k": 49.59, "c_source": "tau_x_env_ua",
            "ua_w_per_k": None}
    base.update(row)
    monkeypatch.setattr(tl, "_calibration_row", lambda: dict(base))
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 200.0, raising=False)


def test_c_stale_basis_is_recomputed(monkeypatch):
    _cal(monkeypatch, c_ua_basis_w_per_k=600.0)
    assert tl.get_building_thermal_mass_kwh_per_k() == pytest.approx(82.7 * 200 / 1000, abs=1e-6)
    res = tl.thermal_mass_resolution()
    assert res["c_recomputed"] is True and res["c_basis_ua_w_per_k"] == 600.0


def test_c_unknown_basis_is_recomputed(monkeypatch):
    _cal(monkeypatch, c_ua_basis_w_per_k=None)
    assert tl.get_building_thermal_mass_kwh_per_k() == pytest.approx(16.54, abs=1e-6)
    assert tl.thermal_mass_resolution()["c_recomputed"] is True


def test_c_measured_source_is_kept(monkeypatch):
    _cal(monkeypatch, c_kwh_per_k=14.0, c_source="heat_capacity_fit", c_ua_basis_w_per_k=None)
    res = tl.thermal_mass_resolution()
    assert res["c_kwh_per_k"] == 14.0 and res["c_recomputed"] is False
    assert tl.get_building_thermal_mass_kwh_per_k() == 14.0


def test_c_out_of_bounds_stored_is_recomputed_with_reason(monkeypatch):
    _cal(monkeypatch, c_kwh_per_k=90.0, c_ua_basis_w_per_k=200.0)
    res = tl.thermal_mass_resolution()
    assert res["c_recomputed"] is True and res["c_reason"] == "stored_c_out_of_bounds"
    assert res["c_kwh_per_k"] == pytest.approx(82.7 * 200 / 1000, abs=1e-6)


def test_refresh_uses_bounded_ua_for_c(monkeypatch, tmpdb):
    """An out-of-bounds learned UA (HDD fit 2000 W/K) must not stamp C = tau x 2000."""
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 200.0, raising=False)
    monkeypatch.setattr(db, "get_indoor_readings_range", lambda s, e: [
        {"captured_at": "2026-11-04T03:00:00Z", "room": "a", "temp_c": 20.0}])
    monkeypatch.setattr(tl, "_outdoor_series", lambda a, b: [])
    monkeypatch.setattr(tl, "select_decay_episodes", lambda *a, **k: [])
    monkeypatch.setattr(tl, "fit_tau", lambda *a, **k: {
        "status": "ok", "tau_hours": 60.0, "r2_median": 0.9, "episodes": 5})
    monkeypatch.setattr(tl, "_ua_fit_from_db", lambda *a, **k: {
        "status": "ok", "ua_w_per_k": 2000.0, "r2": 0.9, "samples": 30, "assumed_cop": 3.0})
    assert tl.refresh_building_thermal_calibration()["status"] == "ok"
    row = db.get_building_thermal_calibration()
    assert row["c_ua_basis_w_per_k"] == 200.0
    assert row["c_kwh_per_k"] == pytest.approx(12.0)
    assert row["c_source"] == "tau_x_env_ua"


def test_c_matching_basis_returns_stored(monkeypatch):
    _cal(monkeypatch, c_kwh_per_k=16.54, c_ua_basis_w_per_k=200.0)
    assert tl.get_building_thermal_mass_kwh_per_k() == pytest.approx(16.54)
    assert tl.thermal_mass_resolution()["c_recomputed"] is False


def test_calibration_row_stores_ua_basis(tmpdb):
    db.upsert_building_thermal_calibration({
        "tau_hours": 80.0, "c_kwh_per_k": 16.0, "c_source": "tau_x_env_ua",
        "c_ua_basis_w_per_k": 200.0,
    })
    row = db.get_building_thermal_calibration()
    assert row["c_ua_basis_w_per_k"] == 200.0


def test_migration_adds_nullable_basis_column(monkeypatch):
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE building_thermal_calibration (
            id INTEGER PRIMARY KEY CHECK (id = 1), tau_hours REAL, c_kwh_per_k REAL,
            c_source TEXT, computed_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO building_thermal_calibration VALUES (1, 82.7, 49.59, 'tau_x_env_ua', 'x')")
        conn.commit()
        conn.close()
        monkeypatch.setattr("src.config.config.DB_PATH", path)
        db.init_db()
        row = db.get_building_thermal_calibration()
        assert "c_ua_basis_w_per_k" in row and row["c_ua_basis_w_per_k"] is None


# ── the ceiling in the LP ────────────────────────────────────────────────────


def _day_inputs():
    d = date(2026, 11, 4)
    starts, prices = [], []
    for h in range(24):
        for m in (0, 30):
            starts.append(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).astimezone(UTC))
            if 4 <= h < 7 or 13 <= h < 16 or h >= 22:
                prices.append(COSY_CHEAP)
            elif 16 <= h < 19:
                prices.append(COSY_PEAK)
            else:
                prices.append(COSY_DAY)
    return starts, prices


def _solve(monkeypatch, *, ceiling, indoor0=21.0):
    monkeypatch.setattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 25)
    monkeypatch.setattr(config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LP_W3_PEAK_COAST_DELTA_C", 0.0, raising=False)
    monkeypatch.setattr(config, "THERMAL_LEARNED_VALUES_ENABLED", False, raising=False)
    # Small thermal mass + leaky house + strong pump so banking in the cheap
    # bands actually reaches the ceiling (prod constants never do: ~0.5 K/band).
    # The per-slot recovery cap, radiator cap and lift cap are lifted so they
    # do not bind before the ceiling does.
    monkeypatch.setattr(config, "DAIKIN_LWT_ABS_MAX_C", 60.0, raising=False)
    monkeypatch.setattr(config, "RADIATOR_MAX_KW", 20.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 6.0, raising=False)
    monkeypatch.setattr(config, "LP_W3_MAX_RECOVERY_C_PER_SLOT", 4.0, raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 300.0, raising=False)
    monkeypatch.setattr(config, "BUILDING_THERMAL_MASS_KWH_PER_K", 4.0, raising=False)
    monkeypatch.setattr("src.physics.get_kw_per_degc_lwt", lambda: 0.15)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(config, "LP_W3_CEILING_C", ceiling)
    monkeypatch.setattr(config, "LP_W3_COMFORT_PEN_PENCE_PER_DEGC_SLOT", 300.0, raising=False)
    starts, prices = _day_inputs()
    n = len(starts)
    w = WeatherLpSeries(
        slot_starts_utc=starts, temperature_outdoor_c=[6.0] * n, shortwave_radiation_wm2=[50.0] * n,
        cloud_cover_pct=[80.0] * n, pv_kwh_per_slot=[0.05] * n, cop_space=[3.0] * n, cop_dhw=[2.6] * n,
    )
    plan = solve_lp(
        slot_starts_utc=starts, price_pence=prices, base_load_kwh=[0.4] * n, weather=w,
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=48.0, indoor_temp_c=indoor0), tz=TZ,
    )
    assert plan.ok, plan.status
    return plan


def test_ceiling_binds_and_stops_banking(monkeypatch):
    free = _solve(monkeypatch, ceiling=28.0)
    tight = _solve(monkeypatch, ceiling=22.0)
    assert tight.w3_ceiling_c == pytest.approx(22.0)
    assert free.w3_ceiling_c == pytest.approx(28.0)
    # without the ceiling the cheap bands bank well above 22.5; with it, never above 22.05
    assert max(free.indoor_temp_c) > 22.5   # ~24.8 at these constants
    assert max(tight.indoor_temp_c) <= 22.05
    assert sum(tight.comfort_slack_hi_c) == pytest.approx(0.0, abs=1e-6)
    # heating stops once the house reaches the ceiling: no cheap-band heat while at/over 21.95
    for i in range(len(tight.space_electric_kwh)):
        if tight.indoor_temp_c[i + 1] >= 21.95 and tight.indoor_temp_c[i] >= 21.95:
            # holding at the ceiling only needs the leak replaced, never a lift beyond it
            assert tight.indoor_temp_c[i + 1] <= tight.indoor_temp_c[i] + 0.06
    assert sum(tight.space_electric_kwh) < sum(free.space_electric_kwh)


def test_start_above_ceiling_reports_overshoot_and_buys_no_heat(monkeypatch):
    """House starts ABOVE the ceiling: the soft bound is violated only where it
    physically must be (reported as ceiling slack) and no heat is bought."""
    tight = _solve(monkeypatch, ceiling=22.0, indoor0=23.5)
    assert tight.comfort_slack_hi_c[0] > 0.2
    assert all(s <= 1e-6 for s in tight.comfort_slack_hi_c[12:])  # settled under it
    assert sum(tight.space_electric_kwh[:4]) <= 1e-6
    # floor slack (model-health signal) is NOT polluted by the ceiling overshoot
    assert sum(tight.comfort_slack_c) == pytest.approx(0.0, abs=1e-6)


def test_start_above_ceiling_does_not_trip_plausibility_gate():
    plan = _plan(n=48, lwt=[0.0] * 48, space=[0.0] * 48, indoor=[23.5] * 49)
    plan.w3_ceiling_c = 23.0
    plan.comfort_slack_c = [0.0] * 48
    plan.comfort_slack_hi_c = [0.5] * 48
    assert w3_trajectory_plausible(plan) == (True, "ok")


def test_heating_above_ceiling_trips_plausibility_gate():
    plan = _plan(n=48, lwt=[4.0] * 48, space=[0.3] * 48, indoor=[23.5] * 49)
    plan.w3_ceiling_c = 23.0
    plan.comfort_slack_c = [0.0] * 48
    plan.comfort_slack_hi_c = [0.5] * 6 + [0.0] * 42
    ok, reason = w3_trajectory_plausible(plan)
    assert not ok and reason.startswith("comfort_slack:6_slots")


def test_floor_slack_still_trips_plausibility_gate():
    plan = _plan(lwt=[0.0] * 8, indoor=[21.0] * 9)
    plan.w3_ceiling_c = 23.0
    plan.comfort_slack_c = [0.0, 0.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.0]
    ok, reason = w3_trajectory_plausible(plan)
    assert not ok and reason.startswith("comfort_slack:5_slots")


def test_effective_ceiling_helper(monkeypatch):
    from src.scheduler.lwt_coast import effective_w3_ceiling_c
    monkeypatch.setattr(config, "LP_W3_CEILING_C", 23.0)
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 21.0)
    assert effective_w3_ceiling_c() == 23.0
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 22.8)
    assert effective_w3_ceiling_c() == pytest.approx(23.3)


def test_plausibility_uses_ceiling_not_setpoint(monkeypatch):
    plan = _plan(lwt=[4.0] * 8, indoor=[21.0, 21.8, 22.4, 22.9, 22.9, 22.5, 22.0, 21.5, 21.0])
    plan.w3_setpoint_c = 21.0
    plan.w3_ceiling_c = 23.0
    assert w3_trajectory_plausible(plan) == (True, "ok")
    plan.indoor_temp_c = [21.0] * 8 + [27.5]
    ok, reason = w3_trajectory_plausible(plan)
    assert not ok and reason.startswith("trajectory_implausible_max")


# ── boost guards vs the ceiling ──────────────────────────────────────────────


def test_tier_boost_kept_below_guard_zeroed_above():
    plan = _plan(bands=["cheap"] * 8)
    assert _tier_offsets(plan, [], 22.0) == [3] * 8     # house at 22.0, setpoint 21 → bank heat
    assert _tier_offsets(plan, [], 22.6) == [0] * 8     # >= ceiling 23 - band 0.5
    assert _tier_offsets(plan, [], None) == [3] * 8


def test_lp_boost_kept_below_guard_zeroed_above_near_now():
    now = datetime(2026, 11, 4, 0, 10, tzinfo=UTC)
    plan = _plan(lwt=[4.0] * 8, indoor=[21.0] * 9, bands=["cheap"] * 8)
    assert _lp_offsets(plan, 22.0, now_utc=now) == [4] * 8
    assert _lp_offsets(plan, 22.6, now_utc=now)[:2] == [0, 0]
    assert _lp_offsets(plan, 22.6, now_utc=now)[2:] == [4] * 6   # far slots not guarded by live reading


def test_guard_follows_runtime_ceiling(monkeypatch):
    monkeypatch.setattr(config, "LP_W3_CEILING_C", 22.0)
    plan = _plan(n=4, bands=["cheap"] * 4)
    assert _tier_offsets(plan, [], 21.4) == [3] * 4
    assert _tier_offsets(plan, [], 21.6) == [0] * 4


def test_wire_cheap_boost_at_22c_reaches_apply(monkeypatch, tmpdb):
    import src.state_machine as sm
    from src.daikin.models import DaikinDevice

    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.scheduler.lp_dispatch._space_heating_demand_present", lambda: True)
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 150)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()
    calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: calls.append(params) or True,
    )
    # house at 22.0 (setpoint 21, ceiling 23): the guard must NOT cancel the boost
    monkeypatch.setattr(
        db, "get_latest_indoor_reading",
        lambda max_age_minutes=30: {"temp_c": 22.0, "rooms_c": {"a": 22.0}, "aggregate": "mean"},
    )
    now_utc = datetime.now(UTC).replace(second=0, microsecond=0)
    start = now_utc - timedelta(minutes=10)
    plan = _plan(n=4, lwt=[4.0] * 4, indoor=[21.0] * 5, bands=["cheap"] * 4, start=start)
    plan_date = now_utc.date().isoformat()
    assert _write_lwt_preheat_actions(plan_date, plan, []) >= 1
    rows = db.get_actions_for_plan_date(plan_date, device="daikin")

    def _p(r):
        return r["params"] if isinstance(r["params"], dict) else json.loads(r["params"])

    assert any(_p(r).get("lwt_offset", 0) > 0 for r in rows if r["action_type"] == "lwt_preheat")
    dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)
    sm._reconcile_daikin_actions(rows, MagicMock(), dev, now_utc, trigger="test")
    assert calls and calls[-1].get("lwt_offset", 0) > 0


# ── runtime setting validator ────────────────────────────────────────────────


def test_ceiling_setting_validator(tmpdb):
    assert set_setting("LP_W3_CEILING_C", 23.5, actor="test") == 23.5
    with pytest.raises(SettingValidationError):
        set_setting("LP_W3_CEILING_C", 21.2, actor="test")   # < setpoint 21 + 0.5
    with pytest.raises(SettingValidationError):
        set_setting("LP_W3_CEILING_C", 35.0, actor="test")   # > max


def test_setpoint_setting_validator_symmetric(tmpdb):
    assert set_setting("LP_W3_CEILING_C", 23.5, actor="test") == 23.5
    with pytest.raises(SettingValidationError):
        set_setting("INDOOR_SETPOINT_C", 23.2, actor="test")  # > ceiling 23.5 - 0.5
    assert set_setting("INDOOR_SETPOINT_C", 22.5, actor="test") == 22.5


# ── learning log + telemetry ─────────────────────────────────────────────────


def test_learning_log_records_ceiling(tmpdb):
    from src.scheduler import lwt_coast

    plan = _plan(n=4, lwt=[4.0] * 4, indoor=[21.0] * 5)
    plan.w3_ceiling_c = 23.0
    plan.w3_night_floor_c, plan.w3_setpoint_c, plan.w3_peak_coast_delta_c = 17.5, 21.0, 0.0
    assert lwt_coast.record_planned(plan, source_used="tier", coast_mode="setback",
                                    written_offsets=[3] * 4) == 4
    rows = db.get_lwt_learning_rows("2026-11-04T00:00:00Z", "2026-11-05T00:00:00Z")
    assert rows and all(r["ceiling_c"] == 23.0 for r in rows)


def test_thermal_calibration_endpoint_has_basis(monkeypatch, tmpdb):
    import asyncio

    from src.api.routers import sensors

    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 200.0, raising=False)
    monkeypatch.setattr(config, "THERMAL_LEARNED_VALUES_ENABLED", True, raising=False)
    db.upsert_building_thermal_calibration({
        "tau_hours": 82.7, "c_kwh_per_k": 49.59, "c_source": "tau_x_env_ua",
        "c_ua_basis_w_per_k": 600.0,
    })
    eff = asyncio.run(sensors.get_thermal_calibration())["effective"]
    assert eff["c_recomputed"] is True and eff["c_basis_ua_w_per_k"] == 600.0
    assert eff["c_kwh_per_k"] == pytest.approx(16.54, abs=0.01)


def test_heating_by_band_and_indoor_minmax(monkeypatch, tmpdb):
    from src.analytics import cosy_scorecard as cs
    from src.analytics.load_expected import BandWindow

    day = date(2026, 11, 4)
    a = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    b = a + timedelta(days=1)
    cheap = BandWindow("band_cheap", "cheap", a, a + timedelta(hours=4), 12.5,
                       {(h, m) for h in range(0, 4) for m in (0, 30)})
    peak = BandWindow("band_peak", "peak", a + timedelta(hours=16), a + timedelta(hours=19), 38.0,
                      {(h, m) for h in range(16, 19) for m in (0, 30)})
    monkeypatch.setattr(db, "get_daikin_consumption_2hourly_range",
                        lambda s, e: [{"date": s, "bucket_idx": 0, "kwh_heating": 2.0},
                                      {"date": s, "bucket_idx": 8, "kwh_heating": 1.0},
                                      {"date": s, "bucket_idx": 5, "kwh_heating": 0.5}])
    out = cs._heating_kwh_by_band(day, [cheap, peak], TZ, a, b)
    # bucket0 (00-02 local = UTC in Nov) -> cheap 2.0; bucket 8 (16-18) -> peak 1.0; bucket 5 -> standard 0.5
    assert out == {"cheap": 2.0, "standard": 0.5, "peak": 1.0}
    monkeypatch.setattr(db, "get_indoor_readings_range", lambda s, e: [
        {"captured_at": "2026-11-04T03:00:00Z", "room": "a", "temp_c": 20.0},
        {"captured_at": "2026-11-04T03:00:00Z", "room": "b", "temp_c": 22.0},
        {"captured_at": "2026-11-04T14:00:00Z", "room": "a", "temp_c": 22.5},
        {"captured_at": "2026-11-04T14:00:00Z", "room": "b", "temp_c": 23.5},
    ])
    assert cs._indoor_min_max(day, TZ, a, b) == (21.0, 23.0)


def test_indoor_minmax_carries_slow_room_forward(monkeypatch):
    """Room b reports every 30 min, room a every 15: buckets without b must use
    b's last reading, not aggregate a alone."""
    from src.analytics import cosy_scorecard as cs

    day = date(2026, 11, 4)
    a = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    b = a + timedelta(days=1)
    monkeypatch.setattr(db, "get_indoor_readings_range", lambda s, e: [
        {"captured_at": "2026-11-04T10:00:00Z", "room": "a", "temp_c": 20.0},
        {"captured_at": "2026-11-04T10:00:00Z", "room": "b", "temp_c": 22.0},   # mean 21
        {"captured_at": "2026-11-04T10:15:00Z", "room": "a", "temp_c": 24.0},   # b carried: mean 23
        {"captured_at": "2026-11-04T10:30:00Z", "room": "a", "temp_c": 20.0},
        {"captured_at": "2026-11-04T10:30:00Z", "room": "b", "temp_c": 22.0},
    ])
    assert cs._indoor_min_max(day, TZ, a, b) == (21.0, 23.0)
