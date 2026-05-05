"""Cloud-aware PV calibration (per-hour × per-cloud-bucket).

PR #232 — extends the per-hour table with a cloud-bucket dimension. Empirical
observation (Open Climate Fix): cloud-cover bucket explains roughly 60% of
forecast residual variance once you've already corrected for hour-of-day.

The fallback chain:
    1. cloud-aware ``pv_calibration_hourly_cloud[(hour, bucket)]``
    2. per-hour ``pv_calibration_hourly[hour]``                  (PR #186)
    3. flat factor                                                (legacy)
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.config import config as app_config
from src.weather import (
    HourlyForecast,
    cloud_bucket,
    forecast_to_lp_inputs,
    get_pv_calibration_factor_for,
)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


# ---------- cloud_bucket ----------

@pytest.mark.parametrize("pct,expected", [
    (0.0, 0), (24.99, 0),
    (25.0, 1), (49.99, 1),
    (50.0, 2), (74.99, 2),
    (75.0, 3), (100.0, 3),
])
def test_cloud_bucket_boundaries(pct: float, expected: int) -> None:
    assert cloud_bucket(pct) == expected


def test_cloud_bucket_none_defaults_to_partly() -> None:
    """Missing cloud_cover (very old meteo rows) → bucket 1 (middle)."""
    assert cloud_bucket(None) == 1


# ---------- get_pv_calibration_factor_for ----------

def test_resolver_prefers_cloud_table_when_cell_exists() -> None:
    cloud = {(12, 0): 0.95, (12, 3): 0.45}    # clear vs overcast at noon
    hourly = {12: 0.70}
    assert get_pv_calibration_factor_for(12, 5.0, cloud_table=cloud,
                                          hourly_table=hourly, flat=1.0) == 0.95
    assert get_pv_calibration_factor_for(12, 90.0, cloud_table=cloud,
                                          hourly_table=hourly, flat=1.0) == 0.45


def test_resolver_falls_back_to_hourly_when_cloud_cell_missing() -> None:
    """Cloud table only has clear noon; mid-cloud at noon → fall back to hourly."""
    cloud = {(12, 0): 0.95}
    hourly = {12: 0.70}
    # cloud_pct=60 → bucket 2 (mostly), not in cloud_table → use hourly
    assert get_pv_calibration_factor_for(12, 60.0, cloud_table=cloud,
                                          hourly_table=hourly, flat=1.0) == 0.70


def test_resolver_falls_back_to_flat_when_neither_table_has_data() -> None:
    assert get_pv_calibration_factor_for(8, 30.0, cloud_table={},
                                          hourly_table={}, flat=0.55) == 0.55


def test_resolver_handles_none_cloud_pct() -> None:
    """None cloud → bucket 1 (partly). Should still honor cloud table if cell exists."""
    cloud = {(10, 1): 0.80}
    hourly = {10: 0.60}
    assert get_pv_calibration_factor_for(10, None, cloud_table=cloud,
                                          hourly_table=hourly, flat=1.0) == 0.80


# ---------- forecast_to_lp_inputs callable branch ----------

def _hourly(time_utc: datetime, *, irr: float = 600.0,
            cloud: float = 50.0, temp: float = 15.0) -> HourlyForecast:
    return HourlyForecast(
        time_utc=time_utc, temperature_c=temp,
        shortwave_radiation_wm2=irr, cloud_cover_pct=cloud,
        estimated_pv_kw=0.0, heating_demand_factor=1.0,
    )


def test_forecast_to_lp_inputs_accepts_callable_pv_scale() -> None:
    """Callable pv_scale receives (hour, cloud_pct) and returns the per-slot factor."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    forecast = [_hourly(base + timedelta(hours=h), irr=800.0, cloud=20.0) for h in range(2)]
    starts = [base, base + timedelta(minutes=30)]

    seen: list[tuple[int, float]] = []
    def cb(hour: int, cloud: float) -> float:
        seen.append((hour, cloud))
        return 0.5  # half the raw forecast

    out = forecast_to_lp_inputs(forecast, starts, pv_scale=cb)
    assert len(seen) == 2
    assert all(h == 12 for h, _ in seen)
    assert all(out.pv_kwh_per_slot[i] >= 0.0 for i in range(2))


def test_forecast_to_lp_inputs_callable_zero_factor_zeros_pv() -> None:
    """A callable returning 0.0 should drive PV to 0 (overcast bucket fully zeroes)."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    forecast = [_hourly(base, irr=800.0, cloud=80.0)]
    starts = [base]
    out = forecast_to_lp_inputs(forecast, starts, pv_scale=lambda h, c: 0.0)
    assert out.pv_kwh_per_slot[0] == 0.0


def test_direct_pv_path_still_uses_calibration_tables() -> None:
    """Quartz direct PV should still be corrected by the site calibration tables."""
    from src.weather import forecast_pv_kw_from_row

    cloud = {(12, 1): 0.50}
    hourly = {12: 0.75}
    pv = forecast_pv_kw_from_row(
        12,
        0.0,
        40.0,
        direct_pv_kw=2.0,
        cloud_table=cloud,
        hourly_table=hourly,
        flat=1.0,
    )
    assert pv == pytest.approx(1.0)


def test_forecast_to_lp_inputs_callable_exception_falls_back_safely() -> None:
    """If callable raises, slot scale falls back to 1.0 (no crash)."""
    base = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    forecast = [_hourly(base, irr=600.0, cloud=50.0)]
    starts = [base]
    def bad(_h: int, _c: float) -> float:
        raise RuntimeError("boom")
    out = forecast_to_lp_inputs(forecast, starts, pv_scale=bad)
    assert out.pv_kwh_per_slot[0] > 0.0       # still produced PV (used scale=1.0)


# ---------- DB round-trip ----------

def test_upsert_and_get_cloud_table_roundtrip() -> None:
    factors = {(10, 0): 0.92, (10, 3): 0.31, (12, 1): 0.78}
    samples = {(10, 0): 12, (10, 3): 5, (12, 1): 9}
    n = db.upsert_pv_calibration_hourly_cloud(factors, samples, window_days=30)
    assert n == 3
    out = db.get_pv_calibration_hourly_cloud()
    assert out[(10, 0)] == 0.92
    assert out[(10, 3)] == 0.31
    assert out[(12, 1)] == 0.78


def test_upsert_replaces_existing_cell() -> None:
    db.upsert_pv_calibration_hourly_cloud({(10, 0): 0.50}, {(10, 0): 5}, 30)
    db.upsert_pv_calibration_hourly_cloud({(10, 0): 0.85}, {(10, 0): 12}, 30)
    out = db.get_pv_calibration_hourly_cloud()
    assert out[(10, 0)] == 0.85
