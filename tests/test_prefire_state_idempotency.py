"""Epic 14 (#386): pre-fire state-match idempotency.

The heartbeat reconciler reads live Daikin state from the cached device
snapshot and skips PATCH writes when the state already matches a pending
row's params. This:

- Kills the READ_ONLY_CHARACTERISTIC HTTP 400s seen on prod when
  tank_power=False is written to an already-off tank (bug E)
- De-duplicates overlapping replan rows by completing the second/third/...
  duplicates after the first one actually changes state (bug D)
- Fails open: unknown live state (cache miss, telemetry blip) falls
  through to the normal apply path
- Honours a feature flag (PREFIRE_STATE_MATCH_ENABLED) for rollback
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from src.daikin.models import DaikinDevice


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


def _common_monkeypatch(monkeypatch, *, prefire_enabled: bool = True):
    """Defaults used by every test below."""
    import src.state_machine as sm

    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", prefire_enabled)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()


def test_prefire_skips_when_state_matches(monkeypatch):
    """Tank target already at 45, row wants 45 → row completed, no API call."""
    import src.state_machine as sm
    from src import db

    _common_monkeypatch(monkeypatch)

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            rid = _seed_active_row(
                conn, start_offset_seconds=60,
                params={"tank_power": True, "tank_temp": 45},
            )
        finally:
            conn.close()

        dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=45.0)
        client = MagicMock()
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        row = db.get_action_by_id(rid)
        assert row is not None
        assert row["status"] == "completed"
        assert (row.get("error_msg") or "").startswith("noop")
        assert len(apply_calls) == 0


def test_prefire_skips_when_tank_already_off(monkeypatch):
    """Bug E regression — row wants tank_power=False, tank already off → skip.

    Pre-fix, the Daikin client would PATCH onOffMode=off and get HTTP 400
    READ_ONLY_CHARACTERISTIC, marking the row 'failed' and polluting the audit.
    """
    import src.state_machine as sm
    from src import db

    _common_monkeypatch(monkeypatch)

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            rid = _seed_active_row(
                conn, start_offset_seconds=60, params={"tank_power": False},
            )
        finally:
            conn.close()

        dev = DaikinDevice(id="gw", name="x", tank_on=False)
        client = MagicMock()
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        row = db.get_action_by_id(rid)
        assert row is not None
        assert row["status"] == "completed"
        assert len(apply_calls) == 0


def test_prefire_writes_when_state_diverges(monkeypatch):
    """Tank at 37, row wants 45 → state diverges → apply called normally."""
    import src.state_machine as sm
    from src import db

    _common_monkeypatch(monkeypatch)

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_active_row(
                conn, start_offset_seconds=60,
                params={"tank_power": True, "tank_temp": 45},
            )
        finally:
            conn.close()

        dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=37.0)
        client = MagicMock()
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        assert len(apply_calls) == 1
        assert apply_calls[0].get("tank_temp") == 45


def test_prefire_falls_through_when_state_unknown(monkeypatch):
    """Cache miss (dev.tank_target=None) → can't confirm match → apply normally."""
    import src.state_machine as sm
    from src import db

    _common_monkeypatch(monkeypatch)

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_active_row(
                conn, start_offset_seconds=60,
                params={"tank_power": True, "tank_temp": 45},
            )
        finally:
            conn.close()

        # tank_target is None — cannot verify match.
        dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=None)
        client = MagicMock()
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        assert len(apply_calls) == 1


def test_prefire_disabled_via_flag(monkeypatch):
    """PREFIRE_STATE_MATCH_ENABLED=False → idempotency check is bypassed."""
    import src.state_machine as sm
    from src import db

    _common_monkeypatch(monkeypatch, prefire_enabled=False)

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_active_row(
                conn, start_offset_seconds=60,
                params={"tank_power": True, "tank_temp": 45},
            )
        finally:
            conn.close()

        dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=45.0)
        client = MagicMock()
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        # State matched but flag is off → apply was called anyway.
        assert len(apply_calls) == 1


def test_prefire_dedupes_overlapping_rows(monkeypatch):
    """Bug D regression — five replan rows with same params, dev state matches
    the first one immediately. After tick 1 the row that fires is whichever
    sorts first; the rest hit the matched-state path and complete without
    additional API calls.
    """
    import src.state_machine as sm
    from src import db

    _common_monkeypatch(monkeypatch)

    apply_calls: list[dict] = []

    def _record_and_set_state(dev, client, params, trigger):
        apply_calls.append(dict(params))
        # Simulate the device echoing the write back into our cached snapshot.
        if "tank_temp" in params:
            dev.tank_target = float(params["tank_temp"])
        if "tank_power" in params:
            dev.tank_on = bool(params["tank_power"])

    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params", _record_and_set_state,
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            # Five rows, same params, all pending and inside the firing window.
            for _ in range(5):
                _seed_active_row(
                    conn, start_offset_seconds=60,
                    params={"tank_power": True, "tank_temp": 45},
                )
        finally:
            conn.close()

        # Live tank starts off-target so the first row WILL apply; remaining
        # rows in the same tick should see the just-written state and skip.
        dev = DaikinDevice(id="gw", name="x", tank_on=True, tank_target=37.0)
        client = MagicMock()
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="test")

        # Exactly ONE apply call, even though 5 rows existed.
        assert len(apply_calls) == 1, f"expected 1 apply, got {len(apply_calls)}"
