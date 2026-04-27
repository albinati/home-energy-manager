"""Pure-function tests for Google Calendar tier classification + merging."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.google_calendar.tiers import (
    EXPENSIVE_ABS_FLOOR_P,
    SEVERE_PEAK_ABS_FLOOR_P,
    TIER_ABOVE_AVG,
    TIER_EXPENSIVE,
    TIER_GREEN_LIGHT,
    TIER_MODERATE,
    TIER_SEVERE_PEAK,
    TIER_VERY_CHEAP,
    VERY_CHEAP_ABS_CEILING_P,
    Slot,
    _quantile,
    classify_day,
    classify_one,
    format_event,
)


# ── classify_one boundary semantics ────────────────────────────────────────


def test_very_cheap_requires_both_percentile_and_absolute_floor():
    """A 14p slot at the day's bottom decile is NOT very_cheap (>12p ceiling)."""
    median = 25.0
    p10 = 14.0
    assert classify_one(14.0, median=median, p10=p10).key != TIER_VERY_CHEAP.key
    # Same percentile position but under the absolute ceiling → very cheap.
    assert classify_one(11.0, median=median, p10=11.0).key == TIER_VERY_CHEAP.key


def test_severe_peak_requires_both_percentile_and_absolute_floor():
    """A 25p slot at well over 1.5× median is NOT severe_peak (under 30p floor)."""
    median = 13.0  # 1.5× = 19.5p; 25p easily clears the ratio gate
    assert classify_one(25.0, median=median, p10=8.0).key != TIER_SEVERE_PEAK.key
    # Above 30p AND ≥ 1.5× median → severe peak.
    median = 18.0  # 1.5× = 27p
    assert classify_one(33.0, median=median, p10=8.0).key == TIER_SEVERE_PEAK.key


def test_expensive_triggers_on_absolute_floor_alone():
    """On a flat-cheap day, a 27p slot still reads as Expensive even if
    barely 1.3× the median — the absolute floor (>25p) catches it."""
    median = 22.0  # 1.3× = 28.6p, so ratio gate alone misses 27p
    assert classify_one(27.0, median=median, p10=15.0).key == TIER_EXPENSIVE.key


def test_above_avg_vs_moderate_band():
    median = 20.0
    # 1.05× median = 21.0; 0.90× = 18.0 → MODERATE inside [18.0, 21.0]
    assert classify_one(20.0, median=median, p10=10.0).key == TIER_MODERATE.key
    assert classify_one(19.0, median=median, p10=10.0).key == TIER_MODERATE.key
    # Just above the upper edge → ABOVE_AVG
    assert classify_one(22.0, median=median, p10=10.0).key == TIER_ABOVE_AVG.key
    # Just below the lower edge → GREEN_LIGHT
    assert classify_one(17.0, median=median, p10=10.0).key == TIER_GREEN_LIGHT.key


def test_extreme_tiers_outrank_others():
    """Order of checks: VERY_CHEAP wins over GREEN_LIGHT; SEVERE outranks EXPENSIVE."""
    # Very-cheap conditions met; would also satisfy GREEN_LIGHT (well below median)
    assert classify_one(8.0, median=20.0, p10=8.0).key == TIER_VERY_CHEAP.key
    # Severe conditions met; would also satisfy EXPENSIVE
    assert classify_one(40.0, median=20.0, p10=10.0).key == TIER_SEVERE_PEAK.key


def test_floor_constants_are_exposed():
    """Documented contract: thresholds are module-level so callers can read them."""
    assert VERY_CHEAP_ABS_CEILING_P == 12.0
    assert SEVERE_PEAK_ABS_FLOOR_P == 30.0
    assert EXPENSIVE_ABS_FLOOR_P == 25.0


# ── _quantile helper ───────────────────────────────────────────────────────


def test_quantile_handles_edge_lengths():
    assert _quantile([], 0.5) == 0.0
    assert _quantile([5.0], 0.5) == 5.0
    # 0th and 100th percentile of a sorted list = endpoints
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _quantile(xs, 0.0) == 1.0
    assert _quantile(xs, 1.0) == 5.0
    # Linear interpolation: q=0.5 of [1..5] → 3.0
    assert _quantile(xs, 0.5) == 3.0


# ── classify_day + merge ───────────────────────────────────────────────────


def _make_slots(prices: list[float], start_hour: int = 0) -> list[Slot]:
    """Helper: build contiguous 30-min slots starting at the given UTC hour."""
    base = datetime(2026, 4, 27, start_hour, 0, tzinfo=UTC)
    return [
        Slot(
            start_utc=base + timedelta(minutes=30 * i),
            end_utc=base + timedelta(minutes=30 * (i + 1)),
            price_p=p,
        )
        for i, p in enumerate(prices)
    ]


def test_classify_day_empty_returns_empty():
    assert classify_day([]) == []


def test_classify_day_single_slot_yields_single_window():
    slots = _make_slots([18.0])
    windows = classify_day(slots)
    assert len(windows) == 1
    assert windows[0].start_utc == slots[0].start_utc
    assert windows[0].end_utc == slots[0].end_utc


