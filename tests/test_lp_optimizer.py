"""PuLP MILP optimizer (V9) unit tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch):
    """Keep CI fast and deterministic."""
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    # Disable inverter stress and MPC knobs that add constraint complexity
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    # Disable terminal-SoC soft-cost by default so legacy tests evaluate against
    # the same objective they were written for. Tests that exercise the soft-cost
    # set this knob explicitly.
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)


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
    base = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    n = 12
    slots, w = _series(n, base)
    prices = [12.0] * n
    base_load = [0.4] * n
    # indoor_temp_c at 20.5 (= INDOOR_SETPOINT_C − 0.5) matches the solver's terminal floor
    # so short horizons remain feasible under the tighter LWT offset cap.
    st = LpInitialState(soc_kwh=4.0, tank_temp_c=44.0, indoor_temp_c=20.5)
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
    base = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    n = 24
    slots, w = _series(n, base)
    prices = [30.0] * n
    base_load = [0.3] * n
    # Tank starts at the new DHW_TEMP_COMFORT_C ceiling (48°C) for positive-price slots.
    st = LpInitialState(soc_kwh=8.0, tank_temp_c=48.0, indoor_temp_c=21.0)
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
    base = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    n = 16
    slots, w = _series(n, base)
    prices = [8.0] * n
    base_load = [0.45] * n
    # indoor_temp_c must be close to INDOOR_SETPOINT_C (21°C) — the solver has a
    # hard terminal floor of INDOOR_SETPOINT_C-0.5 that short horizons can't recover from.
    st = LpInitialState(soc_kwh=3.0, tank_temp_c=42.0, indoor_temp_c=20.5)
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
    base = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    n = 20
    slots, w = _series(n, base)
    prices = [8.0] * n
    base_load = [0.3] * n
    # indoor_temp_c at 20.5 = terminal floor; otherwise short horizons can't recover.
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=20.5)
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


def test_terminal_soc_value_changes_terminal_soc(monkeypatch):
    """S10.1 (#168): LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH > 0 should produce a
    higher terminal SoC than 0 when prices favour draining (early peak followed
    by cheap window). Without the soft-cost, LP exports the peak and refills
    cheap; with a high enough soft-cost, LP keeps the battery and skips the
    marginal arbitrage.
    """
    base = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    n = 20
    slots, w = _series(n, base)
    # First 4 slots at 30p (peak), then 16 slots at 8p (cheap refill window).
    # With terminal value=0, LP exports during peak and refills cheap → terminal SoC ≈ floor.
    # With terminal value=20p/kWh, the 22p spread is < 20p → keep battery.
    prices = [30.0] * 4 + [8.0] * (n - 4)
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=6.0, tank_temp_c=45.0, indoor_temp_c=20.5)

    monkeypatch.setattr(app_config, "LP_SOC_FINAL_KWH", 1.0)

    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    plan_off = solve_lp(
        slot_starts_utc=slots, price_pence=prices, base_load_kwh=base_load,
        weather=w, initial=st, tz=ZoneInfo("Europe/London"),
    )
    assert plan_off.ok
    soc_off = plan_off.soc_kwh[-1]

    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 20.0)
    plan_on = solve_lp(
        slot_starts_utc=slots, price_pence=prices, base_load_kwh=base_load,
        weather=w, initial=st, tz=ZoneInfo("Europe/London"),
    )
    assert plan_on.ok
    soc_on = plan_on.soc_kwh[-1]

    assert soc_on > soc_off, (
        f"Soft-cost should keep more battery: terminal SoC value=0 → {soc_off:.2f}, "
        f"value=20 → {soc_on:.2f}; expected value=20 to be higher."
    )


def test_cop_curve_interpolation_monotonic():
    """Heating-mode COP increases with outdoor temperature (curve from config)."""
    from src.config import cop_at_temperature, parse_cop_curve_csv

    curve = parse_cop_curve_csv("-7:1.8,2:2.6,20:4.2")
    assert cop_at_temperature(curve, -7.0) < cop_at_temperature(curve, 20.0)
    assert cop_at_temperature(curve, 2.0) <= cop_at_temperature(curve, 12.0)


def test_pv_curtailment_slack_prevents_infeasible():
    """Huge PV vs capped export should still be feasible via curtailment."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
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
    # Tank capped at DHW_TEMP_COMFORT_C=48 °C for positive-price slots.
    st = LpInitialState(soc_kwh=9.0, tank_temp_c=48.0, indoor_temp_c=22.0)
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
    base = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
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
    base = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    n = 12
    slots, w = _series(n, base)
    # Flat price: without stress, solver is free to charge at max every slot
    prices = [10.0] * n
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=2.0, tank_temp_c=45.0, indoor_temp_c=20.5)

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
    base = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)  # 04:00 UTC = outside occupied
    n = 16  # 8 hours — enough to heat and maintain
    slots, w = _series(n, base)
    prices = [6.0] * n  # cheap — heat pump should run
    base_load = [0.3] * n
    # indoor_temp_c must be close to INDOOR_SETPOINT_C (21°C) — see terminal-floor note in earlier test.
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=40.0, indoor_temp_c=20.5)
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


