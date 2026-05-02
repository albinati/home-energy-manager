"""REST endpoint tests for /api/v1/appliances and /api/v1/integrations/smartthings.

The PAT-set endpoint validates with a SmartThings list_devices round-trip;
we mock the SmartThings client so no HTTP traffic.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src import db
from src.api.main import app
from src.smartthings.client import SmartThingsError


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# /api/v1/appliances CRUD
# ---------------------------------------------------------------------------

def test_list_empty(client):
    r = client.get("/api/v1/appliances")
    assert r.status_code == 200
    body = r.json()
    assert body["appliances"] == []
    assert body["count"] == 0


def test_register_then_list(client):
    payload = {
        "vendor_device_id": "uuid-1",
        "name": "Samsung Washer",
        "device_type": "washer",
        "default_duration_minutes": 120,
        "deadline_local_time": "07:00",
        "typical_kw": 0.6,
    }
    r = client.post("/api/v1/appliances", json=payload)
    assert r.status_code == 201, r.text
    appliance = r.json()
    assert appliance["vendor_device_id"] == "uuid-1"
    assert appliance["enabled"] is True
    appliance_id = appliance["id"]

    r = client.get("/api/v1/appliances")
    assert r.json()["count"] == 1
    assert r.json()["appliances"][0]["id"] == appliance_id


def test_register_duplicate_returns_409(client):
    payload = {"vendor_device_id": "uuid-dup", "name": "W"}
    assert client.post("/api/v1/appliances", json=payload).status_code == 201
    r = client.post("/api/v1/appliances", json=payload)
    assert r.status_code == 409


def test_patch_appliance(client):
    appliance = client.post(
        "/api/v1/appliances",
        json={"vendor_device_id": "uuid-2", "name": "W", "deadline_local_time": "08:00"},
    ).json()
    r = client.patch(
        f"/api/v1/appliances/{appliance['id']}",
        json={"deadline_local_time": "06:00", "enabled": False, "typical_kw": 0.4},
    )
    assert r.status_code == 200
    assert r.json()["deadline_local_time"] == "06:00"
    assert r.json()["enabled"] is False
    assert r.json()["typical_kw"] == 0.4


def test_delete_appliance(client):
    appliance = client.post(
        "/api/v1/appliances", json={"vendor_device_id": "uuid-3", "name": "W"}
    ).json()
    r = client.delete(f"/api/v1/appliances/{appliance['id']}")
    assert r.status_code == 204
    assert client.get("/api/v1/appliances").json()["count"] == 0


def test_delete_nonexistent_404(client):
    r = client.delete("/api/v1/appliances/9999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/v1/appliances/jobs
# ---------------------------------------------------------------------------

def test_list_jobs_empty(client):
    r = client.get("/api/v1/appliances/jobs")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_cancel_scheduled_job(client):
    appliance = client.post(
        "/api/v1/appliances", json={"vendor_device_id": "uuid-4", "name": "W"}
    ).json()
    # Insert a scheduled job directly through the DAL.
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    job_id = db.create_appliance_job(
        appliance_id=appliance["id"],
        status="scheduled",
        armed_at_utc=now.isoformat(),
        deadline_utc=(now + timedelta(hours=4)).isoformat(),
        duration_minutes=120,
        planned_start_utc=(now + timedelta(hours=2)).isoformat(),
        planned_end_utc=(now + timedelta(hours=4)).isoformat(),
        avg_price_pence=5.0,
    )
    r = client.post(f"/api/v1/appliances/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert r.json()["error_msg"] == "cancelled_via_api"


def test_cancel_nonexistent_job_404(client):
    r = client.post("/api/v1/appliances/jobs/9999/cancel")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/v1/integrations/smartthings
# ---------------------------------------------------------------------------

def test_status_no_pat(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.config.config.SMARTTHINGS_TOKEN_FILE", tmp_path / ".st-token-missing"
    )
    r = client.get("/api/v1/integrations/smartthings/status")
    assert r.status_code == 200
    body = r.json()
    assert body["pat_present"] is False
    assert body["reachable"] is None
    # Crucially: no PAT field at all.
    assert "pat" not in body


def test_set_credentials_validates_via_round_trip(client, monkeypatch, tmp_path):
    token_path = tmp_path / ".st-token-set"
    monkeypatch.setattr("src.config.config.SMARTTHINGS_TOKEN_FILE", token_path)

    # Mock SmartThings list_devices to succeed.
    def fake_list_devices(self):
        return [{"deviceId": "x", "label": "Washer"}]

    with patch("src.smartthings.client.SmartThingsClient.list_devices",
               new=fake_list_devices):
        r = client.post(
            "/api/v1/integrations/smartthings/credentials",
            json={"pat": "valid-pat-123"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["device_count"] == 1
    # Token persisted with correct contents and 0600 perms.
    assert token_path.read_text().strip() == "valid-pat-123"
    assert oct(token_path.stat().st_mode & 0o777) == "0o600"
    # Crucially: the response never echoes the PAT.
    assert "pat" not in body


def test_set_credentials_rejects_invalid_pat(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.config.config.SMARTTHINGS_TOKEN_FILE", tmp_path / ".st-token-rej"
    )

    def fake_list_devices_401(self):
        raise SmartThingsError("pat_invalid", "401", http_status=401)

    with patch("src.smartthings.client.SmartThingsClient.list_devices",
               new=fake_list_devices_401):
        r = client.post(
            "/api/v1/integrations/smartthings/credentials",
            json={"pat": "bad-pat"},
        )
    assert r.status_code == 401
    # Token file must NOT have been written.
    assert not (tmp_path / ".st-token-rej").exists()


def test_delete_credentials(client, monkeypatch, tmp_path):
    token_path = tmp_path / ".st-token-del"
    monkeypatch.setattr("src.config.config.SMARTTHINGS_TOKEN_FILE", token_path)
    token_path.write_text("some-pat\n")
    assert token_path.exists()
    r = client.delete("/api/v1/integrations/smartthings/credentials")
    assert r.status_code == 204
    assert not token_path.exists()
