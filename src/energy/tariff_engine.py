"""Tariff comparison engine — simulate costs and recommend the best tariff.

Data source priority (best → fallback):
  1. Octopus half-hourly smart meter data (authenticated, most accurate)
  2. Octopus day-aggregated consumption (authenticated, good for flat/TOU)
  3. Fox ESS daily energy breakdown (unauthenticated, no export granularity)
  4. Synthetic defaults (8.5 import / 2.0 export kWh/day)

For Agile tariffs, half-hourly consumption is matched slot-by-slot against
half-hourly prices for exact cost simulation. For flat/TOU tariffs, daily
totals suffice.
"""
from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..config import config
from .tariff_models import (
    ContractType,
    PricingStructure,
    TariffProduct,
    TariffRecommendation,
    TariffSimulationResult,
)

logger = logging.getLogger(__name__)


# ── Cost simulation ──────────────────────────────────────────────────────────

def simulate_tariff_cost(
    tariff: TariffProduct,
    import_kwh: float,
    export_kwh: float,
    period_days: int,
    *,
    agile_rates: Optional[list[dict]] = None,
    half_hourly_import_kwh: Optional[list[float]] = None,
) -> TariffSimulationResult:
    """Simulate the cost of a tariff over a historical period."""
    rates = tariff.rates
    standing_pence = rates.standing_charge_pence_per_day * period_days

    if tariff.pricing == PricingStructure.HALF_HOURLY and agile_rates and half_hourly_import_kwh:
        import_cost = _simulate_agile_exact(agile_rates, half_hourly_import_kwh)
    elif tariff.pricing == PricingStructure.HALF_HOURLY and agile_rates:
        mean_rate = _mean_agile_rate(agile_rates)
        import_cost = import_kwh * mean_rate
    elif tariff.pricing == PricingStructure.TIME_OF_USE:
        import_cost = _simulate_tou(tariff, import_kwh)
    else:
        rate = rates.unit_rate_pence or 0.0
        import_cost = import_kwh * rate

    export_rate = rates.export_rate_pence or config.MANUAL_TARIFF_EXPORT_PENCE or 0.0
    export_earnings = export_kwh * export_rate
    net_cost = import_cost + standing_pence - export_earnings

    daily = net_cost / max(1, period_days)
    annual_net = daily * 365 / 100
    annual_import = (import_cost / max(1, period_days)) * 365 / 100
    annual_standing = rates.standing_charge_pence_per_day * 365 / 100
    annual_export = (export_earnings / max(1, period_days)) * 365 / 100

    exit_fee_pounds = tariff.policy.exit_fee_pence / 100 if tariff.policy.exit_fee_pence else 0.0
    lock_in = tariff.policy.contract_months
    first_year = annual_net + exit_fee_pounds

    return TariffSimulationResult(
        tariff=tariff,
        period_days=period_days,
        import_kwh=import_kwh,
        export_kwh=export_kwh,
        import_cost_pence=round(import_cost, 2),
        export_earnings_pence=round(export_earnings, 2),
        standing_charge_pence=round(standing_pence, 2),
        net_cost_pence=round(net_cost, 2),
        annual_net_cost_pounds=round(annual_net, 2),
        annual_import_cost_pounds=round(annual_import, 2),
        annual_standing_charge_pounds=round(annual_standing, 2),
        annual_export_earnings_pounds=round(annual_export, 2),
        exit_fee_pounds=round(exit_fee_pounds, 2),
        lock_in_months=lock_in,
        first_year_effective_cost_pounds=round(first_year, 2),
    )


def _simulate_agile_exact(
    agile_rates: list[dict], half_hourly_kwh: list[float],
) -> float:
    total_pence = 0.0
    n = min(len(agile_rates), len(half_hourly_kwh))
    for i in range(n):
        rate = float(agile_rates[i].get("value_inc_vat") or 0)
        total_pence += rate * half_hourly_kwh[i]
    return total_pence


def _mean_agile_rate(agile_rates: list[dict]) -> float:
    if not agile_rates:
        return 0.0
    total = sum(float(r.get("value_inc_vat") or 0) for r in agile_rates)
    return total / len(agile_rates)


