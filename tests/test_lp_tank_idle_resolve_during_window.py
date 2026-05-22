"""``tank_idle_overnight`` classification must survive an LP re-solve that
fires AFTER the day's last shower window has already ended.

Issue #323: when the LP runs ``lp_plan_to_slots`` on a horizon whose FIRST
slot starts past the latest shower-window end of the day, the
``seen_shower_in_window`` tracker never flipped True (no shower slot in
the horizon to trigger it), so the post-shower overnight slots stayed
``standard`` and the dispatcher emitted no ``tank_idle_overnight`` action.
Result: tank held whatever the previous restore left (~45 °C) all night,
foregoing the modest standing-loss saving the idle setback provides.

This file pins the fix: pre-arm ``seen_shower_in_window`` when slot 0's
local clock position falls inside the wraparound idle window between the
schedule's latest shower-end and earliest next-day shower-start.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src.config import config as app_config
from src.scheduler.lp_dispatch import lp_plan_to_slots
from src.scheduler.lp_optimizer import LpPlan


@pytest.fixture(autouse=True)
def _init_db() -> None:
    _db.init_db()


@pytest.fixture(autouse=True)
def _idle_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "DHW_TANK_OVERNIGHT_IDLE_ENABLED", "true")
    # Two-shower schedule: morning + evening (the prod default shape).
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "06:00-07:30,21:30-22:30")
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE_GUESTS", "")
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "normal")
    # PR C — ENERGY_STRATEGY_MODE removed (was here).


def _build_plan(
    base_utc: datetime,
    *,
    n_slots: int,
    kind_overrides: dict[int, str] | None = None,
) -> LpPlan:
    """Build an LpPlan with all-standard slots; kind classification is
    derived by ``lp_plan_to_slots`` from prices + chg/dis/exp. Default
    here: price=12 (standard), no charge / discharge / export."""
    starts = [base_utc + i * timedelta(minutes=30) for i in range(n_slots)]
    plan = LpPlan(
        ok=True, status="Optimal", objective_pence=0.0,
        peak_threshold_pence=18.0, cheap_threshold_pence=8.0,
    )
    plan.slot_starts_utc = list(starts)
    plan.price_pence = [12.0] * n_slots
    plan.import_kwh = [0.3] * n_slots
    plan.export_kwh = [0.0] * n_slots
    plan.battery_charge_kwh = [0.0] * n_slots
    plan.battery_discharge_kwh = [0.0] * n_slots
    plan.pv_use_kwh = [0.0] * n_slots
    plan.pv_curtail_kwh = [0.0] * n_slots
    plan.dhw_electric_kwh = [0.0] * n_slots
    plan.space_electric_kwh = [0.0] * n_slots
    plan.lwt_offset_c = [0.0] * n_slots
    plan.tank_temp_c = [45.0] * (n_slots + 1)
    plan.soc_kwh = [5.0] * (n_slots + 1)
    plan.temp_outdoor_c = [10.0] * n_slots
    return plan


# ---------------------------------------------------------------------------
# THE BUG: solve fires after evening shower ended; first slot starts mid-idle.
# ---------------------------------------------------------------------------

def test_horizon_starting_after_evening_shower_marks_idle() -> None:
    """Bug reproducer: LP solves at 21:25 BST (mid-idle window since shower
    ended at 21:30 BST is now in the past — wait, 21:30 BST is the START so
    the END is 22:30 BST). Solve at 22:25 BST → horizon starts 22:30 BST
    (= 21:30 UTC). Slot 0 midpoint 22:45 BST = 1365 min, past the
    1350-min (22:30 BST) shower end. Must mark slots tank_idle_overnight."""
    # Slot 0 starts at 21:30 UTC = 22:30 BST. Two-shower schedule has
    # evening 21:30-22:30 BST = 20:30-21:30 UTC.
    base = datetime(2026, 5, 19, 21, 30, tzinfo=UTC)  # 22:30 BST
    plan = _build_plan(base, n_slots=10)  # 5 h window, 22:30-03:30 BST

    slots = lp_plan_to_slots(plan)
    assert slots, "no slots returned"

    # Every slot in this horizon should be marked tank_idle_overnight
    # (post-shower, no productive reset hit, all standard kind).
    kinds = [s.kind for s in slots]
    assert all(k == "tank_idle_overnight" for k in kinds), (
        f"expected all slots tank_idle_overnight, got: {kinds}"
    )


def test_horizon_starting_in_post_midnight_idle_marks_idle() -> None:
    """Edge case: solve at 02:00 BST. Idle window wraps midnight (yesterday's
    22:30 → today's 06:00). Slot 0 midpoint 02:15 BST = 135 min, BEFORE
    today's morning shower start (06:00 = 360 min). Must mark idle."""
    base = datetime(2026, 5, 20, 1, 0, tzinfo=UTC)  # 02:00 BST
    plan = _build_plan(base, n_slots=6)  # 02:00-05:00 BST

    slots = lp_plan_to_slots(plan)
    kinds = [s.kind for s in slots]
    assert all(k == "tank_idle_overnight" for k in kinds), (
        f"expected all idle (pre-morning-shower wraparound), got: {kinds}"
    )


# ---------------------------------------------------------------------------
# Behavior preservation: pre-fix correctness for cases that already worked.
# ---------------------------------------------------------------------------

def test_horizon_starting_before_evening_shower_no_pre_arm() -> None:
    """Solve at 18:00 BST (= 17:00 UTC). Slot 0 midpoint 18:15 BST = 1095
    min, before the evening shower (1290 min). Pre-arm must NOT fire;
    slots stay standard until the shower slot is reached in the loop."""
    base = datetime(2026, 5, 19, 17, 0, tzinfo=UTC)  # 18:00 BST
    plan = _build_plan(base, n_slots=4)  # 18:00-20:00 BST (no shower in horizon)

    slots = lp_plan_to_slots(plan)
    kinds = [s.kind for s in slots]
    assert all(k == "standard" for k in kinds), (
        f"expected all standard (before evening shower), got: {kinds}"
    )


def test_horizon_spans_evening_shower_then_idle_then_morning() -> None:
    """Full overnight: starts 19:00 BST, spans evening shower (21:30-22:30),
    then idle through to morning shower (06:00-07:30). Loop should mark
    pre-shower slots standard, shower slots not-marked (default), and
    post-shower-pre-morning-shower slots idle."""
    base = datetime(2026, 5, 19, 18, 0, tzinfo=UTC)  # 19:00 BST
    # n=22 ends at 06:00 BST exactly (just before morning shower starts).
    plan = _build_plan(base, n_slots=22)  # 11 h, 19:00 → 06:00 BST

    slots = lp_plan_to_slots(plan)
    kinds = [s.kind for s in slots]
    # First few slots (19:00-21:30): no pre-arm + no shower yet → standard.
    assert kinds[0] == "standard"
    assert kinds[1] == "standard"
    # Slots covering shower window (around index 5-6 = 21:30-22:30 BST):
    # the shower_mask filter skips them in the idle-marking loop, so they
    # keep their default classification (standard) — but the FLAG flips
    # for subsequent slots.
    # Subsequent slots (after evening shower) should be tank_idle_overnight.
    # Slot 8 = 23:00 BST midpoint 23:15 — definitely post-shower.
    assert kinds[8] == "tank_idle_overnight", f"slot 8 (23:00 BST): {kinds[8]}"
    # Final pre-morning-shower slot (~05:30 BST): still idle.
    assert kinds[-1] == "tank_idle_overnight", f"last slot: {kinds[-1]}"


def test_idle_disabled_returns_all_standard(monkeypatch: pytest.MonkeyPatch) -> None:
    """``DHW_TANK_OVERNIGHT_IDLE_ENABLED=false`` short-circuits the whole pass."""
    monkeypatch.setattr(app_config, "DHW_TANK_OVERNIGHT_IDLE_ENABLED", "false")
    base = datetime(2026, 5, 19, 21, 30, tzinfo=UTC)
    plan = _build_plan(base, n_slots=6)
    slots = lp_plan_to_slots(plan)
    assert all(s.kind == "standard" for s in slots)


def test_morning_only_schedule_no_wraparound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schedule with only a morning shower (no evening). After morning
    shower the tank idles all day until productive slot. Pre-arm should
    fire if slot 0 is post-morning-shower without wraparound."""
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "06:00-07:30")
    # Slot 0 at 10:00 BST (post morning shower).
    base = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)  # 10:00 BST
    plan = _build_plan(base, n_slots=4)
    slots = lp_plan_to_slots(plan)
    # With latest_end (450) > earliest_start (360) it's a wraparound case.
    # Slot 0 mid = 615 → >= 450 → in_idle → pre-arm. Idle.
    assert all(s.kind == "tank_idle_overnight" for s in slots), (
        f"morning-only schedule, post-shower slot must be idle: {[s.kind for s in slots]}"
    )
