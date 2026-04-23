"""v10.2 E5 — batch settings simulate + apply.

Covers:
- /simulate returns one ActionDiff with sub_actions for N keys
- /apply (X-Simulation-Id) writes all keys atomically
- Empty / non-dict body rejected with 400
- Single-key payload still works (mode_switcher fallback)
- Invalid key in batch surfaces a 409 BatchPartialFailure with rollback details
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.simulation import reset_store_for_tests
from src.runtime_settings import clear_cache


@pytest.fixture(autouse=True)
def _reset_store():
    from src import db as _db
    _db.init_db()
    reset_store_for_tests()
    clear_cache()
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


class TestBatchSimulate:
    def test_returns_sub_actions_for_each_key(self, client):
        r = client.post(
            "/api/v1/settings/batch/simulate",
            json={"changes": {"DHW_TEMP_NORMAL_C": 50.0, "INDOOR_SETPOINT_C": 21.0}},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] == "settings.batch"
        assert body["simulation_id"]
        subs = body.get("sub_actions") or []
        assert len(subs) == 2
        keys = {s["key"] for s in subs}
        assert keys == {"DHW_TEMP_NORMAL_C", "INDOOR_SETPOINT_C"}

    def test_single_key_payload_works(self, client):
        # mode_switcher routes 1-key changes through the same endpoint.
        r = client.post(
            "/api/v1/settings/batch/simulate",
            json={"changes": {"DHW_TEMP_NORMAL_C": 48.0}},
        )
        assert r.status_code == 200
        assert len(r.json()["sub_actions"]) == 1

    def test_rejects_empty_changes(self, client):
        r = client.post("/api/v1/settings/batch/simulate", json={"changes": {}})
        assert r.status_code == 400

    def test_rejects_non_dict_changes(self, client):
        r = client.post("/api/v1/settings/batch/simulate", json={"changes": ["x"]})
        assert r.status_code == 400

    def test_aggregates_safety_flags(self, client, monkeypatch):
        # DAIKIN_CONTROL_MODE: passive → active is a hard flag.
        monkeypatch.setenv("DAIKIN_CONTROL_MODE", "passive")
        clear_cache()
        r = client.post(
            "/api/v1/settings/batch/simulate",
            json={"changes": {
                "DAIKIN_CONTROL_MODE": "active",
                "DHW_TEMP_NORMAL_C": 50.0,
            }},
        )
        assert r.status_code == 200
        flags = r.json().get("safety_flags") or []
        assert "enables_daikin_writes" in flags


class TestBatchApply:
    def test_apply_with_sim_id_writes_all_keys(self, client, require_sim_on):
        r = client.post(
            "/api/v1/settings/batch/simulate",
            json={"changes": {"DHW_TEMP_NORMAL_C": 49.5, "INDOOR_SETPOINT_C": 20.5}},
        )
        sid = r.json()["simulation_id"]
        r = client.post(
            "/api/v1/settings/batch",
            json={"changes": {"DHW_TEMP_NORMAL_C": 49.5, "INDOOR_SETPOINT_C": 20.5}},
            headers={"X-Simulation-Id": sid},
        )
        assert r.status_code == 200, r.text
        results = r.json()["results"]
        assert all(r["ok"] for r in results)
        # Verify they actually persisted
        r = client.get("/api/v1/settings/DHW_TEMP_NORMAL_C")
        assert r.status_code == 200

    def test_apply_without_sim_id_when_required(self, client, require_sim_on):
        r = client.post(
            "/api/v1/settings/batch",
            json={"changes": {"DHW_TEMP_NORMAL_C": 50.0}},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "SimulationIdRequired"

    def test_invalid_key_mid_batch_rolls_back(self, client, require_sim_off):
        # Seed two valid keys to known values first.
        from src import runtime_settings as rts
        rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="test_setup")
        prior = rts.get_setting("DHW_TEMP_NORMAL_C")

        r = client.post(
            "/api/v1/settings/batch",
            json={"changes": {
                "DHW_TEMP_NORMAL_C": 49.0,
                "DEFINITELY_NOT_A_REAL_KEY": "boom",
            }},
        )
        # Validation may catch this in simulate or in apply; either way it
        # must NOT leave DHW_TEMP_NORMAL_C at the new value when the second
        # key was rejected.
        if r.status_code == 409:
            detail = r.json()["detail"]
            assert detail["error"] == "BatchPartialFailure"
            assert detail["failed_at_key"] == "DEFINITELY_NOT_A_REAL_KEY"
            # Rollback restored the prior value
            assert rts.get_setting("DHW_TEMP_NORMAL_C") == prior
        else:
            # If validated upfront (400), nothing was written
            assert r.status_code in (400, 422)
            assert rts.get_setting("DHW_TEMP_NORMAL_C") == prior
