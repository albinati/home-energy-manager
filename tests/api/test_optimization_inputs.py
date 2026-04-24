"""Phase 3 — GET /api/v1/optimization/inputs contract.

The Forecast tab renders everything here. Pins the shape and the cache-only
contract (response must be fast and never crash when sources are cold).
"""
from __future__ import annotations

import time

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


def test_inputs_shape(client):
    r = client.get("/api/v1/optimization/inputs")
    assert r.status_code == 200
    body = r.json()
    expected = {
        "now_utc", "planner_tz", "horizon_hours", "slots", "initial",
        "micro_climate_offset_c", "thresholds", "config_snapshot",
        "target_vwap_pence", "estimated_cost_pence", "strategy_summary",
        "tomorrow_rates_available", "workbench_schema",
    }
    assert set(body.keys()) == expected


def test_inputs_slots_have_expected_columns(client):
    r = client.get("/api/v1/optimization/inputs")
    slots = r.json()["slots"]
    if slots:
        assert set(slots[0].keys()) == {
            "t_utc", "price_import_p", "price_export_p",
            "temp_c", "solar_w_m2", "base_load_kwh",
        }


def test_inputs_initial_has_source_fields(client):
    r = client.get("/api/v1/optimization/inputs")
    init = r.json()["initial"]
    for key in ("soc_kwh", "soc_pct", "tank_c", "indoor_c",
                "soc_source", "tank_source", "indoor_source"):
        assert key in init


def test_inputs_honours_horizon_hours_clamp(client):
    # Out-of-range horizon gets clamped into [4, 48]
    r = client.get("/api/v1/optimization/inputs?horizon_hours=200")
    assert r.status_code == 200
    assert r.json()["horizon_hours"] == 48

    r = client.get("/api/v1/optimization/inputs?horizon_hours=1")
    assert r.status_code == 200
    assert r.json()["horizon_hours"] == 4


def test_inputs_never_hits_network_on_cold_state(client):
    # Cache-only contract — should return fast even with empty DB.
    t0 = time.monotonic()
    r = client.get("/api/v1/optimization/inputs")
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert elapsed < 2.0, f"endpoint took {elapsed:.2f}s — possible cloud call"


def test_forecast_page_renders(client):
    r = client.get("/forecast")
    assert r.status_code == 200
    assert b"LP inputs" in r.content
    assert b"forecast.js" in r.content
