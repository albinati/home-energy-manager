"""Tariff structure: band-aware cheap/peak classification for ANY Octopus tariff.

Why this exists (Cosy switch, 2026-10 — Tracked by #803 / #804)
----------------------------------------------------------------
Every price classifier in HEM was written for Agile: 48 distinct half-hourly
prices per day, so "cheap" = bottom quartile and "peak" = top quartile is a
sound reading. A *banded* time-of-use tariff (Cosy: three flat levels, 16 cheap
/ 26 day / 6 peak slots per local day) breaks that arithmetic — the 75th
percentile of a Cosy day IS the day-rate level, so every ``price >= peak_thr``
consumer treated 13 h of day band as peak (LWT setback all day, tank shutdown
07–13 / 19–24, charge floors at 00:00 and 07:00), while every strict
``price < cheap_thr`` consumer never saw the cheap band at all.

This module is the ONE place that decides whether a price series is
``banded`` (≤ ``TARIFF_BANDED_MAX_LEVELS`` distinct levels) or ``dynamic``
(Agile-like), and derives thresholds that make every existing consumer
correct without touching its comparison operator:

* **banded** → thresholds are the MIDPOINTS between adjacent levels. Cosy:
  ``cheap_thr = 18.97``, ``peak_thr = 31.81``. The cheap level (12.49) is below
  the cheap threshold whether the caller uses ``<`` or ``<=``; the peak level
  (38.17) is above the peak threshold either way; the day level (25.45) is
  neither.
* **dynamic** → bit-for-bit the formulas the callers used before (two flavours
  exist in the codebase and both are preserved — see ``DynamicRule``).

Kill switch: ``OCTOPUS_TARIFF_STRUCTURE=dynamic`` forces the legacy percentile
path everywhere; ``banded`` forces band detection; ``auto`` (default) detects.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from ..config import config

logger = logging.getLogger(__name__)

StructureKind = Literal["banded", "dynamic"]
Band = Literal["negative", "cheap", "standard", "peak"]
DynamicRule = Literal["lp", "legacy"]
"""Which pre-existing percentile formula to reproduce on a dynamic tariff.

