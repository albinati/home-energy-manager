"""Pure tier classification + merging for Octopus rate slots.

Six tiers, in ascending cost order. Day-relative thresholds (ratio to that
day's median) so a windy-cheap day doesn't paint itself with "Severe Peak"
just because some slot is the day's local max — combined with absolute pence
floors on the two extreme tiers (VERY_CHEAP requires < 12p AND bottom decile;
SEVERE_PEAK requires > 30p AND ≥ 1.8× median) so cosmetic outliers on a
flat-priced day don't trigger the loudest signals.

The Google Calendar palette IDs are documented at:
https://developers.google.com/calendar/api/v3/reference/colors

  1=Lavender 2=Sage 3=Grape 4=Flamingo 5=Banana
  6=Tangerine 7=Peacock 8=Graphite 9=Blueberry 10=Basil 11=Tomato
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple


class Tier(NamedTuple):
    key: str          # stable machine identifier, persisted in DB
    emoji: str
    title: str        # human label, used in event summary
    color_id: str     # Google Calendar colorId
    advice: str       # description body


# Ordered cheapest → most expensive. Cost-order is implied by list position.
# Negative is a distinct headline tier — being PAID to consume is rare enough
# (typically a windy night or solar-glut afternoon with a negative Outgoing
# rate) that it deserves its own colour and copy. We use Blueberry (9) for
# unambiguous "blue blue" so the family can spot it across green near-zeros.
TIER_NEGATIVE = Tier(
    "negative", "🔵", "PAID to use", "9",
    "NEGATIVE PRICE — you're being PAID to consume! Run laundry, dishwasher, "
    "oven, charge the EV. Don't curtail PV either; the export rate is below "
    "the import rate, so consuming is the cheapest disposal.",
)
TIER_VERY_CHEAP = Tier(
    "very_cheap", "🟢", "Very cheap", "10",
    "Best (positive) window of the day — run heavy appliances now (laundry, "
    "dishwasher, oven, EV charging if applicable).",
)
TIER_GREEN_LIGHT = Tier(
    "green_light", "🟩", "Green light", "2",
    "Cheap relative to today — heavy appliances OK.",
)
TIER_MODERATE = Tier(
    "moderate", "🟡", "Moderate", "5",
    "Mid-priced — be cautious with heavy loads.",
)
TIER_ABOVE_AVG = Tier(
    "above_avg", "🪸", "Above average", "6",
    "Above today's average — wait for the green light if you can.",
)
TIER_EXPENSIVE = Tier(
    "expensive", "🟠", "Expensive", "4",
    "Expensive — use heavy appliances sparingly.",
)
TIER_SEVERE_PEAK = Tier(
    "severe_peak", "🚨", "Severe peak", "11",
    "Lockdown — avoid all heavy appliances. Battery and PV will cover most "
    "of the house through this window.",
)

ALL_TIERS: list[Tier] = [
    TIER_NEGATIVE, TIER_VERY_CHEAP, TIER_GREEN_LIGHT, TIER_MODERATE,
    TIER_ABOVE_AVG, TIER_EXPENSIVE, TIER_SEVERE_PEAK,
]
_BY_KEY: dict[str, Tier] = {t.key: t for t in ALL_TIERS}


def tier_by_key(key: str) -> Tier:
    return _BY_KEY[key]


# Threshold constants — exposed as module-level so tests can document the
# contract and downstream tooling can read them.
VERY_CHEAP_ABS_CEILING_P = 12.0
VERY_CHEAP_PERCENTILE = 0.10

SEVERE_PEAK_ABS_FLOOR_P = 30.0
# 1.5× chosen to match the user's existing visual scheme — historical samples
# tag windows starting at ~1.57× median as Severe Peak. Both this ratio and
# the absolute floor must hold so a flat-priced day cannot trigger the
# loudest signal off cosmetic outliers.
SEVERE_PEAK_MEDIAN_RATIO = 1.5

EXPENSIVE_MEDIAN_RATIO = 1.30
EXPENSIVE_ABS_FLOOR_P = 25.0

ABOVE_AVG_MEDIAN_RATIO = 1.05
GREEN_LIGHT_MEDIAN_RATIO = 0.90


def _quantile(sorted_prices: list[float], q: float) -> float:
    """Linear-interpolated quantile on a pre-sorted list. q in [0, 1]."""
    if not sorted_prices:
        return 0.0
    if len(sorted_prices) == 1:
        return sorted_prices[0]
    pos = q * (len(sorted_prices) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_prices) - 1)
    frac = pos - lo
    return sorted_prices[lo] * (1 - frac) + sorted_prices[hi] * frac


def classify_one(price: float, *, median: float, p10: float) -> Tier:
    """Classify a single slot's price.

    Order matters: extreme tiers checked first so absolute floors gate the
    headline signals. ``EXPENSIVE`` ranks above ``ABOVE_AVG`` so a price
    above the absolute floor goes orange/red even if it's only modestly
    above the median (e.g., a flat-cheap day where 25p is huge).

    ``NEGATIVE`` short-circuits all other branches: any price strictly below
    zero means we're being paid to consume — a fundamentally different
    signal than "cheapest of today", so it deserves its own headline tier
    even on a day where −5p happens to also be the bottom decile.
    """
    if price < 0:
        return TIER_NEGATIVE
    if price <= p10 and price < VERY_CHEAP_ABS_CEILING_P:
        return TIER_VERY_CHEAP
    if price >= SEVERE_PEAK_MEDIAN_RATIO * median and price > SEVERE_PEAK_ABS_FLOOR_P:
        return TIER_SEVERE_PEAK
    if price >= EXPENSIVE_MEDIAN_RATIO * median or price > EXPENSIVE_ABS_FLOOR_P:
        return TIER_EXPENSIVE
    if price > ABOVE_AVG_MEDIAN_RATIO * median:
        return TIER_ABOVE_AVG
    if price < GREEN_LIGHT_MEDIAN_RATIO * median:
        return TIER_GREEN_LIGHT
    return TIER_MODERATE


@dataclass
class Slot:
    start_utc: datetime
    end_utc: datetime
    price_p: float


@dataclass
class Window:
    """A merged run of consecutive same-tier slots."""
    start_utc: datetime
    end_utc: datetime
    tier: Tier
    prices: list[float]

    @property
    def price_min(self) -> float:
        return min(self.prices)

    @property
    def price_max(self) -> float:
        return max(self.prices)

    @property
    def price_mean(self) -> float:
        return sum(self.prices) / len(self.prices)


MIN_WINDOW_MINUTES = 60


def _tier_index(t: Tier) -> int:
    return ALL_TIERS.index(t)


def _smooth_aba_noise(tiers: list[Tier]) -> list[Tier]:
    """Per-slot ABA smoother: a single slot whose tier differs from both
    neighbors AND is within 1 step of them adopts the neighbour tier.

    Catches sub-pence oscillation across a tier boundary (e.g., 24.4p →
    25.1p → 24.4p flipping Above-average / Expensive / Above-average).
    Multi-slot noise is handled by ``_fold_short_windows`` after merging.

    The ``NEGATIVE`` tier is exempt from smoothing in either direction —
    being paid to consume is a real qualitative signal that should never
    be silently absorbed into a "very cheap" neighbour, even for a single
    30-min slot.
    """
    if len(tiers) < 3:
        return tiers
    out = list(tiers)
    for i in range(1, len(out) - 1):
        prev_t, this_t, next_t = out[i - 1], out[i], out[i + 1]
        if this_t.key == TIER_NEGATIVE.key or prev_t.key == TIER_NEGATIVE.key or next_t.key == TIER_NEGATIVE.key:
            continue
        if prev_t.key == next_t.key and this_t.key != prev_t.key:
            if abs(_tier_index(this_t) - _tier_index(prev_t)) == 1:
                out[i] = prev_t
    return out


def _fold_short_windows(windows: list[Window]) -> list[Window]:
    """Iteratively fold any window shorter than ``MIN_WINDOW_MINUTES`` into
    the longer adjacent neighbour (ties broken in favour of the cheaper
    neighbour — under-warning beats over-warning for the family signal).

    After folding, adjacent windows of the now-same tier are coalesced.
    Single-window inputs (and shorts with no neighbours) are returned as-is.
    """
    if len(windows) < 2:
        return windows

    def duration_min(w: Window) -> float:
        return (w.end_utc - w.start_utc).total_seconds() / 60.0

    while True:
        target_idx = None
        for i, w in enumerate(windows):
            # NEGATIVE windows are never folded — even a single 30-min
            # "you're being paid" event is worth keeping on the calendar
            # so the family doesn't miss it.
            if w.tier.key == TIER_NEGATIVE.key:
                continue
            if duration_min(w) < MIN_WINDOW_MINUTES:
                target_idx = i
                break
        if target_idx is None:
            break

        i = target_idx
        prev_w = windows[i - 1] if i > 0 else None
        next_w = windows[i + 1] if i < len(windows) - 1 else None
        if prev_w is None and next_w is None:
            break

        if prev_w is None:
            absorber = next_w
        elif next_w is None:
            absorber = prev_w
        else:
            d_prev, d_next = duration_min(prev_w), duration_min(next_w)
            if d_prev > d_next:
                absorber = prev_w
            elif d_next > d_prev:
                absorber = next_w
            else:
                # Tie: prefer the cheaper tier so we under-warn.
                absorber = prev_w if _tier_index(prev_w.tier) <= _tier_index(next_w.tier) else next_w

        absorber.prices.extend(windows[i].prices)
        if absorber is prev_w:
            absorber.end_utc = windows[i].end_utc
        else:
            absorber.start_utc = windows[i].start_utc
        del windows[i]

    # Coalesce adjacent same-tier windows that may have become contiguous
    # after a short between two same-tier windows was folded.
    out: list[Window] = []
    for w in windows:
        if out and out[-1].tier.key == w.tier.key and out[-1].end_utc == w.start_utc:
            out[-1].end_utc = w.end_utc
            out[-1].prices.extend(w.prices)
        else:
            out.append(w)
    return out


def classify_day(slots: list[Slot]) -> list[Window]:
    """Classify each slot, smooth single-slot noise, merge into windows,
    then fold any windows shorter than ``MIN_WINDOW_MINUTES`` into longer
    neighbours. Returns [] for empty input.

    Smoothing prevents sub-pence oscillation around tier boundaries from
    fragmenting the day into many tiny events — the family-facing signal
    is meant to be readable at a glance.
    """
    if not slots:
        return []

    prices = sorted(s.price_p for s in slots)
    median = statistics.median(prices)
    p10 = _quantile(prices, VERY_CHEAP_PERCENTILE)

    raw_tiers = [classify_one(s.price_p, median=median, p10=p10) for s in slots]
    tiers = _smooth_aba_noise(raw_tiers)

    windows: list[Window] = []
    for s, t in zip(slots, tiers):
        if windows and windows[-1].tier.key == t.key and windows[-1].end_utc == s.start_utc:
            windows[-1].end_utc = s.end_utc
            windows[-1].prices.append(s.price_p)
        else:
            windows.append(Window(
                start_utc=s.start_utc,
                end_utc=s.end_utc,
                tier=t,
                prices=[s.price_p],
            ))
    return _fold_short_windows(windows)


def format_event(window: Window) -> tuple[str, str]:
    """Return ``(summary, description)`` strings for a Google Calendar event.

    Summary uses different bracket text on the extremes:
      * ``NEGATIVE``  → ``down to -5.3p`` (negative price emphasised; calls out
        that the family is being paid).
      * ``VERY_CHEAP`` → ``down to 9.8p``
      * ``SEVERE_PEAK`` → ``up to 34.5p``
    Mid-tiers use a min–max range.
    """
    t = window.tier
    if t.key == TIER_NEGATIVE.key:
        # Negative window — emphasise the sign so the family sees a -5.3p
        # number rather than mistaking it for a very-low positive price.
        summary = f"{t.emoji} {t.title} (down to {window.price_min:.1f}p — paid!)"
    elif t.key == TIER_VERY_CHEAP.key:
        summary = f"{t.emoji} {t.title} (down to {window.price_min:.1f}p)"
    elif t.key == TIER_SEVERE_PEAK.key:
        summary = f"{t.emoji} {t.title} (up to {window.price_max:.1f}p)"
    else:
        summary = f"{t.emoji} {t.title} ({window.price_min:.1f}p - {window.price_max:.1f}p)"

    description = (
        f"Mean: {window.price_mean:.1f}p | "
        f"Range: {window.price_min:.1f}p - {window.price_max:.1f}p\n\n"
        f"{t.advice}"
    )
    return summary, description
