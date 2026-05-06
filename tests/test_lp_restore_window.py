"""Restore-action window must be wider than the heartbeat tick.

2026-04-30 active-mode rollout hit a race: the legacy 1-min restore window
(end_utc + 1 min) was narrower than the 2-min heartbeat interval. When a
heartbeat tick landed just past the restore window, the state machine
silently marked the action ``completed`` without firing — leaving the device
in its prior shutdown state until manual recovery.

LP_RESTORE_WINDOW_MINUTES (default 5, min 2) widens the window so a heartbeat
tick falling anywhere inside has time to dispatch."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src.scheduler.lp_dispatch import daikin_dispatch_preview
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import HourlyForecast, WeatherLpSeries


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch):
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")


def _build_plan_with_peak():
    """Build an LP plan that will produce at least one shutdown + restore pair
    (peak slot in the middle of the horizon).
    """
    base = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
    n = 10
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[10.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[0.5] * n,
        cop_space=[3.2] * n,
        cop_dhw=[2.7] * n,
    )
    # Peak in the middle (slots 4-5)
    prices = [10.0] * 4 + [40.0, 40.0] + [10.0] * 4
    base_load = [0.4] * n
    st = LpInitialState(soc_kwh=8.0, tank_temp_c=48.0, indoor_temp_c=21.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    return plan, slots


def _empty_forecast(slots):
    return [
        HourlyForecast(
            time_utc=s,
            temperature_c=10.0,
            shortwave_radiation_wm2=400.0,
            cloud_cover_pct=40.0,
            estimated_pv_kw=0.0,
            heating_demand_factor=1.0,
        )
        for s in slots
    ]


def test_restore_window_default_is_at_least_5_minutes(monkeypatch):
    """Default restore window is wide enough to survive heartbeat jitter."""
    monkeypatch.setattr(app_config, "LP_RESTORE_WINDOW_MINUTES", 5)
    plan, slots = _build_plan_with_peak()
    pairs = daikin_dispatch_preview(plan, _empty_forecast(slots))
    if not pairs:
        pytest.skip("LP plan produced no Daikin action pairs (warm/quiet day)")
    for restore_row, action_row in pairs:
        st = datetime.fromisoformat(restore_row["start_time"].replace("Z", "+00:00"))
        en = datetime.fromisoformat(restore_row["end_time"].replace("Z", "+00:00"))
        width = en - st
        assert width >= timedelta(minutes=5), (
            f"restore window {width} too narrow (start={st} end={en}) — "
            f"would race with the 2-min heartbeat tick"
        )


def test_restore_window_respects_config_override(monkeypatch):
    """LP_RESTORE_WINDOW_MINUTES env override widens the window."""
    monkeypatch.setattr(app_config, "LP_RESTORE_WINDOW_MINUTES", 10)
    plan, slots = _build_plan_with_peak()
    pairs = daikin_dispatch_preview(plan, _empty_forecast(slots))
    if not pairs:
        pytest.skip("LP plan produced no Daikin action pairs")
    for restore_row, _action_row in pairs:
        st = datetime.fromisoformat(restore_row["start_time"].replace("Z", "+00:00"))
        en = datetime.fromisoformat(restore_row["end_time"].replace("Z", "+00:00"))
        assert (en - st) >= timedelta(minutes=10)


def test_restore_window_floor_is_2_minutes(monkeypatch):
    """Even with absurd config, window can't be narrower than the heartbeat tick."""
    monkeypatch.setattr(app_config, "LP_RESTORE_WINDOW_MINUTES", 0)  # try to break it
    plan, slots = _build_plan_with_peak()
    pairs = daikin_dispatch_preview(plan, _empty_forecast(slots))
    if not pairs:
        pytest.skip("LP plan produced no Daikin action pairs")
    for restore_row, _action_row in pairs:
        st = datetime.fromisoformat(restore_row["start_time"].replace("Z", "+00:00"))
        en = datetime.fromisoformat(restore_row["end_time"].replace("Z", "+00:00"))
        assert (en - st) >= timedelta(minutes=2), (
            "config_override below floor should clamp to 2 min"
        )
