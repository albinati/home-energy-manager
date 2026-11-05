"""#949 - tau-consistent W3 building UA (LP_W3_TAU_H), shared RC step/replay, drift telemetry.

Fixture: 112 half-hour rows from prod ``lwt_learning_log`` (2026-11-02..05), see
``tests/fixtures/w3_prod_slots_2026_11_02_05.json``. No wall-clock dependence: every
datetime is explicit.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import db, physics
from src.analytics import lwt_learning as ll
from src.config import config as app_config
from src.config import cop_at_temperature
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

TZ = ZoneInfo("Europe/London")
C_KWH = 16.531
UA_PIN = 200.0
TAU_MEAS = 53.6
FIX = Path(__file__).parent / "fixtures" / "w3_prod_slots_2026_11_02_05.json"

PROD_ENV = {
    "DAIKIN_CONTROL_MODE": "active", "LP_HP_MIN_ON_SLOTS": 1, "LP_INVERTER_STRESS_COST_PENCE": 0.0,
    "LP_W3_TIN_ENABLED": True, "DAIKIN_LWT_SOURCE": "lp", "DAIKIN_HOLIDAY_DETECT_ENABLED": True,
    "BUILDING_UA_W_PER_K": UA_PIN, "BUILDING_THERMAL_MASS_KWH_PER_K": C_KWH,
    "BUILDING_UA_LEARNED_AUTO_APPLY": False,
    "LP_W3_SPACE_FLOOR_ENABLED": True, "LP_W3_SPACE_FLOOR_KW_PER_K": 0.0515,
    "LP_W3_SPACE_FLOOR_FACTOR": 1.25, "LP_COP_LWT_OFFSET_PENALTY_PER_K": 0.025,
    "LP_W3_INTERNAL_GAIN_KW": 0.15, "INDOOR_SETPOINT_C": 21.0, "LP_W3_NIGHT_FLOOR_C": 20.0,
    "DAIKIN_WEATHER_CURVE_LOW_C": 0.0, "DAIKIN_WEATHER_CURVE_LOW_LWT_C": 40.0,
    "DAIKIN_WEATHER_CURVE_HIGH_C": 18.0, "DAIKIN_WEATHER_CURVE_HIGH_LWT_C": 18.0, "DAIKIN_WEATHER_CURVE_OFFSET_C": 0.0,
    "OPTIMIZATION_LWT_OFFSET_MIN": -2.0,  # prod knob (code default -10 lets the LP plan -5 nights)
    "LP_W3_PEAK_COAST_DELTA_C": 0.0, "LP_W3_EMITTER_TAU_MIN": 0.0, "LP_W3_TAU_H": 0.0,
    "BATTERY_CAPACITY_KWH": 10.0, "LP_SOC_FINAL_KWH": 2.5,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()
    for k, v in PROD_ENV.items():
        monkeypatch.setattr(app_config, k, v, raising=False)
    physics._w3_tau_warned.clear()


def _rows() -> list[dict]:
    return json.loads(FIX.read_text())["rows"]


def _z(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _window(rows, start: str, end: str):
    a, b = _z(start), _z(end)
    return [r for r in rows if a <= _z(r["slot_time_utc"]) <= b]


def _replay(win, tau_h: float | None) -> tuple[float, float]:
    """(replayed T at the window end, measured T at the window end) from the measured start.

    Thermal kWh from the EXECUTED offset via ``w3_q_th_kwh_for_offset`` with the CURVE COP at the
    measured outdoor temp (the logged ``cop_space`` is the LP's already lift-derated COP at the
    planned offset: using it would double-count the penalty and echo LP output)."""
    ua = UA_PIN if not tau_h else physics.w3_ua_from_tau(C_KWH, tau_h)
    body = win[:-1]
    t_out = [r["outdoor_real_c"] if r["outdoor_real_c"] is not None else r["outdoor_fc_c"] for r in body]
    q = [physics.w3_q_th_kwh_for_offset(r["device_offset"], t, max(1.0, cop_at_temperature(app_config.DAIKIN_COP_CURVE, t)))
         for r, t in zip(body, t_out)]
    traj = physics.w3_rc_replay(win[0]["indoor_real_c"], t_out, q, [r["gain_kw"] for r in body], ua, C_KWH)
    return traj[-1], win[-1]["indoor_real_c"]


COAST = [("2026-11-02T22:00:00Z", "2026-11-03T04:00:00Z"), ("2026-11-04T20:00:00Z", "2026-11-05T00:00:00Z")]
LIFT = [("2026-11-03T02:00:00Z", "2026-11-03T06:00:00Z"), ("2026-11-04T04:00:00Z", "2026-11-04T08:00:00Z")]


@pytest.mark.parametrize("start,end", COAST)
def test_coast_replay_matches_only_with_tau_consistent_ua(start, end):
    win = _window(_rows(), start, end)
    assert len(win) >= 8
    rep_tau, meas = _replay(win, TAU_MEAS)
    rep_pin, _ = _replay(win, None)
    d_tau, d_pin = rep_tau - meas, rep_pin - meas
    msg = f"{start}: drift tau53.6={d_tau:+.3f} K, pin UA200={d_pin:+.3f} K (replay - measured)"
    print(msg)
    assert abs(d_tau) <= 0.15, msg
    assert d_pin > 0.2, msg  # the bug: UA 200 predicts a warm house


@pytest.mark.parametrize("start,end", LIFT)
def test_lift_windows_do_not_regress(start, end):
    win = _window(_rows(), start, end)
    assert len(win) >= 8
    rep_tau, meas = _replay(win, TAU_MEAS)
    rep_pin, _ = _replay(win, None)
    e_tau, e_pin = abs(rep_tau - meas), abs(rep_pin - meas)
    print(f"{start}: lift drift tau53.6={rep_tau - meas:+.3f} pin={rep_pin - meas:+.3f}")
    assert e_tau <= max(e_pin, 0.25), (e_tau, e_pin)


def test_replay_and_lp_share_one_step():
    import pulp

    v = pulp.LpVariable("t")
    expr = physics.w3_rc_step(v, 9.0, 1.0e6, 2.0e5, 300.0, 5.9e7, 1800.0)
    assert isinstance(expr, pulp.LpAffineExpression)  # still linear in t_in
    t = 20.5
    num = physics.w3_rc_step(t, 9.0, 1.0e6, 2.0e5, 300.0, 5.9e7, 1800.0)
    rep = physics.w3_rc_replay(t, [9.0], [1.0e6 / 3.6e6], [2.0e5 / (0.5 * 3.6e6)], 300.0, 5.9e7 / 3.6e6)
    assert rep[1] == pytest.approx(num, abs=1e-12)


def _w3_slots(n=48):
    base = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    return [base + timedelta(minutes=30 * i) for i in range(n)]


def _prices(slots):
    out = []
    for s in slots:
        h = s.hour + s.minute / 60.0
        if 16 <= h < 19:
            out.append(42.98)
        elif 4 <= h < 7 or 13 <= h < 16 or h >= 22:
            out.append(14.05)
        else:
            out.append(28.66)
    return out


def _solve(outdoor=9.5, indoor=21.0, n=48):
    slots = _w3_slots(n)
    w = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[outdoor] * n,
        shortwave_radiation_wm2=[0.0] * n, cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.0] * n, cop_space=[3.8] * n, cop_dhw=[2.5] * n,
    )
    st = LpInitialState(soc_kwh=6.0, tank_temp_c=45.0, indoor_temp_c=indoor)
    return solve_lp(slot_starts_utc=slots, price_pence=_prices(slots), base_load_kwh=[0.3] * n,
                    weather=w, initial=st, tz=TZ)


GOLDEN = Path(__file__).parent / "fixtures" / "w3_golden_pre949_1278998.json"


def test_tau_zero_matches_pre_pr_golden(monkeypatch):
    """Golden = objective / t_in / offsets produced by the PRE-PR optimizer at commit 1278998
    (same harness, 24 slots, prod constants, 3 cases incl. the no-gain branch). t_in and offsets
    are compared exactly (they are stored rounded to 6 dp by the LP); the objective to 1e-8
    relative (it differed from the pre-PR run by <= 6e-7 absolute when regenerated)."""
    golden = json.loads(GOLDEN.read_text())
    assert len(golden) == 3
    for g in golden:
        monkeypatch.setattr(app_config, "LP_W3_INTERNAL_GAIN_KW", g["gain"], raising=False)
        monkeypatch.setattr(app_config, "LP_W3_TAU_H", 0.0, raising=False)
        p = _solve(outdoor=g["outdoor"], indoor=g["indoor"], n=24)
        assert p.ok
        assert p.objective_pence == pytest.approx(g["objective"], rel=1e-8), g["outdoor"]
        assert list(p.indoor_temp_c) == g["t_in"], g["outdoor"]
        assert list(p.lwt_offset_c) == g["offsets"], g["outdoor"]
        assert p.w3_ua_source == "pin" and p.w3_ua_w_per_k == pytest.approx(UA_PIN)


def test_bounds_and_ua_on_plan(monkeypatch, caplog):
    monkeypatch.setattr(app_config, "LP_W3_TAU_H", 10.0, raising=False)
    with caplog.at_level(logging.WARNING, logger="src.physics"):
        assert physics.effective_w3_tau_h() == 0.0
        assert physics.effective_w3_tau_h() == 0.0
    warns = [r for r in caplog.records if "LP_W3_TAU_H" in r.getMessage()]
    assert len(warns) == 1  # warns once
    p0 = _solve(n=24)
    assert p0.w3_ua_source == "pin" and p0.w3_ua_w_per_k == pytest.approx(UA_PIN)

    monkeypatch.setattr(app_config, "LP_W3_TAU_H", TAU_MEAS, raising=False)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="src.scheduler.lp_optimizer"):
        p = _solve(n=24)
    assert p.w3_ua_source == "tau"
    assert p.w3_ua_w_per_k == pytest.approx(1000.0 * C_KWH / TAU_MEAS, rel=1e-6)
    assert p.w3_ua_w_per_k == pytest.approx(308.4, abs=0.1)
    assert p.w3_tau_h == pytest.approx(TAU_MEAS) and p.w3_c_kwh_per_k == pytest.approx(C_KWH, rel=1e-6)
    assert any("W3 RC: UA=" in r.getMessage() and "source=tau" in r.getMessage() for r in caplog.records)


def test_settings_schema_range(monkeypatch):
    from src import runtime_settings as rs

    spec = rs.SCHEMA["LP_W3_TAU_H"]
    assert (spec.min_value, spec.max_value) == (0.0, 150.0)
    assert spec.env_default() == 0.0


def _smoke(monkeypatch, tau):
    monkeypatch.setattr(app_config, "LP_W3_TAU_H", tau, raising=False)
    p = _solve(outdoor=9.5, n=48)
    assert p.ok, p.status
    slots = p.slot_starts_utc
    night = [i for i, s in enumerate(slots) if s.hour >= 22 or s.hour < 7]
    # setpoint-holding hours; 20:00-22:00 is the planned pre-night coast into the 22:00 cheap band
    # (night floor 20 starts at 22:00)
    day = [i for i, s in enumerate(slots) if 7 <= s.hour < 20]
    mean_night = sum(p.lwt_offset_c[i] for i in night) / len(night)
    t_day_min = min(p.indoor_temp_c[i + 1] for i in day)
    print(f"tau={tau}: night mean offset {mean_night:+.2f}, day min t_in {t_day_min:.2f}")
    return p, mean_night, t_day_min


def test_solve_smoke_pin_ua_reproduces_all_night_coast(monkeypatch):
    p, mean_night, _ = _smoke(monkeypatch, 0.0)
    night = [p.lwt_offset_c[i] for i, s in enumerate(p.slot_starts_utc) if s.hour >= 22 or s.hour < 7]
    assert mean_night == pytest.approx(-2.0, abs=0.05) and max(night) <= -1.99  # the bug


@pytest.mark.parametrize("tau", [TAU_MEAS, 55.0])
def test_solve_smoke_no_all_night_coast(monkeypatch, tau):
    p, mean_night, t_day_min = _smoke(monkeypatch, tau)
    assert mean_night >= 0.0
    assert t_day_min >= 20.9
    from src.scheduler.lp_dispatch import w3_gate_snapshot

    snap = w3_gate_snapshot(p)
    assert set(snap) >= {"ok", "reason"}  # the gate verdict is recorded


def test_replay_telemetry_payload(monkeypatch):
    monkeypatch.setattr(app_config, "LP_W3_TAU_H", TAU_MEAS, raising=False)
    rows = _rows()
    out = ll.replay_telemetry(rows, C_KWH, UA_PIN)
    assert out["replay_tau_ref_source"] == "config" and out["replay_tau_ref_h"] == TAU_MEAS
    assert 20.0 <= out["tau_fit_h"] <= 150.0
    bands = out["replay_drift_by_band"]
    assert set(bands) == {"le_m1", "0_to_2", "3_to_5", "gt_5"}
    coast = bands["le_m1"]
    assert coast["n_slots"] > 0 and coast["n_windows_8h"] >= 1
    # the CONFIGURED tau (out of sample) drifts less on the coast windows than the pinned UA
    assert abs(coast["ua_tau"]["mean_8h_err_k"]) < abs(coast["ua_pin"]["mean_8h_err_k"])
    print("tau_fit_h", out["tau_fit_h"], "coast", coast)


def test_replay_telemetry_knob_off_marks_in_sample(monkeypatch):
    monkeypatch.setattr(app_config, "LP_W3_TAU_H", 0.0, raising=False)
    out = ll.replay_telemetry(_rows(), C_KWH, UA_PIN)
    assert out["replay_tau_ref_source"] == "fit_in_sample" and out["replay_tau_ref_h"] == out["tau_fit_h"]


def test_replay_bands_are_half_open_and_total():
    for off in (-5.0, -1.0, -0.999, -0.5, 0.0, 2.0, 2.0001, 2.4999, 2.5, 3.0, 5.0, 5.4999, 5.5, 5.5001, 9.0):
        ll._replay_band(off)  # never falls through by accident
    assert [ll._replay_band(x) for x in (-1.0, -0.4, 0.0, 2.0, 2.001, 3.0, 5.0, 5.001, 6.0)] == [
        "le_m1", "0_to_2", "0_to_2", "0_to_2", "0_to_2", "3_to_5", "3_to_5", "3_to_5", "gt_5"]
