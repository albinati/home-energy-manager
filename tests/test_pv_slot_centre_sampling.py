"""Slot-CENTRE forecast sampling (PV_FORECAST_SLOT_CENTRE_SAMPLING).

A 30-min slot's energy is ``kw × 0.5h``; the honest representative power is the
value at the slot CENTRE (start+15min). Sampling at the slot START attributed
each slot's PV energy ~15 min too late versus the realised trapezoidal roll-up —
a deterministic +15 min lag measured over 21 prod days
(``scripts/diag/pv_time_lag.py``). These tests pin both the centre behaviour and
the legacy slot-start fallback when the flag is disabled.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config as app_config
from src.weather import HourlyForecast, forecast_to_lp_inputs


def _ramp_forecast() -> tuple[datetime, list[HourlyForecast]]:
    """Irradiance ramps linearly 0 → 1200 W/m² from 12:00 to 13:00 UTC; cloud 0
    (so rad_eff == rad), temperature constant. A pure ramp makes the sampling
    point directly observable in the returned shortwave_radiation_wm2 array.
    """
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    forecast = [
        HourlyForecast(
            time_utc=base,
            temperature_c=15.0,
            cloud_cover_pct=0.0,
            shortwave_radiation_wm2=0.0,
            estimated_pv_kw=0.0,
            heating_demand_factor=0.5,
        ),
        HourlyForecast(
            time_utc=base + timedelta(hours=1),
            temperature_c=15.0,
            cloud_cover_pct=0.0,
            shortwave_radiation_wm2=1200.0,
            estimated_pv_kw=0.0,
            heating_demand_factor=0.5,
        ),
    ]
    return base, forecast


def test_slot_centre_samples_midpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "PV_FORECAST_SLOT_CENTRE_SAMPLING", True)
    monkeypatch.setattr(app_config, "FORECAST_NIGHT_TEMP_BIAS_C", 0.0)
    base, forecast = _ramp_forecast()
    slots = [base, base + timedelta(minutes=30)]
    s = forecast_to_lp_inputs(forecast, slots, pv_scale=1.0)
    # Slot [12:00,12:30] is sampled at 12:15 → 0.25 × 1200; [12:30,13:00] at 12:45.
    assert s.shortwave_radiation_wm2[0] == pytest.approx(300.0)
    assert s.shortwave_radiation_wm2[1] == pytest.approx(900.0)


def test_slot_start_legacy_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "PV_FORECAST_SLOT_CENTRE_SAMPLING", False)
    monkeypatch.setattr(app_config, "FORECAST_NIGHT_TEMP_BIAS_C", 0.0)
    base, forecast = _ramp_forecast()
    slots = [base, base + timedelta(minutes=30)]
    s = forecast_to_lp_inputs(forecast, slots, pv_scale=1.0)
    # Legacy: sampled at the slot START → 12:00 (0) and 12:30 (0.5 × 1200).
    assert s.shortwave_radiation_wm2[0] == pytest.approx(0.0)
    assert s.shortwave_radiation_wm2[1] == pytest.approx(600.0)


def test_slot_centre_advances_pv_energy_on_morning_ramp(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a rising curve the centre-sampled PV energy for an early slot exceeds
    the start-sampled one (the slot's energy is pulled earlier in time, removing
    the forecast-late lag)."""
    monkeypatch.setattr(app_config, "FORECAST_NIGHT_TEMP_BIAS_C", 0.0)
    base, forecast = _ramp_forecast()
    slots = [base, base + timedelta(minutes=30)]

    monkeypatch.setattr(app_config, "PV_FORECAST_SLOT_CENTRE_SAMPLING", True)
    centre = forecast_to_lp_inputs(forecast, slots, pv_scale=1.0)
    monkeypatch.setattr(app_config, "PV_FORECAST_SLOT_CENTRE_SAMPLING", False)
    start = forecast_to_lp_inputs(forecast, slots, pv_scale=1.0)

    assert centre.pv_kwh_per_slot[0] > start.pv_kwh_per_slot[0]
