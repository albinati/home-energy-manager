"""Peak window logic stays in sync between agile slots and Daikin LWT."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from src.scheduler import daikin as daikin_mod
from src.scheduler.agile import (
    scheduler_peak_contains_wall_time,
    utc_instant_in_scheduler_peak,
)
from src.scheduler.daikin import compute_lwt_adjustment


def test_scheduler_peak_contains_wall_time_inclusive() -> None:
    from datetime import time

    assert scheduler_peak_contains_wall_time(time(16, 30), "16:00", "19:00") is True
    assert scheduler_peak_contains_wall_time(time(16, 0), "16:00", "19:00") is True
    assert scheduler_peak_contains_wall_time(time(19, 0), "16:00", "19:00") is True
    assert scheduler_peak_contains_wall_time(time(15, 59), "16:00", "19:00") is False


def test_bst_lwt_matches_slot_peak_for_same_instant(monkeypatch: pytest.MonkeyPatch) -> None:
    """UTC 15:30 on a summer day is 16:30 Europe/London (BST): in 16:00–19:00 peak.

    Slot iteration uses ``valid_from.astimezone(London).time()``; LWT uses
    ``utc_instant_in_scheduler_peak`` — same ``scheduler_peak_contains_wall_time`` rule.
    """
    tz_london = "Europe/London"
    peak_start, peak_end = "16:00", "19:00"
    fixed_now = datetime(2026, 6, 15, 15, 30, 0, tzinfo=UTC)

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            if tz is UTC:
                return fixed_now
            return datetime.now(tz)

    monkeypatch.setattr(daikin_mod, "datetime", _FrozenDatetime)

    assert utc_instant_in_scheduler_peak(fixed_now, peak_start, peak_end, tz_london) is True

    # Same instant as Octopus slot start (UTC): list labels peak via local slot start
    vf = datetime.fromisoformat("2026-06-15T15:30:00+00:00")
    slot_local = vf.astimezone(ZoneInfo(tz_london)).time()
    assert scheduler_peak_contains_wall_time(slot_local, peak_start, peak_end) is True

    # LWT: expensive (not cheap) → peak adjustment (-2 clamped) at same frozen instant
    adj = compute_lwt_adjustment(35.0, 10.0, peak_start, peak_end, preheat_boost=3.0)
    assert adj == -2.0


def test_compute_lwt_cheap_overrides_peak() -> None:
    """Below cheap threshold returns preheat regardless of peak."""
    assert compute_lwt_adjustment(5.0, 10.0, "16:00", "19:00", 3.0) == 3.0
