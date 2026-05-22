"""Range-keyed action clear: rolling 24 h replans must clear the exact window,
preserving in-flight pending rows and the restore partners of active rows.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def test_removes_only_rows_whose_start_falls_in_range(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    before = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
    in_range = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    after = datetime(2026, 6, 2, 18, 0, tzinfo=UTC)

    rid_b = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(before), end_time=_iso(before + timedelta(minutes=30)),
        device="daikin", action_type="pre_heat", params={}, status="pending",
    )
    rid_in = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(in_range), end_time=_iso(in_range + timedelta(minutes=30)),
        device="daikin", action_type="pre_heat", params={}, status="pending",
    )
    rid_a = db.upsert_action(
        plan_date="2026-06-02",
        start_time=_iso(after), end_time=_iso(after + timedelta(minutes=30)),
        device="daikin", action_type="pre_heat", params={}, status="pending",
    )

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 12, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 12, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(rid_b) is not None
    assert db.get_action_by_id(rid_in) is None
    assert db.get_action_by_id(rid_a) is not None


def test_preserves_in_flight_pending_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pending pair with start ≤ now < end must survive an overlapping clear."""
    now = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t0 = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)
    t_restore_end = t1 + timedelta(minutes=1)

    rid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t1), end_time=_iso(t_restore_end),
        device="daikin", action_type="restore", params={}, status="pending",
    )
    aid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t0), end_time=_iso(t1),
        device="daikin", action_type="shutdown", params={}, status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 15, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 15, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(aid) is not None
    assert db.get_action_by_id(rid) is not None


def test_preserves_restore_of_active_row_outside_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Active row's restore partner must survive even when the active row itself
    is outside the clear range (started earlier)."""
    now = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t0 = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)  # active main — BEFORE clear range
    t1 = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)  # restore start — INSIDE clear range

    rid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t1), end_time=_iso(t1 + timedelta(minutes=1)),
        device="daikin", action_type="restore", params={}, status="pending",
    )
    aid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t0), end_time=_iso(t1),
        device="daikin", action_type="shutdown", params={}, status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)
    db.mark_action(aid, "active")

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 17, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 17, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(rid) is not None, "restore of active row must be preserved"
    assert db.get_action_by_id(aid) is not None  # active outside range anyway, but sanity


def test_removes_purely_future_pending_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pending pair with both rows in the future (inside the clear range) is removed."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t0 = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 1, 21, 0, tzinfo=UTC)

    rid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t1), end_time=_iso(t1 + timedelta(minutes=1)),
        device="daikin", action_type="restore", params={}, status="pending",
    )
    aid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t0), end_time=_iso(t1),
        device="daikin", action_type="pre_heat", params={}, status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 12, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 12, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(aid) is None
    assert db.get_action_by_id(rid) is None


def test_382_preserves_imminent_restore_when_parent_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #382: real incident replay.

    A 16:30→18:00 shutdown row whose ``set_tank_power(False)`` write FAILED
    at 16:36 with READ_ONLY_CHARACTERISTIC (user had already turned the
    tank off manually). At 17:55 the MPC ran ``clear_actions_in_range``;
    the existing "active parent → preserve restore" rule didn't apply
    because the parent was ``failed``, not ``active``, and the restore's
    own window hadn't started yet. The new lead-minutes rule preserves
    any pending restore within RESTORE_PRESERVE_LEAD_MINUTES of now.
    """
    now = datetime(2026, 6, 1, 17, 55, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t_main_start = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)
    t_restore_start = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)  # 5 min from now

    rid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_restore_start),
        end_time=_iso(t_restore_start + timedelta(minutes=5)),
        device="daikin", action_type="restore",
        params={"tank_power": True, "tank_temp": 45}, status="pending",
    )
    aid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_main_start), end_time=_iso(t_restore_start),
        device="daikin", action_type="shutdown",
        params={"tank_power": False}, status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)
    db.mark_action(aid, "failed", error_msg="[read_only] HTTP 400: READ_ONLY_CHARACTERISTIC")

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 17, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 17, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(rid) is not None, (
        "imminent restore must survive even when parent shutdown FAILED"
    )


