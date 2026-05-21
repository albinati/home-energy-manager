"""Epic 14 follow-up (#388): local dev snapshot mutation + inter-row settle.

After each successful ``client.set_*`` PATCH, ``apply_scheduled_daikin_params``
must mutate the local ``dev`` snapshot so subsequent reads in the same
process see the predicted state. Without this, the cached snapshot is only
refreshed via the 30-min ``DAIKIN_DEVICES_CACHE_TTL_SECONDS`` cycle, and
pre-fire idempotency checks in later heartbeat ticks would correctly fall
through (stale dev) → redundant PATCH (the prod bug D pattern).

Plus the heartbeat reconciler now sleeps ``DAIKIN_VALVE_SETTLE_SECONDS``
between consecutive applies in the same tick so Onecta has propagation
time. Idle ticks (all rows pre-fire-skipped) take zero extra time.
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.daikin.models import DaikinDevice


@pytest.fixture
def _db(monkeypatch):
    """Temp SQLite — needed because apply_scheduled_daikin_params writes
    action_log rows via db.log_action."""
    from src import db
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        yield path


# ── Local snapshot mutation ───────────────────────────────────────────────────

def test_apply_mutates_tank_target_after_set_tank_temperature(monkeypatch, _db):
    """After client.set_tank_temperature(45), dev.tank_target == 45.0."""
    from src.daikin_bulletproof import apply_scheduled_daikin_params

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    # Eliminate sleep noise in the test
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("src.daikin_bulletproof.time.sleep", lambda _: None)

    dev = DaikinDevice(
        id="gw", name="x", tank_on=True, tank_target=37.0, tank_powerful=False,
    )
    client = MagicMock()
    apply_scheduled_daikin_params(
        dev, client,
        {"tank_power": True, "tank_temp": 45, "tank_powerful": False},
        trigger="test",
    )
    assert dev.tank_target == 45.0
    assert dev.tank_on is True
    assert dev.tank_powerful is False


def test_apply_mutates_tank_on_after_set_tank_power_off(monkeypatch, _db):
    """tank_power=False path must also mutate dev.tank_on."""
    from src.daikin_bulletproof import apply_scheduled_daikin_params

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("src.daikin_bulletproof.time.sleep", lambda _: None)

    dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_powerful=False)
    client = MagicMock()
    apply_scheduled_daikin_params(
        dev, client,
        {"tank_power": False, "tank_powerful": False},
        trigger="test",
    )
    assert dev.tank_on is False


def test_apply_mutates_lwt_offset(monkeypatch, _db):
    """After successful client.set_lwt_offset(-2), dev.lwt_offset == -2.0."""
    from src.daikin_bulletproof import apply_scheduled_daikin_params

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("src.daikin_bulletproof.time.sleep", lambda _: None)

    dev = DaikinDevice(
        id="gw", name="x", is_on=True, lwt_offset=0.0, tank_on=True,
    )
    client = MagicMock()
    apply_scheduled_daikin_params(
        dev, client,
        {"lwt_offset": -2, "climate_on": True},
        trigger="test",
    )
    assert dev.lwt_offset == -2.0
    assert dev.is_on is True


def test_apply_does_not_mutate_when_client_raises(monkeypatch, _db):
    """If client.set_tank_temperature raises a non-read_only error, dev.tank_target
    must NOT be updated (the write didn't land)."""
    from src.daikin.client import DaikinError
    from src.daikin_bulletproof import apply_scheduled_daikin_params

    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("src.daikin_bulletproof.time.sleep", lambda _: None)

    dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=37.0)
    client = MagicMock()
    client.set_tank_temperature.side_effect = DaikinError("HTTP 503 service unavailable")

    import pytest as _pytest
    with _pytest.raises(DaikinError):
        apply_scheduled_daikin_params(
            dev, client, {"tank_power": True, "tank_temp": 45}, trigger="test",
        )

    # tank_on was set successfully BEFORE the temp call raised — so True is correct.
    assert dev.tank_on is True
    # tank_target must NOT have been mutated since the PATCH failed.
    assert dev.tank_target == 37.0


# ── Inter-row settle in the reconciler ────────────────────────────────────────

def _seed_active_row(conn, *, start_offset_seconds: int, params: dict) -> int:
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


