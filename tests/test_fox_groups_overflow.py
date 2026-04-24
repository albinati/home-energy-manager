"""Fox V3 8-group cap: eager SelfUse merge, trivial-SelfUse drop, dynamic-replan
truncation, and back-bias compression fallback with peak_export protection.

Regression guards for the 2026-04-24 incidents where the legacy compressor
squashed ``merged[0]/merged[1]`` (sacrificing the immediate future) and the
upload payload spent half its 8-slot budget on SelfUse-with-default-floor gaps
that the firmware naturally falls back to anyway (Fox app: "Remaining Time
Work Mode: Self-use").
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config as app_config
from src.scheduler.optimizer import HalfHourSlot, _merge_fox_groups


@pytest.fixture(autouse=True)
def _london_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")


def _slot(
    start_utc: datetime,
    *,
    kind: str,
    minutes: int = 30,
    lp_grid_import_w: int | None = None,
    target_soc_pct: int | None = None,
) -> HalfHourSlot:
    return HalfHourSlot(
        start_utc=start_utc,
        end_utc=start_utc + timedelta(minutes=minutes),
        price_pence=10.0,
        kind=kind,
        lp_grid_import_w=lp_grid_import_w,
        target_soc_pct=target_soc_pct,
    )


# --- Camada 1: eager _coarse_merge_fox merges adjacent SelfUse variants ----


def test_eager_merge_collapses_solar_charge_next_to_standard_selfuse() -> None:
    """A solar_charge slot (SelfUse minSoc=100) adjacent to a standard slot
    (SelfUse minSoc=MIN_SOC_RESERVE) must merge into one SelfUse window with
    the higher minSoc — no overflow needed to trigger the merge.
    """
    base = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)  # noon BST → all-daylight, no midnight cross
    slots = [
        _slot(base, kind="solar_charge"),
        _slot(base + timedelta(minutes=30), kind="standard"),
    ]
    groups = _merge_fox_groups(slots)
    assert len(groups) == 1
    assert groups[0].work_mode == "SelfUse"
    # The solar_charge minSoc (100) wins over the standard minSoc (MIN_SOC_RESERVE).
    assert groups[0].min_soc_on_grid == int(
        getattr(app_config, "FOX_SOLAR_CHARGE_MIN_SOC_PERCENT", 100)
    )


# --- Camada 2: truncate_horizon=True returns (groups, replan_at_utc) ------


def _alternating_overflow_slots(base: datetime, count: int) -> list[HalfHourSlot]:
    """Build ``count`` adjacent slots alternating between SelfUse-causing kinds
    (``standard`` → SelfUse minSoc=reserve) and ForceCharge kinds (``cheap``)
    so each pair has a different fox-key — no eager merge possible.
    """
    out: list[HalfHourSlot] = []
    for i in range(count):
        kind = "standard" if i % 2 == 0 else "cheap"
        out.append(
            _slot(
                base + timedelta(minutes=30 * i),
                kind=kind,
                lp_grid_import_w=2000 if kind == "cheap" else None,
                target_soc_pct=80 if kind == "cheap" else None,
            )
        )
    return out


def test_truncate_horizon_returns_first_8_windows_and_replan_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When truncate_horizon=True and >8 distinct windows result, dispatch keeps
    only the first 8 windows (preserving the immediate future) and reports the
    end-time of the 8th as replan_at_utc.

    Filter disabled so the alternating SelfUse/FC pattern produces a real
    overflow scenario (otherwise the SelfUse halves get dropped pre-truncation).
    """
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", False)
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)  # noon UTC, no midnight cross
    base_noon = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    slots = _alternating_overflow_slots(base_noon, 9)  # 9 distinct windows
    result = _merge_fox_groups(slots, max_groups=8, truncate_horizon=True)
    assert isinstance(result, tuple)
    groups, replan_at = result
    # 8 windows kept (no compression squash applied), replan boundary set.
    assert len(groups) == 8
    assert replan_at is not None
    # 9 slots × 30min starting 09:00 UTC: 8th slot ends at 13:00 UTC.
    assert replan_at == base_noon + timedelta(minutes=30 * 8)


