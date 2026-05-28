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


def _realised_export_pence(day: date) -> tuple[float, float]:
    """Sum per-slot ``export_kwh × export_rate_pence`` for the day.

    Returns ``(export_revenue_pence, export_kwh_total)``. Per-slot export kWh
    comes from :func:`db.half_hourly_grid_export_kwh_for_day` (trapezoidal
    integration of ``pv_realtime_history.grid_export_kw``). Per-slot export
    rates come from ``agile_export_rates`` when ``OCTOPUS_EXPORT_TARIFF_CODE``
    is configured; otherwise (or for unmatched slots) the flat
    ``EXPORT_RATE_PENCE`` constant is used.

    Closes #207: ``compute_daily_pnl`` previously sank only import × import-
    tariff and silently dropped export earnings. On the user's tariff (Outgoing
    Agile), peak export hours land at 30-60p/kWh — losing them flipped the
    delta-vs-SVT sign on solar-heavy days (e.g. 2026-05-01 reported as
    -£0.30 deficit when the actual delta was +£0.40 surplus).
    """
    bucket_kwh = db.half_hourly_grid_export_kwh_for_day(day)
    if not bucket_kwh:
        return 0.0, 0.0
    a, b = _day_bounds(day)
    rate_rows = (
        db.get_agile_export_rates_in_range(a, b)
        if (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
        else []
    )
    rate_by_start: dict[str, float] = {}
    for r in rate_rows:
        try:
            iso = r["valid_from"].replace("+00:00", "Z")
            rate_by_start[iso] = float(r["value_inc_vat"])
        except (KeyError, TypeError, ValueError):
            continue
    flat = float(config.EXPORT_RATE_PENCE)
    revenue = 0.0
    total_kwh = 0.0
    matched = 0
    for slot_iso, kwh in bucket_kwh.items():
        rate = rate_by_start.get(slot_iso, flat)
        if slot_iso in rate_by_start:
            matched += 1
        revenue += kwh * rate
        total_kwh += kwh
    logger.info(
        "Realised export %s: %.3f kWh -> %.2fp (%d/%d slots matched per-slot rates, rest @ flat %.2fp)",
        day.isoformat(), total_kwh, revenue, matched, len(bucket_kwh), flat,
    )
    return revenue, total_kwh


def _realised_import_pence(day: date) -> tuple[float, float]:
    """Per-slot ``import_kwh × Agile_slot_rate`` from MEASURED grid telemetry.

    Returns ``(import_cost_pence, import_kwh_total)``. Per-slot import kWh comes
    from :func:`db.half_hourly_grid_import_kwh_for_day` (trapezoidal integration
    of ``pv_realtime_history.grid_import_kw``). Per-slot import rates come from
    ``agile_rates`` matched on ``valid_from``.

    Issue #306: replaces the load-based pre-image used by the legacy
    ``realised_import_gbp`` field, which billed *household load* (not net grid
    import) at Agile rates and inflated absolute £ figures ~3-4×.
    """
    bucket_kwh = db.half_hourly_grid_import_kwh_for_day(day)
    if not bucket_kwh:
        return 0.0, 0.0
    a, b = _day_bounds(day)
    # Pull every Agile import row touching the day (no tariff_code filter —
    # we want whatever rate row the LP would have priced against).
    rate_rows = db.get_execution_logs(from_ts=a, to_ts=b, limit=5000)
    # The execution_log already aligns one row per slot with agile_price_pence
    # at heartbeat write time, which is the same source the LP uses. Match
    # buckets by slot ISO.
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

    export_revenue, export_kwh = _realised_export_pence(day)
    real_import_cost, import_kwh = _realised_import_pence(day)
    standing_p = float(config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY or 0)

    # === Legacy "load-billed" view (counterfactual: if no solar/battery) ===
    realised = realised_import_load + standing_p - export_revenue
    svt_cost = svt_energy_cost + standing_p
    fixed_cost = fixed_energy_cost + standing_p
    alpha_svt = (svt_cost - realised) / 100.0
    alpha_fixed = (fixed_cost - realised) / 100.0

    # === Real-money view (measured grid traffic × rates) ===
    realised_real = real_import_cost + standing_p - export_revenue
    svt_real = (import_kwh * svt) + standing_p - export_revenue
    fixed_real = (import_kwh * fixed) + standing_p - export_revenue
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
        bg_cost_real = (import_kwh * bg_rate) + bg_standing - export_revenue
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


def compute_period_pnl(start_day: date, end_day: date, *, label: str = "") -> dict[str, Any]:
    """Aggregate daily PnL across a date range (both bounds inclusive).

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

    for i in range(n):
        d = start_day + timedelta(days=i)
        p = compute_daily_pnl(d)
        for k in totals:
            totals[k] += float(p.get(k) or 0.0)
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


