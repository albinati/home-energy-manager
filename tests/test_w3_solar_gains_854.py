"""#854 — W3 RC model gains (internal + PV-driven solar), learned in the nightly estimator."""
from __future__ import annotations

import math
import tempfile
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.analytics.lwt_learning import fit_ua_c_joint, pred_resid_breakdown
from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

TZ = ZoneInfo("Europe/London")
UTC_TZ = ZoneInfo("UTC")


# ── LP ───────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _lp_env(monkeypatch):
    monkeypatch.setattr(app_config, "DB_PATH", tempfile.mktemp(suffix=".db"), raising=False)
    db.init_db()
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1, raising=False)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "BUILDING_UA_W_PER_K", 60.0, raising=False)
    monkeypatch.setattr(app_config, "BUILDING_THERMAL_MASS_KWH_PER_K", 12.0, raising=False)
    monkeypatch.setattr(app_config, "INDOOR_SETPOINT_C", 21.0, raising=False)


N = 24
BASE = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)   # 06:00 UTC = 06:00 local in January
SUN = range(12, 18)                              # 12:00-15:00 local


def _solve(*, price=20.0, pv_kw_sun=2.0, indoor=21.0):
    slots = [BASE + timedelta(minutes=30 * i) for i in range(N)]
    pv = [pv_kw_sun * 0.5 if i in SUN else 0.0 for i in range(N)]
    w = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[6.0] * N,
        shortwave_radiation_wm2=[40.0] * N, cloud_cover_pct=[20.0] * N,
        pv_kwh_per_slot=pv, cop_space=[3.2] * N, cop_dhw=[2.5] * N,
    )
    st = LpInitialState(soc_kwh=6.0, tank_temp_c=45.0, indoor_temp_c=indoor)
    prices = [price] * N if not isinstance(price, list) else price
    return solve_lp(slot_starts_utc=slots, price_pence=prices, base_load_kwh=[0.3] * N,
                    weather=w, initial=st, tz=TZ, export_price_pence=[25.0] * N)


