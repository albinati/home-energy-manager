"""Tests for ApiV1RoleAuth — the viewer-open / admin-gated /api/v1 guard.

The model (replacing the old all-or-nothing bearer guard):

* gate flag off → middleware is a no-op (dev: everything open)
* gate flag on:
    - safe reads (GET) on non-admin paths → open to VIEWERS (no token)
    - writes (POST/PUT/PATCH/DELETE) → require an ADMIN token
    - Settings + Journal (action-log) reads → require an ADMIN token
    - HEM_UI_TOKEN is NOT admin (it's baked into the UI's config.js)
    - HEM_ADMIN_TOKEN + HEM_OPENCLAW_TOKEN ARE admin
* /api/v1/health and /api/v1/whoami stay public
* /mcp keeps its own HEM_OPENCLAW_TOKEN guard
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """Fresh DB per test; lifespan token bootstrap writes the new files here."""
    db_path = str(tmp_path / "auth.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from src import config as _config
    monkeypatch.setattr(_config.config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(
        _config.config, "HEM_UI_TOKEN_FILE",
        str(tmp_path / ".hem-ui-token"), raising=False,
    )
    monkeypatch.setattr(
        _config.config, "HEM_OPENCLAW_TOKEN_FILE",
        str(tmp_path / ".openclaw-token"), raising=False,
    )
    from src import db as _db
    _db.init_db()
    yield


def _set_tokens(monkeypatch, *, ui="ui-tok", admin="admin-tok", openclaw="oc-tok",
                ingest="ingest-tok", required=True) -> None:
    from src import config as _config
    monkeypatch.setattr(_config.config, "HEM_UI_TOKEN", ui, raising=False)
    monkeypatch.setattr(_config.config, "HEM_ADMIN_TOKEN", admin, raising=False)
    monkeypatch.setattr(_config.config, "HEM_OPENCLAW_TOKEN", openclaw, raising=False)
    monkeypatch.setattr(_config.config, "HEM_SENSOR_INGEST_TOKEN", ingest, raising=False)
    monkeypatch.setattr(_config.config, "HEM_UI_AUTH_REQUIRED", required, raising=False)


def _client() -> TestClient:
    from src.api.main import app
    return TestClient(app)


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ── Gate OFF — everything open (dev) ────────────────────────────────────────

def test_all_open_when_gate_disabled(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=False)
    c = _client()
    assert c.get("/api/v1/health").status_code == 200
    # Even a write passes with no token when the gate is off.
    assert c.post("/api/v1/optimization/propose", json={}).status_code != 401


# ── Viewer reads are OPEN when the gate is on ───────────────────────────────

def test_safe_read_open_to_viewer_without_token(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    resp = _client().get("/api/v1/scheduler/timeline")
    # The whole point: a viewer with NO token can read. Endpoint may 200 or
    # 5xx on its own, but the auth layer must not 401.
    assert resp.status_code != 401


def test_ui_token_is_viewer_level_for_reads(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    resp = _client().get("/api/v1/scheduler/timeline", headers=_auth("ui-tok"))
    assert resp.status_code != 401


# ── Writes require an ADMIN token ───────────────────────────────────────────

def test_write_rejected_without_token(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    resp = _client().post("/api/v1/optimization/propose", json={})
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").startswith("Bearer")


def test_write_rejected_with_ui_token(monkeypatch) -> None:
    """HEM_UI_TOKEN is baked into config.js → must NOT grant writes."""
    _set_tokens(monkeypatch, required=True)
    resp = _client().post("/api/v1/optimization/propose", json={}, headers=_auth("ui-tok"))
    assert resp.status_code == 401
    assert "admin" in resp.json()["error"].lower()


def test_write_passes_auth_with_admin_token(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    resp = _client().post("/api/v1/optimization/propose", json={}, headers=_auth("admin-tok"))
    assert resp.status_code != 401  # past the auth layer (handler may still 4xx/5xx)


def test_write_passes_auth_with_openclaw_token(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    resp = _client().post("/api/v1/optimization/propose", json={}, headers=_auth("oc-tok"))
    assert resp.status_code != 401


# ── Scoped sensor-ingest token (#540 W1) ────────────────────────────────────
# A non-admin credential that unlocks ONLY POST /api/v1/sensors/indoor — the
# token an internet-exposed ESPHome sensor carries.

_SENSOR = "/api/v1/sensors/indoor"


def test_ingest_token_can_post_its_own_route(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    from datetime import UTC, datetime
    body = {"readings": [{
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "temp_c": 21.0, "room": "living",
    }]}
    resp = _client().post(_SENSOR, json=body, headers=_auth("ingest-tok"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"] == 1


def test_ingest_route_still_open_to_admin(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    resp = _client().post(_SENSOR, json={"readings": []}, headers=_auth("admin-tok"))
    # 422 (empty batch fails min_length) is fine — past the auth layer.
    assert resp.status_code != 401


def test_ingest_route_rejects_no_token(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    assert _client().post(_SENSOR, json={"readings": []}).status_code == 401


def test_ingest_token_cannot_write_other_routes(monkeypatch) -> None:
    """The scoped token must be useless anywhere but its one endpoint."""
    _set_tokens(monkeypatch, required=True)
    resp = _client().post("/api/v1/optimization/propose", json={}, headers=_auth("ingest-tok"))
    assert resp.status_code == 401


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_ingest_token_is_post_only_on_its_route(monkeypatch, method) -> None:
    """The scope is EXACT `POST <route>` — no other WRITE verb rides the token.
    (GET on this route is a viewer-open read, gated separately, so it's not
    tested here — a token is irrelevant to public reads.)"""
    _set_tokens(monkeypatch, required=True)
    resp = getattr(_client(), method)(_SENSOR, headers=_auth("ingest-tok"))
    assert resp.status_code == 401, f"{method.upper()} {_SENSOR} must not pass ingest auth"


@pytest.mark.parametrize("path", [
    "/api/v1/sensors/indoorX",       # sibling sharing the prefix
    "/api/v1/sensors/indoor-purge",  # a plausible future admin op
    "/api/v1/sensors/indoor/clear",  # a sub-path
])
def test_ingest_token_is_exact_path_not_prefix(monkeypatch, path) -> None:
    """A POST to a sibling/sub path of the ingest route must NOT be unlocked —
    exact-path match, so a future route under the prefix can't ride the token."""
    _set_tokens(monkeypatch, required=True)
    assert _client().post(path, json={}, headers=_auth("ingest-tok")).status_code == 401


