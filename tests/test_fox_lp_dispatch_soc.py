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
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
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
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
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
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
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
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
    assert wm == "SelfUse"
    assert msg == _min_r()


def test_solar_charge_emits_solar_sponge_max_soc_100() -> None:
    """A solar_charge slot must emit SelfUse + minSoc=100 + maxSoc=100 — the
    canonical Fox V3 'Solar Sponge' shape so the firmware never tops via grid past 100 %."""
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        price_pence=10.0,
        kind="solar_charge",
    )
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
    assert wm == "SelfUse"
    assert fds is None
    assert pwr is None
    assert msg == 100
    assert max_soc == 100
    # Confirm propagation through the merge pipeline + into the API payload.
    groups = _merge_fox_groups([s])
    assert len(groups) == 1
    assert groups[0].max_soc == 100
    assert groups[0].to_api_dict()["extraParam"]["maxSoc"] == 100


def test_non_solar_charge_kinds_have_no_max_soc() -> None:
    """ForceCharge / ForceDischarge / Backup / standard SelfUse must NOT emit maxSoc —
    only solar_charge does. Defensive against accidentally capping other modes."""
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    for kind in ("cheap", "negative", "standard", "negative_hold", "peak_export"):
        s = HalfHourSlot(
            start_utc=t0,
            end_utc=t0 + timedelta(minutes=30),
            price_pence=5.0,
            kind=kind,
            lp_grid_import_w=1000 if kind in ("cheap", "negative") else None,
            target_soc_pct=80 if kind in ("cheap", "negative") else None,
        )
        _, _, _, _, max_soc = _slot_fox_tuple(s)
        assert max_soc is None, f"{kind} must NOT set max_soc, got {max_soc}"


def test_negative_hold_kind_when_battery_full_and_price_negative() -> None:
    """When LP has chg≈0 during a negative-price slot (battery saturated), the
    dispatcher must emit kind='negative_hold' — not 'standard'. This maps to
    Fox Backup mode to prevent battery discharge while grid is paying us to
    import (see #57).
    """
    t0 = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    cap = float(config.BATTERY_CAPACITY_KWH)
    plan = LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=-5.0,
        slot_starts_utc=[t0, t0 + timedelta(minutes=30)],
        price_pence=[-1.3, -0.9],
        import_kwh=[0.3, 0.3],  # grid covers base load
        export_kwh=[0.0, 0.0],
        battery_charge_kwh=[0.0, 0.0],  # battery already full
        battery_discharge_kwh=[0.0, 0.0],  # Fix B
        pv_use_kwh=[0.0, 0.0],
        pv_curtail_kwh=[0.0, 0.0],
        dhw_electric_kwh=[0.0, 0.0],
        space_electric_kwh=[0.0, 0.0],
        soc_kwh=[cap, cap, cap],  # fully saturated
        peak_threshold_pence=30.0,
    )
    slots = lp_plan_to_slots(plan)
    assert len(slots) == 2
    for i, s in enumerate(slots):
        assert s.kind == "negative_hold", (
            f"slot {i} (price={plan.price_pence[i]}p, chg=0, full SoC): kind={s.kind!r}"
        )
    wm, fds, pwr, _, _ = _slot_fox_tuple(slots[0])
    assert wm == "Backup", f"negative_hold must map to Fox Backup mode, got {wm!r}"
    assert fds is None
    assert pwr is None


def test_negative_hold_merges_adjacent_with_same_params() -> None:
    """Adjacent negative_hold slots must collapse into one Backup group so the
    Fox schedule stays within the 8-group limit during long plunge windows.
    """
    t0 = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    slots = [
        HalfHourSlot(
            start_utc=t0 + timedelta(minutes=30 * i),
            end_utc=t0 + timedelta(minutes=30 * (i + 1)),
            price_pence=-1.0,
            kind="negative_hold",
        )
        for i in range(4)
    ]
    groups = _merge_fox_groups(slots)
    assert len(groups) == 1
    assert groups[0].work_mode == "Backup"
