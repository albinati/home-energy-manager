"""Daikin auth circuit breaker.

Once the refresh_token goes bad (>1 year old, revoked, etc.) every
heartbeat would try refresh_tokens() → 401 → log + retry. The breaker
counts consecutive failures and, after the threshold, refuses further
attempts for a cool-down window so the service keeps running on cached
+ estimated telemetry rather than spamming the Onecta token endpoint.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from src.daikin import auth


@pytest.fixture(autouse=True)
def _reset_circuit():
    """Ensure a clean breaker state between tests."""
    auth._reset_auth_circuit()
    yield
    auth._reset_auth_circuit()


class _FakeResult:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _token_file(tmp_path, monkeypatch) -> None:
    """Pin the token file path + prevent real disk writes in save_tokens()."""
    p = tmp_path / "fake-tokens.json"
    p.write_text(json.dumps({
        "refresh_token": "r" * 16,
        "access_token": "a" * 16,
        "expires_in": 3600,
        "obtained_at": 0,
    }))
    monkeypatch.setattr(auth, "TOKEN_FILE", p)


def test_breaker_trips_after_threshold_consecutive_failures(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_AUTH_CIRCUIT_THRESHOLD", 3)

    def fake_run(*args, **kwargs):
        return _FakeResult(stdout='{"error":"invalid_grant","error_description":"expired"}')
    monkeypatch.setattr(subprocess, "run", fake_run)

    tokens = auth.load_tokens()
    # First 3 failures each raise RuntimeError; the 4th should raise
    # DaikinAuthCircuitOpen because the circuit has tripped.
    for i in range(3):
        with pytest.raises(RuntimeError):
            auth.refresh_tokens(tokens)

    with pytest.raises(auth.DaikinAuthCircuitOpen):
        auth.refresh_tokens(tokens)


def test_breaker_clears_on_success(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_AUTH_CIRCUIT_THRESHOLD", 2)

    calls = {"n": 0}
    def fake_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResult(stdout='{"error":"invalid_grant"}')
        # Second call: succeed.
        return _FakeResult(stdout=json.dumps({
            "access_token": "new", "refresh_token": "new", "expires_in": 3600,
        }))
    monkeypatch.setattr(subprocess, "run", fake_run)

    tokens = auth.load_tokens()
    with pytest.raises(RuntimeError):
        auth.refresh_tokens(tokens)
    # One failure; circuit not yet tripped (threshold=2).
    n_fails, tripped, _ = auth._auth_circuit_state()
    assert n_fails == 1
    assert tripped is False

    # Next call succeeds — counter must reset.
    auth.refresh_tokens(tokens)
    n_fails, tripped, _ = auth._auth_circuit_state()
    assert n_fails == 0
    assert tripped is False


def test_tripped_circuit_fails_fast_without_curl(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_AUTH_CIRCUIT_THRESHOLD", 1)

    curl_calls = {"n": 0}
    def fake_run(*args, **kwargs):
        curl_calls["n"] += 1
        return _FakeResult(stdout='{"error":"invalid_grant"}')
    monkeypatch.setattr(subprocess, "run", fake_run)

    tokens = auth.load_tokens()
    # First call: curl fires, threshold=1 so this trips the circuit.
    with pytest.raises(RuntimeError):
        auth.refresh_tokens(tokens)
    assert curl_calls["n"] == 1

    # Second call: MUST NOT fire curl — should raise DaikinAuthCircuitOpen immediately.
    with pytest.raises(auth.DaikinAuthCircuitOpen):
        auth.refresh_tokens(tokens)
    assert curl_calls["n"] == 1, "circuit must short-circuit before hitting curl"


def test_notification_fires_once_per_trip(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch)
    monkeypatch.setattr("src.config.config.DAIKIN_AUTH_CIRCUIT_THRESHOLD", 2)

    def fake_run(*args, **kwargs):
        return _FakeResult(stdout='{"error":"invalid_grant"}')
    monkeypatch.setattr(subprocess, "run", fake_run)

    notify_calls: list[tuple[str, dict]] = []
    def fake_notify(msg, extra=None):
        notify_calls.append((msg, extra or {}))
    monkeypatch.setattr("src.notifier.notify_risk", fake_notify)

    tokens = auth.load_tokens()
    # Three consecutive failures; first two pre-trip, third is post-trip
    # (DaikinAuthCircuitOpen) and must not re-notify.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            auth.refresh_tokens(tokens)
    with pytest.raises(auth.DaikinAuthCircuitOpen):
        auth.refresh_tokens(tokens)

    # Exactly one notification.
    assert len(notify_calls) == 1
    assert "refresh_token likely expired" in notify_calls[0][0]
