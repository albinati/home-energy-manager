"""upsert_action must be a genuine upsert on (device, action_type, start_time).

Before this, it was a plain INSERT: every re-plan that re-emitted the same slot
created a duplicate row, and because clear_actions_in_range only removes pending
in-range rows, an already-fired (completed) or in-flight row for the same slot
was never cleared — so the re-emit produced a past-dated pending dup that fired
again. Prod showed ~18 identical tank_warmup rows in a single day.
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


def _count(device, action_type, start_time) -> int:
    rows = [r for r in db.schedule_for_date(start_time[:10].replace("Z", ""))
            if r["device"] == device and r["action_type"] == action_type
            and r["start_time"] == start_time]
    return len(rows)


def _mk(start, *, status="pending", temp=45, atype="tank_warmup"):
    return db.upsert_action(
        plan_date="2026-06-01",
        start_time=_iso(start), end_time=_iso(start + timedelta(hours=9)),
        device="daikin", action_type=atype, params={"tank_temp": temp}, status=status,
    )


def test_reemit_same_slot_does_not_duplicate() -> None:
    start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    id1 = _mk(start)
    id2 = _mk(start)  # re-plan re-emits the identical pending slot
    assert _count("daikin", "tank_warmup", _iso(start)) == 1
    assert id1 == id2  # refreshed in place, not duplicated


def test_reemit_refreshes_params_of_future_pending(monkeypatch) -> None:
    monkeypatch.setattr(db, "_now_utc", lambda: datetime(2026, 6, 1, 6, 0, tzinfo=UTC))
    start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)  # future relative to now
    _mk(start, temp=45)
    _mk(start, temp=48)  # re-plan with a different target
    rows = [r for r in db.schedule_for_date("2026-06-01")
            if r["action_type"] == "tank_warmup" and r["start_time"] == _iso(start)]
    assert len(rows) == 1
    assert rows[0]["params"]["tank_temp"] == 48  # picked up the new params


def test_completed_slot_is_not_recreated() -> None:
    """A re-plan that re-emits an already-FIRED slot must NOT create a new
    pending dup (the past dup that fired again in prod)."""
    start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    done = _mk(start, status="completed")
    again = _mk(start, status="pending")  # re-plan re-emits it
    assert again == done  # skipped — returns the existing completed row
    rows = [r for r in db.schedule_for_date("2026-06-01")
            if r["action_type"] == "tank_warmup" and r["start_time"] == _iso(start)]
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"  # untouched; no new pending dup


def test_different_action_types_same_slot_coexist() -> None:
    """warmup and negative_boost can legitimately share a start_time."""
    start = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
    _mk(start, atype="tank_warmup")
    _mk(start, atype="tank_negative_boost", temp=60)
    rows = [r for r in db.schedule_for_date("2026-06-01") if r["start_time"] == _iso(start)]
    assert {r["action_type"] for r in rows} == {"tank_warmup", "tank_negative_boost"}
