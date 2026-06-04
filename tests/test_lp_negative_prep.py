"""Negative-tariff pre-positioning: pre-negative battery export-drain (1B),
export<0 safety (1C), and DHW boost-to-max energy budgeting (1A)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.scheduler.lp_dispatch import lp_plan_to_slots
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    # Don't let terminal-SoC value discourage draining.
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "normal", raising=False)


def _weather(starts, pv=0.0) -> WeatherLpSeries:
    n = len(starts)
    return WeatherLpSeries(
        slot_starts_utc=starts,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[pv] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )


def _starts(n: int):
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def _solve(prices, export, *, soc=4.5, tank=48.0, base=0.3, pv=0.0):
    starts = _starts(len(prices))
    return solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=[base] * len(prices),
        weather=_weather(starts, pv=pv),
        initial=LpInitialState(soc_kwh=soc, tank_temp_c=tank, soc_source="t", tank_source="t"),
        tz=ZoneInfo("UTC"),
        export_price_pence=export,
    )


# ── 1B: pre-negative battery export-drain ────────────────────────────────────

def test_pre_negative_export_drains_battery(monkeypatch):
    """High export price + negative window ahead + battery charged → the LP
    drains the battery to the grid (export > PV) on pre-window slots."""
    monkeypatch.setattr(app_config, "LP_PRE_NEGATIVE_PREP_ENABLED", True)
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 12)
    n = 24
    # Slots 0-7 positive import (15p), HIGH export (25p). Slots 8-9 negative (-8p).
    prices = [15.0] * 8 + [-8.0] * 2 + [15.0] * 14
    export = [25.0] * 8 + [25.0] * 2 + [4.0] * 14
    plan = _solve(prices, export, soc=4.5, pv=0.0)
    assert plan.ok, plan.status
    drained = sum(plan.export_kwh[i] for i in range(8))
    discharged = sum(plan.battery_discharge_kwh[i] for i in range(8))
    assert drained > 0.5, f"expected battery export-drain before negative, got {drained:.3f} kWh"
    assert discharged > 0.5, f"expected battery discharge to feed the drain, got {discharged:.3f}"
    # Labelled pre_negative_export (not peak_export) → bypasses robustness filter.
    slots = lp_plan_to_slots(plan)
    assert any(s.kind == "pre_negative_export" for s in slots[:8])
    assert all(s.kind != "peak_export" for s in slots)


def test_pre_negative_export_off_keeps_no_battery_export(monkeypatch):
    """Flag off → PR D invariant holds (exp <= pv_use, no battery export)."""
    monkeypatch.setattr(app_config, "LP_PRE_NEGATIVE_PREP_ENABLED", False)
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 12)
    prices = [15.0] * 8 + [-8.0] * 2 + [15.0] * 14
    export = [25.0] * 24
    plan = _solve(prices, export, soc=4.5, pv=0.0)
    assert plan.ok, plan.status
    # PV=0 → any export would be battery; with the flag off there must be none.
    assert sum(plan.export_kwh) < 0.05, f"battery exported with flag off: {sum(plan.export_kwh):.3f}"


def test_pre_negative_export_drains_below_old_margin(monkeypatch):
    """No arbitrary margin gate: a sub-2p export rate still drains when the
    economics work out — selling now plus the paid refill during the negative
    window beats the round-trip loss. The removed
    ``LP_PRE_NEGATIVE_EXPORT_MARGIN_PENCE=2p`` cliff would have wrongly blocked
    this and is exactly the kind of spurious toggle we eliminated."""
    monkeypatch.setattr(app_config, "LP_PRE_NEGATIVE_PREP_ENABLED", True)
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 12)
    # Battery near-full → no headroom to absorb the long negative window unless
    # it drains first. Sell at 1.5p (below the old 2p gate), then get paid 8p to
    # refill that freed capacity. The drained energy is recovered in-window, so
    # the only cost is round-trip loss — comfortably beaten.
    prices = [15.0] * 8 + [-8.0] * 4 + [15.0] * 12
    export = [1.5] * 24
    plan = _solve(prices, export, soc=9.5, pv=0.0)
    assert plan.ok, plan.status
    drained = sum(plan.export_kwh[i] for i in range(8))
    assert drained > 0.3, f"sub-margin export should still drain on good economics, got {drained:.3f}"


def test_pre_negative_export_declines_when_uneconomic(monkeypatch):
    """The OBJECTIVE (not a threshold gate) decides: with a negligible export
    value, a shallow negative window, and a heavy cycle penalty, draining loses
    money so the LP declines — even though pre-negative export is *eligible*."""
    monkeypatch.setattr(app_config, "LP_PRE_NEGATIVE_PREP_ENABLED", True)
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 12)
    monkeypatch.setattr(app_config, "LP_CYCLE_PENALTY_PENCE_PER_KWH", 5.0)
    prices = [15.0] * 8 + [-0.5] * 2 + [15.0] * 14  # shallow negative (tiny paid refill)
    export = [0.2] * 24  # negligible export value
    plan = _solve(prices, export, soc=4.5, pv=0.0)
    assert plan.ok, plan.status
    assert sum(plan.export_kwh) < 0.1, (
        f"should not drain when the gain can't cover cycle cost: {sum(plan.export_kwh):.3f}"
    )


# ── 1C: never export when export price is negative ───────────────────────────

def test_no_export_when_export_price_negative(monkeypatch):
    """Negative Outgoing rate → exp==0 (don't pay to export); surplus PV curtails."""
    monkeypatch.setattr(app_config, "LP_PRE_NEGATIVE_PREP_ENABLED", False)
    n = 12
    prices = [15.0] * n
    export = [-5.0] * n  # negative export price everywhere
    # Lots of PV, no load headroom → would normally spill to export.
    plan = _solve(prices, export, soc=4.9, pv=2.0, base=0.1)
    assert plan.ok, plan.status
    assert sum(plan.export_kwh) < 0.05, f"exported at negative export price: {sum(plan.export_kwh):.3f}"


# ── 1A: DHW boost-to-max energy budgeted into the plan ───────────────────────

def test_negative_window_budgets_dhw_boost_to_max(monkeypatch):
    """With DHW pinned, a negative window drives e_dhw to the heater cap (tank
    heated toward MAX) and raises import on those slots."""
    monkeypatch.setattr(app_config, "DHW_FIXED_SCHEDULE_ENABLED", True)
    monkeypatch.setattr(app_config, "LP_PRE_NEGATIVE_PREP_ENABLED", False)
    n = 24
    # Negative overnight window slots 4-7 (02:00-04:00 UTC).
    prices = [15.0] * 4 + [-6.0] * 4 + [15.0] * 16
    export = [5.0] * n
    plan = _solve(prices, export, soc=2.5, tank=45.0, pv=0.0)
    assert plan.ok, plan.status
    boost = sum(plan.dhw_electric_kwh[i] for i in range(4, 8))
    assert boost > 1.5, f"DHW boost energy not budgeted during negative: {boost:.3f} kWh"
    # The tank trajectory should climb toward MAX across the window.
    assert plan.tank_temp_c[8] >= 55.0, f"tank not driven up during boost: {plan.tank_temp_c[8]:.1f}"
    # Import covers the boost (PV=0, discharge locked during negative).
    assert sum(plan.import_kwh[i] for i in range(4, 8)) > 1.5
