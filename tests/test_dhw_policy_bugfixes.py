"""K1.1 bug-fix regression tests.

These exercise the edge cases the initial K1 PR (#404) missed, found in
the code review by the Plan agent on 2026-05-23. Each test corresponds
to a specific bug:

* **Bug #1** — duplicate tank rows when LP runs after the daily warmup
  boundary. The widened clear range (today's warmup → day-after's warmup)
  must include the in-flight ``tank_warmup`` row's ``start_time`` so the
  upserter doesn't double-write.
* **Bug #2** — vacation flip mid-day must clear leftover pending rows
  (dhw_policy returns []; the clear must still run so the heartbeat
  doesn't fire a stale tank_warmup).
* **Bug #3** — DST-safe anchor construction. ``replace(hour=N)`` and
  ``+timedelta(days=1)`` are offset-blind; verify warmup/setback hours
  land correctly on transition days.
* **Bug #4** — early-morning negative-price slots (during overnight
  setback) must still trigger a boost. Fetch range must match
  ``generate_daily_tank_schedule``'s horizon, not calendar midnight.
* **Bug #5** — past-date guard returns [] instead of writing rows that
  immediately fire as stale.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src import dhw_policy
from src.config import config

TZ_LOCAL = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    _db.init_db()
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13, raising=False)
    monkeypatch.setattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_SETBACK_C", 37.0, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    yield


# ---------------------------------------------------------------------------
# Bug #5 — past-date guard
# ---------------------------------------------------------------------------


def test_past_date_returns_empty(monkeypatch):
    """``generate_daily_tank_schedule(yesterday)`` returns [] — past rows
    would fire immediately as stale, wasting write quota."""
    # Anchor today via the same _tz_local helper the module uses
    today = datetime.now(TZ_LOCAL).date()
    yesterday = today - timedelta(days=1)
    rows = dhw_policy.generate_daily_tank_schedule(yesterday, mode="normal")
    assert rows == []


def test_tomorrow_still_writes_rows(monkeypatch):
    """Future dates still write — advance scheduling is a valid use case."""
    today = datetime.now(TZ_LOCAL).date()
    tomorrow = today + timedelta(days=1)
    rows = dhw_policy.generate_daily_tank_schedule(tomorrow, mode="normal")
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Bug #4 — negative slot in early-morning setback window
# ---------------------------------------------------------------------------


def test_negative_slot_in_overnight_setback_window_detected():
    """A negative-price slot at 03:00 BST (= 02:00 UTC) — which falls in
    the OVERNIGHT SETBACK portion of the day's horizon — must still
    trigger a tank_negative_boost row. Pre-fix, this was silently
    dropped because the fetch range used calendar-day midnight."""
    today = datetime.now(TZ_LOCAL).date()
    # Negative slot at 02:00 UTC tomorrow = 03:00 BST tomorrow
    tomorrow_local = today + timedelta(days=1)
    neg_slot_utc = datetime(
        tomorrow_local.year, tomorrow_local.month, tomorrow_local.day,
        2, 0, tzinfo=UTC,
    ).isoformat().replace("+00:00", "Z")
    outgoing = [{"valid_from": neg_slot_utc, "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        today, agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert len(boost) == 1, (
        "Negative slot at 03:00 BST during setback should produce a boost row"
    )
    assert boost[0]["params"]["tank_temp"] == 60


def test_negative_slot_at_horizon_end_excluded():
    """A negative slot AT the next-warmup boundary (start of next horizon)
    belongs to the NEXT day's schedule, not today's. Test the boundary."""
    today = datetime.now(TZ_LOCAL).date()
    tomorrow_local = today + timedelta(days=1)
    # Negative at exactly 13:00 local tomorrow = next horizon start
    boundary_utc = datetime(
        tomorrow_local.year, tomorrow_local.month, tomorrow_local.day,
        13, 0, tzinfo=TZ_LOCAL,
    ).astimezone(UTC).isoformat().replace("+00:00", "Z")
    outgoing = [{"valid_from": boundary_utc, "value_inc_vat": -3.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        today, agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert boost == []  # at horizon end (exclusive); belongs to next day


# ---------------------------------------------------------------------------
# Bug #3 — DST-safe anchor construction
# ---------------------------------------------------------------------------


def test_spring_forward_day_emits_correct_local_hours():
    """On UK spring-forward (last Sunday of March 2027 = 2027-03-28),
    01:00 → 02:00 local skipped. Warmup at 13:00 local should still
    produce a row with that wall-clock hour — verify it's not 12:00 or 14:00.

    Use 2027 (future) to avoid the past-date guard added in bug #5.
    """
    spring_forward = date(2027, 3, 28)
    rows = dhw_policy.generate_daily_tank_schedule(spring_forward, mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    setback = next(r for r in rows if r["action_type"] == "tank_setback")

    warmup_start_local = datetime.fromisoformat(
        warmup["start_time"].replace("Z", "+00:00")
    ).astimezone(TZ_LOCAL)
    setback_start_local = datetime.fromisoformat(
        setback["start_time"].replace("Z", "+00:00")
    ).astimezone(TZ_LOCAL)
    setback_end_local = datetime.fromisoformat(
        setback["end_time"].replace("Z", "+00:00")
    ).astimezone(TZ_LOCAL)

    assert warmup_start_local.hour == 13
    assert setback_start_local.hour == 22
    # next warmup must be at 13:00 the FOLLOWING DAY (post-DST)
    assert setback_end_local.day == 29  # March 29, the next day
    assert setback_end_local.hour == 13


def test_fall_back_day_emits_correct_local_hours():
    """On UK fall-back (last Sunday of October 2026 = 2026-10-25),
    01:00 → 00:00 local repeated. Verify warmup/setback hours."""
    fall_back = date(2026, 10, 25)
    rows = dhw_policy.generate_daily_tank_schedule(fall_back, mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    setback = next(r for r in rows if r["action_type"] == "tank_setback")

    warmup_local = datetime.fromisoformat(
        warmup["start_time"].replace("Z", "+00:00")
    ).astimezone(TZ_LOCAL)
    setback_local = datetime.fromisoformat(
        setback["start_time"].replace("Z", "+00:00")
    ).astimezone(TZ_LOCAL)
    setback_end_local = datetime.fromisoformat(
        setback["end_time"].replace("Z", "+00:00")
    ).astimezone(TZ_LOCAL)

    assert warmup_local.hour == 13
    assert setback_local.hour == 22
    assert setback_end_local.day == 26
    assert setback_end_local.hour == 13


def test_warmup_to_setback_duration_normal_day():
    """On a regular (non-DST) day, warmup window is exactly 9h."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    s = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    e = datetime.fromisoformat(warmup["end_time"].replace("Z", "+00:00"))
    duration_h = (e - s).total_seconds() / 3600
    assert duration_h == 9.0


