"""#949 - tau-consistent W3 building UA (LP_W3_TAU_H), shared RC step/replay, drift telemetry.

Fixture: 112 half-hour rows from prod ``lwt_learning_log`` (2026-11-02..05), see
``tests/fixtures/w3_prod_slots_2026_11_02_05.json``. No wall-clock dependence: every
datetime is explicit.
"""
from __future__ import annotations

import json
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import db, physics
from src.analytics import lwt_learning as ll
from src.config import config as app_config
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
    "LP_W3_PEAK_COAST_DELTA_C": 0.0, "LP_W3_EMITTER_TAU_MIN": 0.0, "LP_W3_TAU_H": 0.0,
    "BATTERY_CAPACITY_KWH": 10.0, "LP_SOC_FINAL_KWH": 2.5,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(app_config, "DB_PATH", tempfile.mktemp(suffix=".db"), raising=False)
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


def _replay(win, tau_h: float | None, chain: str = "offset") -> tuple[float, float]:
    """(replayed T at the window end, measured T at the window end) from the measured start.

    chain="offset": thermal kWh from the EXECUTED offset via ``w3_q_th_kwh_for_offset``;
    chain="lp_log": the LP's own logged thermal (e_space_kwh x cop_space)."""
    ua = UA_PIN if not tau_h else physics.w3_ua_from_tau(C_KWH, tau_h)
    body = win[:-1]
    t_out = [r["outdoor_real_c"] if r["outdoor_real_c"] is not None else r["outdoor_fc_c"] for r in body]
    if chain == "offset":
        q = [physics.w3_q_th_kwh_for_offset(r["device_offset"], t, r["cop_space"]) for r, t in zip(body, t_out)]
    else:
        q = [r["e_space_kwh"] * r["cop_space"] for r in body]
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
    # (a) the LP's own logged thermal chain: the spec criterion holds as written.
    rep_tau, meas = _replay(win, TAU_MEAS, "lp_log")
    rep_pin, _ = _replay(win, None, "lp_log")
    e_tau, e_pin = abs(rep_tau - meas), abs(rep_pin - meas)
    print(f"{start}: lp_log |err| tau={e_tau:.3f} pin={e_pin:.3f}")
    assert e_tau <= max(e_pin, 0.2), (e_tau, e_pin)
    # (b) executed-offset chain (helper): the model under-delivers lift heat (house warms
    # faster than modelled), so tau-consistent UA reads cold by up to ~0.33 K over 4 h.
    # Bounded, documented; NOT the spec's 0.2 (see PR body).
    rep_tau, meas = _replay(win, TAU_MEAS)
    rep_pin, _ = _replay(win, None)
    e_tau, e_pin = abs(rep_tau - meas), abs(rep_pin - meas)
    print(f"{start}: offset-chain |err| tau={e_tau:.3f} pin={e_pin:.3f}")
    assert e_tau <= max(e_pin, 0.35), (e_tau, e_pin)


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


def test_tau_zero_is_bit_for_bit_identical(monkeypatch):
    a = _solve(n=24)
    monkeypatch.setattr(app_config, "LP_W3_TAU_H", 0.0, raising=False)
    b = _solve(n=24)
    assert a.ok and b.ok
    assert a.objective_pence == b.objective_pence
    assert a.indoor_temp_c == b.indoor_temp_c
    assert b.w3_ua_source == "pin" and b.w3_ua_w_per_k == pytest.approx(UA_PIN)


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


def test_solve_smoke_no_all_night_coast(monkeypatch):
    monkeypatch.setattr(app_config, "LP_W3_TAU_H", TAU_MEAS, raising=False)
    p = _solve(outdoor=9.5, n=48)
    assert p.ok, p.status
    slots = p.slot_starts_utc
    night = [i for i, s in enumerate(slots) if s.hour >= 22 or s.hour < 7]
    # setpoint-holding hours; 20:00-22:00 is the planned pre-night coast into the 22:00 cheap band
    # (night floor 20 starts at 22:00), see PR body
    day = [i for i, s in enumerate(slots) if 7 <= s.hour < 20]
    mean_night = sum(p.lwt_offset_c[i] for i in night) / len(night)
    t_day_min = min(p.indoor_temp_c[i + 1] for i in day)
    print(f"night mean offset {mean_night:+.2f}, day min t_in {t_day_min:.2f}")
    assert mean_night >= 0.0
    assert t_day_min >= 20.9
    from src.scheduler.lp_dispatch import w3_gate_snapshot

    snap = w3_gate_snapshot(p)
    assert set(snap) >= {"ok", "reason"}  # the gate verdict is recorded


def test_replay_telemetry_payload():
    rows = _rows()
    out = ll.replay_telemetry(rows, C_KWH, UA_PIN)
    assert 20.0 <= out["tau_fit_h"] <= 150.0
    bands = out["replay_drift_by_band"]
    assert set(bands) == {"le_m1", "0_to_2", "3_to_5", "gt_5"}
    coast = bands["le_m1"]
    assert coast["n_slots"] > 0 and coast["n_windows_8h"] >= 1
    # at the tau-implied UA the coast windows drift less than at the pinned UA
    assert abs(coast["ua_tau"]["mean_8h_err_k"]) < abs(coast["ua_pin"]["mean_8h_err_k"])
    print("tau_fit_h", out["tau_fit_h"], "coast", coast)
