from __future__ import annotations

import json

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
            """INSERT INTO execution_log
               (timestamp, daikin_outdoor_temp)
               VALUES (?, ?)""",
            ("2026-05-02T10:10:00+00:00", 15.0),
        )
        conn.execute(
            """INSERT INTO execution_log
               (timestamp, daikin_outdoor_temp)
               VALUES (?, ?)""",
            ("2026-05-02T10:40:00+00:00", 17.0),
        )
        conn.commit()
    finally:
        conn.close()

    run_id = db.log_optimizer_run({
        "run_at": "2026-05-02T09:45:00+00:00",
        "rates_count": 2,
        "cheap_slots": 0,
        "peak_slots": 0,
        "standard_slots": 2,
        "negative_slots": 0,
        "target_vwap": 0.0,
        "actual_agile_mean": 0.0,
        "battery_warning": False,
        "strategy_summary": "test",
        "fox_schedule_uploaded": False,
        "daikin_actions_count": 0,
    })
    db.save_lp_snapshots(
        run_id,
        {
            "run_at_utc": "2026-05-02T09:45:00+00:00",
            "plan_date": "2026-05-02",
            "horizon_hours": 1,
            "soc_initial_kwh": 5.0,
            "tank_initial_c": 45.0,
            "indoor_initial_c": 21.0,
            "soc_source": "test",
            "tank_source": "test",
            "indoor_source": "test",
            "base_load_json": json.dumps([0.20, 0.30]),
            "micro_climate_offset_c": 0.0,
            "forecast_fetch_at_utc": forecast_fetch_late,
            "exogenous_snapshot_json": "{}",
            "config_snapshot_json": "{}",
            "price_quantize_p": 0.0,
            "peak_threshold_p": 30.0,
            "cheap_threshold_p": 10.0,
            "daikin_control_mode": "passive",
            "optimization_preset": "test",
            "energy_strategy_mode": "savings_first",
        },
        [
            {
                "slot_index": 0,
                "slot_time_utc": "2026-05-02T10:00:00+00:00",
                "price_p": 20.0,
                "import_kwh": 0.0,
                "export_kwh": 0.0,
                "charge_kwh": 0.0,
                "discharge_kwh": 0.0,
                "pv_use_kwh": 0.0,
                "pv_curtail_kwh": 0.0,
                "dhw_kwh": 0.05,
                "space_kwh": 0.10,
                "soc_kwh": 5.0,
                "tank_temp_c": 45.0,
                "indoor_temp_c": 21.0,
                "outdoor_temp_c": 14.0,
                "lwt_offset_c": 0.0,
            },
            {
                "slot_index": 1,
                "slot_time_utc": "2026-05-02T10:30:00+00:00",
                "price_p": 20.0,
                "import_kwh": 0.0,
                "export_kwh": 0.0,
                "charge_kwh": 0.0,
                "discharge_kwh": 0.0,
                "pv_use_kwh": 0.0,
                "pv_curtail_kwh": 0.0,
                "dhw_kwh": 0.05,
                "space_kwh": 0.15,
                "soc_kwh": 5.0,
                "tank_temp_c": 45.0,
                "indoor_temp_c": 21.0,
                "outdoor_temp_c": 14.0,
                "lwt_offset_c": 0.0,
            },
        ],
    )

    rows_written = db.rebuild_forecast_skill_log_for_date("2026-05-02")
    assert rows_written == 1

    rows = db.get_forecast_skill_rows("2026-05-02", "2026-05-02")
    assert len(rows) == 1
    row = rows[0]
    assert row["date_utc"] == "2026-05-02"
    assert row["hour_of_day"] == 10
    assert row["predicted_temp_c"] == pytest.approx(14.0)
    assert row["actual_temp_c"] == pytest.approx(16.0)
    assert row["actual_pv_kwh"] == pytest.approx(3.0)
    assert row["predicted_pv_kwh"] is not None
    assert row["actual_load_kwh"] == pytest.approx(0.65)
    assert row["predicted_load_kwh"] == pytest.approx(0.85)
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
