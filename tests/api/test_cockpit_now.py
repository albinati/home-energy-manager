"""Phase 2 — GET /api/v1/cockpit/now aggregator contract.

The hero panel reads from a single coherent snapshot instead of four
parallel fetches. This test pins the payload shape so frontend refactors
don't silently break it. Functional correctness (price matching, current
fox-group detection) is exercised in other layers — here we just confirm
the contract and that the endpoint NEVER raises even when every upstream
source is cold.
"""
from __future__ import annotations

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


def test_cockpit_now_shape(client):
    r = client.get("/api/v1/cockpit/now")
    assert r.status_code == 200
    body = r.json()

    # Top-level keys.
    assert set(body.keys()) == {
        "now_utc", "planner_tz", "current_slot", "next_transition",
        "state", "freshness", "thresholds", "modes", "plan_date",
    }

    # current_slot always has the 5-key shape.
    assert set(body["current_slot"].keys()) == {
        "t_utc", "t_end_utc", "price_import_p", "price_export_p", "fox_mode",
    }

    # state has the 12 fields the hero panel reads.
    assert set(body["state"].keys()) == {
        "soc_pct", "soc_kwh", "solar_kw", "load_kw", "grid_kw", "battery_kw",
        "fox_mode", "tank_c", "indoor_c", "outdoor_c", "lwt_c", "daikin_mode",
    }

    # freshness — one block per source.
    assert set(body["freshness"].keys()) == {"agile", "fox", "daikin", "plan"}
    for block in body["freshness"].values():
        assert set(block.keys()) == {"fetched_at_utc", "age_s", "stale"}

    # thresholds.
    assert set(body["thresholds"].keys()) == {"cheap_p", "peak_p"}

    # modes.
    assert set(body["modes"].keys()) == {
        "daikin_control_mode", "optimization_preset", "energy_strategy_mode",
    }


def test_cockpit_now_returns_iso_utc_and_planner_tz(client):
    r = client.get("/api/v1/cockpit/now")
    body = r.json()
    assert body["now_utc"].endswith("Z")
    assert isinstance(body["planner_tz"], str) and len(body["planner_tz"]) > 0


def test_cockpit_now_survives_cold_state(client):
    # With no cached Fox/Daikin/Octopus data, the endpoint should still 200 and
    # return None in all state slots rather than raising.
    r = client.get("/api/v1/cockpit/now")
    assert r.status_code == 200
    body = r.json()
    for key in ("soc_pct", "soc_kwh", "solar_kw", "load_kw", "tank_c", "indoor_c"):
        # Either populated (if the test env has live caches) or None.
        assert body["state"][key] is None or isinstance(body["state"][key], (int, float))


def test_cockpit_now_never_triggers_cloud_calls(client):
    # Contract: this endpoint MUST be cache-only. If it ever goes async-heavy
    # on cloud calls the quota pills would burn silently. A smoke check on
    # response time guards the worst regressions.
    import time
    t0 = time.monotonic()
    r = client.get("/api/v1/cockpit/now")
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    # Cold call should be well under a second — no network waits allowed.
    assert elapsed < 1.5, f"endpoint took {elapsed:.2f}s — possible cloud call leak"
