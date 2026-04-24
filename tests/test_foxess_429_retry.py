"""Fox 429 retry — mirrors the Daikin pattern.

Verifies:
* HTTP 429 triggers a retry up to FOX_HTTP_429_MAX_RETRIES times.
* Retry-After (seconds + HTTP-date) is respected, capped by
  FOX_HTTP_429_MAX_SLEEP_SECONDS.
* A 429 beyond the max-retries budget surfaces as FoxESSError.
* Non-429 HTTPErrors are not retried.
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
import urllib.error

from src.foxess.client import FoxESSClient, FoxESSError


class _HTTP429(urllib.error.HTTPError):
    """Synthetic 429 with a controllable Retry-After header."""
    def __init__(self, retry_after: str | None = "2"):
        hdrs = {"Retry-After": retry_after} if retry_after is not None else {}
        super().__init__(
            url="https://foxesscloud.com/op/v0/device/real/query",
            code=429, msg="Too Many Requests",
            hdrs=hdrs, fp=io.BytesIO(b'{"errno":429,"msg":"rate limited"}'),
        )


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()
    def read(self):
        return self._body


def _mk_client() -> FoxESSClient:
    return FoxESSClient(device_sn="SN123", api_key="X" * 32)


def _ok_result() -> dict:
    return {"errno": 0, "result": {"datas": []}}


def test_429_triggers_retry_and_eventually_succeeds(monkeypatch):
    c = _mk_client()
    calls = {"n": 0}

    def fake_urlopen(req, timeout=15):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _HTTP429(retry_after="0.05")
        return _Resp(_ok_result())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: None)

    result = c._open_post("/device/real/query", {"sn": "SN123"})
    assert calls["n"] == 2
    assert "datas" in result


def test_429_respects_max_retries_and_surfaces_as_foxesserror(monkeypatch):
    c = _mk_client()
    monkeypatch.setattr("src.config.config.FOX_HTTP_429_MAX_RETRIES", 1)

    calls = {"n": 0}

    def fake_urlopen(req, timeout=15):
        calls["n"] += 1
        raise _HTTP429(retry_after="0.01")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: None)

    with pytest.raises(FoxESSError) as ei:
        c._open_post("/device/real/query", {"sn": "SN123"})

    # 1 initial + 1 retry = 2 attempts (max_retries=1).
    assert calls["n"] == 2
    assert "429" in str(ei.value)


def test_non_429_is_not_retried(monkeypatch):
    c = _mk_client()

    calls = {"n": 0}

    def fake_urlopen(req, timeout=15):
        calls["n"] += 1
        err = urllib.error.HTTPError(
            url="...", code=500, msg="Server Error", hdrs={}, fp=io.BytesIO(b"oops"),
        )
        raise err

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(FoxESSError) as ei:
        c._open_post("/device/real/query", {"sn": "SN123"})

    # No retry — single attempt then surface.
    assert calls["n"] == 1
    assert "500" in str(ei.value)


def test_retry_after_cap_respected(monkeypatch):
    c = _mk_client()
    # Pretend the server asked for 86400s like Daikin's edge case. We should
    # cap at FOX_HTTP_429_MAX_SLEEP_SECONDS.
    monkeypatch.setattr("src.config.config.FOX_HTTP_429_MAX_SLEEP_SECONDS", 10)

    calls = {"n": 0}
    slept: list[float] = []

    def fake_urlopen(req, timeout=15):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _HTTP429(retry_after="86400")
        return _Resp(_ok_result())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: slept.append(s))

    c._open_post("/device/real/query", {"sn": "SN123"})
    assert calls["n"] == 2
    # Sleep should have been capped, not 86400s.
    assert slept == [10.0]


def test_retry_after_missing_uses_default(monkeypatch):
    c = _mk_client()

    calls = {"n": 0}
    slept: list[float] = []

    def fake_urlopen(req, timeout=15):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _HTTP429(retry_after=None)
        return _Resp(_ok_result())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: slept.append(s))

    c._open_post("/device/real/query", {"sn": "SN123"})
    assert calls["n"] == 2
    # Default 5s (under the cap).
    assert slept and 0 < slept[0] <= 5.0
