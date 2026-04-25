"""Per-hour-of-day PV calibration: cache table, compute fn, LP integration."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from src import db
    monkeypatch.setattr(db, "_DB_PATH", str(db_path), raising=False)
    db.init_db()
    yield


def test_upsert_and_get_pv_calibration_hourly_round_trip():
    from src import db
    factors = {6: 0.7, 12: 0.85, 18: 0.15}
    samples = {6: 20, 12: 30, 18: 25}
    n = db.upsert_pv_calibration_hourly(factors, samples, window_days=30)
    assert n == 3
    got = db.get_pv_calibration_hourly()
    assert got == factors


def test_upsert_replaces_existing_hour():
    from src import db
    db.upsert_pv_calibration_hourly({12: 0.5}, {12: 10}, 30)
    db.upsert_pv_calibration_hourly({12: 0.85}, {12: 50}, 30)
    got = db.get_pv_calibration_hourly()
    assert got[12] == 0.85


def test_get_pv_calibration_hourly_empty_returns_empty_dict():
    from src import db
    assert db.get_pv_calibration_hourly() == {}


def test_compute_skips_when_pv_realtime_history_empty():
    from src.weather import compute_pv_calibration_hourly_table
    status = compute_pv_calibration_hourly_table()
    assert status["status"] == "skipped"
    assert "no pv_realtime_history" in status["reason"]


def test_forecast_to_lp_inputs_accepts_per_hour_dict():
    """When pv_scale is a dict, the per-slot factor is looked up by UTC hour."""
    from src.weather import HourlyForecast, forecast_to_lp_inputs

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    slots = [base.replace(hour=h) for h in (10, 12, 17)]  # 10am, midday, 5pm
    forecast = [
        HourlyForecast(
            time_utc=base.replace(hour=h),
            temperature_c=15.0,
            cloud_cover_pct=0.0,
            shortwave_radiation_wm2=500.0,
            estimated_pv_kw=0.0,
            heating_demand_factor=0.0,
        )
        for h in range(0, 24)
    ]
    factors = {10: 0.5, 12: 0.9, 17: 0.1}
    series = forecast_to_lp_inputs(forecast, slots, pv_scale=factors)
    # Same irradiance everywhere; per-hour factor should make midday > 10am > 5pm.
    assert series.pv_kwh_per_slot[1] > series.pv_kwh_per_slot[0]
    assert series.pv_kwh_per_slot[0] > series.pv_kwh_per_slot[2]


def test_forecast_to_lp_inputs_dict_falls_back_to_median_for_missing_hours():
    """A slot whose hour isn't in the dict uses the median of provided values."""
    from src.weather import HourlyForecast, forecast_to_lp_inputs

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    slots = [base.replace(hour=h) for h in (10, 12, 23)]  # 23 is not in the dict
    forecast = [
        HourlyForecast(
            time_utc=base.replace(hour=h),
            temperature_c=15.0,
            cloud_cover_pct=0.0,
            shortwave_radiation_wm2=500.0,
            estimated_pv_kw=0.0,
            heating_demand_factor=0.0,
        )
        for h in range(0, 24)
    ]
    factors = {10: 0.5, 12: 0.9}  # median (upper-half) = 0.9
    series = forecast_to_lp_inputs(forecast, slots, pv_scale=factors)
    # The 23h slot uses the dict's median fallback (0.9) — should equal or exceed
    # the 10h slot value (which uses 0.5). Ceiling caps may equalise with 12h.
    assert series.pv_kwh_per_slot[2] > series.pv_kwh_per_slot[0]


def test_forecast_to_lp_inputs_float_scale_still_works():
    """Legacy float-scalar path must remain unchanged."""
    from src.weather import HourlyForecast, forecast_to_lp_inputs

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    slots = [base, base.replace(hour=13)]
    forecast = [
        HourlyForecast(
            time_utc=base.replace(hour=h),
            temperature_c=15.0,
            cloud_cover_pct=0.0,
            shortwave_radiation_wm2=500.0,
            estimated_pv_kw=0.0,
            heating_demand_factor=0.0,
        )
        for h in range(0, 24)
    ]
    series_a = forecast_to_lp_inputs(forecast, slots, pv_scale=1.0)
    series_b = forecast_to_lp_inputs(forecast, slots, pv_scale=0.5)
    # Halving the scale should halve PV (within ceiling).
    for a, b in zip(series_a.pv_kwh_per_slot, series_b.pv_kwh_per_slot):
        if a > 0.001:
            assert b == pytest.approx(a / 2, abs=0.05) or b < a
