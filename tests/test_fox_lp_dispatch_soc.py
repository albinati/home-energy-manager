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


def test_slot_fox_tuple_negative_hold_pins_maxsoc_to_floor(monkeypatch) -> None:
    """Backup path (default since 2026-07-04): the optional 2026-06-07 pin
    sets maxSoc = reserve floor so solar/grid can't charge the battery
    during the hold (when the pin is enabled; default off)."""
    monkeypatch.setattr(config, "LP_NEGATIVE_HOLD_FOX_MODE", "backup", raising=False)
    monkeypatch.setattr(config, "LP_NEGATIVE_HOLD_PIN_MAXSOC", True, raising=False)
    t0 = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0, end_utc=t0 + timedelta(minutes=30),
        price_pence=-4.0, kind="negative_hold",
    )
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
    assert wm == "Backup"
    assert msg == _min_r()
    assert max_soc == _min_r()  # pinned at the floor → no solar charge above it


def test_slot_fox_tuple_negative_hold_maxsoc_none_when_disabled(monkeypatch) -> None:
    """Default Backup: maxSoc stays None when the pin is off, so the
    firmware may top the battery up toward full from the PAID grid."""
    monkeypatch.setattr(config, "LP_NEGATIVE_HOLD_FOX_MODE", "backup", raising=False)
    monkeypatch.setattr(config, "LP_NEGATIVE_HOLD_PIN_MAXSOC", False, raising=False)
    t0 = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0, end_utc=t0 + timedelta(minutes=30),
        price_pence=-4.0, kind="negative_hold",
    )
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
    assert wm == "Backup"
    assert max_soc is None


def test_slot_fox_tuple_negative_hold_forcecharge_holds_at_target(monkeypatch) -> None:
    """Fallback mode (LP_NEGATIVE_HOLD_FOX_MODE=forcecharge, the #607/#630
    interim): negative_hold dispatches as ForceCharge-to-the-LP-target so the
    battery never discharges — load grid-fed at the paid negative rate."""
    monkeypatch.setattr(config, "LP_NEGATIVE_HOLD_FOX_MODE", "forcecharge", raising=False)
    t0 = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0, end_utc=t0 + timedelta(minutes=30),
        price_pence=-4.0, kind="negative_hold",
        target_soc_pct=_min_r(),
    )
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
    assert wm == "ForceCharge", "negative_hold must NEVER discharge → ForceCharge"
    assert fds == _min_r()  # hold at the LP's planned target (~reserve)
    assert pwr == config.FOX_FORCE_CHARGE_MAX_PWR  # no per-slot import → max fallback
    assert msg == _min_r()
    assert max_soc is None


def test_slot_fox_tuple_negative_hold_forcecharge_uses_lp_import_power(monkeypatch) -> None:
    """forcecharge fallback: the LP per-slot import power is used as fdPwr."""
    monkeypatch.setattr(config, "LP_NEGATIVE_HOLD_FOX_MODE", "forcecharge", raising=False)
    t0 = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
    s = HalfHourSlot(
        start_utc=t0, end_utc=t0 + timedelta(minutes=30),
        price_pence=-4.0, kind="negative_hold",
        lp_grid_import_w=1800, target_soc_pct=40,
    )
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(s)
    assert wm == "ForceCharge"
    assert fds == 40
    assert pwr == 1800


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


def test_merge_adjacent_force_charge_uses_duration_weighted_avg_pwr() -> None:
    """Two equal-length slots → duration-weighted avg = arithmetic mean of pwr.

    fd_soc and min_soc_on_grid still take MAX (charge to highest target,
    enforce highest reserve). Only fd_pwr is averaged.
    """
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
    assert groups[0].fd_pwr == 1100  # weighted avg of 1000 & 1200 over equal durations
    assert groups[0].min_soc_on_grid == _min_r()


