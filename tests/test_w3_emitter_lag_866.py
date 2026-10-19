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


def solve(*, tau=0.0, penalty=0.025, indoor=21.0, lwt=None, t_out=12.0, prices=None):
    config.LP_COP_LWT_OFFSET_PENALTY_PER_K = penalty
    config.LP_W3_EMITTER_TAU_MIN = tau
    w = WeatherLpSeries(
        slot_starts_utc=SLOTS, temperature_outdoor_c=[t_out] * N,
        shortwave_radiation_wm2=[0.0] * N, cloud_cover_pct=[100.0] * N,
        pv_kwh_per_slot=[0.0] * N, cop_space=[4.7] * N, cop_dhw=[3.0] * N,
    )
    st = LpInitialState(soc_kwh=3.0, tank_temp_c=45.0, indoor_temp_c=indoor, lwt_actual_c=lwt)
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
    assert sorted(p.import_kwh) == pytest.approx(sorted(GOLD_IMP), abs=1e-5)
    assert sum(p.import_kwh) == pytest.approx(sum(GOLD_IMP), abs=1e-5)
    assert p.indoor_temp_c == pytest.approx(GOLD_IND, abs=1e-4)
    assert p.objective_pence == pytest.approx(GOLD_OBJ, abs=1e-3)
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
    # What changes on this fixture (honest): the lag makes the planner START the 04-07 cheap-band
    # boost earlier (extra lift in the 03:00-04:00 slots, +1 K written), buy more heat in
    # the cheap bands, and cost slightly more (each produced kWh reaches the room late/partly
    # unused at the horizon end). Indoor min/max are unchanged within 0.05 K.
    pre0 = sum(p0.space_electric_kwh[6:8])
    pre35 = sum(p35.space_electric_kwh[6:8])
    assert pre35 > pre0 + 0.02, (pre0, pre35)
    assert band_heat(p35)["cheap"] > band_heat(p0)["cheap"] + 0.1
    assert p35.objective_pence > p0.objective_pence
    assert abs(min(p35.indoor_temp_c[1:]) - min(p0.indoor_temp_c[1:])) < 0.05


def test_h0_from_fresh_pump_on_telemetry():
    p_cold = solve(tau=35.0, lwt=None)
    p_off = solve(tau=35.0, lwt=22.0)              # lwt - indoor < 3 -> empty loop
    p_on = solve(tau=35.0, lwt=38.0)
    assert p_cold.w3_emitter_h0_kwh == 0.0 and p_off.w3_emitter_h0_kwh == 0.0
    from src.physics import get_kw_per_degc_lwt
    assert p_on.w3_emitter_h0_kwh == pytest.approx(get_kw_per_degc_lwt() * 20.0 * 4.7 * 35 / 60, rel=1e-6)
    assert p_on.w3_emitter_h0_kwh > 0.5
    assert p_on.w3_delivered_kwh[0] > p_cold.w3_delivered_kwh[0]


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
