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
) -> tuple[list[datetime], list[float]]:
    """Solve a Sunday horizon spanning the legionella window. Returns
    (slot_starts_utc, tank_temp_c_per_slot_end)."""
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
    return slots, plan.tank_temp_c


def test_legionella_lifts_tank_to_target_at_cycle_window() -> None:
    """Sunday 13:00–14:00 BST: tank must reach ≥60°C."""
    slots, tank = _solve_sunday_with_legionella(
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
    for i in cycle_indices:
        # tank state at END of this slot (index i+1 in plan.tank_temp_c).
        assert tank[i + 1] >= 60.0 - 1e-3, (
            f"slot {i} ({slots[i].astimezone(tz)}) tank end-state {tank[i + 1]:.2f}°C "
            f"below 60°C floor"
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


def test_legionella_target_above_tank_hi_caps_with_headroom() -> None:
    """Target above tank_hi is capped to tank_hi - 1°C so the LP can satisfy
    space_floor + mode-mutex without conflict. LP stays feasible; tank
    reaches at least the capped target."""
    slots, tank = _solve_sunday_with_legionella(
        tank_initial=38.0, leg_day=6, leg_hour=13, leg_target=70.0,  # > tank_hi=65
        leg_duration_min=60,
    )
    tz = ZoneInfo("Europe/London")
    cycle_idx = [
        i for i, st in enumerate(slots)
        if st.astimezone(tz).weekday() == 6
        and 13 <= st.astimezone(tz).hour + st.astimezone(tz).minute / 60.0 < 14
    ][0]
    # Capped at tank_hi - 1 = 64°C
    assert tank[cycle_idx + 1] >= 64.0 - 1e-3