def test_382_preserves_imminent_restore_when_parent_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as above but with parent ``completed`` (the post-#387 world
    where pre-fire idempotency marks an already-off shutdown as completed
    without error). Restore is still imminent and must survive."""
    now = datetime(2026, 6, 1, 17, 55, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t_main_start = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)
    t_restore_start = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)

    rid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_restore_start),
        end_time=_iso(t_restore_start + timedelta(minutes=5)),
        device="daikin", action_type="restore",
        params={"tank_power": True, "tank_temp": 45}, status="pending",
    )
    aid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_main_start), end_time=_iso(t_restore_start),
        device="daikin", action_type="shutdown",
        params={"tank_power": False}, status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)
    db.mark_action(aid, "completed")

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 17, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 17, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(rid) is not None


def test_382_does_not_preserve_far_future_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore well outside the lead window IS cleared — the next LP solve has
    time to re-emit one. Only imminent restores get the bare-life guarantee."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t_restore_start = datetime(2026, 6, 1, 21, 0, tzinfo=UTC)  # 9h ahead
    rid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_restore_start),
        end_time=_iso(t_restore_start + timedelta(minutes=5)),
        device="daikin", action_type="restore",
        params={"tank_power": True, "tank_temp": 45}, status="pending",
    )

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 12, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 12, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(rid) is None


def test_382_does_not_preserve_imminent_non_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is restore-only. A pending shutdown 5 min from now is still
    deletable — the LP can re-emit a fresh shutdown if it wants one."""
    now = datetime(2026, 6, 1, 17, 55, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t_start = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    aid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_start), end_time=_iso(t_start + timedelta(hours=1)),
        device="daikin", action_type="shutdown",
        params={"tank_power": False}, status="pending",
    )

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 17, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 17, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(aid) is None


def test_382_for_date_variant_also_preserves_imminent_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan-date-keyed clear path (clear_actions_for_date) is used by the
    older non-rolling planner. The same lead-minutes rule applies there too —
    the bug fix needs to cover both call sites."""
    now = datetime(2026, 6, 1, 17, 55, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t_main_start = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)
    t_restore_start = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)

    rid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_restore_start),
        end_time=_iso(t_restore_start + timedelta(minutes=5)),
        device="daikin", action_type="restore",
        params={"tank_power": True, "tank_temp": 45}, status="pending",
    )
    aid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_main_start), end_time=_iso(t_restore_start),
        device="daikin", action_type="shutdown",
        params={"tank_power": False}, status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)
    db.mark_action(aid, "failed", error_msg="READ_ONLY")

    db.clear_actions_for_date("2026-06-01", device="daikin")

    assert db.get_action_by_id(rid) is not None


def test_382_disabled_when_lead_minutes_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting RESTORE_PRESERVE_LEAD_MINUTES=0 disables the new rule (escape
    hatch for instant rollback if it misbehaves)."""
    from src.config import config as _cfg
    monkeypatch.setattr(_cfg, "RESTORE_PRESERVE_LEAD_MINUTES", 0.0, raising=False)

    now = datetime(2026, 6, 1, 17, 55, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: now)

    t_restore_start = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    rid = db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(t_restore_start),
        end_time=_iso(t_restore_start + timedelta(minutes=5)),
        device="daikin", action_type="restore",
        params={"tank_power": True, "tank_temp": 45}, status="pending",
    )

    db.clear_actions_in_range(
        _iso(datetime(2026, 6, 1, 17, 0, tzinfo=UTC)),
        _iso(datetime(2026, 6, 2, 17, 0, tzinfo=UTC)),
        device="daikin",
    )

    assert db.get_action_by_id(rid) is None