def _simulate_tou(tariff: TariffProduct, import_kwh: float) -> float:
    rates = tariff.rates
    day_rate = rates.day_rate_pence or rates.unit_rate_pence or 0.0
    night_rate = rates.night_rate_pence or day_rate

    code_upper = tariff.product_code.upper()
    if "GO" in code_upper:
        off_peak_fraction = 0.35
    elif "COSY" in code_upper or "FLUX" in code_upper:
        off_peak_fraction = 0.25
    else:
        off_peak_fraction = 0.30

    return import_kwh * (1 - off_peak_fraction) * day_rate + import_kwh * off_peak_fraction * night_rate


def _simulate_day_cost_pence(
    tariff: TariffProduct,
    import_kwh: float,
    export_kwh: float,
) -> float:
    """Compute net cost in pence for a single day."""
    rates = tariff.rates
    standing = rates.standing_charge_pence_per_day

    if tariff.pricing == PricingStructure.TIME_OF_USE:
        import_cost = _simulate_tou(tariff, import_kwh)
    else:
        rate = rates.unit_rate_pence or 0.0
        import_cost = import_kwh * rate

    export_rate = rates.export_rate_pence or config.MANUAL_TARIFF_EXPORT_PENCE or 0.0
    export_earnings = export_kwh * export_rate
    return import_cost + standing - export_earnings


# ── Granular period comparison ───────────────────────────────────────────────

def _get_daily_usage(months_back: int = 1) -> list[dict]:
    """Get per-day import/export kWh.

    Priority:
    1. Octopus smart meter day-aggregated consumption (authenticated)
    2. Fox ESS daily energy breakdown
    3. Synthetic defaults
    """
    today = date.today()
    period_to = datetime.now(timezone.utc)
    period_from = period_to - timedelta(days=months_back * 31)

    # --- Source 1: Octopus day-aggregated consumption ---
    if config.OCTOPUS_API_KEY and (config.OCTOPUS_MPAN_IMPORT or config.OCTOPUS_MPAN_1):
        try:
            from .octopus_client import (
                fetch_consumption,
                get_mpan_roles,
            )
            roles = get_mpan_roles()
            daily_all: list[dict] = []

            if roles.import_mpan and roles.import_serial:
                import_slots = fetch_consumption(
                    roles.import_mpan,
                    roles.import_serial,
                    period_from,
                    period_to,
                    group_by="day",
                )
                import_by_date = {
                    s.interval_start.date().isoformat(): s.consumption_kwh
                    for s in import_slots
                }
            else:
                import_by_date = {}

            if roles.export_mpan and roles.export_serial:
                export_slots = fetch_consumption(
                    roles.export_mpan,
                    roles.export_serial,
                    period_from,
                    period_to,
                    group_by="day",
                )
                export_by_date = {
                    s.interval_start.date().isoformat(): s.consumption_kwh
                    for s in export_slots
                }
            else:
                export_by_date = {}

            if import_by_date:
                all_dates = sorted(
                    set(import_by_date.keys()) | set(export_by_date.keys())
                )
                for ds in all_dates:
                    d = date.fromisoformat(ds)
                    if d <= today:
                        daily_all.append({
                            "date": ds,
                            "import_kwh": import_by_date.get(ds, 0.0),
                            "export_kwh": export_by_date.get(ds, 0.0),
                            "source": "octopus",
                        })
                if daily_all:
                    logger.info(
                        "Daily usage: %d days from Octopus smart meter", len(daily_all)
                    )
                    daily_all.sort(key=lambda r: r["date"])
                    return daily_all
        except Exception as exc:
            logger.info("Octopus day consumption unavailable (%s), trying Fox ESS", exc)

    # --- Source 2: Fox ESS daily breakdown ---
    try:
        from ..foxess.client import FoxESSClient
        client = FoxESSClient(**config.foxess_client_kwargs())
        daily_all = []
        months_done: set[tuple[int, int]] = set()
        for offset in range(months_back):
            m = today.month - offset
            y = today.year
            while m <= 0:
                m += 12
                y -= 1
            key = (y, m)
            if key in months_done:
                continue
            months_done.add(key)
            try:
                _totals, daily = client.get_energy_month_daily_breakdown(y, m)
                for row in daily:
                    d = date.fromisoformat(row["date"])
                    if d <= today:
                        daily_all.append({**row, "source": "foxess"})
            except Exception:
                continue
        if daily_all:
            logger.info(
                "Daily usage: %d days from Fox ESS", len(daily_all)
            )
            daily_all.sort(key=lambda r: r["date"])
            return daily_all
    except Exception:
        pass

    # --- Source 3: Synthetic fallback ---
    logger.warning(
        "No real usage data available — using synthetic defaults (8.5/2.0 kWh/day)"
    )
    daily_all = []
    for i in range(months_back * 30):
        d = today - timedelta(days=i)
        daily_all.append({
            "date": d.isoformat(),
            "import_kwh": 8.5,
            "export_kwh": 2.0,
            "source": "synthetic",
        })
    daily_all.sort(key=lambda r: r["date"])
    return daily_all


