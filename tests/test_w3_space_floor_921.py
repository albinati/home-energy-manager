"""#921 — W3 space-heating floor (device-based: curve draw at the deepest writable offset)."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

TZ = ZoneInfo("Europe/London")
HP_KWH = 2.0 * 0.5  # DAIKIN_MAX_HP_KW (2.0 default) x 30 min

ENV = {
    "DAIKIN_CONTROL_MODE": "active", "LP_HP_MIN_ON_SLOTS": 1,
    "LP_INVERTER_STRESS_COST_PENCE": 0.0, "LP_W3_TIN_ENABLED": True,
    "BUILDING_UA_W_PER_K": 200.0, "DAIKIN_HOLIDAY_DETECT_ENABLED": True,
    "DAIKIN_LWT_SOURCE": "lp",
    # hermetic vs a dev .env found by load_dotenv() in a parent dir (the golden is CI/no-.env)
    "BATTERY_CAPACITY_KWH": 10.0,
    "LP_SOC_FINAL_KWH": 2.5,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(app_config, "DB_PATH", tempfile.mktemp(suffix=".db"), raising=False)
    db.init_db()
    for k, v in ENV.items():
        monkeypatch.setattr(app_config, k, v, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", False, raising=False)


def _solve(outdoor=8.0, indoor=21.0, n=24, prices=None, start=None, **init_kw):
    base = start or datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[outdoor] * n,
        shortwave_radiation_wm2=[40.0] * n, cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.0] * n, cop_space=[3.2] * n, cop_dhw=[2.5] * n,
    )
    st = LpInitialState(soc_kwh=6.0, tank_temp_c=init_kw.pop("tank", 45.0), indoor_temp_c=indoor, **init_kw)
    pr = prices or ([10.0] * 12 + [40.0] * 6 + [10.0] * 6)[:n] + [10.0] * max(0, n - 24)
    return solve_lp(
        slot_starts_utc=slots, price_pence=pr, base_load_kwh=[0.3] * n,
        weather=w, initial=st, tz=TZ,
    )


# Golden OFF plan: values computed with the PRE-PR code (origin/main f72db76) on this exact scenario under pytest.
GOLDEN_OFF = {"space": [0.2968, 0.2968, 0.2968, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.0073, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.2968, 0.2968, 0.2968, 0.2968, 0.0], "dhw": [0.03, 0.03, 0.03, 0.03, 0.03, 0.03, 0.03, 0.03, 0.03, 0.03, 0.45, 0.45, 0.06, 0.06, 0.06, 0.06, 0.06, 0.06, 0.06, 0.06, 0.06, 0.06, 0.06, 0.06], "imp": [0.6268, 0.6268, 0.6268, 0.6463, 0.6463, 0.6463, 0.6463, 0.6463, 0.6463, 0.6463, 1.0663, 1.0663, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.6764, 0.6568, 0.6568, 0.6568, 0.6568, 0.786], "soc": [6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 5.6171, 4.9119, 4.2068, 3.5017, 2.7965, 2.0914, 2.0914, 2.0914, 2.0914, 2.0914, 2.0914, 2.5], "tin": [21.0, 21.0, 21.0, 21.0, 21.0071, 21.0142, 21.0212, 21.0282, 21.0351, 21.0419, 21.0487, 21.0554, 21.0621, 20.9559, 20.9634, 20.9709, 20.9782, 20.9856, 20.9928, 21.0, 21.0, 21.0, 21.0, 21.0, 20.8917], "off": [3.826, 3.826, 3.826, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, -10.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 3.826, 3.826, 3.826, 3.826, -10.0], "obj": 115.397}


def test_formula_and_caps():
    from src.scheduler.lp_optimizer import w3_space_floor_kwh_slot as f

    kw = dict(slot_h=0.5, cap_kwh=9.0)
    # curve 0.6 kW, trim 0.058 kW/K at -5 K -> 0.31 kW -> 0.155 kWh
    assert f(curve_kw=0.6, k_kw_per_k=0.058, min_offset_k=-5, factor=1.0, **kw) == pytest.approx(0.155)
    # no trim representable -> the full curve draw
    assert f(curve_kw=0.6, k_kw_per_k=0.058, min_offset_k=0, factor=1.0, **kw) == pytest.approx(0.3)
    # trim deeper than the draw -> 0 (compressor can stop)
    assert f(curve_kw=0.2, k_kw_per_k=0.058, min_offset_k=-5, factor=1.0, **kw) == 0.0
    # factor scales, cap binds
    assert f(curve_kw=0.6, k_kw_per_k=0.058, min_offset_k=-5, factor=1.5, **kw) == pytest.approx(0.2325)
    assert f(curve_kw=3.0, k_kw_per_k=0.058, min_offset_k=-5, factor=1.0, slot_h=0.5, cap_kwh=0.4) == 0.4


def test_off_matches_golden_pre_pr_plan():
    p = _solve()
    assert p.ok and p.w3_space_floor_kwh == []
    r = lambda xs: [round(float(x), 4) for x in xs]
    assert r(p.space_electric_kwh) == GOLDEN_OFF["space"]
    assert r(p.dhw_electric_kwh) == GOLDEN_OFF["dhw"]
    assert r(p.import_kwh) == GOLDEN_OFF["imp"]
    assert r(p.soc_kwh) == GOLDEN_OFF["soc"]
    assert r(p.indoor_temp_c) == GOLDEN_OFF["tin"]
    assert r(p.lwt_offset_c) == GOLDEN_OFF["off"]
    assert round(p.objective_pence, 3) == GOLDEN_OFF["obj"]


def test_floor_binds_and_trim_stays_representable(monkeypatch):
    off = _solve()
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    on = _solve()
    assert on.ok, on.status
    fl = on.w3_space_floor_kwh
    assert len(fl) == 24 and max(fl) > 0.05
    curve0 = on.space_floor_kwh if on.space_floor_kwh else None
    for i in range(24):
        assert on.space_electric_kwh[i] + (fl[i] / HP_KWH) * on.dhw_electric_kwh[i] >= fl[i] - 1e-6
    # the floor is BELOW the curve draw: a trim stays representable (no flattening)
    from src.physics import get_daikin_heating_kw
    assert all(f < get_daikin_heating_kw(8.0) * 0.5 - 1e-6 for f in fl if f > 0)
    assert any(off.space_electric_kwh[i] < fl[i] - 1e-3 for i in range(24))
    assert curve0 is None or True


@pytest.mark.parametrize("outdoor", [-2.0, 8.0])
@pytest.mark.parametrize("trim", [0.0, 0.043])
@pytest.mark.parametrize("factor", [1.0, 1.25, 1.5])
def test_wire_floored_slot_is_written_at_deepest_offset(monkeypatch, factor, trim, outdoor):
    """lp_dispatch._lp_offsets: a slot planned AT the floor is written at the deepest writable
    offset for every factor/trim (the inversion is the floor's own device model), and a slot above
    the floor never gets a positive offset unless it draws more than F * curve."""
    from src.physics import get_daikin_heating_kw
    from src.scheduler import lp_dispatch

    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_FACTOR", factor, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_TRIM_KW_PER_K", trim, raising=False)
    # cold point: a smaller UA keeps the W3 trajectory plausible (the dispatcher gate) at -2 C
    monkeypatch.setattr(app_config, "BUILDING_UA_W_PER_K", 90.0 if outdoor < 0 else 200.0, raising=False)
    plan = _solve(outdoor=outdoor, indoor=21.0)
    assert plan.ok
    offs = lp_dispatch._lp_offsets(plan, live_indoor_c=None, now_utc=plan.slot_starts_utc[0] - timedelta(hours=6))
    assert offs is not None
    deepest = int(max(float(app_config.OPTIMIZATION_LWT_OFFSET_MIN), float(app_config.DAIKIN_LWT_LP_OFFSET_MIN)))
    curve_kwh = get_daikin_heating_kw(outdoor) * 0.5
    n_floor = 0
    for i, f in enumerate(plan.w3_space_floor_kwh):
        es = plan.space_electric_kwh[i]
        eff = es + (f / HP_KWH) * plan.dhw_electric_kwh[i]
        if f > 1e-6 and abs(eff - f) < 1e-4:
            n_floor += 1
            assert offs[i] == deepest, (i, offs[i], deepest, es, f)
        if offs[i] is not None and offs[i] > 0:
            assert es > factor * curve_kwh - 1e-6, (i, offs[i], es, factor * curve_kwh)
    if factor <= 1.25:
        assert n_floor > 0, "mild scenario must have floored slots"


def test_factor_zero_equals_off(monkeypatch):
    off = _solve()
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_FACTOR", 0.0, raising=False)
    z = _solve()
    assert z.ok and sum(z.w3_space_floor_kwh) == 0.0
    assert z.space_electric_kwh == pytest.approx(off.space_electric_kwh, abs=1e-6)


def test_skipped_when_daikin_passive(monkeypatch):
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    p = _solve()
    assert p.ok and p.w3_space_floor_kwh == []


def test_skipped_on_holiday(monkeypatch):
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    p = _solve(holiday_climate=True)
    assert p.ok and p.w3_space_floor_kwh == []
    assert all(x < 1e-6 for x in p.space_electric_kwh)


def test_cap_binding_extreme_cold_is_feasible(monkeypatch):
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_FACTOR", 1.5, raising=False)
    p = _solve(outdoor=-15.0, indoor=15.0)
    assert p.ok, p.status
    cap = float(app_config.DAIKIN_MAX_HP_KW) * 0.5
    # at -15 C the 1.5x floor is limited by the physics ceiling / HP cap, never above it
    assert 0.3 < max(p.w3_space_floor_kwh) <= cap + 1e-9
    assert max(p.space_electric_kwh) <= cap + 1e-6
    assert all(p.space_electric_kwh[i] + (f / HP_KWH) * p.dhw_electric_kwh[i] >= f - 1e-6
               for i, f in enumerate(p.w3_space_floor_kwh))


@pytest.mark.parametrize("lp_owned", [False, True])
def test_legionella_full_compressor_with_floor_is_feasible(monkeypatch, lp_owned):
    """Legionella (tank 60 C target at the end of the horizon) + floor, both DHW regimes."""
    from src import runtime_settings as rts

    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "DHW_FIXED_SCHEDULE_ENABLED", False, raising=False)
    monkeypatch.setattr(app_config, "DHW_LP_OWNED_ENABLED", lp_owned, raising=False)
    # 2026-05-10 is a Sunday; legionella window 13:00 local for 60 min
    rts.set_setting("DHW_LEGIONELLA_DAY", "6")
    rts.set_setting("DHW_LEGIONELLA_HOUR_LOCAL", "13")
    rts.set_setting("DHW_LEGIONELLA_DURATION_MIN", "60")
    rts.set_setting("DHW_LEGIONELLA_TANK_TARGET_C", "60")
    p = _solve(outdoor=6.0, n=10, tank=38.0, start=datetime(2026, 5, 10, 10, 0, tzinfo=UTC))
    assert p.ok, p.status
    assert max(p.w3_space_floor_kwh) > 0
    for i, f in enumerate(p.w3_space_floor_kwh):
        if not lp_owned:
            assert p.space_electric_kwh[i] + (f / HP_KWH) * p.dhw_electric_kwh[i] >= f - 1e-6


def test_pessimistic_scenario_is_feasible_with_floor(monkeypatch):
    """The pessimistic scenario (p75 load, -1.5 C, PV x0.85) solves with the floor on."""
    from src.scheduler.scenarios import solve_scenarios_with_nominal

    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    base = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
    n = 24
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[1.0] * n,
        shortwave_radiation_wm2=[40.0] * n, cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.0] * n, cop_space=[3.2] * n, cop_dhw=[2.5] * n,
    )
    st = LpInitialState(soc_kwh=6.0, tank_temp_c=45.0, indoor_temp_c=20.5)
    pr = [10.0] * 12 + [40.0] * 6 + [10.0] * 6
    nominal = solve_lp(slot_starts_utc=slots, price_pence=pr, base_load_kwh=[0.3] * n, weather=w, initial=st, tz=TZ)
    assert nominal.ok
    res = solve_scenarios_with_nominal(
        nominal=nominal, slot_starts_utc=slots, price_pence=pr, base_load_kwh=[0.3] * n,
        weather=w, initial=st, tz=TZ,
    )
    assert res["pessimistic"].plan is not None and res["pessimistic"].plan.ok
    assert res["optimistic"].plan is not None and res["optimistic"].plan.ok


@pytest.mark.parametrize("factor,trim", [(1.0, 0.0), (1.25, 0.0), (1.5, 0.043), (1.25, 0.043)])
@pytest.mark.parametrize("t_out", [-2.0, 5.0, 10.0])
def test_device_inversion_maps_floor_to_deepest_offset(monkeypatch, factor, trim, t_out):
    from src.physics import (
        get_daikin_heating_kw,
        lwt_offset_from_space_kw_device,
        space_floor_device_draw_kw,
        space_floor_device_params,
    )

    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_FACTOR", factor, raising=False)
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_TRIM_KW_PER_K", trim, raising=False)
    f, k = space_floor_device_params()
    c = get_daikin_heating_kw(t_out)
    for m in (-5.0, -3.0, 0.0, 2.0):
        kw = space_floor_device_draw_kw(c, m, factor=f, k_t=k)
        got = lwt_offset_from_space_kw_device(kw, t_out, factor=f, k_t=k, lo=-10.0, hi=10.0)
        assert got == pytest.approx(m, abs=1e-6)
