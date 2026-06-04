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
from datetime import UTC, date, datetime, timedelta

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
    agile_rates: list[dict] | None = None,
    half_hourly_import_kwh: list[float] | None = None,
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


def _get_half_hourly_usage(
    period_from: datetime | None = None,
    period_to: datetime | None = None,
) -> object | None:
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
                # The Fox aggregate covers only elapsed days for the current
                # month, so the day-count (used for standing + the usage label)
                # must match: elapsed days now, full month for past months.
                # Billing the full month here while import/export are elapsed
                # inflates every tariff row's standing charge.
                if (y, m) == (today.year, today.month):
                    total_days += today.day
                else:
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
    import_kwh: float | None = None,
    export_kwh: float | None = None,
    period_days: int | None = None,
    months_back: int = 1,
    agile_rates: list[dict] | None = None,
    half_hourly_import_kwh: list[float] | None = None,
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


def _resolve_current_product_code() -> str | None:
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
    current_tariff_code: str | None = None,
) -> TariffRecommendation:
    """Build a ranked recommendation from simulation results."""
    now = datetime.now(UTC)
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
    current_tariff_code: str | None = None,
) -> TariffRecommendation:
    """End-to-end: fetch products, simulate costs, build recommendation.

    Uses Octopus half-hourly consumption for exact Agile cost simulation
    when available; falls back to mean rate x daily totals.
    """
    from ..agile_cache import get_agile_cache
    from .octopus_products import get_available_tariffs

    tariffs = get_available_tariffs(max_products=max_tariffs)
    if not tariffs:
        return TariffRecommendation(
            summary="Could not fetch tariff catalogue from Octopus. Try again later.",
            generated_at=datetime.now(UTC),
        )

    # Prefer Octopus half-hourly consumption for exact Agile simulation
    hhd = _get_half_hourly_usage(
        period_from=datetime.now(UTC) - timedelta(days=months_back * 31),
    )
    half_hourly_import_kwh: list[float] | None = None
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