def test_consecutive_same_tier_slots_merge():
    """Three contiguous moderate slots fuse into one 90-min window."""
    # All near the median → all MODERATE.
    slots = _make_slots([18.0, 18.0, 18.0, 18.0])
    windows = classify_day(slots)
    assert len(windows) == 1
    assert windows[0].tier.key == TIER_MODERATE.key
    # Window covers all four slots = 2h
    assert windows[0].end_utc - windows[0].start_utc == timedelta(hours=2)


def test_tier_change_breaks_window():
    """A realistic day: cheap morning, moderate body, peak evening — three
    tiers, three windows in chronological order."""
    # 3 very-cheap slots, 42 moderate slots at the day-median, 3 severe-peak
    # slots. Median = 18.0p → 1.5× = 27p, so 33-35p slots clear the ratio
    # gate AND the 30p absolute floor.
    prices = [8.0, 8.5, 9.0] + [18.0] * 42 + [33.0, 34.0, 35.0]
    windows = classify_day(_make_slots(prices))
    assert len(windows) == 3
    assert windows[0].tier.key == TIER_VERY_CHEAP.key
    assert windows[1].tier.key == TIER_MODERATE.key
    assert windows[-1].tier.key == TIER_SEVERE_PEAK.key


def test_window_aggregates_prices_correctly():
    slots = _make_slots([17.5, 18.0, 18.5])
    windows = classify_day(slots)
    assert len(windows) == 1
    w = windows[0]
    assert w.price_min == 17.5
    assert w.price_max == 18.5
    assert w.price_mean == pytest.approx(18.0)


def test_flat_day_does_not_show_severe_peak_or_very_cheap():
    """A day where every slot is 18p has no extremes — the absolute floors
    suppress both VERY_CHEAP and SEVERE_PEAK even though some slot is the
    day's max (and min)."""
    windows = classify_day(_make_slots([18.0] * 48))
    # All MODERATE (within ±5% of the median which equals 18.0).
    assert all(w.tier.key == TIER_MODERATE.key for w in windows)
    # And merged into a single 24h window.
    assert len(windows) == 1


# ── format_event ───────────────────────────────────────────────────────────


def test_format_event_uses_special_phrasing_on_extremes():
    # Mixed-price day so extremes register against a normal median.
    cheap_day = [8.0, 8.5, 9.0] + [18.0] * 42 + [33.0, 34.0, 35.0]
    windows = classify_day(_make_slots(cheap_day))
    cheap_window = windows[0]
    peak_window = windows[-1]
    assert cheap_window.tier.key == TIER_VERY_CHEAP.key
    assert peak_window.tier.key == TIER_SEVERE_PEAK.key

    summary, _ = format_event(cheap_window)
    assert "down to" in summary
    summary, _ = format_event(peak_window)
    assert "up to" in summary


def test_format_event_description_contains_mean_and_advice():
    w = classify_day(_make_slots([17.5, 18.0, 18.5]))[0]
    _, description = format_event(w)
    assert "Mean: 18.0p" in description
    assert "Range: 17.5p - 18.5p" in description
    # Advice is the tier's advice text — non-empty.
    assert w.tier.advice in description


# ── smoothing: per-slot ABA noise + window-duration fold ───────────────────


def test_aba_single_slot_noise_is_smoothed():
    """24.4 → 25.1 → 24.4 → 25.1 → 24.4 oscillates Above-avg / Expensive
    across the 25p absolute floor on sub-pence increments. Smoothing should
    collapse the single-slot Expensive flips into the surrounding tier."""
    # Fill the rest of the day with cheap slots so the median is well below
    # the boundary in question, otherwise the day's median dominates.
    body = [16.0] * 30
    flips = [24.4, 25.1, 24.4, 25.1, 24.4, 25.1, 24.4, 25.1, 24.4]
    prices = body + flips + body[:9]
    windows = classify_day(_make_slots(prices))
    # No single-slot Expensive windows should survive — each was sandwiched
    # by Above-average neighbours within 1 tier.
    short_expensive = [
        w for w in windows
        if w.tier.key == "expensive"
        and (w.end_utc - w.start_utc).total_seconds() < 60 * 60
    ]
    assert short_expensive == []


def test_short_window_folds_into_longer_neighbour():
    """A 30-min sliver tier change between two long windows is folded
    away — under MIN_WINDOW_MINUTES, the family doesn't get a 30-min event."""
    # 90 min Moderate, 30 min Above-avg sliver, 90 min Moderate.
    prices = [18.0] * 3 + [22.0] * 1 + [18.0] * 3
    windows = classify_day(_make_slots(prices))
    # All collapsed to a single Moderate window covering 7 slots = 3.5 h.
    assert len(windows) == 1
    assert windows[0].tier.key == "moderate"


def test_smoothing_preserves_meaningful_long_windows():
    """A 90 min Above-avg window between cheap blocks must survive — only
    sub-60-min windows fold away."""
    # median = 16p, 20p / 16p = 1.25× → ABOVE_AVG (1.05–1.30× range).
    prices = [16.0] * 3 + [20.0] * 3 + [16.0] * 3
    windows = classify_day(_make_slots(prices))
    keys = [w.tier.key for w in windows]
    assert "above_avg" in keys
    assert len(windows) == 3