def _set(monkeypatch, internal=0.0, solar=0.0):
    monkeypatch.setattr(app_config, "LP_W3_INTERNAL_GAIN_KW", internal, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_SOLAR_GAIN_KW_PER_PV_KW", solar, raising=False)


def test_gains_zero_is_bit_for_bit(monkeypatch):
    """Defaults (never set) and explicit 0.0 produce the identical plan; nothing recorded."""
    plan_default = _solve()
    _set(monkeypatch, 0.0, 0.0)
    plan_zero = _solve()
    assert plan_default.ok and plan_zero.ok
    assert plan_zero.space_electric_kwh == plan_default.space_electric_kwh
    assert plan_zero.indoor_temp_c == plan_default.indoor_temp_c
    assert plan_zero.objective_pence == plan_default.objective_pence
    assert plan_zero.w3_gain_kw == []
    assert plan_zero.w3_solar_gain_kw_per_pv_kw == 0.0 and plan_zero.w3_internal_gain_kw == 0.0


def test_gains_default_config_is_zero():
    assert app_config.LP_W3_INTERNAL_GAIN_KW == 0.0
    assert app_config.LP_W3_SOLAR_GAIN_KW_PER_PV_KW == 0.0


def test_sunny_afternoon_needs_no_heating_with_solar_gain(monkeypatch):
    base = _solve()
    assert base.ok
    base_window = sum(base.space_electric_kwh[i] for i in SUN)
    assert base_window > 0.05, "without gains the plan must heat to hold 21 in the sunny window"
    _set(monkeypatch, 0.0, 1.0)
    sunny = _solve()
    assert sunny.ok, sunny.status
    assert sum(sunny.space_electric_kwh[i] for i in SUN) == pytest.approx(0.0, abs=1e-6)
    # predicted trajectory rises through the sunny window
    assert sunny.indoor_temp_c[SUN[-1] + 1] > sunny.indoor_temp_c[SUN[0]] + 0.1
    # the plan records what it assumed (kW thermal per slot = solar coeff x PV kW)
    assert sunny.w3_solar_gain_kw_per_pv_kw == 1.0
    assert sunny.w3_gain_kw[SUN[0]] == pytest.approx(2.0)
    assert sunny.w3_gain_kw[0] == 0.0
    assert sum(sunny.space_electric_kwh) < sum(base.space_electric_kwh)


def test_internal_gain_lowers_night_heating(monkeypatch):
    base = _solve(pv_kw_sun=0.0)
    _set(monkeypatch, 0.3, 0.0)
    g = _solve(pv_kw_sun=0.0)
    assert g.ok
    assert sum(g.space_electric_kwh) < sum(base.space_electric_kwh)
    assert g.w3_gain_kw[0] == pytest.approx(0.3)


def test_ceiling_still_binds_with_gains(monkeypatch):
    """Cheap prices + big gains: any slot that heats must not overshoot the ceiling, and the
    heating-driven rise stays under the gentle-recovery cap."""
    _set(monkeypatch, 0.2, 1.5)
    cheap = [4.0 if i in SUN else 30.0 for i in range(N)]
    plan = _solve(price=cheap)
    assert plan.ok, plan.status
    for i, e in enumerate(plan.space_electric_kwh):
        if e > 1e-6:
            assert plan.comfort_slack_hi_c[i] <= 1e-6, f"slot {i} heats into a ceiling overshoot"
            assert plan.indoor_temp_c[i + 1] <= plan.w3_ceiling_c + 1e-6
    c_bld = 12.0
    recov = float(getattr(app_config, "LP_W3_MAX_RECOVERY_C_PER_SLOT", 0.5))
    assert max(e * 3.2 / c_bld for e in plan.space_electric_kwh) <= recov + 1e-6


def test_pessimistic_pv_scales_gain(monkeypatch):
    """Gain follows the (scenario-scaled) PV the LP is handed: half the PV, half the solar gain."""
    _set(monkeypatch, 0.0, 1.0)
    full = _solve(pv_kw_sun=2.0)
    half = _solve(pv_kw_sun=1.0)
    assert half.w3_gain_kw[SUN[0]] == pytest.approx(full.w3_gain_kw[SUN[0]] / 2)


def test_gain_settings_are_runtime_tunable(monkeypatch):
    from src.runtime_settings import SCHEMA
    for k in ("LP_W3_INTERNAL_GAIN_KW", "LP_W3_SOLAR_GAIN_KW_PER_PV_KW"):
        assert k in SCHEMA and SCHEMA[k].env_default() == 0.0


# ── learner ──────────────────────────────────────────────────────────────────
UA, C, COP = 150.0, 12.0, 3.5
G_INT, S_GAIN = 0.2, 0.6
T0 = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


def _z(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _house(n_days=14, solar=True, day_pv=True):
    rows = []
    t_in = 20.0
    for i in range(48 * n_days):
        st = T0 + timedelta(minutes=30 * i)
        slot = i % 48
        to = 5.0 + 3.0 * math.sin(i / 7.0)
        hk = 0.0
        if slot in range(8, 14):
            hk = 0.3 + 0.15 * (slot % 3)
        # daylight bell, strongly varying by day (cloudy / clear)
        day_f = 0.3 + 0.7 * (0.5 + 0.5 * math.sin(i / 48.0 * 2.7))
        pv = max(0.0, 3.0 * day_f * math.sin(math.pi * (slot - 16) / 16.0)) if 16 <= slot <= 32 else 0.0
        row = {"slot_time_utc": _z(st), "indoor_real_c": t_in, "outdoor_real_c": to,
               "heating_kwh": hk, "heating_kwh_source": "onecta_cache",
               "device_offset": -2 if hk == 0 else 3}
        if day_pv:
            row["pv_real_kw"] = pv
        rows.append(row)
        gain = G_INT + (S_GAIN * pv if solar else 0.0)
        t_in += (hk * COP + gain * 0.5 - (UA / 1000.0) * (t_in - to) * 0.5) / C
    return rows


def test_learner_recovers_solar_and_internal_gain():
    fit = fit_ua_c_joint(_house(), cop_fn=lambda t: COP, tz=UTC_TZ)
    assert fit["identifiable"] is True, fit
    assert fit["solar_identifiable"] is True, fit["solar_reason"]
    assert fit["n_day_coast_blocks"] >= 6
    assert abs(fit["solar_gain_kw_per_pv_kw"] - S_GAIN) / S_GAIN < 0.25
    # internal gain is collinear with UA on night-coast data -> only a coarse check
    assert abs(fit["internal_gain_kw"] - G_INT) < 0.1
    assert fit["solar_gain_se"] > 0 and fit["internal_gain_se"] > 0


def test_learner_no_solar_in_data_gives_near_zero():
    fit = fit_ua_c_joint(_house(solar=False), cop_fn=lambda t: COP, tz=UTC_TZ)
    assert fit["solar_identifiable"] is True
    assert abs(fit["solar_gain_kw_per_pv_kw"]) < 0.15


def test_night_only_data_solar_not_identifiable():
    rows = _house()
    for r in rows:   # drop realised PV: daytime coast blocks become unusable
        r.pop("pv_real_kw", None)
    fit = fit_ua_c_joint(rows, cop_fn=lambda t: COP, tz=UTC_TZ)
    assert fit["identifiable"] is True            # UA / C unchanged
    assert fit["solar_identifiable"] is False
    assert fit["solar_reason"] == "no_daytime_coast_blocks"
    assert fit["solar_gain_kw_per_pv_kw"] is None


def test_daytime_blocks_do_not_bias_headline_ua():
    """The headline UA/C fit must ignore daytime coast blocks (they belong to the solar fit)."""
    with_pv = fit_ua_c_joint(_house(), cop_fn=lambda t: COP, tz=UTC_TZ)
    rows = _house()
    for r in rows:
        r.pop("pv_real_kw", None)
    without = fit_ua_c_joint(rows, cop_fn=lambda t: COP, tz=UTC_TZ)
    assert with_pv["ua_w_per_k"] == without["ua_w_per_k"]
    assert with_pv["c_kwh_per_k"] == without["c_kwh_per_k"]


def test_pred_resid_breakdown_by_day_night_and_pv_tercile():
    rows = []
    for i in range(48):
        st = T0 + timedelta(minutes=30 * i)
        pv = 3.0 if 24 <= i < 30 else (1.0 if 18 <= i < 24 else 0.0)
        pred = 21.0
        real = 21.0 + (0.4 * pv / 3.0 if 16 <= i <= 32 else 0.0)   # sun makes the house warmer
        rows.append({"slot_time_utc": _z(st), "indoor_real_c": real, "indoor_pred_c": pred, "pv_real_kw": pv})
    out = pred_resid_breakdown(rows, UTC_TZ)
    assert out["night"]["mean_c"] == pytest.approx(0.0, abs=1e-9)
    assert out["day"]["mean_c"] > 0.0
    ter = {t["tercile"]: t["mean_c"] for t in out["by_pv_tercile"]}
    assert ter["high"] > ter["low"] and ter["high"] > 0.25


def test_db_roundtrip_gain_and_pv_columns():
    db.upsert_lwt_learning_planned([{"slot_time_utc": "2026-11-04T10:00:00Z", "gain_kw": 1.25}])
    db.update_lwt_learning_realised("2026-11-04T10:00:00Z", {"pv_real_kw": 2.5})
    r = db.get_lwt_learning_rows("2026-11-04T09:00:00Z", "2026-11-04T11:00:00Z")[0]
    assert r["gain_kw"] == 1.25 and r["pv_real_kw"] == 2.5
