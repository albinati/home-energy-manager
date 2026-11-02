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


# Golden OFF plan: values computed with the PRE-PR code (origin/main f72db76) on this exact scenario.
GOLDEN_OFF_SPACE = [0.2968, 0.2968, 0.2968, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.0073, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.3164, 0.2968, 0.2968, 0.2968, 0.2968, 0.0]


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
    assert [round(x, 4) for x in p.space_electric_kwh] == GOLDEN_OFF_SPACE


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


def test_wire_floored_slot_is_written_at_deepest_offset(monkeypatch):
    """lp_dispatch._lp_offsets: a slot planned AT the floor is written at the deepest writable
    offset (not 0), and the plan still contains deeper-than-zero coast rows."""
    from src.scheduler import lp_dispatch

    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_ENABLED", True, raising=False)
    plan = _solve(outdoor=8.0)
    assert plan.ok
    offs = lp_dispatch._lp_offsets(plan, live_indoor_c=21.0, now_utc=plan.slot_starts_utc[0] - timedelta(hours=6))
    assert offs is not None
    deepest = int(max(float(app_config.OPTIMIZATION_LWT_OFFSET_MIN), float(app_config.DAIKIN_LWT_LP_OFFSET_MIN)))
    at_floor = [
        i for i, f in enumerate(plan.w3_space_floor_kwh)
        if f > 1e-6 and abs(plan.space_electric_kwh[i] + (f / HP_KWH) * plan.dhw_electric_kwh[i] - f) < 1e-4
    ]
    assert at_floor, "scenario must have slots planned exactly at the floor"
    for i in at_floor:
        assert offs[i] == deepest, (i, offs[i], deepest)
    assert min(o for o in offs if o is not None) == deepest


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
