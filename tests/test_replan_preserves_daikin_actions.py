"""Replan must not drop in-flight Daikin rows (#27 — replan amnesia)."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def tmp_db(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        yield


def test_clear_preserves_pending_restore_while_main_action_active(
    tmp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending restore linked from an active row must survive clear_actions (MPC replan)."""
    day = "2026-06-01"
    frozen = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: frozen)

    t_shutdown0 = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    t_shutdown1 = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)
    t_restore1 = datetime(2026, 6, 1, 19, 1, tzinfo=UTC)

    rid = db.upsert_action(
        plan_date=day,
        start_time=_iso(t_shutdown1),
        end_time=_iso(t_restore1),
        device="daikin",
        action_type="restore",
        params={"lwt_offset": 0.0},
        status="pending",
    )
    aid = db.upsert_action(
        plan_date=day,
        start_time=_iso(t_shutdown0),
        end_time=_iso(t_shutdown1),
        device="daikin",
        action_type="shutdown",
        params={"climate_on": False},
        status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)
    db.mark_action(aid, "active")

    db.clear_actions_for_date(day, device="daikin")

    restore = db.get_action_by_id(rid)
    assert restore is not None
    assert restore["status"] == "pending"
    main = db.get_action_by_id(aid)
    assert main is not None
    assert main["status"] == "active"


def test_clear_preserves_in_window_pending_pair_before_heartbeat(
    tmp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If MPC runs before heartbeat marks the main row active, keep main + paired restore."""
    day = "2026-06-01"
    frozen = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: frozen)

    t0 = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 1, 19, 0, tzinfo=UTC)
    t_restore_end = t1 + timedelta(minutes=1)

    rid = db.upsert_action(
        plan_date=day,
        start_time=_iso(t1),
        end_time=_iso(t_restore_end),
        device="daikin",
        action_type="restore",
        params={},
        status="pending",
    )
    aid = db.upsert_action(
        plan_date=day,
        start_time=_iso(t0),
        end_time=_iso(t1),
        device="daikin",
        action_type="shutdown",
        params={},
        status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)

    db.clear_actions_for_date(day, device="daikin")

    assert db.get_action_by_id(aid) is not None
    assert db.get_action_by_id(rid) is not None


def test_clear_deletes_future_only_pending_pair(
    tmp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Purely future pending rows are removed so the new LP plan can replace them."""
    day = "2026-06-01"
    frozen = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(db, "_now_utc", lambda: frozen)

    t0 = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 1, 21, 0, tzinfo=UTC)
    t_restore_end = t1 + timedelta(minutes=1)

    rid = db.upsert_action(
        plan_date=day,
        start_time=_iso(t1),
        end_time=_iso(t_restore_end),
        device="daikin",
        action_type="restore",
        params={},
        status="pending",
    )
    aid = db.upsert_action(
        plan_date=day,
        start_time=_iso(t0),
        end_time=_iso(t1),
        device="daikin",
        action_type="pre_heat",
        params={},
        status="pending",
        restore_action_id=rid,
    )
    db.update_action_restore_link(aid, rid)

    db.clear_actions_for_date(day, device="daikin")

    assert db.get_action_by_id(aid) is None
    assert db.get_action_by_id(rid) is None
