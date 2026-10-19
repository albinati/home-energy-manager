"""#866 — report-only emitter-lag (tau) estimate from logged offset steps."""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from src.analytics import lwt_learning as L

UA, C = 200.0, 16.5
DT_H = 0.5


def heat_fn(r):
    """Synthetic production (thermal kWh/slot): 0.2 kWh base + 0.15 per K of offset."""
    return max(0.0, 0.2 + 0.15 * float(r["device_offset"]))


def synth_rows(tau_min, *, noise=0.01, days=14, seed=1, hold=12):
    """Offset staircase (0 -> +8 -> -4 -> 0 ...) held `hold` slots; room simulated with the
    exact first-order lag of `tau_min`, then Gaussian noise on the logged indoor."""
    rng = random.Random(seed)
    n = days * 48
    levels = [0.0, 8.0, -4.0, 0.0, 6.0, -3.0]
    start = datetime(2026, 10, 1, tzinfo=UTC)
    offs = [levels[(i // hold) % len(levels)] for i in range(n)]
    to = [8.0 + 2.0 * math.sin(i / 20.0) for i in range(n)]
    rows = [{"device_offset": offs[i], "outdoor_real_c": to[i]} for i in range(n)]
    q = [heat_fn(r) for r in rows]
    t = [20.5]
    if tau_min > 0:
        tau_h = tau_min / 60.0
        a = 1 - math.exp(-DT_H / tau_h)
        b = 1 - (tau_h / DT_H) * a
        h = q[0] * tau_h / DT_H
    for i in range(n):
        if tau_min > 0:
            d = a * h + b * q[i]
            h = h + q[i] - d
        else:
            d = q[i]
        t.append(t[-1] + (d - UA / 1000 * (t[-1] - to[i]) * DT_H) / C)
    out = []
    for i in range(n):
        out.append({
            "slot_time_utc": (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "indoor_real_c": t[i] + rng.gauss(0, noise),
            "outdoor_real_c": to[i],
            "device_offset": offs[i],
        })
    return out


@pytest.mark.parametrize("true_tau", [20, 40, 60])
def test_recovers_known_tau_within_10_min(true_tau):
    fit = L.fit_emitter_tau(synth_rows(true_tau), ua_w_per_k=UA, c_kwh_per_k=C, heat_fn=heat_fn)
    assert fit["identifiable"] and fit["n_events"] >= 10
    assert abs(fit["best_tau_min"] - true_tau) <= 10, fit
    assert fit["applied"] is False


def test_off_grid_tau_lands_on_a_neighbour():
    fit = L.fit_emitter_tau(synth_rows(35), ua_w_per_k=UA, c_kwh_per_k=C, heat_fn=heat_fn)
    assert abs(fit["best_tau_min"] - 35) <= 10, fit


def test_tau_zero_data_recovers_about_zero():
    fit = L.fit_emitter_tau(synth_rows(0), ua_w_per_k=UA, c_kwh_per_k=C, heat_fn=heat_fn)
    assert fit["best_tau_min"] <= 10, fit
    assert set(fit["sse_by_tau_min"]) == {str(x) for x in range(0, 100, 10)}


def test_no_events_and_sparse_events_are_reported_not_applied():
    flat = [dict(r, device_offset=0.0) for r in synth_rows(30)]
    assert L.fit_emitter_tau(flat, ua_w_per_k=UA, c_kwh_per_k=C, heat_fn=heat_fn)["reason"] == "no_step_events"
    few = synth_rows(30, days=1)
    f = L.fit_emitter_tau(few, ua_w_per_k=UA, c_kwh_per_k=C, heat_fn=heat_fn)
    assert f["n_events"] < 10
    gappy = [r for i, r in enumerate(synth_rows(30)) if i % 5 != 0]       # holes break contiguity
    assert L.fit_emitter_tau(gappy, ua_w_per_k=UA, c_kwh_per_k=C, heat_fn=heat_fn)["n_events"] == 0


def test_default_heat_model_runs_on_log_rows(monkeypatch):
    """Smoke: the default (LP draw model x COP) path works on learning-log style rows."""
    rows = synth_rows(30)
    f = L.fit_emitter_tau(rows, ua_w_per_k=UA, c_kwh_per_k=C)
    assert f["n_events"] > 0 and f["best_tau_min"] in range(0, 100, 10)