def _get_half_hourly_usage(
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
) -> Optional[object]:
    """Fetch half-hourly import and export from Octopus smart meter.

    Returns a HalfHourlyData object or None if unavailable.
    Used for exact Agile slot-matched simulation.
    """
    if not config.OCTOPUS_API_KEY:
        return None
    try:
        from .octopus_client import fetch_half_hourly_consumption
        data = fetch_half_hourly_consumption(period_from, period_to)
        if data.import_slots:
            logger.info(
                "Half-hourly usage: %d import slots from Octopus",
                len(data.import_slots),
            )
            return data
    except Exception as exc:
        logger.info("Half-hourly consumption unavailable: %s", exc)
    return None


def _aggregate_weekly(daily: list[dict]) -> list[dict]:
    """Aggregate daily data into ISO weeks. Returns [{week_label, import_kwh, export_kwh, days}]."""
    weeks: dict[str, dict] = {}
    for row in daily:
        d = date.fromisoformat(row["date"])
        iso_year, iso_week, _ = d.isocalendar()
        week_start = d - timedelta(days=d.weekday())
        key = f"{iso_year}-W{iso_week:02d}"
        if key not in weeks:
            weeks[key] = {
                "week_label": key,
                "week_start": week_start.isoformat(),
                "import_kwh": 0.0,
                "export_kwh": 0.0,
                "days": 0,
            }
        weeks[key]["import_kwh"] += float(row.get("import_kwh") or 0)
        weeks[key]["export_kwh"] += float(row.get("export_kwh") or 0)
        weeks[key]["days"] += 1
    return sorted(weeks.values(), key=lambda w: w["week_start"])


def _aggregate_monthly(daily: list[dict]) -> list[dict]:
    """Aggregate daily data into calendar months. Returns [{month_label, import_kwh, export_kwh, days}]."""
    months: dict[str, dict] = {}
    for row in daily:
        d = date.fromisoformat(row["date"])
        key = f"{d.year:04d}-{d.month:02d}"
        if key not in months:
            months[key] = {
                "month_label": key,
                "import_kwh": 0.0,
                "export_kwh": 0.0,
                "days": 0,
            }
        months[key]["import_kwh"] += float(row.get("import_kwh") or 0)
        months[key]["export_kwh"] += float(row.get("export_kwh") or 0)
        months[key]["days"] += 1
    return sorted(months.values(), key=lambda m: m["month_label"])


