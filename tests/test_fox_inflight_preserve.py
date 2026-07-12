"""Issue #458 follow-up + #693: a mid-slot Fox re-upload must not drop the
in-progress slot's ForceCharge/ForceDischarge to the firmware's SelfUse default.

The plan horizon starts at the NEXT half-hour boundary (Daikin quota integrity),
but a Fox upload replaces the whole schedule — so without preservation the
in-progress slot is left bare and the firmware falls back to SelfUse. During a
negative (paid-import) slot that silently stops force-charge (observed in prod
2026-06-04: upload left 13:34–14:00 BST uncovered).

#693 (2026-07-12): boot recovery's `apply_safe_defaults` persisted a
disabled/empty schedule-state row, starving the bridge — the boot re-plan left
~29 min of SelfUse during a −1.30p slot. The bridge now (1) looks back past a
wipe row saved WITHIN the current slot to the schedule it replaced, and (2) when
no authoritative schedule exists at all, synthesizes a bridge from the plan's
next-boundary group — only for ForceCharge/Backup and only when the current
slot's import price is NEGATIVE (elsewhere SelfUse is the least-bad blind
default).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config as app_config
from src.foxess.models import SchedulerGroup
from src.scheduler import lp_dispatch
from src.scheduler.lp_dispatch import _prepend_inflight_group

TZ = ZoneInfo("Europe/London")

# Most tests anchor "now" at 13:34 BST → current slot 13:30–14:00, boundary 14:00.
NOW = datetime(2026, 6, 4, 13, 34, tzinfo=TZ)


def _g(sh, sm, eh, em, mode="ForceCharge", **kw):
    return SchedulerGroup(start_hour=sh, start_minute=sm, end_hour=eh, end_minute=em,
                          work_mode=mode, **kw)


FC_COVERING_NOW = {
    "startHour": 13, "startMinute": 30, "endHour": 13, "endMinute": 59,
    "workMode": "ForceCharge",
    "extraParam": {"minSocOnGrid": 10, "fdSoc": 100, "fdPwr": 5000},
}


def _enabled(groups_api, uploaded_at=None):
    return {
        "enabled": 1,
        "uploaded_at": (uploaded_at or (NOW - timedelta(hours=1))).isoformat(),
        "groups": groups_api,
    }


def _wipe(uploaded_at):
    """A safe-defaults wipe row (scheduler off, no groups)."""
    return {"enabled": 0, "uploaded_at": uploaded_at.isoformat(), "groups": []}


def _states(monkeypatch, rows):
    monkeypatch.setattr(db, "get_recent_fox_schedule_states", lambda limit=12: rows)


def _price(monkeypatch, value):
    monkeypatch.setattr(lp_dispatch.db, "get_agile_rate_at", lambda ts: value)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(app_config, "FOX_PRESERVE_INFLIGHT_GROUP", True, raising=False)
    monkeypatch.setattr(app_config, "FOX_INFLIGHT_EXTEND_FIRST_GROUP", True, raising=False)


# --- carry bridge (the schedule in force at slot start decides) ---


def test_bridges_inflight_forcecharge(monkeypatch):
    """Re-solve at 13:34 BST: horizon starts 14:00, previous schedule had
    ForceCharge over 13:30–13:59 → a bridge group [13:30,13:59] ForceCharge is
    prepended so the current negative slot keeps charging."""
    _states(monkeypatch, [_enabled([FC_COVERING_NOW])])
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    out = _prepend_inflight_group(groups, now_local=NOW)
    assert len(out) == 2
    bridge = out[0]
    assert bridge.work_mode == "ForceCharge"
    assert (bridge.start_hour, bridge.start_minute) == (13, 30)
    assert (bridge.end_hour, bridge.end_minute) == (13, 59)   # :59 inclusive convention
    assert bridge.fd_soc == 100 and bridge.fd_pwr == 5000


def test_bridges_through_boot_wipe(monkeypatch):
    """#693: a safe-defaults wipe WITHIN the current slot must not starve the
    bridge — the schedule in force at slot start is carried."""
    wipe_at = NOW - timedelta(minutes=2)                     # 13:32, inside 13:30–14:00
    _states(monkeypatch, [_wipe(wipe_at), _enabled([FC_COVERING_NOW])])
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    out = _prepend_inflight_group(groups, now_local=NOW)
    assert len(out) == 2
    assert out[0].work_mode == "ForceCharge"
    assert (out[0].start_hour, out[0].start_minute) == (13, 30)


def test_bridges_through_multiple_wipes(monkeypatch):
    """Several wipe rows within the slot (restart flap) still reach the
    in-force schedule beneath them."""
    _states(monkeypatch, [
        _wipe(NOW - timedelta(minutes=1)),
        _wipe(NOW - timedelta(minutes=2)),
        _wipe(NOW - timedelta(minutes=3)),
        _enabled([FC_COVERING_NOW]),
    ])
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    out = _prepend_inflight_group(groups, now_local=NOW)
    assert len(out) == 2 and out[0].work_mode == "ForceCharge"


def test_boundary_minute_upload_does_not_carry_ended_group(monkeypatch):
    """An upload landing exactly in the boundary minute (13:30) must NOT carry
    a group that ENDED at 13:30 (exclusive :30 end convention)."""
    now = datetime(2026, 6, 4, 13, 30, tzinfo=TZ)
    _states(monkeypatch, [_enabled([
        {"startHour": 13, "startMinute": 0, "endHour": 13, "endMinute": 30,
         "workMode": "ForceCharge", "extraParam": {}},
    ])])
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    assert _prepend_inflight_group(groups, now_local=now) == groups


def test_bare_slot_under_in_force_schedule_stays_bare(monkeypatch):
    """An in-force schedule with no group covering now = deliberate SelfUse gap
    (FOX_SKIP_TRIVIAL_SELFUSE_GROUPS elides SelfUse windows). No bridge, and no
    no-authority fallback either — even at a negative price."""
    _states(monkeypatch, [_enabled([
        {"startHour": 9, "startMinute": 0, "endHour": 9, "endMinute": 59,
         "workMode": "ForceCharge", "extraParam": {}},
    ])])
    _price(monkeypatch, -2.0)
    groups = [_g(14, 0, 14, 30, "ForceCharge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_enabled_empty_groups_row_is_authority(monkeypatch):
    """An enabled row with groups=[] is a deliberate all-SelfUse plan — it
    blocks the no-authority fallback."""
    _states(monkeypatch, [_enabled([])])
    _price(monkeypatch, -2.0)
    groups = [_g(14, 0, 14, 30, "ForceCharge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_no_bridge_when_prev_was_selfuse(monkeypatch):
    """SelfUse is the firmware default — nothing to re-assert, and the in-force
    schedule's explicit SelfUse decision also blocks the fallback."""
    _states(monkeypatch, [_enabled([
        {"startHour": 13, "startMinute": 30, "endHour": 13, "endMinute": 59,
         "workMode": "SelfUse", "extraParam": {"minSocOnGrid": 10}},
    ])])
    _price(monkeypatch, -2.0)
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


