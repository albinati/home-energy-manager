"""Fox Scheduler V3 read-back after set (#23) + idempotent upload guard (#38)."""

import logging

import pytest

from src.foxess.client import FoxESSClient
from src.foxess.models import SchedulerGroup, SchedulerState


def test_warn_if_scheduler_v3_mismatch_logs_when_counts_differ(caplog):
    exp = [
        SchedulerGroup(0, 0, 6, 0, "SelfUse"),
        SchedulerGroup(6, 0, 12, 0, "SelfUse"),
    ]

    class Dummy:
        def get_scheduler_v3(self) -> SchedulerState:
            return SchedulerState(enabled=True, groups=[exp[0]])

    caplog.set_level(logging.WARNING)
    FoxESSClient.warn_if_scheduler_v3_mismatch(Dummy(), exp)
    assert "read-back mismatch" in caplog.text


def test_warn_if_scheduler_v3_mismatch_silent_when_match(caplog):
    g = SchedulerGroup(0, 0, 12, 0, "SelfUse")
    exp = [g]

    class Dummy:
        def get_scheduler_v3(self) -> SchedulerState:
            return SchedulerState(enabled=True, groups=[g])

    caplog.set_level(logging.WARNING)
    FoxESSClient.warn_if_scheduler_v3_mismatch(Dummy(), exp)
    assert "read-back mismatch" not in caplog.text


def test_warn_if_scheduler_v3_mismatch_logs_on_get_failure(caplog):
    class Dummy:
        def get_scheduler_v3(self) -> SchedulerState:
            raise RuntimeError("network")

    caplog.set_level(logging.WARNING)
    FoxESSClient.warn_if_scheduler_v3_mismatch(Dummy(), [])
    assert "read-back after set failed" in caplog.text


# ---------------------------------------------------------------------------
# #38 — idempotent set_scheduler_v3: skip POST when identical, upload on diff.
# ---------------------------------------------------------------------------


class _CaptureClient:
    """Minimal duck-typed FoxESSClient for set_scheduler_v3 idempotency tests."""

    def __init__(self, current: list[SchedulerGroup]):
        self._current = current
        self.post_calls: list[tuple[str, dict]] = []

    def _sn_scheduler(self) -> str:
        return "TEST-SN"

    def get_scheduler_v3(self) -> SchedulerState:
        return SchedulerState(enabled=True, groups=list(self._current))

    def _open_post_v3(self, path: str, payload: dict) -> None:
        self.post_calls.append((path, payload))


def test_set_scheduler_v3_skips_when_equal(monkeypatch, caplog):
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 1000)
    g = [
        SchedulerGroup(0, 0, 6, 0, "SelfUse", min_soc_on_grid=10),
        SchedulerGroup(6, 0, 12, 0, "ForceCharge", min_soc_on_grid=10, fd_soc=95, fd_pwr=3000),
    ]
    client = _CaptureClient(current=g)
    caplog.set_level(logging.INFO, logger="src.foxess.client")
    FoxESSClient.set_scheduler_v3(client, g)
    assert client.post_calls == [], "identical schedule must not be re-uploaded"
    assert "unchanged" in caplog.text


def test_set_scheduler_v3_uploads_when_different(monkeypatch):
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 1000)
    current = [SchedulerGroup(0, 0, 12, 0, "SelfUse", min_soc_on_grid=10)]
    new = [
        SchedulerGroup(0, 0, 6, 0, "SelfUse", min_soc_on_grid=10),
        SchedulerGroup(6, 0, 12, 0, "ForceCharge", min_soc_on_grid=10, fd_soc=95, fd_pwr=3000),
    ]
    client = _CaptureClient(current=current)
    FoxESSClient.set_scheduler_v3(client, new)
    assert len(client.post_calls) == 1
    path, payload = client.post_calls[0]
    assert path == "/device/scheduler/enable"
    assert len(payload["groups"]) == 2


def test_set_scheduler_v3_uploads_unconditionally_when_budget_low(monkeypatch):
    """Fail-open: when Fox quota has <2 calls left, skip the pre-read GET and
    upload anyway — the safety invariant 'push the latest plan' beats saving
    one redundant call when the quota is already tight."""
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 1)
    g = [SchedulerGroup(0, 0, 24, 0, "SelfUse")]

    class NoGet(_CaptureClient):
        def get_scheduler_v3(self) -> SchedulerState:  # type: ignore[override]
            raise AssertionError("pre-read GET must be skipped under low budget")

    client = NoGet(current=[])
    FoxESSClient.set_scheduler_v3(client, g)
    assert len(client.post_calls) == 1


def test_set_scheduler_v3_uploads_when_get_fails(monkeypatch):
    """If the pre-read GET raises, fall through and upload — we never want a
    network hiccup on the optimisation happy-path to also block the write."""
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 1000)
    g = [SchedulerGroup(0, 0, 24, 0, "SelfUse")]

    class FlakyGet(_CaptureClient):
        def get_scheduler_v3(self) -> SchedulerState:  # type: ignore[override]
            raise RuntimeError("transient network")

    client = FlakyGet(current=[])
    FoxESSClient.set_scheduler_v3(client, g)
    assert len(client.post_calls) == 1


def test_set_scheduler_v3_skip_if_equal_false_forces_upload(monkeypatch):
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 1000)
    g = [SchedulerGroup(0, 0, 24, 0, "SelfUse")]
    client = _CaptureClient(current=g)
    FoxESSClient.set_scheduler_v3(client, g, skip_if_equal=False)
    assert len(client.post_calls) == 1


def test_scheduler_group_fingerprint_stable_and_distinguishes():
    a = SchedulerGroup(6, 0, 12, 0, "ForceCharge", min_soc_on_grid=10, fd_soc=95, fd_pwr=3000)
    b = SchedulerGroup(6, 0, 12, 0, "ForceCharge", min_soc_on_grid=10, fd_soc=95, fd_pwr=3000)
    c = SchedulerGroup(6, 0, 12, 0, "ForceCharge", min_soc_on_grid=10, fd_soc=95, fd_pwr=2500)
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()
    # Must be hashable (goes into sets / dict keys in future callers).
    assert hash(a.fingerprint()) == hash(b.fingerprint())
