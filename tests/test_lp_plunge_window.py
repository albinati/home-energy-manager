"""LP pre-plunge discipline must respect ``LP_PLUNGE_PREP_HOURS``.

Audit finding 2026-05-02: the unbounded look-ahead constraint
(``forbid grid→battery on positive slot whenever ANY negative slot exists
in the horizon``) starved the battery on days where the next negative
window was >24 h away. Only 33 % of charge slots landed in the cheap
quartile on the worst day. Fix: bound the look-ahead to a configurable
window (default 12 h).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the test_lp_optimizer.py setup so the LP solves quickly + feasibly."""
    monkeypatch.setattr(app_config, "LP_HIGHS_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)


def _flat_weather(starts: list[datetime], pv_per_slot: float = 0.0) -> WeatherLpSeries:
    """Mild weather with optional PV (use 0 for no-PV scenarios)."""
    n = len(starts)
    return WeatherLpSeries(
        slot_starts_utc=starts,
        temperature_outdoor_c=[15.0] * n,            # mild — minimal heating demand
        shortwave_radiation_wm2=[400.0 * (pv_per_slot > 0)] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[pv_per_slot] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )


def _initial() -> LpInitialState:
    # indoor_temp_c=20.5 matches the LP's terminal floor (INDOOR_SETPOINT_C − 0.5
    # = 21 − 0.5) — keeps the small-horizon LP feasible without forcing space heat.
    return LpInitialState(
        soc_kwh=2.5,                 # 50% of 5 kWh
        tank_temp_c=48.0,
        indoor_temp_c=20.5,
        soc_source="test",
        tank_source="test",
        indoor_source="test",
    )


def _starts(n: int, base: datetime | None = None) -> list[datetime]:
    base = base or datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def test_plunge_window_blocks_charge_when_negative_within_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative slot 4 hours away (within 12 h window): grid charge on the
    positive slot should be forbidden (only PV → battery, but PV=0 here)."""
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 12)
    from zoneinfo import ZoneInfo

    n = 24  # 12 hours
    starts = _starts(n)
    # Slots 0–7: positive cheap (5p). Slots 8–9: negative (-5p). Rest: standard (15p).
    prices = [5.0] * 8 + [-5.0] * 2 + [15.0] * 14
    base_load = [0.3] * n

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=_flat_weather(starts),
        initial=_initial(),
        tz=ZoneInfo("UTC"),
        export_price_pence=[5.0] * n,
    )
    assert plan.ok, f"LP failed: {plan.status}"

    # On positive slots before the negative window: no grid→battery charge
    # allowed. PV is 0 here, so chg should be 0.
    for i in range(8):
        assert plan.battery_charge_kwh[i] < 0.05, (
            f"slot {i} ({prices[i]}p): chg={plan.battery_charge_kwh[i]:.3f} "
            "should be ~0 because pre-plunge discipline forbids grid→battery "
            "with negative window 4 h away"
        )


def test_plunge_window_allows_charge_when_negative_far_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative slot 16 hours away (OUTSIDE 6 h window): grid charge on
    cheap positive slots SHOULD be allowed.

    Pre-fix: the unbounded look-ahead would block this — slot 0 sees the
    far-away negative and refuses cheap charge. With the bounded window,
    slot 0 only looks 12 slots (6 h) ahead and sees nothing → charge OK.
    """
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 6)
    from zoneinfo import ZoneInfo

    n = 48  # 24 hours
    starts = _starts(n)
    # Slots 0–4: super cheap (1p). Slot 32 (16 h ahead): negative. Rest: normal (15p).
    prices = [1.0] * 5 + [15.0] * 27 + [-5.0] * 2 + [15.0] * 14
    base_load = [0.2] * n

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=_flat_weather(starts, pv_per_slot=0.3),
        initial=_initial(),
        tz=ZoneInfo("UTC"),
        export_price_pence=[5.0] * n,
    )
    assert plan.ok, f"LP failed: {plan.status}"

    # At least ONE of the cheap slots 0–4 should see grid charge — the LP's
    # economically rational choice is to load up at 1p. Pre-fix: blocked.
    cheap_charge = sum(plan.battery_charge_kwh[i] for i in range(5))
    assert cheap_charge > 0.5, (
        f"Cheap-slot charge total: {cheap_charge:.3f} kWh. Pre-plunge bound "
        "should allow grid→battery on the cheap slots when the negative "
        "window is >6 h away. Got essentially zero."
    )


def test_plunge_window_zero_disables_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LP_PLUNGE_PREP_HOURS=0 → constraint never fires. Same as the case where
    no negative slots exist anywhere in the horizon."""
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 0)
    from zoneinfo import ZoneInfo

    n = 24
    starts = _starts(n)
    prices = [5.0] * 8 + [-5.0] * 2 + [15.0] * 14  # negative slots present
    base_load = [0.5] * n

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=_flat_weather(starts),
        initial=_initial(),
        tz=ZoneInfo("UTC"),
        export_price_pence=[5.0] * n,
    )
    assert plan.ok, f"LP failed: {plan.status}"
    # With constraint OFF, LP should grid-charge cheap slots even with
    # negatives ahead — pure cost optimization
    cheap_charge = sum(plan.battery_charge_kwh[i] for i in range(8))
    assert cheap_charge > 0.5, (
        f"With LP_PLUNGE_PREP_HOURS=0, cheap-slot grid charge should be "
        f"unrestricted. Got {cheap_charge:.3f} kWh."
    )