def test_truncate_horizon_no_truncation_returns_replan_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the input fits in 8 windows, no truncation happens and replan_at is None."""
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", False)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    slots = _alternating_overflow_slots(base, 4)  # 4 windows, well under cap
    result = _merge_fox_groups(slots, max_groups=8, truncate_horizon=True)
    groups, replan_at = result
    assert len(groups) == 4
    assert replan_at is None


# --- Camada 3: compression fallback (back-bias + peak guard) -------------


def test_compression_fallback_back_bias_preserves_morning_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When truncate_horizon=False (legacy path) and >8 windows, the back-bias
    compressor must squash from the tail. The first ForceCharge window
    (the critical near-future) survives intact.

    Filter off so the SelfUse halves remain and we exercise the real overflow.
    """
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", False)
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    slots = _alternating_overflow_slots(base, 9)
    groups = _merge_fox_groups(slots, max_groups=8)
    assert len(groups) <= 8
    # First group must be the ForceCharge slot from index 0 of the LP plan
    # (which was a "standard" → SelfUse… wait, let's check both possibilities).
    # In our alternating pattern slot[0] is "standard" → SelfUse.
    # So we assert the SECOND group (slot[1] = cheap → ForceCharge) survives:
    # there must be a ForceCharge group within the first 3 (it can't have been
    # squashed to the tail).
    early_modes = [g.work_mode for g in groups[:3]]
    assert "ForceCharge" in early_modes, (
        f"Expected at least one ForceCharge in early groups (back-bias should "
        f"preserve the front), got {early_modes}"
    )


def test_compression_fallback_peak_guard_protects_force_discharge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ForceDischarge (peak_export) window late in the day must NOT be the
    victim of the brutal squash even though tail-bias would normally pick it.

    Filter off so the SelfUse halves remain and we exercise the cap pressure.
    """
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", False)
    base = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)  # all-daylight summer, no midnight cross
    # Build 9 picotated windows: 7 alternating standard/cheap, then peak_export
    # near the tail, then a final standard to ensure peak_export is in the
    # interior of the tail (so the back-bias would naturally try to merge it).
    slots: list[HalfHourSlot] = []
    for i in range(7):
        kind = "standard" if i % 2 == 0 else "cheap"
        slots.append(
            _slot(
                base + timedelta(minutes=30 * i),
                kind=kind,
                lp_grid_import_w=2000 if kind == "cheap" else None,
                target_soc_pct=80 if kind == "cheap" else None,
            )
        )
    # peak_export window
    slots.append(_slot(base + timedelta(minutes=30 * 7), kind="peak_export"))
    # final standard to push count to 9
    slots.append(_slot(base + timedelta(minutes=30 * 8), kind="standard"))
    groups = _merge_fox_groups(slots, max_groups=8)
    # The ForceDischarge group MUST still be present — peak guard prevented
    # its squash.
    modes = [g.work_mode for g in groups]
    assert "ForceDischarge" in modes, (
        f"peak_export ForceDischarge group was squashed; got modes={modes}"
    )


# --- Camada 0: trivial SelfUse drop ---------------------------------------


def _trivial_selfuse_slot(start_utc: datetime, *, minutes: int = 30) -> HalfHourSlot:
    """A 'standard' kind → SelfUse with minSoc=MIN_SOC_RESERVE_PERCENT (the global)."""
    return _slot(start_utc, kind="standard", minutes=minutes)


def _force_charge_slot(start_utc: datetime) -> HalfHourSlot:
    return _slot(
        start_utc,
        kind="cheap",
        lp_grid_import_w=2000,
        target_soc_pct=80,
    )


def test_trivial_selfuse_groups_are_filtered_when_flag_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the 2026-04-24 incident shape: 4 trivial SelfUse gaps interleaved
    with 4 ForceCharge windows. After the filter, only the 4 FCs remain — the
    inverter's "Remaining Time Work Mode: Self-use" handles the gaps for free.
    """
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", True)
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)  # all daylight, no midnight cross
    slots: list[HalfHourSlot] = []
    for i in range(8):
        if i % 2 == 0:
            slots.append(_trivial_selfuse_slot(base + timedelta(minutes=30 * i)))
        else:
            slots.append(_force_charge_slot(base + timedelta(minutes=30 * i)))
    groups = _merge_fox_groups(slots, max_groups=8)
    modes = [g.work_mode for g in groups]
    assert all(m == "ForceCharge" for m in modes), (
        f"trivial SelfUse should be filtered out; got modes={modes}"
    )
    # 4 ForceCharge windows survive (one per even/odd boundary in the input).
    assert len(groups) == 4


def test_solar_charge_selfuse_with_elevated_min_soc_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A solar_charge slot maps to SelfUse minSoc=100 — not the global default —
    so the filter must KEEP it. Otherwise the battery wouldn't be held at 100%
    during PV-stockpile windows.
    """
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", True)
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    slots = [
        _trivial_selfuse_slot(base),
        _slot(base + timedelta(minutes=30), kind="solar_charge"),
        _trivial_selfuse_slot(base + timedelta(hours=1)),
    ]
    groups = _merge_fox_groups(slots, max_groups=8)
    # Trivial SelfUse stripped on both sides; only the elevated-floor SelfUse remains.
    assert len(groups) == 1
    assert groups[0].work_mode == "SelfUse"
    assert groups[0].min_soc_on_grid == int(
        getattr(app_config, "FOX_SOLAR_CHARGE_MIN_SOC_PERCENT", 100)
    )


def test_filter_disabled_keeps_legacy_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off, every LP window becomes a Fox group (subject to the
    standard merge / cap logic). Provides instant rollback if the firmware
    misbehaves with sparse schedules."""
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", False)
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    slots = [
        _trivial_selfuse_slot(base),
        _force_charge_slot(base + timedelta(minutes=30)),
        _trivial_selfuse_slot(base + timedelta(hours=1)),
    ]
    groups = _merge_fox_groups(slots, max_groups=8)
    modes = [g.work_mode for g in groups]
    assert "SelfUse" in modes  # trivial gaps preserved when flag off
    assert "ForceCharge" in modes


