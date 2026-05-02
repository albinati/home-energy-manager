"""SmartThings OAuth auth module tests.

Covers:
  - Basic Auth header construction (the differentiator from Daikin).
  - Token persistence (load/save round-trip + 0600 perms).
  - exchange_code happy path.
  - refresh_tokens carries refresh_token forward when Samsung omits it.
  - Circuit breaker trips after 3 consecutive failures.
  - get_valid_access_token refresh-if-stale + min-gap throttle.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config import config as app_config
from src.smartthings import auth as st_auth


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_config, "SMARTTHINGS_CLIENT_ID", "test-client")
    monkeypatch.setattr(app_config, "SMARTTHINGS_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(
        app_config, "SMARTTHINGS_REDIRECT_URI",
        "http://localhost:8080/oauth/smartthings/callback",
    )
    monkeypatch.setattr(app_config, "SMARTTHINGS_TOKEN_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(app_config, "SMARTTHINGS_OAUTH_SCOPES", "r:devices:* x:devices:*")
    monkeypatch.setattr(app_config, "SMARTTHINGS_ACCESS_REFRESH_LEEWAY_SECONDS", 60)
    monkeypatch.setattr(app_config, "SMARTTHINGS_TOKEN_REFRESH_MIN_INTERVAL_SECONDS", 0)
    # Reset circuit breaker between tests
    st_auth._reset_auth_circuit()
    st_auth._last_token_refresh_monotonic = 0.0


def test_basic_auth_header_encodes_client_credentials() -> None:
    h = st_auth._basic_auth_header()
    assert h.startswith("Basic ")
    decoded = base64.b64decode(h[6:]).decode()
    assert decoded == "test-client:test-secret"


def test_basic_auth_header_raises_when_creds_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "SMARTTHINGS_CLIENT_ID", "")
    with pytest.raises(st_auth.SmartThingsAuthError):
        st_auth._basic_auth_header()


def test_save_load_tokens_roundtrip() -> None:
    tokens = {
        "access_token": "abc",
        "refresh_token": "def",
        "expires_in": 86400,
        "token_type": "bearer",
        "scope": "r:devices:* x:devices:*",
        "obtained_at": 1714657123,
    }
    st_auth.save_tokens(tokens)
    loaded = st_auth.load_tokens()
    assert loaded == tokens

    # 0600 perms
    p = st_auth._resolve_token_path()
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_load_tokens_raises_when_file_missing() -> None:
    with pytest.raises(st_auth.SmartThingsAuthError) as ex:
        st_auth.load_tokens()
    assert "not present" in str(ex.value)


def test_has_tokens_reflects_file_presence() -> None:
    assert st_auth.has_tokens() is False
    st_auth.save_tokens({"access_token": "x", "refresh_token": "y"})
    assert st_auth.has_tokens() is True


def test_authorize_url_includes_required_params() -> None:
    url = st_auth.authorize_url("test-state-token")
    assert "client_id=test-client" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Foauth%2Fsmartthings%2Fcallback" in url
    assert "scope=r%3Adevices%3A%2A+x%3Adevices%3A%2A" in url
    assert "state=test-state-token" in url
    assert "response_type=code" in url


def test_exchange_code_calls_token_endpoint_with_basic_auth() -> None:
    payload = {
        "access_token": "AT-NEW",
        "refresh_token": "RT-NEW",
        "expires_in": 86400,
        "scope": "r:devices:* x:devices:*",
        "token_type": "bearer",
    }
    fake_proc = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
    with patch("subprocess.run", return_value=fake_proc) as run:
        out = st_auth.exchange_code("test-code")
    assert out["access_token"] == "AT-NEW"
    assert out["refresh_token"] == "RT-NEW"
    assert "obtained_at" in out  # we set this

    # Confirm Basic Auth header was sent
    call_args = run.call_args[0][0]  # the curl argv list
    assert any(
        "Authorization: Basic" in a for a in call_args
    ), f"Basic Auth header missing in curl args: {call_args}"
    # Confirm grant_type=authorization_code was form-encoded into -d body
    body_arg_idx = call_args.index("-d") + 1
    assert "grant_type=authorization_code" in call_args[body_arg_idx]
    assert "code=test-code" in call_args[body_arg_idx]


def test_refresh_tokens_carries_refresh_token_when_omitted() -> None:
    """Samsung may omit refresh_token on refresh — our code must preserve the old one."""
    old = {
        "access_token": "AT-OLD",
        "refresh_token": "RT-OLD",
        "expires_in": 86400,
        "obtained_at": int(time.time()) - 90000,
    }
    new_payload = {
        "access_token": "AT-FRESH",
        "expires_in": 86400,
        "scope": "r:devices:* x:devices:*",
        # NOTE: no refresh_token in response
    }
    fake_proc = MagicMock(returncode=0, stdout=json.dumps(new_payload), stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        out = st_auth.refresh_tokens(old)
    assert out["access_token"] == "AT-FRESH"
    assert out["refresh_token"] == "RT-OLD"  # carried over


def test_refresh_circuit_trips_after_three_failures() -> None:
    old = {"access_token": "x", "refresh_token": "RT", "expires_in": 86400, "obtained_at": 0}
    fake_proc = MagicMock(returncode=0, stdout='{"error":"invalid_grant"}', stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        for _ in range(3):
            with pytest.raises(st_auth.SmartThingsAuthError):
                st_auth.refresh_tokens(old)
        # 4th call → circuit open
        with pytest.raises(st_auth.SmartThingsAuthCircuitOpen):
            st_auth.refresh_tokens(old)


def test_get_valid_access_token_refreshes_when_stale() -> None:
    """Token expired beyond leeway → refresh_tokens called."""
    stale_tokens = {
        "access_token": "AT-STALE",
        "refresh_token": "RT",
        "expires_in": 100,           # very short
        "obtained_at": int(time.time()) - 200,  # already expired
    }
    st_auth.save_tokens(stale_tokens)

    fresh_payload = {
        "access_token": "AT-FRESH",
        "expires_in": 86400,
        "scope": "r:devices:*",
    }
    fake_proc = MagicMock(returncode=0, stdout=json.dumps(fresh_payload), stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        token = st_auth.get_valid_access_token()
    assert token == "AT-FRESH"


def test_get_valid_access_token_returns_cached_when_not_stale() -> None:
    fresh_tokens = {
        "access_token": "AT-FRESH",
        "refresh_token": "RT",
        "expires_in": 86400,
        "obtained_at": int(time.time()),
    }
    st_auth.save_tokens(fresh_tokens)

    def boom(*a, **kw):  # noqa: ANN001
        raise AssertionError("subprocess.run must NOT be called when token is fresh")

    with patch("subprocess.run", side_effect=boom):
        token = st_auth.get_valid_access_token()
    assert token == "AT-FRESH"


def test_get_valid_access_token_force_refresh_overrides_min_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """force_refresh=True ignores SMARTTHINGS_TOKEN_REFRESH_MIN_INTERVAL_SECONDS."""
    monkeypatch.setattr(app_config, "SMARTTHINGS_TOKEN_REFRESH_MIN_INTERVAL_SECONDS", 3600)
    tokens = {
        "access_token": "AT-OLD",
        "refresh_token": "RT",
        "expires_in": 86400,
        "obtained_at": int(time.time()),  # not stale
    }
    st_auth.save_tokens(tokens)
    fresh_payload = {"access_token": "AT-FORCED", "expires_in": 86400}
    fake_proc = MagicMock(returncode=0, stdout=json.dumps(fresh_payload), stderr="")
    with patch("subprocess.run", return_value=fake_proc):
        token = st_auth.get_valid_access_token(force_refresh=True)
    assert token == "AT-FORCED"
