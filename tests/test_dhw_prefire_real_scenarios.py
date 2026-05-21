"""Epic 14 (#386) — replay of the three prod incidents from 2026-05-21.

Each test reconstructs the exact rows, params, timestamps and device state
that was observed on prod, runs them through the new reconciler, and
asserts the behaviour the user should have seen.

Data source: ``/srv/hem/data/energy_state.db`` queried 2026-05-21 ~21:50 UTC.
The row IDs reference real prod rows; values are reproduced exactly.

Why these are kept as a separate file: they pin the new behaviour to
concrete, named prod incidents so any future regression has a clear
"this was the bug that drove the fix" reference.
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from src.daikin.models import DaikinDevice


def _insert_row(
    conn,
    *,
    aid: int | None = None,
    start: str,
    end: str,
    action_type: str,
    status: str,
    params: dict,
    overridden_at: str | None = None,
    restore_action_id: int | None = None,
) -> int:
    """Insert a row with explicit values, optionally pinning the id (so tests
    can match the prod row id for clarity in failure messages)."""
    now_iso = datetime.now(UTC).isoformat()
    date_str = start[:10]
    cols = "date, start_time, end_time, device, action_type, params, status, created_at"
    vals: list = [date_str, start, end, "daikin", action_type, json.dumps(params), status, now_iso]
    if overridden_at is not None:
        cols += ", overridden_by_user_at"
        vals.append(overridden_at)
    if restore_action_id is not None:
        cols += ", restore_action_id"
        vals.append(restore_action_id)
    if aid is not None:
        cols = "id, " + cols
        vals = [aid] + vals
    placeholders = ",".join("?" * len(vals))
    conn.execute(
        f"INSERT INTO action_schedule ({cols}) VALUES ({placeholders})",
        vals,
    )
    if aid is None:
        aid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return aid


def _setup_test(monkeypatch, path, *, prefire_enabled: bool = True):
    """Common setup: temp DB, common config patches, clean process state."""
    import src.state_machine as sm
    from src import db

    monkeypatch.setattr("src.config.config.DB_PATH", str(path))
    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", prefire_enabled)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()
    db.init_db()


# ── BUG C — REPLAN-CHAIN OVERRIDE PATTERN ─────────────────────────────────────
#
# Recurring across multiple days in the prod data (sample, last 30 days):
#   * 2026-05-13: 4 pre_heat rows (tank=48, on) overridden across 133 min
#   * 2026-05-16: 3 solar_preheat rows (tank=45, on) overridden across 208 min
#   * 2026-05-20 evening: rows 5518 + 5529 (tank_idle_overnight, tank=37, on)
#       overridden 16 min apart — user CLEARLY didn't want overnight tank, but
#       the LP replan kept inserting fresh rows with the same params.
#   * 2026-05-21 evening: rows 6011 + 6025 — same pattern. Caveat: the user
#       may have been testing the Onecta app rather than expressing a stable
#       preference; the 2026-05-20 case is cleaner.
#
# In all cases the system behavior is identical: HEM correctly marks the
# active row overridden, then the next LP replan inserts a NEW row covering
# the same window with identical params and no awareness of the recent
# user gesture. The new row fires, undoing the gesture.
#
# Two replay tests below: 2026-05-20 (cleanest) and 2026-05-21 (latest).


def test_real_bug_C_overnight_2026_05_20_clean_signal(monkeypatch):
    """Replays rows 5518 + 5529 from 2026-05-20 21:00-21:43 UTC — the cleanest
    bug C signal in 30d of prod data (16-minute gap between two identical
    tank_idle_overnight rows both getting overridden).

    Expected: row 5529 inherits the override from 5518 and does NOT fire.
    """
    import src.state_machine as sm
    from src import db

    notifications: list[str] = []
    monkeypatch.setattr(
        "src.state_machine.notify_user_override",
        lambda msg: notifications.append(msg),
    )
    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "prod_replay.db"
        _setup_test(monkeypatch, path)
        # "Now" ≈ 21:32 — moment row 5529 fired in prod (analogous to 6025).
        now_utc = datetime(2026, 5, 20, 21, 32, 0, tzinfo=UTC)

        conn = db.get_connection()
        try:
            _insert_row(
                conn, aid=5518,
                start="2026-05-20T21:00:00Z",
                end="2026-05-21T06:00:00Z",
                action_type="tank_idle_overnight",
                status="active",
                params={"tank_powerful": False, "lp_optimizer": True,
                        "tank_power": True, "tank_temp": 37},
                overridden_at="2026-05-20T21:27:57.000000+00:00",
            )
            _insert_row(
                conn, aid=5529,
                start="2026-05-20T21:30:00Z",
                end="2026-05-21T06:00:00Z",
                action_type="tank_idle_overnight",
                status="pending",
                params={"tank_powerful": False, "lp_optimizer": True,
                        "tank_power": True, "tank_temp": 37},
            )
        finally:
            conn.close()

        # User had the tank off after the 21:27 gesture.
        dev = DaikinDevice(id="gw", name="x", tank_on=False, tank_target=37.0)
        client = MagicMock()

        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="heartbeat")

        row_5529 = db.get_action_by_id(5529)
        assert row_5529 is not None
        assert row_5529.get("overridden_by_user_at") is not None, (
            "Row 5529 must inherit override from row 5518 — without this fix "
            "the user had to turn the tank off again at 21:43 in prod"
        )
        assert len(apply_calls) == 0
        assert len(notifications) == 1
        assert "row 5518" in notifications[0]


def test_real_bug_C_overnight_2026_05_21_latest_observation(monkeypatch):
    """Replays rows 6011 + 6025 from 2026-05-21 21:00-21:42 UTC.

    NOTE: the user noted they may have been testing the Onecta app on this
    evening (rather than expressing a stable "no tank tonight" preference).
    Either way, the system's BEHAVIOUR is the same — a new row with
    identical params is inserted minutes after the override and fires
    blindly. This test pins that behaviour even if the gesture was
    exploratory. See 2026-05-20 test above for an unambiguous case.
    """
    import src.state_machine as sm
    from src import db

    notifications: list[str] = []
    monkeypatch.setattr(
        "src.state_machine.notify_user_override",
        lambda msg: notifications.append(msg),
    )
    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "prod_replay.db"
        _setup_test(monkeypatch, path)
        # "Now" = 21:32:19 UTC — the moment row 6025 fired in prod.
        now_utc = datetime(2026, 5, 21, 21, 32, 19, tzinfo=UTC)

        conn = db.get_connection()
        try:
            # Row 6011 — already overridden by user at 21:11:44.
            _insert_row(
                conn, aid=6011,
                start="2026-05-21T21:00:00Z",
                end="2026-05-22T06:00:00Z",
                action_type="tank_idle_overnight",
                status="active",
                params={"tank_powerful": False, "lp_optimizer": True,
                        "tank_power": True, "tank_temp": 37},
                overridden_at="2026-05-21T21:11:44.114153+00:00",
            )
            # Row 6025 — the replacement row, pending at "now".
            _insert_row(
                conn, aid=6025,
                start="2026-05-21T21:30:00Z",
                end="2026-05-22T06:00:00Z",
                action_type="tank_idle_overnight",
                status="pending",
                params={"tank_powerful": False, "lp_optimizer": True,
                        "tank_power": True, "tank_temp": 37},
            )
        finally:
            conn.close()

        # Device state: user has the tank OFF. We don't know exact tank
        # temperature at this moment but the telemetry sample from 22:25
        # UTC showed 51°C target=45 (legacy from earlier in day), so it's
        # reasonable to use tank_target=37 (the row 6011 setpoint that
        # Daikin had echoed back before override).
        dev = DaikinDevice(id="gw", name="x", tank_on=False, tank_target=37.0)
        client = MagicMock()

        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="heartbeat")

        # Assert row 6025 was inherited-suppressed.
        row_6025 = db.get_action_by_id(6025)
        assert row_6025 is not None
        assert row_6025.get("overridden_by_user_at") is not None, (
            "Row 6025 must inherit the override from row 6011 — otherwise the "
            "user would have to turn the tank off again (the actual prod bug)"
        )
        # Assert no Daikin write happened on row 6025.
        assert len(apply_calls) == 0, (
            f"Expected zero apply calls when override inheritance fires; "
            f"got {len(apply_calls)} (params: {apply_calls})"
        )
        # Assert single notification referencing source row 6011.
        assert len(notifications) == 1
        assert "row 6011" in notifications[0]


# ── BUG D: rows 5601, 5613, 5639, 5653, 5667 (2026-05-21 08:00-14:00) ─────────
#
# Prod incident: between 08:00 and 14:00 UTC five separate `solar_preheat`
# rows fired with identical params {tank_power: True, tank_temp: 45}.
# Daikin telemetry shows tank_target stayed at 45 throughout — only the
# first write actually changed state, the other four were redundant.
# Each one consumed quota from the 200/day Onecta budget.
#
# With Epic 14, after the first write sets target=45, subsequent rows
# pre-fire compare dev.tank_target=45 against their param.tank_temp=45
# and complete with `noop (state matched pre-fire)` — zero further writes.


def test_real_bug_D_overlapping_solar_preheat_dedup(monkeypatch):
    """Replays rows 5601, 5613, 5639, 5653 from 2026-05-21 08:00-13:30 UTC.

    Five rows in prod → 5 API calls. With Epic 14 → 1 API call.
    (5667 is excluded from the assertion because its params dict on prod
    didn't carry tank_temp; this test focuses on the canonical pattern.)
    """
    import src.state_machine as sm
    from src import db

    apply_calls: list[dict] = []

    def _record_and_echo_to_dev(dev, client, params, trigger):
        apply_calls.append(dict(params))
        # Simulate the Daikin write echoing back into the cached snapshot.
        # Each subsequent row in the same tick reads against the freshly-
        # updated snapshot and hits the idempotency match.
        if "tank_temp" in params:
            dev.tank_target = float(params["tank_temp"])
        if "tank_power" in params:
            dev.tank_on = bool(params["tank_power"])
        if "tank_powerful" in params:
            dev.tank_powerful = bool(params["tank_powerful"])

    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params", _record_and_echo_to_dev,
    )
    monkeypatch.setattr("src.state_machine.notify_user_override", lambda msg: None)

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "prod_replay.db"
        _setup_test(monkeypatch, path)
        # "Now" = 12:30 UTC — all four canonical rows are within their
        # firing windows at this moment.
        now_utc = datetime(2026, 5, 21, 12, 30, tzinfo=UTC)

        conn = db.get_connection()
        try:
            # Each row from prod, in id order. Mark all pending so the
            # reconciler tries to fire them in this tick.
            common_params = {"tank_powerful": False, "lp_optimizer": True,
                             "tank_power": True, "tank_temp": 45}
            _insert_row(
                conn, aid=5601, start="2026-05-21T08:00:00Z", end="2026-05-21T14:00:00Z",
                action_type="solar_preheat", status="pending", params=common_params,
            )
            _insert_row(
                conn, aid=5613, start="2026-05-21T09:00:00Z", end="2026-05-21T14:00:00Z",
                action_type="solar_preheat", status="pending", params=common_params,
            )
            _insert_row(
                conn, aid=5639, start="2026-05-21T11:00:00Z", end="2026-05-21T13:30:00Z",
                action_type="solar_preheat", status="pending", params=common_params,
            )
            _insert_row(
                conn, aid=5653, start="2026-05-21T11:30:00Z", end="2026-05-21T13:30:00Z",
                action_type="solar_preheat", status="pending", params=common_params,
            )
        finally:
            conn.close()

        # Device state pre-apply: tank target diverges from the row's intent
        # so the first row WILL apply and echo back. tank_powerful is set
        # because the row's params include it (strict observability gate).
        dev = DaikinDevice(
            id="gw", name="x",
            tank_on=True, tank_target=37.0, tank_powerful=False,
        )
        client = MagicMock()

        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="heartbeat")

        # Exactly ONE apply call regardless of how many rows hit the window.
        assert len(apply_calls) == 1, (
            f"Expected 1 apply call (first row fires + state echoes; rest are "
            f"deduped via pre-fire match), got {len(apply_calls)} calls. "
            f"Prod observed 5 redundant API calls — this is the regression test."
        )
        # And the surviving call has the canonical params.
        assert apply_calls[0].get("tank_temp") == 45
        assert apply_calls[0].get("tank_power") is True


# ── BUG E: row 5830 (2026-05-21 16:30-16:36 — failed shutdown) ────────────────
#
# Prod incident: at 15:30 row 5828 (`tank_idle_overnight`, tank=37, on)
# fired. At 15:40:51 the user manually turned the tank off (override on 5828).
# At 16:30 row 5830 (`shutdown`, tank_power=False) became active. At 16:36:37
# it tried to set tank_power=False on an already-off tank and got
# HTTP 400 `READ_ONLY_CHARACTERISTIC`. Row marked `failed`, error_msg
# pollutes the daily audit timer.
#
# With Epic 14:
#   - Idempotency check: dev.tank_on=False vs params.tank_power=False → MATCH
#   - Row completes pre-fire with "noop (state matched pre-fire)"
#   - Zero API calls, zero failures, zero audit pollution


def test_real_bug_E_shutdown_after_user_turned_tank_off(monkeypatch):
    """Replays rows 5828 + 5830 from 2026-05-21 15:30-16:36 UTC.

    Expected: row 5830 completes via pre-fire match — no Daikin API call,
    no READ_ONLY_CHARACTERISTIC HTTP 400, no `failed` status.
    """
    import src.state_machine as sm
    from src import db

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )
    monkeypatch.setattr("src.state_machine.notify_user_override", lambda msg: None)

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "prod_replay.db"
        _setup_test(monkeypatch, path)
        # "Now" = 16:36 UTC — the moment 5830 fired and failed on prod.
        now_utc = datetime(2026, 5, 21, 16, 36, 37, tzinfo=UTC)

        conn = db.get_connection()
        try:
            # Row 5828 — earlier overridden row (already completed at end_time).
            _insert_row(
                conn, aid=5828,
                start="2026-05-21T15:30:00Z",
                end="2026-05-21T16:30:00Z",
                action_type="tank_idle_overnight",
                status="completed",
                params={"tank_powerful": False, "lp_optimizer": True,
                        "tank_power": True, "tank_temp": 37},
                overridden_at="2026-05-21T15:40:51.873403+00:00",
            )
            # Row 5830 — the shutdown row that failed in prod.
            _insert_row(
                conn, aid=5830,
                start="2026-05-21T16:30:00Z",
                end="2026-05-21T18:00:00Z",
                action_type="shutdown",
                status="pending",
                params={"tank_powerful": False, "lp_optimizer": True,
                        "tank_power": False},
            )
        finally:
            conn.close()

        # Device state at 16:36: user had the tank off since 15:40.
        # tank_powerful was already false from earlier (the param is in the
        # row's intent so the observability gate needs it set).
        dev = DaikinDevice(id="gw", name="x", tank_on=False, tank_powerful=False)
        client = MagicMock()

        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="heartbeat")

        # No API call attempted → no READ_ONLY error possible.
        assert len(apply_calls) == 0, (
            f"Expected zero apply calls (state already matches), got "
            f"{len(apply_calls)} — pre-fire idempotency check failed"
        )
        # Row 5830 should be COMPLETED via pre-fire match — not 'failed'.
        row_5830 = db.get_action_by_id(5830)
        assert row_5830 is not None
        assert row_5830["status"] == "completed", (
            f"Expected completed (via pre-fire match), got '{row_5830['status']}' "
            f"with error_msg={row_5830.get('error_msg')!r}. "
            f"Prod observed status='failed' with the READ_ONLY_CHARACTERISTIC error."
        )
        assert (row_5830.get("error_msg") or "").startswith("noop")


# ── BUG E (variant): user_override path also catches this defensively ─────────
#
# Even if PREFIRE_STATE_MATCH_ENABLED is somehow off, the user-override
# inheritance path should catch row 5830 — row 5828 was overridden 56 min
# ago, within the 4h respect window, and the user's tank-off gesture is
# still in effect.


def test_real_bug_E_falls_back_to_override_inheritance_when_idempotency_off(monkeypatch):
    """Defence-in-depth: with the idempotency feature flag disabled, the
    override-inheritance branch still suppresses row 5830."""
    import src.state_machine as sm
    from src import db

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params),
    )
    notifications: list[str] = []
    monkeypatch.setattr(
        "src.state_machine.notify_user_override",
        lambda msg: notifications.append(msg),
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "prod_replay.db"
        _setup_test(monkeypatch, path, prefire_enabled=False)
        now_utc = datetime(2026, 5, 21, 16, 36, 37, tzinfo=UTC)

        conn = db.get_connection()
        try:
            _insert_row(
                conn, aid=5828,
                start="2026-05-21T15:30:00Z",
                end="2026-05-21T16:30:00Z",
                action_type="tank_idle_overnight",
                status="completed",
                params={"tank_powerful": False, "lp_optimizer": True,
                        "tank_power": True, "tank_temp": 37},
                overridden_at="2026-05-21T15:40:51.873403+00:00",
            )
            _insert_row(
                conn, aid=5830,
                start="2026-05-21T16:30:00Z",
                end="2026-05-21T18:00:00Z",
                action_type="shutdown",
                status="pending",
                params={"tank_powerful": False, "lp_optimizer": True,
                        "tank_power": False},
            )
        finally:
            conn.close()

        # Important: NOT setting tank_powerful on this dev. Real prod
        # snapshots populate tank_powerful, but here we exercise the case
        # where tank_powerful is unknown — which forces
        # daikin_device_matches_params to return False on that field. That
        # makes the override-inheritance path engage (would_change_state=True).
        dev = DaikinDevice(id="gw", name="x", tank_on=False)
        client = MagicMock()

        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, client, dev, now_utc, trigger="heartbeat")

        # Defence in depth confirmed: when idempotency is off, override-
        # inheritance still suppresses row 5830 because (a) row 5828's
        # gesture (tank_on=False, contradicting overridden params tank_power=True)
        # is still in effect, and (b) the strict-comparator returns False
        # on the unknown tank_powerful field → would_change_state=True.
        assert len(apply_calls) == 0, (
            f"Override inheritance should fire when idempotency is off and "
            f"a recent user gesture exists. Got {len(apply_calls)} apply call(s)."
        )
        row_5830 = db.get_action_by_id(5830)
        assert row_5830 is not None
        assert row_5830.get("overridden_by_user_at") is not None, (
            "Row 5830 should inherit override from row 5828"
        )
        assert len(notifications) == 1
        assert "row 5828" in notifications[0]
