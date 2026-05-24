"""PR L1.1 integration tests — exercise the REAL compute functions
against a seeded ``meteo_forecast_value`` + ``pv_realtime_history``
dataset. Catches granularity/unit bugs (H1, H2 from L1.1 review) that
mock-based tests miss.

The tests verify:
1. Half-hour aggregation correctly produces hourly kWh
2. The latest pre-slot fetch wins (revision picking)
3. The single-half-only edge case extrapolates by 2x
4. The cloud-bucket aggregation averages cloud_cover across halves
5. Computed factor matches expected actual/forecast ratio
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.config import config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(config, "PV_CALIBRATION_WINDOW_DAYS", 30, raising=False)
    monkeypatch.setattr(config, "WEATHER_LAT", "51.494", raising=False)
    monkeypatch.setattr(config, "WEATHER_LON", "-0.275", raising=False)
    db.init_db()


def _seed_meteo_snapshot(
    conn: sqlite3.Connection,
    slot_time_utc: datetime,
    direct_pv_kw: float,
    cloud_pct: float | None = 20.0,
    fetch_at_utc: datetime | None = None,
) -> None:
    """Seed one meteo_forecast_value row (and its parent snapshot)."""
    if fetch_at_utc is None:
        fetch_at_utc = slot_time_utc - timedelta(hours=1)
    fetch_iso = fetch_at_utc.isoformat().replace("+00:00", "Z")
    slot_iso = slot_time_utc.isoformat().replace("+00:00", "Z")
    # Parent snapshot (UNIQUE key)
    conn.execute(
        "INSERT OR IGNORE INTO meteo_forecast_snapshot (forecast_fetch_at_utc, source)"
        " VALUES (?, ?)",
        (fetch_iso, "quartz"),
    )
    conn.execute(
        """INSERT OR REPLACE INTO meteo_forecast_value
           (forecast_fetch_at_utc, slot_time, direct_pv_kw, cloud_cover_pct,
            solar_w_m2, temp_c)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fetch_iso, slot_iso, direct_pv_kw, cloud_pct, 0.0, 15.0),
    )


def _seed_actual(
    conn: sqlite3.Connection,
    captured_at_utc: datetime,
    solar_kw: float,
) -> None:
    """Seed one pv_realtime_history row."""
    conn.execute(
        """INSERT INTO pv_realtime_history
           (captured_at, solar_power_kw, soc_pct, load_power_kw, source)
           VALUES (?, ?, ?, ?, ?)""",
        (
            captured_at_utc.isoformat().replace("+00:00", "Z"),
            solar_kw, 50.0, 0.3, "test",
        ),
    )


# ---------------------------------------------------------------------------
# Per-hour table
# ---------------------------------------------------------------------------


def test_compute_hourly_aggregates_two_halves_correctly():
    """For one hour with Quartz forecasts at :00 (3.0 kW) + :30 (2.0 kW),
    expected hourly kWh = 3.0×0.5 + 2.0×0.5 = 2.5 kWh.
    Actual mean kW over the hour = 2.0 → ratio = 2.0 / 2.5 = 0.8.

    We seed enough days that hour passes the min_samples_per_hour=7 floor.
    """
    from src.weather import compute_pv_calibration_hourly_table

    conn = sqlite3.connect(config.DB_PATH)
    today = datetime.now(UTC).date()
    # Seed 8 days of hour-12 data, each with 3.0 kW @:00 + 2.0 kW @:30 forecast
    # and 2.0 mean kW actual.
    for d in range(1, 9):
        day = today - timedelta(days=d)
        base = datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)
        # Two half-hour Quartz rows
        _seed_meteo_snapshot(conn, base, direct_pv_kw=3.0)
        _seed_meteo_snapshot(conn, base + timedelta(minutes=30), direct_pv_kw=2.0)
        # Actual samples: four readings of 2.0 kW over the hour → mean 2.0
        for m in (5, 20, 35, 50):
            _seed_actual(conn, base + timedelta(minutes=m), solar_kw=2.0)
    conn.commit()
    conn.close()

    result = compute_pv_calibration_hourly_table(window_days=30, min_samples_per_hour=7)
    assert result["status"] == "ok", result
    factors = db.get_pv_calibration_hourly()
    assert 12 in factors, f"hour 12 should be present in factors; got {factors}"
    # Expected ratio: actual mean kW (=2.0 kWh/h) / modelled hourly kWh (=2.5)
    # = 0.8. Clamped within [0.10, 2.0].
    assert factors[12] == pytest.approx(0.8, abs=0.05), (
        f"Expected ratio 0.8 (= 2.0 actual / 2.5 modelled); got {factors[12]}. "
        "If = 1.0, H1 bug still active (only :30 forecast used = 2.0; "
        "ratio 2.0/2.0=1.0). If = 0.67, ratio uses sum of halves without ×0.5."
    )


