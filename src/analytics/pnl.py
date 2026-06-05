"""Volume-weighted cost and shadow PnL from execution_log."""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .. import db
from ..config import config
from .shadow_pricing import fixed_shadow_rate_pence, svt_rate_pence

logger = logging.getLogger(__name__)


def _day_bounds(d: date) -> tuple[str, str]:
    start = datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _seg_flat_rate_pence() -> float:
    """The flat Smart Export Guarantee rate (p/kWh) the household is actually paid."""
    return float(config.EXPORT_SEG_RATE_PENCE or 0)


def _export_meter_start() -> date | None:
    """Export-meter activation date; export before it earns nothing (MPAN not live)."""
    raw = (config.EXPORT_METER_START_DATE or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning("EXPORT_METER_START_DATE=%r is not a valid ISO date — ignoring", raw)
        return None


def export_revenues_for_day(day: date) -> dict[str, float]:
    """BOTH export valuations for ``day`` on the same measured kWh:

    - ``seg_flat_pence`` — Σ export_kwh × flat ``EXPORT_SEG_RATE_PENCE`` (the real
      SEG bill).
    - ``agile_pence`` — Σ per-slot export_kwh × Outgoing Agile rate
      (``agile_export_rates``; flat ``EXPORT_RATE_PENCE`` for unmatched slots) —
      the alternative tariff, for the side-by-side comparison.

    Per-slot export kWh from :func:`db.half_hourly_grid_export_kwh_for_day`. Zero
    before :func:`_export_meter_start` (export MPAN not yet live). No VAT on export.
    """
    empty = {"seg_flat_pence": 0.0, "agile_pence": 0.0, "export_kwh": 0.0, "agile_avg_p": 0.0}
    start = _export_meter_start()
    if start is not None and day < start:
        return empty
    bucket_kwh = db.half_hourly_grid_export_kwh_for_day(day)
    if not bucket_kwh:
        return empty
    a, b = _day_bounds(day)
    rate_rows = (
        db.get_agile_export_rates_in_range(a, b)
        if (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
        else []
    )
    rate_by_start: dict[str, float] = {}
    for r in rate_rows:
        try:
            rate_by_start[r["valid_from"].replace("+00:00", "Z")] = float(r["value_inc_vat"])
        except (KeyError, TypeError, ValueError):
            continue
    flat_agile = float(config.EXPORT_RATE_PENCE)
    seg = _seg_flat_rate_pence()
    seg_rev = 0.0
    agile_rev = 0.0
    total_kwh = 0.0
    for slot_iso, kwh in bucket_kwh.items():
        total_kwh += kwh
        seg_rev += kwh * seg
        agile_rev += kwh * rate_by_start.get(slot_iso, flat_agile)
    return {
        "seg_flat_pence": seg_rev,
        "agile_pence": agile_rev,
        "export_kwh": total_kwh,
        "agile_avg_p": (agile_rev / total_kwh) if total_kwh > 0 else 0.0,
    }


def _realised_export_pence(day: date) -> tuple[float, float]:
    """Realised export revenue (pence) + kWh, valued at the ACTUAL export tariff
    the household is paid on (``config.EXPORT_TARIFF_MODE``): ``seg_flat`` → flat
    SEG, ``outgoing_agile`` → per-slot Agile. Same signature as before so every
    consumer (compute_daily_pnl, fair_compare, brief, /energy/today-cumulative,
    VWAP) stays correct. Rates are inc-VAT (no VAT on export)."""
    rev = export_revenues_for_day(day)
    actual = (
        rev["agile_pence"]
        if config.EXPORT_TARIFF_MODE == "outgoing_agile"
        else rev["seg_flat_pence"]
    )
    return actual, rev["export_kwh"]


def agile_import_rate_by_slot(day: date) -> dict[str, float]:
    """Per-slot realised Agile import rate (p/kWh) for ``day``, keyed by ISO
    half-hour slot start. Source: ``execution_log.agile_price_pence`` (the same
    rate the LP priced against). Rates may be NEGATIVE on plunge-price slots.

    Shared by :func:`_realised_import_pence` and the fair tariff comparison so the
    realised bill and the negative-price credit are computed off one rate map.
    """
    a, b = _day_bounds(day)
    rate_rows = db.get_execution_logs(from_ts=a, to_ts=b, limit=5000)
    rate_by_start: dict[str, float] = {}
    for r in rate_rows:
        ts = r.get("timestamp")
        p = r.get("agile_price_pence")
        if ts is None or p is None:
            continue
        # Normalise heartbeat ts (microsecond precision) to half-hour slot key
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        slot_min = (t.minute // 30) * 30
        slot = t.replace(minute=slot_min, second=0, microsecond=0)
        slot_iso = slot.astimezone(UTC).isoformat().replace("+00:00", "Z")
        rate_by_start.setdefault(slot_iso, float(p))
    return rate_by_start


def _import_buckets_preferring_meter(day: date) -> dict[str, float]:
    """Per-slot import kWh, preferring the Octopus METER over Fox telemetry.

    The Octopus import meter is the billed truth; Fox CT-clamp telemetry under-
    reads ~4% (sampling + >30min gaps). For a PAST day with metered data we use
    it; today/live (no backfill yet) falls back to Fox.

    Priority:
      1. day < today AND metered ``execution_log`` rows exist → per-slot metered
         consumption (the import MPAN reads grid import; keeps slot-level pricing).
      2. only a daily ``octopus_daily_meter`` total → scale Fox per-slot buckets by
         ``meter_total / fox_total`` (Fox shape, meter magnitude — approximate).
      3. else → Fox per-slot (``half_hourly_grid_import_kwh_for_day``).
    """
    fox = db.half_hourly_grid_import_kwh_for_day(day)
    # Local "today" (the backfill runs on the local plan day); a UTC date would
    # mis-flag yesterday-local as "today" during the 00:00-01:00 BST window.
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(config.BULLETPROOF_TIMEZONE)).date()
    if day >= today:
        return fox
    # 1. per-slot metered execution_log. The backfill tags real metered readings
    # "metered"; slots it had to synthesise (heartbeat gap) are "metered_synthetic"
    # — both are metered truth, so accept either (else gap days under-report).
    a, b = _day_bounds(day)
    metered: dict[str, float] = {}
    for r in db.get_execution_logs(from_ts=a, to_ts=b, limit=5000):
        if not (r.get("source") or "").startswith("metered"):
            continue
        ts = r.get("timestamp")
        kwh = r.get("consumption_kwh")
        if ts is None or kwh is None:
            continue
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        slot = t.replace(minute=(t.minute // 30) * 30, second=0, microsecond=0)
        slot_iso = slot.astimezone(UTC).isoformat().replace("+00:00", "Z")
        metered[slot_iso] = metered.get(slot_iso, 0.0) + float(kwh)
    fox_total = sum(fox.values())
    # SANITY GUARD: the metered backfill can be INCOMPLETE (missing slots/days) —
    # observed on prod where a month's metered total was ~40% of Fox. Trusting a
    # partial meter UNDER-reports the bill far worse than Fox's ~4% noise. So only
    # prefer the meter when its total is plausibly complete vs Fox (within ±35%);
    # otherwise Fox is the better source. (When Fox itself is ~0, accept the meter.)
    def _plausible(meter_total: float) -> bool:
        if fox_total <= 0.01:
            return meter_total > 0
        return 0.65 <= (meter_total / fox_total) <= 1.5
    # 1. per-slot metered execution_log (if plausibly complete)
    if metered and _plausible(sum(metered.values())):
        return metered
    # 2. scale Fox by the daily meter total (if plausibly complete)
    meter = db.get_octopus_daily_meter(day.isoformat())
    if meter and meter.get("import_kwh") and fox_total > 0 and _plausible(float(meter["import_kwh"])):
        ratio = float(meter["import_kwh"]) / fox_total
        return {k: v * ratio for k, v in fox.items()}
    # 3. Fox (default — and the fallback when the meter looks incomplete)
    return fox


def _realised_import_pence(day: date) -> tuple[float, float]:
    """Per-slot ``import_kwh × Agile_slot_rate``, preferring the metered grid
    import (the billed truth) over Fox telemetry — see
    :func:`_import_buckets_preferring_meter`.

    Returns ``(import_cost_pence, import_kwh_total)``. Per-slot import rates come
    from ``execution_log.agile_price_pence`` (inc-VAT; the same the LP priced
    against). Negative rates pass through as a credit, no clamp.

    Issue #306: replaces the load-based pre-image used by the legacy
    ``realised_import_gbp`` field, which billed *household load* (not net grid
    import) at Agile rates and inflated absolute £ figures ~3-4×.
    """
    bucket_kwh = _import_buckets_preferring_meter(day)
    if not bucket_kwh:
        return 0.0, 0.0
    rate_by_start = agile_import_rate_by_slot(day)
    cost = 0.0
    total_kwh = 0.0
    matched = 0
    unmatched_kwh = 0.0
    for slot_iso, kwh in bucket_kwh.items():
        rate = rate_by_start.get(slot_iso)
        total_kwh += kwh
        if rate is not None:
            cost += kwh * rate
            matched += 1
        else:
            unmatched_kwh += kwh
    if unmatched_kwh > 0:
        logger.info(
            "Realised import %s: %d/%d slots matched Agile rates; %.3f kWh unmatched (priced at 0)",
            day.isoformat(), matched, len(bucket_kwh), unmatched_kwh,
        )
    return cost, total_kwh


def compute_daily_pnl(day: date) -> dict[str, Any]:
    """Daily PnL with apples-to-apples standing-charge accounting.

    Two parallel views, both included in every result:

    1. **Real-money fields** (``realised_net_cost_gbp``, ``*_shadow_real_gbp``,
       ``delta_vs_*_real_gbp``, ``import_kwh``): computed from MEASURED grid
       traffic in ``pv_realtime_history`` × per-slot tariff rates. This is
       what actually moved through the meter.

    2. **Load-billed counterfactual fields** (``realised_cost_gbp``,
       ``*_shadow_gbp``, ``delta_vs_*_gbp``, ``kwh``): the legacy formulation
       that bills the *whole household load* at Agile/SVT/Fixed rates. Useful
       for "what would I pay if I had no solar/battery" framing but NOT what
       the household actually paid. Inflates absolute £ ~3-4× on a typical
       solar-rich day. Kept for backward compatibility (#306) — the brief
       markdown labels these fields explicitly so callers can't confuse them.

    All shadow costs include the standing charge so deltas reflect what the
    household would actually pay on each tariff.
    """
    a, b = _day_bounds(day)
    rows = db.get_execution_logs(from_ts=a, to_ts=b, limit=5000)
    svt = svt_rate_pence()
    fixed = fixed_shadow_rate_pence()
    realised_import_load = 0.0      # legacy: load × Agile (counterfactual)
    svt_energy_cost = 0.0
    fixed_energy_cost = 0.0
    kwh_sum = 0.0                   # household LOAD total (legacy)
    cheap_kwh = 0.0
    peak_kwh = 0.0
    for r in rows:
        kwh = float(r.get("consumption_kwh") or 0)
        p = r.get("agile_price_pence")
        if p is None:
            continue
        kwh_sum += kwh
        realised_import_load += kwh * float(p)
        svt_energy_cost += kwh * svt
        fixed_energy_cost += kwh * fixed
        sk = (r.get("slot_kind") or "").lower()
        if sk == "peak":
            peak_kwh += kwh
        if sk in ("negative", "cheap"):
            cheap_kwh += kwh

    # One export integration; derive the actual from the mode (avoids calling
    # _realised_export_pence, which re-runs export_revenues_for_day internally).
    export_both = export_revenues_for_day(day)
    export_revenue = (
        export_both["agile_pence"]
        if config.EXPORT_TARIFF_MODE == "outgoing_agile"
        else export_both["seg_flat_pence"]
    )
    export_kwh = export_both["export_kwh"]
    real_import_cost, import_kwh = _realised_import_pence(day)
    standing_p = float(config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY or 0)
    # Per-tariff fairness (matches src/analytics/fair_compare): SVT uses its own
    # standing; non-Agile shadows don't earn the Outgoing Agile export revenue —
    # they'd be on the same flat SEG the household is actually on (EXPORT_SEG_RATE_PENCE).
    svt_standing_p = float(config.SVT_STANDING_PENCE_PER_DAY or 0) or standing_p
    seg_export = export_kwh * _seg_flat_rate_pence()

    # === Legacy "load-billed" view (counterfactual: if no solar/battery) ===
    realised = realised_import_load + standing_p - export_revenue
    svt_cost = svt_energy_cost + standing_p
    fixed_cost = fixed_energy_cost + standing_p
    alpha_svt = (svt_cost - realised) / 100.0
    alpha_fixed = (fixed_cost - realised) / 100.0

    # === Real-money view (measured grid traffic × rates) ===
    realised_real = real_import_cost + standing_p - export_revenue
    svt_real = (import_kwh * svt) + svt_standing_p - seg_export
    fixed_real = (import_kwh * fixed) + standing_p - seg_export
    alpha_svt_real = (svt_real - realised_real) / 100.0
    alpha_fixed_real = (fixed_real - realised_real) / 100.0

    out: dict[str, Any] = {
        "date": day.isoformat(),
        # === Real-money (preferred — use these for user-facing £ figures) ===
        "realised_net_cost_gbp": round(realised_real / 100.0, 4),
        "import_kwh": round(import_kwh, 3),
        "import_cost_gbp": round(real_import_cost / 100.0, 4),
        "svt_shadow_real_gbp": round(svt_real / 100.0, 4),
        "fixed_shadow_real_gbp": round(fixed_real / 100.0, 4),
        "delta_vs_svt_real_gbp": round(alpha_svt_real, 4),
        "delta_vs_fixed_real_gbp": round(alpha_fixed_real, 4),
        # === Shared (true regardless of view) ===
        "export_revenue_gbp": round(export_revenue / 100.0, 4),
        # Both export valuations for the side-by-side comparison (summed by
        # compute_period_pnl). actual = seg or agile per EXPORT_TARIFF_MODE.
        "export_revenue_seg_gbp": round(export_both["seg_flat_pence"] / 100.0, 4),
        "export_revenue_agile_gbp": round(export_both["agile_pence"] / 100.0, 4),
        "export_kwh": round(export_kwh, 3),
        "standing_charge_gbp": round(standing_p / 100.0, 4),
        # === Legacy "load-billed" counterfactual (DO NOT quote as real money) ===
        "kwh": round(kwh_sum, 3),
        "realised_cost_gbp": round(realised / 100.0, 4),
        "realised_import_gbp": round(realised_import_load / 100.0, 4),
        "svt_shadow_gbp": round(svt_cost / 100.0, 4),
        "fixed_shadow_gbp": round(fixed_cost / 100.0, 4),
        "delta_vs_svt_gbp": round(alpha_svt, 4),
        "delta_vs_fixed_gbp": round(alpha_fixed, 4),
        "cheap_slot_kwh": round(cheap_kwh, 3),
        "peak_kwh": round(peak_kwh, 3),
    }

    # Optional: legacy fixed tariff comparison (e.g. previous British Gas plan).
    # Both real and load-billed flavours emitted when configured.
    bg_rate = float(config.FIXED_TARIFF_RATE_PENCE or 0)
    bg_standing = float(config.FIXED_TARIFF_STANDING_PENCE_PER_DAY or 0)
    if bg_rate > 0 and bg_standing > 0:
        bg_cost_load = (kwh_sum * bg_rate) + bg_standing
        bg_cost_real = (import_kwh * bg_rate) + bg_standing - seg_export
        out["fixed_tariff_label"] = config.FIXED_TARIFF_LABEL or "fixed tariff"
        out["fixed_tariff_shadow_real_gbp"] = round(bg_cost_real / 100.0, 4)
        out["delta_vs_fixed_tariff_real_gbp"] = round((bg_cost_real - realised_real) / 100.0, 4)
        out["fixed_tariff_shadow_gbp"] = round(bg_cost_load / 100.0, 4)
        out["delta_vs_fixed_tariff_gbp"] = round((bg_cost_load - realised) / 100.0, 4)

    return out


def compute_vwap(day: date) -> float | None:
    """Realised import VWAP — the average p/kWh we actually paid for grid
    imports today.

    Uses MEASURED grid import (``pv_realtime_history.grid_import_kw`` →
    half-hour buckets) weighted by Agile rates, NOT total household
    consumption. The LP's ``target_vwap`` is also import-only; matching
    the weighting basis makes the two directly comparable.

    Prior bug: this used ``execution_log.consumption_kwh`` (total load),
    which conflated battery-discharge + solar self-use with grid spend.
    On a heavy-self-use day with 1 kWh of import, that produced a notional
    ~17 p/kWh that looked like a 19 p/kWh slippage against a -2 p target.
    """
    imp_pence, imp_kwh = _realised_import_pence(day)
    if imp_kwh <= 0:
        return None
    return round(imp_pence / imp_kwh, 4)


def compute_slippage(day: date) -> float | None:
    vwap = compute_vwap(day)
    if vwap is None:
        return None
    tgt = db.get_daily_target(day)
    if not tgt or tgt.get("target_vwap") is None:
        return None
    return round(vwap - float(tgt["target_vwap"]), 4)


def _slot_anchor_iso(timestamp_iso: str) -> str | None:
    """Floor a heartbeat-tick ISO timestamp to its 30-minute slot anchor,
    matching the key format used by ``db.half_hourly_grid_*_kwh_for_day``."""
    if not timestamp_iso:
        return None
    s = timestamp_iso.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    d = d.astimezone(UTC).replace(minute=(d.minute // 30) * 30, second=0, microsecond=0)
    return d.isoformat().replace("+00:00", "Z")


def _slot_index_by_anchor(day: date) -> tuple[dict[str, float], dict[str, str]]:
    """Walk execution_log for the day, indexing price + slot_kind by 30-min
    anchor (matches the import-bucket keying)."""
    a, b = _day_bounds(day)
    rows = db.get_execution_logs(from_ts=a, to_ts=b, limit=5000)
    price_by: dict[str, float] = {}
    kind_by: dict[str, str] = {}
    for r in rows:
        anchor = _slot_anchor_iso(r.get("timestamp") or "")
        if not anchor:
            continue
        p = r.get("agile_price_pence")
        if p is not None:
            price_by.setdefault(anchor, float(p))
        k = (r.get("slot_kind") or "").lower()
        if k:
            kind_by.setdefault(anchor, k)
    return price_by, kind_by


def compute_arbitrage_efficiency(day: date) -> float | None:
    """Percent of ACTUAL GRID IMPORT that landed in today's cheap quartile.

    Same weighting basis as ``compute_vwap`` — measured grid import per
    slot, not total consumption. Prior bug treated self-use as if it were
    grid spend, which inflated the "cheap-quartile share" when most load
    was actually covered by battery/solar (the cheap-slot question doesn't
    apply to non-imported kWh).
    """
    bucket_kwh = db.half_hourly_grid_import_kwh_for_day(day)
    if not bucket_kwh:
        return None
    price_by, _ = _slot_index_by_anchor(day)
    prices = sorted(price_by.values())
    if len(prices) < 4:
        return None
    q1 = prices[max(0, len(prices) // 4 - 1)]
    imp = 0.0
    cheap = 0.0
    for slot_iso, kwh in bucket_kwh.items():
        p = price_by.get(slot_iso)
        if p is None or kwh <= 0:
            continue
        imp += kwh
        if p <= q1:
            cheap += kwh
    return round(100.0 * cheap / imp, 2) if imp > 0 else None


def compute_peak_ratio(day: date) -> float | None:
    """Percent of ACTUAL GRID IMPORT that landed in peak slots.

    Same weighting basis as ``compute_vwap``. Prior bug used total
    consumption, which made the ratio meaningless on solar-heavy days
    (all consumption coincides with daylight peaks but most of it was
    self-use, not paid imports).
    """
    bucket_kwh = db.half_hourly_grid_import_kwh_for_day(day)
    if not bucket_kwh:
        return None
    _, kind_by = _slot_index_by_anchor(day)
    tot = 0.0
    peak = 0.0
    for slot_iso, kwh in bucket_kwh.items():
        if kwh <= 0:
            continue
        tot += kwh
        if kind_by.get(slot_iso) == "peak":
            peak += kwh
    return round(100.0 * peak / tot, 2) if tot > 0 else None


def _agile_start_date() -> date | None:
    """Parse ``config.AGILE_TARIFF_START_DATE``; ``None`` when unset/invalid."""
    raw = (config.AGILE_TARIFF_START_DATE or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning(
            "AGILE_TARIFF_START_DATE=%r is not a valid ISO date — ignoring clamp", raw,
        )
        return None


def compute_period_pnl(
    start_day: date, end_day: date, *, label: str = "", include_daily: bool = False
) -> dict[str, Any]:
    """Aggregate daily PnL across a date range (both bounds inclusive).

    ``include_daily=True`` attaches an ``out["daily"]`` list of
    ``{date, import_kwh, export_kwh}`` (one per day in the clamped range) so a
    caller that also needs per-day kWh (e.g. chart bars that must sum to the
    foot, #470) gets them from this single daily loop instead of re-running
    ``compute_daily_pnl`` itself.

    Sums every numeric component of ``compute_daily_pnl`` so callers get a full
    breakdown — not just the deltas. Standing charge is implicitly accounted for
    because each day's compute_daily_pnl already includes the daily standing fee
    (so summing N days gives N × standing automatically).

    The start date is clamped upward to ``config.AGILE_TARIFF_START_DATE`` when
    set — pre-switch days were on a different tariff and would pollute the
    realised cost + shadow deltas. When clamping happens, the response carries
    ``clamped=True`` + ``clamp_reason`` + ``requested_start`` so OpenClaw can
    render an honest "since 2026-04-01" qualifier.

    Returns a dict with the same keys as ``compute_daily_pnl`` plus
    ``period_start``, ``period_end``, ``n_days``, ``label``. The optional legacy
    fixed tariff (``fixed_tariff_*``) is included only if at least one day
    surfaced it (i.e. ``FIXED_TARIFF_*`` env vars were set during that solve).
    """
    if end_day < start_day:
        start_day, end_day = end_day, start_day

    requested_start = start_day
    clamped = False
    clamp_reason: str | None = None
    agile_start = _agile_start_date()
    if agile_start and start_day < agile_start:
        if end_day < agile_start:
            # Entire requested period predates Agile — return an empty shape
            # so the caller can render "n/a (since YYYY-MM-DD)".
            return {
                "label": label or f"{requested_start.isoformat()}..{end_day.isoformat()}",
                "period_start": agile_start.isoformat(),
                "period_end": end_day.isoformat(),
                "requested_start": requested_start.isoformat(),
                "clamped": True,
                "clamp_reason": (
                    f"Entire range predates AGILE_TARIFF_START_DATE={agile_start.isoformat()}"
                ),
                "n_days": 0,
                "realised_net_cost_gbp": 0.0,
                "import_kwh": 0.0,
                "import_cost_gbp": 0.0,
                "svt_shadow_real_gbp": 0.0,
                "fixed_shadow_real_gbp": 0.0,
                "delta_vs_svt_real_gbp": 0.0,
                "delta_vs_fixed_real_gbp": 0.0,
                "kwh": 0.0,
                "realised_cost_gbp": 0.0,
                "realised_import_gbp": 0.0,
                "export_revenue_gbp": 0.0,
                "export_revenue_seg_gbp": 0.0,
                "export_revenue_agile_gbp": 0.0,
                "export_kwh": 0.0,
                "standing_charge_gbp": 0.0,
                "svt_shadow_gbp": 0.0,
                "fixed_shadow_gbp": 0.0,
                "delta_vs_svt_gbp": 0.0,
                "delta_vs_fixed_gbp": 0.0,
                "cheap_slot_kwh": 0.0,
                "peak_kwh": 0.0,
            }
        start_day = agile_start
        clamped = True
        clamp_reason = (
            f"Clamped to AGILE_TARIFF_START_DATE={agile_start.isoformat()} "
            f"(before that the household was on a different tariff)"
        )

    n = (end_day - start_day).days + 1

    totals = {
        # Real-money axis
        "realised_net_cost_gbp": 0.0,
        "import_kwh": 0.0,
        "import_cost_gbp": 0.0,
        "svt_shadow_real_gbp": 0.0,
        "fixed_shadow_real_gbp": 0.0,
        "delta_vs_svt_real_gbp": 0.0,
        "delta_vs_fixed_real_gbp": 0.0,
        # Shared
        "export_revenue_gbp": 0.0,
        "export_revenue_seg_gbp": 0.0,
        "export_revenue_agile_gbp": 0.0,
        "export_kwh": 0.0,
        "standing_charge_gbp": 0.0,
        # Legacy load-billed axis
        "kwh": 0.0,
        "realised_cost_gbp": 0.0,
        "realised_import_gbp": 0.0,
        "svt_shadow_gbp": 0.0,
        "fixed_shadow_gbp": 0.0,
        "delta_vs_svt_gbp": 0.0,
        "delta_vs_fixed_gbp": 0.0,
        "cheap_slot_kwh": 0.0,
        "peak_kwh": 0.0,
    }
    bg_shadow = 0.0
    bg_delta = 0.0
    bg_shadow_real = 0.0
    bg_delta_real = 0.0
    bg_label: str | None = None
    bg_seen = False
    daily_rows: list[dict[str, Any]] = []

    for i in range(n):
        d = start_day + timedelta(days=i)
        p = compute_daily_pnl(d)
        for k in totals:
            totals[k] += float(p.get(k) or 0.0)
        if include_daily:
            daily_rows.append({
                "date": d.isoformat(),
                "import_kwh": round(float(p.get("import_kwh") or 0.0), 3),
                "export_kwh": round(float(p.get("export_kwh") or 0.0), 3),
            })
        if "fixed_tariff_shadow_gbp" in p:
            bg_seen = True
            bg_shadow += float(p["fixed_tariff_shadow_gbp"])
            bg_delta += float(p["delta_vs_fixed_tariff_gbp"])
            bg_shadow_real += float(p.get("fixed_tariff_shadow_real_gbp") or 0.0)
            bg_delta_real += float(p.get("delta_vs_fixed_tariff_real_gbp") or 0.0)
            bg_label = bg_label or p.get("fixed_tariff_label")

    base_label = label or f"{start_day.isoformat()}..{end_day.isoformat()}"
    if clamped:
        base_label = f"{base_label} (since {start_day.isoformat()})"

    out: dict[str, Any] = {
        "label": base_label,
        "period_start": start_day.isoformat(),
        "period_end": end_day.isoformat(),
        "n_days": n,
    }
    if clamped:
        out["requested_start"] = requested_start.isoformat()
        out["clamped"] = True
        out["clamp_reason"] = clamp_reason
    out.update({k: round(v, 4 if not k.endswith("_kwh") else 3) for k, v in totals.items()})
    if include_daily:
        out["daily"] = daily_rows
    if bg_seen:
        out["fixed_tariff_label"] = bg_label or "fixed tariff"
        out["fixed_tariff_shadow_real_gbp"] = round(bg_shadow_real, 4)
        out["delta_vs_fixed_tariff_real_gbp"] = round(bg_delta_real, 4)
        out["fixed_tariff_shadow_gbp"] = round(bg_shadow, 4)
        out["delta_vs_fixed_tariff_gbp"] = round(bg_delta, 4)
    return out


def compute_weekly_pnl(end_day: date) -> dict[str, Any]:
    """Trailing 7-day aggregate ending on ``end_day`` (inclusive)."""
    out = compute_period_pnl(end_day - timedelta(days=6), end_day, label="trailing-7d")
    # Back-compat: keep ``week_end`` alongside the new period_* fields.
    out["week_end"] = end_day.isoformat()
    return out


def compute_monthly_pnl(end_day: date) -> dict[str, Any]:
    """Calendar-month aggregate for the month containing ``end_day``.

    NOTE: this iterates the FULL calendar month (1st → last day), not just up
    to ``end_day``. For partial-month "month so far" use :func:`compute_mtd_pnl`.
    """
    from calendar import monthrange

    y, m = end_day.year, end_day.month
    last_day = monthrange(y, m)[1]
    out = compute_period_pnl(date(y, m, 1), date(y, m, last_day), label=f"calendar-{y:04d}-{m:02d}")
    out["month"] = f"{y:04d}-{m:02d}"
    return out


def compute_mtd_pnl(end_day: date) -> dict[str, Any]:
    """Month-to-date: 1st of ``end_day``'s month → ``end_day`` (inclusive)."""
    out = compute_period_pnl(end_day.replace(day=1), end_day, label="month-to-date")
    out["month"] = f"{end_day.year:04d}-{end_day.month:02d}"
    return out


def compute_ytd_pnl(end_day: date) -> dict[str, Any]:
    """Year-to-date: Jan 1 of ``end_day``'s year → ``end_day`` (inclusive)."""
    out = compute_period_pnl(date(end_day.year, 1, 1), end_day, label="year-to-date")
    out["year"] = f"{end_day.year:04d}"
    return out


