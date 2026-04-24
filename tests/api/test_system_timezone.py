"""Phase 1 — GET /api/v1/system/timezone contract + defaults.

The cockpit JS fetches this once per page load to format slot times in the
planner's timezone rather than the browser's local tz. The contract is:

* ``planner_tz`` is the config.BULLETPROOF_TIMEZONE (default Europe/London).
* ``plan_push_tz`` is fixed UTC (documented in CLAUDE.md — not configurable).
* ``now_utc`` / ``now_local`` are ISO strings, ``now_utc`` ends in ``Z``.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

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


def test_timezone_endpoint_default_shape(client):
    r = client.get("/api/v1/system/timezone")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"planner_tz", "plan_push_tz", "now_utc", "now_local"}
    assert body["plan_push_tz"] == "UTC"
    # planner_tz defaults to Europe/London per config; unit tests honour that.
    assert body["planner_tz"] == "Europe/London"


def test_timezone_endpoint_returns_z_suffixed_utc(client):
    r = client.get("/api/v1/system/timezone")
    assert r.status_code == 200
    assert r.json()["now_utc"].endswith("Z")
    # now_local should be a parseable ISO string
    local = r.json()["now_local"]
    datetime.fromisoformat(local)  # raises on bad format


def test_timezone_endpoint_respects_config_override(client, monkeypatch):
    # Force a different tz on the live config singleton.
    import src.config as config_mod
    monkeypatch.setattr(config_mod.config, "BULLETPROOF_TIMEZONE", "America/New_York")
    r = client.get("/api/v1/system/timezone")
    assert r.status_code == 200
    body = r.json()
    assert body["planner_tz"] == "America/New_York"
    # plan_push_tz is not configurable — still UTC.
    assert body["plan_push_tz"] == "UTC"


def test_timezone_endpoint_falls_back_to_utc_on_bad_config(client, monkeypatch):
    import src.config as config_mod
    monkeypatch.setattr(config_mod.config, "BULLETPROOF_TIMEZONE", "Not/A/Real/Zone")
    r = client.get("/api/v1/system/timezone")
    assert r.status_code == 200
    assert r.json()["planner_tz"] == "UTC"


def test_now_utc_and_now_local_are_the_same_moment(client):
    r = client.get("/api/v1/system/timezone")
    body = r.json()
    utc = datetime.fromisoformat(body["now_utc"].replace("Z", "+00:00"))
    local = datetime.fromisoformat(body["now_local"])
    # They represent the same instant within a small solve window.
    local_as_utc = local.astimezone(ZoneInfo("UTC"))
    delta = abs((local_as_utc - utc).total_seconds())
    assert delta < 2.0
