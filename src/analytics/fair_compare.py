"""Fair, apples-to-apples tariff comparison.

Every tariff is priced against the SAME measured half-hourly grid import/export
(the household's real usage), so the only thing that differs between tariffs is
their own rate card: per-tariff unit rate (flat / day-night TOU), per-tariff
standing charge, and per-tariff export rate. Negative-price import slots CREDIT
the bill (they reduce import cost), never inflate it.

This replaces both legacy comparison paths:
  * the PnL shadow (`pnl.compute_*`) applied one standing charge + the Agile
    export revenue to every shadow — unfair across tariffs with different terms;
  * the tariff dashboard (`tariff_engine`) replayed *daily-aggregate* kWh with a
    guessed off-peak fraction — TOU rows were fiction.

Here usage is replayed PER SLOT, so TOU tariffs are priced on the household's
actual usage timing, and the current Agile tariff reuses the realised per-slot
rates (the real bill) from `pnl`.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..energy.tariff_models import (
    PricingStructure,
    RateSchedule,
    TariffProduct,
)
from . import pnl
from .shadow_pricing import svt_rate_pence

logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")


# --- time-of-use helpers ---------------------------------------------------

def _hm_to_min(s: str) -> int | None:
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _in_off_peak(minute_of_day: int, start: str, end: str) -> bool:
    """Is ``minute_of_day`` (local) inside the off-peak window [start, end)?

    Handles windows that cross midnight (start > end), e.g. a 23:30→05:30 band.
    """
    s = _hm_to_min(start)
    e = _hm_to_min(end)
    if s is None or e is None or s == e:
        return False
    if s < e:
        return s <= minute_of_day < e
    return minute_of_day >= s or minute_of_day < e


def _local_minute_of_day(slot_iso: str) -> int:
    """Local (Europe/London) minute-of-day for a UTC slot ISO. DST-correct."""
    dt = datetime.fromisoformat(slot_iso.replace("Z", "+00:00")).astimezone(LONDON)
    return dt.hour * 60 + dt.minute


# --- candidate tariffs -----------------------------------------------------

def _is_export_only(product_code: str) -> bool:
    """Outgoing / SEG export products — not import tariffs, exclude from the
    import-cost comparison."""
    c = (product_code or "").upper()
    return "OUTGOING" in c or "-EXPORT" in c or "POWER-PACK" in c or "SEG" in c


def _current_product_code() -> str:
    code = (config.OCTOPUS_TARIFF_CODE or "").strip()
    m = re.match(r"^E-\d+R-(.+)-[A-Z]$", code)
    return m.group(1) if m else (code or "AGILE")


def _synthetic_candidates() -> list[TariffProduct]:
    """SVT + the configured fixed tariff as flat TariffProducts (pure-DB, always
    available). Export priced at the flat SEG (`EXPORT_SEG_RATE_PENCE`) — i.e. "on
    this import tariff you'd export at the same standard SEG you're on now"."""
    seg = float(config.EXPORT_SEG_RATE_PENCE or 0)
    out: list[TariffProduct] = []
    svt_standing = float(
        config.SVT_STANDING_PENCE_PER_DAY
        or config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY
        or 0
    )
    out.append(
        TariffProduct(
            product_code="SVT",
            tariff_code="SVT",
            display_name="Standard Variable (SVT)",
            full_name="Ofgem price-cap standard variable",
            pricing=PricingStructure.FLAT,
            rates=RateSchedule(
                unit_rate_pence=svt_rate_pence(),
                standing_charge_pence_per_day=svt_standing,
                export_rate_pence=seg,
            ),
        )
    )
    fixed_rate = float(config.FIXED_TARIFF_RATE_PENCE or 0)
    if fixed_rate > 0:
        out.append(
            TariffProduct(
                product_code="FIXED",
                tariff_code="FIXED",
                display_name=config.FIXED_TARIFF_LABEL or "Fixed tariff",
                full_name=config.FIXED_TARIFF_LABEL or "Configured fixed tariff",
                pricing=PricingStructure.FLAT,
                rates=RateSchedule(
                    unit_rate_pence=fixed_rate,
                    standing_charge_pence_per_day=float(
                        config.FIXED_TARIFF_STANDING_PENCE_PER_DAY or 0
                    ),
                    export_rate_pence=seg,
                ),
            )
        )
    return out


# --- per-day per-tariff pricing -------------------------------------------

