"""Fox Scheduler V3 read-back after set (#23)."""

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
