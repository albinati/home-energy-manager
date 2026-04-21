"""Tests for quota accounting at the Daikin transport layer (Phase 4.1, issue #40).

The canonical contract is:
    one physical HTTP call from DaikinClient._get / _patch →
    exactly one api_quota.record_call("daikin", kind, ok) entry.

Failures (HTTP 4xx/5xx) still count against the budget because Daikin sees them.
"""
from __future__ import annotations

import urllib.error
from io import BytesIO
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Each test gets its own SQLite file; no module reloads (that pattern
    pollutes other tests' cached module references)."""
    monkeypatch.setattr("src.config.config.DB_PATH", str(tmp_path / "test_quota.db"))
    import src.api_quota as aq
    aq.ensure_table()


def _mock_response(body: bytes = b'{"ok": true}') -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = body
    return resp


def test_get_records_one_call_on_success(monkeypatch):
    import src.api_quota as aq
    import src.daikin.client as client_mod

    monkeypatch.setattr(client_mod, "get_valid_access_token", lambda **_: "tok")
    monkeypatch.setattr(
        client_mod.urllib.request, "urlopen",
        lambda *a, **kw: _mock_response(b'{"ok": true}'),
    )

    client = client_mod.DaikinClient()
    client._get("/gateway-devices")

    assert aq.count_calls_24h("daikin") == 1


def test_get_records_failure_on_http_error(monkeypatch):
    import src.api_quota as aq
    import src.daikin.client as client_mod

    monkeypatch.setattr(client_mod, "get_valid_access_token", lambda **_: "tok")

    def _raise_500(*a, **kw):
        raise urllib.error.HTTPError("u", 500, "boom", None, BytesIO(b"error"))

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise_500)

    client = client_mod.DaikinClient()
    with pytest.raises(client_mod.DaikinError):
        client._get("/gateway-devices")

    assert aq.count_calls_24h("daikin") == 1


def test_patch_records_one_call_on_success(monkeypatch):
    import src.api_quota as aq
    import src.daikin.client as client_mod

    monkeypatch.setattr(client_mod, "get_valid_access_token", lambda **_: "tok")
    monkeypatch.setattr(
        client_mod.urllib.request, "urlopen",
        lambda *a, **kw: _mock_response(b""),
    )

    client = client_mod.DaikinClient()
    client._patch("/x", {"value": 1})

    assert aq.count_calls_24h("daikin") == 1


def test_patch_records_failure_on_read_only_error(monkeypatch):
    """400 READ_ONLY_CHARACTERISTIC still counts — Daikin served the request."""
    import src.api_quota as aq
    import src.daikin.client as client_mod

    monkeypatch.setattr(client_mod, "get_valid_access_token", lambda **_: "tok")

    def _raise_400(*a, **kw):
        raise urllib.error.HTTPError(
            "u", 400, "bad", None,
            BytesIO(b'{"error":"READ_ONLY_CHARACTERISTIC"}'),
        )

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _raise_400)

    client = client_mod.DaikinClient()
    with pytest.raises(client_mod.DaikinError, match=r"\[read_only\]"):
        client._patch("/x", {})

    assert aq.count_calls_24h("daikin") == 1


def test_get_401_retry_records_both_attempts(monkeypatch):
    """401 auth-retry path issues 2 physical HTTP requests; both must count."""
    import src.api_quota as aq
    import src.daikin.client as client_mod

    monkeypatch.setattr(client_mod, "get_valid_access_token", lambda **_: "tok")

    calls = {"n": 0}

    def _urlopen(req, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError("u", 401, "unauth", None, BytesIO(b""))
        return _mock_response(b'{"ok": true}')

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", _urlopen)

    client = client_mod.DaikinClient()
    client._get("/gateway-devices")

    assert aq.count_calls_24h("daikin") == 2