def compare_tariffs_granular(
    tariffs: list[TariffProduct],
    *,
    months_back: int = 1,
    granularity: str = "daily",  # daily | weekly | monthly
) -> dict:
    """Compare tariffs at daily/weekly/monthly granularity.

    Returns {
        "granularity": str,
        "periods": [{label, costs: {product_code: cost_pence}, winner: product_code}],
        "totals": [{product_code, display_name, total_pence, daily_avg_pence, annual_pounds, ...}],
        "current_product_code": str | None,
        "usage": {total_import_kwh, total_export_kwh, total_days}
    }
    """
    daily_data = _get_daily_usage(months_back)
    if not daily_data:
        return {"granularity": granularity, "periods": [], "totals": [], "usage": {}}

    data_source = daily_data[0].get("source", "unknown") if daily_data else "unknown"

    # Aggregate into chosen granularity
    if granularity == "weekly":
        buckets = _aggregate_weekly(daily_data)
        label_key = "week_label"
    elif granularity == "monthly":
        buckets = _aggregate_monthly(daily_data)
        label_key = "month_label"
    else:
        # daily
        buckets = [
            {"day_label": r["date"], "import_kwh": float(r.get("import_kwh") or 0),
             "export_kwh": float(r.get("export_kwh") or 0), "days": 1}
            for r in daily_data
        ]
        label_key = "day_label"

    total_days = sum(b["days"] for b in buckets)

    # Compute cost per tariff per period
    periods = []
    tariff_totals: dict[str, float] = {t.product_code: 0.0 for t in tariffs}
    tariff_wins: dict[str, int] = {t.product_code: 0 for t in tariffs}

    for bucket in buckets:
        imp = float(bucket["import_kwh"])
        exp = float(bucket["export_kwh"])
        ndays = int(bucket["days"])
        label = bucket[label_key]
        costs: dict[str, float] = {}
        for t in tariffs:
            c = _simulate_day_cost_pence(t, imp, exp) if ndays == 1 else (
                _simulate_day_cost_pence(t, imp, exp) + t.rates.standing_charge_pence_per_day * (ndays - 1)
                if ndays > 1 else 0
            )
            # For multi-day buckets, standing charge is already included once per day in _simulate_day_cost_pence
            # Recalculate properly
            if ndays != 1:
                rates = t.rates
                standing = rates.standing_charge_pence_per_day * ndays
                if t.pricing == PricingStructure.TIME_OF_USE:
                    import_cost = _simulate_tou(t, imp)
                else:
                    rate = rates.unit_rate_pence or 0.0
                    import_cost = imp * rate
                export_rate = rates.export_rate_pence or config.MANUAL_TARIFF_EXPORT_PENCE or 0.0
                c = import_cost + standing - exp * export_rate
            costs[t.product_code] = round(c, 2)
            tariff_totals[t.product_code] += c

        winner = min(costs, key=costs.get) if costs else None
        if winner:
            tariff_wins[winner] = tariff_wins.get(winner, 0) + 1
        periods.append({
            "label": label,
            "import_kwh": round(imp, 2),
            "export_kwh": round(exp, 2),
            "days": ndays,
            "costs": costs,
            "winner": winner,
        })

    # Build totals ranking
    tariff_map = {t.product_code: t for t in tariffs}
    totals = []
    for code, total_pence in sorted(tariff_totals.items(), key=lambda x: x[1]):
        t = tariff_map[code]
        daily_avg = total_pence / max(1, total_days)
        annual = daily_avg * 365 / 100
        totals.append({
            "product_code": code,
            "display_name": t.display_name,
            "pricing": t.pricing.value,
            "total_pence": round(total_pence, 2),
            "daily_avg_pence": round(daily_avg, 2),
            "annual_pounds": round(annual, 2),
            "standing_per_day": t.rates.standing_charge_pence_per_day,
            "unit_rate_pence": t.rates.unit_rate_pence,
            "contract_type": t.policy.contract_type.value,
            "contract_months": t.policy.contract_months,
            "exit_fee_pounds": round(t.policy.exit_fee_pence / 100, 2) if t.policy.exit_fee_pence else 0.0,
            "is_green": t.policy.is_green,
            "wins": tariff_wins.get(code, 0),
        })

    current_code = _resolve_current_product_code()

    total_import = sum(float(b["import_kwh"]) for b in buckets)
    total_export = sum(float(b["export_kwh"]) for b in buckets)

    return {
        "granularity": granularity,
        "periods": periods,
        "totals": totals,
        "current_product_code": current_code,
        "data_source": data_source,
        "usage": {
            "total_import_kwh": round(total_import, 2),
            "total_export_kwh": round(total_export, 2),
            "total_days": total_days,
        },
    }


# ── Comparison + recommendation ──────────────────────────────────────────────

def _get_usage_data(months_back: int = 1) -> tuple[float, float, int]:
    """Get total import/export kWh and days from Fox ESS."""
    today = date.today()
    try:
        from ..foxess import get_cached_energy_month
        total_import = total_export = 0.0
        total_days = 0
        for offset in range(months_back):
            m = today.month - offset
            y = today.year
            while m <= 0:
                m += 12
                y -= 1
            try:
                raw = get_cached_energy_month(y, m)
                total_import += float(raw.get("gridConsumptionEnergyToday", 0) or 0)
                total_export += float(raw.get("feedinEnergyToday", 0) or 0)
                total_days += monthrange(y, m)[1]
            except Exception:
                continue
        if total_days > 0:
            return total_import, total_export, total_days
    except Exception:
        pass
    days = months_back * 30
    return 8.5 * days, 2.0 * days, days