def test_cbc_solver_used_by_default():
    """CBC should be the default solver backend."""
    from src.scheduler.lp_optimizer import _make_solver

    solver = _make_solver()
    assert type(solver).__name__ == "PULP_CBC_CMD"


def test_negative_price_max_charges_battery(monkeypatch):
    """All-negative prices: LP should charge the battery to ~full within one horizon.

    Export rate is zeroed so grid-arbitrage (charge + discharge-to-export) isn't preferred
    over simply absorbing the negative-price energy into the battery.
    """
    monkeypatch.setattr(app_config, "EXPORT_RATE_PENCE", 0.0)
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    n = 8  # 4 hours × negative
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[12.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,  # no PV — all charge must come from grid
        cop_space=[3.2] * n,
        cop_dhw=[2.7] * n,
    )
    prices = [-5.0] * n
    base_load = [0.3] * n
    initial_soc = 2.0
    st = LpInitialState(soc_kwh=initial_soc, tank_temp_c=45.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    # With no export reward, the LP's only profit is absorbing import at negative price —
    # so the battery should end the horizon at or near soc_max.
    assert plan.soc_kwh[-1] >= float(app_config.BATTERY_CAPACITY_KWH) - 0.5, (
        f"battery should be ~full after all-negative horizon, got SoC={plan.soc_kwh[-1]:.2f}"
    )


def test_dhw_ceiling_48_when_positive_price():
    """Tank must stay ≤ DHW_TEMP_COMFORT_C (48°C) during positive-price slots."""
    base = datetime(2026, 4, 23, 6, 0, tzinfo=UTC)
    n = 24  # 12 hours
    slots, w = _series(n, base)
    # First half positive, second half negative
    prices = [10.0] * 12 + [-3.0] * 12
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    comfort_c = float(app_config.DHW_TEMP_COMFORT_C)
    for i in range(n):
        if prices[i] >= 0:
            assert plan.tank_temp_c[i + 1] <= comfort_c + 0.1, (
                f"slot {i} (price={prices[i]}p): tank={plan.tank_temp_c[i+1]:.2f}°C > {comfort_c}"
            )


def test_no_grid_to_battery_before_plunge():
    """Morning positive + afternoon negative: morning grid→battery flow must be 0."""
    base = datetime(2026, 4, 23, 6, 0, tzinfo=UTC)
    n = 16  # 8 hours
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[12.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,  # no PV — grid is the only import source
        cop_space=[3.2] * n,
        cop_dhw=[2.7] * n,
    )
    prices = [12.0] * 8 + [-2.0] * 8  # positive morning, negative afternoon
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    # Morning positive slots: battery charge must be ≤ pv_use (=0), so ≈0.
    for i in range(8):
        assert plan.battery_charge_kwh[i] <= plan.pv_use_kwh[i] + 1e-3, (
            f"morning slot {i}: chg={plan.battery_charge_kwh[i]:.3f} > pv={plan.pv_use_kwh[i]:.3f}"
        )


def test_lwt_offset_capped_at_5():
    """LP lwt_offset_c must never exceed OPTIMIZATION_LWT_OFFSET_MAX (5 °C)."""
    base = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    n = 12
    # Outdoor=10 °C from the default _series helper is below the DAIKIN_WEATHER_CURVE_HIGH_C
    # threshold, so space_floor_kwh > 0 and the LP schedules space heating in every slot —
    # exercising the LWT cap.
    slots, w = _series(n, base)
    prices = [8.0] * n
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    lwt_max_cfg = float(app_config.OPTIMIZATION_LWT_OFFSET_MAX)
    assert plan.lwt_offset_c, "expected lwt_offset_c populated when heating runs"
    assert max(plan.lwt_offset_c) <= lwt_max_cfg + 0.1, (
        f"lwt_offset_c.max={max(plan.lwt_offset_c):.2f} > cap {lwt_max_cfg}"
    )


def test_dis_zero_during_negative_slots():
    """Fix B: LP must plan dis=0 for every slot where price < 0, even when a
    subsequent high-price slot would make discharge nominally profitable.
    """
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[12.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.2] * n,
        cop_dhw=[2.7] * n,
    )
    # 4 negative slots followed by 4 peak slots (tempting discharge during negatives
    # if the LP were allowed to swing battery both ways within the horizon).
    prices = [-3.0] * 4 + [35.0] * 4
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=9.0, tank_temp_c=45.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    for i in range(4):
        assert plan.battery_discharge_kwh[i] < 1e-3, (
            f"negative slot {i} (price={prices[i]}p): dis={plan.battery_discharge_kwh[i]:.4f} must be 0"
        )