def test_setback_to_next_warmup_duration_normal_day():
    """Setback window is 15h on a regular day (22:00 → 13:00 next day)."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    setback = next(r for r in rows if r["action_type"] == "tank_setback")
    s = datetime.fromisoformat(setback["start_time"].replace("Z", "+00:00"))
    e = datetime.fromisoformat(setback["end_time"].replace("Z", "+00:00"))
    duration_h = (e - s).total_seconds() / 3600
    assert duration_h == 15.0


# ---------------------------------------------------------------------------
# Bug #1 + #2 — clear range / vacation orphan (via lp_dispatch integration)
# ---------------------------------------------------------------------------


def _seed_stale_warmup(start_iso: str, end_iso: str) -> int:
    """Seed an existing tank_warmup row for the clear test."""
    return _db.upsert_action(
        plan_date="2026-06-01",
        device="daikin", action_type="tank_warmup",
        start_time=start_iso, end_time=end_iso,
        params={"tank_temp": 45, "tank_power": True, "tank_powerful": False, "dhw_policy": True},
        status="pending",
    )


def _count_pending_rows(action_type: str | None = None) -> int:
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    if action_type:
        rows = conn.execute(
            "SELECT COUNT(*) FROM action_schedule WHERE device='daikin' "
            "AND status='pending' AND action_type=?", (action_type,)
        ).fetchone()
    else:
        rows = conn.execute(
            "SELECT COUNT(*) FROM action_schedule WHERE device='daikin' "
            "AND status='pending'"
        ).fetchone()
    return int(rows[0])


def test_lp_dispatch_does_not_duplicate_today_warmup(monkeypatch):
    """Bug #1 regression: LP solving at 14:00 BST (after today's 13:00
    warmup boundary) must NOT create a duplicate tank_warmup row.
    The widened clear range (today's warmup → day-after's warmup)
    catches the existing row before reinsertion.

    Uses a future date (2027-06-01) so the past-date guard doesn't trip.
    """
    from datetime import timezone as _tz
    from src.scheduler import lp_dispatch
    from src.scheduler.lp_optimizer import LpPlan

    # Seed yesterday's "today's warmup" — start 13:00 BST = 12:00 UTC
    warmup_today_start = datetime(2027, 6, 1, 12, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    warmup_today_end = datetime(2027, 6, 1, 21, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    _seed_stale_warmup(warmup_today_start, warmup_today_end)
    assert _count_pending_rows("tank_warmup") == 1

    # Stub out heavy LP infra — just enough for the new K1 branch to run
    monkeypatch.setattr(lp_dispatch, "_drop_legionella_window_pairs", lambda x: x)
    monkeypatch.setattr(lp_dispatch, "daikin_dispatch_preview", lambda p, f: [])
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)

    # LP plan with first slot = 14:00 BST = 13:00 UTC (after today's warmup boundary)
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [
        datetime(2027, 6, 1, 13, 30, tzinfo=_tz.utc) + timedelta(minutes=30 * i)
        for i in range(4)
    ]
    lp_dispatch.write_daikin_from_lp_plan("2027-06-01", plan, forecast=[])

    # After: exactly TWO tank_warmup rows (today + tomorrow), not three.
    n_warmup = _count_pending_rows("tank_warmup")
    assert n_warmup == 2, (
        f"Expected 2 tank_warmup rows (today + tomorrow), got {n_warmup} (dupe bug?)"
    )


def test_lp_dispatch_vacation_clears_pending_rows(monkeypatch):
    """Bug #2 regression: when mode flips to vacation, dhw_policy returns
    [] but the LP-side clear must STILL happen so any leftover pending
    rows don't fire from the heartbeat."""
    from datetime import timezone as _tz
    from src.scheduler import lp_dispatch
    from src.scheduler.lp_optimizer import LpPlan

    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(lp_dispatch, "_drop_legionella_window_pairs", lambda x: x)
    monkeypatch.setattr(lp_dispatch, "daikin_dispatch_preview", lambda p, f: [])

    # Seed a stale tank_warmup row in the future (it would fire when reached)
    warmup_start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    warmup_end = datetime(2026, 6, 1, 21, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    _seed_stale_warmup(warmup_start, warmup_end)
    assert _count_pending_rows("tank_warmup") == 1

    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [
        datetime(2026, 6, 1, 11, 30, tzinfo=_tz.utc) + timedelta(minutes=30 * i)
        for i in range(4)
    ]
    lp_dispatch.write_daikin_from_lp_plan("2026-06-01", plan, forecast=[])

    # Vacation: zero rows after the call
    assert _count_pending_rows("tank_warmup") == 0, (
        "Vacation mode must clear stale tank_warmup rows even though "
        "dhw_policy returns []"
    )


# ---------------------------------------------------------------------------
# Bug #6 — log message accuracy when vacation
# ---------------------------------------------------------------------------


def test_lp_dispatch_log_message_reports_row_count_only(monkeypatch, caplog):
    """Log should say 'wrote 0 rows' on vacation, not 'wrote 0 rows across 2 days'
    which was misleading. Just verify the count-only format is present."""
    import logging

    from datetime import timezone as _tz
    from src.scheduler import lp_dispatch
    from src.scheduler.lp_optimizer import LpPlan

    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(lp_dispatch, "_drop_legionella_window_pairs", lambda x: x)
    monkeypatch.setattr(lp_dispatch, "daikin_dispatch_preview", lambda p, f: [])

    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [
        datetime(2026, 6, 1, 11, 30, tzinfo=_tz.utc) + timedelta(minutes=30 * i)
        for i in range(4)
    ]
    with caplog.at_level(logging.INFO, logger="src.scheduler.lp_dispatch"):
        lp_dispatch.write_daikin_from_lp_plan("2026-06-01", plan, forecast=[])
    # Log line says wrote 0 rows, doesn't claim "across N days"
    msgs = [r.message for r in caplog.records]
    target = next((m for m in msgs if "DHW_FIXED_SCHEDULE" in m), "")
    assert "wrote 0 rows" in target
    assert "across" not in target  # bug #6 fix: removed misleading "across N days"


# ---------------------------------------------------------------------------
# Flag off — LP-driven path resumes
# ---------------------------------------------------------------------------


def test_disabling_flag_resumes_lp_driven_path(monkeypatch, caplog):
    """When DHW_FIXED_SCHEDULE_ENABLED=False, the new branch is bypassed —
    no 'DHW_FIXED_SCHEDULE' log line is emitted."""
    import logging

    from datetime import timezone as _tz
    from src.scheduler import lp_dispatch
    from src.scheduler.lp_optimizer import LpPlan

    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(lp_dispatch, "_drop_legionella_window_pairs", lambda x: x)
    monkeypatch.setattr(lp_dispatch, "daikin_dispatch_preview", lambda p, f: [])

    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [
        datetime(2026, 6, 1, 11, 30, tzinfo=_tz.utc) + timedelta(minutes=30 * i)
        for i in range(4)
    ]
    with caplog.at_level(logging.INFO, logger="src.scheduler.lp_dispatch"):
        lp_dispatch.write_daikin_from_lp_plan("2026-06-01", plan, forecast=[])
    msgs = [r.message for r in caplog.records]
    assert not any("DHW_FIXED_SCHEDULE" in m for m in msgs), (
        f"Should not log DHW_FIXED_SCHEDULE when flag off; got {msgs}"
    )