def test_inter_row_settle_fires_when_two_rows_apply(monkeypatch):
    """Two rows both trigger apply in the same tick → exactly one settle
    sleep between them, at DAIKIN_VALVE_SETTLE_SECONDS."""
    import src.state_machine as sm
    from src import db

    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.config.config.DAIKIN_VALVE_SETTLE_SECONDS", 10)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()

    # Mock the apply function to always return True (write attempted).
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: True,
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr("src.state_machine.time.sleep", lambda s: sleep_calls.append(s))

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            # Two distinct rows that both fall in the fire window. Different
            # params to avoid the idempotency-match path.
            _seed_active_row(conn, start_offset_seconds=60,
                             params={"tank_power": True, "tank_temp": 45})
            _seed_active_row(conn, start_offset_seconds=60,
                             params={"tank_power": True, "tank_temp": 50})
        finally:
            conn.close()

        dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=37.0)
        client = MagicMock()
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        # Exactly one inter-row sleep, at the configured 10s value.
        assert sleep_calls == [10], (
            f"Expected one 10s inter-row settle between two applies, "
            f"got sleep_calls={sleep_calls}"
        )


def test_inter_row_settle_skipped_when_first_row_does_not_apply(monkeypatch):
    """If the first row hits the pre-fire match (no API call), the second
    row should NOT sleep. Idle tick = zero overhead."""
    import src.state_machine as sm
    from src import db

    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.config.config.DAIKIN_VALVE_SETTLE_SECONDS", 10)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()

    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: True,
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr("src.state_machine.time.sleep", lambda s: sleep_calls.append(s))

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            # Row 1: state matches → pre-fire skip → no apply, no flag set.
            _seed_active_row(conn, start_offset_seconds=60,
                             params={"tank_power": True, "tank_temp": 45,
                                     "tank_powerful": False})
            # Row 2: state differs → apply.
            _seed_active_row(conn, start_offset_seconds=60,
                             params={"tank_power": True, "tank_temp": 50,
                                     "tank_powerful": False})
        finally:
            conn.close()

        dev = DaikinDevice(
            id="gw", name="x", tank_on=True, tank_target=45.0, tank_powerful=False,
        )
        client = MagicMock()
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        # No sleeps — first row skipped pre-fire, so the flag stayed False
        # when the second row processed.
        assert sleep_calls == [], (
            f"Expected zero sleeps when first row was pre-fire-skipped, "
            f"got {sleep_calls}"
        )


# ── End-to-end: bug D dedup via REAL apply (no mock mutation needed) ──────────

def test_real_bug_D_dedup_with_real_apply_path(monkeypatch):
    """Same scenario as test_real_bug_D_overlapping_solar_preheat_dedup but
    using the REAL apply_scheduled_daikin_params (not a mocked one that
    manually echoes state). The local snapshot mutation in
    apply_scheduled_daikin_params is what makes this work without manual
    echo — pinning the integration.
    """
    import src.state_machine as sm
    from src import db

    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("src.daikin_bulletproof.time.sleep", lambda _: None)
    # Same for the reconciler's inter-row settle — eliminate sleep noise.
    monkeypatch.setattr("src.config.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("src.state_machine.time.sleep", lambda _: None)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        now_utc = datetime(2026, 5, 21, 12, 30, tzinfo=UTC)
        conn = db.get_connection()
        try:
            common_params = {"tank_powerful": False, "lp_optimizer": True,
                             "tank_power": True, "tank_temp": 45}
            for s in ("08:00", "09:00", "11:00", "11:30"):
                conn.execute(
                    """INSERT INTO action_schedule
                       (date, start_time, end_time, device, action_type, params, status, created_at)
                       VALUES (?, ?, ?, 'daikin', 'solar_preheat', ?, 'pending', ?)""",
                    ("2026-05-21",
                     f"2026-05-21T{s}:00Z", "2026-05-21T14:00:00Z",
                     json.dumps(common_params),
                     now_utc.isoformat()),
                )
            conn.commit()
        finally:
            conn.close()

        # Stale dev — first row should apply and mutate; remaining 3 hit match.
        dev = DaikinDevice(
            id="gw", name="x", tank_on=True, tank_target=37.0, tank_powerful=False,
        )
        client = MagicMock()
        rows = db.get_actions_for_plan_date("2026-05-21", device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="heartbeat")

        # Tank temp written exactly once (rest were deduped via the now-
        # current local snapshot).
        assert client.set_tank_temperature.call_count == 1, (
            f"Expected 1 PATCH (rest deduped via local snapshot mutation), "
            f"got {client.set_tank_temperature.call_count}"
        )
        # And the snapshot reflects the write.
        assert dev.tank_target == 45.0
