"""forecast_to_lp_inputs COP arrays with optional lift (#29)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config as app_config
from src.weather import HourlyForecast, forecast_to_lp_inputs


def _slot_starts(n: int, base: datetime) -> list[datetime]:
    return [base + timedelta(minutes=30 * i) for i in range(n)]


def test_forecast_cop_lift_disabled_matches_legacy_subtract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.0)
    monkeypatch.setattr(app_config, "COP_DHW_PENALTY", 0.5)
    base = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
    slots = _slot_starts(4, base)
    forecast = [
        HourlyForecast(
            time_utc=base,
            temperature_c=5.0,
            cloud_cover_pct=40.0,
            shortwave_radiation_wm2=100.0,
            estimated_pv_kw=0.2,
            heating_demand_factor=0.5,
        )
    ]
    s = forecast_to_lp_inputs(forecast, slots)
    for i in range(4):
        assert s.cop_dhw[i] == pytest.approx(max(1.0, s.cop_space[i] - 0.5))


def test_forecast_cop_lift_reduces_cop_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.02)
    monkeypatch.setattr(app_config, "LP_COP_LIFT_REFERENCE_DELTA_K", 20.0)
    monkeypatch.setattr(app_config, "LP_COP_LIFT_MIN_MULTIPLIER", 0.5)
    base = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
    slots = _slot_starts(4, base)
    forecast = [
        HourlyForecast(
            time_utc=base,
            temperature_c=-5.0,
            cloud_cover_pct=40.0,
            shortwave_radiation_wm2=100.0,
            estimated_pv_kw=0.2,
            heating_demand_factor=0.5,
        )
    ]
    monkeypatch.setattr(app_config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.0)
    ref = forecast_to_lp_inputs(forecast, slots)
    monkeypatch.setattr(app_config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.02)
    lifted = forecast_to_lp_inputs(forecast, slots)
    assert lifted.cop_space[0] < ref.cop_space[0]
    assert lifted.cop_space[0] >= 1.0
    assert lifted.cop_dhw[0] >= 1.0