def compare_tariffs(
    tariffs: list[TariffProduct],
    *,
    import_kwh: Optional[float] = None,
    export_kwh: Optional[float] = None,
    period_days: Optional[int] = None,
    months_back: int = 1,
    agile_rates: Optional[list[dict]] = None,
    half_hourly_import_kwh: Optional[list[float]] = None,
) -> list[TariffSimulationResult]:
    """Simulate all candidate tariffs against actual usage. Returns sorted by annual net cost.

    When half_hourly_import_kwh is provided alongside agile_rates, Agile tariffs
    are simulated slot-by-slot for exact cost calculation.
    """
    if import_kwh is None or export_kwh is None or period_days is None:
        import_kwh, export_kwh, period_days = _get_usage_data(months_back)

    results = []
    for t in tariffs:
        try:
            sim = simulate_tariff_cost(
                t, import_kwh, export_kwh, period_days,
                agile_rates=agile_rates if t.pricing == PricingStructure.HALF_HOURLY else None,
                half_hourly_import_kwh=(
                    half_hourly_import_kwh
                    if t.pricing == PricingStructure.HALF_HOURLY else None
                ),
            )
            results.append(sim)
        except Exception as exc:
            logger.warning("Simulation failed for %s: %s", t.product_code, exc)

    results.sort(key=lambda r: r.annual_net_cost_pounds)
    return results


def _resolve_current_product_code() -> Optional[str]:
    """Determine the current tariff product code.

    Priority:
    1. Explicit CURRENT_TARIFF_PRODUCT config
    2. Octopus account API (discover_current_tariff)
    3. Infer from OCTOPUS_TARIFF_CODE env var
    """
    explicit = config.CURRENT_TARIFF_PRODUCT
    if explicit:
        return explicit

    # Try account API
    if config.OCTOPUS_API_KEY and config.OCTOPUS_ACCOUNT_NUMBER:
        try:
            from .octopus_client import discover_current_tariff
            tariff = discover_current_tariff()
            if tariff and tariff.product_code:
                logger.info(
                    "Current tariff auto-detected: %s", tariff.product_code
                )
                return tariff.product_code
        except Exception as exc:
            logger.debug("Tariff auto-detect failed: %s", exc)

    # Infer from OCTOPUS_TARIFF_CODE: E-1R-VAR-22-11-01-H -> VAR-22-11-01
    tariff_code = config.OCTOPUS_TARIFF_CODE
    if tariff_code:
        import re
        m = re.match(r"E-\d+R-(.+)-[A-P]$", tariff_code)
        if m:
            return m.group(1)
    return None


def build_recommendation(
    results: list[TariffSimulationResult],
    current_tariff_code: Optional[str] = None,
) -> TariffRecommendation:
    """Build a ranked recommendation from simulation results."""
    now = datetime.now(timezone.utc)
    current = None
    if current_tariff_code:
        for r in results:
            if r.tariff.tariff_code == current_tariff_code or r.tariff.product_code == current_tariff_code:
                current = r
                break

    best = results[0] if results else None
    savings = None
    if current and best and current != best:
        savings = round(current.annual_net_cost_pounds - best.annual_net_cost_pounds, 2)

    summary_lines = []
    if best:
        summary_lines.append(
            f"Best tariff: {best.tariff.display_name} "
            f"— projected £{best.annual_net_cost_pounds:.0f}/yr "
            f"(standing £{best.annual_standing_charge_pounds:.0f}/yr)"
        )
    if savings is not None and savings > 0 and current:
        summary_lines.append(
            f"Saves £{savings:.0f}/yr vs your current tariff ({current.tariff.display_name})"
        )
    elif current:
        summary_lines.append(
            f"Your current tariff ({current.tariff.display_name}) is already competitive."
        )

    if best and best.tariff.policy.contract_type == ContractType.FIXED:
        months = best.tariff.policy.contract_months or 0
        fee = best.tariff.policy.exit_fee_pence / 100
        if months:
            note = f"Note: {months}-month lock-in"
            if fee > 0:
                note += f" with £{fee:.0f} exit fee per fuel"
            summary_lines.append(note)

    for i, r in enumerate(results[:5]):
        lock = ""
        if r.tariff.policy.contract_months:
            lock = f" ({r.tariff.policy.contract_months}mo"
            if r.tariff.policy.exit_fee_pence > 0:
                lock += f", £{r.tariff.policy.exit_fee_pence / 100:.0f} exit"
            lock += ")"
        marker = " ← current" if (current and r is current) else ""
        summary_lines.append(
            f"  {i + 1}. {r.tariff.display_name}: £{r.annual_net_cost_pounds:.0f}/yr"
            f" ({r.tariff.rates.standing_charge_pence_per_day:.1f}p/day standing){lock}{marker}"
        )

    return TariffRecommendation(
        current_tariff=current,
        candidates=results,
        best=best,
        savings_vs_current_pounds=savings,
        summary="\n".join(summary_lines),
        generated_at=now,
    )


