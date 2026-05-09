"""Static-physics DHW draw model — bridge to V11-C / #196.

The LP's tank energy balance previously only modeled standing loss
(``ua_tank × (tank − indoor)``) — missing the much larger energy removal
from someone showering. Result: LP planned no heating during daytime,
expecting tank to drift gradually from standing loss alone, but reality has
tank dropping ~6 °C per shower as hot water exits the tank and is replaced
by cold water.

This module ships a static-physics DHW draw model: per-slot kWh thermal
energy removed during shower-window slots, computed from configured daily
mix-litres × use-temp delta. Replaces the missing ``q_draw`` term.

The full V11-C work (#196) layers learned-from-history priors on top.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


def _make_weather(slots, pv_kwh=None, base_kwh=None):
    from src.weather import WeatherLpSeries
    n = len(slots)
    return WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[50.0] * n,
        pv_kwh_per_slot=pv_kwh or [0.0] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )


def test_dhw_draw_model_drops_tank_temp_during_shower_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DHW_DAILY_SHOWER_LITRES > 0, the LP's planned tank trajectory
    must drop materially during shower-window slots (real physics) instead
    of staying nearly flat (standing loss only)."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(app_config, "DHW_DAILY_SHOWER_LITRES", 144.0, raising=False)
    monkeypatch.setattr(app_config, "DHW_USAGE_TEMP_C", 40.0, raising=False)
    monkeypatch.setattr(app_config, "DHW_COLD_INLET_TEMP_C", 10.0, raising=False)

    # 6 slots covering the 19:00-22:00 shower window. Constant 20p price, no PV,
    # tank initial 50°C. LP should plan zero or minimal heating during shower
    # slots (heating during showers is wasteful — wait until cheap window).
    base = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)  # 19:00 BST
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[20.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_make_weather(slots),
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=50.0, indoor_temp_c=21.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status

    # Without draw model, tank would have dropped ~0.5°C over 3h (just standing
    # loss). With draw model, even with LP's heating to maintain ≥ 45°C, the
    # END temperature should be at the floor (close to 45°C) — meaning the LP
    # had to plan substantial heating to OFFSET the draw. Without draw model
    # the LP would just leave tank at 49+ all the way through.
    end_tank = plan.tank_temp_c[-1]
    # With 144L/day = 5 kWh thermal over 6 slots, draw alone (no heat) would
    # drop tank by ~21°C in this window. LP must heat substantially. End-of-
    # window tank should be close to the floor 45°C, not the starting 50°C.
    assert end_tank < 49.0, (
        f"With draw model, tank should NOT stay near starting temp 50°C "
        f"through shower window — that would mean LP didn't see the draw. "
        f"Got end_tank={end_tank:.1f}"
    )
    # Sanity: still above floor.
    assert end_tank >= 44.5, (
        f"LP must keep tank ≥ floor (45°C) at all shower slots; got end_tank={end_tank:.1f}"
    )


def test_dhw_draw_model_zero_litres_disables_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting DHW_DAILY_SHOWER_LITRES=0 reverts to previous behavior — LP
    sees only standing loss, plans minimal heating."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(app_config, "DHW_DAILY_SHOWER_LITRES", 0.0, raising=False)

    base = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[20.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_make_weather(slots),
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=50.0, indoor_temp_c=21.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status
    end_tank = plan.tank_temp_c[-1]
    # No draw → tank stays close to starting temp (only standing loss applies).
    # 3 h × ~0.2 °C/h = small drop; should still be > 49°C.
    assert end_tank > 49.0, (
        f"With DHW_DAILY_SHOWER_LITRES=0 (draw disabled), tank should barely "
        f"drop over 3 h shower window; got end_tank={end_tank:.1f}"
    )


def test_dhw_draw_model_forces_pv_time_heating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: with draw model active, LP plans heating during PV-
    abundant or cheap morning slots to satisfy the evening shower constraint.
    Without draw model, LP would leave the tank alone all day (wrong).

    Test: 24h horizon (08:00 → 08:00 next day) with PV in middle, cheap
    rates morning, shower window 19:00-22:00. With draw model, e_dhw must be
    non-zero somewhere before the shower window.
    """
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(app_config, "DHW_DAILY_SHOWER_LITRES", 144.0, raising=False)

    base = datetime(2026, 6, 1, 7, 0, tzinfo=UTC)  # 08:00 BST
    n = 24  # 12 h
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    # Cheap rates in afternoon (PV-rich), expensive in morning/evening.
    prices = []
    for i in range(n):
        local_h = (slots[i] + timedelta(minutes=15)).astimezone(ZoneInfo("Europe/London")).hour
        if 12 <= local_h < 16:
            prices.append(10.0)  # cheap PV-time
        elif 16 <= local_h < 20:
            prices.append(28.0)  # peak
        else:
            prices.append(20.0)
    pv = [0.0 if h < 6 else (1.5 if 6 <= h < 16 else 0.0) for h in range(n)]
    pv = []  # rebuild against actual times
    for s in slots:
        local_h = (s + timedelta(minutes=15)).astimezone(ZoneInfo("Europe/London")).hour
        if 11 <= local_h <= 15:
            pv.append(2.0)  # mid-day sunshine
        else:
            pv.append(0.0)

    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=[0.3] * n,
        weather=_make_weather(slots, pv_kwh=pv),
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=50.0, indoor_temp_c=21.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status

    # Check that LP planned positive e_dhw somewhere BEFORE the shower window
    # (i.e. on cheap or PV-time slots), not just during.
    pre_shower_dhw = sum(
        plan.dhw_electric_kwh[i] for i in range(n)
        if (slots[i] + timedelta(minutes=15)).astimezone(ZoneInfo("Europe/London")).hour < 19
    )
    assert pre_shower_dhw > 0.5, (
        f"With draw model, LP must plan substantial pre-shower DHW heating "
        f"(during cheap PV slots) to maintain 45°C through evening. Got "
        f"{pre_shower_dhw:.2f} kWh before 19:00."
    )