def _price_flat_or_tou_day(
    t: TariffProduct, import_bucket: dict[str, float]
) -> tuple[float, float]:
    """(import_cost_pence, import_kwh) for a FLAT/CAPPED/TOU tariff over one day's
    per-slot import. HALF_HOURLY/TRACKER are handled post-loop (approximate)."""
    import_cost = 0.0
    import_kwh = 0.0
    if t.pricing == PricingStructure.TIME_OF_USE:
        day_r = t.rates.day_rate_pence or t.rates.unit_rate_pence or 0.0
        night_r = t.rates.night_rate_pence if t.rates.night_rate_pence is not None else day_r
        has_window = bool(t.rates.off_peak_start and t.rates.off_peak_end)
        for slot, kwh in import_bucket.items():
            off = has_window and _in_off_peak(
                _local_minute_of_day(slot), t.rates.off_peak_start, t.rates.off_peak_end
            )
            import_cost += kwh * (night_r if off else day_r)
            import_kwh += kwh
    else:  # FLAT / CAPPED_VARIABLE
        rate = t.rates.unit_rate_pence or 0.0
        for kwh in import_bucket.values():
            import_cost += kwh * rate
            import_kwh += kwh
    return import_cost, import_kwh


# --- public API ------------------------------------------------------------

def _empty(requested_start: date, end_day: date, agile_start: date) -> dict[str, Any]:
    return {
        "period_start": agile_start.isoformat(),
        "period_end": end_day.isoformat(),
        "requested_start": requested_start.isoformat(),
        "clamped": True,
        "clamp_reason": f"Entire range predates AGILE_TARIFF_START_DATE={agile_start.isoformat()}",
        "n_days": 0,
        "days_with_data": 0,
        "basis": {"import_kwh": 0.0, "export_kwh": 0.0},
        "current_product_code": _current_product_code(),
        "tariffs": [],
        "winner_product_code": None,
        "savings_vs_current_pounds": 0.0,
        "catalogue_unavailable": False,
        "data_source": "measured_per_slot",
    }


