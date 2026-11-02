"""#929 - LP initial SoC / tank rolled forward to slot 0."""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from src import db
from src.config import config
from src.foxess.models import RealTimeData
from src.scheduler import lp_initial_state, optimizer
from src.scheduler.lp_roll_forward import (
    find_schedule_onset,
    roll_soc_forward,
    roll_soc_with_onset,
    roll_tank_forward,
)

KW = dict(capacity_kwh=10.0, reserve_kwh=1.5, max_power_kw=6.0)
TANK = dict(ua_w_per_k=3.25, ambient_c=21.0, litres=192.0, cp_j_per_kg_k=4186.0)
LON = ZoneInfo("Europe/London")


# --- pure SoC roll ----------------------------------------------------------
def test_soc_charging_idle_discharging():
    # 3 kW for 20 min = +1.0 kWh ; discharge symmetric ; idle unchanged
    assert roll_soc_forward(5.0, 3.0, 60, 1200, **KW).soc_kwh == pytest.approx(6.0)
    assert roll_soc_forward(5.0, -3.0, 60, 1200, **KW).soc_kwh == pytest.approx(4.0)
    r = roll_soc_forward(5.0, 0.0, 60, 1200, **KW)
    assert r.soc_kwh == 5.0 and r.applied


def test_soc_clamps():
    assert roll_soc_forward(9.5, 6.0, 0, 1800, **KW).soc_kwh == 10.0       # capacity
    assert roll_soc_forward(2.0, -6.0, 0, 1800, **KW).soc_kwh == 1.5       # reserve
    # inverter limit: 20 kW reading is clamped to 6 kW -> 0.5 h * 6 = 3 kWh
    assert roll_soc_forward(2.0, 20.0, 0, 1800, **KW).soc_kwh == pytest.approx(5.0)
    # already below reserve: never rolled further down
    assert roll_soc_forward(1.0, -3.0, 0, 1800, **KW).soc_kwh == 1.0


def test_soc_stale_or_missing_power_falls_back():
    for power, age in ((None, 10), (3.0, None), (3.0, 301), (float("nan"), 5)):
        r = roll_soc_forward(5.0, power, age, 1200, **KW)
        assert r.soc_kwh == 5.0 and not r.applied
    assert not roll_soc_forward(5.0, 3.0, 5, 0, **KW).applied


def test_soc_schedule_onset_uses_group_power_after_start():
    t0 = datetime(2026, 10, 18, 11, 59, 30, tzinfo=UTC)         # reading 12:59:30 BST
    slot0 = t0 + timedelta(minutes=30)
    groups = [{"workMode": "ForceCharge", "startHour": 13, "startMinute": 0,
               "endHour": 15, "endMinute": 59, "extraParam": {"fdPwr": 4000, "fdSoc": 100}}]
    onset = find_schedule_onset(groups, t0, slot0, LON, max_power_kw=6.0)
    assert onset is not None and onset.offset_s == pytest.approx(30.0)
    assert onset.power_kw == 4.0
    # measured -2 kW (discharging) for 30 s, then +4 kW for the rest
    r = roll_soc_with_onset(5.0, -2.0, 30, 1800, onset, **KW)
    exp = 5.0 - 2.0 * 30 / 3600 + 4.0 * 1770 / 3600
    assert r.soc_kwh == pytest.approx(exp) and r.reason == "ok+schedule_onset"
    # fdSoc ceiling honoured (52 % of 10 kWh = 5.2)
    groups[0]["extraParam"]["fdSoc"] = 52
    onset = find_schedule_onset(groups, t0, slot0, LON, max_power_kw=6.0)
    assert roll_soc_with_onset(5.0, -2.0, 30, 1800, onset, **KW).soc_kwh == pytest.approx(5.2)


