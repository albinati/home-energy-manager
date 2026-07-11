"""#679 — honour LP battery-hold decisions at positive prices (Backup, not the
ignored SelfUse floor), plus solar_charge → Backup (A2).

Incident (prod 2026-07-10 UTC): PV under-delivered; the battery hit the 10%
floor at 08:01 then discharged ~1 kWh into a midday load spike at 18.1p while
the 33-39p evening peak was minutes away — because those slots dispatched as
SelfUse(minSoc=100) and the H1 IGNORES a per-group SelfUse floor as a discharge
freeze (A0 finding: 40.6% of samples discharged below floor; Backup 0.0%).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config
from src.scheduler.lp_dispatch import lp_plan_to_slots
from src.scheduler.lp_optimizer import LpPlan
from src.scheduler.optimizer import _merge_fox_groups, _slot_fox_tuple

T0 = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
CAP = 10.36  # matches config default BATTERY_CAPACITY_KWH


def _plan(
    prices: list[float],
    imps: list[float],
    *,
    chgs: list[float] | None = None,
    diss: list[float] | None = None,
    soc_pct: float = 77.0,
    peak_thr: float = 30.0,
) -> LpPlan:
    n = len(prices)
    chgs = chgs if chgs is not None else [0.0] * n
    diss = diss if diss is not None else [0.0] * n
    soc = round(soc_pct / 100.0 * CAP, 3)
    return LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=[T0 + timedelta(minutes=30 * i) for i in range(n)],
        price_pence=list(prices),
        import_kwh=list(imps),
        export_kwh=[0.0] * n,
        battery_charge_kwh=list(chgs),
        battery_discharge_kwh=list(diss),
        pv_use_kwh=[0.0] * n,
        pv_curtail_kwh=[0.0] * n,
        dhw_electric_kwh=[0.0] * n,
        space_electric_kwh=[0.0] * n,
        soc_kwh=[soc] * (n + 1),
        peak_threshold_pence=peak_thr,
    )


@pytest.fixture(autouse=True)
def _normal_preset_hold_on(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", CAP, raising=False)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 15.0, raising=False)
    monkeypatch.setattr(config, "LP_POSITIVE_HOLD_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE", 5.0, raising=False)
    monkeypatch.setattr(config, "LP_POSITIVE_HOLD_MAX_GROUPS", 2, raising=False)
    monkeypatch.setattr(config, "LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT", 2.0, raising=False)
    monkeypatch.setattr(config, "LP_SOLAR_CHARGE_FOX_MODE", "selfuse", raising=False)
    # Keep slot KINDS deterministic — the post-shower tank_idle overlay would
    # otherwise relabel `standard` slots as `tank_idle_overnight` depending on
    # the wall clock (still valid holds, but noisy for these assertions).
    monkeypatch.setattr(config, "DHW_TANK_OVERNIGHT_IDLE_ENABLED", "false", raising=False)


# Two contiguous holds (imp>0), two non-import fillers, then a peak.
_HOLD_PRICES = [18.0, 18.0, 18.0, 18.0, 35.0]
_HOLD_IMPS = [0.5, 0.5, 0.0, 0.0, 0.0]


def test_hold_slot_gets_soc_floor():
    slots = lp_plan_to_slots(_plan(_HOLD_PRICES, _HOLD_IMPS))
    # Slots 0,1 are the dis=0/chg=0/imp>0/price>0 holds ahead of the peak.
    assert slots[0].kind == "standard" and slots[1].kind == "standard"
    assert slots[0].soc_floor_pct == 80  # ceil(77.2/5)*5
    assert slots[1].soc_floor_pct == 80
    # Non-import fillers are NOT holds (imp==0).
    assert slots[2].soc_floor_pct is None
    assert slots[3].soc_floor_pct is None
    # The peak slot itself is not a hold (no later peak with uplift).
    assert slots[4].kind == "peak"
    assert slots[4].soc_floor_pct is None


def test_tank_idle_overnight_kind_is_a_valid_hold():
    # A tank_idle_overnight slot carrying a soc_floor_pct maps to pinned Backup
    # too (it is in the hold set) — the overlay and the hold are orthogonal.
    from src.scheduler.optimizer import HalfHourSlot
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    s = HalfHourSlot(
        start_utc=T0, end_utc=T0 + timedelta(minutes=30),
        price_pence=18.0, kind="tank_idle_overnight", soc_floor_pct=75,
    )
    wm, _, _, msg, max_soc = _slot_fox_tuple(s)
    # Pure-hold pin: maxSoc = reserve (no top-up), regardless of soc_floor_pct.
    assert wm == "Backup" and msg == reserve and max_soc == reserve


def test_hold_maps_to_pinned_backup():
    slots = lp_plan_to_slots(_plan(_HOLD_PRICES, _HOLD_IMPS))
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    # soc_floor_pct is carried for telemetry, but the EMITTED pure-hold pin is
    # Backup(reserve, reserve): no discharge (Backup) AND no charge (SoC already
    # above maxSoc) — the proven benign form, no grid top-up.
    assert slots[0].soc_floor_pct == 80
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(slots[0])
    assert wm == "Backup"
    assert fds is None and pwr is None
    assert msg == reserve
    assert max_soc == reserve


def test_no_hold_without_a_future_peak():
    # All prices flat + positive, no slot clears peak_threshold → no hold.
    slots = lp_plan_to_slots(_plan([18.0] * 5, [0.5] * 5))
    assert all(s.soc_floor_pct is None for s in slots)


def test_no_hold_when_uplift_below_threshold():
    # A future peak exists (35 >= 30) but the hold price is 31 → uplift 4 < 5.
    prices = [31.0, 31.0, 31.0, 31.0, 35.0]
    slots = lp_plan_to_slots(_plan(prices, _HOLD_IMPS))
    assert all(s.soc_floor_pct is None for s in slots)


def test_no_hold_at_negative_price():
    # price <= 0 slots are negative_hold, never positive holds.
    prices = [-2.0, -2.0, 18.0, 18.0, 35.0]
    imps = [0.5, 0.5, 0.0, 0.0, 0.0]
    slots = lp_plan_to_slots(_plan(prices, imps))
    assert slots[0].kind in ("negative", "negative_hold")
    assert slots[0].soc_floor_pct is None
    assert slots[1].soc_floor_pct is None


def test_no_hold_at_soc_near_reserve():
    # Planned SoC just at reserve+margin → nothing worth protecting.
    slots = lp_plan_to_slots(_plan(_HOLD_PRICES, _HOLD_IMPS, soc_pct=16.0))
    assert all(s.soc_floor_pct is None for s in slots)


def test_disabled_flag_never_sets_floor(monkeypatch):
    monkeypatch.setattr(config, "LP_POSITIVE_HOLD_ENABLED", False, raising=False)
    slots = lp_plan_to_slots(_plan(_HOLD_PRICES, _HOLD_IMPS))
    assert all(s.soc_floor_pct is None for s in slots)
    # ...and mapping is byte-identical to legacy SelfUse(reserve).
    wm, _, _, msg, max_soc = _slot_fox_tuple(slots[0])
    assert wm == "SelfUse" and msg == int(config.MIN_SOC_RESERVE_PERCENT) and max_soc is None


def test_vacation_never_holds(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    slots = lp_plan_to_slots(_plan(_HOLD_PRICES, _HOLD_IMPS))
    assert all(s.soc_floor_pct is None for s in slots)


@pytest.mark.parametrize("messy", ["  VACATION ", "Vacation", "travel", "AWAY"])
def test_messy_or_alias_vacation_preset_blocks_holds(monkeypatch, messy):
    # A raw un-normalized preset (whitespace/mixed-case, or a legacy alias like
    # 'travel'/'away') stored directly in the override dict — bypassing the
    # setter's strip/lower. The labeller must still recognise it as vacation via
    # _optimization_preset_away_like's defensive normalization + alias handling,
    # so no A1 hold can leak into vacation.
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", messy)
    slots = lp_plan_to_slots(_plan(_HOLD_PRICES, _HOLD_IMPS))
    assert all(s.soc_floor_pct is None for s in slots)


def test_max_groups_caps_number_of_holds(monkeypatch):
    monkeypatch.setattr(config, "LP_POSITIVE_HOLD_MAX_GROUPS", 1, raising=False)
    # Two separate hold runs (0-1 @ 18p and 4-5 @ 25p), peak 35p at index 7.
    # Score = (peak_max - price) * protectable_kwh: run 0-1 has the bigger
    # spread (17p vs 10p) → higher protected value → kept; run 4-5 cleared.
    prices = [18.0, 18.0, 20.0, 20.0, 25.0, 25.0, 22.0, 35.0]
    imps = [0.5, 0.5, 0.0, 0.0, 0.5, 0.5, 0.0, 0.0]
    slots = lp_plan_to_slots(_plan(prices, imps))
    flagged_runs = [
        i for i in range(len(slots))
        if slots[i].soc_floor_pct is not None
    ]
    # Exactly one run kept (2 contiguous slots) — the higher-spread one.
    assert flagged_runs == [0, 1]


# --- A2: solar_charge → Backup / escape hatch / vacation -------------------


def _solar_slot(target_soc_pct: int | None = 90):
    from src.scheduler.optimizer import HalfHourSlot
    return HalfHourSlot(
        start_utc=T0,
        end_utc=T0 + timedelta(minutes=30),
        price_pence=12.0,
        kind="solar_charge",
        target_soc_pct=target_soc_pct,
    )


def test_solar_charge_default_is_plain_selfuse():
    # Final A2 default (selfuse, 2026-07-11 CORRECTED): plain SelfUse at reserve
    # — PV fills, the inverter never auto-imports. NOT the retired 100,100 shape.
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(_solar_slot(90))
    assert wm == "SelfUse"
    assert fds is None and pwr is None
    assert msg == reserve
    assert max_soc is None


def test_solar_charge_backup_hold_pins_reserve(monkeypatch):
    # backup_hold: strict no-discharge hold, maxSoc = reserve (blocks PV fill,
    # no grid-import). Same tuple A1 emits.
    monkeypatch.setattr(config, "LP_SOLAR_CHARGE_FOX_MODE", "backup_hold", raising=False)
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    wm, _, _, msg, max_soc = _slot_fox_tuple(_solar_slot(90))
    assert wm == "Backup"
    assert msg == reserve
    assert max_soc == reserve  # pure hold — no PV fill above reserve


def test_solar_charge_backup_fill_is_selectable_but_firmware_gated(monkeypatch):
    # backup_fill emits Backup(reserve, target). It is FIRMWARE-GATED (fw<1.55
    # grid-imports toward maxSoc) — still selectable for post-upgrade use. Note:
    # _slot_fox_tuple alone does NOT clamp; the no-import guard clamps at merge
    # time when a live SoC is known (tested separately).
    monkeypatch.setattr(config, "LP_SOLAR_CHARGE_FOX_MODE", "backup_fill", raising=False)
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    wm, _, _, msg, max_soc = _slot_fox_tuple(_solar_slot(90))
    assert wm == "Backup"
    assert msg == reserve
    assert max_soc == 90
    _, _, _, _, max_soc_full = _slot_fox_tuple(_solar_slot(None))
    assert max_soc_full == 100


@pytest.mark.parametrize("mode", ["backup_hold", "backup_fill", "selfuse"])
def test_solar_charge_vacation_is_plain_selfuse(monkeypatch, mode):
    # Vacation forces plain SelfUse(reserve) regardless of the mode knob.
    monkeypatch.setattr(config, "LP_SOLAR_CHARGE_FOX_MODE", mode, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    wm, _, _, msg, max_soc = _slot_fox_tuple(_solar_slot(90))
    assert wm == "SelfUse"
    assert msg == int(config.MIN_SOC_RESERVE_PERCENT)
    assert max_soc is None


def test_unknown_solar_mode_falls_back_to_selfuse(monkeypatch):
    monkeypatch.setattr(config, "LP_SOLAR_CHARGE_FOX_MODE", "bogus", raising=False)
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    wm, _, _, msg, max_soc = _slot_fox_tuple(_solar_slot(90))
    # Fallback is the default 'selfuse' → plain SelfUse(reserve, None).
    assert wm == "SelfUse" and msg == reserve and max_soc is None


@pytest.mark.parametrize("mode", ["backup_hold", "backup_fill", "selfuse"])
@pytest.mark.parametrize("preset", ["normal", "vacation", "guests"])
def test_solar_charge_never_emits_hundred_hundred(monkeypatch, mode, preset):
    monkeypatch.setattr(config, "LP_SOLAR_CHARGE_FOX_MODE", mode, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", preset, raising=False)
    for tgt in (None, 50, 90, 100):
        wm, _, _, msg, max_soc = _slot_fox_tuple(_solar_slot(tgt))
        assert not (wm == "SelfUse" and msg == 100 and max_soc == 100), (
            f"the retired SelfUse(100,100) shape must never be emitted "
            f"(mode={mode}, preset={preset}, target={tgt})"
        )


# --- No-import-hold invariant guard (_guard_nonneg_backup_maxsoc) ----------


def test_guard_clamps_positive_price_backup_maxsoc_above_soc():
    from src.scheduler.optimizer import _guard_nonneg_backup_maxsoc
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    # backup_fill shape at a POSITIVE price with maxSoc (90) way above live SoC
    # (40) → clamp maxSoc to reserve (would grid-import on fw<1.55 otherwise).
    key = ("Backup", None, None, reserve, 90)
    out = _guard_nonneg_backup_maxsoc(key, price_pence=18.0, live_soc_pct=40.0)
    assert out == ("Backup", None, None, reserve, reserve)


def test_guard_allows_backup_maxsoc_at_or_below_soc():
    from src.scheduler.optimizer import _guard_nonneg_backup_maxsoc
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    # maxSoc (60) <= live SoC (65) → no import possible → untouched.
    key = ("Backup", None, None, reserve, 60)
    assert _guard_nonneg_backup_maxsoc(key, 18.0, 65.0) == key
    # A1 pure hold (maxSoc = reserve) is always safe → untouched.
    a1 = ("Backup", None, None, reserve, reserve)
    assert _guard_nonneg_backup_maxsoc(a1, 18.0, 20.0) == a1


def test_guard_exempts_negative_price_and_unpinned():
    from src.scheduler.optimizer import _guard_nonneg_backup_maxsoc
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    # negative_hold window: price <= 0 → paid top-up intended → untouched even
    # with a high maxSoc.
    key = ("Backup", None, None, reserve, 100)
    assert _guard_nonneg_backup_maxsoc(key, -3.0, 20.0) == key
    # Unpinned Backup (maxSoc=None, negative_hold default) → untouched.
    unp = ("Backup", None, None, reserve, None)
    assert _guard_nonneg_backup_maxsoc(unp, -3.0, 20.0) == unp
    # None live SoC (no reading) → guard disabled → untouched.
    fill = ("Backup", None, None, reserve, 90)
    assert _guard_nonneg_backup_maxsoc(fill, 18.0, None) == fill
    # Non-Backup tuples pass through.
    su = ("SelfUse", None, None, reserve, None)
    assert _guard_nonneg_backup_maxsoc(su, 18.0, 20.0) == su


def test_guard_integrates_via_merge_backup_fill(monkeypatch):
    # End-to-end: backup_fill solar_charge at a positive price with low live SoC
    # → the merged Fox group is clamped to a reserve-pinned Backup (no import).
    from src.scheduler.optimizer import HalfHourSlot, _merge_fox_groups
    monkeypatch.setattr(config, "LP_SOLAR_CHARGE_FOX_MODE", "backup_fill", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    s = HalfHourSlot(
        start_utc=T0, end_utc=T0 + timedelta(minutes=30),
        price_pence=18.0, kind="solar_charge", target_soc_pct=90,
    )
    # live SoC 40% << maxSoc 90 → guard clamps.
    groups = _merge_fox_groups([s], live_soc_pct=40.0)
    assert len(groups) == 1
    assert groups[0].work_mode == "Backup"
    assert groups[0].max_soc == reserve
    # Without a live SoC the guard is inert (unit-test/back-compat path).
    groups_noguard = _merge_fox_groups([s])
    assert groups_noguard[0].max_soc == 90


# --- Merge / cap interactions ---------------------------------------------


def test_two_hold_runs_hours_apart_stay_two_groups(monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    slots = lp_plan_to_slots(_plan(_HOLD_PRICES, _HOLD_IMPS))
    # Build a second identical hold run 3h later so both floors are 80 (same
    # byte-identical Backup tuple). The #480 merge must NOT bridge the gap.
    hold_a = [s for s in slots if s.soc_floor_pct is not None]
    assert len(hold_a) == 2
    later = []
    base2 = T0 + timedelta(hours=3)
    from src.scheduler.optimizer import HalfHourSlot
    for i in range(2):
        later.append(
            HalfHourSlot(
                start_utc=base2 + timedelta(minutes=30 * i),
                end_utc=base2 + timedelta(minutes=30 * (i + 1)),
                price_pence=18.0,
                kind="standard",
                soc_floor_pct=80,
            )
        )
    groups = _merge_fox_groups(hold_a + later)
    backups = [g for g in groups if g.work_mode == "Backup"]
    assert len(backups) == 2, "byte-identical holds 3h apart must stay two groups"


def test_hold_does_not_change_daikin_output(monkeypatch):
    # soc_floor_pct is orthogonal to Daikin (which dispatches by KIND). The tank
    # rows must be byte-identical whether or not positive holds are flagged.
    from src.scheduler.lp_dispatch import daikin_dispatch_preview

    plan = _plan(_HOLD_PRICES, _HOLD_IMPS)

    monkeypatch.setattr(config, "LP_POSITIVE_HOLD_ENABLED", True, raising=False)
    slots_on = lp_plan_to_slots(plan)
    assert any(s.soc_floor_pct is not None for s in slots_on)  # holds ARE flagged
    pairs_on = daikin_dispatch_preview(plan, [])

    monkeypatch.setattr(config, "LP_POSITIVE_HOLD_ENABLED", False, raising=False)
    slots_off = lp_plan_to_slots(plan)
    assert all(s.soc_floor_pct is None for s in slots_off)  # none flagged
    pairs_off = daikin_dispatch_preview(plan, [])

    assert pairs_on == pairs_off, "positive holds must not perturb Daikin rows"
