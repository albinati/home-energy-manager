"""PuLP MILP optimizer (V9) unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from zoneinfo import ZoneInfo

from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch):
    """Keep CI fast — use HiGHS with a short time limit (falls back to CBC if unavailable)."""
    monkeypatch.setattr(app_config, "LP_HIGHS_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    # Disable inverter stress and MPC knobs that add constraint complexity
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)


def _series(n: int, base: datetime) -> tuple[list[datetime], WeatherLpSeries]:
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[10.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[0.5] * n,
        cop_space=[3.2] * n,
        cop_dhw=[2.7] * n,
    )
    return slots, w


def test_lp_solves_optimal_small_horizon():
    base = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    n = 12
    slots, w = _series(n, base)
    prices = [12.0] * n
    base_load = [0.4] * n
    st = LpInitialState(soc_kwh=4.0, tank_temp_c=44.0, indoor_temp_c=20.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.status == "Optimal"
    assert plan.ok
    assert len(plan.soc_kwh) == n + 1
    assert len(plan.import_kwh) == n


def test_seg_export_bounded_by_pv_and_discharge():
    """Grid export cannot exceed PV use + battery discharge (SEG-style constraint)."""
    base = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    n = 24
    slots, w = _series(n, base)
    prices = [30.0] * n
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=8.0, tank_temp_c=50.0, indoor_temp_c=21.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    for i in range(n):
        assert plan.export_kwh[i] <= plan.pv_use_kwh[i] + plan.battery_discharge_kwh[i] + 1e-3


def test_terminal_soc_at_least_initial(monkeypatch):
    """Without a hard floor, terminal SoC should be at least the initial value."""
    monkeypatch.setattr(app_config, "LP_SOC_FINAL_KWH", 0.0)  # disable hard floor
    base = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    n = 16
    slots, w = _series(n, base)
    prices = [8.0] * n
    base_load = [0.45] * n
    st = LpInitialState(soc_kwh=3.0, tank_temp_c=42.0, indoor_temp_c=19.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    assert plan.soc_kwh[-1] >= st.soc_kwh - 1e-2


def test_terminal_soc_hard_floor(monkeypatch):
    """LP_SOC_FINAL_KWH hard constraint: terminal SoC must be >= configured value."""
    target_soc = 4.0
    monkeypatch.setattr(app_config, "LP_SOC_FINAL_KWH", target_soc)
    base = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    n = 20
    slots, w = _series(n, base)
    prices = [8.0] * n
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=20.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    assert plan.soc_kwh[-1] >= target_soc - 1e-2


def test_cop_curve_interpolation_monotonic():
    """Heating-mode COP increases with outdoor temperature (curve from config)."""
    from src.config import cop_at_temperature, parse_cop_curve_csv

    curve = parse_cop_curve_csv("-7:1.8,2:2.6,20:4.2")
    assert cop_at_temperature(curve, -7.0) < cop_at_temperature(curve, 20.0)
    assert cop_at_temperature(curve, 2.0) <= cop_at_temperature(curve, 12.0)


def test_pv_curtailment_slack_prevents_infeasible():
    """Huge PV vs capped export should still be feasible via curtailment."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[25.0] * n,
        shortwave_radiation_wm2=[900.0] * n,
        cloud_cover_pct=[0.0] * n,
        pv_kwh_per_slot=[3.0] * n,
        cop_space=[4.0] * n,
        cop_dhw=[3.5] * n,
    )
    prices = [5.0] * n
    base_load = [0.2] * n
    st = LpInitialState(soc_kwh=9.0, tank_temp_c=55.0, indoor_temp_c=22.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.status == "Optimal"


def test_smoothing_penalties_and_price_quantize_still_optimal(monkeypatch):
    """TV penalties + price quantization should not break a small feasible model."""
    monkeypatch.setattr(app_config, "LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA", 0.2)
    monkeypatch.setattr(app_config, "LP_HP_POWER_TV_PENALTY_PENCE_PER_KWH_DELTA", 0.3)
    monkeypatch.setattr(app_config, "LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA", 0.1)
    monkeypatch.setattr(app_config, "LP_PRICE_QUANTIZE_PENCE", 2.0)
    base = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    n = 10
    slots, w = _series(n, base)
    prices = [10.2, 11.7, 10.1, 12.3, 9.8, 10.0, 11.1, 10.9, 12.0, 10.5]
    base_load = [0.35] * n
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=48.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.status == "Optimal"
    assert plan.ok
    assert len(plan.price_pence) == n
    assert plan.price_pence[0] == 10.2


def test_inverter_stress_reduces_peak_battery_power(monkeypatch):
    """Inverter stress penalty should discourage bang-bang max charge/discharge."""
    base = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    n = 12
    slots, w = _series(n, base)
    # Flat price: without stress, solver is free to charge at max every slot
    prices = [10.0] * n
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=2.0, tank_temp_c=45.0, indoor_temp_c=20.0)

    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    plan_no_stress = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )

    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 1.0)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_SEGMENTS", 8)
    plan_stress = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )

    assert plan_no_stress.ok and plan_stress.ok
    max_power_no_stress = max(
        plan_no_stress.battery_charge_kwh[i] + plan_no_stress.battery_discharge_kwh[i]
        for i in range(n)
    )
    max_power_stress = max(
        plan_stress.battery_charge_kwh[i] + plan_stress.battery_discharge_kwh[i]
        for i in range(n)
    )
    # Stress cost should not increase peak power (should equal or reduce it)
    assert max_power_stress <= max_power_no_stress + 1e-3


