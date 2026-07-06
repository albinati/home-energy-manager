"""W3 (#540) — LP indoor-temperature state + soft comfort + gentle recovery.

Pins the safety contract: flag-off is a no-op (byte-identical plan), flag-on
solves Optimal, seeds t_in from the sensor, respects the soft comfort floor via
slack (never Infeasible), and honours the gentle-recovery cap.
"""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src import db
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

TZ = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _active_mode(monkeypatch):
    monkeypatch.setattr(app_config, "DB_PATH", tempfile.mktemp(suffix=".db"), raising=False)
    db.init_db()
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1, raising=False)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0, raising=False)


def _inputs(n=24, outdoor=4.0, indoor=19.0, indoor_seed=True):
    base = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[outdoor] * n,
        shortwave_radiation_wm2=[40.0] * n, cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.0] * n, cop_space=[3.2] * n, cop_dhw=[2.5] * n,
    )
    st = LpInitialState(soc_kwh=6.0, tank_temp_c=45.0,
                        indoor_temp_c=indoor if indoor_seed else None)
    return slots, w, st


def _solve(slots, w, st, prices=None):
    n = len(slots)
    return solve_lp(
        slot_starts_utc=slots, price_pence=prices or [20.0] * n,
        base_load_kwh=[0.3] * n, weather=w, initial=st, tz=TZ,
    )


def test_flag_off_is_noop(monkeypatch):
    """W3 off → no indoor state on the plan, and it still solves."""
    monkeypatch.setattr(app_config, "LP_W3_TIN_ENABLED", False, raising=False)
    slots, w, st = _inputs()
    plan = _solve(slots, w, st)
    assert plan.ok, plan.status
    assert plan.indoor_temp_c == []          # no t_in when off


def test_flag_on_solves_and_carries_indoor(monkeypatch):
    monkeypatch.setattr(app_config, "LP_W3_TIN_ENABLED", True, raising=False)
    slots, w, st = _inputs(n=24, indoor=19.0)
    plan = _solve(slots, w, st)
    assert plan.ok, plan.status
    assert len(plan.indoor_temp_c) == len(slots) + 1   # N+1 states
    assert abs(plan.indoor_temp_c[0] - 19.0) < 1e-6    # seeded from the sensor


def test_no_sensor_seed_keeps_w3_off(monkeypatch):
    """Flag on but no fresh sensor → W3 stays off (no indoor state)."""
    monkeypatch.setattr(app_config, "LP_W3_TIN_ENABLED", True, raising=False)
    slots, w, st = _inputs(indoor_seed=False)
    plan = _solve(slots, w, st)
    assert plan.ok, plan.status
    assert plan.indoor_temp_c == []


def test_soft_floor_never_infeasible_on_cold_deep_night(monkeypatch):
    """A very cold start well below the floor must NOT be Infeasible — the
    comfort floor is slack-penalised, so the LP solves and surfaces the deficit
    as a dipped indoor rather than no plan (the Phase-B hard-floor bug)."""
    monkeypatch.setattr(app_config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "BUILDING_UA_W_PER_K", 900.0, raising=False)  # leaky → unheatable
    slots, w, st = _inputs(n=24, outdoor=-5.0, indoor=12.0)
    plan = _solve(slots, w, st, prices=[60.0] * len(slots))  # expensive heat
    assert plan.ok, f"cold-night must stay feasible via slack, got {plan.status}"
    assert len(plan.indoor_temp_c) == len(slots) + 1


def test_warm_weather_stays_feasible(monkeypatch):
    """Passive conductive GAIN (outdoor warmer than indoor) can rise the house
    faster than the recovery cap. The cap must bound only the HEATING rise, not
    the net delta — else it conflicts with the RC equality and goes Infeasible
    (adversarial-review regression). Cool house on a hot day, leaky + low-mass."""
    monkeypatch.setattr(app_config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "BUILDING_UA_W_PER_K", 1400.0, raising=False)
    monkeypatch.setattr(app_config, "BUILDING_THERMAL_MASS_KWH_PER_K", 6.0, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_MAX_RECOVERY_C_PER_SLOT", 0.3, raising=False)
    slots, w, st = _inputs(n=24, outdoor=30.0, indoor=18.0)  # heatwave, cool house
    plan = _solve(slots, w, st)
    assert plan.ok, f"warm slot with passive gain must stay feasible, got {plan.status}"
    assert len(plan.indoor_temp_c) == len(slots) + 1


def test_gentle_recovery_cap(monkeypatch):
    """The modelled indoor rise per slot never exceeds the recovery cap."""
    monkeypatch.setattr(app_config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_MAX_RECOVERY_C_PER_SLOT", 0.4, raising=False)
    monkeypatch.setattr(app_config, "BUILDING_UA_W_PER_K", 120.0, raising=False)  # headroom to rise
    slots, w, st = _inputs(n=24, outdoor=8.0, indoor=17.0)
    plan = _solve(slots, w, st, prices=[8.0] * len(slots))  # cheap → wants to heat up
    assert plan.ok, plan.status
    rises = [plan.indoor_temp_c[i + 1] - plan.indoor_temp_c[i] for i in range(len(slots))]
    assert max(rises) <= 0.4 + 1e-6, f"recovery cap breached: {max(rises):.3f}"
