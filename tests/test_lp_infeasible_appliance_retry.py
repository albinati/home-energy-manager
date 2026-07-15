"""When ``_run_optimizer_lp`` returns Infeasible AND the base_load had a
non-zero appliance contribution, the optimizer must retry once with the
appliance kWh dropped. If that retry is Optimal the appliance-blind plan
ships (the APScheduler cron is untouched — the appliance still fires at
its planned time). If the retry is still Infeasible, behaviour falls
through to the existing PR #338 held-schedule defensive path.

Background: 2026-05-19 prod incident at 21:25 BST. LP went Infeasible on
the tier_boundary MPC trigger. The only state change vs the previous
(Optimal) solve at 18:55 was the appliance dispatch arming a washing
machine for Wed 02:00 (+0.75 kWh across 3 cheap-overnight slots). PR-A
captured the inputs; this PR makes the optimizer self-heal.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src import db
from src.config import config as app_config
from src.scheduler import optimizer
from src.scheduler.lp_optimizer import LpPlan

TARIFF = "E-1R-AGILE-TEST-APPLIANCE-RETRY"


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _lp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(app_config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", True)
    monkeypatch.setattr(app_config, "APPLIANCE_DISPATCH_ENABLED", True)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_realistic_day(start: datetime) -> None:
    rows = []
    vf = start
    for _ in range(48):
        if 1 <= vf.hour < 5:
            price = -2.0
        elif 5 <= vf.hour < 16:
            price = 10.0
        elif 16 <= vf.hour < 19:
            price = 35.0
        else:
            price = 12.0
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, TARIFF)


def _seed_armed_washer(*, planned_start: datetime, duration_min: int = 90) -> int:
    """Insert a washer + scheduled job covering ``[planned_start, +duration)``.
    Returns the appliance id. Mirrors what appliance_dispatch.reconcile would
    write after picking the cheapest contiguous window.
    """
    appliance_id = db.add_appliance(
        vendor="smartthings",
        vendor_device_id="test-washer-1",
        name="Washing machine",
        device_type="washer",
        default_duration_minutes=duration_min,
        deadline_local_time="07:00",
        typical_kw=0.5,
        enabled=True,
    )
    planned_end = planned_start + timedelta(minutes=duration_min)
    deadline = planned_start + timedelta(hours=8)
    db.create_appliance_job(
        appliance_id=appliance_id,
        armed_at_utc=_iso(planned_start - timedelta(hours=1)),
        deadline_utc=_iso(deadline),
        duration_minutes=duration_min,
        planned_start_utc=_iso(planned_start),
        planned_end_utc=_iso(planned_end),
        avg_price_pence=-1.5,
        status="scheduled",
    )
    return appliance_id


class _TwoPhaseSolver:
    """First call → Infeasible. Second call → Optimal. Records the
    ``base_load_kwh`` arg of each call so the test can assert the retry was
    invoked with the appliance contribution removed.
    """

    def __init__(self) -> None:
        self.calls: list[list[float]] = []

    def __call__(
        self,
        *,
        slot_starts_utc: list[datetime],
        price_pence: list[float],
        base_load_kwh: list[float],
        **_kwargs: Any,
    ) -> LpPlan:
        self.calls.append(list(base_load_kwh))
        if len(self.calls) == 1:
            plan = LpPlan(
                ok=False, status="Infeasible", objective_pence=0.0,
                peak_threshold_pence=18.0, cheap_threshold_pence=8.0,
            )
            plan.slot_starts_utc = list(slot_starts_utc)
            plan.price_pence = list(price_pence)
            plan.temp_outdoor_c = [12.0] * len(slot_starts_utc)
            return plan
        # Second call: realistic Optimal plan shape. Per-slot vectors length N;
        # state vectors length N+1.
        n = len(slot_starts_utc)
        plan = LpPlan(
            ok=True, status="Optimal", objective_pence=42.0,
            peak_threshold_pence=18.0, cheap_threshold_pence=8.0,
        )
        plan.slot_starts_utc = list(slot_starts_utc)
        plan.price_pence = list(price_pence)
        plan.import_kwh = [0.0] * n
        plan.export_kwh = [0.0] * n
        plan.battery_charge_kwh = [0.0] * n
        plan.battery_discharge_kwh = [0.0] * n
        plan.pv_use_kwh = [0.0] * n
        plan.pv_curtail_kwh = [0.0] * n
        plan.dhw_electric_kwh = [0.0] * n
        plan.space_electric_kwh = [0.0] * n
        plan.lwt_offset_c = [0.0] * n
        plan.tank_temp_c = [45.0] * (n + 1)
        plan.soc_kwh = [5.0] * (n + 1)
        plan.temp_outdoor_c = [12.0] * n
        return plan


def _today_utc() -> datetime:
    """Midnight UTC of the REAL current day.

    #383: these fixtures were pinned to 2026-04-22, but the horizon extender's
    priors query (``db.get_half_hourly_agile_priors``) cuts off at
    ``datetime.now(UTC) - 28d`` on the REAL clock. Once the pinned date aged out,
    the priors came back EMPTY, the horizon truncated to ~5 h, and the seeded
    D+1 appliance at 01:30 fell OUTSIDE it — so ``appliance_kwh_total`` was zero,
    the retry guard never fired, and the test saw 1 solve instead of 2.
    The retry code was correct all along.
    """
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def test_lp_infeasible_with_appliance_retries_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First solve Infeasible, second solve Optimal — the second solve must
    have been called with the appliance kWh removed from ``base_load_kwh``.
    The optimizer must then proceed down the Optimal path (no held-schedule
    return) and the strategy_summary must surface the appliance exclusion.
    """
    today = _today_utc()
    now = today + timedelta(hours=18)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(today)
    # Place the washer inside the LP horizon (next 48 h from now=18:00 UTC).
    _seed_armed_washer(
        planned_start=today + timedelta(days=1, hours=1, minutes=30),
        duration_min=90,
    )

    # The LP-owned economic shadow (#714) re-solves the committed inputs and would count
    # as an extra solve_lp call — it is orthogonal to the infeasibility-retry this test
    # checks, so switch it off here.
    monkeypatch.setattr(app_config, "DHW_LP_OWNED_SHADOW_ENABLED", False, raising=False)

    solver = _TwoPhaseSolver()
    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", solver)

    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result.get("ok") is True, f"expected Optimal after retry, got {result}"
    assert len(solver.calls) == 2, (
        f"expected exactly 2 solve_lp calls (Infeasible + retry), "
        f"got {len(solver.calls)}"
    )

    first_total = sum(solver.calls[0])
    second_total = sum(solver.calls[1])
    assert first_total > second_total + 1e-6, (
        f"retry base_load was not reduced: first={first_total:.3f} "
        f"second={second_total:.3f}"
    )
    # Specifically, the difference should match the appliance contribution
    # (0.5 kW × 0.5 h × 3 slots = 0.75 kWh; the planned window spans 3 half-
    # hour slots starting at 01:30).
    delta = first_total - second_total
    assert 0.6 <= delta <= 0.9, (
        f"appliance-drop delta out of expected range: {delta:.3f} kWh "
        f"(expected ~0.75 kWh)"
    )

    # The strategy_summary on the daily_target row must note the exclusion.
    target = db.get_daily_target((today + timedelta(days=1)).date().isoformat())
    if target is None:
        target = db.get_daily_target(today.date().isoformat())
    assert target is not None, "no daily_target row written after retry"
    assert "appliance" in (target.get("strategy_summary") or "").lower(), (
        f"strategy_summary missing appliance-exclusion note: "
        f"{target.get('strategy_summary')!r}"
    )


