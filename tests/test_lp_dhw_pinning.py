"""Tests for PR K2 — LP DHW pinning.

When ``DHW_FIXED_SCHEDULE_ENABLED=True``, the LP solver must pin
``e_dhw[i]`` and ``tank_temp[i+1]`` to the dhw_policy forecast instead
of optimizing them. This removes the K1 drift where the LP planned to
heat the tank (lifting tank to 60 °C in its model) but the dispatch
layer skipped emitting those actions — resulting in over-aggressive
Force Charge slots and misleading audit data.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import dhw_policy
from src.config import config


TZ_LOCAL = ZoneInfo("Europe/London")


def _make_weather(slots, pv_kwh):
    from src.weather import WeatherLpSeries
    n = len(slots)
    return WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[18.0] * n,
        shortwave_radiation_wm2=[600.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=pv_kwh,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )


def _solve(slots, prices, pv, base_load, init_soc=8.0, init_tank=40.0,
           export_prices=None):
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    init = LpInitialState(soc_kwh=init_soc, tank_temp_c=init_tank)
    return solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=_make_weather(slots, pv),
        initial=init,
        tz=TZ_LOCAL,
        export_price_pence=export_prices,
    )


@pytest.fixture(autouse=True)
def _pin_enabled(monkeypatch):
    """Default: pinning enabled. Individual tests opt out via monkeypatch.

    ``DAIKIN_CONTROL_MODE`` / ``OPTIMIZATION_PRESET`` are runtime-settings
    PROPERTIES. A bare ``setattr`` on them routes through the property setter →
    ``Config._rt_set`` → the CLASS-LEVEL ``_overrides`` dict, which monkeypatch
    cannot undo — it leaks into every later test and beats even ``setenv``.
    (That leak is why ``test_lp_solves_in_passive_mode`` below could not set
    passive at all.) Use ``monkeypatch.setitem(config._overrides, ...)``, which
    restores properly.
    """
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setitem(config._overrides, "DHW_WARMUP_START_HOUR_LOCAL", 13)
    monkeypatch.setitem(config._overrides, "DHW_SETBACK_START_HOUR_LOCAL", 22)
    monkeypatch.setitem(config._overrides, "DHW_TEMP_NORMAL_C", 45.0)
    monkeypatch.setattr(config, "DHW_TEMP_SETBACK_C", 37.0, raising=False)
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "normal")
    yield


# ---------------------------------------------------------------------------
# forecast_dhw_load_per_slot
# ---------------------------------------------------------------------------


def test_forecast_returns_correct_lengths():
    """e_dhw has length N; tank has length N+1 (matches LP's tank[]) array."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(10)]
    e_dhw, tank = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert len(e_dhw) == 10
    assert len(tank) == 11


def test_forecast_warmup_window_has_higher_load_than_setback():
    """Warmup window slots draw more electric than setback slots."""
    # Slots 12:00, 12:30, 13:00, 13:30, ..., 23:00 BST
    # warmup starts at 13:00 local; setback at 22:00 local
    base = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(24)]
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    # 13:00 local = slot 2 (warmup transition) — biggest pulse
    # 22:00 local = slot 20 (setback)
    assert e_dhw[2] > e_dhw[20]
    # warmup maintenance slots (3..19) > setback maintenance (slot 21..)
    assert e_dhw[5] > e_dhw[21]


def test_forecast_vacation_returns_zeros():
    """Vacation mode: no LP-attributed DHW load (firmware-only legionella
    is out of scope for the LP horizon by convention)."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(10)]
    e_dhw, tank = dhw_policy.forecast_dhw_load_per_slot(slots, mode="vacation")
    assert all(v == 0.0 for v in e_dhw)


def test_forecast_guests_keeps_warmup_loads():
    """Guests mode: tank at NORMAL 24h, so warmup-maintenance level
    constantly (no setback discount)."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(24)]
    e_dhw, tank = dhw_policy.forecast_dhw_load_per_slot(slots, mode="guests")
    # No setback dip — all slots should be at least warmup-maintenance
    for v in e_dhw:
        assert v >= 0.04 - 1e-6  # WARMUP_MAINTENANCE_KWH
    # tank temp always NORMAL=45 in guests
    assert all(t == 45.0 for t in tank)