* ``lp``     — ``cheap = sorted[n//4 - 1]``, ``peak = sorted[(3n)//4]``
               (``lp_optimizer.solve_lp`` and the strategy summary).
* ``legacy`` — ``cheap = min(mean × 0.85, q25)`` with strict ``<``,
               ``peak = max(q75, OPTIMIZATION_PEAK_THRESHOLD_PENCE)`` with
               strict ``>`` (``optimizer._classify_slots``, ``/api/v1/agile/day``,
               ``analytics.patterns``).
"""


@dataclass(frozen=True)
class TariffStructure:
    kind: StructureKind
    levels: tuple[float, ...] = ()
    """Sorted distinct quantised positive price levels (banded only)."""
    cheap_level: float | None = None
    peak_level: float | None = None
    cheap_thr: float = 0.0
    peak_thr: float = 0.0
    n_slots: int = 0
    reason: str = ""
    band_by_level: dict[float, Band] = field(default_factory=dict)

    @property
    def is_banded(self) -> bool:
        return self.kind == "banded"

    @property
    def has_cheap(self) -> bool:
        return self.cheap_level is not None

    @property
    def has_peak(self) -> bool:
        return self.peak_level is not None

    def band_of(self, price: float) -> Band:
        """Band for one price under this structure (banded: by threshold)."""
        p = float(price)
        if p <= 0.0:
            return "negative"
        if self.has_cheap and p <= self.cheap_thr:
            return "cheap"
        if self.has_peak and p >= self.peak_thr:
            return "peak"
        return "standard"


# ---------------------------------------------------------------------------
# Config readers (getattr so an older Config/test double still works)
# ---------------------------------------------------------------------------

def _mode() -> str:
    return str(getattr(config, "OCTOPUS_TARIFF_STRUCTURE", "auto") or "auto").strip().lower()


def _max_levels() -> int:
    return int(getattr(config, "TARIFF_BANDED_MAX_LEVELS", 4))


def _min_slots() -> int:
    return int(getattr(config, "TARIFF_BANDED_MIN_SLOTS", 12))


def _contrast_ratio() -> float:
    return float(getattr(config, "TARIFF_BAND_CONTRAST_RATIO", 1.25))


def _quantise(p: float, quantum: float) -> float:
    if quantum <= 0:
        return float(p)
    return round(round(float(p) / quantum) * quantum, 6)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _dynamic(prices: list[float], reason: str, rule: DynamicRule) -> TariffStructure:
    cheap_thr, peak_thr = _dynamic_thresholds(prices, rule)
    return TariffStructure(
        kind="dynamic", cheap_thr=cheap_thr, peak_thr=peak_thr,
        n_slots=len(prices), reason=reason,
    )


def _dynamic_thresholds(prices: list[float], rule: DynamicRule) -> tuple[float, float]:
    n = len(prices)
    if n == 0:
        return 0.0, 0.0
    sorted_p = sorted(float(p) for p in prices)
    q25 = sorted_p[max(0, n // 4 - 1)]
    q75 = sorted_p[min(n - 1, (3 * n) // 4)]
    if rule == "lp":
        return q25, q75
    mean_p = sum(sorted_p) / n
    cheap_thr = min(mean_p * 0.85, q25)
    peak_thr = max(q75, float(getattr(config, "OPTIMIZATION_PEAK_THRESHOLD_PENCE", 25.0)))
    return cheap_thr, peak_thr


def detect(
    prices: list[float] | tuple[float, ...],
    *,
    quantum: float = 0.01,
    dynamic_rule: DynamicRule = "lp",
) -> TariffStructure:
    """Decide banded vs dynamic for a price series and derive thresholds.

    Pure: no I/O. Negative/zero prices are ignored for level counting (a
    plunge on Agile is still dynamic; a banded tariff never goes negative).
    """
    px = [float(p) for p in prices if p is not None and math.isfinite(float(p))]
    n = len(px)
    mode = _mode()
    if mode == "dynamic":
        return _dynamic(px, "forced:dynamic", dynamic_rule)
    if n < _min_slots() and mode != "banded":
        return _dynamic(px, f"short:{n}<{_min_slots()}", dynamic_rule)

    positive = sorted({_quantise(p, quantum) for p in px if p > 0.0})
    if not positive:
        return _dynamic(px, "no_positive_prices", dynamic_rule)
    if len(positive) > _max_levels() and mode != "banded":
        return _dynamic(px, f"levels:{len(positive)}>{_max_levels()}", dynamic_rule)
    if len(positive) > _max_levels():
        # forced banded on a dynamic-looking series: collapse to quartile
        # levels so the caller still gets *some* banding — but say so loudly.
        logger.warning(
            "OCTOPUS_TARIFF_STRUCTURE=banded forced on %d distinct levels; "
            "falling back to dynamic thresholds", len(positive),
        )
        return _dynamic(px, f"forced_banded_but_levels:{len(positive)}", dynamic_rule)

    levels = tuple(positive)
    cheap_level: float | None = None
    peak_level: float | None = None
    ratio = _contrast_ratio()
    if len(levels) == 1:
        pass  # flat tariff: no cheap, no peak
    elif len(levels) == 2:
        # Two levels: the MAJORITY level is the day's "standard" rate; the
        # minority is cheap if it sits below it (Go / Economy-7: a 4-7 h
        # off-peak), or a peak if it sits above it (a flat day with one
        # expensive evening block). Tie → the lower level is cheap.
        counts = {lv: 0 for lv in levels}
        for p in px:
            if p > 0.0:
                q = _quantise(p, quantum)
                if q in counts:
                    counts[q] += 1
        lo, hi = levels
        if counts[hi] > counts[lo]:
            if lo <= hi / ratio:
                cheap_level = lo
        else:
            if hi >= lo * ratio:
                peak_level = hi
    else:
        middle = levels[1:-1]
        mid_ref = sum(middle) / len(middle)
        if levels[0] <= mid_ref / ratio:
            cheap_level = levels[0]
        if levels[-1] >= mid_ref * ratio:
            peak_level = levels[-1]

    # Midpoint thresholds (see module docstring). When a side has no band,
    # push the threshold OUTSIDE the price range so no comparison can match.
    if cheap_level is not None:
        nxt = levels[levels.index(cheap_level) + 1]
        cheap_thr = (cheap_level + nxt) / 2.0
    else:
        cheap_thr = levels[0] - 1.0
    if peak_level is not None:
        prv = levels[levels.index(peak_level) - 1]
        peak_thr = (prv + peak_level) / 2.0
    else:
        peak_thr = levels[-1] + 1.0

    band_by_level: dict[float, Band] = {}
    for lv in levels:
        if cheap_level is not None and lv == cheap_level:
            band_by_level[lv] = "cheap"
        elif peak_level is not None and lv == peak_level:
            band_by_level[lv] = "peak"
        else:
            band_by_level[lv] = "standard"

    return TariffStructure(
        kind="banded", levels=levels, cheap_level=cheap_level, peak_level=peak_level,
        cheap_thr=round(cheap_thr, 4), peak_thr=round(peak_thr, 4),
        n_slots=n, reason=f"levels:{len(levels)}", band_by_level=band_by_level,
    )


def thresholds(
    prices: list[float] | tuple[float, ...],
    *,
    dynamic_rule: DynamicRule = "lp",
    quantum: float = 0.01,
) -> tuple[float, float]:
    """``(cheap_thr, peak_thr)`` — band midpoints on a banded tariff, the
    caller's historical percentile formula otherwise."""
    s = detect(prices, quantum=quantum, dynamic_rule=dynamic_rule)
    return s.cheap_thr, s.peak_thr


def classify(
    prices: list[float] | tuple[float, ...],
    *,
    dynamic_rule: DynamicRule = "legacy",
    structure: TariffStructure | None = None,
) -> list[Band]:
    """Per-slot band labels.

    On a dynamic tariff this reproduces the historical ``_classify_slots``
    semantics exactly (``<= 0`` negative, strict ``<`` cheap, strict ``>`` peak)
    so Agile days classify as before. On a banded tariff the band thresholds
    are midpoints, so ``<=`` / ``>=`` are safe and used.
    """
    s = structure or detect(prices, dynamic_rule=dynamic_rule)
    out: list[Band] = []
    if s.is_banded:
        for p in prices:
            out.append(s.band_of(float(p)))
        return out
    for p in prices:
        fp = float(p)
        if fp <= 0:
            out.append("negative")
        elif fp < s.cheap_thr:
            out.append("cheap")
        elif fp > s.peak_thr:
            out.append("peak")
        else:
            out.append("standard")
    return out


# ---------------------------------------------------------------------------
# Local-clock band profile (horizon filler for banded tariffs)
# ---------------------------------------------------------------------------

def band_profile_local(
    tariff_code: str,
    *,
    window_days: int | None = None,
    tz_name: str | None = None,
    now_utc: datetime | None = None,
) -> dict[tuple[int, int], float]:
    """``{(local_hour, local_minute): price}`` from stored rows of a BANDED tariff.

    Banded tariffs follow the local clock, so bucketing by local (hour, minute)
    is correct across the BST→GMT change (each row lands in its own local
    bucket regardless of its UTC hour) — unlike ``db.get_half_hourly_agile_priors``
    which buckets by UTC. Future rows (already-published D+1) are included:
    they are real prices, not history. Returns ``{}`` when the stored series
    does not detect as banded, so callers fall back to the Agile prior path.
    """
    from .. import db  # local import: keep this module importable without a DB

    code = (tariff_code or "").strip()
    if not code:
        return {}
    # The filler asserts "this tariff repeats daily on the local clock" — a
    # stronger claim than threshold banding, so it ALSO needs the product
    # family to be a time-of-use one (Cosy/Go/Flux/Intelligent). A flat test
    # series stored under an AGILE code must keep the Agile prior path.
    if _mode() != "banded" and not is_tou_family(code):
        return {}
    days = int(window_days or getattr(config, "TARIFF_BAND_PRIOR_WINDOW_DAYS", 7))
    tzn = tz_name or str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    try:
        tz = ZoneInfo(tzn)
    except Exception:
        tz = ZoneInfo("Europe/London")
    now = now_utc or datetime.now(UTC)
    rows = db.get_rates_for_period(code, now - timedelta(days=days), now + timedelta(days=3)) or []
    prices: list[float] = []
    buckets: dict[tuple[int, int], dict[float, int]] = {}
    for r in rows:
        try:
            vf = datetime.fromisoformat(str(r["valid_from"]).replace("Z", "+00:00"))
            if vf.tzinfo is None:
                vf = vf.replace(tzinfo=UTC)
            p = float(r["value_inc_vat"])
        except (KeyError, TypeError, ValueError):
            continue
        prices.append(p)
        loc = vf.astimezone(tz)
        key = (loc.hour, 30 if loc.minute >= 30 else 0)
        q = _quantise(p, 0.01)
        buckets.setdefault(key, {})
        buckets[key][q] = buckets[key].get(q, 0) + 1
    s = detect(prices)
    if not s.is_banded:
        return {}
    out: dict[tuple[int, int], float] = {}
    for key, hist in buckets.items():
        # mode per bucket; ties → the cheaper level (never over-price a slot)
        best = sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out[key] = best
    return out


def is_tou_family(tariff_code: str | None) -> bool:
    """True when the product family of ``tariff_code`` is a banded time-of-use
    product in the catalogue (``octopus_products._PRODUCT_PRICING``)."""
    from .octopus_products import _PRODUCT_PRICING
    from .tariff_models import PricingStructure

    code = (tariff_code or "").upper()
    for prefix, ps in _PRODUCT_PRICING.items():
        if prefix in code:
            return ps == PricingStructure.TIME_OF_USE
    return False


# ---------------------------------------------------------------------------
# Display name
# ---------------------------------------------------------------------------

_FAMILY_NAMES = {
    "AGILE": "Agile",
    "COSY": "Cosy",
    "GO": "Go",
    "FLUX": "Flux",
    "INTELLI": "Intelligent Go",
    "SILVER": "Tracker",
    "VAR": "Flexible",
    "FIX": "Fixed",
}


def display_name(tariff_code: str | None = None) -> str:
    """Short human tariff name ("Cosy", "Agile") for titles and labels.

    ``TARIFF_DISPLAY_NAME`` overrides; otherwise derived from the product family
    token of the tariff code (``E-1R-COSY-22-12-08-H`` → ``Cosy``).
    """
    override = str(getattr(config, "TARIFF_DISPLAY_NAME", "") or "").strip()
    if override:
        return override
    code = (tariff_code if tariff_code is not None else getattr(config, "OCTOPUS_TARIFF_CODE", "")) or ""
    parts = [t for t in str(code).upper().split("-") if t]
    # drop register prefix (E / 1R / 2R)
    while parts and parts[0] in ("E", "1R", "2R", "G"):
        parts.pop(0)
    if not parts:
        return "Tariff"
    fam = parts[0]
    for key, name in _FAMILY_NAMES.items():
        if fam.startswith(key):
            return name
    return fam.title()
