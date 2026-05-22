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
    """When DHW_DAILY_SHOWER_LITRES > 0 (legacy escape hatch), the LP's
    planned tank trajectory must drop materially during shower-window
    slots (real physics) instead of staying nearly flat (standing loss
    only). PR G: tank starts AT the required floor so any draw forces
    the LP to allocate maintenance heating to stay above slack."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(app_config, "DHW_DAILY_SHOWER_LITRES", 144.0, raising=False)
    # Force a higher required floor by going guests mode (6 showers → ~47 °C floor).
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "guests", raising=False)
    monkeypatch.setattr(app_config, "DHW_GUEST_COUNT", 2, raising=False)

    base = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)  # 19:00 BST
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[20.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_make_weather(slots),
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=50.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status

    # With the guests-mode floor (~47 °C) and 144 L/day draw spread over 6
    # slots, tank should drop materially from the 50 °C start (heat is
    # planned but doesn't fully offset draw). End-of-window must be below
    # the start AND the LP must have allocated some e_dhw.
    end_tank = plan.tank_temp_c[-1]
    total_dhw = sum(plan.dhw_electric_kwh)
    assert end_tank < 49.0, (
        f"With draw model, tank should drop below starting temp 50°C. "
        f"Got end_tank={end_tank:.1f}"
    )
    assert total_dhw > 0.3, (
        f"With shower draw active, LP must allocate at least some e_dhw "
        f"to maintain the guests floor. Got total_dhw={total_dhw:.2f} kWh"
    )


def test_dhw_draw_per_day_normalization_2day_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: daily_shower_litres must be divided by per-day shower
    slot count, not the horizon-wide total. A 48h horizon with 2 daily
    shower windows has 12 shower slots; using 12 as divisor would split
    each day's draw across the OTHER day's slots, under-modelling per-day
    draw by ~50%. Tank trajectory in the LP would diverge from physical
    reality → firmware reheats from grid at unfavorable rates the LP
    didn't predict (~£75-90/year of avoidable peak imports).
    """
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import (
        _resolve_active_shower_windows,
        _window_set_slot_mask,
    )

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(app_config, "DHW_DAILY_SHOWER_LITRES", 144.0, raising=False)
    monkeypatch.setattr(app_config, "DHW_USAGE_TEMP_C", 40.0, raising=False)
    monkeypatch.setattr(app_config, "DHW_COLD_INLET_TEMP_C", 10.0, raising=False)
    # PR G — force guests mode so the floor (~47 °C) is meaningfully above
    # the starting tank (50 °C minus ongoing draw) and the LP must heat
    # daily to maintain. Normal mode under PR G defaults has a 40 °C floor,
    # which the natural standing loss + draw still leaves above (no heat
    # needed → regression test loses its sensor).
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "guests", raising=False)
    monkeypatch.setattr(app_config, "DHW_GUEST_COUNT", 2, raising=False)

    # Solve the LP with a 48h horizon spanning 2 days.
    # Each day's shower window: 19:00-22:00 BST = 6 slots × 30 min.
    # Day 1: tank starts at 50°C. Day 2: tank should also reach the floor
    # by end of shower window after appropriate heating.
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)  # 13:00 BST
    n = 48  # 24 h
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]

    # Sanity: there should be 6 shower slots in this 24h window
    # (19:00, 19:30, 20:00, 20:30, 21:00, 21:30 BST = 6 slots).
    tz = ZoneInfo("Europe/London")
    shower_windows = _resolve_active_shower_windows(False)
    mask = _window_set_slot_mask(slots, tz, windows=shower_windows)
    n_mask_true = sum(1 for x in mask if x)
    assert n_mask_true == 6, f"expected 6 shower slots in 24h, got {n_mask_true}"

    # Now extend to 48h
    n2 = 96
    slots2 = [base + timedelta(minutes=30 * i) for i in range(n2)]
    mask2 = _window_set_slot_mask(slots2, tz, windows=shower_windows)
    n_mask_true_2 = sum(1 for x in mask2 if x)
    assert n_mask_true_2 == 12, f"expected 12 shower slots in 48h, got {n_mask_true_2}"

    # Solve the actual LP with 48h horizon
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    plan = solve_lp(
        slot_starts_utc=slots2,
        price_pence=[20.0] * n2,
        base_load_kwh=[0.3] * n2,
        weather=_make_weather(slots2),
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=50.0),
        tz=tz,
    )
    assert plan.ok, plan.status

    # The total energy removed in shower-window slots must equal
    # 2 days × daily_thermal_kWh ≈ 2 × 5 = 10 kWh thermal across both
    # shower windows. Each day's window: ~5 kWh thermal. Per-slot: ~0.84 kWh.
    #
    # If the bug were still present (n=12 in divisor), per-slot draw would
    # be ~0.42 kWh — half the correct value. Test below validates the
    # per-day division is correct.
    #
    # We can't directly inspect shower_draw_j from outside solve_lp, but we
    # can verify the LP plan: with correct draw, LP should plan ~5 kWh of
    # heating PER DAY to maintain shower constraint. With the bug, it would
    # plan only ~2.5 kWh per day.
    daily_dhw_kwh: dict = {}
    for i, t in enumerate(slots2):
        local_d = t.astimezone(tz).date()
        daily_dhw_kwh[local_d] = daily_dhw_kwh.get(local_d, 0.0) + plan.dhw_electric_kwh[i]
    # Each day should have ~1.67 kWh electrical (5 kWh thermal / COP 3) of heating.
    for d, kwh in daily_dhw_kwh.items():
        # 0.84 expected — verify it's much closer to that than 0.42 (the bug)
        # Floor the assertion at 1.0 kWh to detect the half-draw bug.
        # (Could be slightly less than 1.67 because LP optimizes spread.)
        if d == sorted(daily_dhw_kwh)[0]:
            # First day's draw is the meaningful test (later days might be
            # affected by horizon-end terminal-floor relaxation).
            assert kwh > 1.0, (
                f"Day {d}: LP planned only {kwh:.2f} kWh DHW heating. "
                f"Expected ~1.67 kWh for 5 kWh thermal draw at COP 3. "
                f"BUG: draw is being under-modelled (per-horizon vs per-day "
                f"division)."
            )


def test_dhw_draw_model_zero_litres_disables_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero showers (PR B: explicit demand model) means no draw — LP sees
    only standing loss, plans minimal heating.

    PR B replaced ``DHW_DAILY_SHOWER_LITRES`` (single aggregate) with a
    count × duration × flow × mixer-temp model. Setting all shower counts
    to 0 plus the legacy aggregate to 0 unambiguously disables draw."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(app_config, "DHW_DAILY_SHOWER_LITRES", 0.0, raising=False)
    # PR B — zero out the explicit count model too.
    monkeypatch.setattr(app_config, "DHW_SHOWERS_NORMAL_EVENING", 0, raising=False)
    monkeypatch.setattr(app_config, "DHW_SHOWERS_NORMAL_MORNING_RESERVE", 0, raising=False)

    base = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[20.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_make_weather(slots),
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=50.0),
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
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=50.0),
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