def test_ingest_token_cannot_read_admin_surfaces(monkeypatch) -> None:
    """A write credential must not unlock admin READS (Settings/Journal)."""
    _set_tokens(monkeypatch, required=True)
    c = _client()
    assert c.get("/api/v1/settings", headers=_auth("ingest-tok")).status_code == 401
    assert c.get("/api/v1/action-log", headers=_auth("ingest-tok")).status_code == 401


def test_ingest_token_is_not_admin_on_whoami(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    assert _client().get("/api/v1/whoami", headers=_auth("ingest-tok")).json()["role"] == "viewer"


def test_ingest_disabled_when_token_unset(monkeypatch) -> None:
    """Empty HEM_SENSOR_INGEST_TOKEN → feature off; the would-be token is just
    an invalid bearer, so the sensor route falls back to admin-only."""
    _set_tokens(monkeypatch, ingest="", required=True)
    assert _client().post(_SENSOR, json={"readings": []}, headers=_auth("ingest-tok")).status_code == 401


# ── Settings + Journal reads require ADMIN ──────────────────────────────────

def test_settings_read_requires_admin(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    c = _client()
    assert c.get("/api/v1/settings").status_code == 401
    assert c.get("/api/v1/settings", headers=_auth("ui-tok")).status_code == 401
    assert c.get("/api/v1/settings", headers=_auth("admin-tok")).status_code != 401


def test_action_log_read_requires_admin(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    c = _client()
    assert c.get("/api/v1/action-log").status_code == 401
    assert c.get("/api/v1/action-log", headers=_auth("admin-tok")).status_code != 401


@pytest.mark.parametrize("path", [
    "/api/v1/schedule/history",   # same action_log journal, different route
    "/api/v1/recent-triggers",    # action_log triggers
    "/api/v1/workbench/profiles", # LP override sandbox (admin tool)
    "/api/v1/integrations/smartthings/status",
])
def test_other_journal_and_admin_reads_require_admin(monkeypatch, path) -> None:
    """The Journal data leaks via more than one path — gate the DATA, not one
    route. A viewer must not read any of these."""
    _set_tokens(monkeypatch, required=True)
    c = _client()
    assert c.get(path).status_code == 401, f"{path} leaked to viewer"
    assert c.get(path, headers=_auth("ui-tok")).status_code == 401, f"{path} leaked to ui-token"
    assert c.get(path, headers=_auth("admin-tok")).status_code != 401, f"{path} blocked admin"


def test_viewer_can_read_plan_schedule_not_history(monkeypatch) -> None:
    """The PLAN (/api/v1/schedule) is viewer-readable; only its /history
    (the journal) is admin-gated. Guards against over-gating the prefix."""
    _set_tokens(monkeypatch, required=True)
    assert _client().get("/api/v1/schedule").status_code != 401


def test_viewer_refresh_does_not_burn_daikin_quota(monkeypatch) -> None:
    """GET /daikin/status?refresh=true is a privileged side effect (spends the
    Onecta quota). A viewer's refresh must be downgraded to the cached read;
    only an admin forces a live refresh."""
    _set_tokens(monkeypatch, required=True)
    from src.api import main as _main

    class _Cached:
        devices: list = []
        source = "test"
        stale = False

    calls = {"force": 0, "cached": 0}
    monkeypatch.setattr(_main.daikin_service, "force_refresh_devices",
                        lambda actor=None: (calls.__setitem__("force", calls["force"] + 1), _Cached())[1])
    monkeypatch.setattr(_main.daikin_service, "get_cached_devices",
                        lambda allow_refresh=False, actor=None: (calls.__setitem__("cached", calls["cached"] + 1), _Cached())[1])
    monkeypatch.setattr(_main, "get_daikin_client", lambda: None)

    c = _client()
    # Viewer asks to refresh → downgraded to cached (no quota burn).
    c.get("/api/v1/daikin/status?refresh=true")
    assert calls["force"] == 0 and calls["cached"] == 1
    # Admin refresh → live force.
    c.get("/api/v1/daikin/status?refresh=true", headers=_auth("admin-tok"))
    assert calls["force"] == 1


# ── Public paths ────────────────────────────────────────────────────────────

def test_health_and_whoami_public(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    c = _client()
    assert c.get("/api/v1/health").status_code == 200
    assert c.get("/api/v1/whoami").status_code == 200


def test_whoami_reports_role(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    c = _client()
    assert c.get("/api/v1/whoami").json()["role"] == "viewer"
    assert c.get("/api/v1/whoami", headers=_auth("ui-tok")).json()["role"] == "viewer"
    body = c.get("/api/v1/whoami", headers=_auth("admin-tok")).json()
    assert body["role"] == "admin"
    assert body["admin_configured"] is True
    assert body["auth_enforced"] is True


# ── /mcp untouched ──────────────────────────────────────────────────────────

def test_mcp_still_uses_openclaw_token(monkeypatch) -> None:
    _set_tokens(monkeypatch, openclaw="oc-tok-mcp", required=False)
    c = _client()
    assert c.get("/mcp/").status_code == 401
    assert c.get("/mcp/", headers=_auth("wrong")).status_code == 401
    assert c.get("/mcp/", headers=_auth("ui-tok")).status_code == 401


def test_non_api_v1_routes_unaffected(monkeypatch) -> None:
    _set_tokens(monkeypatch, required=True)
    assert _client().get("/healthz").status_code != 401
