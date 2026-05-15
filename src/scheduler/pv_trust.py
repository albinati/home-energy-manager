"""PV-trust helpers for the strict_savings guard rail (issue: #incident-2026-05-15).

Two concerns:

1. **P75 upward bias** — When recent days' forecast under-delivers (skill log
   shows ``actual/predicted`` ratios clustering above 1.0), the LP's
   ``today_factor`` calibration drags PV expectations down, which then biases
   the LP toward grid-charging in the morning "just in case". Computing the
   P-th percentile of the recent ``actual_pv_kwh / predicted_pv_kwh`` ratios
   (configurable, default P75) gives the LP a robust upper-mid estimate that
   reduces this drag. The ratio is applied as a scalar multiplier on top of
   the existing today_factor calibration, only to *today's* slots, only in
   ``strict_savings`` mode.

2. **Hard PV-sufficiency guard rail** — When the LP's forecast PV for today
   (after #1) ≥ battery-headroom + remaining-daytime-load × ``margin``, the
   LP must not grid-charge in any pre-peak slot today. Mirrors the pre-plunge
   constraint at ``lp_optimizer.py:794`` — ``chg[i] <= pv_use[i]`` for the
   matched slots. Bounded loss case (genuinely cloudy day after a sunny
   forecast): one evening hour of grid imp at ~28 p before next-night cheap
   window — small absolute, capped per-day.

Both knobs default to enabled but only fire under ``ENERGY_STRATEGY_MODE=
strict_savings``. ``savings_first`` (the alternate mode, peak_export-enabled)
keeps legacy behaviour untouched.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date as _date, timedelta
from typing import Any

from ..config import config

logger = logging.getLogger(__name__)


@dataclass
class PvTrustBias:
    """Result of computing the recent forecast-skill bias."""

    factor: float = 1.0
    """Multiplier to apply on top of today_factor. 1.0 == no bias."""
    n_samples: int = 0
    """How many recent days contributed."""
    percentile: float = 0.75
    """Percentile used (P75 default)."""
    raw_ratio: float | None = None
    """The unclamped percentile value before min/max bounding."""
    reason: str = ""
    """Why factor is 1.0 (insufficient data, etc.) or "ok"."""
    contributing_days: list[str] = field(default_factory=list)


def compute_pv_trust_bias(
    *,
    db_path: str | None = None,
    as_of_date_utc: _date | None = None,
    lookback_days: int | None = None,
    percentile: float | None = None,
    min_samples: int | None = None,
    min_bias: float | None = None,
    max_bias: float | None = None,
) -> PvTrustBias:
    """Compute the P-th percentile of ``actual_pv_kwh / predicted_pv_kwh``
    over the last ``lookback_days`` days from ``forecast_skill_log``.

    Returns ``PvTrustBias(factor=1.0, reason='...')`` on any of these
    fallbacks:

    * insufficient samples (``< min_samples`` valid days)
    * ``predicted_pv_kwh`` sums to zero (degenerate)
    * exception reading the DB (returns ``factor=1.0, reason='db_error: ...'``)

    The returned ``factor`` is bounded to ``[min_bias, max_bias]`` so a wildly
    optimistic upper tail does not cascade into a no-grid-charge plan that
    can't recover overnight.

    All knobs default to the matching ``config.LP_PV_TRUST_*`` values. Pass
    explicit values from tests / replay scripts.
    """
    lookback = int(
        lookback_days
        if lookback_days is not None
        else getattr(config, "LP_PV_TRUST_LOOKBACK_DAYS", 14)
    )
    pct = float(
        percentile
        if percentile is not None
        else getattr(config, "LP_PV_TRUST_PERCENTILE", 0.75)
    )
    min_n = int(
        min_samples
        if min_samples is not None
        else getattr(config, "LP_PV_TRUST_MIN_SAMPLES", 5)
    )
    lo = float(
        min_bias
        if min_bias is not None
        else getattr(config, "LP_PV_TRUST_MIN_BIAS", 0.7)
    )
    hi = float(
        max_bias
        if max_bias is not None
        else getattr(config, "LP_PV_TRUST_MAX_BIAS", 1.5)
    )
    if lo > hi:
        lo, hi = hi, lo

    if as_of_date_utc is None:
        from datetime import datetime
        as_of_date_utc = datetime.now(UTC).date()

    start = (as_of_date_utc - timedelta(days=lookback)).isoformat()
    end_excl = as_of_date_utc.isoformat()  # exclude today; today is in-progress

    try:
        if db_path is None:
            # Use the project's connection helper so monkey-patches
            # (replay scripts) take effect.
            from .. import db as _db
            conn = _db.get_connection()
        else:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT date_utc,
                   SUM(predicted_pv_kwh) AS pred,
                   SUM(actual_pv_kwh)    AS actual
            FROM forecast_skill_log
            WHERE date_utc >= ?
              AND date_utc <  ?
              AND predicted_pv_kwh IS NOT NULL
              AND actual_pv_kwh    IS NOT NULL
            GROUP BY date_utc
            ORDER BY date_utc
            """,
            (start, end_excl),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("compute_pv_trust_bias: db error %s", exc)
        return PvTrustBias(factor=1.0, percentile=pct, reason=f"db_error: {exc}")

    # Drop degenerate days (zero predicted PV — usually winter / data gap).
    DAY_MIN_KWH = 1.0  # 1 kWh predicted over the day — below this, ratios are noisy
    ratios: list[float] = []
    contributing: list[str] = []
    for r in rows:
        d = dict(r)
        pred = float(d.get("pred") or 0.0)
        actual = float(d.get("actual") or 0.0)
        if pred < DAY_MIN_KWH:
            continue
        ratios.append(actual / pred)
        contributing.append(str(d["date_utc"]))

    if len(ratios) < min_n:
        return PvTrustBias(
            factor=1.0,
            n_samples=len(ratios),
            percentile=pct,
            reason=f"insufficient_samples ({len(ratios)}<{min_n})",
            contributing_days=contributing,
        )

    raw = _percentile(ratios, pct)
    clamped = max(lo, min(hi, raw))
    logger.info(
        "PV-trust bias: %d days, P%d ratio=%.3f → factor=%.3f (clamp [%.2f, %.2f])",
        len(ratios), int(pct * 100), raw, clamped, lo, hi,
    )
    return PvTrustBias(
        factor=clamped,
        n_samples=len(ratios),
        percentile=pct,
        raw_ratio=raw,
        reason="ok",
        contributing_days=contributing,
    )


