"""#866 — W3 emitter/water-loop lag (LP_W3_EMITTER_TAU_MIN)."""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from src import db  # noqa: E402
from src.config import config  # noqa: E402
from src.scheduler import lp_dispatch  # noqa: E402
from src.scheduler.lp_initial_state import read_lp_initial_state  # noqa: E402
from src.scheduler.lp_optimizer import LpInitialState, solve_lp  # noqa: E402
from src.weather import WeatherLpSeries  # noqa: E402
from test_lp_cop_lift_863 import CURVE, N, SLOTS, TZ, _local_h, cosy_prices  # noqa: E402

# Golden from UNCHANGED main (cab34fd + #863, no emitter code): prod fixture, penalty 0.025.
GOLD_E = [0.108192, 0.1665, 0.1665, 0.1665, 0.1665, 0.1665, 0.1665, 0.1665, 0.2664, 0.2664, 0.2664, 0.2664, 0.2664, 0.2664, 0.24975, 0.21645, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.202487, 0.20783, 0.21645, 0.1998, 0.1998, 0.1998, 0.1998, 0.1998, 0.1998, 0.1998, 0.1998, 0.1665, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
GOLD_IMP = [0.0, 0.0, 0.044237, 0.4965, 0.4965, 0.4965, 0.4965, 0.4965, 0.5964, 0.5964, 0.5964, 3.063428, 3.0964, 3.0964, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.952487, 0.952487, 0.562487, 3.062487, 3.06783, 3.07645, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.33, 0.33, 0.33, 0.33]
GOLD_IND = [21.0, 20.97627, 20.9693, 20.96237, 20.95548, 20.94863, 20.94182, 20.93506, 20.92833, 20.93872, 20.94905, 20.95931, 20.96952, 20.97966, 20.98973, 20.9975, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.0, 21.00095, 21.00343, 21.00293, 21.00244, 21.00194, 21.00145, 21.00097, 21.00048, 21.0, 20.99952, 20.99241, 20.93791, 20.88374, 20.8299, 20.77638, 20.72319, 20.67032, 20.61778]
GOLD_OBJ = 356.9991528018581


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tempfile.mktemp(suffix=".db"), raising=False)
    db.init_db()
    for k, v in dict(
        DAIKIN_CONTROL_MODE="active", OPENCLAW_READ_ONLY=False, LP_HP_MIN_ON_SLOTS=1,
        LP_INVERTER_STRESS_COST_PENCE=0.0, LP_W3_TIN_ENABLED=True, BUILDING_UA_W_PER_K=200.0,
        BUILDING_THERMAL_MASS_KWH_PER_K=16.5, BUILDING_UA_LEARNED_AUTO_APPLY=False,
        INDOOR_SETPOINT_C=21.0, LP_W3_NIGHT_FLOOR_C=20.0, LP_W3_PEAK_COAST_DELTA_C=0.0,
        DAIKIN_COP_CURVE_STR=CURVE, BULLETPROOF_TIMEZONE="Europe/London",
        LP_BATTERY_WEAR_COST_PENCE_PER_KWH=5.0, BATTERY_CAPACITY_KWH=10.36,
        OPTIMIZATION_LWT_OFFSET_MAX=10.0, DAIKIN_LWT_SOURCE="lp", DAIKIN_LWT_LP_OFFSET_MAX=10.0,
        DAIKIN_LWT_ABS_MAX_C=60.0, LP_W3_EMERGENCY_PEN_PENCE_PER_DEGC_SLOT=0.0,
        LP_COP_LIFT_PENALTY_PER_KELVIN=0.0,
    ).items():
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setitem(config._overrides, "LP_W3_CEILING_C", 23.0)
    yield
    for k in ("LP_COP_LWT_OFFSET_PENALTY_PER_K", "LP_W3_EMITTER_TAU_MIN"):
        config._overrides.pop(k, None)


