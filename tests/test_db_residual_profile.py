"""db.half_hourly_residual_load_profile_kwh — Daikin-subtracted load profile.

S10.13 (#179): the LP energy balance ``imp + pv + dis == base_load + exp +
chg + (e_dhw + e_space)`` adds physics-predicted Daikin on top of base_load.
Once base_load source switched to real Fox load_power_kw (#176) — which is
HOUSE TOTAL including Daikin's actual past draw — Daikin gets counted twice.
This profile subtracts the same physics estimator the LP uses for prediction
to back out the residual (non-Daikin) load.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _seed_pv(t: datetime, load_kw: float) -> None:
    db.save_pv_realtime_sample(t.isoformat().replace("+00:00", "Z"), load_power_kw=load_kw)


def _seed_meteo(t: datetime, temp_c: float) -> None:
    """Seed a meteo_forecast_history row at the hour boundary so SUBSTR matching works."""
    hour_iso = t.replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "+00:00")
    db.save_meteo_forecast_history(
        t.isoformat().replace("+00:00", "Z"),
        [{"slot_time": hour_iso, "temp_c": temp_c, "solar_w_m2": 0.0}],
    )


def test_warm_outdoor_no_daikin_subtraction() -> None:
    """At outdoor > DAIKIN_WEATHER_CURVE_HIGH_C (default 18), physics yields zero
    Daikin draw → residual equals raw load (minus DHW standing-loss only)."""
    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0)
    _seed_meteo(base, temp_c=25.0)  # warm — Daikin off
    _seed_pv(base, load_kw=0.6)
    _seed_pv(base + timedelta(minutes=10), load_kw=0.6)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    # Bucket should be near load × 0.5h = 0.3 kWh (DHW standing loss tiny, < 0.05 kWh)
    found = [v for v in profile.values() if 0.25 < v < 0.31]
    assert found, f"expected at least one bucket near 0.3 kWh (no Daikin subtraction); got {sorted(set(round(v,3) for v in profile.values()))[:8]}"


def test_cold_outdoor_subtracts_daikin_space() -> None:
    """At cold outdoor (5 °C), the climate curve predicts non-trivial space heating
    draw → residual < raw load."""
    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0)
    _seed_meteo(base, temp_c=5.0)  # cold
    _seed_pv(base, load_kw=1.5)
    _seed_pv(base + timedelta(minutes=10), load_kw=1.5)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    # Raw load × 0.5h = 0.75 kWh. With Daikin space subtracted, residual must be < 0.75.
    found_residuals = [v for v in profile.values() if 0.0 <= v < 0.75]
    assert found_residuals, (
        f"expected residual buckets < 0.75 kWh after Daikin subtraction; "
        f"profile values: {sorted(set(round(v,3) for v in profile.values()))[:8]}"
    )


def test_no_outdoor_data_falls_back_to_zero_daikin() -> None:
    """Sample without a matching meteo_forecast_history row → fallback outdoor 25 °C
    → physics yields zero Daikin → residual equals raw load (conservative)."""
    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0)
    # No meteo seeded → fallback should kick in
    _seed_pv(base, load_kw=0.4)
    _seed_pv(base + timedelta(minutes=10), load_kw=0.4)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    found = [v for v in profile.values() if 0.18 < v < 0.21]
    assert found, (
        f"with no outdoor data, residual should ≈ raw load × 0.5h (≈ 0.2 kWh); "
        f"got {sorted(set(round(v,3) for v in profile.values()))[:8]}"
    )


def test_residual_never_negative() -> None:
    """Subtracted Daikin estimate must not push residual below zero."""
    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0)
    # Very cold → Daikin estimate will be largeish; very small load
    _seed_meteo(base, temp_c=-10.0)
    _seed_pv(base, load_kw=0.05)  # tiny load
    _seed_pv(base + timedelta(minutes=10), load_kw=0.05)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    assert all(v >= 0 for v in profile.values()), (
        f"residual went negative: {[(k,v) for k,v in profile.items() if v<0]}"
    )