def _percentile(xs: list[float], p: float) -> float:
    """Linear-interp percentile. ``p`` in [0, 1]."""
    if not xs:
        return 1.0
    sorted_xs = sorted(xs)
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    p = max(0.0, min(1.0, p))
    pos = p * (len(sorted_xs) - 1)
    lo_i = int(pos)
    hi_i = min(lo_i + 1, len(sorted_xs) - 1)
    frac = pos - lo_i
    return sorted_xs[lo_i] * (1 - frac) + sorted_xs[hi_i] * frac


@dataclass
class PvSufficiencyGuardDiag:
    """Audit data for the PV-sufficiency guard rail decision."""

    enabled: bool = False
    strict_savings: bool = False
    applied: bool = False
    reason: str = ""
    forecast_pv_today_kwh: float = 0.0
    expected_load_today_kwh: float = 0.0
    battery_headroom_kwh: float = 0.0
    demand_kwh: float = 0.0
    margin: float = 1.0
    first_peak_slot_idx: int | None = None
    pre_peak_slot_indices: list[int] = field(default_factory=list)

    def to_snapshot_dict(self) -> dict[str, Any]:
        """Compact dict for ``exogenous_snapshot_json.pv_sufficiency_guard``."""
        return {
            "enabled": self.enabled,
            "strict_savings": self.strict_savings,
            "applied": self.applied,
            "reason": self.reason,
            "forecast_pv_today_kwh": round(self.forecast_pv_today_kwh, 3),
            "expected_load_today_kwh": round(self.expected_load_today_kwh, 3),
            "battery_headroom_kwh": round(self.battery_headroom_kwh, 3),
            "demand_kwh": round(self.demand_kwh, 3),
            "margin": self.margin,
            "first_peak_slot_idx": self.first_peak_slot_idx,
            "n_pre_peak_slots": len(self.pre_peak_slot_indices),
        }