def solve(*, tau=0.0, penalty=0.025, indoor=21.0, lwt=None, t_out=12.0, prices=None, h0=None):
    config.LP_COP_LWT_OFFSET_PENALTY_PER_K = penalty
    config.LP_W3_EMITTER_TAU_MIN = tau
    w = WeatherLpSeries(
        slot_starts_utc=SLOTS, temperature_outdoor_c=[t_out] * N,
        shortwave_radiation_wm2=[0.0] * N, cloud_cover_pct=[100.0] * N,
        pv_kwh_per_slot=[0.0] * N, cop_space=[4.7] * N, cop_dhw=[3.0] * N,
    )
    st = LpInitialState(soc_kwh=3.0, tank_temp_c=45.0, indoor_temp_c=indoor, lwt_actual_c=lwt,
                         emitter_h0_kwh=h0)
    return solve_lp(
        slot_starts_utc=SLOTS, price_pence=prices or cosy_prices(), base_load_kwh=[0.3] * N,
        weather=w, initial=st, tz=TZ, export_price_pence=[5.0] * N,
    )


def written(plan):
    offs = lp_dispatch._lp_offsets(plan, None, now_utc=SLOTS[0])
    if offs is None:
        return None
    heating = [e > 1e-6 and o is not None and o > -3 for e, o in zip(plan.space_electric_kwh, offs)]
    return lp_dispatch.smooth_lp_offsets(offs, 2, heating=heating, bands=plan.price_band)


def band_heat(plan):
    out = {"cheap": 0.0, "day": 0.0, "peak": 0.0}
    for i, e in enumerate(plan.space_electric_kwh):
        h = _local_h(i)
        k = "peak" if 16 <= h < 19 else ("cheap" if (4 <= h < 7 or 13 <= h < 16 or h >= 22) else "day")
        out[k] += e
    return out


def test_tau_zero_is_bit_for_bit_vs_main():
    p = solve(tau=0.0)
    assert p.space_electric_kwh == pytest.approx(GOLD_E, abs=1e-5)
    # import timing among equal-cost slots is a MIP tie (host-dependent, as in #863 test): compare the multiset
    # import timing / objective differ by host (equal-cost MIP ties, CI vs local): the HP plan and
    # the indoor trajectory are the bit-for-bit signal; import and objective within 1 %.
    assert sum(p.import_kwh) == pytest.approx(sum(GOLD_IMP), rel=0.01)
    assert p.indoor_temp_c == pytest.approx(GOLD_IND, abs=1e-4)
    assert p.objective_pence == pytest.approx(GOLD_OBJ, rel=0.01)
    assert p.w3_delivered_kwh == [] and p.w3_emitter_tau_min == 0.0 and p.w3_emitter_h0_kwh == 0.0


def test_lag_delays_boost_and_tail_heat_after_setback():
    """Cold house forced to heat (indoor 19 vs floor 20/21). With a lag the room rises less
    in the first boosted slot, and heat keeps arriving (d > 0) in slots with no production."""
    p0 = solve(tau=0.0, indoor=19.0, prices=[38.17] * N)
    p35 = solve(tau=35.0, indoor=19.0, prices=[38.17] * N)
    assert p0.ok and p35.ok and p35.w3_emitter_tau_min == 35.0
    assert p0.space_electric_kwh[0] > 0.1 and p35.space_electric_kwh[0] > 0.1
    rise0 = p0.indoor_temp_c[1] - p0.indoor_temp_c[0]
    rise35 = p35.indoor_temp_c[1] - p35.indoor_temp_c[0]
    print(f"[#866] first-slot rise: tau0 {rise0:.3f} K vs tau35 {rise35:.3f} K")
    assert rise35 < 0.8 * rise0
    tail = [i for i in range(N) if p35.space_electric_kwh[i] < 1e-6 and p35.w3_delivered_kwh[i] > 1e-3]
    assert tail, "heat must still be delivered in slots with no production"


def test_energy_conserved_through_the_loop():
    p = solve(tau=35.0)
    produced = sum(e * c for e, c in zip(p.space_electric_kwh, p.cop_space_effective))
    # nothing is created; some heat may still sit in the loop at the horizon end
    assert sum(p.w3_delivered_kwh) <= produced + p.w3_emitter_h0_kwh + 1e-6
    assert sum(p.w3_delivered_kwh) >= 0.5 * produced


