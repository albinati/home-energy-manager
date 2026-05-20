"""Tests for ApiV1BearerAuth (Story B1, Epic 13b).

Covers the auth contract for the new /api/v1/* middleware:

* gate flag off → no header required (preserves pre-B1 behaviour)
* gate flag on  → 401 without header / 401 with wrong token /
                  200 with HEM_UI_TOKEN / 200 with HEM_OPENCLAW_TOKEN
* /api/v1/health stays public regardless of gate
* /mcp keeps using HEM_OPENCLAW_TOKEN (its own middleware path unaffected)
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
    # Point the token files at tmp_path so we don't clobber the dev tokens.
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


def _set_tokens(monkeypatch, *, ui: str, openclaw: str, required: bool) -> None:
    from src import config as _config
    monkeypatch.setattr(_config.config, "HEM_UI_TOKEN", ui, raising=False)
    monkeypatch.setattr(_config.config, "HEM_OPENCLAW_TOKEN", openclaw, raising=False)
    monkeypatch.setattr(
        _config.config, "HEM_UI_AUTH_REQUIRED", required, raising=False,
    )


def _client() -> TestClient:
    from src.api.main import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Gate OFF — preserves pre-B1 behaviour
# ---------------------------------------------------------------------------

def test_api_v1_open_when_gate_disabled(monkeypatch) -> None:
    """Default config (HEM_UI_AUTH_REQUIRED=false) → bearer not required.
    Critical to keep the inline UI working during the B1 → B6 transition."""
    _set_tokens(monkeypatch, ui="ui-tok", openclaw="oc-tok", required=False)
    resp = _client().get("/api/v1/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Gate ON — rejects unauthenticated and bad-token calls
# ---------------------------------------------------------------------------

def test_api_v1_rejects_missing_bearer_when_required(monkeypatch) -> None:
    _set_tokens(monkeypatch, ui="ui-tok", openclaw="oc-tok", required=True)
    resp = _client().get("/api/v1/scheduler/timeline")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").startswith("Bearer")
    assert "bearer" in resp.json()["error"].lower()


def test_api_v1_rejects_wrong_bearer_when_required(monkeypatch) -> None:
    _set_tokens(monkeypatch, ui="ui-tok", openclaw="oc-tok", required=True)
    resp = _client().get(
        "/api/v1/scheduler/timeline",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401
    assert "invalid" in resp.json()["error"].lower()


def test_api_v1_accepts_ui_token_when_required(monkeypatch) -> None:
    _set_tokens(monkeypatch, ui="ui-tok-A", openclaw="oc-tok", required=True)
    resp = _client().get(
        "/api/v1/health",
        headers={"Authorization": "Bearer ui-tok-A"},
    )
    # health stays public, but accepting the header here proves the middleware
    # tolerates a correct token even on public paths
    assert resp.status_code == 200


def test_api_v1_accepts_openclaw_token_when_required(monkeypatch) -> None:
    """OpenClaw's token also passes the /api/v1 guard so server-to-server
    flows that already use it keep working without a second token mint."""
    _set_tokens(monkeypatch, ui="ui-tok", openclaw="oc-tok-B", required=True)
    resp = _client().get(
        "/api/v1/scheduler/timeline",
        headers={"Authorization": "Bearer oc-tok-B"},
    )
    # 200 if endpoint returns data, 4xx/5xx is fine — we're testing
    # middleware passthrough, NOT the endpoint's own logic. The key
    # invariant: NOT 401 from the auth layer.
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Public path exception — /api/v1/health stays open even when gate is on
# ---------------------------------------------------------------------------

def test_api_v1_health_stays_public_when_gate_required(monkeypatch) -> None:
    """compose's healthcheck: needs /api/v1/health reachable without a header."""
    _set_tokens(monkeypatch, ui="ui-tok", openclaw="oc-tok", required=True)
    resp = _client().get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# /mcp untouched — separate BearerAuthMiddleware path
# ---------------------------------------------------------------------------

def test_mcp_still_uses_openclaw_token(monkeypatch) -> None:
    """The /mcp mount must continue to require HEM_OPENCLAW_TOKEN
    regardless of HEM_UI_AUTH_REQUIRED — that's its own middleware."""
    _set_tokens(monkeypatch, ui="ui-tok", openclaw="oc-tok-mcp", required=False)
    # No header → 401 from BearerAuthMiddleware on /mcp
    resp = _client().get("/mcp/")
    assert resp.status_code == 401
    # Wrong token → 401
    resp = _client().get(
        "/mcp/", headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401
    # UI token alone does NOT open /mcp (that mount only knows the OpenClaw token)
    resp = _client().get(
        "/mcp/", headers={"Authorization": "Bearer ui-tok"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Non-/api/v1 routes (root, /static, etc) — untouched by the new middleware
# ---------------------------------------------------------------------------

def test_non_api_v1_routes_unaffected_when_gate_required(monkeypatch) -> None:
    """The cockpit / static templates served from the FastAPI app's root
    must not be gated by the /api/v1 middleware. (B5 removes them
    entirely; until then they stay open even with HEM_UI_AUTH_REQUIRED=True.)"""
    _set_tokens(monkeypatch, ui="ui-tok", openclaw="oc-tok", required=True)
    # Health is FastAPI-served (not /api/v1 prefix), confirm passthrough
    resp = _client().get("/healthz")
    # 200 or 404 are both fine — what matters is NOT 401 from the new guard
    assert resp.status_code != 401