def test_compute_hourly_latest_pre_slot_fetch_wins():
    """When the same slot has multiple Quartz fetches, the LATEST
    PRE-SLOT one should be used (the value the LP saw). Verify the
    revision picker behaviour."""
    from src.weather import compute_pv_calibration_hourly_table

    conn = sqlite3.connect(config.DB_PATH)
    today = datetime.now(UTC).date()
    for d in range(1, 9):
        day = today - timedelta(days=d)
        base = datetime(day.year, day.month, day.day, 14, 0, tzinfo=UTC)
        # OLD fetch (8h before slot) — value 1.0 kW. Should be IGNORED.
        _seed_meteo_snapshot(conn, base, direct_pv_kw=1.0,
                              fetch_at_utc=base - timedelta(hours=8))
        # NEWER fetch (30 min before slot) — value 3.0 kW. Should WIN.
        _seed_meteo_snapshot(conn, base, direct_pv_kw=3.0,
                              fetch_at_utc=base - timedelta(minutes=30))
        # Mirror :30 slot
        s30 = base + timedelta(minutes=30)
        _seed_meteo_snapshot(conn, s30, direct_pv_kw=3.0,
                              fetch_at_utc=s30 - timedelta(minutes=30))
        # Actual 3.0 mean
        for m in (10, 40):
            _seed_actual(conn, base + timedelta(minutes=m), solar_kw=3.0)
    conn.commit()
    conn.close()

    result = compute_pv_calibration_hourly_table(window_days=30, min_samples_per_hour=7)
    assert result["status"] == "ok", result
    factors = db.get_pv_calibration_hourly()
    # If the latest fetch wins (3.0 kW per half), modelled hourly kWh = 3.0×0.5 + 3.0×0.5 = 3.0
    # actual mean = 3.0 → ratio 3.0/3.0 = 1.0
    # If the OLD fetch leaked in (1.0 kW), ratio = 3.0/2.0 = 1.5 (different)
    assert factors[14] == pytest.approx(1.0, abs=0.05), (
        f"Latest-fetch picker broken; got {factors[14]} (expected ~1.0). "
        "1.5 would indicate the old fetch leaked into the calc."
    )


def test_compute_hourly_single_half_extrapolates():
    """When only ONE of the two half-hours has data (e.g. dawn/dusk where
    Quartz starts/stops mid-hour), modelled hourly kWh should be the
    single half × 1 (effectively 2× to fill the missing half)."""
    from src.weather import compute_pv_calibration_hourly_table

    conn = sqlite3.connect(config.DB_PATH)
    today = datetime.now(UTC).date()
    for d in range(1, 9):
        day = today - timedelta(days=d)
        # Only :00 half present, no :30
        slot = datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)
        _seed_meteo_snapshot(conn, slot, direct_pv_kw=2.0)
        # actual 2.0 mean
        _seed_actual(conn, slot + timedelta(minutes=10), solar_kw=2.0)
        _seed_actual(conn, slot + timedelta(minutes=20), solar_kw=2.0)
    conn.commit()
    conn.close()

    result = compute_pv_calibration_hourly_table(window_days=30, min_samples_per_hour=7)
    assert result["status"] == "ok", result
    factors = db.get_pv_calibration_hourly()
    # modelled hourly kWh = 2.0 × 0.5 × 2 (extrapolation) = 2.0
    # actual mean kW = 2.0
    # ratio = 1.0
    assert factors[6] == pytest.approx(1.0, abs=0.05), (
        f"Single-half extrapolation broken; got {factors[6]}. "
        "= 2.0 means extrapolation didn't fire (only counted 0.5h instead of 1h)."
    )


def test_compute_hourly_no_quartz_data_returns_skipped():
    """When meteo_forecast_value has NO direct_pv_kw rows in window
    (cold-start before Quartz was enabled), compute returns skipped."""
    from src.weather import compute_pv_calibration_hourly_table

    conn = sqlite3.connect(config.DB_PATH)
    today = datetime.now(UTC).date()
    base = datetime(today.year, today.month, today.day, 12, 0, tzinfo=UTC)
    # Only actuals, no forecasts
    _seed_actual(conn, base, solar_kw=2.0)
    conn.commit()
    conn.close()

    result = compute_pv_calibration_hourly_table(window_days=30)
    assert result["status"] == "skipped"
    assert "no quartz" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Cloud-bucket table
# ---------------------------------------------------------------------------


def test_compute_cloud_aware_bucket_assignment():
    """Cloud bucket from average of half-hour cloud_cover values."""
    from src.weather import compute_pv_calibration_hourly_cloud_table

    conn = sqlite3.connect(config.DB_PATH)
    today = datetime.now(UTC).date()
    # Seed 5 days of hour 13 with cloud_cover 10% (clear bucket = 0)
    # Quartz at 4.0 kW per half-hour, actual 3.0 mean kW
    for d in range(1, 6):
        day = today - timedelta(days=d)
        base = datetime(day.year, day.month, day.day, 13, 0, tzinfo=UTC)
        _seed_meteo_snapshot(conn, base, direct_pv_kw=4.0, cloud_pct=10.0)
        _seed_meteo_snapshot(conn, base + timedelta(minutes=30),
                              direct_pv_kw=4.0, cloud_pct=10.0)
        for m in (5, 25, 45):
            _seed_actual(conn, base + timedelta(minutes=m), solar_kw=3.0)
    conn.commit()
    conn.close()

    result = compute_pv_calibration_hourly_cloud_table(window_days=30, min_samples_per_cell=4)
    assert result["status"] == "ok", result
    factors = db.get_pv_calibration_hourly_cloud()
    # Expect (13, 0) bucket present, ratio = 3.0 / (4.0×0.5 + 4.0×0.5) = 3.0/4.0 = 0.75
    assert (13, 0) in factors, f"(13, 0) clear bucket should be present; got {factors}"
    assert factors[(13, 0)] == pytest.approx(0.75, abs=0.05), (
        f"Expected ratio 0.75; got {factors[(13, 0)]}"
    )
