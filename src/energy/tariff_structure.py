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
    short_ok: bool = False,
) -> TariffStructure:
    """Decide banded vs dynamic for a price series and derive thresholds.

    Pure: no I/O. Negative/zero prices are ignored for level counting (a
    plunge on Agile is still dynamic; a banded tariff never goes negative).
    ``short_ok`` skips the minimum-slot gates — for callers that already know
    the tariff family is banded (calendar/floor on a TOU code) and classify a
    PARTIAL local day; without it an 8-slot Cosy tail fell back to the
    day-relative tiers and read the day band as "expensive" (#805 review).
    """
    px = [float(p) for p in prices if p is not None and math.isfinite(float(p))]
    n = len(px)
    mode = _mode()
    if mode == "dynamic":
        return _dynamic(px, "forced:dynamic", dynamic_rule)
    # Family gate (review F1): when a tariff code IS configured and it is not a
    # time-of-use product, never band — a short Agile window with repeated
    # prices (12-23 slots, ≥ n-8 duplicates) could otherwise pass the level
    # gate. An empty code (pure/library use) stays data-driven.
    configured = str(getattr(config, "OCTOPUS_TARIFF_CODE", "") or "").strip()
    if mode != "banded" and configured and not is_tou_family(configured):
        return _dynamic(px, "family:not_tou", dynamic_rule)
    if n < _min_slots() and mode != "banded" and not short_ok:
        return _dynamic(px, f"short:{n}<{_min_slots()}", dynamic_rule)

    positive_prices = [p for p in px if p > 0.0]
    if len(positive_prices) < _min_slots() and mode != "banded" and not short_ok:
        # An Agile plunge day with a handful of positive prices must not read
        # as "banded" just because the positives happen to be few (review M3).
        return _dynamic(px, f"short_positive:{len(positive_prices)}<{_min_slots()}", dynamic_rule)
    positive = sorted({_quantise(p, quantum) for p in positive_prices})
    if not positive:
        return _dynamic(px, "no_positive_prices", dynamic_rule)
    # Primary gate: the RAW distinct-level count. An Agile day has ~48 distinct
    # prices whatever its spread (a flat 20-24p Agile day must stay dynamic so
    # its quartiles still split cheap/peak as on main); a banded tariff has a
    # handful, at most 2× the band count across one reprice.
    max_raw = _max_levels() * 2
    if len(positive) > max_raw and mode != "banded":
        return _dynamic(px, f"levels:{len(positive)}>{max_raw}", dynamic_rule)
    # Density guard (review F1): a band is a level that REPEATS — require ≥ 3
    # slots per distinct level on average (Cosy 3/48, reprice 6/96 pass; a
    # 12-slot Agile window with 6 paired prices fails).
    if len(positive) * 3 > len(positive_prices) and mode != "banded" and not short_ok:
        return _dynamic(px, f"sparse_levels:{len(positive)}/{len(positive_prices)}", dynamic_rule)
    ratio = _contrast_ratio()
    # NB the span clustering assumes any reprice moves a band by LESS than the
    # contrast ratio (25 %). Octopus Cosy reprices have been < 10 % (review F2);
    # a ≥ 25 % jump of one band would split it into two for the overlap window.
    # Then cluster quantised levels whose whole SPAN stays within the contrast
    # ratio into ONE band (review M1): a horizon spanning an Octopus reprice
    # (Cosy 12.49→12.99, 25.45→26.5, 38.17→39.7) is six raw levels but three
    # bands; without this it fell back to the percentile misread for two days
    # and starved the band filler for a week. Span-based (not chained) so a
    # continuum can never collapse into one cluster.
    clusters: list[list[float]] = []
    for lv in positive:
        if clusters and lv < clusters[-1][0] * ratio:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    if len(clusters) > _max_levels() and mode != "banded":
        return _dynamic(px, f"levels:{len(clusters)}>{_max_levels()}", dynamic_rule)
    if len(clusters) > _max_levels():
        logger.warning(
            "OCTOPUS_TARIFF_STRUCTURE=banded forced on %d distinct bands; "
            "falling back to dynamic thresholds", len(clusters),
        )
        return _dynamic(px, f"forced_banded_but_levels:{len(clusters)}", dynamic_rule)

    levels = tuple(positive)
    band_lo = [c[0] for c in clusters]          # cheapest raw level of each band
    band_hi = [c[-1] for c in clusters]         # dearest raw level of each band
    band_mean = [sum(c) / len(c) for c in clusters]
    cheap_level: float | None = None
    peak_level: float | None = None
    cheap_idx: int | None = None
    peak_idx: int | None = None
    if len(clusters) == 1:
        pass  # flat tariff: no cheap, no peak
    elif len(clusters) == 2:
        # Two bands: the MAJORITY band is the day's "standard" rate; the
        # minority is cheap if it sits below it (Go / Economy-7: a 4-7 h
        # off-peak), or a peak if it sits above it (a flat day with one
        # expensive evening block). Tie → the HIGHER band is the peak (never
        # import at the dear level by mistake; review M2 pins this).
        counts = [0, 0]
        for p in positive_prices:
            q = _quantise(p, quantum)
            counts[0 if q <= band_hi[0] else 1] += 1
        if counts[1] > counts[0]:
            if band_mean[0] <= band_mean[1] / ratio:
                cheap_level, cheap_idx = band_lo[0], 0
        else:
            if band_mean[1] >= band_mean[0] * ratio:
                peak_level, peak_idx = band_hi[1], 1
    else:
        mid_ref = sum(band_mean[1:-1]) / len(band_mean[1:-1])
        if band_mean[0] <= mid_ref / ratio:
            cheap_level, cheap_idx = band_lo[0], 0
        if band_mean[-1] >= mid_ref * ratio:
            peak_level, peak_idx = band_hi[-1], len(clusters) - 1

    # Midpoint thresholds (see module docstring), between the dearest level of
    # the cheap band and the cheapest level of the next band (and vice versa
    # for the peak). When a side has no band, push the threshold OUTSIDE the
    # price range so no comparison can match.
    if cheap_idx is not None:
        cheap_thr = (band_hi[cheap_idx] + band_lo[cheap_idx + 1]) / 2.0
    else:
        cheap_thr = levels[0] - 1.0
    if peak_idx is not None:
        peak_thr = (band_hi[peak_idx - 1] + band_lo[peak_idx]) / 2.0
    else:
        peak_thr = levels[-1] + 1.0

    band_by_level: dict[float, Band] = {}
    for lv in levels:
        if cheap_idx is not None and lv <= band_hi[cheap_idx]:
            band_by_level[lv] = "cheap"
        elif peak_idx is not None and lv >= band_lo[peak_idx]:
            band_by_level[lv] = "peak"
        else:
            band_by_level[lv] = "standard"

    return TariffStructure(
        kind="banded", levels=levels, cheap_level=cheap_level, peak_level=peak_level,
        cheap_thr=round(cheap_thr, 4), peak_thr=round(peak_thr, 4),
        n_slots=n, reason=f"levels:{len(levels)}/bands:{len(clusters)}", band_by_level=band_by_level,
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
    latest: dict[tuple[int, int], tuple[datetime, float]] = {}
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
        # The MOST RECENT published row wins per bucket (not the mode): after
        # an Octopus reprice the new band price is the truth for the tail of
        # the horizon, and the stored price is used as-is (no quantisation).
        if key not in latest or vf > latest[key][0]:
            latest[key] = (vf, p)
    # Detect on the 48 latest-per-bucket values, not every stored row: a window
    # spanning two reprices (9 raw levels) would otherwise read as dynamic and
    # silently hand the local-clock tariff to the UTC-keyed Agile priors (F3).
    s = detect([p for (_, p) in latest.values()])
    if not s.is_banded:
        logger.info("band_profile_local(%s): stored series not banded (%s) — Agile prior path", code, s.reason)
        return {}
    return {key: p for key, (_, p) in latest.items()}


def prefer_plan_thresholds(tariff_code: str | None = None) -> bool:
    """Should a consumer with a STATIC pence cut-off (heartbeat low-SoC alert,
    heating-plan tiers, status next-cheap) switch to the plan's stored
    thresholds? Only on a banded (TOU-family) tariff, where the static 12p/25p
    cut-offs misread the bands. On Agile the historical static behaviour is
    kept bit-for-bit (review H1/H2)."""
    mode = _mode()
    if mode == "dynamic":
        return False
    if mode == "banded":
        return True
    code = tariff_code if tariff_code is not None else getattr(config, "OCTOPUS_TARIFF_CODE", "")
    return is_tou_family(code)


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