def test_schedule_onset_ignores_groups_outside_window_and_other_modes():
    t0 = datetime(2026, 10, 18, 11, 59, 30, tzinfo=UTC)
    slot0 = t0 + timedelta(minutes=30)
    g = [{"workMode": "SelfUse", "startHour": 13, "startMinute": 0, "endHour": 13, "endMinute": 59,
          "extraParam": {"fdPwr": 4000}},
         {"workMode": "ForceCharge", "startHour": 18, "startMinute": 0, "endHour": 19, "endMinute": 0,
          "extraParam": {"fdPwr": 4000}},
         {"workMode": "ForceCharge", "startHour": 12, "startMinute": 0, "endHour": 12, "endMinute": 59,
          "extraParam": {"fdPwr": 4000}}]   # already started before the reading
    assert find_schedule_onset(g, t0, slot0, LON, max_power_kw=6.0) is None


# --- pure tank roll ---------------------------------------------------------
def test_tank_reheat_extrapolates_rate_capped_at_target():
    # +4 C in 20 min = 0.2 C/min ; 10 min gap -> +2 C
    r = roll_tank_forward(42.0, 600, prev_tank_c=38.0, sample_gap_s=1200, tank_on=True,
                          target_c=60.0, **TANK)
    assert r.mode == "reheat" and r.tank_c == pytest.approx(44.0)
    # capped at target
    r = roll_tank_forward(42.0, 1800, prev_tank_c=38.0, sample_gap_s=1200, tank_on=True,
                          target_c=45.0, **TANK)
    assert r.tank_c == 45.0
    # rate capped at physics max (0.6 C/min)
    r = roll_tank_forward(42.0, 600, prev_tank_c=20.0, sample_gap_s=1200, tank_on=True,
                          target_c=60.0, **TANK)
    assert r.tank_c == pytest.approx(42.0 + 6.0)


def test_tank_standing_loss_vs_noise():
    # hot-ish tank, short gap: loss below the 0.5 C sensor-resolution threshold: unchanged
    r = roll_tank_forward(45.0, 3600, prev_tank_c=45.0, sample_gap_s=1800, tank_on=True,
                          target_c=45.0, **TANK)
    assert r.tank_c == 45.0 and not r.applied
    # very hot tank, long gap: loss exceeds the threshold and is applied
    r = roll_tank_forward(70.0, 2700, prev_tank_c=70.0, sample_gap_s=1800, tank_on=None,
                          target_c=70.0, **TANK)
    assert r.mode == "standing_loss" and r.tank_c < 70.0


def test_tank_missing_data_falls_back():
    r = roll_tank_forward(45.0, 0, prev_tank_c=None, sample_gap_s=None, tank_on=None,
                          target_c=None, **TANK)
    assert r.tank_c == 45.0 and not r.applied
    # rising but no ceiling info -> not extrapolated
    r = roll_tank_forward(45.0, 600, prev_tank_c=40.0, sample_gap_s=1200, tank_on=True,
                          target_c=None, **TANK)
    assert r.tank_c == 45.0
    # an off tank is not extrapolated as reheating
    r = roll_tank_forward(45.0, 600, prev_tank_c=40.0, sample_gap_s=1200, tank_on=False,
                          target_c=60.0, **TANK)
    assert r.mode != "reheat"


# --- read_lp_initial_state integration --------------------------------------
NOW = datetime(2026, 11, 2, 10, 40, tzinfo=UTC)
SLOT0 = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)


@pytest.fixture
def env(monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.0, raising=False)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 10.0, raising=False)
    monkeypatch.setattr(config, "MAX_INVERTER_KW", 6.0, raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    monkeypatch.setattr(lp_initial_state, "_now_utc", lambda: NOW)
    monkeypatch.setattr(lp_initial_state, "get_cached_realtime_age_seconds", lambda: 60.0)
    monkeypatch.setattr(
        lp_initial_state, "get_cached_realtime",
        lambda *a, **k: RealTimeData(soc=50.0, battery_power=3.0),
    )
    monkeypatch.setattr(
        lp_initial_state.daikin_service, "get_lp_state_cached_or_estimated",
        lambda actor=None: {"tank_temp_c": 42.0, "source": "live"},
    )
    conn = db.get_connection()
    conn.execute("DELETE FROM daikin_telemetry")
    conn.commit()
    conn.close()
    return monkeypatch


def _tele(age_s, tank, target=60.0, on=1):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO daikin_telemetry (fetched_at, source, tank_temp_c, tank_target_c, tank_on) "
        "VALUES (?,?,?,?,?)", (NOW.timestamp() - age_s, "live", tank, target, on))
    conn.commit()
    conn.close()


