"""The shower-window tank floor (``tank[i+1] >= t_min_dhw``) is now soft.

Before PR #344 this constraint was HARD on every slot inside a shower window.
That stacked with the LP firing 5 min before the evening shower window (the
``tier_boundary`` MPC trigger at 21:25 BST) to drive 8 of 9 above-reserve
infeasibilities in the 60-day audit window. The pattern: horizon slot 0
starts at 21:30 BST, which IS the shower window. Tank temperature at the
solve moment is sometimes too low to lift to 45 °C in a single 30-min slot
(physics floor: ~10 K of heating per slot at max HP draw and COP 2.5). With
a hard constraint the solver returned Infeasible. With this PR's soft
constraint (slack variable + 50 p / K-slot penalty), the LP heats as fast
as physically possible and surfaces the unavoidable deficit as a quantified
slack — no Infeasible.

Penalty calibration: each saved kWh of HP electricity costs ~12-35 p (cheap
to peak Agile). Each K saved on tank temp avoids ~0.1 kWh. So saving 5 K
saves ~1 p. The default 50 p / K-slot penalty exceeds any conceivable
saving, ensuring the LP only breaches the floor when physically forced
(not when it would just be slightly cheaper to skip).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _init_db() -> None:
    _db.init_db()


@pytest.fixture(autouse=True)
def _active_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match the prod active-mode shape that hits this bug class."""
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(app_config, "ENERGY_STRATEGY_MODE", "savings_first")
    monkeypatch.setattr(app_config, "BATTERY_CAPACITY_KWH", 10.36)
    monkeypatch.setattr(app_config, "MIN_SOC_RESERVE_PERCENT", 10.0)
    monkeypatch.setattr(app_config, "DAIKIN_MAX_HP_KW", 2.0)
    monkeypatch.setattr(app_config, "DHW_TANK_LITRES", 200.0)
    monkeypatch.setattr(app_config, "DHW_DAILY_SHOWER_LITRES", 0.0)  # isolate the floor
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 0)
    monkeypatch.setattr(app_config, "LP_SOC_FINAL_KWH", 0.0)
    monkeypatch.setattr(app_config, "LP_PV_SUFFICIENCY_GUARD", False)
    # Evening shower starts EXACTLY at the horizon slot-0 boundary.
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "21:30-22:30")
    monkeypatch.setattr(app_config, "LP_SHOWER_MORNING_LOCAL", "")
    monkeypatch.setattr(app_config, "LP_SHOWER_EVENING_LOCAL", "")


def _build(
    n: int, *, t_out: float = -2.0,
) -> tuple[list[datetime], WeatherLpSeries, list[float], list[float]]:
    """Horizon starts at 21:30 BST (20:30 UTC). Slot 0 IS inside the shower
    window; subsequent slots span overnight then the next day.

    Default ``t_out=-2 °C`` because solve_lp recomputes COP from the
    Daikin curve (ignoring any cop_dhw / cop_space in WeatherLpSeries),
    so the actual lift-per-slot is driven by ``cop_at_temperature(t_out)``.
    At -2 °C the COP is ~2.9 and the per-slot lift cap is ~12 K — meaning
    a cold tank at 28 °C cannot reach the 45 °C shower floor in one slot.
    """
    base = datetime(2026, 5, 19, 20, 30, tzinfo=UTC)
    starts = [base + i * timedelta(minutes=30) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[t_out] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.0] * n,  # IGNORED — solve_lp recomputes from temp
        cop_dhw=[2.5] * n,
    )
    prices = [12.0] * n
    base_load = [0.3] * n
    return starts, weather, prices, base_load


def test_cold_tank_in_slot_0_shower_window_is_now_feasible() -> None:
    """The reproduction of the 8-of-9 above-reserve infeasibility pattern.

    Tank starts at 28 °C (matches 2026-05-14 20:05 + 2026-05-18 19:55 prod
    incidents — see audit memo). Shower window starts at slot 0. Lifting
    from 28 → 45 °C requires +17 K thermal in 30 min; max HP electrical
    is 1 kWh × COP 2.5 = 2.5 kWh thermal = ~3 K with the 200 L tank's
    heat capacity at this slot length. So slot-0 floor is PHYSICALLY
    impossible. Pre-#344 this returned Infeasible.

    Post-#344 the LP solves with a positive slack reflecting the
    unavoidable deficit, and the rest of the horizon plans cleanly.
    """
    n = 12
    starts, w, prices, base_load = _build(n)
    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=28.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, (
        f"LP should be Optimal with soft shower floor (status={plan.status}). "
        f"Pre-#344 this exact scenario reproduced the residual-class "
        f"infeasibility audit."
    )
    # The LP should have lifted the tank as fast as physically possible.
    # tank[1] = tank[0] + lift - loss - draw. With tank[0]=28 and ~3 K max
    # lift per slot, tank[1] should be ~30-31 °C — short of the 45 °C floor
    # by 14-15 K. That deficit IS the slack value (the diagnostic signal).
    assert plan.tank_temp_c[1] < 45.0, (
        f"Expected tank[1] < 45 (physics floor unreachable in 1 slot from "
        f"28 °C), got {plan.tank_temp_c[1]}"
    )
    # And the LP must have planned DHW heating in slot 0 to lift as much as
    # possible — verify e_dhw[0] is at or near max_hp_kwh.
    max_hp_kwh = float(app_config.DAIKIN_MAX_HP_KW) * 0.5
    assert plan.dhw_electric_kwh[0] >= max_hp_kwh * 0.5, (
        f"LP should max-out DHW heating in slot 0 to minimize the deficit, "
        f"got e_dhw[0] = {plan.dhw_electric_kwh[0]:.3f} (max {max_hp_kwh:.3f})"
    )


def test_warm_tank_in_slot_0_shower_window_no_slack() -> None:
    """When tank starts warm enough to satisfy the shower floor naturally,
    the slack should be effectively zero (LP doesn't pay the penalty when
    not forced to).
    """
    n = 12
    starts, w, prices, base_load = _build(n)
    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=47.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, f"LP status: {plan.status}"
    # Tank starts ABOVE the floor; the LP should keep it there without
    # paying slack penalty. Tank[1] (end of slot 0) should be ≥ 45.
    assert plan.tank_temp_c[1] >= 45.0 - 1e-3, (
        f"warm-tank scenario: tank[1] = {plan.tank_temp_c[1]} should ≥ 45 "
        f"without using slack"
    )


def test_2026_05_14_replay_now_feasible() -> None:
    """Synthetic replay of the 2026-05-14 20:05 prod incident:
    SoC=54 %, tank=**28 °C**, outdoor=**7 °C**, evening shower window
    starting at 21:30 BST.

    Pre-#344 returned Infeasible (and PR #338 didn't exist yet so the
    heuristic fallback fired). Post-#344 must solve, possibly with a
    small slack (at 7 °C the COP is ~4.3, so 28 → 44.3 is feasible —
    a ~0.7 K deficit). Either way, no Infeasible.
    """
    n = 24
    base = datetime(2026, 5, 14, 19, 5, tzinfo=UTC)  # 20:05 BST
    starts = [base + i * timedelta(minutes=30) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[7.0] * n,  # matches prod telemetry
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    prices = [18.0] * n
    base_load = [0.3] * n

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        # SoC 54 % of 10.36 ≈ 5.6 kWh. Tank 28 °C — well below shower floor.
        initial=LpInitialState(soc_kwh=5.6, tank_temp_c=28.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, (
        f"2026-05-14 replay returned {plan.status} — the shower-floor soft "
        f"constraint should rescue this incident class"
    )
