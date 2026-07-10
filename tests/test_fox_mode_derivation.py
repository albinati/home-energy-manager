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


@pytest.mark.parametrize("order", ["outgoing_first", "incoming_first"])
def test_shared_half_hour_boundary_prefers_incoming_group(monkeypatch, order):
    """PR #672 review finding 1: only :00 ends get the :59 adjustment in
    _merge_fox_groups — a half-hour end is stored as :30, back-to-back with
    the next group's :30 start. At exactly HH:30 the inverter has switched to
    the incoming group, so the derivation must prefer the group STARTING at
    now over the one merely ending on it — regardless of list order."""
    outgoing = _g(13, 0, 13, 30, "ForceCharge")     # :30 end stored as-is
    incoming = _g(13, 30, 13, 59, "ForceDischarge")
    groups = [outgoing, incoming] if order == "outgoing_first" else [incoming, outgoing]
    monkeypatch.setattr(db, "get_latest_fox_schedule_state", lambda: _state(groups))
    now = datetime(2026, 7, 8, 13, 30, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:ForceDischarge"
    # One minute earlier the outgoing group still owns the slot.
    now = datetime(2026, 7, 8, 13, 29, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:ForceCharge"


def test_shared_boundary_without_incoming_group_keeps_outgoing(monkeypatch):
    """A :30 end with NOTHING starting at :30 — the spanning group is all we
    have; keep its mode rather than jump to SelfUse a minute early."""
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: _state([_g(13, 0, 13, 30, "ForceCharge")]),
    )
    now = datetime(2026, 7, 8, 13, 30, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:ForceCharge"


# ---------------------------------------------------------------------------
# PR #672 review finding 2: the REAL scheduler-off events (survival mode,
# apply_safe_defaults) must persist the disabled state so the derivation stops
# walking stale groups and reports schedule:SelfUse for those windows.
# ---------------------------------------------------------------------------


class _RecordingFox:
    api_key = "test-key"

    def __init__(self) -> None:
        self.set_scheduler_flag_calls: list[bool] = []
        self.set_work_mode_calls: list[str] = []
        self.set_min_soc_calls: list[int] = []

    def set_scheduler_flag(self, flag: bool) -> None:
        self.set_scheduler_flag_calls.append(flag)

    def set_work_mode(self, mode: str) -> None:
        self.set_work_mode_calls.append(mode)

    def set_min_soc(self, value: int) -> None:
        self.set_min_soc_calls.append(int(value))


@pytest.fixture()
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config.DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", False)
    db.init_db()


def test_apply_safe_defaults_persists_scheduler_off(monkeypatch, _tmp_db):
    """apply_safe_defaults disables the hardware scheduler flag — the stale
    uploaded groups are no longer in force, so the derivation must report
    schedule:SelfUse, not e.g. schedule:ForceCharge from the old plan."""
    from src.state_machine import apply_safe_defaults

    db.save_fox_schedule_state([_g(0, 0, 23, 59, "ForceCharge")], enabled=True)
    now = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now) == "schedule:ForceCharge"

    fox = _RecordingFox()
    apply_safe_defaults(fox, daikin=None, trigger="test")

    assert fox.set_scheduler_flag_calls == [False]
    state = db.get_latest_fox_schedule_state()
    assert state is not None and not state["enabled"]
    assert derive_fox_mode_from_schedule(now) == "schedule:SelfUse"


def test_survival_mode_persists_scheduler_off(monkeypatch, _tmp_db):
    """Survival mode (24h without Agile rates) disables the scheduler flag and
    locks Self Use — the derivation must follow."""
    from datetime import UTC, timedelta

    from src.scheduler import octopus_fetch as of

    db.save_fox_schedule_state([_g(0, 0, 23, 59, "ForceDischarge")], enabled=True)
    now_local = datetime(2026, 7, 8, 13, 34, tzinfo=TZ)
    assert derive_fox_mode_from_schedule(now_local) == "schedule:ForceDischarge"

    monkeypatch.setattr(of, "notify_critical", lambda *a, **k: None)
    fox = _RecordingFox()
    now_utc = datetime(2026, 7, 8, 12, 34, tzinfo=UTC)
    streak_start = (now_utc - timedelta(hours=25)).isoformat()
    of._maybe_survival_mode(fox, 5, streak_start, now_utc)

    assert fox.set_scheduler_flag_calls == [False]
    assert fox.set_work_mode_calls == ["Self Use"]
    state = db.get_latest_fox_schedule_state()
    assert state is not None and not state["enabled"]
    assert derive_fox_mode_from_schedule(now_local) == "schedule:SelfUse"
