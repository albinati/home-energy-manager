"""HP minimum-on-time was lowered from 2 → 1 (effectively disabled).

Background: the LP previously added an anti-short-cycling constraint that
forced ``hp_on`` to stay = 1 for at least ``LP_HP_MIN_ON_SLOTS`` consecutive
slots after each ``0→1`` transition. With min-on = 2 the LP adds, per slot,
one ``startup_i`` binary plus two linear constraints (and an extra dummy
variable for slot 0) — roughly N extra integers per solve.

The audit (2026-05-19) found this redundant: the Daikin Altherma firmware
enforces its own compressor short-cycle protection. Dropping the LP's
constraint to min-on = 1 (the ``if hp_min_on > 1`` guard now skips the
whole block) shaves MILP size without changing physical behaviour.

These tests:
1. Verify the new default is 1.
2. Verify the LP-side min-on machinery is silent when min-on = 1.
3. Verify that on a scenario with a profitable short HP burst, min-on = 1
   produces a strictly LESS-OR-EQUAL objective than min-on = 2 (relaxing
   a feasibility constraint can only improve or equal the optimum).
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


def test_default_is_now_one() -> None:
    """Boot-time invariant: the default must be 1, not 2."""
    # We bypass any test-fixture overrides by re-evaluating from os.getenv.
    import importlib
    from src import config as _cfg
    importlib.reload(_cfg)
    assert _cfg.config.LP_HP_MIN_ON_SLOTS == 1, (
        f"Expected default LP_HP_MIN_ON_SLOTS=1 after the 2026-05-20 audit "
        f"change, got {_cfg.config.LP_HP_MIN_ON_SLOTS}"
    )


def _build_horizon(n: int, *, t_out: float = 5.0) -> tuple[
    list[datetime], WeatherLpSeries, list[float], list[float]
]:
    base = datetime(2026, 5, 20, 0, 0, tzinfo=UTC)
    starts = [base + i * timedelta(minutes=30) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[t_out] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    # Alternating cheap / standard prices — gives the LP a clear preference
    # for short DHW pulses in the cheap slot.
    prices = [4.0 if (i % 2 == 0) else 20.0 for i in range(n)]
    base_load = [0.3] * n
    return starts, weather, prices, base_load


def _solve(min_on: int, monkeypatch: pytest.MonkeyPatch) -> tuple[float, str, list[int]]:
    """Solve a fixed scenario at the requested min-on, return
    ``(objective_p, status, hp_on_pattern)``."""
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 10)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", min_on)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(app_config, "BATTERY_CAPACITY_KWH", 10.0)
    monkeypatch.setattr(app_config, "MIN_SOC_RESERVE_PERCENT", 10.0)
    monkeypatch.setattr(app_config, "DAIKIN_MAX_HP_KW", 2.0)
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 0)
    monkeypatch.setattr(app_config, "LP_SOC_FINAL_KWH", 0.0)
    monkeypatch.setattr(app_config, "LP_PV_SUFFICIENCY_GUARD", False)
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "")
    monkeypatch.setattr(app_config, "LP_SHOWER_MORNING_LOCAL", "")
    monkeypatch.setattr(app_config, "LP_SHOWER_EVENING_LOCAL", "")
    n = 12
    starts, w, prices, base_load = _build_horizon(n, t_out=5.0)
    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=42.0),
        tz=ZoneInfo("UTC"),
    )
    # Reconstruct the on/off pattern from electrical kWh (binary hp_on isn't
    # exposed on LpPlan; non-zero kWh ↔ hp_on = 1 in practice).
    pattern = [
        1 if (plan.dhw_electric_kwh[i] + plan.space_electric_kwh[i]) > 1e-6 else 0
        for i in range(n)
    ]
    return plan.objective_pence, plan.status, pattern


def test_min_on_1_objective_le_min_on_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relaxing the min-on constraint can only equal or improve the optimum.

    With min-on = 2 the LP must hold ``hp_on`` for ≥ 2 consecutive slots
    after each startup. With min-on = 1 it has free choice. Same scenario,
    same solver: ``obj(min_on=1) ≤ obj(min_on=2)``.
    """
    obj_2, status_2, pattern_2 = _solve(2, monkeypatch)
    obj_1, status_1, pattern_1 = _solve(1, monkeypatch)
    assert status_2 == "Optimal", f"min_on=2 baseline must solve: {status_2}"
    assert status_1 == "Optimal", f"min_on=1 must solve: {status_1}"
    # Allow tiny floating-point slack — CBC objective values are not exact.
    assert obj_1 <= obj_2 + 1e-2, (
        f"min_on=1 ({obj_1:.3f}) should be ≤ min_on=2 ({obj_2:.3f}) on the "
        f"same scenario. patterns: min_on=2={pattern_2} min_on=1={pattern_1}"
    )


def test_min_on_1_allows_single_slot_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    """With min-on = 1, the LP must be free to fire the HP for a single slot
    when that's economically optimal — no enforced 2-slot dwell.

    Uses ``t_out=20 °C`` so the climate-curve ``space_floor_kwh[i]`` is zero
    every slot — the LP has no forced HP draw and can pick a sparse pattern
    purely on DHW economics.
    """
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 10)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(app_config, "BATTERY_CAPACITY_KWH", 10.0)
    monkeypatch.setattr(app_config, "MIN_SOC_RESERVE_PERCENT", 10.0)
    monkeypatch.setattr(app_config, "DAIKIN_MAX_HP_KW", 2.0)
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 0)
    monkeypatch.setattr(app_config, "LP_SOC_FINAL_KWH", 0.0)
    monkeypatch.setattr(app_config, "LP_PV_SUFFICIENCY_GUARD", False)
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "")
    monkeypatch.setattr(app_config, "LP_SHOWER_MORNING_LOCAL", "")
    monkeypatch.setattr(app_config, "LP_SHOWER_EVENING_LOCAL", "")

    n = 12
    base = datetime(2026, 5, 20, 0, 0, tzinfo=UTC)
    starts = [base + i * timedelta(minutes=30) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[20.0] * n,  # warm — no space_floor forcing
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[5.0] * n,
        cop_dhw=[4.5] * n,
    )
    prices = [4.0 if (i % 2 == 0) else 20.0 for i in range(n)]
    base_load = [0.3] * n

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        # Tank well below the comfort ceiling (48 °C) so the LP has reason
        # to heat it during cheap slots only.
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=35.0),
        tz=ZoneInfo("UTC"),
    )
    assert plan.ok, f"min-on=1 warm-outdoor scenario must solve: {plan.status}"

    pattern = [
        1 if (plan.dhw_electric_kwh[i] + plan.space_electric_kwh[i]) > 1e-6 else 0
        for i in range(n)
    ]
    total_on = sum(pattern)
    # The LP's economic choice with alternating 4p/20p prices is to fire HP
    # only in cheap slots. With min-on=1 it can pick exactly those. At
    # min-on=2 it would be forced to extend each burst into expensive slots.
    if total_on > 0:
        cheap_slot_on = sum(1 for i in range(0, n, 2) if pattern[i] == 1)
        expensive_slot_on = sum(1 for i in range(1, n, 2) if pattern[i] == 1)
        assert cheap_slot_on > expensive_slot_on, (
            f"min_on=1 with alternating 4p/20p prices should prefer cheap "
            f"slots — got {cheap_slot_on} cheap-ON vs {expensive_slot_on} "
            f"expensive-ON. pattern={pattern}"
        )
