"""SmartThings client tests — mock urllib.request, assert auth header,
response parsing, error mapping, and OPENCLAW_READ_ONLY semantics."""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from src.smartthings.client import SmartThingsClient, SmartThingsError


def _resp(status: int, body: dict | str) -> "object":
    """Fake urlopen context manager."""
    raw = json.dumps(body).encode() if isinstance(body, dict) else (body or "").encode()

    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *_):
            return False

        def read(self_inner):
            return raw

        def getcode(self_inner):
            return status

    return _Resp()


def _http_error(status: int, body: bytes = b""):
    """Build a urllib HTTPError with a body that .read() returns."""
    import urllib.error
    return urllib.error.HTTPError(
        "http://example", status, "boom", hdrs=None, fp=io.BytesIO(body)
    )


def test_request_sets_bearer_header():
    client = SmartThingsClient(access_token="test-pat", base_url="https://api.smartthings.com/v1")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return _resp(200, {"items": []})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.list_devices()

    assert captured["url"] == "https://api.smartthings.com/v1/devices"
    assert captured["method"] == "GET"
    # urllib normalizes header keys to title-case
    auth = captured["headers"].get("Authorization") or captured["headers"].get("authorization")
    assert auth == "Bearer test-pat"


def test_401_raises_auth_invalid_with_explicit_token():
    """Explicit-token clients (test fixtures) get auth_invalid on 401 — no
    refresh attempt because there's no refresh_token in the test path."""
    client = SmartThingsClient(access_token="bad-pat")
    with patch("urllib.request.urlopen", side_effect=_http_error(401, b"unauthorized")):
        with pytest.raises(SmartThingsError) as ex:
            client.list_devices()
    assert ex.value.code == "auth_invalid"
    assert ex.value.http_status == 401


def test_401_triggers_refresh_when_using_oauth_token_file():
    """Production path: client without access_token sources via auth.get_valid_access_token.
    On 401, must call get_valid_access_token(force_refresh=True) and retry."""
    from unittest.mock import MagicMock

    client = SmartThingsClient()  # no explicit token → uses auth module
    calls = {"refresh_count": 0, "request_count": 0}

    def fake_token(*, force_refresh=False):
        calls["refresh_count"] += int(force_refresh)
        return "fresh-token-after-refresh" if force_refresh else "stale-token"

    def fake_urlopen(req, timeout=None):
        calls["request_count"] += 1
        auth_hdr = dict(req.header_items()).get("Authorization", "")
        if "stale-token" in auth_hdr:
            raise _http_error(401, b"expired")
        # Refreshed token → success
        return _resp(200, {"items": []})

    with patch("src.smartthings.client._auth.get_valid_access_token", side_effect=fake_token):
        with patch("urllib.request.urlopen", fake_urlopen):
            client.list_devices()

    assert calls["request_count"] == 2  # initial + retry-after-refresh
    assert calls["refresh_count"] == 1  # exactly one forced refresh


def test_get_remote_control_enabled_parses_string_true():
    client = SmartThingsClient(access_token="x")
    payload = {"remoteControlEnabled": {"value": "true"}}
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        assert client.get_remote_control_enabled("dev-1") is True


def test_get_remote_control_enabled_parses_native_bool():
    client = SmartThingsClient(access_token="x")
    payload = {"remoteControlEnabled": {"value": True}}
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        assert client.get_remote_control_enabled("dev-1") is True


def test_get_remote_control_enabled_false_when_string_false():
    client = SmartThingsClient(access_token="x")
    payload = {"remoteControlEnabled": {"value": "false"}}
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        assert client.get_remote_control_enabled("dev-1") is False


def test_get_remote_control_enabled_false_on_unparseable():
    client = SmartThingsClient(access_token="x")
    payload = {"remoteControlEnabled": {"value": None}}
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        assert client.get_remote_control_enabled("dev-1") is False


def test_start_cycle_body_shape():
    client = SmartThingsClient(access_token="x", base_url="https://api.smartthings.com/v1")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data.decode() if req.data else None
        return _resp(200, {"results": [{"status": "ACCEPTED"}]})

    with patch("urllib.request.urlopen", fake_urlopen):
        # Force read-only off so the cycle actually fires.
        with patch("src.smartthings.client.config") as cfg:
            cfg.OPENCLAW_READ_ONLY = False
            client.start_cycle("dev-1")

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/devices/dev-1/commands")
    body = json.loads(captured["body"])
    assert body == {
        "commands": [
            {
                "component": "main",
                "capability": "washerOperatingState",
                "command": "setMachineState",
                "arguments": ["run"],
            }
        ]
    }


def test_start_cycle_skipped_in_read_only(monkeypatch):
    """OPENCLAW_READ_ONLY=true → no HTTP call, response says skipped."""
    client = SmartThingsClient(access_token="x")

    def boom(*a, **kw):
        raise AssertionError("urlopen must NOT be called in read-only mode")

    with patch("urllib.request.urlopen", boom):
        with patch("src.smartthings.client.config") as cfg:
            cfg.OPENCLAW_READ_ONLY = True
            result = client.start_cycle("dev-1")
    assert result == {"skipped": "read_only"}


def test_5xx_retries_once_then_raises():
    client = SmartThingsClient(access_token="x")
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(503, b"unavailable")

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(SmartThingsError) as ex:
            client.list_devices()
    assert ex.value.code == "http_error"
    assert ex.value.http_status == 503
    assert calls["n"] == 2  # one retry on 5xx


def test_list_devices_returns_items():
    client = SmartThingsClient(access_token="x")
    payload = {
        "items": [
            {"deviceId": "dev-1", "label": "Washer"},
            {"deviceId": "dev-2", "label": "Dryer"},
        ]
    }
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        devices = client.list_devices()
    assert len(devices) == 2
    assert devices[0]["deviceId"] == "dev-1"


def test_list_devices_empty_when_no_items_key():
    client = SmartThingsClient(access_token="x")
    with patch("urllib.request.urlopen", return_value=_resp(200, {})):
        devices = client.list_devices()
    assert devices == []