def get_tariff_recommendation(
    *,
    months_back: int = 1,
    max_tariffs: int = 15,
    current_tariff_code: Optional[str] = None,
) -> TariffRecommendation:
    """End-to-end: fetch products, simulate costs, build recommendation.

    Uses Octopus half-hourly consumption for exact Agile cost simulation
    when available; falls back to mean rate x daily totals.
    """
    from .octopus_products import get_available_tariffs
    from ..agile_cache import get_agile_cache

    tariffs = get_available_tariffs(max_products=max_tariffs)
    if not tariffs:
        return TariffRecommendation(
            summary="Could not fetch tariff catalogue from Octopus. Try again later.",
            generated_at=datetime.now(timezone.utc),
        )

    # Prefer Octopus half-hourly consumption for exact Agile simulation
    hhd = _get_half_hourly_usage(
        period_from=datetime.now(timezone.utc) - timedelta(days=months_back * 31),
    )
    half_hourly_import_kwh: Optional[list[float]] = None
    if hhd and hhd.import_slots:
        half_hourly_import_kwh = [s.consumption_kwh for s in hhd.import_slots]

    cache = get_agile_cache()
    agile_rates = cache.rates if cache.rates else None

    results = compare_tariffs(
        tariffs,
        months_back=months_back,
        agile_rates=agile_rates,
        half_hourly_import_kwh=half_hourly_import_kwh,
    )

    current_code = current_tariff_code or _resolve_current_product_code()
    return build_recommendation(results, current_tariff_code=current_code)


def get_tariff_comparison_dashboard(
    *,
    months_back: int = 1,
    granularity: str = "daily",
    max_tariffs: int = 10,
) -> dict:
    """Full dashboard payload: granular comparison + totals + recommendation.

    This is the main entry point for the tariff dashboard.
    """
    from .octopus_products import get_available_tariffs

    tariffs = get_available_tariffs(max_products=max_tariffs)
    if not tariffs:
        return {
            "ok": False,
            "error": "Could not fetch tariff catalogue. Try again later.",
        }

    result = compare_tariffs_granular(
        tariffs,
        months_back=months_back,
        granularity=granularity,
    )

    # Identify current tariff in totals and compute savings
    current_code = result.get("current_product_code")
    current_annual = None
    if current_code:
        for t in result["totals"]:
            if t["product_code"] == current_code:
                current_annual = t["annual_pounds"]
                t["is_current"] = True
                break

    # Add savings_vs_current to each total
    for t in result["totals"]:
        if current_annual is not None:
            t["savings_vs_current_pounds"] = round(current_annual - t["annual_pounds"], 2)
        else:
            t["savings_vs_current_pounds"] = None
        t.setdefault("is_current", False)

    # Annotate data source from first period entry
    data_source = "synthetic"
    periods = result.get("periods") or []
    if periods:
        # Each period's data source is embedded in daily_data via _get_daily_usage
        data_source = result.get("data_source", "unknown")

    result["ok"] = True
    result["current_annual_pounds"] = current_annual
    result["data_source"] = data_source
    return result
