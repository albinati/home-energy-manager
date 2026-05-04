"""Phase 4 — GET /api/v1/cockpit/at?when=... replay from LP snapshots.

The History page reads this to rehydrate past moments. The endpoint joins
optimizer_log (via find_run_for_time) with lp_solution_snapshot,
lp_inputs_snapshot, agile_rates, and execution_log.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src import db
from src.api.main import app


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


@pytest.fixture
def client():
    return TestClient(app)


def _seed_run_with_slot(run_at: datetime, slot_time: datetime) -> int:
    """Create an optimizer_log row + inputs + one solution slot at slot_time."""
    run_id = db.log_optimizer_run({
        "run_at": run_at.isoformat(),
        "rates_count": 48, "cheap_slots": 5, "peak_slots": 3,
        "standard_slots": 36, "negative_slots": 0,
        "target_vwap": 18.0, "actual_agile_mean": 20.0, "battery_warning": False,
        "strategy_summary": "seed", "fox_schedule_uploaded": True, "daikin_actions_count": 0,
    })
    db.save_lp_snapshots(
        run_id,
        {
            "run_at_utc": run_at.isoformat(), "plan_date": slot_time.date().isoformat(),
            "horizon_hours": 24,
            "soc_initial_kwh": 5.0, "tank_initial_c": 46.0, "indoor_initial_c": 20.5,
            "soc_source": "fox_realtime_cache", "tank_source": "daikin_cache",
            "indoor_source": "daikin_cache",
            "base_load_json": "[]", "micro_climate_offset_c": 0.0,
            "exogenous_snapshot_json": json.dumps({
                "base_load_components": {
                    "residual_profile_kwh": [0.2, 0.3],
                    "appliance_profile_kwh": [0.0, 0.1],
                    "flat_fallback_kwh": 0.25,
                    "fox_mean_kwh_per_slot": 0.22,
                    "profile_bucket_count": 48,
                },
                "weather_adjustment": {
                    "forecast_fetch_at_utc": "2026-04-24T05:00:00+00:00",
                    "today_factor": 0.95,
                    "flat_scale": 1.0,
                    "cloud_table_cells": 12,
                    "hourly_table_cells": 24,
                },
                "tariffs": {
                    "export_price_pence": [5.0, 7.5],
                    "uses_flat_export_rate": False,
                },
            }),
            "config_snapshot_json": "{}",
            "price_quantize_p": 0.0, "peak_threshold_p": 25.0, "cheap_threshold_p": 12.0,
            "daikin_control_mode": "passive", "optimization_preset": "normal",
            "energy_strategy_mode": "savings_first",
        },
        [
            {
                "slot_index": 0,
                "slot_time_utc": slot_time.isoformat().replace("+00:00", "+00:00"),
                "price_p": 15.0, "import_kwh": 0.5, "export_kwh": 0.0,
                "charge_kwh": 0.2, "discharge_kwh": 0.0, "pv_use_kwh": 0.0,
                "pv_curtail_kwh": 0.0, "dhw_kwh": 0.1, "space_kwh": 0.0,
                "soc_kwh": 5.2, "tank_temp_c": 46.5, "indoor_temp_c": 20.6,
                "outdoor_temp_c": 12.0, "lwt_offset_c": 0.0,
            },
        ],
    )
    return run_id


def test_at_rejects_bad_iso(client):
    r = client.get("/api/v1/cockpit/at?when=not-a-date")
    assert r.status_code == 400


def test_at_returns_none_source_when_no_snapshots(client):
    r = client.get("/api/v1/cockpit/at?when=2026-04-24T12:00:00Z")
    assert r.status_code == 200
    body = r.json()
    # Shape check — source block present but run_id/lp_run_at null.
    assert "source" in body
    assert body["source"]["run_id"] is None
    assert body["source"]["lp_run_at_utc"] is None
    assert "state" in body
    assert "current_slot" in body


def test_at_rehydrates_from_snapshot(client):
    run_at = datetime(2026, 4, 24, 6, 0, tzinfo=UTC)
    slot_time = datetime(2026, 4, 24, 6, 30, tzinfo=UTC)
    run_id = _seed_run_with_slot(run_at, slot_time)

    r = client.get("/api/v1/cockpit/at?when=2026-04-24T06:30:00Z")
    assert r.status_code == 200
    body = r.json()

    assert body["source"]["run_id"] == run_id
    assert body["source"]["lp_run_at_utc"] is not None
    # lp_inputs block carries the provenance strings we seeded.
    li = body.get("lp_inputs") or {}
    assert li.get("soc_source") == "fox_realtime_cache"
    assert li.get("tank_source") == "daikin_cache"
    lx = body.get("lp_exogenous") or {}
    assert lx["base_load_components"]["profile_bucket_count"] == 48
    assert lx["weather_adjustment"]["today_factor"] == pytest.approx(0.95)
    why = body.get("lp_why") or []
    assert why
    assert any("forecast fetch" in line for line in why)


def test_history_page_renders(client):
    r = client.get("/history")
    assert r.status_code == 200
    assert b"Replay a past moment" in r.content
    assert b"history.js" in r.content


def test_at_picks_most_recent_run_when_multiple(client):
    run_early = datetime(2026, 4, 24, 6, 0, tzinfo=UTC)
    run_late = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    slot = datetime(2026, 4, 24, 14, 0, tzinfo=UTC)
    _seed_run_with_slot(run_early, slot)
    rid_late = _seed_run_with_slot(run_late, slot)
    # At 15:00 both runs are historic; endpoint should pick the later one.
    r = client.get("/api/v1/cockpit/at?when=2026-04-24T15:00:00Z")
    assert r.status_code == 200
    assert r.json()["source"]["run_id"] == rid_late
