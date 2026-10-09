"""#843 — joint UA/C fit from heating + coast slots, COP sensitivity, band-rise table."""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.analytics.lwt_learning import fit_ua_c_joint, night_rise_per_band

import tempfile
from pathlib import Path

import pytest

UTC_TZ = ZoneInfo("UTC")


@pytest.fixture()
def tmpdb(monkeypatch):
    from src import db
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        yield

UA, C, COP = 150.0, 12.0, 3.5
T0 = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


def _z(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _house(n_days=8, heat=True, src="onecta_cache"):
    rows = []
    t_in = 20.0
    for i in range(48 * n_days):
        st = T0 + timedelta(minutes=30 * i)
        to = 5.0 + 3.0 * math.sin(i / 7.0)
        # heating bursts of varying power in the cheap-ish hours, coast otherwise
        hk = 0.0
        if heat and (i % 48) in range(8, 14):
            hk = 0.3 + 0.15 * ((i % 48) % 3)
        rows.append({"slot_time_utc": _z(st), "indoor_real_c": t_in, "outdoor_real_c": to,
                     "heating_kwh": hk, "heating_kwh_source": src,
                     "device_offset": -2 if hk == 0 else 3})
        q = hk * COP
        t_in += (q - (UA / 1000.0) * (t_in - to) * 0.5) / C
    return rows


def test_joint_fit_recovers_ua_and_c():
    rows = _house()
    fit = fit_ua_c_joint(rows, cop_fn=lambda t: COP, tau_prior_h=C / (UA / 1000.0), tz=UTC_TZ)
    assert fit["identifiable"] is True, fit
    assert fit["n_heat_episodes"] >= 5 and fit["n_coast_blocks"] >= 8
    assert abs(fit["ua_w_per_k"] - UA) / UA < 0.15
    assert abs(fit["c_kwh_per_k"] - C) / C < 0.15
    assert fit["ua_se"] > 0 and fit["c_se"] > 0 and fit["resid_rms_c"] is not None
    assert "r2" not in fit
    tf = fit["tau_fixed"]
    assert tf is not None and abs(tf["c_kwh_per_k"] - C) / C < 0.15
    assert "tau_constrained" not in fit


def test_coast_only_is_not_identifiable_but_reports_tau():
    rows = _house(heat=False)
    fit = fit_ua_c_joint(rows, cop_fn=lambda t: COP, tz=UTC_TZ)
    assert fit["identifiable"] is False and fit["reason"] == "too_few_heat_episodes"
    assert fit["ua_w_per_k"] is None and fit["c_kwh_per_k"] is None
    assert fit["n_heat_episodes"] == 0
    assert abs(fit["coast_tau_h"] - 80.0) / 80.0 < 0.10


def test_cop_sensitivity_reported_and_moves_c():
    fit = fit_ua_c_joint(_house(), cop_fn=lambda t: COP, tz=UTC_TZ)
    sens = fit["cop_sensitivity"]
    assert set(sens) == {"x0.8", "x1.2"}
    # less COP -> less delivered heat for the same rise -> smaller C; more COP -> larger C
    assert sens["x0.8"]["c_kwh_per_k"] < fit["c_kwh_per_k"] < sens["x1.2"]["c_kwh_per_k"]


def test_default_cop_fn_uses_lp_curve():
    fit = fit_ua_c_joint(_house(), tz=UTC_TZ)   # default cop_fn = config.DAIKIN_COP_CURVE
    assert fit["identifiable"] is True and fit["c_kwh_per_k"] > 0


def test_night_rise_per_band_table():
    rows = []
    for i in range(12):
        st = T0 + timedelta(minutes=30 * i)
        cheap = 4 <= i < 8
        rows.append({
            "slot_time_utc": _z(st), "price_band": "cheap" if cheap else "day",
            "offset_written": 10 if cheap else 0,
            "indoor_real_c": 19.0 + (0.2 * (i - 4) if cheap else 0.0),
            "indoor_pred_c": 19.0 + (0.5 * (i - 4) if cheap else 0.0),
        })
    # a cheap band with a non-positive offset must not appear
    rows[10]["price_band"] = "cheap"
    out = night_rise_per_band(rows)
    assert len(out) == 1
    b = out[0]
    assert b["n_slots"] == 4 and b["mean_offset_c"] == 10.0
    assert abs(b["measured_rise_c"] - 0.6) < 1e-6 and abs(b["predicted_rise_c"] - 1.5) < 1e-6
    assert abs(b["model_error_c"] - 0.9) < 1e-6


# -- realistic data ------------------------------------------------------------


def _realistic_house(seed=0, src="onecta_cache", days=14, ua=200.0, c=16.5):
    """14 days at Cosy bands (04-07, 13-16 heated 0.5-0.9 kW_el, +-35 % modulation),
    +0.15 kW internal gain, daytime solar <= 0.6 kW, 1 h emitter lag, outdoor +-1 C, indoor
    quantised to 0.05 C + 0.01 noise, Onecta whole-kWh 2 h buckets."""
    rnd = random.Random(seed)
    cop = lambda to: 3.0 + 0.08 * to  # noqa: E731
    t_in, emitter = 20.0, 0.0
    recs = []
    for i in range(48 * days):
        st = T0 + timedelta(minutes=30 * i)
        h = st.hour + st.minute / 60
        to = 6 + 3 * math.sin(2 * math.pi * (i / 48 - 0.25)) + math.sin(i / 5.0)
        heating = (4 <= h < 7) or (13 <= h < 16)
        el = rnd.uniform(0.5, 0.9) * (1 + rnd.uniform(-0.35, 0.35)) if heating else 0.0
        lwt = (33 + rnd.uniform(-2, 2)) if el > 0 else (t_in + 2 + rnd.uniform(-1, 1))
        sol = 0.6 * max(0.0, math.sin(math.pi * (h - 8) / 8)) if 8 <= h <= 16 else 0.0
        recs.append({"st": st, "to": to + rnd.gauss(0, 0.5), "in": round(t_in / 0.05) * 0.05 + rnd.gauss(0, 0.01),
                     "kwh": el * 0.5, "lwt": lwt})
        emitter += (el * cop(to) - emitter) * (1 - math.exp(-0.5))     # 1 h lag
        t_in += (emitter + 0.15 + sol - ua / 1000 * (t_in - to)) * 0.5 / c
    bucket: dict = {}
    for r in recs:
        k = (r["st"].date(), r["st"].hour // 2)
        bucket[k] = bucket.get(k, 0.0) + r["kwh"]
    rows = [{"slot_time_utc": _z(r["st"]), "indoor_real_c": r["in"], "outdoor_real_c": r["to"],
             "lwt_actual_c": r["lwt"], "device_offset": None, "heating_kwh_source": src,
             "heating_kwh": round(bucket[(r["st"].date(), r["st"].hour // 2)]) / 4.0} for r in recs]
    return rows, cop


def test_episode_estimator_realistic_within_20pct():
    rows, cop = _realistic_house(0)
    fit = fit_ua_c_joint(rows, cop_fn=cop, tau_prior_h=82.5, tz=UTC_TZ)
    assert fit["identifiable"] is True, fit
    assert abs(fit["ua_w_per_k"] - 200.0) / 200.0 < 0.20, fit
    assert abs(fit["c_kwh_per_k"] - 16.5) / 16.5 < 0.20, fit
    assert fit["n_heat_episodes"] >= 20 and fit["n_coast_blocks"] >= 30
    assert fit["ua_se"] > 0 and fit["c_se"] > 0
    assert fit["gain_kw"] is not None and fit["resid_rms_c"] < 0.1
    assert fit["slot_fit"] is None or "r2" in fit["slot_fit"]   # diagnostic only


def test_telemetry_integral_buckets_are_excluded():
    rows, cop = _realistic_house(0, src="telemetry_integral")
    fit = fit_ua_c_joint(rows, cop_fn=cop, tau_prior_h=82.5, tz=UTC_TZ)
    assert fit["identifiable"] is False and fit["reason"] == "no_measured_input"
    assert fit["slot_fit"] is None and fit["n_heat_episodes"] == 0
    # one measured day among model-derived buckets must not bring the model into the fit
    mixed, _ = _realistic_house(0)
    for r in mixed[48:]:
        r["heating_kwh_source"] = "telemetry_integral"
    f2 = fit_ua_c_joint(mixed, cop_fn=cop, tz=UTC_TZ)
    assert f2["identifiable"] is False and f2["reason"] == "too_few_heat_episodes"


def test_hidden_heat_in_zero_bucket_is_not_a_coast():
    rows, cop = _realistic_house(0)
    base = fit_ua_c_joint(rows, cop_fn=cop, tz=UTC_TZ)
    # a metered 0 whose water temperature shows the compressor ran is hidden heat
    for r in rows[: 48 * 5]:
        if r["heating_kwh"] == 0:
            r["lwt_actual_c"] = r["indoor_real_c"] + 20.0
    after = fit_ua_c_joint(rows, cop_fn=cop, tz=UTC_TZ)
    assert after["n_coast_blocks"] < base["n_coast_blocks"] - 10


def test_consistency_flag_when_free_tau_disagrees_with_coast_tau():
    rows, cop = _realistic_house(0)
    fit = fit_ua_c_joint(rows, cop_fn=cop, tz=UTC_TZ)
    ct, tf = fit["coast_tau_h"], fit["tau_h"]
    expect = abs(tf - ct) / ct > 0.25
    assert (fit["consistency_flag"] == "lag_or_gain_contamination_suspected") is expect


def test_band_rise_mixed_plans_flagged():
    rows = []
    for i in range(8):
        rows.append({
            "slot_time_utc": _z(T0 + timedelta(minutes=30 * i)), "price_band": "cheap", "offset_written": 3,
            "indoor_real_c": 19.0 + 0.1 * i, "indoor_pred_c": 19.0 + 0.3 * i,
            "plan_updated_at_utc": "plan-A" if i < 4 else "plan-B",
        })
    (b,) = night_rise_per_band(rows)
    assert b["mixed_plans"] is True and b["n_plans"] == 2 and b["plan_token"] == "plan-A"
    # predicted rise only from the slots of the FIRST plan (0.3 * 3), measured over the span
    assert abs(b["predicted_rise_c"] - 0.9) < 1e-6 and abs(b["measured_rise_c"] - 0.7) < 1e-6
    assert b["end_utc"] == _z(T0 + timedelta(minutes=30 * 8)) and b["n_slots"] == 8
    for r in rows:
        r["plan_updated_at_utc"] = "plan-A"
    (b2,) = night_rise_per_band(rows)
    assert b2["mixed_plans"] is False and b2["n_plans"] == 1


def test_band_rise_uses_only_slots_with_both_readings():
    rows = []
    for i in range(6):
        rows.append({
            "slot_time_utc": _z(T0 + timedelta(minutes=30 * i)), "price_band": "cheap", "offset_written": 2,
            "indoor_real_c": None if i == 0 else 19.0 + 0.1 * i,
            "indoor_pred_c": 19.0 + 0.2 * i if i < 5 else None, "plan_updated_at_utc": "p"})
    (b,) = night_rise_per_band(rows)
    # both readings on slots 1..4 only
    assert b["n_slots"] == 4 and abs(b["measured_rise_c"] - 0.3) < 1e-6
    assert abs(b["predicted_rise_c"] - 0.6) < 1e-6
    assert b["end_utc"] == _z(T0 + timedelta(minutes=30 * 5))


def test_heating_kwh_source_is_stored(tmpdb):
    from src import db
    from src.analytics.lwt_learning import fill_realised
    day = (T0 + timedelta(days=1)).date()
    db.upsert_daikin_consumption_2hourly(date=day.isoformat(), bucket_idx=3, kwh_total=1.0,
                                         kwh_heating=2.0, source="onecta_cache")
    db.upsert_daikin_consumption_2hourly(date=day.isoformat(), bucket_idx=4, kwh_total=1.0,
                                         kwh_heating=0.7, source="telemetry_integral")
    fill_realised(day, UTC_TZ)
    rows = {r["slot_time_utc"]: r for r in db.get_lwt_learning_rows(
        _z(datetime(day.year, day.month, day.day, tzinfo=UTC)), _z(datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)))}
    r3 = rows[_z(datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC))]
    r4 = rows[_z(datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC))]
    assert r3["heating_kwh_source"] == "onecta_cache" and abs(r3["heating_kwh"] - 0.5) < 1e-6
    assert r4["heating_kwh_source"] == "telemetry_integral"


def test_api_passes_joint_fit_and_band_rise_through(tmpdb):
    from fastapi.testclient import TestClient

    from src import db
    from src.api.main import app

    fit = {"identifiable": False, "reason": "too_few_heat_episodes", "n_heat_episodes": 2}
    bands = [{"start_utc": "x", "end_utc": "y", "mixed_plans": True, "n_plans": 2}]
    db.upsert_lwt_learning_daily({"date": "2026-11-03", "n_coast_slots": 1, "n_heat_slots": 1,
                                  "ua_est_w_per_k": 190.0, "k_est_kw_per_c": 0.06,
                                  "pred_err_mean_c": 0.1, "pred_err_p90_c": 0.4,
                                  "payload": {"joint_fit": fit, "night_rise_per_band": bands}})
    body = TestClient(app).get("/api/v1/thermal/lwt-learning?days=7").json()
    d = body["daily"][0]
    assert d["joint_fit"] == fit and d["night_rise_per_band"] == bands
    assert d["ua_est_circular"] is True and "circular" not in d
    assert d["ua_from_tau_scaled_w_per_k"] == 190.0