def evaluate_pv_sufficiency_guard(
    *,
    slot_starts_utc: list[Any],  # list[datetime]
    pv_avail: list[float],
    base_load_kwh: list[float],
    price_line: list[float],
    peak_threshold_p: float,
    initial_soc_kwh: float,
    soc_max_kwh: float,
    strict_savings: bool,
    enabled: bool | None = None,
    margin: float | None = None,
    as_of_utc: Any = None,  # datetime — defaults to slot_starts_utc[0]
) -> PvSufficiencyGuardDiag:
    """Decide whether to apply the PV-sufficiency guard rail and which slots it
    targets. Pure function — no LP constraints added here. Callers fold the
    returned ``pre_peak_slot_indices`` into their MILP.

    Returns ``PvSufficiencyGuardDiag`` for both the auditing snapshot and the
    LP wiring. ``applied=True`` ⇒ caller must add ``chg[i] <= pv_use[i]`` for
    every ``i`` in ``pre_peak_slot_indices``.

    Mathematical rule
    -----------------
    Let ``T = today (UTC) per slot_starts_utc[0]``,
        ``Pv = Σ pv_avail[i] over i where slot_starts_utc[i].date() == T``,
        ``L  = Σ base_load_kwh[i] over the same i``,
        ``H  = max(0, soc_max_kwh - initial_soc_kwh)``,
        ``D  = H + L``.
    Then the guard fires iff ``strict_savings == True`` and ``enabled == True``
    and ``Pv × margin ≥ D``. When it fires, the targeted slots are every
    today-slot strictly before the first today-slot where
    ``price_line[i] >= peak_threshold_p``.

    Why "today-slots before first peak" rather than "all today-slots":
    - Slots IN the peak window are typically discharge slots; the LP wouldn't
      grid-charge there anyway.
    - Including peak slots would block any unusual cheap mid-peak dip from
      becoming a charge opportunity, which is an over-reach.
    - Leaving the after-peak (evening cheap) window unblocked lets the LP
      pre-load battery for the next day if Outgoing Agile is high overnight.
    """
    enabled_eff = bool(
        enabled if enabled is not None
        else getattr(config, "LP_PV_SUFFICIENCY_GUARD", True)
    )
    margin_eff = float(
        margin if margin is not None
        else getattr(config, "LP_PV_SUFFICIENCY_MARGIN", 1.0)
    )
    diag = PvSufficiencyGuardDiag(
        enabled=enabled_eff,
        strict_savings=strict_savings,
        margin=margin_eff,
    )
    if not enabled_eff:
        diag.reason = "disabled"
        return diag
    if not strict_savings:
        diag.reason = "not_strict_savings"
        return diag

    n = len(slot_starts_utc)
    if n == 0:
        diag.reason = "empty_horizon"
        return diag

    # "Today" = the UTC date of the FIRST slot in the horizon. The LP solves
    # forward from "now", so slot 0's date is the right anchor; tomorrow's
    # slots are scoped out by the date filter below.
    today_utc = slot_starts_utc[0].astimezone(UTC).date()

    today_idx = [
        i for i in range(n)
        if slot_starts_utc[i].astimezone(UTC).date() == today_utc
    ]
    if not today_idx:
        diag.reason = "no_today_slots"
        return diag

    first_peak: int | None = None
    for i in today_idx:
        if price_line[i] >= peak_threshold_p:
            first_peak = i
            break
    diag.first_peak_slot_idx = first_peak

    pre_peak_today = [
        i for i in today_idx
        if first_peak is None or i < first_peak
    ]
    diag.pre_peak_slot_indices = pre_peak_today

    forecast_pv = sum(float(pv_avail[i]) for i in today_idx)
    expected_load = sum(float(base_load_kwh[i]) for i in today_idx)
    headroom = max(0.0, float(soc_max_kwh) - float(initial_soc_kwh))
    demand = headroom + expected_load
    diag.forecast_pv_today_kwh = forecast_pv
    diag.expected_load_today_kwh = expected_load
    diag.battery_headroom_kwh = headroom
    diag.demand_kwh = demand

    if not pre_peak_today:
        diag.applied = False
        diag.reason = "no_pre_peak_slots"
        return diag

    if forecast_pv * margin_eff >= demand:
        diag.applied = True
        diag.reason = "sufficient_pv"
    else:
        diag.applied = False
        diag.reason = "insufficient_pv"
    return diag
