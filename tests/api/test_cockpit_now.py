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
        "state", "freshness", "thresholds", "modes", "plan_date", "_legend",
    }
    # _legend disambiguates sign conventions for LLM consumers (OpenClaw etc.).
    # The signed fields MUST carry a description so a negative value cannot be
    # misread as an unsigned magnitude — see the 2026-05-03 OpenClaw "Daikin
    # off" misread audit for context.
    legend = body["_legend"]
    assert "IMPORTING" in legend["grid_kw"] and "EXPORTING" in legend["grid_kw"]
    assert "CHARGING" in legend["battery_kw"] and "DISCHARGING" in legend["battery_kw"]

    # current_slot always has the 5-key shape.
    assert set(body["current_slot"].keys()) == {
        "t_utc", "t_end_utc", "price_import_p", "price_export_p", "fox_mode",
    }

    # state has the fields the hero panel reads (indoor = rich sensor snapshot).
    assert set(body["state"].keys()) == {
        "soc_pct", "soc_kwh", "solar_kw", "load_kw", "grid_kw", "battery_kw",
        "fox_mode", "tank_c", "indoor_c", "outdoor_c", "lwt_c", "daikin_mode",
        "indoor",
    }

    # freshness — one block per source (indoor sensors #540 W1).
    assert set(body["freshness"].keys()) == {"agile", "fox", "daikin", "plan", "indoor"}
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


def test_cockpit_now_indoor_from_sensor(monkeypatch, tmp_path):
    """#540 W1 — the freshest room-sensor reading is folded into the consolidated
    snapshot (same path as Fox/tank) and drives state.indoor_c. Daikin never
    sourced indoor (no room stat)."""
    from datetime import UTC, datetime

    from src.config import config as _cfg

    monkeypatch.setattr(_cfg, "DB_PATH", str(tmp_path / "cn.db"), raising=False)
    db.init_db()
    z = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.save_device_reading_log([
        {"captured_at": z, "temp_c": 23.5, "humidity_pct": 61, "mac": "AA", "room": "sala"},
    ])

    body = TestClient(app).get("/api/v1/cockpit/now").json()
    ind = body["state"]["indoor"]
    assert ind is not None and ind["n_rooms"] == 1
    assert ind["mean_c"] == 23.5
    assert ind["rooms"][0]["room"] == "sala"
    assert body["state"]["indoor_c"] == 23.5          # sensor drives indoor_c
    assert body["freshness"]["indoor"]["stale"] is False