def test_initial_state_rolls_soc_and_tank(env):
    config.LP_INITIAL_STATE_ROLL_FORWARD_ENABLED = True
    _tele(1800, 38.0)
    _tele(600, 42.0)   # +4 C / 20 min = 0.2 C/min ; reading 10 min old
    st = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    # SoC: 5.0 kWh + 3 kW * (20 min + 1 min reading age)
    assert st.soc_kwh == pytest.approx(5.0 + 3.0 * 21 / 60)
    # tank: reading 10 min old -> 30 min to slot0 = +6 C
    assert st.tank_temp_c == pytest.approx(48.0)
    assert st.roll_forward["soc"]["applied"] and st.roll_forward["tank"]["mode"] == "reheat"


def test_kill_switch_off_is_identical_to_legacy(env):
    _tele(1800, 38.0)
    _tele(600, 42.0)
    config.LP_INITIAL_STATE_ROLL_FORWARD_ENABLED = False
    off = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    legacy = lp_initial_state.read_lp_initial_state()
    assert off.soc_kwh == legacy.soc_kwh == 5.0
    assert off.tank_temp_c == legacy.tank_temp_c == 42.0
    assert off.roll_forward is None
    # enabled but no slot0 given (other callers) is legacy too
    config.LP_INITIAL_STATE_ROLL_FORWARD_ENABLED = True
    assert lp_initial_state.read_lp_initial_state().soc_kwh == 5.0


def test_stale_battery_power_falls_back(env):
    config.LP_INITIAL_STATE_ROLL_FORWARD_ENABLED = True
    env.setattr(lp_initial_state, "get_cached_realtime_age_seconds", lambda: 900.0)
    st = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    assert st.soc_kwh == 5.0
    assert st.roll_forward["soc"]["reason"] == "stale_power"


def test_missing_indoor_records_w3_disabled_reason(env, caplog):
    env.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    with caplog.at_level("WARNING"):
        st = lp_initial_state.read_lp_initial_state()
    assert st.indoor_temp_c is None
    assert st.w3_disabled_reason == "no_fresh_indoor_reading"
    assert any("W3" in r.message for r in caplog.records)


# --- wire: the optimizer hands solve_lp the rolled-forward soc ---------------
def test_wire_solve_lp_receives_rolled_forward_soc(monkeypatch):
    from test_lp_appliance_real_solver import (
        TARIFF,
        _seed_realistic_day,
        _stub_optimal_lp_plan,
    )
    db.init_db()
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.0, raising=False)
    monkeypatch.setattr(config, "MAX_INVERTER_KW", 6.0, raising=False)
    now = datetime(2026, 5, 20, 18, 5, tzinfo=UTC)          # slot 0 = 18:30
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    monkeypatch.setattr(lp_initial_state, "_now_utc", lambda: now)
    monkeypatch.setattr(lp_initial_state, "get_cached_realtime_age_seconds", lambda: 0.0)
    monkeypatch.setattr(lp_initial_state, "get_cached_realtime",
                        lambda *a, **k: RealTimeData(soc=40.0, battery_power=3.0))
    monkeypatch.setattr(lp_initial_state.daikin_service, "get_lp_state_cached_or_estimated",
                        lambda actor=None: {"tank_temp_c": 45.0, "source": "live"})
    _seed_realistic_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))

    seen: dict = {}

    def _capture(*, initial, **kwargs):
        seen.setdefault("initial", initial)
        return _stub_optimal_lp_plan(**kwargs)

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _capture)
    config.LP_INITIAL_STATE_ROLL_FORWARD_ENABLED = True
    optimizer.run_optimizer(fox=None, daikin=None)
    # 4.0 kWh + 3 kW * 25 min = +1.25
    assert seen["initial"].soc_kwh == pytest.approx(4.0 + 3.0 * 25 / 60)

    seen.clear()
    config.LP_INITIAL_STATE_ROLL_FORWARD_ENABLED = False
    optimizer.run_optimizer(fox=None, daikin=None)
    assert seen["initial"].soc_kwh == pytest.approx(4.0)