def test_merge_adjacent_force_charge_weighted_avg_preserves_lp_total_energy() -> None:
    """Front-loaded LP plan (high-pwr slot followed by low-pwr trickle) must
    not get the entire merged block running at the high-pwr slot's rate.

    Before this fix, max-merge meant the 09:30-13:30 ForceCharge today blew
    through SoC 11→91% in 3 hours instead of taking the planned 4 hours
    (over-imported by +36% / +2.7 kWh). Weighted-avg ensures the merged
    fdPwr ≈ (LP planned total kWh) / window_hours × 1000.
    """
    t0 = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)
    # Mimic today's 09:30-13:30 LP plan (8 × 30-min slots, varying imports)
    plan_imp_w = [1350, 4900, 2250, 2250, 750, 750, 750, 750]
    slots = [
        HalfHourSlot(
            start_utc=t0 + timedelta(minutes=30 * i),
            end_utc=t0 + timedelta(minutes=30 * (i + 1)),
            price_pence=16.0,
            kind="cheap",
            lp_grid_import_w=p,
            target_soc_pct=95,
        )
        for i, p in enumerate(plan_imp_w)
    ]
    groups = _merge_fox_groups(slots)
    assert len(groups) == 1
    assert groups[0].work_mode == "ForceCharge"
    expected_avg = round(sum(plan_imp_w) / len(plan_imp_w))
    assert groups[0].fd_pwr == expected_avg, (
        f"expected weighted avg {expected_avg} W, got {groups[0].fd_pwr} W. "
        "Max-merge would have returned 4900 W (the peak slot)."
    )
    # Sanity: weighted result is bounded by [min, max] of inputs
    assert min(plan_imp_w) <= groups[0].fd_pwr <= max(plan_imp_w)
    # Sanity: weighted result is far below the historical max-merge value
    assert groups[0].fd_pwr < max(plan_imp_w) // 2


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
    """ForceCharge / ForceDischarge / standard SelfUse must NOT emit maxSoc.
    Only solar_charge (cap=100) and negative_hold (cap=floor, 2026-06-07 pin,
    tested separately) do. Defensive against accidentally capping other modes."""
    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    for kind in ("cheap", "negative", "standard", "peak_export"):
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
    # Default (2026-07-04): negative_hold dispatches Backup (0 discharges in
    # 413 prod samples) — load grid-fed at the paid rate; the full battery holds.
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(slots[0])
    assert wm == "Backup", f"negative_hold must NOT discharge, got {wm!r}"
    assert msg == _min_r()
    assert max_soc is None  # unpinned → paid grid top-up allowed


def test_negative_hold_merges_adjacent_with_same_params() -> None:
    """Adjacent negative_hold slots must collapse into one group so the Fox
    schedule stays within the 8-group limit during long plunge windows.
    """
    t0 = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    slots = [
        HalfHourSlot(
            start_utc=t0 + timedelta(minutes=30 * i),
            end_utc=t0 + timedelta(minutes=30 * (i + 1)),
            price_pence=-1.0,
            kind="negative_hold",
            target_soc_pct=_min_r(),
        )
        for i in range(4)
    ]
    groups = _merge_fox_groups(slots)
    assert len(groups) == 1
    assert groups[0].work_mode == "Backup"


def test_no_negative_slot_maps_to_discharge_capable_mode(monkeypatch) -> None:
    """Regression (2026-06-28): across a negative window mixing shallow holds
    (chg≈0 → negative_hold) and deep charge slots (chg>0 → negative) plus a heavy
    DHW load, NO slot with price<=0 may map to a mode that can discharge the
    battery (Backup/SelfUse/ForceDischarge). Otherwise the battery self-supplies
    the load instead of the PAID grid. Also assert the negative period collapses
    to at most 2 ForceCharge windows (hold@reserve + charge@100) — well under the
    8-group Fox V3 cap."""
    monkeypatch.setattr(config, "LP_NEGATIVE_HOLD_FOX_MODE", "forcecharge", raising=False)
    DISCHARGE_CAPABLE = {"Backup", "SelfUse", "ForceDischarge"}
    # shallow-neg holds, then deep-neg charge slots (battery fills here)
    kinds = ["negative_hold", "negative_hold", "negative", "negative"]
    targets = [_min_r(), _min_r(), 70, 100]
    t0 = datetime(2026, 6, 28, 8, 30, tzinfo=UTC)
    slots = [
        HalfHourSlot(
            start_utc=t0 + timedelta(minutes=30 * i),
            end_utc=t0 + timedelta(minutes=30 * (i + 1)),
            price_pence=-3.0,
            kind=kinds[i],
            lp_grid_import_w=2500 if kinds[i] == "negative" else None,
            target_soc_pct=targets[i],
        )
        for i in range(len(kinds))
    ]
    for s in slots:
        wm, *_ = _slot_fox_tuple(s)
        assert wm not in DISCHARGE_CAPABLE, (
            f"price<=0 slot kind={s.kind!r} mapped to discharge-capable {wm!r}"
        )
        assert wm == "ForceCharge"
    groups = _merge_fox_groups(slots)
    assert len(groups) <= 2, f"expected ≤2 ForceCharge windows, got {len(groups)}"
    assert all(g.work_mode == "ForceCharge" for g in groups)
    # PR D (2026-07-02) superseded the old "Option A" front-load: hold and fill
    # rows no longer merge into each other, so the fill-to-100 now lands on the
    # FILL window (deepest-negative slots), not the whole period. The fill
    # group must still reach fdSoc=100.
    assert max(g.fd_soc for g in groups) == 100, (
        f"fill ForceCharge group must reach fdSoc=100, got {[g.fd_soc for g in groups]}"
    )