def test_lp_infeasible_with_appliance_double_fail_falls_through_to_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If BOTH the original solve and the appliance-drop retry return
    Infeasible, the optimizer must fall through to the held-schedule path
    (no Fox upload) and the audit row must mention that the appliance retry
    was also Infeasible. This is the "appliance isn't the cause" branch.
    """
    today = _today_utc()
    now = today + timedelta(hours=18)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(today)
    _seed_armed_washer(
        planned_start=today + timedelta(days=1, hours=1, minutes=30),
        duration_min=90,
    )

    calls: list[list[float]] = []

    def _always_infeasible(
        *,
        slot_starts_utc: list[datetime],
        price_pence: list[float],
        base_load_kwh: list[float],
        **_kwargs: Any,
    ) -> LpPlan:
        calls.append(list(base_load_kwh))
        plan = LpPlan(
            ok=False, status="Infeasible", objective_pence=0.0,
            peak_threshold_pence=18.0, cheap_threshold_pence=8.0,
        )
        plan.slot_starts_utc = list(slot_starts_utc)
        plan.price_pence = list(price_pence)
        plan.temp_outdoor_c = [12.0] * len(slot_starts_utc)
        return plan

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _always_infeasible)

    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result.get("ok") is False
    assert result.get("fallback") == "hold_previous_schedule"
    assert len(calls) == 2, (
        f"expected 2 solve_lp attempts (original + appliance-drop retry), "
        f"got {len(calls)}"
    )

    # And the optimizer_log strategy_summary must surface that the retry
    # also failed — so the audit script can distinguish "appliance was the
    # cause" from "something else was the cause".
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT strategy_summary FROM optimizer_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        summary = (row["strategy_summary"] or "").lower()
        assert "infeasible" in summary
        assert "appliance-drop retry also infeasible" in summary, (
            f"strategy_summary doesn't tag the failed appliance retry: "
            f"{row['strategy_summary']!r}"
        )
    finally:
        conn.close()


def test_lp_infeasible_without_appliance_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When there is no armed appliance contributing to base_load, the
    optimizer must NOT call solve_lp a second time. The retry is a targeted
    intervention for appliance-induced infeasibility, not a generic re-try
    loop.
    """
    today = _today_utc()
    now = today + timedelta(hours=18)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(today)
    # No appliance seeded — appliance_load_profile_kw returns {}.

    calls: list[int] = []

    def _record_call(
        *,
        slot_starts_utc: list[datetime],
        price_pence: list[float],
        **_kwargs: Any,
    ) -> LpPlan:
        calls.append(1)
        plan = LpPlan(
            ok=False, status="Infeasible", objective_pence=0.0,
            peak_threshold_pence=18.0, cheap_threshold_pence=8.0,
        )
        plan.slot_starts_utc = list(slot_starts_utc)
        plan.price_pence = list(price_pence)
        plan.temp_outdoor_c = [12.0] * len(slot_starts_utc)
        return plan

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _record_call)

    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result.get("ok") is False
    assert result.get("fallback") == "hold_previous_schedule"
    assert len(calls) == 1, (
        f"expected exactly 1 solve_lp call when no appliance was armed, "
        f"got {len(calls)} (retry should be gated on appliance presence)"
    )
