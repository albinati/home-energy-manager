"""Phase 3 — GET /api/v1/optimization/inputs contract.

The Forecast tab renders everything here. Pins the shape and the cache-only
contract (response must be fast and never crash when sources are cold).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src import db
from src.api.main import app
from src.config import config


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


def test_export_prices_populated_from_agile_export_rates(client, monkeypatch):
    """Regression: dashboard must read Outgoing prices from agile_export_rates,
    not from agile_rates (which only stores import). Bug shipped with PR #154 —
    LP itself read the right table; this view endpoint did not."""
    monkeypatch.setattr(config, "OCTOPUS_EXPORT_TARIFF_CODE",
                        "E-1R-AGILE-OUTGOING-19-05-13-H")

    # Seed two export-price rows aligned with the next two half-hour slots
    # the endpoint will build (it computes day_start = now.replace(minute=0)).
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = [
        {
            "valid_from": now.isoformat().replace("+00:00", "Z"),
            "valid_to": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": 19.55,
        },
        {
            "valid_from": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "valid_to": (now + timedelta(minutes=60)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": 19.63,
        },
    ]
    db.save_agile_export_rates(rows, "E-1R-AGILE-OUTGOING-19-05-13-H")

    r = client.get("/api/v1/optimization/inputs")
    assert r.status_code == 200
    slots = r.json()["slots"]
    by_t = {s["t_utc"]: s for s in slots}

    k1 = now.isoformat().replace("+00:00", "Z")
    k2 = (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    assert by_t[k1]["price_export_p"] == pytest.approx(19.55)
    assert by_t[k2]["price_export_p"] == pytest.approx(19.63)