def test_prod_like_fixture_tau0_vs_tau35():
    p0 = solve(tau=0.0)
    p35 = solve(tau=35.0)
    assert p0.ok and p35.ok
    print("[#866 numbers] written offsets tau0 :", written(p0))
    print("[#866 numbers] written offsets tau35:", written(p35))
    for tag, p in (("tau0", p0), ("tau35", p35)):
        ind = p.indoor_temp_c[1:]
        b = {k: round(v, 3) for k, v in band_heat(p).items()}
        print(
            f"[#866 numbers] {tag}: heat by band {b} total {sum(p.space_electric_kwh):.3f} kWh, "
            f"indoor min {min(ind):.2f} max {max(ind):.2f}, import {sum(p.import_kwh):.3f} kWh, "
            f"obj {p.objective_pence:.1f}p"
        )
    # What changes on this fixture (honest): with h seeded at the curve (house heating all
    # night, the realistic start) the lag leaves the SHAPE nearly unchanged - same cheap-band
    # boost, same peak setback slot, same comfort min/max - and shifts a little heat out of the
    # day band; cost moves by a few pence either way. The seeded loop heat is delivered in the
    # first slots (that is part of the cost difference vs an empty loop).
    assert p35.w3_emitter_h0_kwh > 0.3
    assert max(abs(a - b) for a, b in zip(p0.space_electric_kwh, p35.space_electric_kwh)) > 0.02
    assert written(p0) != written(p35)
    assert abs(min(p35.indoor_temp_c[1:]) - min(p0.indoor_temp_c[1:])) < 0.05
    assert abs(max(p35.indoor_temp_c[1:]) - max(p0.indoor_temp_c[1:])) < 0.05
    first_setback = lambda w: next(i for i in range(36, len(w)) if w[i] < 0)  # noqa: E731
    assert abs(first_setback(written(p35)) - first_setback(written(p0))) <= 1
    assert abs(p35.objective_pence - p0.objective_pence) < 5.0
    p35_empty = solve(tau=35.0, h0=0.0)
    print(f"[#866 numbers] tau35 with EMPTY loop (h0=0): obj {p35_empty.objective_pence:.1f}p, "
          f"written {written(p35_empty)}")
    assert p35_empty.objective_pence > p35.objective_pence


def _curve_h0(tau, t_out=12.0, indoor=21.0, cop=4.7):
    from src.physics import get_kw_per_degc_lwt, get_lwt_base_c
    c = get_lwt_base_c(t_out)
    ramp = min(1.0, max(0.0, (c - indoor - 1.0) / 3.0))
    return get_kw_per_degc_lwt() * (c - 18.0) * cop * tau / 60.0 * ramp


def test_h0_seeded_from_plausible_live_lwt_and_curve_fallback():
    from src.physics import get_kw_per_degc_lwt, get_lwt_base_c
    curve = get_lwt_base_c(12.0)
    assert curve - 21.0 > 4.0, "fixture needs a curve well above the room"
    p_abs = solve(tau=35.0, lwt=None)              # absent -> steady state at the curve
    p_curve = solve(tau=35.0, lwt=curve)
    assert p_abs.w3_emitter_h0_kwh == pytest.approx(_curve_h0(35.0), rel=1e-6)
    assert p_curve.w3_emitter_h0_kwh == pytest.approx(_curve_h0(35.0), rel=1e-6)
    assert p_abs.w3_emitter_h0_kwh > 0.3
    # a plausible boosted reading (curve + 3 K) -> more stored heat, lift-derated COP (0.025/K)
    p_boost = solve(tau=35.0, lwt=curve + 3.0)
    want = get_kw_per_degc_lwt() * (curve + 3.0 - 18.0) * 4.7 * (1 - 0.025 * 3.0) * 35 / 60
    assert p_boost.w3_emitter_h0_kwh == pytest.approx(want, rel=1e-6)
    assert p_boost.w3_emitter_h0_kwh > p_curve.w3_emitter_h0_kwh
    # stored heat is delivered into the first slot
    assert p_abs.w3_delivered_kwh[0] > solve(tau=35.0, lwt=19.0).w3_delivered_kwh[0] - 1e-9