def test_forecast_normal_tank_trajectory():
    """tank temp follows the schedule: NORMAL during warmup window,
    SETBACK during overnight."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(24)]
    _, tank = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    # Slot 2 starts at 13:00 BST (warmup) — tank should be at NORMAL
    # Slot 20 starts at 22:00 BST (setback start) — tank should be at SETBACK
    assert tank[2] == 45.0
    assert tank[20] == 37.0


# ---------------------------------------------------------------------------
# LP integration — pinning is in effect when flag on
# ---------------------------------------------------------------------------


def test_lp_pinning_enforces_e_dhw_matches_forecast():
    """With flag on, LP's e_dhw values match the dhw_policy forecast
    exactly — LP cannot 'plan' tank heating beyond the forecast."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots,
        prices=[10.0] * n,        # cheap import (would normally tempt LP to heat tank)
        pv=[3.0] * n,             # abundant PV
        base_load=[0.3] * n,
        init_soc=9.5,
        init_tank=40.0,
        export_prices=[0.0] * n,  # zero export → only DHW would be value-positive
    )
    assert plan.ok, plan.status
    # Even with strong incentives to heat tank, e_dhw is pinned to forecast.
    expected_e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    for i in range(n):
        assert abs(plan.dhw_electric_kwh[i] - expected_e_dhw[i]) < 1e-3, (
            f"slot {i}: e_dhw={plan.dhw_electric_kwh[i]:.3f} "
            f"expected={expected_e_dhw[i]:.3f} (pinning failed)"
        )


def test_lp_pinning_enforces_tank_temp_matches_forecast():
    """tank_temp_c values in the LP solution match the dhw_policy schedule
    target — no more fictional 60 °C in the audit trail."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots,
        prices=[10.0] * n,
        pv=[3.0] * n,
        base_load=[0.3] * n,
        init_soc=9.5,
        init_tank=40.0,
    )
    assert plan.ok, plan.status
    _, expected_tank = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    # Compare boundaries 1..N (boundary 0 is pinned to initial.tank_temp_c)
    for i in range(n):
        assert abs(plan.tank_temp_c[i + 1] - expected_tank[i + 1]) < 1e-3, (
            f"tank boundary {i+1}: got={plan.tank_temp_c[i+1]:.1f} "
            f"expected={expected_tank[i+1]:.1f}"
        )


def test_lp_pinning_disabled_resumes_free_optimization(monkeypatch):
    """When flag off, LP optimizes e_dhw / tank freely as before
    (regression: don't accidentally affect the legacy path)."""
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 10.0, raising=False)
    monkeypatch.setattr(config, "DHW_PV_ABUNDANCE_THRESHOLD_KWH", 0.5, raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots,
        prices=[10.0] * n,
        pv=[3.0] * n,
        base_load=[0.3] * n,
        init_soc=9.5,
        init_tank=40.0,
        export_prices=[0.0] * n,
    )
    assert plan.ok
    # Without pinning + with abundance reward + cheap import, LP heats tank
    total_e_dhw = sum(plan.dhw_electric_kwh)
    assert total_e_dhw > 0.5, (
        f"Free LP should heat tank when incentivised; got total={total_e_dhw:.2f}"
    )


