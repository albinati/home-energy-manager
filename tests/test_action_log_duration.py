"""PR B — action_log duration tracking + recent-triggers endpoint.

The user asked for "manual triggers in the cockpit — when fired, how long
it took". This file covers the plumbing:

* log_action_timed() captures started_at + completed_at + duration_ms for
  both success and failure paths.
* get_recent_triggers() filters out heartbeat + notification noise by
  default so the cockpit sees meaningful events.
* /api/v1/recent-triggers wraps it for the browser.
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
    # Start each test with an empty action_log so counts + filters are deterministic.
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM action_log")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# log_action_timed
# ---------------------------------------------------------------------------

def test_log_action_timed_captures_success_duration():
    with db.log_action_timed(
        device="test", action="sample", params={"n": 1},
        trigger="mcp", actor="test",
    ):
        time.sleep(0.03)

    rows = db.get_recent_triggers(limit=5, exclude_triggers=[])
    assert len(rows) == 1
    r = rows[0]
    assert r["device"] == "test"
    assert r["action"] == "sample"
    assert r["result"] == "success"
    assert r["actor"] == "test"
    assert r["started_at"] is not None
    assert r["completed_at"] is not None
    assert r["duration_ms"] is not None
    # 30ms ± 50ms slack for slow CI.
    assert 20 <= r["duration_ms"] <= 200, f"duration_ms={r['duration_ms']} outside expected band"


def test_log_action_timed_writes_row_on_failure_and_reraises():
    with pytest.raises(ValueError, match="boom"):
        with db.log_action_timed(
            device="test", action="fails", params={}, trigger="mcp", actor="test",
        ):
            raise ValueError("boom")

    rows = db.get_recent_triggers(limit=5, exclude_triggers=[])
    assert len(rows) == 1
    assert rows[0]["result"] == "failure"
    assert "boom" in (rows[0]["error_msg"] or "")
    assert rows[0]["duration_ms"] is not None


def test_log_action_legacy_path_leaves_new_columns_null():
    db.log_action(
        device="system", action="heartbeat_tick", params={},
        result="success", trigger="heartbeat",
    )
    rows = db.get_recent_triggers(limit=5, exclude_triggers=[])
    # Heartbeat filtered out by default — pass exclude_triggers=[] above.
    assert len(rows) == 1
    assert rows[0]["started_at"] is None
    assert rows[0]["completed_at"] is None
    assert rows[0]["duration_ms"] is None
    assert rows[0]["actor"] is None


# ---------------------------------------------------------------------------
# get_recent_triggers filtering
# ---------------------------------------------------------------------------

def test_get_recent_triggers_hides_heartbeat_and_notification_by_default():
    db.log_action(device="system", action="heartbeat_tick", params={}, result="success", trigger="heartbeat")
    db.log_action(device="system", action="plan_proposed", params={}, result="success", trigger="notification")
    db.log_action(device="foxess", action="set_work_mode", params={"mode": "Self Use"},
                  result="success", trigger="mcp", actor="mcp")

    rows = db.get_recent_triggers(limit=10)
    # Only the foxess row survives the default filter.
    assert len(rows) == 1
    assert rows[0]["device"] == "foxess"


def test_get_recent_triggers_custom_exclude():
    db.log_action(device="foxess", action="set_work_mode", params={}, result="success", trigger="mcp")
    db.log_action(device="system", action="mpc_tick", params={}, result="success", trigger="scheduler")

    only_mcp = db.get_recent_triggers(limit=10, exclude_triggers=["heartbeat", "notification", "scheduler"])
    assert len(only_mcp) == 1
    assert only_mcp[0]["device"] == "foxess"


# ---------------------------------------------------------------------------
# /api/v1/recent-triggers endpoint
# ---------------------------------------------------------------------------

def test_recent_triggers_endpoint_returns_ok_shape(client):
    with db.log_action_timed(
        device="daikin", action="set_tank_temperature",
        params={"temperature": 48}, trigger="mcp", actor="mcp",
    ):
        pass

    r = client.get("/api/v1/recent-triggers?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["rows"][0]["action"] == "set_tank_temperature"
    assert body["rows"][0]["actor"] == "mcp"
    assert body["rows"][0]["duration_ms"] is not None


def test_recent_triggers_endpoint_include_heartbeat_param(client):
    db.log_action(device="system", action="heartbeat_tick", params={},
                  result="success", trigger="heartbeat")
    # Default: excluded.
    default_body = client.get("/api/v1/recent-triggers").json()
    assert all(r["trigger"] != "heartbeat" for r in default_body["rows"])
    # Opt-in: included.
    included = client.get("/api/v1/recent-triggers?include_heartbeat=true").json()
    assert any(r["trigger"] == "heartbeat" for r in included["rows"])