def test_h0_rejects_dhw_contaminated_reading():
    """A tank reheat reads 45-50 C on the same sensor: that is NOT loop heat for the room."""
    p_dhw = solve(tau=35.0, lwt=48.0)
    p_abs = solve(tau=35.0, lwt=None)
    assert p_dhw.w3_emitter_h0_kwh == pytest.approx(p_abs.w3_emitter_h0_kwh, rel=1e-9)
    assert p_dhw.w3_emitter_h0_kwh < 1.5            # not the ~5 kWh_th of a 48 C steady state


def test_h0_ramp_is_continuous_not_a_cliff():
    from src.physics import get_lwt_base_c
    curve = get_lwt_base_c(12.0)
    # readings just under the curve + max lift cap but close to the room: sweep 0.5..5 K above room
    vals = []
    for above in (0.5, 1.0, 1.5, 2.5, 3.5, 4.0, 5.0):
        lwt = 21.0 + above
        # only meaningful while the reading is plausible (<= curve + 10 + 5)
        vals.append(solve(tau=35.0, lwt=lwt).w3_emitter_h0_kwh)
    assert vals[0] == 0.0 and vals[1] == 0.0          # <= 1 K above the room: empty loop
    assert vals[2] > 0.0                              # no jump at the old 3 K cliff
    assert all(b >= a - 1e-9 for a, b in zip(vals, vals[1:]))
    steps = [b - a for a, b in zip(vals, vals[1:])]
    assert max(steps) < 0.8 * vals[-1], (vals, "no single-step cliff")


def test_explicit_h0_override_wins():
    p = solve(tau=35.0, lwt=None, h0=1.234)
    assert p.w3_emitter_h0_kwh == pytest.approx(1.234)
    assert p.w3_emitter_heat_kwh[0] == pytest.approx(1.234)
    assert len(p.w3_emitter_heat_kwh) == N + 1


def test_initial_state_reads_only_fresh_live_lwt(monkeypatch):
    def _boom():
        raise RuntimeError("no fox")

    monkeypatch.setattr("src.scheduler.lp_initial_state.get_cached_realtime", _boom)
    monkeypatch.setattr(
        "src.scheduler.lp_initial_state.daikin_service.get_lp_state_cached_or_estimated",
        lambda actor=None: {"tank_temp_c": 45.0, "source": "x"},
    )

    def put(age_s, source="live", lwt=36.0):
        conn = db.get_connection()
        conn.execute("DELETE FROM daikin_telemetry")
        conn.execute(
            "INSERT INTO daikin_telemetry (fetched_at, source, lwt_actual_c) VALUES (?,?,?)",
            (time.time() - age_s, source, lwt),
        )
        conn.commit()
        conn.close()

    assert read_lp_initial_state().lwt_actual_c is None            # absent
    put(5 * 60)
    assert read_lp_initial_state().lwt_actual_c == 36.0            # fresh
    put(45 * 60)
    assert read_lp_initial_state().lwt_actual_c is None            # stale
    put(60, source="estimate")
    assert read_lp_initial_state().lwt_actual_c is None            # not live


def test_setting_registered_and_whitelisted():
    from src.runtime_settings import SCHEMA
    from src.scheduler.lp_overrides import WHITELIST
    s = SCHEMA["LP_W3_EMITTER_TAU_MIN"]
    assert s.env_default() == 0.0 and (s.min_value, s.max_value) == (0.0, 120.0)
    assert WHITELIST["LP_W3_EMITTER_TAU_MIN"].config_attr == "LP_W3_EMITTER_TAU_MIN"


# ── plausibility gate ─────────────────────────────────────────────────────────
def _gate_plan(slack_slots, *, tau, em_slots=()):
    from datetime import UTC, datetime, timedelta

    from src.scheduler.lp_optimizer import LpPlan
    n = 12
    t0 = datetime(2026, 11, 4, tzinfo=UTC)
    p = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
               slot_starts_utc=[t0 + timedelta(minutes=30 * i) for i in range(n)])
    p.lwt_offset_c = [0.0] * n
    p.space_electric_kwh = [0.3] * n
    p.indoor_temp_c = [20.5] * (n + 1)
    p.comfort_slack_c = [0.5 if i in slack_slots else 0.0 for i in range(n)]
    p.comfort_slack_hi_c = [0.0] * n
    p.comfort_slack_emergency_c = [0.5 if i in em_slots else 0.0 for i in range(n)]
    p.w3_emergency_floor_c = 20.0
    p.w3_night_floor_c = 20.0
    p.w3_ceiling_c = 23.0
    p.w3_emitter_tau_min = tau
    return p


