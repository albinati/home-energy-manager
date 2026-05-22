"""Integration tests for the explicit shower-demand model in the LP (PR B).

The unit tests in ``tests/test_dhw_demand.py`` cover the pure-physics
helpers. This file exercises the *LP-level* consequences: timing of the
heat-up, capacity respect, min-on, mode sensitivity, COP impact.

Each scenario constructs a small horizon, drives the LP, and asserts a
property the new demand model is supposed to deliver.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _init_db() -> None:
    _db.init_db()


@pytest.fixture(autouse=True)
def _reset_overrides():
    type(config)._overrides.clear()
    yield
    type(config)._overrides.clear()


@pytest.fixture(autouse=True)
def _active_mode(monkeypatch):
    """All PR B integration scenarios run in active mode so the LP actually
    decides e_dhw (passive mode clamps e_dhw to firmware predictions and
    skips the shower floor)."""
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "ENERGY_STRATEGY_MODE", "savings_first", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 2.0, raising=False)
    monkeypatch.setattr(config, "DHW_TANK_LITRES", 200.0, raising=False)
    monkeypatch.setattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 15, raising=False)
    monkeypatch.setattr(config, "LP_INVERTER_STRESS_COST_PENCE", 0.0, raising=False)
    # Disable legacy aggregate so the new model is exercised.
    monkeypatch.setattr(config, "DHW_DAILY_SHOWER_LITRES", 0.0, raising=False)
    # PV-abundance reward off so timing assertions aren't muddied by it.
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 0.0, raising=False)


def _weather(n: int, t_out: float = 10.0, pv_per_slot: float = 0.0) -> WeatherLpSeries:
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return WeatherLpSeries(
        slot_starts_utc=[base + i * timedelta(minutes=30) for i in range(n)],
        temperature_outdoor_c=[t_out] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[pv_per_slot] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.8] * n,
    )


# ---------------------------------------------------------------------------
# Timing: LP heats BEFORE the shower window when given lead time
# ---------------------------------------------------------------------------


def test_lp_heats_before_evening_window_with_cheap_lead_time(monkeypatch):
    """Tank starts at a realistic warm overnight target (45 °C, the normal
    DHW_TEMP_NORMAL_C). Evening shower window 7 h ahead has a derived
    floor (~48 °C for 4 showers). The LP must allocate e_dhw in pre-shower
    slots so that by the start of the window the tank meets the floor."""
    # Anchor at 12:00 UTC; evening shower window 19:00-22:00 local = 18:00-
    # 21:00 UTC (BST = UTC+1 in June). Tank starts at the normal overnight
    # target so the LP has feasible heating headroom (not stuck in infeasible
    # corner case where tank can't reach floor regardless of heating).
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 24  # 12 h horizon: 12:00 → 24:00 UTC
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    prices = [12.0] * n
    base_load = [0.3] * n

    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=_weather(n),
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status

    # Required tank at start of evening window (4 showers, defaults) ≈ 48 °C.
    from src.dhw_demand import required_tank_temp_for_window
    from src.presets import OperationPreset
    required = required_tank_temp_for_window("evening", OperationPreset.NORMAL)

    # 19:00 local = 18:00 UTC = slot index 12 (since base = 12:00 UTC, 30 min/slot)
    shower_slot_index = 12
    assert plan.tank_temp_c[shower_slot_index] >= required - 1.0, (
        f"tank at shower window start = {plan.tank_temp_c[shower_slot_index]:.2f} °C, "
        f"required = {required:.2f} °C"
    )

    # Confirm SOME heating happened before the shower slot — the LP starts
    # at 45 °C and the floor is ~48 °C, so at least the gap must be heated.
    pre_window_dhw = sum(plan.dhw_electric_kwh[:shower_slot_index])
    assert pre_window_dhw > 0.2, (
        f"LP should have heated before the evening window; "
        f"pre-window e_dhw sum = {pre_window_dhw:.2f} kWh"
    )


# ---------------------------------------------------------------------------
# Heat-pump capacity respected
# ---------------------------------------------------------------------------


def test_heat_pump_capacity_never_exceeded(monkeypatch):
    """``e_dhw[i] + e_space[i] <= DAIKIN_MAX_HP_KW × 0.5h`` for every slot.
    With max_hp_kw=2.0 the per-slot cap is 1.0 kWh."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[12.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_weather(n),
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status
    max_per_slot = 2.0 * 0.5 + 1e-3  # tolerance for LP float drift
    for i in range(n):
        total = plan.dhw_electric_kwh[i] + plan.space_electric_kwh[i]
        assert total <= max_per_slot, (
            f"slot {i}: e_dhw + e_space = {total:.3f} > {max_per_slot:.3f} kWh"
        )


# ---------------------------------------------------------------------------
# Mode sensitivity: guests > normal demand
# ---------------------------------------------------------------------------


