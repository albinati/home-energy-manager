"""Fox V3 8-group cap: eager SelfUse merge, dynamic-replan truncation, and
back-bias compression fallback with peak_export protection.

Regression guards for the 2026-04-24 incident where the legacy compressor
squashed ``merged[0]/merged[1]`` (sacrificing the immediate future) when the LP
plan exceeded the inverter's 8-group hardware cap.
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


def test_truncate_horizon_returns_first_8_windows_and_replan_at() -> None:
    """When truncate_horizon=True and >8 distinct windows result, dispatch keeps
    only the first 8 windows (preserving the immediate future) and reports the
    end-time of the 8th as replan_at_utc.
    """
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


def test_truncate_horizon_no_truncation_returns_replan_none() -> None:
    """When the input fits in 8 windows, no truncation happens and replan_at is None."""
    base = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    slots = _alternating_overflow_slots(base, 4)  # 4 windows, well under cap
    result = _merge_fox_groups(slots, max_groups=8, truncate_horizon=True)
    groups, replan_at = result
    assert len(groups) == 4
    assert replan_at is None


# --- Camada 3: compression fallback (back-bias + peak guard) -------------


def test_compression_fallback_back_bias_preserves_morning_window() -> None:
    """When truncate_horizon=False (legacy path) and >8 windows, the back-bias
    compressor must squash from the tail. The first ForceCharge window
    (the critical near-future) survives intact.
    """
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


def test_compression_fallback_peak_guard_protects_force_discharge() -> None:
    """A ForceDischarge (peak_export) window late in the day must NOT be the
    victim of the brutal squash even though tail-bias would normally pick it.
    """
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