def test_filter_keeps_at_least_one_group_when_plan_is_all_trivial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degenerate edge: an LP plan of nothing but trivial SelfUse windows. The
    filter would empty the payload and the firmware may reject an empty groups
    array — defensive fallback keeps the first window."""
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", True)
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    slots = [_trivial_selfuse_slot(base + timedelta(minutes=30 * i)) for i in range(4)]
    groups = _merge_fox_groups(slots, max_groups=8)
    # Initial scan + eager merge collapse all 4 into 1 SelfUse window; filter
    # would normally drop it, but the defensive fallback keeps it. Expected: 1.
    assert len(groups) == 1
    assert groups[0].work_mode == "SelfUse"


def test_real_incident_eight_groups_collapse_to_three_with_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay the 2026-04-24 23:25 UTC payload shape on disk:
        SU 23:30-23:59 | SU 00:00-03:30 | FC 03:30-04:59 | SU 05:00-11:30
        FC 11:30-11:59 | SU 12:00-12:30 | FC 12:30-12:59 | SU 13:00-13:30
    Without the filter the LP truncated at 13:30 (PR #141 dynamic replan) —
    losing the 13:30 and 15:30 ForceCharges the LP had planned. With the filter,
    the SelfUse gaps disappear, only 3 useful groups remain, and the would-be-
    truncated tail fits comfortably under the 8-group cap.
    """
    monkeypatch.setattr(app_config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", True)
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)  # noon-ish, no midnight cross for clarity
    # Build the conceptual shape: trivial gap | FC | trivial gap | FC | trivial gap | FC
    slots: list[HalfHourSlot] = []
    cursor = base
    # Gap 1
    for _ in range(2):
        slots.append(_trivial_selfuse_slot(cursor)); cursor += timedelta(minutes=30)
    # FC 1
    for _ in range(3):
        slots.append(_force_charge_slot(cursor)); cursor += timedelta(minutes=30)
    # Gap 2
    for _ in range(4):
        slots.append(_trivial_selfuse_slot(cursor)); cursor += timedelta(minutes=30)
    # FC 2
    for _ in range(1):
        slots.append(_force_charge_slot(cursor)); cursor += timedelta(minutes=30)
    # Gap 3
    for _ in range(1):
        slots.append(_trivial_selfuse_slot(cursor)); cursor += timedelta(minutes=30)
    # FC 3
    for _ in range(1):
        slots.append(_force_charge_slot(cursor)); cursor += timedelta(minutes=30)
    # Tail trivial gap (would have been truncated)
    for _ in range(2):
        slots.append(_trivial_selfuse_slot(cursor)); cursor += timedelta(minutes=30)

    result = _merge_fox_groups(slots, max_groups=8, truncate_horizon=True)
    groups, replan_at = result
    modes = [g.work_mode for g in groups]
    assert modes == ["ForceCharge", "ForceCharge", "ForceCharge"], (
        f"expected exactly 3 ForceCharge groups after filter, got {modes}"
    )
    # No truncation needed — the filter already kept us well under the cap.
    assert replan_at is None


def test_compression_fallback_degenerate_all_force_discharge_does_not_crash() -> None:
    """Degenerate edge: all slots are peak_export so every adjacent pair has
    a ForceDischarge. The fallback must still terminate (tail squash) without
    raising.
    """
    base = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
    # 9 peak_export slots back-to-back: the initial scan and force-charge
    # adjacency merge will collapse them into 1 group, so to actually exercise
    # the degenerate path we interleave with non-mergeable variation by using
    # alternating peak_export and peak with peak_export_discharge=True. Both
    # produce ForceDischarge keys. To force them to be NON-mergeable (different
    # keys) we'd need to vary fd_soc/fd_pwr — but they're constants. Easier:
    # interleave with negative_hold which produces "Backup" keys.
    # Goal: every ADJACENT pair has at least one ForceDischarge — so that the
    # back-bias scan fails to find any non-FD pair.
    slots: list[HalfHourSlot] = []
    for i in range(9):
        if i % 2 == 0:
            slots.append(_slot(base + timedelta(minutes=30 * i), kind="peak_export"))
        else:
            slots.append(_slot(base + timedelta(minutes=30 * i), kind="negative_hold"))
    # Should not raise; fallback exits via tail squash. Final count <= 8.
    groups = _merge_fox_groups(slots, max_groups=8)
    assert len(groups) <= 8
