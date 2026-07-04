"""Regression tests for the 2026-06-14 Fox-upload wedge (~41h open-loop).

Two defects combined to keep the inverter on an obsolete (grid-force-charging)
schedule for ~41 h:

  Defect 1 — the drift comparators (`SchedulerGroup.fingerprint`,
  `state_machine._schedule_signature`) included fdSoc/fdPwr unconditionally, so
  the inverter's STALE fd_* echo on SelfUse/Backup groups (and its vendor maxSoc
  fill) read as perpetual drift → endless re-upload churn. Same vendor-echo
  class fixed for the schedule_diff endpoint in #554, but those two comparators
  were missed.

  Defect 2 — `_prepend_inflight_group` only checked groups[0] for current-slot
  coverage, so a later plan group spanning the live slot let a stale ForceCharge
  bridge OVERLAP it; the overlap guard then refused the WHOLE upload, leaving
  the stale schedule live.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.foxess.models import SchedulerGroup
import src.state_machine as sm
import src.scheduler.lp_dispatch as lpd


# ── Defect 1: mode-aware fingerprints ─────────────────────────────────────────

def test_fingerprint_ignores_stale_fd_echo_on_selfuse():
    """A live SelfUse group with echoed fdSoc/fdPwr == the SelfUse we uploaded
    (which carried no fd_*). This is the equality that was perpetually false."""
    live = SchedulerGroup(9, 0, 9, 30, "SelfUse", min_soc_on_grid=100,
                          fd_soc=31.0, fd_pwr=3800.0, max_soc=100.0)
    uploaded = SchedulerGroup(9, 0, 9, 30, "SelfUse", min_soc_on_grid=100, max_soc=100)
    assert live.fingerprint() == uploaded.fingerprint()


def test_fingerprint_ignores_stale_fd_echo_on_backup_hold():
    """New default negative-hold shape (2026-07-04): uploaded Backup carries no
    fd_* and maxSoc=None; the inverter echoes stale fd_* + vendor maxSoc=100.
    Must compare equal or the heartbeat re-uploads on every tick of every
    negative-price day."""
    live = SchedulerGroup(13, 0, 14, 30, "Backup", min_soc_on_grid=10,
                          fd_soc=91.0, fd_pwr=2850.0, max_soc=100.0)
    uploaded = SchedulerGroup(13, 0, 14, 30, "Backup", min_soc_on_grid=10, max_soc=None)
    assert live.fingerprint() == uploaded.fingerprint()


def test_fingerprint_absent_maxsoc_equals_vendor_default_100():
    live = SchedulerGroup(8, 0, 9, 0, "SelfUse", min_soc_on_grid=100, max_soc=100.0)
    uploaded = SchedulerGroup(8, 0, 9, 0, "SelfUse", min_soc_on_grid=100, max_soc=None)
    assert live.fingerprint() == uploaded.fingerprint()


@pytest.mark.parametrize("mode", ["ForceCharge", "ForceDischarge"])
def test_fingerprint_still_tracks_fd_change_on_active_modes(mode):
    """fdSoc IS meaningful for ForceCharge and ForceDischarge — a real change
    must still register (so a re-solve's new target gets uploaded)."""
    a = SchedulerGroup(11, 0, 11, 30, mode, min_soc_on_grid=10, fd_soc=31, fd_pwr=3800)
    b = SchedulerGroup(11, 0, 11, 30, mode, min_soc_on_grid=10, fd_soc=100, fd_pwr=3800)
    assert a.fingerprint() != b.fingerprint()


def test_fingerprint_int_float_equivalent():
    assert (SchedulerGroup(0, 0, 1, 0, "ForceCharge", fd_soc=31, fd_pwr=3800).fingerprint()
            == SchedulerGroup(0, 0, 1, 0, "ForceCharge", fd_soc=31.0, fd_pwr=3800.0).fingerprint())


def test_fingerprint_ignores_echoed_import_export_limits():
    """import/export limits are never LP-set, so an echoed value on read-back
    must not register as drift (review HIGH on #561 — they were initially
    included in the fingerprint and would re-open the churn)."""
    live = SchedulerGroup(9, 0, 9, 30, "SelfUse", min_soc_on_grid=100, max_soc=100,
                          import_limit=8000, export_limit=3680)
    uploaded = SchedulerGroup(9, 0, 9, 30, "SelfUse", min_soc_on_grid=100, max_soc=100)
    assert live.fingerprint() == uploaded.fingerprint()


def test_schedule_signature_live_attr_equals_stored_dict():
    """The heartbeat compares a live read (attribute objects, with fd_* echo)
    against the stored plan (dicts, no fd_*). They must match for an unchanged
    schedule — the comparison that drove the ~41h re-upload churn."""
    live = [SchedulerGroup(9, 0, 9, 30, "SelfUse", min_soc_on_grid=100,
                           fd_soc=31.0, fd_pwr=3800.0, max_soc=100.0)]
    stored = [{"startHour": 9, "startMinute": 0, "endHour": 9, "endMinute": 30,
               "workMode": "SelfUse", "extraParam": {"minSocOnGrid": 100, "maxSoc": 100}}]
    assert sm._schedule_signature(live) == sm._schedule_signature(stored)


def test_schedule_signature_still_detects_real_forcecharge_change():
    a = [{"startHour": 11, "startMinute": 0, "endHour": 11, "endMinute": 30,
          "workMode": "ForceCharge", "extraParam": {"minSocOnGrid": 10, "fdSoc": 31, "fdPwr": 3800}}]
    b = [SchedulerGroup(11, 0, 11, 30, "ForceCharge", min_soc_on_grid=10, fd_soc=100, fd_pwr=3800)]
    assert sm._schedule_signature(a) != sm._schedule_signature(b)


# ── Defect 2: in-flight bridge must not wedge the upload ───────────────────────

_NOW = datetime(2026, 6, 14, 12, 15, tzinfo=ZoneInfo("Europe/London"))


def _stale_forcecharge_prev(monkeypatch):
    """The inverter's live schedule has a ForceCharge active across 'now'."""
    monkeypatch.setattr(lpd.db, "get_latest_fox_schedule_state", lambda: {
        "groups": [{"startHour": 12, "startMinute": 0, "endHour": 13, "endMinute": 30,
                    "workMode": "ForceCharge",
                    "extraParam": {"minSocOnGrid": 10, "fdSoc": 100, "fdPwr": 3133}}],
    })


def test_no_bridge_when_a_later_plan_group_covers_now(monkeypatch):
    """Plan's SelfUse spans the live slot → no bridge (the plan owns the slot),
    so no overlap and the clean plan can upload. This is the wedge fix."""
    _stale_forcecharge_prev(monkeypatch)
    plan = [SchedulerGroup(11, 30, 15, 59, "SelfUse", min_soc_on_grid=100, max_soc=100)]
    out = lpd._prepend_inflight_group(list(plan), now_local=_NOW)
    assert len(out) == len(plan)               # no bridge added
    assert lpd._detect_overlapping_groups(out) == []


def test_no_bridge_when_a_LATER_indexed_group_covers_now(monkeypatch):
    """The real incident shape: groups[0] starts AFTER now (so the old
    groups[0]-only guard would add a bridge), but a LATER group in the list
    covers the live slot. The all-groups guard must still suppress the bridge.
    (This is the case that FAILS against main — the single-group test above
    passes on main and doesn't actually guard the fix.)"""
    _stale_forcecharge_prev(monkeypatch)
    plan = [
        SchedulerGroup(13, 0, 15, 59, "Backup", min_soc_on_grid=10, max_soc=10),   # groups[0] — after now
        SchedulerGroup(11, 30, 12, 59, "SelfUse", min_soc_on_grid=100, max_soc=100),  # covers now (12:15)
    ]
    out = lpd._prepend_inflight_group(list(plan), now_local=_NOW)
    assert len(out) == len(plan)               # no bridge despite groups[0] being after now
    assert lpd._detect_overlapping_groups(out) == []


def test_bridge_still_added_for_a_genuine_gap(monkeypatch):
    """When NO plan group covers the live slot (horizon starts at the next
    boundary), the bridge is still added — its legitimate #458 purpose."""
    _stale_forcecharge_prev(monkeypatch)
    plan = [SchedulerGroup(13, 0, 15, 59, "SelfUse", min_soc_on_grid=100, max_soc=100)]
    out = lpd._prepend_inflight_group(list(plan), now_local=_NOW)
    assert len(out) == len(plan) + 1           # bridge prepended
    assert out[0].work_mode == "ForceCharge"
    assert lpd._detect_overlapping_groups(out) == []


def test_upload_drops_bridge_on_overlap_instead_of_refusing(monkeypatch):
    """If a bridge somehow still overlaps the plan, the upload drops the bridge
    and pushes the plan — never refuses and leaves the stale schedule live."""
    captured = {}

    class _FakeFox:
        api_key = "k"
        def set_scheduler_v3(self, groups, is_default=False):
            captured["groups"] = groups
        def warn_if_scheduler_v3_mismatch(self, groups): ...
        def set_scheduler_flag(self, on): ...

    monkeypatch.setattr(lpd.config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(lpd.db, "save_fox_schedule_state", lambda *a, **k: None)
    # Force _prepend to return an overlapping (bridge, plan) pair.
    plan = [SchedulerGroup(11, 30, 12, 30, "SelfUse", min_soc_on_grid=100, max_soc=100)]
    bridge = SchedulerGroup(12, 0, 12, 29, "ForceCharge", min_soc_on_grid=10, fd_soc=100, fd_pwr=3133)
    monkeypatch.setattr(lpd, "_prepend_inflight_group", lambda g: [bridge] + g)

    ok = lpd.upload_fox_if_operational(_FakeFox(), plan)
    assert ok is True
    # The bridge was dropped; the plan (no overlap) was uploaded.
    assert captured["groups"] == plan
