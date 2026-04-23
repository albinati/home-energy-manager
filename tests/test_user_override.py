"""User-override acceptance loop — Phase 4.3 (#42).

Covers:
- detect_user_override pure helper (grace period + tolerance check)
- schema migration adds overridden_by_user_at column
- db.mark_action_user_overridden sets the column
- _reconcile_daikin_actions skips rows once overridden
- Integration: a diverged live value past grace → row marked, no PATCH, one notification
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from src.daikin.models import DaikinDevice

# ── Phase 4 review C9: grace clamp ────────────────────────────────────────────

def test_env_int_at_least_clamps_below_minimum(monkeypatch):
    """DAIKIN_OVERRIDE_GRACE_SECONDS=0 in .env would self-DoS. env_int_at_least
    clamps to the minimum. No module reload — uses a pure helper."""
    from src.config import env_int_at_least

    monkeypatch.setenv("TEST_GRACE_CLAMP", "0")
    assert env_int_at_least("TEST_GRACE_CLAMP", 600, 60) == 60
    monkeypatch.setenv("TEST_GRACE_CLAMP", "30")
    assert env_int_at_least("TEST_GRACE_CLAMP", 600, 60) == 60


def test_env_int_at_least_preserves_generous_values(monkeypatch):
    from src.config import env_int_at_least

    monkeypatch.setenv("TEST_GRACE_CLAMP", "900")
    assert env_int_at_least("TEST_GRACE_CLAMP", 600, 60) == 900
    # Default when unset
    monkeypatch.delenv("TEST_GRACE_CLAMP", raising=False)
    assert env_int_at_least("TEST_GRACE_CLAMP", 600, 60) == 600
    # Garbage still clamps to default (not crash)
    monkeypatch.setenv("TEST_GRACE_CLAMP", "bobby_tables")
    assert env_int_at_least("TEST_GRACE_CLAMP", 600, 60) == 600


# ── Pure helper: detect_user_override ──────────────────────────────────────────

def test_detect_user_override_within_grace_returns_false(monkeypatch):
    """Inside the grace window, divergence is ignored (probably our own echo)."""
    from src.daikin_bulletproof import detect_user_override

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_GRACE_SECONDS", 600)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_TOLERANCE_TANK_C", 0.6)

    dev = DaikinDevice(id="gw", name="x", tank_target=55.0)
    now = datetime(2026, 4, 21, 20, 5, 0, tzinfo=UTC)
    started = now - timedelta(seconds=300)  # only 5 min elapsed → within grace

    override, _reason = detect_user_override(
        dev, {"tank_temp": 45.0}, row_started_utc=started, now_utc=now
    )
    assert override is False


def test_detect_user_override_after_grace_tank_temp_divergent(monkeypatch):
    from src.daikin_bulletproof import detect_user_override

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_GRACE_SECONDS", 600)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_TOLERANCE_TANK_C", 0.6)

    dev = DaikinDevice(id="gw", name="x", tank_target=55.0)
    now = datetime(2026, 4, 21, 20, 15, 0, tzinfo=UTC)
    started = now - timedelta(seconds=700)  # past grace

    override, reason = detect_user_override(
        dev, {"tank_temp": 45.0}, row_started_utc=started, now_utc=now
    )
    assert override is True
    assert reason is not None and "tank_temp" in reason


def test_detect_user_override_tank_power_toggle(monkeypatch):
    """User flipped DHW off via the app."""
    from src.daikin_bulletproof import detect_user_override

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_GRACE_SECONDS", 600)

    dev = DaikinDevice(id="gw", name="x", tank_on=False)
    now = datetime(2026, 4, 21, 20, 15, 0, tzinfo=UTC)
    started = now - timedelta(seconds=700)

    override, reason = detect_user_override(
        dev, {"tank_power": True}, row_started_utc=started, now_utc=now
    )
    assert override is True
    assert reason is not None and "tank_power" in reason


def test_detect_user_override_small_divergence_within_tolerance_returns_false(monkeypatch):
    """Tiny drift (< tolerance) is not an override."""
    from src.daikin_bulletproof import detect_user_override

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_GRACE_SECONDS", 600)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_TOLERANCE_TANK_C", 0.6)

    dev = DaikinDevice(id="gw", name="x", tank_target=45.4)
    now = datetime(2026, 4, 21, 20, 15, 0, tzinfo=UTC)
    started = now - timedelta(seconds=700)

    override, _reason = detect_user_override(
        dev, {"tank_temp": 45.0}, row_started_utc=started, now_utc=now
    )
    assert override is False


def test_detect_user_override_unknown_live_value_returns_false(monkeypatch):
    """If the cached snapshot doesn't have the relevant field, don't flag an override."""
    from src.daikin_bulletproof import detect_user_override

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_GRACE_SECONDS", 600)

    dev = DaikinDevice(id="gw", name="x", tank_target=None, tank_on=None)
    now = datetime(2026, 4, 21, 20, 15, 0, tzinfo=UTC)
    started = now - timedelta(seconds=700)

    override, _reason = detect_user_override(
        dev, {"tank_temp": 45.0, "tank_power": True}, row_started_utc=started, now_utc=now
    )
    assert override is False


# ── Schema + DB helpers ────────────────────────────────────────────────────────

def test_migration_adds_overridden_by_user_at_column(monkeypatch):
    from src import db

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(action_schedule)").fetchall()}
            assert "overridden_by_user_at" in cols
        finally:
            conn.close()


def test_mark_action_user_overridden_sets_column(monkeypatch):
    from src import db

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        # Seed an action row
        now_iso = datetime.now(UTC).isoformat()
        conn = db.get_connection()
        try:
            conn.execute(
                """INSERT INTO action_schedule
                   (date, start_time, end_time, device, action_type, params, status, created_at)
                   VALUES (?, ?, ?, 'daikin', 'pre_heat', '{}', 'active', ?)""",
                ("2026-04-21", "2026-04-21T20:00:00Z", "2026-04-21T20:30:00Z", now_iso),
            )
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        db.mark_action_user_overridden(row_id)

        row = db.get_action_by_id(row_id)
        assert row is not None
        assert row.get("overridden_by_user_at") is not None


# ── Reconciler integration ─────────────────────────────────────────────────────

def _seed_active_row(conn, *, start_offset_seconds: int, params: dict) -> int:
    """Insert an active Daikin row that started ``start_offset_seconds`` ago."""
    now = datetime.now(UTC)
    start = (now - timedelta(seconds=start_offset_seconds)).isoformat()
    end = (now + timedelta(seconds=1800)).isoformat()
    conn.execute(
        """INSERT INTO action_schedule
           (date, start_time, end_time, device, action_type, params, status, created_at)
           VALUES (?, ?, ?, 'daikin', 'pre_heat', ?, 'active', ?)""",
        (now.date().isoformat(), start, end, json.dumps(params), now.isoformat()),
    )
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return int(rid)


def test_reconcile_marks_overridden_row_completed_past_end(monkeypatch):
    """Phase 4 review: overridden rows must still transition to 'completed' when their
    window ends, otherwise they pollute every status='active' query forever."""
    from src import db
    from src.state_machine import _reconcile_daikin_actions

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            # Seed an overridden row whose end_time is in the past.
            past_start = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            past_end = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
            conn.execute(
                """INSERT INTO action_schedule
                   (date, start_time, end_time, device, action_type, params, status, created_at)
                   VALUES (?, ?, ?, 'daikin', 'pre_heat', '{}', 'active', ?)""",
                (datetime.now(UTC).date().isoformat(), past_start, past_end, datetime.now(UTC).isoformat()),
            )
            rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()
        finally:
            conn.close()

        db.mark_action_user_overridden(rid)

        dev = DaikinDevice(id="gw", name="x", tank_target=55.0)
        client = MagicMock()
        _reconcile_daikin_actions([], client, dev, datetime.now(UTC), trigger="test")  # warm path
        rows = db.get_actions_for_plan_date(datetime.now(UTC).date().isoformat(), device="daikin")
        _reconcile_daikin_actions(rows, client, dev, datetime.now(UTC), trigger="test")

        row = db.get_action_by_id(rid)
        assert row is not None
        assert row["status"] == "completed", f"expected completed, got {row['status']}"
        # And still no Daikin writes
        assert client.set_tank_temperature.call_count == 0


def test_reconcile_skips_already_overridden_row(monkeypatch):
    from src import db
    from src.state_machine import _reconcile_daikin_actions

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            rid = _seed_active_row(conn, start_offset_seconds=1200, params={"tank_temp": 45.0})
            db.mark_action_user_overridden(rid)
        finally:
            conn.close()

        dev = DaikinDevice(id="gw", name="x", tank_target=55.0)  # diverged live
        client = MagicMock()
        now_utc = datetime.now(UTC)

        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        _reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        # No Daikin API calls on an overridden row
        assert client.set_tank_temperature.call_count == 0
        assert client.set_lwt_offset.call_count == 0


def test_reconcile_detects_override_marks_row_and_notifies(monkeypatch):
    """Integration: FIRST tick applies; SECOND tick past grace with diverged live detects override.

    Phase 4 review C6: override detection only runs after a row has been applied at least once
    in this process. Without this split, a boot-recovery tick would false-fire overrides because
    row.start_time is ancient but we haven't actually had a chance to push our value yet.
    """
    import src.state_machine as sm
    from src import db

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_GRACE_SECONDS", 60)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_TOLERANCE_TANK_C", 0.6)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)

    # C6: clear process-local state between tests.
    sm._FIRST_APPLIED_SESSION.clear()

    notifications: list[str] = []
    monkeypatch.setattr(
        "src.state_machine.notify_user_override",
        lambda msg: notifications.append(msg),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            rid = _seed_active_row(conn, start_offset_seconds=30, params={"tank_temp": 45.0})
        finally:
            conn.close()

        client = MagicMock()
        t1 = datetime.now(UTC)
        # Tick 1: live state matches plan — no override, but seeds _FIRST_APPLIED_SESSION.
        dev_matching = DaikinDevice(id="gw", name="x", tank_target=45.0)
        rows = db.get_actions_for_plan_date(t1.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev_matching, t1, trigger="test")

        row = db.get_action_by_id(rid)
        assert row is not None
        assert row.get("overridden_by_user_at") is None, "first tick must NOT flag override"
        assert len(notifications) == 0
        assert rid in sm._FIRST_APPLIED_SESSION

        # Tick 2: advance past grace window; live state now diverges (user changed via app).
        t2 = t1 + timedelta(seconds=120)  # > grace (60s)
        dev_diverged = DaikinDevice(id="gw", name="x", tank_target=55.0)
        rows = db.get_actions_for_plan_date(t2.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev_diverged, t2, trigger="test")

        row = db.get_action_by_id(rid)
        assert row is not None
        assert row.get("overridden_by_user_at") is not None, "second tick past grace must flag override"
        assert client.set_tank_temperature.call_count == 0
        assert len(notifications) == 1
        assert "tank_temp" in notifications[0]


def test_boot_recovery_does_not_false_flag_override_on_first_tick(monkeypatch):
    """Phase 4 review C6: after systemd restart mid-plan, the row's start_time is ancient
    but we haven't applied it in this process yet. Previously this would immediately flag
    the row as overridden on the first reconcile tick — silently aborting the plan.
    """
    import src.state_machine as sm
    from src import db

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_GRACE_SECONDS", 60)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_OVERRIDE_TOLERANCE_TANK_C", 0.6)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)

    sm._FIRST_APPLIED_SESSION.clear()

    notifications: list[str] = []
    monkeypatch.setattr(
        "src.state_machine.notify_user_override",
        lambda msg: notifications.append(msg),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            # Row started 900s ago (well past grace). Simulate "systemd restart after 15min".
            rid = _seed_active_row(conn, start_offset_seconds=900, params={"tank_temp": 45.0})
        finally:
            conn.close()

        # Live state diverges — cloud hasn't received our write yet (fresh process).
        dev = DaikinDevice(id="gw", name="x", tank_target=55.0)
        client = MagicMock()
        now_utc = datetime.now(UTC)

        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="boot_recovery")

        row = db.get_action_by_id(rid)
        assert row is not None
        assert row.get("overridden_by_user_at") is None, (
            "first tick after boot must NEVER flag an override — we haven't applied yet"
        )
        assert len(notifications) == 0
        # Must have actually APPLIED on this tick — the whole point is that boot-recovery
        # pushes our value rather than assuming the user changed it.
        assert rid in sm._FIRST_APPLIED_SESSION