# --- no-authority fallback (price-gated bridge from the next-boundary group) ---


def test_stale_wipe_negative_price_bridges_next_boundary_forcecharge(monkeypatch):
    """A wipe OLDER than the slot start means the scheduler was off when the
    slot began — the pre-wipe schedule must NOT be resurrected. With a negative
    current price and ForceCharge at the next boundary, the fallback bridges."""
    wipe_at = NOW - timedelta(hours=3)                       # long before 13:30
    _states(monkeypatch, [_wipe(wipe_at), _enabled([FC_COVERING_NOW])])
    _price(monkeypatch, -1.3)
    groups = [_g(14, 0, 14, 30, "ForceCharge", fd_soc=100, fd_pwr=5000)]
    out = _prepend_inflight_group(groups, now_local=NOW)
    assert len(out) == 2
    assert out[0].work_mode == "ForceCharge"
    assert (out[0].start_hour, out[0].start_minute) == (13, 30)
    assert (out[0].end_hour, out[0].end_minute) == (13, 59)
    assert out[0].fd_soc == 100 and out[0].fd_pwr == 5000    # params copied
    assert out[1] == groups[0]                               # plan untouched


def test_no_authority_positive_price_stays_selfuse(monkeypatch):
    """At a positive price the LP never evaluated the current slot — blind
    charging could land on a peak slot, so SelfUse stays the default."""
    _states(monkeypatch, [])
    _price(monkeypatch, 24.99)
    groups = [_g(14, 0, 14, 30, "ForceCharge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_no_authority_unknown_price_stays_selfuse(monkeypatch):
    _states(monkeypatch, [])
    _price(monkeypatch, None)
    groups = [_g(14, 0, 14, 30, "ForceCharge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_no_authority_never_bridges_forcedischarge(monkeypatch):
    """ForceDischarge value depends on the export rate — never synthesized."""
    _states(monkeypatch, [])
    _price(monkeypatch, -2.0)
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_no_authority_backup_bridges_at_negative_price(monkeypatch):
    _states(monkeypatch, [])
    _price(monkeypatch, -0.5)
    groups = [_g(14, 0, 15, 59, "Backup", fd_soc=15, fd_pwr=3680)]
    out = _prepend_inflight_group(groups, now_local=NOW)
    assert len(out) == 2
    assert out[0].work_mode == "Backup"
    assert (out[0].start_hour, out[0].start_minute) == (13, 30)
    assert (out[0].end_hour, out[0].end_minute) == (13, 59)


def test_no_fallback_when_flag_off(monkeypatch):
    _states(monkeypatch, [])
    _price(monkeypatch, -2.0)
    monkeypatch.setattr(app_config, "FOX_INFLIGHT_EXTEND_FIRST_GROUP", False, raising=False)
    groups = [_g(14, 0, 14, 30, "ForceCharge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_no_fallback_when_plan_starts_later(monkeypatch):
    """First group beyond the next boundary = the plan deliberately chose
    SelfUse for the gap — nothing to bridge from."""
    _states(monkeypatch, [])
    _price(monkeypatch, -2.0)
    groups = [_g(16, 0, 16, 30, "ForceCharge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_walk_exhaustion_treated_as_no_authority(monkeypatch):
    """12 wipe rows all within the current slot (restart flapping) → no verdict;
    treated as no authority so the price-gated fallback still covers the slot."""
    rows = [_wipe(NOW - timedelta(seconds=10 * i)) for i in range(1, 13)]
    _states(monkeypatch, rows)
    _price(monkeypatch, -2.0)
    groups = [_g(14, 0, 14, 30, "ForceCharge")]
    out = _prepend_inflight_group(groups, now_local=NOW)
    assert len(out) == 2 and out[0].work_mode == "ForceCharge"


def test_unparseable_uploaded_at_treated_as_no_authority(monkeypatch):
    _states(monkeypatch, [{"enabled": 0, "uploaded_at": "not-a-date", "groups": []},
                          _enabled([FC_COVERING_NOW])])
    _price(monkeypatch, -2.0)
    groups = [_g(14, 0, 14, 30, "ForceCharge")]
    out = _prepend_inflight_group(groups, now_local=NOW)
    assert len(out) == 2                                     # fallback, not carry
    assert (out[0].start_hour, out[0].start_minute) == (13, 30)
    assert out[0].fd_soc is None                             # NOT carried from FC_COVERING_NOW (fdSoc 100)


# --- midnight (23:30–00:00 slot was a total dead zone pre-#693) ---


def test_midnight_slot_carry_bridge(monkeypatch):
    """In the 23:30–00:00 slot a plan starting 00:00 does not intersect the
    current slot — the carry bridge must fire (dead zone fixed)."""
    now = datetime(2026, 6, 4, 23, 34, tzinfo=TZ)
    _states(monkeypatch, [
        _wipe(now - timedelta(minutes=2)),
        _enabled([{"startHour": 23, "startMinute": 30, "endHour": 23, "endMinute": 59,
                   "workMode": "ForceCharge", "extraParam": {"minSocOnGrid": 10}}],
                 uploaded_at=now - timedelta(hours=1)),
    ])
    groups = [_g(0, 0, 0, 30, "ForceCharge"), _g(2, 0, 3, 59, "ForceCharge")]
    out = _prepend_inflight_group(groups, now_local=now)
    assert len(out) == 3
    assert (out[0].start_hour, out[0].start_minute) == (23, 30)
    assert (out[0].end_hour, out[0].end_minute) == (23, 59)


def test_midnight_slot_no_authority_fallback(monkeypatch):
    """No authority at 23:34: the next boundary is 00:00 (tomorrow) — the
    fallback bridges [23:30,23:59] from the 00:00 group at a negative price."""
    now = datetime(2026, 6, 4, 23, 34, tzinfo=TZ)
    _states(monkeypatch, [])
    _price(monkeypatch, -1.0)
    groups = [_g(0, 0, 0, 30, "ForceCharge", fd_soc=100, fd_pwr=5000)]
    out = _prepend_inflight_group(groups, now_local=now)
    assert len(out) == 2
    assert (out[0].start_hour, out[0].start_minute) == (23, 30)
    assert (out[0].end_hour, out[0].end_minute) == (23, 59)
    assert out[0].work_mode == "ForceCharge"


# --- guards preserved from the #458 / 2026-06-14 wedge fixes ---


def test_no_bridge_when_first_group_already_covers_now(monkeypatch):
    """If a planned group intersects the current slot, nothing is added."""
    _states(monkeypatch, [_enabled([FC_COVERING_NOW])])
    groups = [_g(13, 30, 14, 30, "ForceCharge")]  # starts 13:30 <= 13:34
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_flag_off_disables(monkeypatch):
    monkeypatch.setattr(app_config, "FOX_PRESERVE_INFLIGHT_GROUP", False, raising=False)
    _states(monkeypatch, [_enabled([FC_COVERING_NOW])])
    groups = [_g(14, 0, 14, 30, "ForceDischarge")]
    assert _prepend_inflight_group(groups, now_local=NOW) == groups


def test_no_bridge_at_group_cap(monkeypatch):
    """At the Fox 8-group cap there's no room to prepend."""
    _states(monkeypatch, [_enabled([FC_COVERING_NOW])])
    groups = [_g(14, 0, 14, 30, "ForceCharge")] * 8
    assert len(_prepend_inflight_group(groups, now_local=NOW)) == 8


def test_bridge_does_not_overlap_first_group(monkeypatch):
    """The bridge ends exactly at the next boundary (:59 inclusive), never
    overlapping the first planned group — overlap guard would otherwise refuse
    the whole upload."""
    from src.scheduler.lp_dispatch import _detect_overlapping_groups
    _states(monkeypatch, [_enabled([FC_COVERING_NOW])])
    groups = [_g(14, 0, 14, 30, "ForceDischarge"), _g(14, 30, 15, 59, "ForceCharge")]
    out = _prepend_inflight_group(groups, now_local=NOW)
    assert _detect_overlapping_groups(out) == []
