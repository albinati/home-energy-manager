"""Space heating floor constraint vs solution (#19)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src.physics import get_daikin_heating_kw
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch):
    monkeypatch.setattr(app_config, "LP_HIGHS_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)


def _series_cold(
    n: int,
    base: datetime,
    t_out: float,
) -> tuple[list[datetime], WeatherLpSeries]:
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[t_out] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[0.5] * n,
        cop_space=[3.2] * n,
        cop_dhw=[2.7] * n,
    )
    return slots, w


def test_solution_respects_minimum_hp_electric_vs_space_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each slot must meet e_space + e_dhw >= space_floor_kwh[i] when floor > 0 (#19)."""
    monkeypatch.setattr(app_config, "DAIKIN_WEATHER_CURVE_HIGH_C", 18.0)
    monkeypatch.setattr(app_config, "DAIKIN_MAX_HP_KW", 3.0)
    slot_h = 0.5
    max_hp_kwh = float(app_config.DAIKIN_MAX_HP_KW) * slot_h

    # Mild cold (10 °C): climate floor > 0 but LP stays feasible with the default horizon.
    base = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    n = 12
    t_out = 10.0
    slots, w = _series_cold(n, base, t_out)
    prices = [12.0] * n
    base_load = [0.4] * n
    # indoor_temp_c at 20.5 matches the solver's terminal floor (INDOOR_SETPOINT_C−0.5) —
    # short horizons can't recover if it starts below, especially under the tighter LWT cap.
    st = LpInitialState(soc_kwh=4.0, tank_temp_c=44.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok
    for i in range(n):
        floor = min(get_daikin_heating_kw(t_out) * slot_h, max_hp_kwh)
        if floor <= 0:
            continue
        hp = plan.dhw_electric_kwh[i] + plan.space_electric_kwh[i]
        assert hp >= floor - 1e-2, f"slot {i}: hp={hp} floor={floor}"
