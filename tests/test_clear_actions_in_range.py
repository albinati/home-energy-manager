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