def test_lp_pinning_reduces_force_charge_vs_unpinned():
    """The whole point of K2: pinning reduces LP's perceived PV
    consumption (no phantom DHW heating eating PV) → less grid import
    in cheap arbitrage windows. Compare LP grid imports with vs without
    pinning, identical scenario otherwise."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    common_kwargs = dict(
        slots=slots,
        prices=[10.0] * n,
        pv=[2.0] * n,
        base_load=[0.3] * n,
        init_soc=6.0,                # leaves headroom for arbitrage
        init_tank=40.0,
        export_prices=[5.0] * n,
    )
    # With pinning ON (default in fixture)
    plan_pinned = _solve(**common_kwargs)
    assert plan_pinned.ok

    # With pinning OFF (legacy)
    import unittest.mock as _mock
    with _mock.patch.object(config, "DHW_FIXED_SCHEDULE_ENABLED", False):
        with _mock.patch.object(
            config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 10.0,
        ):
            plan_free = _solve(**common_kwargs)
    assert plan_free.ok
    pinned_import = sum(plan_pinned.import_kwh)
    free_import = sum(plan_free.import_kwh)
    # The pinned LP should import less than the free LP because it
    # accurately sees that e_dhw will be small (just maintenance), so the
    # PV available for battery is larger. Equality is OK (no regression);
    # strictly less is the desired direction.
    assert pinned_import <= free_import + 0.01, (
        f"Pinned import {pinned_import:.2f} should be ≤ free import "
        f"{free_import:.2f} kWh (PR K2 promises less phantom DHW load)"
    )


def test_lp_pinning_vacation_mode_e_dhw_zero(monkeypatch):
    """Vacation: forecast gives zero load; LP must reflect that exactly."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots,
        prices=[15.0] * n,
        pv=[2.0] * n,
        base_load=[0.3] * n,
        init_soc=5.0,
        init_tank=40.0,
    )
    assert plan.ok
    for v in plan.dhw_electric_kwh:
        assert v < 1e-3, f"vacation should produce zero e_dhw, got {v:.3f}"


def test_forecast_warm_credit_reduces_first_slots():
    """User-observed case: tank arriving at 52°C (above NORMAL=45) means
    the first warmup/reheat slots have less work to do. The forecast
    should apply a 'warm credit' that reduces e_dhw until consumed."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(8)]

    # Baseline: no initial_tank_c → flat forecast
    e_dhw_flat, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    flat_total = sum(e_dhw_flat)

    # With initial_tank_c=52 (7°C above NORMAL=45)
    # Thermal credit: 7 × 200 × 4186 / 3.6e6 = ~1.63 kWh thermal = ~0.54 kWh electric
    e_dhw_warm, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", initial_tank_c=52.0,
    )
    warm_total = sum(e_dhw_warm)
    credit = flat_total - warm_total
    assert credit == pytest.approx(0.54, abs=0.1), (
        f"Expected ~0.54 kWh warm credit; got {credit:.2f} kWh"
    )


def test_forecast_warm_credit_below_normal_no_effect():
    """Tank arriving AT or below NORMAL → no warm credit (no stored
    excess to offset future heating)."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(8)]
    flat, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    cold, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", initial_tank_c=42.0,  # below NORMAL
    )
    at_normal, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", initial_tank_c=45.0,
    )
    assert sum(cold) == sum(flat)
    assert sum(at_normal) == sum(flat)


def test_forecast_warm_credit_huge_excess_zeros_first_slots():
    """If excess is large enough, the first slots' load goes to zero
    (no over-subtracting into negative)."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(4)]
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", initial_tank_c=80.0,  # huge excess
    )
    # All slots should be non-negative
    for v in e_dhw:
        assert v >= 0.0


def test_lp_pinning_with_warm_initial_tank_reduces_e_dhw(monkeypatch):
    """End-to-end: LP receiving initial.tank_temp_c=52 sees pinned e_dhw
    values smaller than if it had received 45."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan_cold = _solve(
        slots=slots, prices=[15.0] * n, pv=[2.0] * n,
        base_load=[0.3] * n, init_soc=8.0, init_tank=45.0,
    )
    plan_warm = _solve(
        slots=slots, prices=[15.0] * n, pv=[2.0] * n,
        base_load=[0.3] * n, init_soc=8.0, init_tank=52.0,
    )
    assert plan_cold.ok and plan_warm.ok
    cold_dhw = sum(plan_cold.dhw_electric_kwh)
    warm_dhw = sum(plan_warm.dhw_electric_kwh)
    assert warm_dhw < cold_dhw - 0.1, (
        f"Warm-arrival tank should reduce LP e_dhw; cold={cold_dhw:.2f} "
        f"warm={warm_dhw:.2f}"
    )


