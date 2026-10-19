"""#866 — report-only emitter-lag (tau) estimate from logged offset steps.

The synthetic logs are generated the way the real log is built: a MINUTE-level (30 s here, vs
the fit's 1-min) simulation of the room, the logged ``indoor_real_c`` is the MEAN over the slot,
quantised to 0.01 C with sensor noise; some boost windows are tank reheats (no space heat reaches
the room while the offset is still logged and the leaving water reads 48 C)."""
from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from src.analytics import lwt_learning as L
from src.physics import get_lwt_base_c

UA, C = 200.0, 16.5
SUB = 60                      # 30-s truth steps per 30-min slot


def heat_fn(r):
    """Synthetic production (thermal kWh/slot): 0.2 kWh base + 0.15 per K of offset."""
    return max(0.0, 0.2 + 0.15 * float(r["device_offset"]))


def synth_rows(tau_min, *, noise=0.02, quant=0.01, days=14, seed=1, hold=12, dhw_every=0):
    rng = random.Random(seed)
    n = days * 48
    levels = [0.0, 8.0, -4.0, 0.0, 6.0, -3.0]
    start = datetime(2026, 10, 1, tzinfo=UTC)
    offs = [levels[(i // hold) % len(levels)] for i in range(n)]
    to = [8.0 + 2.0 * math.sin(i / 20.0) for i in range(n)]
    rows = [{"device_offset": offs[i], "outdoor_real_c": to[i]} for i in range(n)]
    dhw = set()
    if dhw_every:
        # the +8 / +6 boost blocks: every `dhw_every`-th one is a tank reheat
        blocks = [i for i in range(0, n, hold) if offs[i] > 0]
        for b in blocks[::dhw_every]:
            dhw.update(range(b, b + hold))
    q = [0.0 if i in dhw else heat_fn(rows[i]) for i in range(n)]
    dt = 0.5 / SUB
    t = 20.5
    tau_h = tau_min / 60.0
    h = q[0] / 0.5 * tau_h if tau_min > 0 else 0.0
    means = []
    for i in range(n):
        p = q[i] / 0.5
        acc = 0.0
        for _ in range(SUB):
            if tau_min > 0:
                e = math.exp(-dt / tau_h)
                h_new = h * e + p * tau_h * (1 - e)
                deliv = (h - h_new) / dt * -1 + p - (h_new - h) / dt + (h_new - h) / dt   # = h/tau avg
                deliv = 0.5 * (h + h_new) / tau_h
                h = h_new
            else:
                deliv = p
            t += (deliv - UA / 1000 * (t - to[i])) * dt / C
            acc += t
        means.append(acc / SUB)
    out = []
    for i in range(n):
        v = means[i] + rng.gauss(0, noise)
        v = round(v / quant) * quant
        lwt = 48.0 if i in dhw else get_lwt_base_c(to[i]) + offs[i]
        out.append({
            "slot_time_utc": (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "indoor_real_c": v, "outdoor_real_c": to[i], "device_offset": offs[i],
            "lwt_actual_c": lwt,
        })
    return out, len(dhw)


def fit(rows, **kw):
    return L.fit_emitter_tau(rows, ua_w_per_k=UA, c_kwh_per_k=C, heat_fn=heat_fn, **kw)


@pytest.mark.parametrize("true_tau", [20, 35, 50])
def test_recovers_known_tau_realistic_log(true_tau):
    """Slot-mean indoor, 0.01 C quantisation, DHW reheats inside some boost windows."""
    rows, n_dhw = synth_rows(true_tau, dhw_every=3)
    assert n_dhw > 0
    f = fit(rows)
    print(f"[#866 recovery] true {true_tau} -> {f['best_tau_min']} (n={f['n_events']}, "
          f"dropped_dhw={f['n_events_dropped_dhw']}, gap0={f['sse_gap_vs_zero']}, conf={f['confidence']})")
    assert f["identifiable"] and f["n_events"] >= 6
    assert f["n_events_dropped_dhw"] > 0
    assert abs(f["best_tau_min"] - true_tau) <= 10, f
    assert f["confidence"] in ("ok", "low") and f["sse_spread"] > 0.2 and f["applied"] is False


def test_tau_zero_data_recovers_about_zero():
    rows, _ = synth_rows(0, dhw_every=3)
    f = fit(rows)
    print(f"[#866 recovery] true 0 -> {f['best_tau_min']} (gap0={f['sse_gap_vs_zero']})")
    assert f["best_tau_min"] <= 10, f
    assert set(f["sse_by_tau_min"]) == {str(x) for x in range(0, 100, 10)}


def test_dhw_windows_would_bias_the_fit_if_kept():
    """Without the DHW filter the reheat windows (offset logged, no heat delivered) wreck the
    estimate: the filter is what makes the fit usable."""
    rows, _ = synth_rows(35, dhw_every=2)
    good = fit(rows)
    unfiltered = [dict(r, lwt_actual_c=None) for r in rows]          # filter blind
    bad = fit(unfiltered)
    assert good["n_events_dropped_dhw"] > 0 and bad["n_events_dropped_dhw"] == 0
    assert abs(good["best_tau_min"] - 35) <= 10
    assert bad["sse_by_tau_min"]["0"] > good["sse_by_tau_min"]["0"]


def test_tank_rise_marks_dhw_slots():
    rows, _ = synth_rows(35)
    dhw = {r["slot_time_utc"] for r in rows[100:112]}
    f_all = fit(rows)
    f_dhw = fit(rows, dhw_slots=dhw)
    assert f_dhw["n_events_dropped_dhw"] >= 1
    assert f_dhw["n_events"] < f_all["n_events"]


def test_no_events_and_sparse_events_are_reported_not_applied():
    flat, _ = synth_rows(30)
    flat = [dict(r, device_offset=0.0) for r in flat]
    f = fit(flat)
    assert f["reason"] == "no_step_events" and f["confidence"] == "none"
    few, _ = synth_rows(30, days=1)
    g = fit(few)
    assert g["n_events"] < 6 and g["confidence"] in ("none", "low")
    gappy = [r for i, r in enumerate(synth_rows(30)[0]) if i % 5 != 0]       # holes break contiguity
    assert fit(gappy)["n_events"] == 0


def test_default_heat_model_runs_on_log_rows():
    rows, _ = synth_rows(30)
    f = L.fit_emitter_tau(rows, ua_w_per_k=UA, c_kwh_per_k=C)
    assert f["n_events"] > 0 and f["best_tau_min"] in range(0, 100, 10)
