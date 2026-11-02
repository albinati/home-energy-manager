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
    # absolute rise capped at +5 C
    r = roll_tank_forward(42.0, 600, prev_tank_c=20.0, sample_gap_s=1200, tank_on=True,
                          target_c=60.0, **TANK)
    assert r.tank_c == pytest.approx(47.0)


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
    monkeypatch.setattr(lp_initial_state, "get_cached_realtime_age_seconds", lambda rt=None: 60.0)
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
    config.LP_INITIAL_SOC_ROLL_FORWARD_ENABLED = True
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    _tele(1800, 38.0)
    _tele(600, 42.0)   # +4 C / 20 min = 0.2 C/min ; reading 10 min old
    st = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    # SoC: 5.0 kWh + 3 kW * (20 min + 1 min reading age)
    assert st.soc_kwh == pytest.approx(5.0 + 3.0 * 21 / 60)
    # tank: reading 10 min old -> 30 min to slot0 = +6 C, capped at +5 C
    assert st.tank_temp_c == pytest.approx(47.0)
    assert st.tank_measured_c == 42.0 and st.soc_measured_kwh == 5.0
    assert st.roll_forward["soc"]["applied"] and st.roll_forward["tank"]["mode"] == "reheat"


def test_kill_switch_off_is_identical_to_legacy(env):
    _tele(1800, 38.0)
    _tele(600, 42.0)
    config.LP_INITIAL_SOC_ROLL_FORWARD_ENABLED = False
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = False
    off = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    legacy = lp_initial_state.read_lp_initial_state()
    assert off.soc_kwh == legacy.soc_kwh == 5.0
    assert off.tank_temp_c == legacy.tank_temp_c == 42.0
    assert off.roll_forward is None
    assert off.soc_measured_kwh == 5.0 and off.tank_measured_c == 42.0
    # enabled but no slot0 given (other callers) is legacy too
    config.LP_INITIAL_SOC_ROLL_FORWARD_ENABLED = True
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    assert lp_initial_state.read_lp_initial_state().soc_kwh == 5.0


def test_stale_battery_power_falls_back(env):
    config.LP_INITIAL_SOC_ROLL_FORWARD_ENABLED = True
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    env.setattr(lp_initial_state, "get_cached_realtime_age_seconds", lambda rt=None: 900.0)
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
def _setup_wire(monkeypatch):
    from test_lp_appliance_real_solver import TARIFF, _seed_realistic_day
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
    monkeypatch.setattr(lp_initial_state, "get_cached_realtime_age_seconds", lambda rt=None: 0.0)
    monkeypatch.setattr(lp_initial_state, "get_cached_realtime",
                        lambda *a, **k: RealTimeData(soc=40.0, battery_power=3.0))
    monkeypatch.setattr(lp_initial_state.daikin_service, "get_lp_state_cached_or_estimated",
                        lambda actor=None: {"tank_temp_c": 45.0, "source": "live"})
    _seed_realistic_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))
    return now


def test_wire_solve_lp_receives_rolled_forward_soc(monkeypatch):
    from test_lp_appliance_real_solver import _stub_optimal_lp_plan
    _setup_wire(monkeypatch)
    seen: dict = {}

    def _capture(*, initial, **kwargs):
        seen.setdefault("initial", initial)
        return _stub_optimal_lp_plan(**kwargs)

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _capture)
    config.LP_INITIAL_SOC_ROLL_FORWARD_ENABLED = True
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    optimizer.run_optimizer(fox=None, daikin=None)
    # 4.0 kWh + 3 kW * 25 min = +1.25
    assert seen["initial"].soc_kwh == pytest.approx(4.0 + 3.0 * 25 / 60)

    seen.clear()
    config.LP_INITIAL_SOC_ROLL_FORWARD_ENABLED = False
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = False
    optimizer.run_optimizer(fox=None, daikin=None)
    assert seen["initial"].soc_kwh == pytest.approx(4.0)