def test_lp_solves_in_passive_mode(monkeypatch):
    """#639 regression: passive mode must SOLVE, not return Infeasible.

    In passive mode ``e_dhw`` is already hard-pinned to the physics prediction
    (``e_dhw[i] == passive_e_dhw[i]``). The K2 pin adds a SECOND hard equality
    (``e_dhw[i] == pinned_e_dhw[i]``, from the dhw_policy forecast) on the same
    variable, and the two vectors differ in essentially every slot → Infeasible
    on EVERY solve. Fix: ``_dhw_pinned`` is now gated on ``not passive_daikin``.

    This test previously asserted only ``plan is not None`` and its own comment
    said "either outcome is acceptable" — so it passed for months while the mode
    was completely dead. It now asserts the real contract.
    """
    # Runtime-settings property → override via _overrides (setitem restores; a
    # bare setattr would leak class-level). See the _pin_enabled docstring.
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "passive")
    assert config.DAIKIN_CONTROL_MODE == "passive"

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots, prices=[15.0] * n, pv=[2.0] * n,
        base_load=[0.3] * n, init_soc=5.0, init_tank=40.0,
    )
    assert plan is not None
    assert plan.ok, f"passive-mode LP must be feasible, got status={plan.status}"

    # e_dhw follows the PASSIVE physics prediction (standing loss), NOT the
    # dhw_policy pin — the whole point of the guard. The policy pin would put a
    # warmup/setback shape here; passive is a small flat standing-loss draw.
    assert len(plan.dhw_electric_kwh) == n
    assert all(v < 0.15 for v in plan.dhw_electric_kwh), (
        f"passive e_dhw should be the small standing-loss prediction, got "
        f"{plan.dhw_electric_kwh} — looks like the dhw_policy pin leaked back in"
    )


def test_lp_solves_with_a_tank_hotter_than_the_ceiling(monkeypatch):
    """A live tank above DHW_TEMP_MAX_C must not make the LP Infeasible.

    `tank[0] == initial.tank_temp_c` is a hard equality, and `tank` was bounded
    at `[20, DHW_TEMP_MAX_C]` (default 60 °C) — so a real tank above that ceiling
    (routine right after the firmware's own legionella cycle, or a PV-abundance
    lift plus solar gain) collided with the upBound and killed EVERY solve until
    the tank cooled.
    Slot 0 is a MEASUREMENT, not a decision; `soc[0]` already had exactly this
    relaxation and `tank[0]` did not.
    """
    hot = float(config.DHW_TEMP_MAX_C) + 1.0
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots, prices=[15.0] * n, pv=[2.0] * n,
        base_load=[0.3] * n, init_soc=5.0, init_tank=hot,
    )
    assert plan.ok, f"hot tank ({hot} C) must still solve, got status={plan.status}"
    assert plan.tank_temp_c[0] == pytest.approx(hot)

    # ...and the same in passive mode, where the tank has zero LP freedom.
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "passive")
    plan_p = _solve(
        slots=slots, prices=[15.0] * n, pv=[2.0] * n,
        base_load=[0.3] * n, init_soc=5.0, init_tank=hot,
    )
    assert plan_p.ok, f"passive + hot tank must solve, got status={plan_p.status}"


def test_lp_pinning_guests_with_last_slot_in_shower_window_is_feasible(monkeypatch):
    """#422 regression: under K2 pinning the pinned trajectory clamps
    ``tank[n]`` to the dhw_policy value (e.g. NORMAL 45 °C), but the terminal
    DHW floor was still active and (guests mode, last slot in the evening shower
    window) used ``TARGET_DHW_TEMP_MIN_GUESTS_C-2 = 53`` — directly contradicting
    the pin → Infeasible. Fix: skip the terminal floor when ``_dhw_pinned``.
    Without the guard this asserts ``plan.ok=False``.
    """
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    monkeypatch.setattr(config, "TARGET_DHW_TEMP_MIN_GUESTS_C", 55.0, raising=False)
    # Horizon ending inside the evening shower window (20:00-22:00 BST): 2 slots
    # at 21:00 + 21:30 local, both in-window, so the LAST slot is in-window.
    base = datetime(2026, 6, 1, 21, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    n = 2
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots, prices=[15.0] * n, pv=[1.0] * n,
        base_load=[0.3] * n, init_soc=5.8, init_tank=45.0,
    )
    assert plan.ok, (
        "LP must be feasible under guests+pinning with the last slot in the "
        f"shower window (terminal floor must defer to the pin); got {plan.status}"
    )


# ---------------------------------------------------------------------------
# #681 — price-aware warmup hour + K2 pin lockstep
# ---------------------------------------------------------------------------