def compute_fair_comparison(
    start_day: date, end_day: date, *, max_tariffs: int = 14
) -> dict[str, Any]:
    """Fair per-slot tariff comparison over ``[start_day, end_day]`` (inclusive).

    Returns a dict with the measured basis, one row per tariff (current Agile +
    SVT/fixed + live Octopus catalogue), the winner, and savings-vs-current. All
    money is in PENCE. See module docstring for the fairness contract.
    """
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    requested_start = start_day
    clamped = False
    clamp_reason: str | None = None
    agile_start = pnl._agile_start_date()
    if agile_start and start_day < agile_start:
        if end_day < agile_start:
            return _empty(requested_start, end_day, agile_start)
        start_day = agile_start
        clamped = True
        clamp_reason = (
            f"Clamped to AGILE_TARIFF_START_DATE={agile_start.isoformat()} "
            "(before that the household was on a different tariff)"
        )

    n_days = (end_day - start_day).days + 1
    current_code = _current_product_code()

    # Candidate set: synthetic SVT/fixed (pure-DB) + live Octopus catalogue.
    candidates = _synthetic_candidates()
    catalogue_unavailable = False
    try:
        from ..energy.octopus_products import get_available_tariffs

        for t in get_available_tariffs(max_products=max_tariffs):
            if _is_export_only(t.product_code):
                continue  # Outgoing / SEG-export products aren't import tariffs
            candidates.append(t)
    except Exception:
        logger.warning("fair_compare: Octopus catalogue unavailable", exc_info=True)
        catalogue_unavailable = True

    # Accumulators. Current = the realised Agile bill (the real thing).
    cur = {"import_cost": 0.0, "export_credit": 0.0, "neg_credit": 0.0,
           "import_kwh": 0.0, "export_kwh": 0.0}
    # Flat/TOU candidates accumulate per-slot; HH/tracker handled post-loop.
    flat_tou = [t for t in candidates if t.pricing in (
        PricingStructure.FLAT, PricingStructure.CAPPED_VARIABLE, PricingStructure.TIME_OF_USE)]
    other_hh = [t for t in candidates if t not in flat_tou]
    acc: dict[str, dict[str, float]] = {
        t.product_code: {"import_cost": 0.0, "import_kwh": 0.0} for t in flat_tou
    }

    # Both export valuations summed over the period for the side-by-side panel.
    export_seg_pence = 0.0
    export_agile_pence = 0.0

    days_with_data = 0
    d = start_day
    while d <= end_day:
        # Compute each primitive ONCE per day, then derive the realised figures —
        # calling pnl._realised_import_pence/_realised_export_pence here would
        # re-run the meter-bucket build, rate map, and export integration (a year
        # view over 365 days would otherwise double the DB work).
        import_bucket = pnl._import_buckets_preferring_meter(d)
        export_bucket = db.half_hourly_grid_export_kwh_for_day(d)
        rate_map = pnl.agile_import_rate_by_slot(d)
        ex_both = pnl.export_revenues_for_day(d)
        if import_bucket or export_bucket:
            days_with_data += 1

        ic = sum(kwh * rate_map.get(s, 0.0) for s, kwh in import_bucket.items())
        ik = sum(import_bucket.values())
        er = (
            ex_both["agile_pence"]
            if config.EXPORT_TARIFF_MODE == "outgoing_agile"
            else ex_both["seg_flat_pence"]
        )
        neg = sum(
            kwh * rate_map[s]
            for s, kwh in import_bucket.items()
            if rate_map.get(s, 0.0) < 0
        )
        cur["import_cost"] += ic
        cur["export_credit"] += er
        cur["neg_credit"] += neg
        cur["import_kwh"] += ik
        cur["export_kwh"] += ex_both["export_kwh"]
        export_seg_pence += ex_both["seg_flat_pence"]
        export_agile_pence += ex_both["agile_pence"]

        for t in flat_tou:
            i_cost, i_kwh = _price_flat_or_tou_day(t, import_bucket)
            a = acc[t.product_code]
            a["import_cost"] += i_cost
            a["import_kwh"] += i_kwh
        d += timedelta(days=1)

    export_kwh = cur["export_kwh"]

    def _row(t: TariffProduct, import_cost: float, import_kwh: float,
             neg_credit: float, approximate: bool, is_current: bool) -> dict[str, Any]:
        standing = float(t.rates.standing_charge_pence_per_day) * n_days
        export_rate = float(t.rates.export_rate_pence or 0.0)
        export_credit = export_kwh * export_rate
        net = import_cost + standing - export_credit
        return {
            "product_code": t.product_code,
            "display_name": t.display_name,
            "pricing": t.pricing.value,
            "is_current": is_current,
            "approximate": approximate,
            "import_cost_pence": round(import_cost, 2),
            "standing_pence": round(standing, 2),
            "export_credit_pence": round(export_credit, 2),
            "negative_credit_pence": round(neg_credit, 2),
            "net_pence": round(net, 2),
            "import_kwh": round(import_kwh, 3),
            "export_kwh": round(export_kwh, 3),
            "n_days": n_days,
        }

    rows: list[dict[str, Any]] = []

    # Current Agile (realised) — its own standing + realised Outgoing export.
    cur_standing = float(config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY or 0) * n_days
    cur_net = cur["import_cost"] + cur_standing - cur["export_credit"]
    rows.append({
        "product_code": current_code,
        "display_name": "Octopus Agile (your tariff)",
        "pricing": "half_hourly",
        "is_current": True,
        "approximate": False,
        "import_cost_pence": round(cur["import_cost"], 2),
        "standing_pence": round(cur_standing, 2),
        "export_credit_pence": round(cur["export_credit"], 2),
        "negative_credit_pence": round(cur["neg_credit"], 2),
        "net_pence": round(cur_net, 2),
        "import_kwh": round(cur["import_kwh"], 3),
        "export_kwh": round(export_kwh, 3),
        "n_days": n_days,
    })

    for t in flat_tou:
        if t.product_code == current_code:
            continue
        a = acc[t.product_code]
        rows.append(_row(t, a["import_cost"], a["import_kwh"], 0.0,
                         approximate=False, is_current=False))

    # Other half-hourly / tracker tariffs — can't be priced per-slot (their HH
    # rates aren't stored). Proxy at the realised mean (≈ current import cost on
    # the same usage) and flag approximate so the UI can asterisk them.
    for t in other_hh:
        if t.product_code == current_code:
            continue
        rows.append(_row(t, cur["import_cost"], cur["import_kwh"], 0.0,
                         approximate=True, is_current=False))

    rows.sort(key=lambda r: r["net_pence"])
    winner = rows[0] if rows else None
    savings_vs_current = (
        round((cur_net - winner["net_pence"]) / 100.0, 2) if winner else 0.0
    )

    return {
        "period_start": start_day.isoformat(),
        "period_end": end_day.isoformat(),
        "requested_start": requested_start.isoformat(),
        "clamped": clamped,
        "clamp_reason": clamp_reason,
        "n_days": n_days,
        "days_with_data": days_with_data,
        "basis": {
            "import_kwh": round(cur["import_kwh"], 3),
            "export_kwh": round(export_kwh, 3),
        },
        "current_product_code": current_code,
        "tariffs": rows,
        "winner_product_code": winner["product_code"] if winner else None,
        "savings_vs_current_pounds": savings_vs_current,
        "catalogue_unavailable": catalogue_unavailable,
        "data_source": "measured_per_slot",
        # Export side-by-side: what you're paid on the flat SEG (actual) vs what
        # the Outgoing Agile alternative would have paid on the same kWh.
        "export": {
            "export_kwh": round(export_kwh, 3),
            "mode": config.EXPORT_TARIFF_MODE,
            "seg_rate_p": float(config.EXPORT_SEG_RATE_PENCE or 0),
            "seg_revenue_pence": round(export_seg_pence, 2),
            "agile_revenue_pence": round(export_agile_pence, 2),
            "agile_avg_p": round(export_agile_pence / export_kwh, 2) if export_kwh > 0 else 0.0,
            "uplift_if_switch_pence": round(export_agile_pence - export_seg_pence, 2),
        },
    }