# ---------------------------------------------------------------- review fixes (#933)
def test_tank_end_of_reheat_48_vs_setpoint_47_does_not_roll_up(env):
    """Regression: reading 48, setpoint 47, previous 41 = the ordinary end of a reheat."""
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    env.setattr(lp_initial_state.daikin_service, "get_lp_state_cached_or_estimated",
                lambda actor=None: {"tank_temp_c": 48.0, "source": "live"})
    env.setattr(lp_initial_state.daikin_service, "get_cached_devices",
                lambda **k: (_ for _ in ()).throw(RuntimeError("no cache")))
    _tele(1800, 41.0, target=47.0)
    _tele(600, 48.0, target=47.0)
    st = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    assert st.tank_temp_c == 48.0
    assert st.roll_forward["tank"]["mode"] == "none:reheat_no_headroom"


def test_tank_boost_known_uses_dhw_temp_max(env):
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    env.setattr(config, "DHW_TEMP_MAX_C", 60.0, raising=False)
    env.setattr(lp_initial_state.daikin_service, "get_lp_state_cached_or_estimated",
                lambda actor=None: {"tank_temp_c": 48.0, "source": "live"})

    class _D:
        tank_powerful = True

    class _Res:
        devices = [_D()]

    env.setattr(lp_initial_state.daikin_service, "get_cached_devices", lambda **k: _Res())
    _tele(1800, 41.0, target=47.0)
    _tele(600, 48.0, target=47.0)
    st = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    assert st.roll_forward["tank"]["mode"] == "reheat"
    assert 48.0 < st.tank_temp_c <= 53.0           # +5 C cap, ceiling 60
    # reading >= setpoint + 3 is also a known boost
    assert lp_initial_state._reheat_cap_c(51.0, 47.0, NOW, NOW) == 60.0


def test_tank_estimate_source_is_not_rolled(env):
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    env.setattr(lp_initial_state.daikin_service, "get_lp_state_cached_or_estimated",
                lambda actor=None: {"tank_temp_c": 42.0, "source": "estimate"})
    _tele(1800, 38.0)
    _tele(600, 42.0)
    st = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    assert st.tank_temp_c == 42.0
    assert st.roll_forward["tank"]["mode"] == "none:source_estimate"


def test_tank_reading_older_than_40_min_is_not_rolled(env):
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    _tele(4000, 38.0)
    _tele(2700, 42.0)   # 45 min old
    st = lp_initial_state.read_lp_initial_state(slot0_utc=SLOT0)
    assert st.tank_temp_c == 42.0
    assert st.roll_forward["tank"]["mode"] == "none:reading_too_old"


def test_tank_min_sample_gap_600s():
    r = roll_tank_forward(46.0, 600, prev_tank_c=44.0, sample_gap_s=300, tank_on=True,
                          target_c=60.0, **TANK)
    assert r.mode != "reheat"


def test_onset_exactly_at_slot0_is_ignored():
    t0 = datetime(2026, 10, 18, 11, 30, tzinfo=UTC)          # 12:30 BST
    slot0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)        # 13:00 BST
    g = [{"workMode": "ForceCharge", "startHour": 13, "startMinute": 0, "endHour": 15,
          "endMinute": 59, "extraParam": {"fdPwr": 4000, "fdSoc": 100}}]
    assert find_schedule_onset(g, t0, slot0, LON, max_power_kw=6.0) is None


def test_fdsoc_clamp_only_post_onset_and_discharge_never_raises():
    t0 = datetime(2026, 10, 18, 11, 59, 30, tzinfo=UTC)
    slot0 = t0 + timedelta(minutes=30)
    g = [{"workMode": "ForceDischarge", "startHour": 13, "startMinute": 0, "endHour": 15,
          "endMinute": 59, "extraParam": {"fdPwr": 3000, "fdSoc": 90}}]
    onset = find_schedule_onset(g, t0, slot0, LON, max_power_kw=6.0)
    r = roll_soc_with_onset(5.0, 3.0, 30, 1800, onset, **KW)
    pre = 5.0 + 3.0 * 30 / 3600
    assert r.soc_kwh <= pre + 1e-9                            # a ForceDischarge never raises SoC
    g2 = [{"workMode": "ForceCharge", "startHour": 13, "startMinute": 0, "endHour": 15,
           "endMinute": 59, "extraParam": {"fdPwr": 4000, "fdSoc": 30}}]
    onset2 = find_schedule_onset(g2, t0, slot0, LON, max_power_kw=6.0)
    r2 = roll_soc_with_onset(5.0, 3.0, 30, 1800, onset2, **KW)
    assert r2.soc_kwh >= pre - 1e-9                           # fdSoc below SoC: measured part kept