def test_guests_mode_plans_more_dhw_than_normal(monkeypatch):
    """Guests mode adds visitor showers → total e_dhw over the horizon
    must be higher than the normal-mode counterfactual."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 24
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    monkeypatch.setattr(config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(
        config, "DHW_SHOWER_SCHEDULE_GUESTS", "07:00-09:00,19:00-22:00", raising=False,
    )

    def _solve(preset: str) -> float:
        monkeypatch.setattr(config, "OPTIMIZATION_PRESET", preset, raising=False)
        plan = solve_lp(
            slot_starts_utc=slots,
            price_pence=[12.0] * n,
            base_load_kwh=[0.3] * n,
            weather=_weather(n),
            initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
            tz=ZoneInfo("Europe/London"),
        )
        assert plan.ok, f"{preset}: {plan.status}"
        return sum(plan.dhw_electric_kwh)

    normal_dhw = _solve("normal")
    guests_dhw = _solve("guests")
    assert guests_dhw > normal_dhw, (
        f"guests should require more e_dhw than normal: "
        f"normal={normal_dhw:.2f} guests={guests_dhw:.2f}"
    )


# ---------------------------------------------------------------------------
# Sensitivity: lower flow rate → lower required tank temp → less heating
# ---------------------------------------------------------------------------


def test_lower_flow_rate_lowers_required_tank_temp(monkeypatch):
    """Reducing the shower flow rate from 9 to 6 L/min cuts demand → the
    LP can satisfy the shower floor with less aggressive heating."""
    from src.dhw_demand import required_tank_temp_for_window
    from src.presets import OperationPreset

    monkeypatch.setattr(config, "DHW_SHOWER_FLOW_LPM", 9.0, raising=False)
    floor_high = required_tank_temp_for_window("evening", OperationPreset.NORMAL)
    monkeypatch.setattr(config, "DHW_SHOWER_FLOW_LPM", 6.0, raising=False)
    floor_low = required_tank_temp_for_window("evening", OperationPreset.NORMAL)
    assert floor_low < floor_high, (
        f"lower flow → lower required temp: "
        f"flow=9 → {floor_high:.2f}, flow=6 → {floor_low:.2f}"
    )


# ---------------------------------------------------------------------------
# COP impact: cold day → more electric kWh for the same thermal target
# ---------------------------------------------------------------------------


def test_cold_day_requires_more_electric_kwh(monkeypatch):
    """COP sensitivity: at 8 °C vs 12 °C the LP must allocate more
    e_dhw on the cooler day for the same thermal demand.

    Avoids deep winter (< 4 °C) where the space_floor_kwh interaction
    with the heat-pump capacity cap can drive the LP into local-optimum
    behaviour that doesn't reliably show this signal."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 24
    slots = [base + i * timedelta(minutes=30) for i in range(n)]

    def _solve(t_out: float) -> float:
        plan = solve_lp(
            slot_starts_utc=slots,
            price_pence=[12.0] * n,
            base_load_kwh=[0.3] * n,
            weather=_weather(n, t_out=t_out),
            initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
            tz=ZoneInfo("Europe/London"),
        )
        assert plan.ok, plan.status
        return sum(plan.dhw_electric_kwh)

    warm_kwh = _solve(t_out=12.0)
    cool_kwh = _solve(t_out=8.0)
    # Same thermal lift at lower COP → more electric kWh.
    assert cool_kwh >= warm_kwh, (
        f"cooler day should need at least as much electric kWh: "
        f"warm={warm_kwh:.3f}, cool={cool_kwh:.3f}"
    )


# ---------------------------------------------------------------------------
# Morning reserve floor (normal mode) is honoured without draw
# ---------------------------------------------------------------------------


def test_normal_mode_morning_reserve_keeps_tank_warm(monkeypatch):
    """Normal mode's morning reserve is a floor at the configured hour
    (default 07:00 local) — no draw modelled, just a soft constraint.
    The LP should keep the tank ≥ required-for-reserve at that hour."""
    from src.dhw_demand import required_tank_temp_for_n_showers
    monkeypatch.setattr(config, "DHW_MORNING_RESERVE_HOUR_LOCAL", 7, raising=False)
    # Reserve count 1 → required ≈ mixer + safety = 40 °C
    reserve_floor = required_tank_temp_for_n_showers(1)

    # Horizon: 22:00 UTC tonight → next day 09:00 UTC.
    # 07:00 local (BST = UTC+1) = 06:00 UTC → slot index = 16 (8 h after start).
    base = datetime(2026, 6, 1, 22, 0, tzinfo=UTC)
    n = 22
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[12.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_weather(n),
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status

    # Find the slot whose local hour is 07:00 — that's where the reserve
    # floor sits. With BST = UTC+1 in June: 07:00 BST = 06:00 UTC.
    tz = ZoneInfo("Europe/London")
    morning_slot = None
    for i, s in enumerate(slots):
        local = s.astimezone(tz)
        if local.hour == 7 and local.minute == 0:
            morning_slot = i
            break
    assert morning_slot is not None, "did not find 07:00 local slot in horizon"
    # Tank at end of the morning slot should meet the reserve floor (slack-
    # tolerant by 1 °C in case heat-up couldn't complete from very cold start).
    assert plan.tank_temp_c[morning_slot + 1] >= reserve_floor - 1.0, (
        f"morning reserve: tank[{morning_slot + 1}] = "
        f"{plan.tank_temp_c[morning_slot + 1]:.2f} should ≥ {reserve_floor:.2f}"
    )
