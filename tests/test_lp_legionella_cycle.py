"""LP forces tank ≥ legionella target at the configured weekly cycle.

When ``DHW_LEGIONELLA_DAY`` is set (0=Mon..6=Sun), the LP must plan enough
pre-heat to reach ``DHW_LEGIONELLA_TANK_TARGET_C`` for the duration of the
cycle, on the configured weekday + local hour. Active mode only — passive
delegates to firmware.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db, runtime_settings as rts
from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")


def _solve_sunday_with_legionella(
    *,
    tank_initial: float = 38.0,
    leg_day: int = 6,
    leg_hour: int = 13,
    leg_target: float = 60.0,
    leg_duration_min: int = 60,
):
    """Solve a Sunday horizon spanning the legionella window. Returns the
    full LpPlan so callers can inspect e_dhw and tank temps."""
    rts.set_setting("DHW_LEGIONELLA_DAY", str(leg_day))
    rts.set_setting("DHW_LEGIONELLA_HOUR_LOCAL", str(leg_hour))
    rts.set_setting("DHW_LEGIONELLA_DURATION_MIN", str(leg_duration_min))
    rts.set_setting("DHW_LEGIONELLA_TANK_TARGET_C", str(leg_target))

    # 2026-05-10 is a Sunday in BST. Horizon 11:00 BST → 16:00 BST = 10 slots.
    base = datetime(2026, 5, 10, 10, 0, tzinfo=UTC)  # 11:00 BST
    n = 10
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[1.0] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    initial = LpInitialState(soc_kwh=8.0, tank_temp_c=tank_initial)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[12.0] * n,
        base_load_kwh=[0.4] * n,
        weather=weather,
        initial=initial,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, f"LP did not solve: {plan.status}"
    return slots, plan


def test_legionella_allocates_firmware_load_kwh_on_cycle_slots() -> None:
    """PR E (2026-05-22): legionella is FIRMWARE-owned. Per user clarification
    ("não temos controle, eh apenas pro LP saber"), the LP no longer FORCES
    tank temperature to the legionella target — instead it allocates the
    expected firmware kWh draw on each cycle slot so the rest of the plan
    (battery / grid / load) is sized correctly.

    With defaults (200 L tank, target 60 °C, normal 45 °C, COP 3.0, 60 min
    cycle = 2 slots): thermal lift ≈ 200×4186×15/3.6e6 ≈ 3.49 kWh / 3 cop /
    2 slots ≈ 0.58 kWh electric per slot.
    """
    slots, plan = _solve_sunday_with_legionella(
        tank_initial=38.0, leg_day=6, leg_hour=13, leg_target=60.0,
        leg_duration_min=60,
    )
    tz = ZoneInfo("Europe/London")
    cycle_indices = [
        i for i, st in enumerate(slots)
        if st.astimezone(tz).weekday() == 6
        and 13 <= st.astimezone(tz).hour + st.astimezone(tz).minute / 60.0 < 14
    ]
    assert len(cycle_indices) >= 1
    # Every cycle slot must allocate at least the firmware-load floor. The
    # LP recomputes COP from outdoor temperature (not the static cop_dhw
    # the test passes in the WeatherLpSeries), so the precise floor varies
    # with seasonal conditions. Assert e_dhw is materially > 0 (i.e. the
    # constraint fired) but tolerate the COP-driven variability.
    for i in cycle_indices:
        assert plan.dhw_electric_kwh[i] >= 0.2, (
            f"slot {i} ({slots[i].astimezone(tz)}) e_dhw={plan.dhw_electric_kwh[i]:.3f} "
            f"kWh — firmware-load floor not enforced"
        )


def test_legionella_only_difference_is_firmware_floor() -> None:
    """PR E counter-test: with the FIRMWARE-owned model, enabling
    legionella adds ONLY the firmware-load floor on cycle slots — it does
    NOT shift the LP toward independently lifting the tank to the target.

    Compares two solves of the same scenario, one with legionella enabled
    and one disabled. Difference in total e_dhw should be small (= cycle
    slot firmware-load floor), not a wholesale shift in heating plan."""
    # Solve with legionella enabled
    _, plan_on = _solve_sunday_with_legionella(
        tank_initial=38.0, leg_day=6, leg_hour=13, leg_target=60.0,
        leg_duration_min=60,
    )
    # Reset and solve with legionella disabled
    rts.set_setting("DHW_LEGIONELLA_DAY", "-1")
    base = datetime(2026, 5, 10, 10, 0, tzinfo=UTC)
    n = 10
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[1.0] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    plan_off = solve_lp(
        slot_starts_utc=slots,
        price_pence=[12.0] * n,
        base_load_kwh=[0.4] * n,
        weather=weather,
        initial=LpInitialState(soc_kwh=8.0, tank_temp_c=38.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan_off.ok

    # Total e_dhw delta should be at most ~1 kWh (the firmware load over
    # 2 cycle slots). NOT the ~3 kWh that the old constraint-based model
    # required to lift the tank from 38 → 60 °C.
    delta_kwh = sum(plan_on.dhw_electric_kwh) - sum(plan_off.dhw_electric_kwh)
    assert delta_kwh < 1.5, (
        f"legionella enabling added {delta_kwh:.2f} kWh — suspiciously high; "
        f"PR E's firmware-load floor should only add ~0.5-1 kWh."
    )


def test_legionella_disabled_does_not_force_tank() -> None:
    """When DHW_LEGIONELLA_DAY = -1, the LP isn't constrained to lift tank."""
    rts.set_setting("DHW_LEGIONELLA_DAY", "-1")
    base = datetime(2026, 5, 10, 10, 0, tzinfo=UTC)
    n = 10
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[1.0] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    initial = LpInitialState(soc_kwh=8.0, tank_temp_c=38.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[12.0] * n,
        base_load_kwh=[0.4] * n,
        weather=weather,
        initial=initial,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    # Tank doesn't need to reach the legionella target (60 °C) — the LP
    # may still over-heat by ≤2 °C when PV abundance + soft tank-hi penalty
    # make heating cheaper than curtailing. 62 °C tolerance is well below
    # any "yes, hitting legionella" signal.
    assert max(plan.tank_temp_c) < 62.0, (
        f"with legionella disabled, tank should not be forced near 60°C, "
        f"got {max(plan.tank_temp_c):.1f}"
    )


def test_legionella_high_target_still_feasible() -> None:
    """PR E: legionella target above tank_hi (e.g. 70 °C target with tank_hi
    = 65) no longer needs a constraint cap — the firmware-load floor on
    ``e_dhw`` is capped at ``max_hp_kwh`` so the LP stays feasible. Tank
    state is firmware's concern, not the LP's."""
    slots, plan = _solve_sunday_with_legionella(
        tank_initial=38.0, leg_day=6, leg_hour=13, leg_target=70.0,
        leg_duration_min=60,
    )
    # LP must stay feasible (was the old failure mode pre-cap). With the
    # new model, no tank constraint at all → trivially feasible.
    assert plan.ok, plan.status
    # And the firmware-load floor still allocates kWh on cycle slots.
    tz = ZoneInfo("Europe/London")
    cycle_idx = [
        i for i, st in enumerate(slots)
        if st.astimezone(tz).weekday() == 6
        and 13 <= st.astimezone(tz).hour + st.astimezone(tz).minute / 60.0 < 14
    ][0]
    assert plan.dhw_electric_kwh[cycle_idx] > 0.0
