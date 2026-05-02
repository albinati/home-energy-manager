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

def test_status_no_tokens(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.config.config.SMARTTHINGS_TOKEN_FILE", tmp_path / ".st-tokens-missing"
    )
    r = client.get("/api/v1/integrations/smartthings/status")
    assert r.status_code == 200
    body = r.json()
    assert body["tokens_present"] is False
    assert body["reachable"] is None
    # No secrets in the response (access_token, refresh_token, client_secret).
    for forbidden in ("access_token", "refresh_token", "pat", "client_secret"):
        assert forbidden not in body


def test_oauth_start_returns_authorize_url(client, monkeypatch):
    monkeypatch.setattr("src.config.config.SMARTTHINGS_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        "src.config.config.SMARTTHINGS_REDIRECT_URI",
        "http://localhost:8080/oauth/smartthings/callback",
    )
    monkeypatch.setattr(
        "src.config.config.SMARTTHINGS_OAUTH_SCOPES", "r:devices:* x:devices:*"
    )
    r = client.get("/api/v1/integrations/smartthings/oauth/start")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "authorize_url" in body
    assert "client_id=test-client-id" in body["authorize_url"]
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080" in body["authorize_url"]
    assert "state=" in body["authorize_url"]
    # state is also returned for clients that want to round-trip it.
    assert body["state"]
    # Critical: do not leak the client_secret.
    assert "client_secret" not in body
    assert "client_secret" not in body["authorize_url"]


def test_oauth_start_412_when_client_id_missing(client, monkeypatch):
    monkeypatch.setattr("src.config.config.SMARTTHINGS_CLIENT_ID", "")
    r = client.get("/api/v1/integrations/smartthings/oauth/start")
    assert r.status_code == 412
    assert "SMARTTHINGS_CLIENT_ID" in r.text


def test_delete_credentials_removes_token_file(client, monkeypatch, tmp_path):
    token_path = tmp_path / ".st-tokens-del.json"
    monkeypatch.setattr("src.config.config.SMARTTHINGS_TOKEN_FILE", token_path)
    token_path.write_text('{"access_token": "x", "refresh_token": "y"}')
    assert token_path.exists()
    r = client.delete("/api/v1/integrations/smartthings/credentials")
    assert r.status_code == 204
    assert not token_path.exists()
