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


def compute_daily_pnl(day: date) -> dict[str, Any]:
    a, b = _day_bounds(day)
    rows = db.get_execution_logs(from_ts=a, to_ts=b, limit=5000)
    svt = svt_rate_pence()
    fixed = fixed_shadow_rate_pence()
    realised_import = 0.0
    svt_cost = 0.0
    fixed_cost = 0.0
    kwh_sum = 0.0
    import_kwh = 0.0
    cheap_kwh = 0.0
    peak_kwh = 0.0
    for r in rows:
        kwh = float(r.get("consumption_kwh") or 0)
        p = r.get("agile_price_pence")
        if p is None:
            continue
        kwh_sum += kwh
        realised_import += kwh * float(p)
        svt_cost += kwh * svt
        fixed_cost += kwh * fixed
        sk = (r.get("slot_kind") or "").lower()
        if sk == "peak":
            peak_kwh += kwh
        if sk in ("negative", "cheap"):
            cheap_kwh += kwh
        if float(p) > 0 and kwh > 0:
            import_kwh += kwh

    export_revenue, export_kwh = _realised_export_pence(day)
    realised = realised_import - export_revenue

    alpha_svt = (svt_cost - realised) / 100.0
    alpha_fixed = (fixed_cost - realised) / 100.0
    return {
        "date": day.isoformat(),
        "kwh": round(kwh_sum, 3),
        "realised_cost_gbp": round(realised / 100.0, 4),
        "realised_import_gbp": round(realised_import / 100.0, 4),
        "export_revenue_gbp": round(export_revenue / 100.0, 4),
        "export_kwh": round(export_kwh, 3),
        "svt_shadow_gbp": round(svt_cost / 100.0, 4),
        "fixed_shadow_gbp": round(fixed_cost / 100.0, 4),
        "delta_vs_svt_gbp": round(alpha_svt, 4),
        "delta_vs_fixed_gbp": round(alpha_fixed, 4),
        "cheap_slot_kwh": round(cheap_kwh, 3),
        "peak_kwh": round(peak_kwh, 3),
    }


def compute_vwap(day: date) -> float | None:
    a, b = _day_bounds(day)
    rows = db.get_execution_logs(from_ts=a, to_ts=b, limit=5000)
    num = 0.0
    den = 0.0
    for r in rows:
        kwh = float(r.get("consumption_kwh") or 0)
        p = r.get("agile_price_pence")
        if p is None or kwh <= 0:
            continue
        num += kwh * float(p)
        den += kwh
    return round(num / den, 4) if den > 0 else None


def compute_slippage(day: date) -> float | None:
    vwap = compute_vwap(day)
    if vwap is None:
        return None
    tgt = db.get_daily_target(day)
    if not tgt or tgt.get("target_vwap") is None:
        return None
    return round(vwap - float(tgt["target_vwap"]), 4)


def compute_arbitrage_efficiency(day: date) -> float | None:
    a, b = _day_bounds(day)
    rows = db.get_execution_logs(from_ts=a, to_ts=b, limit=5000)
    prices = [float(r["agile_price_pence"]) for r in rows if r.get("agile_price_pence") is not None]
    if len(prices) < 4:
        return None
    q1 = sorted(prices)[max(0, len(prices) // 4 - 1)]
    imp = 0.0
    cheap = 0.0
    for r in rows:
        kwh = float(r.get("consumption_kwh") or 0)
        p = r.get("agile_price_pence")
        if p is None or kwh <= 0:
            continue
        imp += kwh
        if float(p) <= q1:
            cheap += kwh
    return round(100.0 * cheap / imp, 2) if imp > 0 else None


def compute_peak_ratio(day: date) -> float | None:
    a, b = _day_bounds(day)
    rows = db.get_execution_logs(from_ts=a, to_ts=b, limit=5000)
    tot = 0.0
    peak = 0.0
    for r in rows:
        kwh = float(r.get("consumption_kwh") or 0)
        if kwh <= 0:
            continue
        tot += kwh
        if (r.get("slot_kind") or "").lower() == "peak":
            peak += kwh
    return round(100.0 * peak / tot, 2) if tot > 0 else None


def compute_weekly_pnl(end_day: date) -> dict[str, Any]:
    deltas_svt = []
    deltas_fixed = []
    for i in range(7):
        d = end_day - timedelta(days=i)
        p = compute_daily_pnl(d)
        deltas_svt.append(p["delta_vs_svt_gbp"])
        deltas_fixed.append(p["delta_vs_fixed_gbp"])
    return {
        "week_end": end_day.isoformat(),
        "delta_vs_svt_gbp": round(sum(deltas_svt), 4),
        "delta_vs_fixed_gbp": round(sum(deltas_fixed), 4),
    }


def compute_monthly_pnl(end_day: date) -> dict[str, Any]:
    y, m = end_day.year, end_day.month
    deltas_svt = 0.0
    deltas_fixed = 0.0
    for d in range(1, 32):
        try:
            day = date(y, m, d)
        except ValueError:
            break
        p = compute_daily_pnl(day)
        deltas_svt += p["delta_vs_svt_gbp"]
        deltas_fixed += p["delta_vs_fixed_gbp"]
    return {
        "month": f"{y:04d}-{m:02d}",
        "delta_vs_svt_gbp": round(deltas_svt, 4),
        "delta_vs_fixed_gbp": round(deltas_fixed, 4),
    }
