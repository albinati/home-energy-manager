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


def test_rebuild_applies_recent_bias_factor_like_the_lp_does(monkeypatch):
    """#486 follow-up: the LP weather builder multiplies the calibrated PV
    forecast by the adaptive recent-bias factor. The skill log must measure
    that SAME forecast, or its pv_bias column reports a phantom
    pre-correction residual (the skew class PR L3 fixed for the 3D table)."""
    from src.config import config as app_config

    fetch_at = "2026-05-02T09:00:00+00:00"
    db.save_meteo_forecast_history(
        fetch_at,
        [
            {
                "slot_time": "2026-05-02T10:00:00+00:00",
                "temp_c": 14.0,
                "solar_w_m2": 800.0,
                "cloud_cover_pct": 20.0,
                "direct_pv_kw": 2.0,
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

    # Baseline: corrector disabled → factor must NOT apply.
    monkeypatch.setattr(app_config, "PV_RECENT_BIAS_ENABLED", False, raising=False)
    db.rebuild_forecast_skill_log_for_date("2026-05-02")
    base = db.get_forecast_skill_rows("2026-05-02", "2026-05-02")
    base_pv = next(r["predicted_pv_kwh"] for r in base if r["hour_of_day"] == 10)
    assert base_pv is not None and base_pv > 0

    # Enabled + factor 1.5 for hour 10 → predicted_pv scales by exactly 1.5.
    db.upsert_pv_recent_bias({10: 1.5}, {10: 1.5}, {10: 5}, "2026-05-02T04:20:00Z")
    monkeypatch.setattr(app_config, "PV_RECENT_BIAS_ENABLED", True, raising=False)
    db.rebuild_forecast_skill_log_for_date("2026-05-02")
    rows = db.get_forecast_skill_rows("2026-05-02", "2026-05-02")
    biased_pv = next(r["predicted_pv_kwh"] for r in rows if r["hour_of_day"] == 10)
    assert biased_pv == pytest.approx(base_pv * 1.5, rel=1e-6)

    # Hours without a factor fall back to 1.0 (no row for hour 11 → untouched).
    assert db.get_pv_recent_bias() == {10: 1.5}
