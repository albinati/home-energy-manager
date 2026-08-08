"""The two observability failures behind the #777 outage.

#778 — the v3 scheduler read silently omits every ``enable: 0`` slot, so a
device holding 8 reported 1. Three consumers compared against that partial
view without anyone choosing it.

#779 — ``schedule_diff`` compared the live hardware against the last
*successful* upload. During a write outage the hardware still matches that by
definition, so the check reported ``any_drift: false, diff_count: 0`` for the
whole 37 h while the inverter ran a two-day-old plan.
"""

from __future__ import annotations

import pytest

from src import db
from src.foxess.client import _parse_scheduler_v3_result

# Shape of a real v1/v2 read taken off the affected prod device on 2026-08-08:
# one active group plus seven leftovers from earlier uploads.
V1_READ = {
    "enable": 1,
    "groups": [
        {"enable": 1, "startHour": 2, "startMinute": 0, "endHour": 3, "endMinute": 59,
         "workMode": "Backup", "extraParam": {"minSocOnGrid": 10, "maxSoc": 10}},
        {"enable": 0, "startHour": 10, "startMinute": 30, "endHour": 11, "endMinute": 59,
         "workMode": "Backup", "extraParam": {"minSocOnGrid": 10, "maxSoc": 100}},
        {"enable": 0, "startHour": 12, "startMinute": 0, "endHour": 12, "endMinute": 30,
         "workMode": "ForceCharge", "extraParam": {"minSocOnGrid": 10, "fdSoc": 33}},
    ],
}

# The v3 read of the SAME device at the SAME instant: leftovers absent, and no
# enable flag on what survives.
V3_READ = {
    "enable": 1,
    "maxGroupCount": 96,
    "groups": [
        {"startHour": 2, "startMinute": 0, "endHour": 3, "endMinute": 59,
         "workMode": "Backup", "extraParam": {"minSocOnGrid": 10, "maxSoc": 10}},
    ],
}


# ---------------------------------------------------------------------------
# #778
# ---------------------------------------------------------------------------

def test_disabled_slots_are_parsed_but_kept_out_of_groups():
    state = _parse_scheduler_v3_result(V1_READ)

    # `groups` must keep meaning "what the inverter is running" — every
    # existing consumer was written against the v3 read and assumes that.
    assert len(state.groups) == 1
    assert state.groups[0].start_hour == 2

    # ...while the residue stops being invisible.
    assert len(state.all_groups) == 3
    assert len(state.disabled_groups) == 2
    assert {g.start_hour for g in state.disabled_groups} == {10, 12}


def test_v3_read_still_parses_with_every_slot_active():
    """v3 carries no enable flag; absent must mean active, not disabled."""
    state = _parse_scheduler_v3_result(V3_READ)

    assert len(state.groups) == 1
    assert state.disabled_groups == []
    assert state.all_groups == state.groups


def test_active_view_agrees_across_read_versions():
    """The whole point: switching the read route must not move `groups`.

    Both payloads describe the same device in the same state, so the active
    view has to be identical — otherwise the read version silently changes
    what every consumer compares against.
    """
    v1 = _parse_scheduler_v3_result(V1_READ)
    v3 = _parse_scheduler_v3_result(V3_READ)

    assert [g.fingerprint() for g in v1.groups] == [g.fingerprint() for g in v3.groups]


def test_enable_flag_is_not_part_of_the_fingerprint():
    """`enable` partitions slots; it is not part of what a schedule says."""
    v1 = _parse_scheduler_v3_result(V1_READ)
    v3 = _parse_scheduler_v3_result(V3_READ)

    assert v1.groups[0].enable == 1
    assert v3.groups[0].enable == 1
    assert v1.groups[0].fingerprint() == v3.groups[0].fingerprint()


# ---------------------------------------------------------------------------
# #779
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM fox_schedule_intent")
        conn.execute("DELETE FROM fox_schedule_state")
        conn.commit()
    finally:
        conn.close()


def _group(start_hour: int, mode: str = "Backup") -> dict:
    return {
        "startHour": start_hour, "startMinute": 0, "endHour": start_hour + 1,
        "endMinute": 59, "workMode": mode,
        "extraParam": {"minSocOnGrid": 10, "maxSoc": 10},
    }


def test_failed_upload_is_recorded_as_intent():
    db.save_fox_schedule_intent([_group(11, "ForceCharge")], upload_ok=False,
                                error_msg="API error 41200: Failed to load data")

    latest = db.get_latest_fox_schedule_intent()
    assert latest is not None
    assert latest["upload_ok"] == 0
    assert "41200" in latest["error_msg"]
    assert latest["groups"][0]["workMode"] == "ForceCharge"


def test_intent_diverges_from_the_last_landed_upload_during_an_outage():
    """The #777 incident, reduced.

    A schedule lands, then every later upload fails. The last *successful*
    upload still matches the hardware — which is why the old comparison stayed
    green — but the plan the LP wants has moved on.
    """
    landed = [_group(2)]
    db.save_fox_schedule_state(landed, enabled=True)
    db.save_fox_schedule_intent(landed, upload_ok=True)

    wanted = [_group(2), _group(11, "ForceCharge")]
    for _ in range(3):
        db.save_fox_schedule_intent(wanted, upload_ok=False, error_msg="API error 41200")

    last_landed = db.get_latest_fox_schedule_state()
    latest_intent = db.get_latest_fox_schedule_intent()

    # The hardware (== last landed upload) trivially agrees with itself...
    assert len(last_landed["groups"]) == 1
    # ...while the current plan wants a ForceCharge that never reached it.
    assert len(latest_intent["groups"]) == 2
    assert latest_intent["upload_ok"] == 0
    assert last_landed["groups"] != latest_intent["groups"], (
        "the baseline the drift check uses must be able to disagree with the "
        "hardware during a write outage — otherwise it cannot fire"
    )


def test_successful_upload_keeps_both_records_in_step():
    groups = [_group(2)]
    db.save_fox_schedule_state(groups, enabled=True)
    db.save_fox_schedule_intent(groups, upload_ok=True)

    assert db.get_latest_fox_schedule_intent()["upload_ok"] == 1
    assert (
        db.get_latest_fox_schedule_state()["groups"]
        == db.get_latest_fox_schedule_intent()["groups"]
    )
