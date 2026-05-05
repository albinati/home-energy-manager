from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src import db


@pytest.fixture(autouse=True)
def _db_ready():
    db.init_db()
    yield


def test_rebuild_forecast_skill_log_for_date_uses_latest_prior_fetch_and_actuals():
    forecast_fetch_early = "2026-05-02T09:00:00+00:00"
    forecast_fetch_late = "2026-05-02T09:30:00+00:00"
    db.save_meteo_forecast_history(
        forecast_fetch_early,
        [
            {
                "slot_time": "2026-05-02T10:00:00+00:00",
                "temp_c": 12.0,
                "solar_w_m2": 400.0,
                "cloud_cover_pct": 60.0,
            }
        ],
    )
    db.save_meteo_forecast_history(
        forecast_fetch_late,
        [
            {
                "slot_time": "2026-05-02T10:00:00+00:00",
                "temp_c": 14.0,
                "solar_w_m2": 800.0,
                "cloud_cover_pct": 20.0,
            }
        ],
    )

    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO pv_realtime_history
               (captured_at, solar_power_kw, load_power_kw, soc_pct, source)
               VALUES (?, ?, ?, ?, ?)""",
            ("2026-05-02T10:05:00+00:00", 2.0, 0.5, 55.0, "test"),
        )
        conn.execute(
            """INSERT INTO pv_realtime_history
               (captured_at, solar_power_kw, load_power_kw, soc_pct, source)
               VALUES (?, ?, ?, ?, ?)""",
            ("2026-05-02T10:35:00+00:00", 4.0, 0.8, 56.0, "test"),
        )
        conn.execute(
            """INSERT INTO daikin_telemetry
               (fetched_at, source, outdoor_temp_c)
               VALUES (?, ?, ?)""",
            (datetime(2026, 5, 2, 10, 10, tzinfo=UTC).timestamp(), "live", 15.0),
        )
        conn.execute(
            """INSERT INTO daikin_telemetry
               (fetched_at, source, outdoor_temp_c)
               VALUES (?, ?, ?)""",
            (datetime(2026, 5, 2, 10, 40, tzinfo=UTC).timestamp(), "live", 17.0),
        )
        conn.commit()
    finally:
        conn.close()

    rows_written = db.rebuild_forecast_skill_log_for_date("2026-05-02")
    assert rows_written == 1

    rows = db.get_forecast_skill_rows("2026-05-02", "2026-05-02")
    assert len(rows) == 1
    row = rows[0]
    assert row["date_utc"] == "2026-05-02"
    assert row["hour_of_day"] == 10
    assert row["predicted_temp_c"] == pytest.approx(14.0)
    assert row["actual_temp_c"] == pytest.approx(16.0)
    assert db.get_micro_climate_offset_c(lookback=1) == pytest.approx(2.0)
    assert db.get_micro_climate_offset_by_hour_c(lookback=1)[10] == pytest.approx(2.0)
    assert row["actual_pv_kwh"] == pytest.approx(3.0)
    assert row["predicted_pv_kwh"] is not None
    assert row["built_at_utc"]


def test_rebuild_forecast_skill_log_skips_same_timestamp_fetches():
    fetch_at = "2026-05-02T10:00:00+00:00"
    db.save_meteo_forecast_history(
        fetch_at,
        [
            {
                "slot_time": "2026-05-02T10:00:00+00:00",
                "temp_c": 14.0,
                "solar_w_m2": 800.0,
                "cloud_cover_pct": 20.0,
            }
        ],
    )
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO pv_realtime_history
               (captured_at, solar_power_kw, load_power_kw, soc_pct, source)
               VALUES (?, ?, ?, ?, ?)""",
            ("2026-05-02T10:05:00+00:00", 2.0, 0.5, 55.0, "test"),
        )
        conn.commit()
    finally:
        conn.close()

    rows_written = db.rebuild_forecast_skill_log_for_date("2026-05-02")
    assert rows_written == 0
    assert db.get_forecast_skill_rows("2026-05-02", "2026-05-02") == []
