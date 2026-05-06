"""Phase 0 — LP snapshots, forecast history, and config audit.

Covers:
* Migrations create the three new V11 tables with the right columns.
* ``save_lp_snapshots`` round-trips inputs + per-slot rows and ``find_run_for_time``
  picks the right run.
* ``save_meteo_forecast_history`` preserves each fetch separately (unlike
  ``save_meteo_forecast`` which overwrites by ``slot_time``).
* ``log_config_change`` appends and ``get_config_audit`` reads back in
  reverse-chronological order, filterable by key.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlite3

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _db_ready():
    db.init_db()
    yield


def _isoformat(dt: datetime) -> str:
    return dt.isoformat()


def _columns(table: str) -> set[str]:
    conn = db.get_connection()
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return {str(r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_lp_solution_snapshot_has_expected_columns():
    cols = _columns("lp_solution_snapshot")
    for expected in (
        "id", "run_id", "slot_index", "slot_time_utc", "price_p",
        "import_kwh", "export_kwh", "charge_kwh", "discharge_kwh",
        "pv_use_kwh", "pv_curtail_kwh", "dhw_kwh", "space_kwh",
        "soc_kwh", "tank_temp_c", "indoor_temp_c", "outdoor_temp_c",
        "lwt_offset_c",
    ):
        assert expected in cols, f"missing column {expected}"


def test_lp_inputs_snapshot_has_expected_columns():
    cols = _columns("lp_inputs_snapshot")
    for expected in (
        "run_id", "run_at_utc", "plan_date", "horizon_hours",
        "soc_initial_kwh", "tank_initial_c", "indoor_initial_c",
        "soc_source", "tank_source", "indoor_source",
        "base_load_json", "micro_climate_offset_c", "config_snapshot_json",
        "exogenous_snapshot_json",
        "price_quantize_p", "peak_threshold_p", "cheap_threshold_p",
        "daikin_control_mode", "optimization_preset", "energy_strategy_mode",
        "forecast_fetch_at_utc",
        # V11-A (#194): nullable columns reserved for V11-C/D
        "dhw_draw_prior_json", "occupancy_prior_json",
    ):
        assert expected in cols, f"missing column {expected}"


def test_meteo_forecast_snapshot_has_expected_columns():
    cols = _columns("meteo_forecast_snapshot")
    for expected in (
        "forecast_fetch_at_utc", "source", "model_name", "model_version", "raw_payload_json",
    ):
        assert expected in cols, f"missing column {expected}"


def test_meteo_forecast_value_has_expected_columns():
    cols = _columns("meteo_forecast_value")
    for expected in (
        "id", "forecast_fetch_at_utc", "slot_time", "temp_c", "solar_w_m2", "cloud_cover_pct",
    ):
        assert expected in cols, f"missing column {expected}"


def test_meteo_forecast_history_has_expected_columns():
    cols = _columns("meteo_forecast_history")
    for expected in (
        "id", "forecast_fetch_at_utc", "slot_time", "temp_c", "solar_w_m2",
        # V11-A (#194): cloud_cover_pct closes the closed-loop replay gap
        "cloud_cover_pct",
    ):
        assert expected in cols, f"missing column {expected}"


def test_meteo_forecast_history_round_trips_cloud_cover():
    """V11-A: save_meteo_forecast_history persists cloud_cover_pct, and
    get_meteo_forecast_history_latest_before reads it back."""
    fetched_at = datetime.now(UTC).isoformat()
    rows = [
        {"slot_time": "2026-05-02T10:00:00+00:00", "temp_c": 14.2,
         "solar_w_m2": 540.0, "cloud_cover_pct": 35.0},
        {"slot_time": "2026-05-02T11:00:00+00:00", "temp_c": 15.1,
         "solar_w_m2": 620.0, "cloud_cover_pct": 22.0},
    ]
    db.save_meteo_forecast_history(fetched_at, rows)

    later = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    fetched = db.get_meteo_forecast_history_latest_before(later)
    assert len(fetched) == 2
    by_slot = {r["slot_time"]: r for r in fetched}
    assert by_slot["2026-05-02T10:00:00+00:00"]["cloud_cover_pct"] == pytest.approx(35.0)
    assert by_slot["2026-05-02T11:00:00+00:00"]["cloud_cover_pct"] == pytest.approx(22.0)


def test_meteo_forecast_history_handles_missing_cloud_gracefully():
    """Pre-V11-A snapshots have NULL cloud_cover_pct — the reader returns None
    rather than crashing, and the replay layer falls back to the legacy 0%
    attenuation path."""
    fetched_at = datetime.now(UTC).isoformat()
    rows = [
        {"slot_time": "2026-05-02T10:00:00+00:00", "temp_c": 14.2, "solar_w_m2": 540.0},
    ]
    db.save_meteo_forecast_history(fetched_at, rows)

    later = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    fetched = db.get_meteo_forecast_history_latest_before(later)
    assert len(fetched) == 1
    assert fetched[0].get("cloud_cover_pct") is None


def test_meteo_forecast_slot_date_lookup_uses_slot_time_not_forecast_date():
    rows = [
        {"slot_time": "2026-05-02T00:00:00+00:00", "temp_c": 10.0, "solar_w_m2": 100.0},
    ]
    db.save_meteo_forecast(rows, "2026-05-01")
    fetched = db.get_meteo_forecast_for_slot_date("2026-05-02")
    assert len(fetched) == 1
    assert fetched[0]["slot_time"] == "2026-05-02T00:00:00+00:00"
    assert fetched[0]["forecast_date"] == "2026-05-02"


def test_meteo_forecast_active_at_time_prefers_latest_past_slot():
    rows = [
        {"slot_time": "2026-05-02T00:00:00+00:00", "temp_c": 10.0, "solar_w_m2": 100.0},
        {"slot_time": "2026-05-02T01:00:00+00:00", "temp_c": 11.0, "solar_w_m2": 200.0},
    ]
    db.save_meteo_forecast(rows, "2026-05-01")
    row = db.get_meteo_forecast_at_time("2026-05-02T00:30:00+00:00")
    assert row is not None
    assert row["slot_time"] == "2026-05-02T00:00:00+00:00"



def test_config_audit_has_expected_columns():
    cols = _columns("config_audit")
    for expected in ("id", "key", "value", "op", "actor", "changed_at_utc"):
        assert expected in cols


def test_init_db_is_idempotent():
    # Second init must not raise — migrations use CREATE TABLE IF NOT EXISTS.
    db.init_db()
    db.init_db()


def test_init_db_migrates_old_lp_inputs_snapshot_before_creating_forecast_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE lp_inputs_snapshot (
                run_id INTEGER PRIMARY KEY,
                run_at_utc TEXT NOT NULL,
                plan_date TEXT,
                horizon_hours INTEGER,
                soc_initial_kwh REAL,
                tank_initial_c REAL,
                indoor_initial_c REAL,
                soc_source TEXT,
                tank_source TEXT,
                indoor_source TEXT,
                base_load_json TEXT,
                micro_climate_offset_c REAL,
                config_snapshot_json TEXT,
                price_quantize_p REAL,
                peak_threshold_p REAL,
                cheap_threshold_p REAL,
                daikin_control_mode TEXT,
                optimization_preset TEXT,
                energy_strategy_mode TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(app_config, "DB_PATH", str(db_path), raising=False)
    db.init_db()
    cols = _columns("lp_inputs_snapshot")
    assert "forecast_fetch_at_utc" in cols


# ---------------------------------------------------------------------------
# save_lp_snapshots round-trip
# ---------------------------------------------------------------------------

def _mk_run() -> int:
    """Create one optimizer_log row and return its id."""
    return db.log_optimizer_run({
        "run_at": datetime.now(UTC).isoformat(),
        "rates_count": 48,
        "cheap_slots": 5,
        "peak_slots": 3,
        "standard_slots": 36,
        "negative_slots": 0,
        "target_vwap": 18.5,
        "actual_agile_mean": 20.1,
        "battery_warning": False,
        "strategy_summary": "test run",
        "fox_schedule_uploaded": True,
        "daikin_actions_count": 6,
    })


def _mk_inputs_row() -> dict:
    return {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "plan_date": "2026-04-24",
        "horizon_hours": 24,
        "soc_initial_kwh": 5.1,
        "tank_initial_c": 46.0,
        "indoor_initial_c": 20.8,
        "soc_source": "fox_realtime_cache",
        "tank_source": "daikin_cache",
        "indoor_source": "daikin_cache",
        "base_load_json": json.dumps([0.4] * 48),
        "micro_climate_offset_c": 0.3,
        "exogenous_snapshot_json": json.dumps({"base_load_components": {"appliance_profile_kwh": [0.0] * 48}}),
        "config_snapshot_json": json.dumps({"LP_HORIZON_HOURS": 24, "BATTERY_CAPACITY_KWH": 10.0}),
        "price_quantize_p": 1.0,
        "peak_threshold_p": 25.0,
        "cheap_threshold_p": 12.0,
        "daikin_control_mode": "passive",
        "optimization_preset": "normal",
        "energy_strategy_mode": "savings_first",
    }


def _mk_solution_rows(n: int = 4) -> list[dict]:
    t0 = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        rows.append({
            "slot_index": i,
            "slot_time_utc": (t0 + timedelta(minutes=30 * i)).isoformat(),
            "price_p": 15.0 + i,
            "import_kwh": 0.1 * i,
            "export_kwh": 0.0,
            "charge_kwh": 0.2 if i < 2 else 0.0,
            "discharge_kwh": 0.0 if i < 2 else 0.1,
            "pv_use_kwh": 0.0,
            "pv_curtail_kwh": 0.0,
            "dhw_kwh": 0.0,
            "space_kwh": 0.0,
            "soc_kwh": 5.0 + 0.1 * i,
            "tank_temp_c": 46.0,
            "indoor_temp_c": 20.5,
            "outdoor_temp_c": 12.0,
            "lwt_offset_c": 0.0,
        })
    return rows


def test_save_lp_snapshots_round_trip():
    run_id = _mk_run()
    db.save_lp_snapshots(run_id, _mk_inputs_row(), _mk_solution_rows(4))

    inputs = db.get_lp_inputs(run_id)
    assert inputs is not None
    assert inputs["plan_date"] == "2026-04-24"
    assert inputs["soc_initial_kwh"] == pytest.approx(5.1)
    assert inputs["daikin_control_mode"] == "passive"
    # JSON fields survive the round-trip intact
    assert json.loads(inputs["config_snapshot_json"])["LP_HORIZON_HOURS"] == 24
    assert len(json.loads(inputs["base_load_json"])) == 48
    assert json.loads(inputs["exogenous_snapshot_json"])["base_load_components"]["appliance_profile_kwh"][0] == 0.0

    slots = db.get_lp_solution_slots(run_id)
    assert len(slots) == 4
    assert [s["slot_index"] for s in slots] == [0, 1, 2, 3]
    assert slots[0]["price_p"] == pytest.approx(15.0)
    assert slots[3]["soc_kwh"] == pytest.approx(5.3)


def test_save_lp_snapshots_replaces_on_same_run_id_and_slot_index():
    run_id = _mk_run()
    db.save_lp_snapshots(run_id, _mk_inputs_row(), _mk_solution_rows(2))
    # Re-run with different values for the same slot indices.
    updated = _mk_solution_rows(2)
    updated[0]["price_p"] = 99.0
    db.save_lp_snapshots(run_id, _mk_inputs_row(), updated)

    slots = db.get_lp_solution_slots(run_id)
    assert len(slots) == 2
    assert slots[0]["price_p"] == pytest.approx(99.0)


def test_find_run_for_time_picks_most_recent_before_when():
    t_first = datetime(2026, 4, 24, 6, 0, tzinfo=UTC)
    t_second = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    rid1 = db.log_optimizer_run({
        "run_at": t_first.isoformat(), "rates_count": 48,
        "cheap_slots": 0, "peak_slots": 0, "standard_slots": 48, "negative_slots": 0,
        "target_vwap": 18.0, "actual_agile_mean": 20.0, "battery_warning": False,
        "strategy_summary": "t1", "fox_schedule_uploaded": True, "daikin_actions_count": 0,
    })
    rid2 = db.log_optimizer_run({
        "run_at": t_second.isoformat(), "rates_count": 48,
        "cheap_slots": 0, "peak_slots": 0, "standard_slots": 48, "negative_slots": 0,
        "target_vwap": 18.0, "actual_agile_mean": 20.0, "battery_warning": False,
        "strategy_summary": "t2", "fox_schedule_uploaded": True, "daikin_actions_count": 0,
    })
    # At 10:00 we should see the 06:00 run.
    assert db.find_run_for_time(datetime(2026, 4, 24, 10, 0, tzinfo=UTC).isoformat()) == rid1
    # At 14:00 we should see the 12:00 run.
    assert db.find_run_for_time(datetime(2026, 4, 24, 14, 0, tzinfo=UTC).isoformat()) == rid2
    # Before any run → None.
    assert db.find_run_for_time(datetime(2026, 4, 24, 5, 0, tzinfo=UTC).isoformat()) is None


# ---------------------------------------------------------------------------
# meteo_forecast_history
# ---------------------------------------------------------------------------

def test_meteo_forecast_history_preserves_multiple_fetches_per_slot():
    slot = datetime(2026, 4, 24, 14, 0, tzinfo=UTC).isoformat()
    fetch_a = datetime(2026, 4, 24, 6, 0, tzinfo=UTC).isoformat()
    fetch_b = datetime(2026, 4, 24, 12, 0, tzinfo=UTC).isoformat()

    db.save_meteo_forecast_history(fetch_a, [{"slot_time": slot, "temp_c": 12.0, "solar_w_m2": 250.0}])
    db.save_meteo_forecast_history(fetch_b, [{"slot_time": slot, "temp_c": 13.5, "solar_w_m2": 180.0}])

    at_a = db.get_meteo_forecast_at(fetch_a)
    at_b = db.get_meteo_forecast_at(fetch_b)
    assert len(at_a) == 1 and at_a[0]["temp_c"] == pytest.approx(12.0)
    assert len(at_b) == 1 and at_b[0]["temp_c"] == pytest.approx(13.5)


def test_meteo_forecast_history_ignores_duplicate_fetch_slot():
    slot = datetime(2026, 4, 24, 14, 0, tzinfo=UTC).isoformat()
    fetch = datetime(2026, 4, 24, 6, 0, tzinfo=UTC).isoformat()
    db.save_meteo_forecast_history(fetch, [{"slot_time": slot, "temp_c": 10.0, "solar_w_m2": 100.0}])
    # INSERT OR IGNORE: the second write must not raise, and the first value stays.
    db.save_meteo_forecast_history(fetch, [{"slot_time": slot, "temp_c": 99.9, "solar_w_m2": 999.0}])
    rows = db.get_meteo_forecast_at(fetch)
    assert len(rows) == 1
    assert rows[0]["temp_c"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# config_audit
# ---------------------------------------------------------------------------

def test_log_config_change_appends_and_reads_back():
    db.log_config_change("DHW_TEMP_NORMAL_C", "45.0", op="set", actor="test")
    db.log_config_change("DHW_TEMP_NORMAL_C", "46.0", op="set", actor="test")
    rows = db.get_config_audit(key="DHW_TEMP_NORMAL_C")
    assert len(rows) >= 2
    # DESC order — newest first
    assert rows[0]["value"] == "46.0"
    assert rows[1]["value"] == "45.0"
    assert rows[0]["op"] == "set"


def test_log_config_change_delete_op_stores_null_value():
    db.log_config_change("OPTIMIZATION_PRESET", None, op="delete", actor="test")
    rows = db.get_config_audit(key="OPTIMIZATION_PRESET")
    assert rows[0]["value"] is None
    assert rows[0]["op"] == "delete"


def test_config_audit_global_list_respects_limit():
    for i in range(5):
        db.log_config_change("K", str(i), op="set", actor="test")
    rows = db.get_config_audit(limit=3)
    assert len(rows) == 3
