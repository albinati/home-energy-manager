"""#843 — joint UA/C fit from heating + coast slots, COP sensitivity, band-rise table."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from src.analytics.lwt_learning import fit_ua_c_joint, night_rise_per_band

UA, C, COP = 150.0, 12.0, 3.5
T0 = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


def _z(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _house(n_days=3, heat=True):
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
                     "heating_kwh": hk, "device_offset": -2 if hk == 0 else 3})
        q = hk * COP
        t_in += (q - (UA / 1000.0) * (t_in - to) * 0.5) / C
    return rows


def test_joint_fit_recovers_ua_and_c():
    rows = _house()
    fit = fit_ua_c_joint(rows, cop_fn=lambda t: COP, tau_prior_h=C / (UA / 1000.0))
    assert fit["identifiable"] is True
    assert fit["n_heat"] >= 4 and fit["n_coast"] >= 8
    assert abs(fit["ua_w_per_k"] - UA) / UA < 0.10
    assert abs(fit["c_kwh_per_k"] - C) / C < 0.10
    assert abs(fit["tau_h"] - 80.0) / 80.0 < 0.10
    assert fit["r2"] > 0.95
    tc = fit["tau_constrained"]
    assert tc is not None and abs(tc["c_kwh_per_k"] - C) / C < 0.10


def test_coast_only_is_not_identifiable_but_reports_tau():
    rows = _house(heat=False)
    fit = fit_ua_c_joint(rows, cop_fn=lambda t: COP)
    assert fit["identifiable"] is False
    assert fit["ua_w_per_k"] is None and fit["c_kwh_per_k"] is None
    assert fit["n_heat"] == 0
    assert abs(fit["coast_tau_h"] - 80.0) / 80.0 < 0.10


def test_cop_sensitivity_reported_and_moves_c():
    fit = fit_ua_c_joint(_house(), cop_fn=lambda t: COP)
    sens = fit["cop_sensitivity"]
    assert set(sens) == {"x0.8", "x1.2"}
    # less COP -> less delivered heat for the same rise -> smaller C; more COP -> larger C
    assert sens["x0.8"]["c_kwh_per_k"] < fit["c_kwh_per_k"] < sens["x1.2"]["c_kwh_per_k"]
    assert abs(sens["x1.2"]["c_kwh_per_k"] / fit["c_kwh_per_k"] - 1.2) < 0.05


def test_default_cop_fn_uses_lp_curve():
    fit = fit_ua_c_joint(_house())   # default cop_fn = config.DAIKIN_COP_CURVE
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