def test_stress_cost_inactive_during_negative_prices(monkeypatch):
    """Fix A: the inverter-stress penalty must be suppressed during negative slots,
    so solving with or without stress yields the same battery trajectory when all
    slots are negative-priced.
    """
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[12.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.2] * n,
        cop_dhw=[2.7] * n,
    )
    prices = [-2.0] * n
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=2.0, tank_temp_c=45.0, indoor_temp_c=20.5)

    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    plan_no = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 1.0)
    plan_yes = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan_no.ok and plan_yes.ok
    # Objective must match: if the stress gate works, the stress-cost term
    # contributes 0 during all-negative horizons, so the optimum is identical
    # to the no-stress baseline. (Per-slot chg distribution may differ because
    # any ordering that saturates the battery is an equivalent optimum.)
    assert abs(plan_no.objective_pence - plan_yes.objective_pence) < 0.05, (
        f"objective differs — no_stress={plan_no.objective_pence:.3f} "
        f"vs stress={plan_yes.objective_pence:.3f} — stress gate not fully suppressing"
    )
    # Total energy absorbed into the battery should also match.
    tot_no = sum(plan_no.battery_charge_kwh)
    tot_yes = sum(plan_yes.battery_charge_kwh)
    assert abs(tot_no - tot_yes) < 0.05, (
        f"total chg differs — no_stress={tot_no:.3f} vs stress={tot_yes:.3f}"
    )


def test_negative_run_has_no_charge_gaps(monkeypatch):
    """Fix A + B in concert: a contiguous negative-price run should produce
    battery_charge_kwh > EPS in every slot until SoC saturates — no alternating
    zero-charge gaps caused by stress-cost smoothing.
    """
    # Keep stress cost at production default (0.10); if Fix A is correctly gating
    # it during negatives, this test passes. Without Fix A it fails.
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.10)
    monkeypatch.setattr(app_config, "EXPORT_RATE_PENCE", 0.0)
    # Pin battery DC + grid AC throughputs to the pre-H1-5.0 defaults so 5 kWh
    # headroom (gross ~5.21 kWh charged after rt-eff) fits in 2 slots of
    # full-power charging. Test asserts contiguous charging; capacity-driven
    # fragmentation under the real H1-5.0 hardware (max_batt_kwh=2.5,
    # fuse_kwh=2.5) is a separate (correct) LP behaviour, out of scope here.
    monkeypatch.setattr(app_config, "MAX_INVERTER_KW", 6.0)
    monkeypatch.setattr(app_config, "FOX_FORCE_CHARGE_MAX_PWR", 10000)
    base = datetime(2026, 4, 23, 11, 30, tzinfo=UTC)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[12.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.2] * n,
        cop_dhw=[2.7] * n,
    )
    # Mimic real Agile plunge: most-negative slot at index 2, shallower at 1, 4.
    prices = [-1.24, -0.90, -1.30, -1.30, -1.10, -0.95]
    base_load = [0.3] * n
    # SoC=5 kWh (50%): battery has ~5 kWh of headroom — ~2 slots of full-power charging.
    st = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    # The bug this guards: alternating chg>0 / chg=0 pattern within a negative run
    # ("SelfUse gaps"). Characterise the chg sequence as binary (EPS-threshold)
    # and count transitions from "charging" back to "idle". A correct plan has
    # at most ONE such transition (saturation point); an alternation pattern has
    # two or more.
    is_charging = [c > 0.05 for c in plan.battery_charge_kwh]
    transitions_charging_to_idle = sum(
        1 for i in range(1, n) if is_charging[i - 1] and not is_charging[i]
    )
    assert transitions_charging_to_idle <= 1, (
        f"LP produced alternating chg/idle gaps during negative run: "
        f"chg={[round(c, 2) for c in plan.battery_charge_kwh]}, "
        f"transitions={transitions_charging_to_idle}"
    )
    # Battery must actually saturate (if it doesn't, the LP didn't absorb enough
    # negative-price energy).
    cap = float(app_config.BATTERY_CAPACITY_KWH)
    assert plan.soc_kwh[-1] >= cap - 0.5, (
        f"terminal SoC {plan.soc_kwh[-1]:.2f} < {cap - 0.5} — battery didn't saturate"
    )
    # And no dis in any negative slot (Fix B).
    for i in range(n):
        assert plan.battery_discharge_kwh[i] < 1e-3
