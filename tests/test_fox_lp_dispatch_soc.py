"""LP → Fox dispatch: target SoC and minSocOnGrid alignment."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.config import config
from src.scheduler.lp_dispatch import lp_plan_to_slots
from src.scheduler.lp_optimizer import LpPlan
from src.scheduler.optimizer import HalfHourSlot, _merge_fox_groups, _slot_fox_tuple


def _min_r() -> int:
    return int(config.MIN_SOC_RESERVE_PERCENT)


def test_lp_plan_to_slots_sets_target_soc_pct_from_soc_kwh() -> None:
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    plan = LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=[t0, t0 + timedelta(minutes=30)],
        price_pence=[5.0, 5.0],
        import_kwh=[0.2, 0.2],
        export_kwh=[0.0, 0.0],
        battery_charge_kwh=[0.15, 0.1],
        battery_discharge_kwh=[0.0, 0.0],
        pv_use_kwh=[0.0, 0.0],
        pv_curtail_kwh=[0.0, 0.0],
        dhw_electric_kwh=[0.0, 0.0],
        space_electric_kwh=[0.0, 0.0],
        soc_kwh=[3.0, 6.2, 7.0],
        peak_threshold_pence=30.0,
    )
    cap = float(config.BATTERY_CAPACITY_KWH)
    slots = lp_plan_to_slots(plan)
    assert len(slots) == 2
    assert slots[0].target_soc_pct == max(
        _min_r(), min(100, int(round(plan.soc_kwh[1] / cap * 100.0))))
    assert slots[1].target_soc_pct == max(
        _min_r(), min(100, int(round(plan.soc_kwh[2] / cap * 100.0))))


def test_slot_fox_tuple_force_charge_uses_target_and_reserve() -> None:
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        price_pence=3.0,
        kind="cheap",
        lp_grid_import_w=1500,
        target_soc_pct=62,
    )
    wm, fds, pwr, msg = _slot_fox_tuple(s)
    assert wm == "ForceCharge"
    assert fds == 62
    assert pwr == 1500
    assert msg == _min_r()


def test_slot_fox_tuple_cheap_fallback_when_no_target() -> None:
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        price_pence=3.0,
        kind="cheap",
        target_soc_pct=None,
    )
    wm, fds, pwr, msg = _slot_fox_tuple(s)
    assert fds == 95
    assert msg == _min_r()


def test_merge_adjacent_force_charge_uses_max_fd_soc_and_min_soc() -> None:
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    s1 = HalfHourSlot(
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        price_pence=4.0,
        kind="cheap",
        lp_grid_import_w=1000,
        target_soc_pct=62,
    )
    s2 = HalfHourSlot(
        start_utc=t0 + timedelta(minutes=30),
        end_utc=t0 + timedelta(hours=1),
        price_pence=4.0,
        kind="cheap",
        lp_grid_import_w=1200,
        target_soc_pct=70,
    )
    groups = _merge_fox_groups([s1, s2])
    assert len(groups) == 1
    assert groups[0].work_mode == "ForceCharge"
    assert groups[0].fd_soc == 70
    assert groups[0].fd_pwr == 1200
    assert groups[0].min_soc_on_grid == _min_r()


def test_slot_fox_tuple_peak_export_uses_reserve_min_soc() -> None:
    t0 = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        price_pence=35.0,
        kind="peak_export",
    )
    wm, fds, pwr, msg = _slot_fox_tuple(s)
    assert wm == "ForceDischarge"
    assert msg == _min_r()


def test_slot_fox_tuple_standard_selfuse_uses_reserve_min_soc() -> None:
    t0 = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        price_pence=12.0,
        kind="standard",
    )
    wm, fds, pwr, msg = _slot_fox_tuple(s)
    assert wm == "SelfUse"
    assert msg == _min_r()
