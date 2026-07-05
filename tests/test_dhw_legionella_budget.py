"""#643 — legionella heat-up budget in the pinned DHW forecast.

The firmware runs the weekly thermal-shock cycle on its own; pre-#643 the
forecast carried NO term for it (~0.5 budgeted vs ~3-3.5 kWh drawn on
2026-07-05), so the LP let the battery discharge into the window and hit the
SoC floor mid-cycle. The budget is ENERGY only (tank temps stay the schedule's
comfort targets), never scaled by the bucket-bias corrector, max-not-sum where
it overlaps other heating, per-slot clamped, and window/DOW are UTC-defined
(the firmware's clock).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db, dhw_policy
from src.config import config

TZ_LOCAL = ZoneInfo("Europe/London")


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(db, "_db_path", lambda: path)
    db.init_db()
    return path


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_BUDGET_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_BUDGET_KWH", 3.5, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", 6, raising=False)  # Sunday
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 11, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", 0, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120, raising=False)
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 2.5, raising=False)  # cap 1.25/slot
    monkeypatch.setattr(config, "DHW_FORECAST_AUTOSCALE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", False, raising=False)
    dhw_policy._autoscale_cache.clear()
    yield
    dhw_policy._autoscale_cache.clear()


SUNDAY = date(2026, 7, 5)   # a real Sunday
MONDAY = date(2026, 7, 6)


def _day_slots(day: date):
    start = datetime.combine(day, time(0, 0), tzinfo=UTC)
    return [(start + timedelta(minutes=30 * i)) for i in range(48)]


def _window_idxs():
    # 11:00-13:00 UTC = slots 22..25 on a UTC-midnight-aligned day
    return [22, 23, 24, 25]


def test_sunday_window_gets_budget_evenly(tmp_db):
    slots = _day_slots(SUNDAY)
    e, tank = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    for i in _window_idxs():
        assert e[i] == pytest.approx(3.5 / 4)  # 0.875/slot, under the 1.25 cap
    # neighbours untouched (setback/warmup constants, well below 0.875)
    assert e[21] < 0.2 and e[26] < 0.5


def test_non_legionella_day_unchanged(tmp_db, monkeypatch):
    slots = _day_slots(MONDAY)
    e_on, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    monkeypatch.setattr(config, "DHW_LEGIONELLA_BUDGET_ENABLED", False, raising=False)
    e_off, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert e_on == e_off


def test_disabled_flag_and_zero_budget_are_noops(tmp_db, monkeypatch):
    slots = _day_slots(SUNDAY)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_BUDGET_ENABLED", False, raising=False)
    base, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    monkeypatch.setattr(config, "DHW_LEGIONELLA_BUDGET_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_BUDGET_KWH", 0.0, raising=False)
    zero, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert zero == base
    for i in _window_idxs():
        assert base[i] < 0.2  # the pre-#643 hole this feature fills


def test_small_heater_clamps_per_slot(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 1.0, raising=False)  # cap 0.5
    slots = _day_slots(SUNDAY)
    e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    for i in _window_idxs():
        assert e[i] == pytest.approx(0.5)  # 0.875 clamped to the heater cap


def test_vacation_mode_still_budgets_the_cycle(tmp_db):
    """Firmware fires regardless of the preset — vacation must budget it too."""
    slots = _day_slots(SUNDAY)
    e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="vacation")
    for i in _window_idxs():
        assert e[i] == pytest.approx(3.5 / 4)
    assert sum(e[i] for i in range(48) if i not in _window_idxs()) == pytest.approx(0.0)


def test_tank_temps_untouched(tmp_db, monkeypatch):
    slots = _day_slots(SUNDAY)
    _, tank_on = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    monkeypatch.setattr(config, "DHW_LEGIONELLA_BUDGET_ENABLED", False, raising=False)
    _, tank_off = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert tank_on == tank_off  # energy budget only — comfort targets unchanged


def test_max_not_sum_with_existing_heating(tmp_db, monkeypatch):
    """A window slot that already carries heating (e.g. warmup transition at
    13:00 local when the window is moved there) keeps max, never sum."""
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 13, raising=False)
    slots = _day_slots(SUNDAY)
    e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    per_slot = 3.5 / 4
    # 13:00-13:30 is the warmup transition (0.45 unscaled) — budget wins as max
    assert e[26] == pytest.approx(per_slot)
    assert e[26] < per_slot + 0.45  # not summed


def test_k2_pin_stays_optimal_with_legionella_budget(tmp_db, monkeypatch):
    """Full solve across the Sunday window with a small heater — the pinned
    budget must never make the LP Infeasible."""
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 1.0, raising=False)

    start = datetime(2026, 7, 5, 8, 0, tzinfo=UTC)  # Sunday, window in horizon
    slots = [start + timedelta(minutes=30 * i) for i in range(28)]
    n = len(slots)
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[18.0] * n,
        shortwave_radiation_wm2=[600.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=[0.1] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[12.0] * n,
        base_load_kwh=[0.25] * n,
        weather=weather,
        initial=LpInitialState(soc_kwh=8.0, tank_temp_c=40.0),
        tz=TZ_LOCAL,
    )
    assert plan is not None and plan.status == "Optimal"
    # the budget is actually in the pinned plan
    e_fc, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    win = [i for i, s in enumerate(slots) if s.weekday() == 6 and 11 <= s.hour < 13]
    assert win, "window must be inside the horizon"
    for i in win:
        assert plan.dhw_electric_kwh[i] == pytest.approx(e_fc[i], abs=1e-6)
        assert plan.dhw_electric_kwh[i] >= 0.49  # clamped budget, not the old 0.03
