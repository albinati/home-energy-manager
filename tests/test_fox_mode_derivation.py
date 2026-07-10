"""#669 — heartbeat fox_mode observability.

The Fox Open API realtime query (``/device/real/query``) does not return a
``workMode`` variable for the H1 series, so ``RealTimeData.work_mode`` parsed
to ``"unknown"`` on 100 % of prod heartbeats. The heartbeat now falls back to
``derive_fox_mode_from_schedule`` — a zero-quota local derivation from the
last uploaded Scheduler V3 state (``fox_schedule_state``), labelled
``schedule:<WorkMode>`` so the column stays honest about its source.

All tests pin ``now_local`` to fixed datetimes — no date-relative flakes.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.foxess.service import derive_fox_mode_from_schedule

TZ = ZoneInfo("Europe/London")


def _state(groups, enabled=True):
    return {"groups": groups, "enabled": 1 if enabled else 0}


def _g(sh, sm, eh, em, mode="ForceCharge"):
    return {
        "startHour": sh, "startMinute": sm,
        "endHour": eh, "endMinute": em,
        "workMode": mode,
        "extraParam": {"minSocOnGrid": 10},
    }


def test_group_covering_now(monkeypatch):
    """13:34 inside a 13:30–13:59 ForceCharge group → schedule:ForceCharge."""
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: _state([_g(13, 30, 13, 59, "ForceCharge")]),
    )
    now = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:ForceCharge"


def test_end_minute_is_inclusive(monkeypatch):
    """The :59 end minute itself is covered (same convention as the
    in-flight bridge comparator in lp_dispatch)."""
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: _state([_g(13, 30, 13, 59, "ForceDischarge")]),
    )
    now = datetime(2026, 7, 8, 13, 59, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:ForceDischarge"
    # ...and the next minute is not.
    now = datetime(2026, 7, 8, 14, 0, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:SelfUse"


def test_midnight_crossing_group(monkeypatch):
    """A wrapped 23:00–01:59 group covers both 23:30 and 00:30."""
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: _state([_g(23, 0, 1, 59, "Backup")]),
    )
    assert derive_fox_mode_from_schedule(
        datetime(2026, 7, 8, 23, 30, tzinfo=TZ)) == "schedule:Backup"
    assert derive_fox_mode_from_schedule(
        datetime(2026, 7, 9, 0, 30, tzinfo=TZ)) == "schedule:Backup"
    # 12:00 is outside the wrap.
    assert derive_fox_mode_from_schedule(
        datetime(2026, 7, 9, 12, 0, tzinfo=TZ)) == "schedule:SelfUse"


def test_no_group_covering_now_defaults_selfuse(monkeypatch):
    """Schedule exists but no group applies → the inverter's global default."""
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: _state([_g(2, 0, 4, 59, "ForceCharge"),
                        _g(17, 0, 18, 59, "ForceDischarge")]),
    )
    now = datetime(2026, 7, 8, 10, 15, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:SelfUse"


def test_second_group_matches(monkeypatch):
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: _state([_g(2, 0, 4, 59, "ForceCharge"),
                        _g(17, 0, 18, 59, "ForceDischarge")]),
    )
    now = datetime(2026, 7, 8, 17, 45, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:ForceDischarge"


def test_no_state_at_all_is_unknown(monkeypatch):
    """Genuine failure to determine — nothing was ever uploaded."""
    monkeypatch.setattr(db, "get_latest_fox_schedule_state", lambda: None)
    now = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "unknown"


def test_db_read_failure_is_unknown(monkeypatch):
    def _boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(db, "get_latest_fox_schedule_state", _boom)
    now = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "unknown"


def test_scheduler_disabled_is_selfuse(monkeypatch):
    """Scheduler flag off → groups not in force → firmware global default."""
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: _state([_g(13, 30, 13, 59, "ForceCharge")], enabled=False),
    )
    now = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:SelfUse"


def test_malformed_group_skipped(monkeypatch):
    """A group missing HH:MM keys is skipped, not fatal; later groups still match."""
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: _state([{"workMode": "ForceCharge"},  # no times
                        _g(13, 0, 13, 59, "ForceDischarge")]),
    )
    now = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:ForceDischarge"


def test_empty_groups_list_is_selfuse(monkeypatch):
    """A valid state row with zero groups: nothing applies → default SelfUse."""
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state", lambda: _state([]),
    )
    now = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:SelfUse"


@pytest.mark.parametrize("blank_mode", ["", None, "   "])
def test_blank_workmode_in_covering_group_falls_through(monkeypatch, blank_mode):
    """A covering group with a blank workMode can't be trusted — fall through
    to the SelfUse default rather than emit 'schedule:'."""
    g = _g(13, 30, 13, 59, "ForceCharge")
    g["workMode"] = blank_mode
    monkeypatch.setattr(db, "get_latest_fox_schedule_state", lambda: _state([g]))
    now = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:SelfUse"
