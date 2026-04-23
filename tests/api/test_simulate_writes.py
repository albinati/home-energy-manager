"""PR-A: simulate-first action paradigm tests.

Covers:
- ActionDiff dataclass + SimulationStore behaviour (in-memory, TTL, one-shot consume)
- Every /simulate route returns a valid ActionDiff
- REQUIRE_SIMULATION_ID enforcement (off = legacy bypass, on = strict)
- **Quota safety**: simulate endpoints must NOT call DaikinClient or FoxESSClient
  cloud methods. Mock them to raise on any call; simulate must still succeed.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.simulation import ActionDiff, SimulationStore, get_store, reset_store_for_tests
from src.runtime_settings import clear_cache


@pytest.fixture(autouse=True)
def _reset_store():
    # Init DB so settings PUT path has its table; conftest already isolates path.
    from src import db as _db
    _db.init_db()
    reset_store_for_tests()
    yield
    reset_store_for_tests()


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


# ---------------------------------------------------------------------------
# SimulationStore unit tests
# ---------------------------------------------------------------------------

class TestSimulationStore:
    def test_register_returns_uuid_hex(self):
        s = SimulationStore(ttl_seconds=60)
        d = ActionDiff(action="x", before={}, after={})
        sid = s.register(d)
        assert len(sid) == 32
        assert d.simulation_id == sid
        assert d.expires_at_epoch > time.time()

    def test_consume_is_one_shot(self):
        s = SimulationStore(ttl_seconds=60)
        sid = s.register(ActionDiff(action="x", before={}, after={}))
        assert s.consume(sid) is not None
        assert s.consume(sid) is None

    def test_get_is_non_consuming(self):
        s = SimulationStore(ttl_seconds=60)
        sid = s.register(ActionDiff(action="x", before={}, after={}))
        assert s.get(sid) is not None
        assert s.get(sid) is not None
        assert s.consume(sid) is not None

    def test_consume_returns_none_for_unknown_id(self):
        s = SimulationStore()
        assert s.consume("not-a-real-id") is None

    def test_expired_entries_are_dropped(self):
        s = SimulationStore(ttl_seconds=0.05)
        sid = s.register(ActionDiff(action="x", before={}, after={}))
        time.sleep(0.1)
        assert s.consume(sid) is None
        assert s.get(sid) is None

    def test_singleton_get_store_is_stable(self):
        a = get_store()
        b = get_store()
        assert a is b


# ---------------------------------------------------------------------------
# Simulate route smoke tests — every route must return a valid ActionDiff
# ---------------------------------------------------------------------------

SIMULATE_CASES = [
    ("POST", "/api/v1/daikin/power/simulate", {"on": True}, "daikin.set_power"),
    ("POST", "/api/v1/daikin/temperature/simulate", {"temperature": 21.0}, "daikin.set_temperature"),
    ("POST", "/api/v1/daikin/lwt-offset/simulate", {"offset": 2.0}, "daikin.set_lwt_offset"),
    ("POST", "/api/v1/daikin/mode/simulate", {"mode": "heating"}, "daikin.set_operation_mode"),
    ("POST", "/api/v1/daikin/tank-temperature/simulate", {"temperature": 48.0}, "daikin.set_tank_temperature"),
    ("POST", "/api/v1/daikin/tank-power/simulate", {"on": True}, "daikin.set_tank_power"),
    ("POST", "/api/v1/foxess/mode/simulate", {"mode": "Self Use"}, "foxess.set_mode"),
    ("POST", "/api/v1/optimization/propose/simulate", None, "optimization.propose"),
    ("POST", "/api/v1/optimization/approve/simulate", {"plan_id": "p-1"}, "optimization.approve"),
    ("POST", "/api/v1/optimization/reject/simulate", {"plan_id": "p-1"}, "optimization.reject"),
    ("POST", "/api/v1/optimization/rollback/simulate", None, "optimization.rollback"),
    ("POST", "/api/v1/optimization/preset/simulate", {"preset": "normal"}, "optimization.set_preset"),
    ("POST", "/api/v1/optimization/backend/simulate", {"backend": "lp"}, "optimization.set_backend"),
    ("POST", "/api/v1/optimization/mode/simulate", {"mode": "simulation"}, "optimization.set_mode"),
    ("POST", "/api/v1/optimization/auto-approve/simulate", {"enabled": True}, "optimization.set_auto_approve"),
    ("PUT", "/api/v1/settings/DAIKIN_CONTROL_MODE/simulate", {"value": "active"}, "setting.DAIKIN_CONTROL_MODE"),
    ("POST", "/api/v1/scheduler/pause/simulate", None, "scheduler.pause"),
    ("POST", "/api/v1/scheduler/resume/simulate", None, "scheduler.resume"),
]


@pytest.mark.parametrize("method,url,body,expected_action", SIMULATE_CASES)
def test_simulate_returns_valid_diff(client, method, url, body, expected_action):
    r = client.request(method, url, json=body)
    assert r.status_code == 200, f"{url}: HTTP {r.status_code}: {r.text}"
    payload = r.json()
    assert payload["action"] == expected_action
    assert payload["simulation_id"]
    assert len(payload["simulation_id"]) == 32
    assert payload["human_summary"], f"{url}: empty human_summary"
    assert payload["expires_at_epoch"] > time.time()


def test_simulate_lookup_via_get(client):
    r = client.post("/api/v1/foxess/mode/simulate", json={"mode": "Back Up"})
    sid = r.json()["simulation_id"]
    r2 = client.get(f"/api/v1/simulate/{sid}")
    assert r2.status_code == 200
    assert r2.json()["action"] == "foxess.set_mode"


def test_simulate_lookup_404_for_unknown_id(client):
    r = client.get("/api/v1/simulate/notreal")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# QUOTA SAFETY — simulate endpoints MUST NOT call cloud APIs
# ---------------------------------------------------------------------------

def test_simulate_does_not_call_daikin_cloud(client):
    """If simulate touches DaikinClient, the test will explode here.

    All Daikin /simulate endpoints must compute their diff from cached state +
    SQLite only. Mocking DaikinClient + force_refresh_devices to raise on any
    call is the regression test.
    """
    with patch("src.daikin.service.force_refresh_devices",
               side_effect=AssertionError("simulate must not force-refresh")), \
         patch("src.daikin.client.DaikinClient.set_power",
               side_effect=AssertionError("simulate must not call set_power")), \
         patch("src.daikin.client.DaikinClient.set_temperature",
               side_effect=AssertionError("simulate must not call set_temperature")), \
         patch("src.daikin.client.DaikinClient.set_lwt_offset",
               side_effect=AssertionError("simulate must not call set_lwt_offset")), \
         patch("src.daikin.client.DaikinClient.set_operation_mode",
               side_effect=AssertionError("simulate must not call set_operation_mode")), \
         patch("src.daikin.client.DaikinClient.set_tank_temperature",
               side_effect=AssertionError("simulate must not call set_tank_temperature")), \
         patch("src.daikin.client.DaikinClient.set_tank_power",
               side_effect=AssertionError("simulate must not call set_tank_power")):
        for method, url, body, _ in SIMULATE_CASES:
            if "daikin" not in url:
                continue
            r = client.request(method, url, json=body)
            assert r.status_code == 200, f"{url} should succeed without cloud calls"


def test_simulate_does_not_call_foxess_cloud(client):
    """Same regression for FoxESSClient writers."""
    with patch("src.foxess.client.FoxESSClient.set_work_mode",
               side_effect=AssertionError("simulate must not call set_work_mode")), \
         patch("src.foxess.client.FoxESSClient.set_scheduler_v3",
               side_effect=AssertionError("simulate must not call set_scheduler_v3")):
        for method, url, body, _ in SIMULATE_CASES:
            if "foxess" not in url:
                continue
            r = client.request(method, url, json=body)
            assert r.status_code == 200, f"{url} should succeed without cloud calls"


# ---------------------------------------------------------------------------
# Idempotency enforcement — REQUIRE_SIMULATION_ID off = legacy, on = strict
# ---------------------------------------------------------------------------

def test_real_write_passes_through_when_enforcement_off(client, require_sim_off):
    """REQUIRE_SIMULATION_ID=false: real writers ignore the missing header.

    They may still 4xx for OTHER reasons (validation, auth, etc.) — but never
    SimulationIdRequired.
    """
    r = client.post("/api/v1/foxess/mode", json={"mode": "Self Use"})
    if r.status_code == 409:
        # Should never be SimulationIdRequired
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("error") != "SimulationIdRequired"


def test_real_write_blocked_without_header_when_on(client, require_sim_on):
    r = client.post("/api/v1/foxess/mode", json={"mode": "Self Use"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "SimulationIdRequired"


def test_real_write_410_with_unknown_header(client, require_sim_on):
    r = client.post(
        "/api/v1/foxess/mode",
        json={"mode": "Self Use"},
        headers={"X-Simulation-Id": "deadbeef" * 4},
    )
    assert r.status_code == 410
    assert r.json()["detail"]["error"] == "SimulationExpired"


def test_real_write_409_on_action_mismatch(client, require_sim_on):
    """Sim_id from one action must not validate another."""
    r = client.post("/api/v1/daikin/power/simulate", json={"on": True})
    sid = r.json()["simulation_id"]
    r = client.post(
        "/api/v1/foxess/mode",
        json={"mode": "Self Use"},
        headers={"X-Simulation-Id": sid},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "SimulationIdMismatch"


def test_real_write_consumes_sim_id_one_shot(client, require_sim_on):
    """Even with a valid header, second use returns 410."""
    r = client.post("/api/v1/foxess/mode/simulate", json={"mode": "Self Use"})
    sid = r.json()["simulation_id"]
    # First use: should pass our enforcement check (may still 4xx for other
    # reasons like unconfigured Fox, but NOT SimulationIdRequired)
    r1 = client.post("/api/v1/foxess/mode", json={"mode": "Self Use"},
                     headers={"X-Simulation-Id": sid})
    if r1.status_code == 409 and isinstance(r1.json().get("detail"), dict):
        assert r1.json()["detail"].get("error") != "SimulationIdRequired"
    # Second use: must be 410 regardless of what happened the first time
    r2 = client.post("/api/v1/foxess/mode", json={"mode": "Self Use"},
                     headers={"X-Simulation-Id": sid})
    assert r2.status_code == 410


def test_settings_simulate_then_apply_with_header(client, require_sim_on):
    """Settings PUT requires sim_id when enforcement is on."""
    # Without header: 409
    r = client.put("/api/v1/settings/DAIKIN_CONTROL_MODE", json={"value": "active"})
    assert r.status_code == 409
    # Simulate first, then apply
    r = client.put(
        "/api/v1/settings/DAIKIN_CONTROL_MODE/simulate",
        json={"value": "active"},
    )
    sid = r.json()["simulation_id"]
    r = client.put(
        "/api/v1/settings/DAIKIN_CONTROL_MODE",
        json={"value": "active"},
        headers={"X-Simulation-Id": sid},
    )
    # Real write may succeed (200) or fail for other reasons (e.g. DB),
    # but must NOT be 409 SimulationIdRequired or 410 SimulationExpired.
    if r.status_code in (409, 410):
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("error") not in ("SimulationIdRequired", "SimulationExpired")