def test_inforce_fdsoc_caps_measured_roll():
    from src.scheduler.lp_roll_forward import find_inforce_group
    t0 = datetime(2026, 10, 17, 21, 33, tzinfo=UTC)           # 22:33 BST
    g = [{"workMode": "ForceCharge", "startHour": 22, "startMinute": 0, "endHour": 23,
          "endMinute": 59, "extraParam": {"fdPwr": 1900, "fdSoc": 55}}]
    inf = find_inforce_group(g, t0, LON)
    assert inf is not None and inf.target_soc_pct == 55
    r = roll_soc_with_onset(5.18, 3.0, 0, 1620, None, inforce=inf, **KW)
    assert r.soc_kwh == pytest.approx(5.5)                    # would be 6.53 uncapped
    r = roll_soc_with_onset(6.0, 3.0, 0, 1620, None, inforce=inf, **KW)
    assert r.soc_kwh == pytest.approx(6.0)                    # above ceiling: not pushed down
    assert find_inforce_group(g, datetime(2026, 10, 18, 0, 0, tzinfo=UTC), LON) is None


def test_atomic_age_requires_same_snapshot():
    import time as _t

    from src.foxess import service as fs
    rt = RealTimeData(soc=1.0)
    other = RealTimeData(soc=2.0)
    fs._last_realtime, fs._last_realtime_updated_monotonic = rt, _t.monotonic()
    assert fs.get_cached_realtime_age_seconds(rt) is not None
    assert fs.get_cached_realtime_age_seconds(other) is None


def test_roll_forward_persisted_in_exogenous_snapshot(monkeypatch):
    import json as _json
    _setup_wire(monkeypatch)
    config.LP_INITIAL_SOC_ROLL_FORWARD_ENABLED = True
    optimizer.run_optimizer(fox=None, daikin=None)
    conn = db.get_connection()
    row = conn.execute(
        "SELECT soc_initial_kwh, soc_measured_kwh, tank_measured_c, exogenous_snapshot_json "
        "FROM lp_inputs_snapshot ORDER BY run_id DESC LIMIT 1").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == pytest.approx(4.0 + 3.0 * 25 / 60) and row[1] == pytest.approx(4.0)
    assert row[2] == 45.0
    assert _json.loads(row[3])["initial_roll_forward"]["soc"]["applied"] is True


def test_wire_solve_lp_receives_rolled_forward_tank(monkeypatch):
    from test_lp_appliance_real_solver import _stub_optimal_lp_plan
    now = _setup_wire(monkeypatch)
    conn = db.get_connection()
    conn.execute("DELETE FROM daikin_telemetry")
    for age, t in ((1800, 41.0), (600, 45.0)):
        conn.execute("INSERT INTO daikin_telemetry (fetched_at, source, tank_temp_c, tank_target_c, tank_on) "
                     "VALUES (?,?,?,?,?)", (now.timestamp() - age, "live", t, 50.0, 1))
    conn.commit()
    conn.close()
    seen: dict = {}

    def _capture(*, initial, **kwargs):
        seen.setdefault("initial", initial)
        return _stub_optimal_lp_plan(**kwargs)

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _capture)
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = True
    optimizer.run_optimizer(fox=None, daikin=None)
    # 45 + 0.2 C/min * 25 min = 50 (also the setpoint and the +5 C cap)
    assert seen["initial"].tank_temp_c > 45.0
    assert seen["initial"].tank_temp_c <= 50.0
    seen.clear()
    config.LP_INITIAL_TANK_ROLL_FORWARD_ENABLED = False
    optimizer.run_optimizer(fox=None, daikin=None)
    assert seen["initial"].tank_temp_c == 45.0


def test_lp_simulation_passes_slot0(monkeypatch):
    from test_lp_appliance_real_solver import TARIFF, _seed_realistic_day

    from src.scheduler import lp_simulation
    db.init_db()
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    now = datetime(2026, 5, 20, 18, 5, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))
    captured: dict = {}

    def _fake(daikin=None, **kw):
        captured.update(kw)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(lp_simulation, "read_lp_initial_state", _fake)
    try:
        lp_simulation.run_lp_simulation(allow_daikin_refresh=False)
    except RuntimeError:
        pass
    assert captured.get("slot0_utc") == datetime(2026, 5, 20, 18, 30, tzinfo=UTC)
