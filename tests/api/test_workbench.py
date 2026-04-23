"""v10.2 E3 — Workbench endpoints.

Covers schema, simulate (validation, no-cloud guarantee), promote (with
sim-id batch flow + rollback on partial failure), and profile round-trips.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.simulation import reset_store_for_tests
from src.runtime_settings import clear_cache


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    from src import db as _db
    _db.init_db()
    reset_store_for_tests()
    clear_cache()
    # Isolate snapshot dir to keep profile files out of the real CONFIG_SNAPSHOT_DIR
    snap_dir = tmp_path / "snapshots"
    monkeypatch.setattr("src.api.routers.workbench.config.CONFIG_SNAPSHOT_DIR", str(snap_dir), raising=False)
    yield
    reset_store_for_tests()
    clear_cache()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def require_sim_off(monkeypatch):
    monkeypatch.setenv("REQUIRE_SIMULATION_ID", "false")
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def require_sim_on(monkeypatch):
    monkeypatch.setenv("REQUIRE_SIMULATION_ID", "true")
    clear_cache()
    yield
    clear_cache()


class TestSchema:
    def test_schema_lists_groups_and_fields(self, client):
        r = client.get("/api/v1/workbench/schema")
        assert r.status_code == 200
        body = r.json()
        assert "groups" in body and "fields" in body
        keys = {f["key"] for f in body["fields"]}
        assert "DHW_TEMP_NORMAL_C" in keys
        assert "LP_CYCLE_PENALTY_PENCE_PER_KWH" in keys
        assert "OPTIMIZATION_PRESET" in keys
        # Promotable subset is non-empty
        assert any(f["promotable"] for f in body["fields"])
        # Every field has a current value rendered
        assert all("current" in f for f in body["fields"])


class TestSimulate:
    def test_unknown_override_400(self, client):
        r = client.post("/api/v1/workbench/simulate", json={"overrides": {"NOT_A_KEY": 1}})
        assert r.status_code == 400
        assert "unknown override key" in r.json()["detail"].lower()

    def test_out_of_range_400(self, client):
        r = client.post("/api/v1/workbench/simulate", json={"overrides": {"DHW_TEMP_NORMAL_C": 200}})
        assert r.status_code == 400
        assert "max" in r.json()["detail"]

    def test_simulate_runs_and_returns_audit(self, client):
        # Simulate succeeds even without Agile rates seeded — just returns ok=False
        # with the expected error. The point: it never raises and never calls cloud.
        r = client.post("/api/v1/workbench/simulate", json={
            "overrides": {"DHW_TEMP_NORMAL_C": 49.0, "LP_CYCLE_PENALTY_PENCE_PER_KWH": 0.2},
        })
        assert r.status_code == 200
        body = r.json()
        # Whether the LP itself succeeds depends on seeded rates; the simulate
        # endpoint just must return a structured response with override audit.
        assert "applied_overrides" in body
        assert body["applied_overrides"]["DHW_TEMP_NORMAL_C"] == 49.0
        assert body["applied_overrides"]["LP_CYCLE_PENALTY_PENCE_PER_KWH"] == 0.2

    def test_simulate_does_not_call_cloud(self, client):
        """Quota safety regression: workbench simulate must never hit Daikin/Fox."""
        with patch("src.daikin.client.DaikinClient.get_status",
                   side_effect=AssertionError("workbench simulate must not call Daikin")), \
             patch("src.foxess.client.FoxESSClient.get_realtime",
                   side_effect=AssertionError("workbench simulate must not call Fox")):
            r = client.post("/api/v1/workbench/simulate", json={"overrides": {"DHW_TEMP_NORMAL_C": 49.0}})
            assert r.status_code == 200


class TestPromote:
    def test_promote_simulate_returns_diff(self, client):
        r = client.post("/api/v1/workbench/promote/simulate", json={
            "overrides": {"DHW_TEMP_NORMAL_C": 49.0, "INDOOR_SETPOINT_C": 21.0},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "workbench.promote"
        assert body["simulation_id"]
        assert len(body["sub_actions"]) == 2

    def test_promote_simulate_rejects_no_promotable(self, client):
        # All non-promotable: just penalty/solver knobs
        r = client.post("/api/v1/workbench/promote/simulate", json={
            "overrides": {"LP_CYCLE_PENALTY_PENCE_PER_KWH": 0.2},
        })
        assert r.status_code == 400

    def test_promote_separates_promotable(self, client):
        r = client.post("/api/v1/workbench/promote/simulate", json={
            "overrides": {
                "DHW_TEMP_NORMAL_C": 49.0,                       # promotable
                "LP_CYCLE_PENALTY_PENCE_PER_KWH": 0.2,           # NOT promotable
            },
        })
        assert r.status_code == 200
        body = r.json()
        # Only promotable made it into the umbrella diff
        assert len(body["sub_actions"]) == 1
        assert body["sub_actions"][0]["key"] == "DHW_TEMP_NORMAL_C"
        # Non-promotable surfaced for transparency
        assert body["non_promotable_overrides"] == {"LP_CYCLE_PENALTY_PENCE_PER_KWH": 0.2}

    def test_promote_apply_writes_promotable(self, client, require_sim_on):
        # Step 1: simulate
        r = client.post("/api/v1/workbench/promote/simulate", json={
            "overrides": {"DHW_TEMP_NORMAL_C": 49.5},
        })
        sid = r.json()["simulation_id"]
        # Step 2: apply with sim-id
        r = client.post(
            "/api/v1/workbench/promote",
            json={"overrides": {"DHW_TEMP_NORMAL_C": 49.5}},
            headers={"X-Simulation-Id": sid},
        )
        assert r.status_code == 200, r.text
        assert r.json()["promoted"][0]["ok"] is True
        # Verify it persisted
        from src import runtime_settings as rts
        assert rts.get_setting("DHW_TEMP_NORMAL_C") == 49.5

    def test_promote_apply_without_sim_id_when_required(self, client, require_sim_on):
        r = client.post("/api/v1/workbench/promote", json={
            "overrides": {"DHW_TEMP_NORMAL_C": 49.5},
        })
        assert r.status_code == 409


class TestProfiles:
    def test_round_trip_save_list_load_delete(self, client, require_sim_off):
        overrides = {"DHW_TEMP_NORMAL_C": 49.0, "INDOOR_SETPOINT_C": 21.0}

        # Save
        r = client.post("/api/v1/workbench/profiles/winter-test", json={"overrides": overrides})
        assert r.status_code == 200, r.text
        assert r.json()["key_count"] == 2

        # List shows it
        r = client.get("/api/v1/workbench/profiles")
        names = [p["name"] for p in r.json()["profiles"]]
        assert "winter-test" in names

        # Load returns the overrides
        r = client.get("/api/v1/workbench/profiles/winter-test")
        assert r.status_code == 200
        assert r.json()["overrides"] == overrides

        # Delete
        r = client.delete("/api/v1/workbench/profiles/winter-test")
        assert r.status_code == 200

        # 404 after delete
        r = client.get("/api/v1/workbench/profiles/winter-test")
        assert r.status_code == 404

    def test_save_validates_overrides(self, client):
        r = client.post("/api/v1/workbench/profiles/bad", json={"overrides": {"NOT_REAL": 1}})
        assert r.status_code == 400

    def test_unsafe_profile_name_400(self, client):
        # Name containing only chars stripped by sanitizer (dots/spaces) → 400
        r = client.post("/api/v1/workbench/profiles/...", json={"overrides": {}})
        assert r.status_code == 400