def test_simplified_hp_model_continuous_power(monkeypatch):
    """New HP model: e_dhw[i] should be continuous, not forced to discrete bucket values."""
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)  # no min-on for this test
    monkeypatch.setattr(app_config, "LP_SOC_FINAL_KWH", 0.0)  # no hard soc floor
    base = datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc)  # 04:00 UTC = outside occupied
    n = 16  # 8 hours — enough to heat and maintain
    slots, w = _series(n, base)
    prices = [6.0] * n  # cheap — heat pump should run
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=40.0, indoor_temp_c=18.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, f"Expected Optimal, got {plan.status}"
    # At least one slot should have DHW heat (tank was below target)
    total_dhw = sum(plan.dhw_electric_kwh)
    assert total_dhw > 0.0, "HP should have heated DHW tank when it was cold"
    # Verify values are bounded correctly by the new continuous model
    hp_max_kwh = getattr(app_config, "DAIKIN_MAX_HP_KW", 2.0) * 0.5
    assert all(
        0 <= v <= hp_max_kwh + 1e-3 for v in plan.dhw_electric_kwh
    ), "DHW kWh per slot exceeded max_hp_kw × 0.5"


def test_highs_solver_used_by_default():
    """HiGHS Python API should be the default solver when available."""
    import pulp

    available = pulp.listSolvers(onlyAvailable=True)
    if "HiGHS" not in available:
        pytest.skip("HiGHS not installed in this environment")

    from src.scheduler.lp_optimizer import _make_solver

    solver = _make_solver()
    # Both HiGHS and HiGHS_CMD are acceptable; check name starts with HiGHS
    solver_name = type(solver).__name__
    assert solver_name.startswith("HiGHS"), f"Expected HiGHS solver, got {solver_name}"


def test_mpc_hours_list_parsed_correctly(monkeypatch):
    """LP_MPC_HOURS_LIST property should parse the comma-separated string."""
    monkeypatch.setattr(app_config, "LP_MPC_HOURS", "6,12,18")
    assert app_config.LP_MPC_HOURS_LIST == [6, 12, 18]

    monkeypatch.setattr(app_config, "LP_MPC_HOURS", "")
    assert app_config.LP_MPC_HOURS_LIST == []

    monkeypatch.setattr(app_config, "LP_MPC_HOURS", "0,23,12,6")
    assert app_config.LP_MPC_HOURS_LIST == [0, 6, 12, 23]
