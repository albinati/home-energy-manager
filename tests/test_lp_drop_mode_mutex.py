"""The LP no longer enforces the single-mode-per-slot mutex (m_dhw + m_space ≤ 1).

Background: the Daikin Altherma firmware interleaves DHW and space heating
within a 30-minute slot when both demands are present (e.g. 10 min DHW lift,
then 20 min radiator circulation). The previous LP modelled this as a hard
mutex — one mode per slot — which:

* misrepresented the hardware,
* stacked with the shower-floor + space-floor + tank-hi constraints to push
  the LP infeasible under tight conditions (the 2026-05-19 incident pattern),
* added 2 N extra binary variables to the MILP for no physical reason.

The mutex was removed in the post-#342 audit. Total HP electrical is still
capped at ``max_hp_kwh × hp_on`` per slot (aggregate physical limit), and
``e_space`` keeps its climate-curve physics ceiling. The only change: the
LP can now split that aggregate between DHW and space heating in any ratio
within a single slot.
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
def _fast_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    # Active mode is where mode mutex used to apply — make sure the test exercises it.
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    # No shower windows / legionella in this test — isolate the mode-mutex behaviour.
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "")
    monkeypatch.setattr(app_config, "LP_SHOWER_MORNING_LOCAL", "")
    monkeypatch.setattr(app_config, "LP_SHOWER_EVENING_LOCAL", "")
    monkeypatch.setattr(app_config, "BATTERY_CAPACITY_KWH", 10.0)
    monkeypatch.setattr(app_config, "MIN_SOC_RESERVE_PERCENT", 10.0)
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 0)
    monkeypatch.setattr(app_config, "LP_PV_SUFFICIENCY_GUARD", False)


def _starts(n: int) -> list[datetime]:
    base = datetime(2026, 5, 19, 1, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def _cold_weather(starts: list[datetime], *, t_out: float = 4.0) -> WeatherLpSeries:
    """Cold + zero PV — forces the climate-curve space floor > 0 every slot."""
    n = len(starts)
    return WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[t_out] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )


def test_lp_can_co_heat_dhw_and_space_in_same_slot() -> None:
    """With the mutex removed, the LP must be able to run BOTH e_dhw > 0 AND
    e_space > 0 in the same slot when both demands are present. This is the
    behaviour the Daikin firmware actually exhibits — the LP is now consistent
    with physical reality.

    Scenario: cold outdoor (space_floor > 0 every slot), tank starts cold so
    DHW heating is economically beneficial, single cheap-price horizon. With
    the OLD mutex the LP would have been forced to alternate modes across
    slots; without it, the LP can co-heat in the cheapest slot."""
    n = 6
    starts = _starts(n)
    weather = _cold_weather(starts, t_out=4.0)
    # Flat cheap prices so the LP has no incentive to defer heating across slots.
    prices = [5.0] * n
    base_load = [0.3] * n

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        # Tank starts well below comfort so DHW heating is wanted; SoC mid.
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=30.0),
        tz=ZoneInfo("UTC"),
    )
    assert plan.ok, f"LP must solve with cold weather + DHW demand: {plan.status}"

    # The defining assertion: at least one slot must have BOTH e_dhw > 0 AND
    # e_space > 0. Under the old mutex this was impossible.
    co_heat_slots = [
        i for i in range(n)
        if plan.dhw_electric_kwh[i] > 1e-6 and plan.space_electric_kwh[i] > 1e-6
    ]
    assert co_heat_slots, (
        f"Expected at least one slot with both DHW and space heating active, "
        f"but found none. Per-slot (dhw_kwh, space_kwh): "
        f"{list(zip(plan.dhw_electric_kwh, plan.space_electric_kwh, strict=True))}"
    )


def test_lp_aggregate_hp_cap_still_enforced() -> None:
    """The mutex was removed; the aggregate cap stays. ``e_dhw + e_space`` in
    any single slot must not exceed ``max_hp_kwh = DAIKIN_MAX_HP_KW × 0.5``.
    """
    max_hp_kwh = float(app_config.DAIKIN_MAX_HP_KW) * 0.5
    n = 6
    starts = _starts(n)
    weather = _cold_weather(starts, t_out=2.0)
    prices = [5.0] * n
    base_load = [0.3] * n

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=30.0),
        tz=ZoneInfo("UTC"),
    )
    assert plan.ok, f"LP must solve: {plan.status}"

    for i in range(n):
        total_hp_kwh = plan.dhw_electric_kwh[i] + plan.space_electric_kwh[i]
        assert total_hp_kwh <= max_hp_kwh + 1e-6, (
            f"slot {i}: e_dhw + e_space = {total_hp_kwh:.4f} > "
            f"max_hp_kwh = {max_hp_kwh:.4f} — aggregate cap violated"
        )


def test_lp_space_ceil_still_enforced() -> None:
    """Climate-curve physics ceiling on e_space is preserved (was previously
    gated by m_space; now applied directly). On a cold horizon, the LP must
    NOT exceed ``get_daikin_heating_kw(t_out, lwt_offset_delta=10) × 0.5``
    for ``e_space`` in any slot.
    """
    from src.physics import get_daikin_heating_kw

    n = 8
    starts = _starts(n)
    weather = _cold_weather(starts, t_out=-2.0)
    prices = [5.0] * n
    base_load = [0.4] * n

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
        tz=ZoneInfo("UTC"),
    )
    assert plan.ok, f"LP must solve: {plan.status}"

    lwt_off_max = float(app_config.OPTIMIZATION_LWT_OFFSET_MAX)
    for i, t in enumerate(weather.temperature_outdoor_c):
        # space_ceil per slot mirrors the LP's own formula
        max_hp_kwh = float(app_config.DAIKIN_MAX_HP_KW) * 0.5
        space_ceil = min(
            get_daikin_heating_kw(t, lwt_offset_delta=lwt_off_max) * 0.5,
            max_hp_kwh,
        )
        assert plan.space_electric_kwh[i] <= space_ceil + 1e-6, (
            f"slot {i}: e_space = {plan.space_electric_kwh[i]:.4f} > "
            f"space_ceil = {space_ceil:.4f} at t_out={t}°C — climate-curve "
            f"physics ceiling violated"
        )