def _winter_prices(day, cheap_hour, n=48):
    """Agile-rate dicts for a full local day, cheap at ``cheap_hour`` local."""
    base = datetime(day.year, day.month, day.day, 0, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    rates = []
    for i in range(n):
        ts = base + timedelta(minutes=30 * i)
        local_h = ts.astimezone(TZ_LOCAL).hour
        val = 4.0 if local_h == cheap_hour else 28.0
        rates.append({"valid_from": ts.isoformat().replace("+00:00", "Z"),
                      "value_inc_vat": val})
    return rates


def _price_line_from_rates(slots, rates):
    """Align an agile-rate dict list to a per-slot import price line."""
    from datetime import datetime as _dt
    m = {}
    for r in rates:
        ts = _dt.fromisoformat(str(r["valid_from"]).replace("Z", "+00:00"))
        m[ts.astimezone(UTC)] = float(r["value_inc_vat"])
    return [m.get(s.astimezone(UTC), 28.0) for s in slots]


@pytest.fixture
def _dhw_db():
    """Fresh runtime_settings table for persist-once assertions."""
    from src import db
    db.init_db()
    from src import dhw_policy as _dp
    _dp._autoscale_cache.clear()
    _dp._warmup_shadow_logged.clear()
    yield db
    _dp._autoscale_cache.clear()
    _dp._warmup_shadow_logged.clear()


def test_price_aware_disabled_is_byte_identical(monkeypatch, _dhw_db):
    """Flag OFF (default): forecast + schedule are identical to the static
    13:00 warmup — the whole point of shipping shadow-first."""
    from datetime import date
    monkeypatch.setattr(config, "DHW_WARMUP_PRICE_AWARE_ENABLED", False, raising=False)
    day = date(2026, 1, 15)  # GMT winter
    base = datetime(day.year, day.month, day.day, 10, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    n = 28
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    rates = _winter_prices(day, cheap_hour=15)
    price_line = _price_line_from_rates(slots, rates)

    e_off, tank_off = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", price_line=price_line)
    # Static reference — no price signal at all.
    e_static, tank_static = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert e_off == e_static
    assert tank_off == tank_static
    # No persisted value while disabled.
    assert dhw_policy._persisted_warmup_hour(day) is None
    # Warmup still fires at the static 13:00.
    rows = dhw_policy.generate_daily_tank_schedule(day, agile_rates=rates, allow_past=True)
    warmup = [r for r in rows if r["action_type"] == "tank_warmup"][0]
    assert warmup["start_time"] == "2026-01-15T13:00:00Z"


def test_price_aware_moves_warmup_and_pins_in_lockstep(monkeypatch, _dhw_db):
    """K2 divergence guard (#1): the single writer resolves+persists once; the
    K2 pin (forecast) and the fired warmup row (generate) then BOTH read that
    frozen hour — they can never diverge, at any wall-clock, under any later
    (perturbed) prices."""
    from datetime import date
    monkeypatch.setattr(config, "DHW_WARMUP_PRICE_AWARE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    day = date(2026, 1, 15)
    rates = _winter_prices(day, cheap_hour=15)  # cheapest at 15:00 local

    # The WRITER runs once (this is the LP-solve path's job in prod).
    assert dhw_policy.resolve_warmup_hour_local(day, rates) == 15
    assert dhw_policy._persisted_warmup_hour(day) == 15

    # (a) The K2 pin (forecast, a READER) puts the transition pulse at 15:00.
    base = datetime(day.year, day.month, day.day, 10, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    n = 28  # 10:00 → 24:00 local
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")

    def _slot_local_hour(i):
        return slots[i].astimezone(TZ_LOCAL).hour
    transition_idx = [i for i, v in enumerate(e_dhw) if v > 0.3]
    assert transition_idx, "expected a warmup-transition pulse"
    assert all(_slot_local_hour(i) == 15 for i in transition_idx), (
        f"transition slots at local hours "
        f"{[_slot_local_hour(i) for i in transition_idx]}, expected 15"
    )

    # (b) The fired schedule (generate, a READER) — even handed DIFFERENT
    # prices, it reads the persisted 15:00 and never re-picks.
    rates_shifted = _winter_prices(day, cheap_hour=11)  # would-be cheapest at 11
    rows = dhw_policy.generate_daily_tank_schedule(
        day, agile_rates=rates_shifted, allow_past=True)
    warmup = [r for r in rows if r["action_type"] == "tank_warmup"][0]
    assert warmup["start_time"] == "2026-01-15T15:00:00Z", (
        "reader must hold the warmup at the persisted hour so the fired row "
        "and the K2 pin never diverge"
    )
    # Setback UNMOVED — the restore covenant.
    setback = [r for r in rows if r["action_type"] == "tank_setback"][0]
    assert setback["start_time"] == "2026-01-15T22:00:00Z"


def test_price_aware_warmup_at_window_edges_stays_feasible(monkeypatch, _dhw_db):
    """#422/shower/tank-thermo floor skips stay intact when the warmup moves to
    the window edges (11:00 and 15:00): no Infeasible. The writer persists the
    hour first; the solve's K2 pin reads it."""
    from datetime import date
    monkeypatch.setattr(config, "DHW_WARMUP_PRICE_AWARE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    for cheap_hour, day in ((11, date(2026, 1, 16)), (15, date(2026, 1, 17))):
        dhw_policy._warmup_shadow_logged.clear()
        rates = _winter_prices(day, cheap_hour=cheap_hour)
        assert dhw_policy.resolve_warmup_hour_local(day, rates) == cheap_hour
        assert dhw_policy._persisted_warmup_hour(day) == cheap_hour
        base = datetime(day.year, day.month, day.day, 10, 0,
                        tzinfo=TZ_LOCAL).astimezone(UTC)
        n = 24
        slots = [base + timedelta(minutes=30 * i) for i in range(n)]
        prices = _price_line_from_rates(slots, rates)
        plan = _solve(
            slots=slots, prices=prices, pv=[0.5] * n,
            base_load=[0.3] * n, init_soc=6.0, init_tank=40.0,
        )
        assert plan.ok, (
            f"warmup moved to {cheap_hour}:00 must stay feasible; got {plan.status}"
        )


def test_price_aware_display_reader_never_persists(monkeypatch, _dhw_db):
    """Finding 2: a generate call BEFORE any solve (dashboard hit / boundary
    dispatch) is a pure READER — it must NOT persist, even handed a full,
    real, in-window rate set. Only the LP-solve writer freezes an hour."""
    from datetime import date
    monkeypatch.setattr(config, "DHW_WARMUP_PRICE_AWARE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    day = date(2026, 1, 18)
    rates = _winter_prices(day, cheap_hour=11)
    rows = dhw_policy.generate_daily_tank_schedule(
        day, agile_rates=rates, allow_past=True)
    warmup = [r for r in rows if r["action_type"] == "tank_warmup"][0]
    # Nothing persisted → reader fell back to the static 13:00.
    assert dhw_policy._persisted_warmup_hour(day) is None
    assert warmup["start_time"] == "2026-01-18T13:00:00Z"


def test_price_aware_tomorrow_not_frozen_from_priors(monkeypatch, _dhw_db):
    """Finding 1: when D+1's Agile isn't published, the horizon-extender fills
    the tail with synthetic ``fetched_at="prior"`` rows. The writer must NOT
    freeze tomorrow from those priors — it resolves+persists only once REAL
    rates cover the whole window."""
    from datetime import date
    monkeypatch.setattr(config, "DHW_WARMUP_PRICE_AWARE_ENABLED", True, raising=False)
    today = date(2026, 1, 15)
    tomorrow = date(2026, 1, 16)

    # Today real (cheapest 15). Tomorrow only as SYNTHETIC priors (cheapest 11).
    real_today = _winter_prices(today, cheap_hour=15)
    prior_tomorrow = _winter_prices(tomorrow, cheap_hour=11)
    for r in prior_tomorrow:
        r["fetched_at"] = "prior"
    mixed = real_today + prior_tomorrow

    assert dhw_policy.resolve_warmup_hour_local(today, mixed) == 15
    assert dhw_policy._persisted_warmup_hour(today) == 15
    # Tomorrow window is all priors → static fallback, NOT persisted.
    assert dhw_policy.resolve_warmup_hour_local(tomorrow, mixed) == \
        dhw_policy._static_warmup_hour()
    assert dhw_policy._persisted_warmup_hour(tomorrow) is None

    # Real D+1 rates land (cheapest 12) → now it resolves+persists from REAL.
    real_tomorrow = _winter_prices(tomorrow, cheap_hour=12)
    assert dhw_policy.resolve_warmup_hour_local(tomorrow, real_today + real_tomorrow) == 12
    assert dhw_policy._persisted_warmup_hour(tomorrow) == 12


def test_price_aware_real_optimizer_prior_extension_not_frozen(monkeypatch, _dhw_db):
    """Finding 1, end-to-end: drive the REAL horizon extender
    (``_resolve_plan_window`` synthesising ``fetched_at="prior"`` D+1 rows when
    tomorrow's Agile is unpublished), feed its ``window.rates`` to the single
    writer, and assert tomorrow is NOT frozen from priors — then IS resolved +
    persisted once real D+1 rates land."""
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td
    from src import db
    from src.config import config as app_config
    from src.scheduler import optimizer as _opt

    TARIFF = "E-1R-AGILE-DHW681-A"
    monkeypatch.setattr(app_config, "DHW_WARMUP_PRICE_AWARE_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", TARIFF, raising=False)
    monkeypatch.setattr(app_config, "LP_HORIZON_HOURS", 48, raising=False)

    # Anchor to REAL today @ 09:00 UTC: get_half_hourly_agile_priors' cutoff is
    # the real wall clock (now-28d), so the seeded history must be real-recent.
    # Pinning hour=9 avoids the UTC-midnight date-relative flake.
    now = _dt.now(_UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(_opt, "_now_utc", lambda: now)
    today = now.astimezone(dhw_policy._tz_local()).date()
    tomorrow = today + _td(days=1)

    def _seed(vf, count, price_fn):
        rows = []
        t = vf
        for _ in range(count):
            te = t + _td(minutes=30)
            rows.append({
                "valid_from": t.isoformat().replace("+00:00", "Z"),
                "valid_to": te.isoformat().replace("+00:00", "Z"),
                "value_inc_vat": float(price_fn(t)),
            })
            t = te
        db.save_agile_rates(rows, TARIFF)

    # 28 d of FLAT history → non-empty, flat priors (so a prior-frozen tomorrow
    # would tie and pick the earliest window hour, 11 — a wrong freeze to catch).
    day0 = _dt(now.year, now.month, now.day, tzinfo=_UTC) - _td(days=28)
    _seed(day0, 28 * 48, lambda t: 20.0)
    # TODAY only, real, cheapest at 15:00 local.
    today_start = _dt(now.year, now.month, now.day, tzinfo=_UTC)
    _seed(today_start, 48,
          lambda t: 3.0 if t.astimezone(dhw_policy._tz_local()).hour == 15 else 25.0)

    window = _opt._resolve_plan_window(TARIFF)
    assert window is not None
    # Prove the extender actually synthesised priors for the D+1 tail.
    assert any(str(r.get("fetched_at")) == "prior" for r in window.rates)
    starts = [s.start_utc for s in _opt._build_half_hour_slots(
        window.rates, window.day_start, window.horizon_end)]

    resolved = dhw_policy.resolve_warmup_hours_for_horizon(starts, window.rates)
    # Today is real-covered → persisted at its real cheapest hour (15).
    assert dhw_policy._persisted_warmup_hour(today) == 15
    assert resolved.get(today) == 15
    # Tomorrow's window is priors → NOT persisted (would be 11 if the bug lived).
    assert dhw_policy._persisted_warmup_hour(tomorrow) is None

    # Real D+1 publishes (cheapest 12) → seed + re-resolve → now persisted.
    tomorrow_start = today_start + _td(days=1)
    _seed(tomorrow_start, 48,
          lambda t: 3.0 if t.astimezone(dhw_policy._tz_local()).hour == 12 else 25.0)
    window2 = _opt._resolve_plan_window(TARIFF)
    starts2 = [s.start_utc for s in _opt._build_half_hour_slots(
        window2.rates, window2.day_start, window2.horizon_end)]
    dhw_policy.resolve_warmup_hours_for_horizon(starts2, window2.rates)
    assert dhw_policy._persisted_warmup_hour(tomorrow) == 12
