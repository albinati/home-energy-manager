"""Issue #458 follow-up: a mid-slot Fox re-upload must not drop the in-progress
slot's ForceCharge/ForceDischarge to the firmware's SelfUse default.

The plan horizon starts at the NEXT half-hour boundary (Daikin quota integrity),
but a Fox upload replaces the whole schedule — so without preservation the
in-progress slot is left bare and the firmware falls back to SelfUse. During a
negative (paid-import) slot that silently stops force-charge (observed in prod
2026-06-04: upload left 13:34–14:00 BST uncovered)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config as app_config
from src.foxess.models import SchedulerGroup
from src.scheduler.lp_dispatch import _prepend_inflight_group

TZ = ZoneInfo("Europe/London")


def _g(sh, sm, eh, em, mode="ForceCharge", **kw):
    return SchedulerGroup(start_hour=sh, start_minute=sm, end_hour=eh, end_minute=em,
                          work_mode=mode, **kw)


def _prev(groups_api):
    return {"groups": groups_api}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(app_config, "FOX_PRESERVE_INFLIGHT_GROUP", True, raising=False)


def test_bridges_inflight_forcecharge(monkeypatch):
    """Re-solve at 13:34 BST: horizon starts 14:00, previous schedule had
    ForceCharge over 13:30–13:59 → a bridge group [13:30,13:59] ForceCharge is
    prepended so the current negative slot keeps charging."""
    now = datetime(2026, 6, 4, 13, 34, tzinfo=TZ)
    prev = _prev([
        {"startHour": 13, "startMinute": 30, "endHour": 13, "endMinute": 59,
         "workMode": "ForceCharge",
         "extraParam": {"minSocOnGrid": 10, "fdSoc": 100, "fdPwr": 5000}},
    ])
    monkeypatch.setattr(db, "get_latest_fox_schedule_state", lambda: prev)
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    out = _prepend_inflight_group(groups, now_local=now)
    assert len(out) == 2
    bridge = out[0]
    assert bridge.work_mode == "ForceCharge"
    assert (bridge.start_hour, bridge.start_minute) == (13, 30)
    assert (bridge.end_hour, bridge.end_minute) == (13, 59)   # :59 inclusive convention
    assert bridge.fd_soc == 100 and bridge.fd_pwr == 5000


def test_no_bridge_when_first_group_already_covers_now(monkeypatch):
    """If the first planned group already starts at/before now, nothing dropped."""
    now = datetime(2026, 6, 4, 13, 34, tzinfo=TZ)
    monkeypatch.setattr(db, "get_latest_fox_schedule_state",
                        lambda: _prev([{"startHour": 13, "startMinute": 0, "endHour": 13,
                                        "endMinute": 59, "workMode": "ForceCharge",
                                        "extraParam": {}}]))
    groups = [_g(13, 30, 14, 30, "ForceCharge")]  # starts 13:30 <= 13:34
    assert _prepend_inflight_group(groups, now_local=now) == groups


def test_no_bridge_when_prev_was_selfuse(monkeypatch):
    """SelfUse is the firmware default — nothing to re-assert."""
    now = datetime(2026, 6, 4, 13, 34, tzinfo=TZ)
    monkeypatch.setattr(db, "get_latest_fox_schedule_state",
                        lambda: _prev([{"startHour": 13, "startMinute": 30, "endHour": 13,
                                        "endMinute": 59, "workMode": "SelfUse",
                                        "extraParam": {"minSocOnGrid": 10}}]))
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    assert _prepend_inflight_group(groups, now_local=now) == groups


def test_no_bridge_when_no_prev_schedule(monkeypatch):
    now = datetime(2026, 6, 4, 13, 34, tzinfo=TZ)
    monkeypatch.setattr(db, "get_latest_fox_schedule_state", lambda: None)
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    assert _prepend_inflight_group(groups, now_local=now) == groups


def test_flag_off_disables(monkeypatch):
    monkeypatch.setattr(app_config, "FOX_PRESERVE_INFLIGHT_GROUP", False, raising=False)
    now = datetime(2026, 6, 4, 13, 34, tzinfo=TZ)
    monkeypatch.setattr(db, "get_latest_fox_schedule_state",
                        lambda: _prev([{"startHour": 13, "startMinute": 30, "endHour": 13,
                                        "endMinute": 59, "workMode": "ForceCharge",
                                        "extraParam": {}}]))
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    assert _prepend_inflight_group(groups, now_local=now) == groups


def test_no_bridge_at_group_cap(monkeypatch):
    """At the Fox 8-group cap there's no room to prepend."""
    now = datetime(2026, 6, 4, 13, 34, tzinfo=TZ)
    monkeypatch.setattr(db, "get_latest_fox_schedule_state",
                        lambda: _prev([{"startHour": 13, "startMinute": 30, "endHour": 13,
                                        "endMinute": 59, "workMode": "ForceCharge",
                                        "extraParam": {}}]))
    groups = [_g(14, 0, 14, 30, "ForceCharge")] * 8
    assert len(_prepend_inflight_group(groups, now_local=now)) == 8


def test_bridge_does_not_overlap_first_group(monkeypatch):
    """The bridge ends exactly at the next boundary (:59 inclusive), never
    overlapping the first planned group — overlap guard would otherwise refuse
    the whole upload."""
    from src.scheduler.lp_dispatch import _detect_overlapping_groups
    now = datetime(2026, 6, 4, 13, 34, tzinfo=TZ)
    monkeypatch.setattr(db, "get_latest_fox_schedule_state",
                        lambda: _prev([{"startHour": 13, "startMinute": 30, "endHour": 13,
                                        "endMinute": 59, "workMode": "ForceCharge",
                                        "extraParam": {"minSocOnGrid": 10}}]))
    groups = [_g(14, 0, 14, 30, "ForceDischarge"), _g(14, 30, 15, 59, "ForceCharge")]
    out = _prepend_inflight_group(groups, now_local=now)
    assert _detect_overlapping_groups(out) == []
