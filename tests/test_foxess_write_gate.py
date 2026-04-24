"""Fox inter-write delay — pace writes to avoid 40257 in quick-succession
PATCHes. Mirrors the TonyM1958/FoxESS-Cloud 2-second pattern."""
from __future__ import annotations

import json

import pytest

from src.foxess.client import FoxESSClient
from src.foxess.models import ChargePeriod, SchedulerGroup


class _FakeResp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()
    def read(self):
        return self._body


def _mk_client() -> FoxESSClient:
    return FoxESSClient(device_sn="SN123", api_key="X" * 32)


def _patch_http(monkeypatch, response_payload: dict | None = None):
    response_payload = response_payload or {"errno": 0, "result": {}}
    def fake_urlopen(req, timeout=15):
        return _FakeResp(response_payload)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_first_write_does_not_sleep(monkeypatch):
    c = _mk_client()
    _patch_http(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("src.config.config.FOX_WRITE_INTER_DELAY_SECONDS", 2.0)
    c.set_work_mode("Self Use")
    # _last_write_monotonic defaults to 0.0, so the first write has
    # time.monotonic() - 0 = many seconds, so wait = 2 - many = negative → no sleep.
    assert slept == []


def test_second_write_sleeps_remaining_interval(monkeypatch):
    c = _mk_client()
    _patch_http(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("src.config.config.FOX_WRITE_INTER_DELAY_SECONDS", 2.0)
    # Pin monotonic to make math deterministic.
    clock = [1000.0]
    monkeypatch.setattr("src.foxess.client.time.monotonic", lambda: clock[0])

    c.set_work_mode("Self Use")
    # Second write ~0.5s later should sleep 1.5s to reach 2s gap.
    clock[0] += 0.5
    c.set_work_mode("Back Up")
    assert len(slept) == 1
    assert abs(slept[0] - 1.5) < 1e-6


def test_delay_zero_disables_gate(monkeypatch):
    c = _mk_client()
    _patch_http(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("src.config.config.FOX_WRITE_INTER_DELAY_SECONDS", 0.0)

    c.set_work_mode("Self Use")
    c.set_work_mode("Back Up")
    assert slept == []


def test_scheduler_writes_also_pace(monkeypatch):
    c = _mk_client()
    _patch_http(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("src.config.config.FOX_WRITE_INTER_DELAY_SECONDS", 2.0)
    clock = [1000.0]
    monkeypatch.setattr("src.foxess.client.time.monotonic", lambda: clock[0])

    # Bypass the idempotency pre-read (would also stamp write time otherwise).
    c.set_scheduler_v3([], is_default=False, skip_if_equal=False)
    clock[0] += 0.1
    c.set_scheduler_flag(True)
    # The flag write should have slept ~1.9s.
    assert len(slept) == 1
    assert 1.8 < slept[0] < 2.0


def test_charge_period_gates(monkeypatch):
    c = _mk_client()
    _patch_http(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr("src.foxess.client.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("src.config.config.FOX_WRITE_INTER_DELAY_SECONDS", 2.0)
    clock = [1000.0]
    monkeypatch.setattr("src.foxess.client.time.monotonic", lambda: clock[0])

    c.set_charge_period(0, ChargePeriod(start_time="01:00", end_time="05:00", target_soc=100))
    clock[0] += 0.2
    c.set_charge_period(1, ChargePeriod(start_time="13:00", end_time="15:00", target_soc=95))
    assert len(slept) == 1
    assert 1.7 < slept[0] < 2.0