def test_gate_ignores_floor_slack_in_the_first_lag_slots(monkeypatch):
    from src.scheduler.lp_dispatch import w3_trajectory_plausible
    monkeypatch.setattr(config, "LP_W3_MAX_SLACK_SLOTS", 4, raising=False)
    cold_start = {0, 1, 2, 3, 4, 5}              # 6 slack slots: vetoed at tau 0 (> 4)
    assert w3_trajectory_plausible(_gate_plan(cold_start, tau=0.0))[0] is False
    # tau 35 -> first ceil(35/30)+1 = 3 slots ignored -> 3 left <= 4 -> passes
    assert w3_trajectory_plausible(_gate_plan(cold_start, tau=35.0))[0] is True
    # slack LATER in the plan still vetoes at tau 35
    late = {3, 4, 5, 6, 7, 8, 9}
    assert w3_trajectory_plausible(_gate_plan(late, tau=35.0))[0] is False
    # the skip is exactly ceil(tau/30)+1: tau 90 -> 4 slots ignored
    assert w3_trajectory_plausible(_gate_plan({0, 1, 2, 3, 4, 5, 6, 7}, tau=90.0))[0] is True
    assert w3_trajectory_plausible(_gate_plan({0, 1, 2, 3, 4, 5, 6, 7, 8, 9}, tau=90.0))[0] is False


def test_gate_emergency_slack_skips_only_the_lag_slots():
    from src.scheduler.lp_dispatch import w3_trajectory_plausible
    assert w3_trajectory_plausible(_gate_plan(set(), tau=0.0, em_slots={0}))[1] == "emergency_slack"
    assert w3_trajectory_plausible(_gate_plan(set(), tau=35.0, em_slots={0, 1, 2}))[0] is True
    assert w3_trajectory_plausible(_gate_plan(set(), tau=35.0, em_slots={0, 5}))[1] == "emergency_slack"


# ── chained replay + snapshot ─────────────────────────────────────────────────
def test_state_at_carries_stored_loop_heat():
    from src.scheduler.lp_replay import _state_at
    p = solve(tau=35.0)
    assert len(p.w3_emitter_heat_kwh) == N + 1
    st = _state_at(p, SLOTS[10])
    assert st.emitter_h0_kwh == pytest.approx(p.w3_emitter_heat_kwh[10])
    # the next segment starts from that stored heat (explicit override beats the LWT rule)
    p2 = solve(tau=35.0, lwt=None, h0=st.emitter_h0_kwh)
    assert p2.w3_emitter_h0_kwh == pytest.approx(p.w3_emitter_heat_kwh[10])
    p0 = solve(tau=0.0)
    assert _state_at(p0, SLOTS[10]).emitter_h0_kwh is None            # lag off -> nothing carried


def test_snapshot_roundtrip_of_emitter_seed():
    import json

    from src.scheduler import optimizer as opt
    from src.scheduler.lp_replay import _snapshot_emitter
    p = solve(tau=35.0, lwt=None)
    init = LpInitialState(soc_kwh=3.0, tank_temp_c=45.0, indoor_temp_c=21.0, lwt_actual_c=33.0)
    opt._persist_lp_snapshots(
        run_id=1, run_at_iso="2026-10-20T00:00:00Z", plan_date="2026-10-20", plan=p,
        initial=init, base_load=[0.3] * N, micro_climate_offset=0.0,
    )
    row = db.get_lp_inputs(1)
    em = json.loads(row["exogenous_snapshot_json"])["emitter"]
    assert em["h0_kwh"] == pytest.approx(p.w3_emitter_h0_kwh) and em["lwt_actual_c"] == 33.0
    lwt, h0 = _snapshot_emitter(row)
    assert lwt == 33.0 and h0 == pytest.approx(p.w3_emitter_h0_kwh)
    # a pre-#866 snapshot has no key -> (None, None) -> the LP's steady-state-at-curve fallback
    assert _snapshot_emitter({"exogenous_snapshot_json": "{}"}) == (None, None)
    assert _snapshot_emitter({"exogenous_snapshot_json": None}) == (None, None)
