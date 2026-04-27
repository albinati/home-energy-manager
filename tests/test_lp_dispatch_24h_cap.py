"""Fox V3 dispatcher only ships the first 24 h of the LP plan.

Fox V3 scheduler is daily-cyclic (each group has hour/minute, no date — repeats
every day). With the 48 h LP horizon (S10.2 / #169), naively dispatching all
slots created groups for D+1 actions that shared an hour-of-day with D+0 actions
— resulting in overlapping/duplicate Fox groups visible in the Fox app. This
test guards the cutoff so the 24 h dispatch contract holds even as the LP
horizon grows.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.scheduler.lp_dispatch import build_fox_groups_from_lp
from src.scheduler.lp_optimizer import LpPlan


def _build_48h_plan() -> LpPlan:
    """96 half-hour slots = 48 h. Charge windows at the same UTC hour today and
    tomorrow (would collide on Fox V3 if we dispatched both)."""
    t0 = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    starts = [t0 + i * timedelta(minutes=30) for i in range(96)]
    n = 96
    # Charge slot 0 (today 14:00) AND slot 48 (tomorrow 14:00) — same UTC hour
    chg = [0.0] * n
    imp = [0.0] * n
    for i in (0, 1, 48, 49):
        chg[i] = 0.15
        imp[i] = 0.2
    return LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=starts,
        price_pence=[10.0] * n,
        import_kwh=imp,
        export_kwh=[0.0] * n,
        battery_charge_kwh=chg,
        battery_discharge_kwh=[0.0] * n,
        pv_use_kwh=[0.0] * n,
        pv_curtail_kwh=[0.0] * n,
        dhw_electric_kwh=[0.0] * n,
        space_electric_kwh=[0.0] * n,
        soc_kwh=[5.0] * (n + 1),
        peak_threshold_pence=30.0,
    )


def test_dispatcher_caps_at_24h_so_no_dst_overlap() -> None:
    """The D+1 charge slot at 14:00 must NOT appear in Fox groups —
    it would collide with the D+0 charge slot at 14:00 (same UTC hour)
    on the daily-cyclic Fox V3 scheduler."""
    plan = _build_48h_plan()
    groups, _replan = build_fox_groups_from_lp(plan)

    # The plan has TWO same-hour-of-day ForceCharge slots (today 14 UTC, tomorrow
    # 14 UTC). With the 24 h cap, only D+0's appears in groups — Fox can't see two
    # ForceCharge groups at the same hour without rejecting/overlapping.
    fc_groups = [g for g in groups if g.work_mode == "ForceCharge"]
    assert len(fc_groups) == 1, (
        f"expected exactly 1 ForceCharge group (D+0 only); got {len(fc_groups)} — "
        f"Fox V3 would see same-hour-of-day overlap. "
        f"All groups: {[(g.start_hour, g.start_minute, g.work_mode) for g in groups]}"
    )
    # Confirm no two groups share the same start hour-of-day
    starts_by_hour: dict[tuple[int, int], int] = {}
    for g in groups:
        key = (g.start_hour, g.start_minute)
        starts_by_hour[key] = starts_by_hour.get(key, 0) + 1
    duplicates = {k: v for k, v in starts_by_hour.items() if v > 1}
    assert not duplicates, (
        f"duplicate hour-of-day starts in Fox groups (would overlap on daily-cyclic scheduler): "
        f"{duplicates}"
    )


def test_24h_cap_does_not_drop_groups_when_horizon_fits() -> None:
    """If the LP plan is already <= 24 h, the cap is a no-op."""
    t0 = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    starts = [t0 + i * timedelta(minutes=30) for i in range(48)]  # exactly 24 h
    chg = [0.0] * 48
    chg[0] = 0.15  # one ForceCharge slot
    imp = [0.0] * 48
    imp[0] = 0.2
    plan = LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=starts,
        price_pence=[10.0] * 48,
        import_kwh=imp,
        export_kwh=[0.0] * 48,
        battery_charge_kwh=chg,
        battery_discharge_kwh=[0.0] * 48,
        pv_use_kwh=[0.0] * 48,
        pv_curtail_kwh=[0.0] * 48,
        dhw_electric_kwh=[0.0] * 48,
        space_electric_kwh=[0.0] * 48,
        soc_kwh=[5.0] * 49,
        peak_threshold_pence=30.0,
    )
    groups, _ = build_fox_groups_from_lp(plan)
    assert any(g.work_mode == "ForceCharge" for g in groups), (
        "the single ForceCharge slot at the start of the plan should still be in the dispatched groups"
    )
