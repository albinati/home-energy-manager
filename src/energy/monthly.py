"""Monthly energy summary, cost, heating estimate and gas comparison.

Cost calculation priority:
  1. Octopus half-hourly consumption x half-hourly rates (most accurate)
  2. Manual flat rate from config (MANUAL_TARIFF_IMPORT_PENCE)

Energy data from Fox ESS; heating from Daikin when available, otherwise
estimated from HEATING_LOAD_SHARE fraction of total load.
Supports day, week, and month periods with chart_data for the UI.
Optional weather (WEATHER_LAT/LON) enables degree-days and spend-by-temperature analytics.
"""
import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .. import db
from ..config import config
from ..foxess import get_cached_energy_month
from ..foxess.client import FoxESSClient

logger = logging.getLogger(__name__)


# SEG export floor used for the fixed-tariff counterfactual. A flat fixed
# import tariff would pair with SEG export (not Octopus Outgoing Agile), so the
# shadow bills export at this floor. Mirrors SEG_EXPORT_FALLBACK_P in the UI
# (ui/src/components/home/Hero.tsx / TariffComparisonWidget.tsx).
SEG_EXPORT_FALLBACK_PENCE = 4.0


# Temperature bands for "spend by outdoor temp" (°C)
TEMP_BANDS = [
    ("<0°C", lambda t: t is not None and t < 0),
    ("0–5°C", lambda t: t is not None and 0 <= t < 5),
    ("5–10°C", lambda t: t is not None and 5 <= t < 10),
    ("10–15°C", lambda t: t is not None and 10 <= t < 15),
    ("15°C+", lambda t: t is not None and t >= 15),
]


@dataclass
class HeatingAnalytics:
    """Heating share and weather-based analytics for the period."""
    heating_percent_of_cost: float | None = None  # heating cost / net cost * 100
    heating_percent_of_consumption: float | None = None  # heating_kwh / load_kwh * 100
    avg_outdoor_temp_c: float | None = None
    degree_days: float | None = None  # sum max(0, base - daily_temp)
    cost_per_degree_day_pounds: float | None = None  # heating_cost / degree_days
    heating_kwh_per_degree_day: float | None = None
    temp_bands: list[dict] = field(default_factory=list)  # [{ band, days, heating_kwh, cost_pounds }]


@dataclass
class MonthlyEnergySummary:
    """Energy totals for a calendar month (from Fox ESS)."""
    year: int
    month: int
    month_str: str  # YYYY-MM
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    solar_kwh: float = 0.0
    load_kwh: float = 0.0
    charge_kwh: float = 0.0
    discharge_kwh: float = 0.0


@dataclass
class MonthlyCostSummary:
    """Cost summary for a month (import, export, standing, net)."""
    import_cost_pence: float = 0.0
    export_earnings_pence: float = 0.0
    standing_charge_pence: float = 0.0
    net_cost_pence: float = 0.0
    # Fixed-tariff counterfactual on the SAME metered kWh + day-window as the
    # realised cost above. None when FIXED_TARIFF_* is not configured. Positive
    # delta = Agile cheaper than the fixed tariff would have been.
    fixed_shadow_pence: float | None = None
    delta_vs_fixed_pence: float | None = None

    @property
    def net_cost_pounds(self) -> float:
        return self.net_cost_pence / 100

    @property
    def import_cost_pounds(self) -> float:
        return self.import_cost_pence / 100

    @property
    def export_earnings_pounds(self) -> float:
        return self.export_earnings_pence / 100

    @property
    def fixed_shadow_pounds(self) -> float | None:
        return None if self.fixed_shadow_pence is None else self.fixed_shadow_pence / 100

    @property
    def delta_vs_fixed_pounds(self) -> float | None:
        return None if self.delta_vs_fixed_pence is None else self.delta_vs_fixed_pence / 100


@dataclass
class MonthlyInsights:
    """Full monthly view: energy, cost, heating estimate, gas comparison."""
    energy: MonthlyEnergySummary
    cost: MonthlyCostSummary
    heating_estimate_kwh: float | None = None
    heating_estimate_cost_pence: float | None = None
    equivalent_gas_cost_pence: float | None = None
    gas_comparison_ahead_pounds: float | None = None  # positive = ahead with solar+ASHP

    @property
    def equivalent_gas_cost_pounds(self) -> float | None:
        if self.equivalent_gas_cost_pence is None:
            return None
        return self.equivalent_gas_cost_pence / 100


def _foxess_to_energy_summary(year: int, month: int, raw: dict) -> MonthlyEnergySummary:
    """Map Fox ESS history keys to MonthlyEnergySummary."""
    return MonthlyEnergySummary(
        year=year,
        month=month,
        month_str=f"{year:04d}-{month:02d}",
        import_kwh=raw.get("gridConsumptionEnergyToday", 0.0) or 0.0,
        export_kwh=raw.get("feedinEnergyToday", 0.0) or 0.0,
        solar_kwh=raw.get("pvEnergyToday", 0.0) or 0.0,
        load_kwh=raw.get("loadEnergyToday", 0.0) or 0.0,
        charge_kwh=raw.get("chargeEnergyToday", 0.0) or 0.0,
        discharge_kwh=raw.get("dischargeEnergyToday", 0.0) or 0.0,
    )


def _fixed_shadow(
    import_kwh: float,
    export_kwh: float,
    n_days: float,
    net_cost_pence: float,
) -> tuple[float | None, float | None]:
    """Fixed-tariff counterfactual (pence) on the given metered kWh + day-window.

    Returns ``(fixed_shadow_pence, delta_vs_fixed_pence)``, or ``(None, None)``
    when ``FIXED_TARIFF_*`` is not configured. The fixed tariff would pair with
    SEG export (flat :data:`SEG_EXPORT_FALLBACK_PENCE`), not Octopus Outgoing —
    that asymmetry is intentional. ``delta`` is positive when Agile is cheaper.
    """
    if not (config.FIXED_TARIFF_LABEL and config.FIXED_TARIFF_RATE_PENCE):
        return None, None
    standing = (config.FIXED_TARIFF_STANDING_PENCE_PER_DAY or 0.0) * n_days
    shadow = (
        import_kwh * config.FIXED_TARIFF_RATE_PENCE
        + standing
        - export_kwh * SEG_EXPORT_FALLBACK_PENCE
    )
    return round(shadow, 2), round(shadow - net_cost_pence, 2)


def _compute_cost(energy: MonthlyEnergySummary, n_days: int | None = None) -> MonthlyCostSummary:
    """Apply manual tariff and standing charge.

    ``n_days`` bills the standing charge for that many days (use the elapsed-day
    count for the current/partial month). When ``None`` the full calendar month
    is billed — the original behaviour, kept for completed past months.
    """
    import_rate = config.MANUAL_TARIFF_IMPORT_PENCE or 0.0
    export_rate = config.MANUAL_TARIFF_EXPORT_PENCE or 0.0
    standing_per_day = config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY or 0.0
    days = n_days if n_days is not None else monthrange(energy.year, energy.month)[1]
    import_cost_pence = energy.import_kwh * import_rate
    export_earnings_pence = energy.export_kwh * export_rate
    standing_charge_pence = days * standing_per_day
    net_cost_pence = import_cost_pence + standing_charge_pence - export_earnings_pence
    shadow_pence, delta_pence = _fixed_shadow(
        energy.import_kwh, energy.export_kwh, days, net_cost_pence
    )
    return MonthlyCostSummary(
        import_cost_pence=round(import_cost_pence, 2),
        export_earnings_pence=round(export_earnings_pence, 2),
        standing_charge_pence=round(standing_charge_pence, 2),
        net_cost_pence=round(net_cost_pence, 2),
        fixed_shadow_pence=shadow_pence,
        delta_vs_fixed_pence=delta_pence,
    )


def _compute_cost_octopus(
    energy: MonthlyEnergySummary,
    period_from: datetime | None = None,
    period_to: datetime | None = None,
    n_days: int | None = None,
) -> MonthlyCostSummary | None:
    """Compute monthly cost using Octopus half-hourly consumption x half-hourly rates.

    Returns None if Octopus data is unavailable — caller falls back to _compute_cost().
    Uses the import MPAN for consumption and Agile rates from the Octopus API.
    For non-Agile tariffs, Octopus day-aggregated consumption x tariff rate is used.
    """
    if not config.OCTOPUS_API_KEY:
        return None

    year = energy.year
    month = energy.month
    if period_from is None:
        period_from = datetime(year, month, 1, tzinfo=UTC)
    if period_to is None:
        _, ndays = monthrange(year, month)
        period_to = datetime(year, month, ndays, 23, 59, 59, tzinfo=UTC)

    try:
        from ..scheduler.agile import fetch_agile_rates
        from .octopus_client import fetch_consumption, get_mpan_roles

        roles = get_mpan_roles()
        if not (roles.import_mpan and roles.import_serial):
            return None

        # Fetch half-hourly import consumption
        import_slots = fetch_consumption(
            roles.import_mpan,
            roles.import_serial,
            period_from,
            period_to,
        )
        if not import_slots:
            return None

        # Fetch half-hourly rates for the period. ``fetch_agile_rates`` takes
        # (tariff_code, period_from, period_to) — positional args here would
        # bind ``period_from`` to ``tariff_code`` and crash with
        # "'datetime.datetime' object has no attribute 'strip'".
        agile_rates_raw = fetch_agile_rates(
            period_from=period_from, period_to=period_to,
        )

        import_cost_pence = 0.0
        if agile_rates_raw:
            # Build a rate lookup: slot start (truncated to minute) -> rate_pence
            rate_by_start: dict[str, float] = {}
            for r in agile_rates_raw:
                ts = r.get("valid_from") or r.get("interval_start") or ""
                if ts:
                    rate_by_start[ts[:16]] = float(r.get("value_inc_vat") or 0)

            for slot in import_slots:
                key = slot.interval_start.isoformat()[:16]
                rate = rate_by_start.get(key)
                if rate is not None:
                    import_cost_pence += slot.consumption_kwh * rate
                else:
                    # fallback: use manual rate for unmatched slots
                    import_cost_pence += slot.consumption_kwh * (config.MANUAL_TARIFF_IMPORT_PENCE or 0)
        else:
            # No Agile rates — use manual rate with Octopus consumption quantities
            import_rate = config.MANUAL_TARIFF_IMPORT_PENCE or 0.0
            import_cost_pence = sum(s.consumption_kwh for s in import_slots) * import_rate

        import_kwh_metered = sum(s.consumption_kwh for s in import_slots)

        # Export: use Octopus export consumption if available, billed at the
        # per-slot Outgoing Agile rate (matching pnl.py:_realised_export_pence).
        # Falls back to the flat manual export rate for unmatched slots / when
        # no Outgoing tariff is configured.
        export_earnings_pence = 0.0
        export_kwh_metered = 0.0
        if roles.export_mpan and roles.export_serial:
            export_slots = fetch_consumption(
                roles.export_mpan,
                roles.export_serial,
                period_from,
                period_to,
            )
            export_kwh_metered = sum(s.consumption_kwh for s in export_slots)
            flat_export_rate = config.MANUAL_TARIFF_EXPORT_PENCE or 0.0
            export_rate_by_start: dict[str, float] = {}
            if config.OCTOPUS_EXPORT_TARIFF_CODE:
                for r in db.get_agile_export_rates_in_range(
                    period_from.isoformat(), period_to.isoformat()
                ):
                    ts = r.get("valid_from") or ""
                    if ts:
                        export_rate_by_start[ts[:16]] = float(r.get("value_inc_vat") or 0)
            matched = 0
            for slot in export_slots:
                key = slot.interval_start.isoformat()[:16]
                rate = export_rate_by_start.get(key)
                if rate is not None:
                    matched += 1
                else:
                    rate = flat_export_rate
                export_earnings_pence += slot.consumption_kwh * rate
            logger.info(
                "Monthly export (Octopus %d-%02d): %.3f kWh -> %.2fp (%d/%d slots per-slot rate)",
                year, month, export_kwh_metered, export_earnings_pence, matched, len(export_slots),
            )

        days = n_days if n_days is not None else monthrange(year, month)[1]
        standing_pence = (config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY or 0.0) * days
        net_pence = import_cost_pence + standing_pence - export_earnings_pence
        shadow_pence, delta_pence = _fixed_shadow(
            import_kwh_metered, export_kwh_metered, days, net_pence
        )

        logger.info(
            "Monthly cost (Octopus %d-%02d): import=%.2fp export=%.2fp net=%.2fp (standing %d d)",
            year, month, import_cost_pence, export_earnings_pence, net_pence, days,
        )
        return MonthlyCostSummary(
            import_cost_pence=round(import_cost_pence, 2),
            export_earnings_pence=round(export_earnings_pence, 2),
            standing_charge_pence=round(standing_pence, 2),
            net_cost_pence=round(net_pence, 2),
            fixed_shadow_pence=shadow_pence,
            delta_vs_fixed_pence=delta_pence,
        )
    except Exception as exc:
        logger.info("Octopus cost calculation unavailable for %d-%02d: %s", year, month, exc)
        return None


def _best_cost(energy: MonthlyEnergySummary, n_days: int | None = None) -> MonthlyCostSummary:
    """Return Octopus-sourced cost if available, else manual flat rate.

    ``n_days`` (elapsed days) is threaded through so the standing charge matches
    the energy window for the current/partial month; ``None`` bills the full
    calendar month (completed past months).
    """
    octopus_cost = _compute_cost_octopus(energy, n_days=n_days)
    if octopus_cost is not None:
        return octopus_cost
    return _compute_cost(energy, n_days=n_days)


def _period_cost(
    energy: MonthlyEnergySummary,
    period_from: datetime,
    period_to: datetime,
    n_days: int,
) -> MonthlyCostSummary:
    """Cost for an arbitrary [from, to) window — Octopus per-slot when available,
    else manual flat rate. Used by the day/week branches so every granularity
    bills import/export at the same half-hourly Agile rates as the month view
    (falls back gracefully when Octopus consumption isn't backfilled yet, e.g.
    for 'today')."""
    octopus_cost = _compute_cost_octopus(
        energy, period_from=period_from, period_to=period_to, n_days=n_days
    )
    if octopus_cost is not None:
        return octopus_cost
    return _compute_cost(energy, n_days=n_days)


def _get_daikin_heating_kwh(year: int, month: int) -> float | None:
    """Get heating electrical consumption (kWh) for the month from Daikin when available. Returns None if not configured or not exposed.

    Routes through the cached service layer (30-min device TTL) instead of a
    fresh client — energy insights are built/polled often, and a raw client
    here wire-read on every call (the read-burst root cause)."""
    try:
        from ..daikin import service as daikin_service
        return daikin_service.heating_consumption_kwh(year, month)
    except Exception:
        return None


def _get_daikin_heating_daily_kwh(year: int, month: int) -> list[float] | None:
    """Get daily heating (kWh) for the month from Daikin when available. List
    length = days in month. Cached service read — see :func:`_get_daikin_heating_kwh`."""
    try:
        from ..daikin import service as daikin_service
        return daikin_service.heating_daily_kwh(year, month)
    except Exception:
        return None


def _build_heating_analytics(
    insights: "MonthlyInsights",
    year: int,
    month: int,
) -> HeatingAnalytics:
    """Build heating share and optional weather-based analytics."""
    cost = insights.cost
    energy = insights.energy
    heating_kwh = insights.heating_estimate_kwh
    heating_cost_pence = insights.heating_estimate_cost_pence

    heating_percent_of_cost = None
    if cost.net_cost_pence and cost.net_cost_pence != 0 and heating_cost_pence is not None:
        heating_percent_of_cost = round(100.0 * heating_cost_pence / abs(cost.net_cost_pence), 1)

    heating_percent_of_consumption = None
    if energy.load_kwh and energy.load_kwh > 0 and heating_kwh is not None:
        heating_percent_of_consumption = round(100.0 * heating_kwh / energy.load_kwh, 1)

    avg_outdoor_temp_c = None
    degree_days = None
    cost_per_degree_day_pounds = None
    heating_kwh_per_degree_day = None
    temp_bands: list[dict] = []

    base_temp = config.WEATHER_DEGREE_DAY_BASE_C
    _, ndays = monthrange(year, month)
    start_d = date(year, month, 1)
    end_d = date(year, month, ndays)

    daily_temps = []
    if config.WEATHER_LAT and config.WEATHER_LON:
        from ..weather import fetch_daily_temps
        daily_temps = fetch_daily_temps(start_d, end_d)

    daily_heating = _get_daikin_heating_daily_kwh(year, month)

    if daily_temps:
        temps_by_date = {d: t for d, t in daily_temps}
        degree_day_sum = 0.0
        temp_sum = 0.0
        count = 0
        for day in range(1, ndays + 1):
            d = date(year, month, day)
            key = d.isoformat()
            t = temps_by_date.get(key)
            if t is not None:
                temp_sum += t
                count += 1
                degree_day_sum += max(0.0, base_temp - t)
        if count > 0:
            avg_outdoor_temp_c = round(temp_sum / count, 1)
        if degree_day_sum > 0:
            degree_days = round(degree_day_sum, 1)
            if heating_cost_pence is not None:
                cost_per_degree_day_pounds = round((heating_cost_pence / 100) / degree_day_sum, 3)
            if heating_kwh is not None:
                heating_kwh_per_degree_day = round(heating_kwh / degree_day_sum, 2)

    if daily_temps and daily_heating and len(daily_heating) >= ndays:
        temps_by_date = {d: t for d, t in daily_temps}
        import_rate = config.MANUAL_TARIFF_IMPORT_PENCE or 0.0
        for band_name, pred in TEMP_BANDS:
            days_in_band = 0
            heating_in_band = 0.0
            temp_sum_band = 0.0
            for day in range(ndays):
                d = date(year, month, day + 1)
                key = d.isoformat()
                t = temps_by_date.get(key)
                if pred(t):
                    days_in_band += 1
                    heating_in_band += daily_heating[day]
                    if t is not None:
                        temp_sum_band += t
            cost_band = (heating_in_band * import_rate / 100) if import_rate else 0.0
            avg_t = round(temp_sum_band / days_in_band, 1) if days_in_band else None
            temp_bands.append({
                "band": band_name,
                "days": days_in_band,
                "heating_kwh": round(heating_in_band, 2),
                "cost_pounds": round(cost_band, 2),
                "avg_temp_c": avg_t,
            })

    return HeatingAnalytics(
        heating_percent_of_cost=heating_percent_of_cost,
        heating_percent_of_consumption=heating_percent_of_consumption,
        avg_outdoor_temp_c=avg_outdoor_temp_c,
        degree_days=degree_days,
        cost_per_degree_day_pounds=cost_per_degree_day_pounds,
        heating_kwh_per_degree_day=heating_kwh_per_degree_day,
        temp_bands=temp_bands,
    )


def _heating_analytics_percent_only(insights: "MonthlyInsights") -> HeatingAnalytics | None:
    """Heating share only (no weather/temp_bands). Used for year view."""
    cost = insights.cost
    energy = insights.energy
    heating_kwh = insights.heating_estimate_kwh
    heating_cost_pence = insights.heating_estimate_cost_pence
    heating_percent_of_cost = None
    if cost.net_cost_pence and cost.net_cost_pence != 0 and heating_cost_pence is not None:
        heating_percent_of_cost = round(100.0 * heating_cost_pence / abs(cost.net_cost_pence), 1)
    heating_percent_of_consumption = None
    if energy.load_kwh and energy.load_kwh > 0 and heating_kwh is not None:
        heating_percent_of_consumption = round(100.0 * heating_kwh / energy.load_kwh, 1)
    if heating_percent_of_cost is None and heating_percent_of_consumption is None:
        return None
    return HeatingAnalytics(
        heating_percent_of_cost=heating_percent_of_cost,
        heating_percent_of_consumption=heating_percent_of_consumption,
    )


def _compute_heating_and_gas(
    energy: MonthlyEnergySummary,
    cost: MonthlyCostSummary,
    daikin_heating_kwh: float | None = None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Heating (kWh and cost) and equivalent gas cost + ahead amount.
    When daikin_heating_kwh is provided, use it; otherwise fall back to load * HEATING_LOAD_SHARE."""
    heating_kwh = daikin_heating_kwh
    if heating_kwh is None:
        share = config.HEATING_LOAD_SHARE
        if share <= 0 or energy.load_kwh <= 0:
            return None, None, None, None
        heating_kwh = energy.load_kwh * share
    import_rate = config.MANUAL_TARIFF_IMPORT_PENCE or 0.0
    heating_cost_pence = heating_kwh * import_rate if import_rate else None

    gas_price = config.GAS_PRICE_PENCE_PER_KWH or 0.0
    gas_eff = config.GAS_BOILER_EFFICIENCY or 0.9
    cop = config.HEAT_PUMP_COP_ESTIMATE or 2.8
    if gas_price <= 0:
        return heating_kwh, heating_cost_pence, None, None
    # Heat delivered by heat pump ≈ heating_elec_kwh * COP
    heat_delivered_kwh = heating_kwh * cop
    # Equivalent gas kWh = heat_delivered / efficiency
    equivalent_gas_kwh = heat_delivered_kwh / gas_eff
    equivalent_gas_cost_pence = equivalent_gas_kwh * gas_price
    # Ahead = equivalent gas cost - our electricity cost (for the month we're comparing)
    ahead_pence = equivalent_gas_cost_pence - cost.net_cost_pence
    ahead_pounds = ahead_pence / 100 if ahead_pence is not None else None
    return (
        round(heating_kwh, 2),
        round(heating_cost_pence, 2) if heating_cost_pence is not None else None,
        round(equivalent_gas_cost_pence, 2),
        round(ahead_pounds, 2) if ahead_pounds is not None else None,
    )


def get_monthly_insights(year: int, month: int) -> MonthlyInsights | None:
    """Build full monthly insights (energy, cost, heating estimate, gas comparison).

    Returns None if Fox ESS is not configured or the request fails.
    Raises ValueError for future months (no data).
    """
    from datetime import date as date_type
    today = date_type.today()
    if (year, month) > (today.year, today.month):
        raise ValueError("No data for future months. Select the current month or a past month.")
    try:
        raw = get_cached_energy_month(year, month)
    except Exception:
        raise  # Let API layer return 502 with the actual error
    energy = _foxess_to_energy_summary(year, month, raw)
    # Prorate standing to elapsed days for the current month (the Fox aggregate
    # covers only elapsed days); past months bill the full calendar month.
    n_days = today.day if (year, month) == (today.year, today.month) else None
    cost = _best_cost(energy, n_days=n_days)
    daikin_heating = _get_daikin_heating_kwh(year, month)
    heating_kwh, heating_cost_pence, equiv_gas_pence, ahead_pounds = _compute_heating_and_gas(
        energy, cost, daikin_heating_kwh=daikin_heating
    )
    return MonthlyInsights(
        energy=energy,
        cost=cost,
        heating_estimate_kwh=heating_kwh,
        heating_estimate_cost_pence=heating_cost_pence,
        equivalent_gas_cost_pence=equiv_gas_pence,
        gas_comparison_ahead_pounds=ahead_pounds,
    )


@dataclass
class PeriodInsights:
    """Insights for day/week/month/year with optional chart_data and heating_analytics."""
    period: str  # "day" | "week" | "month" | "year"
    period_label: str  # e.g. "2026-02-10", "4–10 Feb 2026", "Feb 2026", "2026"
    insights: MonthlyInsights
    chart_data: list[dict] = field(default_factory=list)  # [{ date, import_kwh, ... }]
    heating_analytics: HeatingAnalytics | None = None


def _client():
    return FoxESSClient(**config.foxess_client_kwargs())


def get_period_insights(
    period: str,
    date_str: str | None = None,
    month_str: str | None = None,
    year: int | None = None,
) -> PeriodInsights | None:
    """Build insights + chart_data for day, week, month, or year.
    period=day|week|month|year. For day/week pass date_str=YYYY-MM-DD; for month pass month_str=YYYY-MM; for year pass year=YYYY.
    """
    today = date.today()

    if period == "year" and year is not None:
        if year > today.year:
            raise ValueError("No data for future years.")
        try:
            client = _client()
        except Exception:
            raise
        chart_data = []
        import_kwh = export_kwh = solar_kwh = load_kwh = charge_kwh = discharge_kwh = 0.0
        import_cost_pence = export_earnings_pence = standing_charge_pence = 0.0
        fixed_shadow_sum = delta_vs_fixed_sum = 0.0
        any_fixed_shadow = False
        heating_kwh_sum = heating_cost_sum = equiv_gas_sum = ahead_sum = 0.0
        n_months = 0
        for m in range(1, 13):
            if (year, m) > (today.year, today.month):
                break
            try:
                totals, daily = client.get_energy_month_daily_breakdown(year, m)
            except Exception:
                continue
            e = _foxess_to_energy_summary(year, m, totals)
            # Prorate standing to elapsed days for the current month; past
            # months bill the full month (len(daily) == days-in-month).
            n_days = len(daily)
            if (year, m) == (today.year, today.month):
                n_days = min(n_days, today.day)
            c = _best_cost(e, n_days=n_days)
            daikin_h = _get_daikin_heating_kwh(year, m)
            h_kwh, h_cost, equiv, ahead = _compute_heating_and_gas(e, c, daikin_heating_kwh=daikin_h)
            import_kwh += e.import_kwh
            export_kwh += e.export_kwh
            solar_kwh += e.solar_kwh
            load_kwh += e.load_kwh
            charge_kwh += e.charge_kwh
            discharge_kwh += e.discharge_kwh
            import_cost_pence += c.import_cost_pence
            export_earnings_pence += c.export_earnings_pence
            standing_charge_pence += c.standing_charge_pence
            if c.fixed_shadow_pence is not None:
                any_fixed_shadow = True
                fixed_shadow_sum += c.fixed_shadow_pence
                delta_vs_fixed_sum += c.delta_vs_fixed_pence or 0.0
            if h_kwh is not None:
                heating_kwh_sum += h_kwh
            if h_cost is not None:
                heating_cost_sum += h_cost
            if equiv is not None:
                equiv_gas_sum += equiv
            if ahead is not None:
                ahead_sum += ahead
            n_months += 1
            chart_data.append({
                "date": f"{year:04d}-{m:02d}-01",
                "import_kwh": round(e.import_kwh, 2),
                "export_kwh": round(e.export_kwh, 2),
                "solar_kwh": round(e.solar_kwh, 2),
                "load_kwh": round(e.load_kwh, 2),
                "charge_kwh": round(e.charge_kwh, 2),
                "discharge_kwh": round(e.discharge_kwh, 2),
            })
        net_cost_pence = import_cost_pence + standing_charge_pence - export_earnings_pence
        energy = MonthlyEnergySummary(
            year=year,
            month=1,
            month_str=f"{year:04d}-01",
            import_kwh=round(import_kwh, 2),
            export_kwh=round(export_kwh, 2),
            solar_kwh=round(solar_kwh, 2),
            load_kwh=round(load_kwh, 2),
            charge_kwh=round(charge_kwh, 2),
            discharge_kwh=round(discharge_kwh, 2),
        )
        cost = MonthlyCostSummary(
            import_cost_pence=round(import_cost_pence, 2),
            export_earnings_pence=round(export_earnings_pence, 2),
            standing_charge_pence=round(standing_charge_pence, 2),
            net_cost_pence=round(net_cost_pence, 2),
            fixed_shadow_pence=round(fixed_shadow_sum, 2) if any_fixed_shadow else None,
            delta_vs_fixed_pence=round(delta_vs_fixed_sum, 2) if any_fixed_shadow else None,
        )
        heating_kwh = round(heating_kwh_sum, 2) if heating_kwh_sum else None
        heating_cost_pence_out = round(heating_cost_sum, 2) if heating_cost_sum else None
        equiv_gas_pence = round(equiv_gas_sum, 2) if equiv_gas_sum else None
        ahead_pounds = round(ahead_sum, 2) if ahead_sum else None
        insights = MonthlyInsights(
            energy=energy,
            cost=cost,
            heating_estimate_kwh=heating_kwh,
            heating_estimate_cost_pence=heating_cost_pence_out,
            equivalent_gas_cost_pence=equiv_gas_pence,
            gas_comparison_ahead_pounds=ahead_pounds,
        )
        heating_analytics = _heating_analytics_percent_only(insights)
        return PeriodInsights(
            period="year", period_label=str(year), insights=insights, chart_data=chart_data,
            heating_analytics=heating_analytics,
        )

    if period == "month" and month_str:
        y, m = int(month_str[:4]), int(month_str[5:7])
        if (y, m) > (today.year, today.month):
            raise ValueError("No data for future months.")
        try:
            client = _client()
            totals, daily = client.get_energy_month_daily_breakdown(y, m)
        except Exception:
            raise
        energy = _foxess_to_energy_summary(y, m, totals)
        # For the current (partial) month Fox returns a full-month daily
        # breakdown with zero-padded future days. Trim to elapsed days so the
        # standing-charge day-count, chart_data length, and the UI's per-day
        # math (standing ÷ chart_data.length) all agree. Past months keep the
        # full month.
        if (y, m) == (today.year, today.month):
            daily = [r for r in daily if r.get("date", "") <= today.isoformat()]
        n_days = len(daily)
        cost = _best_cost(energy, n_days=n_days)
        daikin_heating = _get_daikin_heating_kwh(y, m)
        heating_kwh, heating_cost_pence, equiv_gas_pence, ahead_pounds = _compute_heating_and_gas(
            energy, cost, daikin_heating_kwh=daikin_heating
        )
        insights = MonthlyInsights(
            energy=energy,
            cost=cost,
            heating_estimate_kwh=heating_kwh,
            heating_estimate_cost_pence=heating_cost_pence,
            equivalent_gas_cost_pence=equiv_gas_pence,
            gas_comparison_ahead_pounds=ahead_pounds,
        )
        from datetime import datetime as dt
        label = dt(y, m, 1).strftime("%b %Y")
        heating_analytics = _build_heating_analytics(insights, y, m)
        return PeriodInsights(
            period="month", period_label=label, insights=insights, chart_data=daily,
            heating_analytics=heating_analytics,
        )

    if period in ("day", "week") and date_str:
        y, m, d = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
        dte = date(y, m, d)
        if dte > today:
            raise ValueError("No data for future dates.")
        try:
            client = _client()
        except Exception:
            raise
        if period == "day":
            raw = client.get_energy_day(y, m, d)
            energy = MonthlyEnergySummary(
                year=y, month=m, month_str=date_str[:7],
                import_kwh=raw.get("gridConsumptionEnergyToday", 0) or 0,
                export_kwh=raw.get("feedinEnergyToday", 0) or 0,
                solar_kwh=raw.get("pvEnergyToday", 0) or 0,
                load_kwh=raw.get("loadEnergyToday", 0) or 0,
                charge_kwh=raw.get("chargeEnergyToday", 0) or 0,
                discharge_kwh=raw.get("dischargeEnergyToday", 0) or 0,
            )
            day_from = datetime(y, m, d, 0, 0, 0, tzinfo=UTC)
            day_to = datetime(y, m, d, 23, 59, 59, tzinfo=UTC)
            cost = _period_cost(energy, day_from, day_to, n_days=1)
            heating_kwh, heating_cost_pence, equiv_gas_pence, ahead_pounds = _compute_heating_and_gas(energy, cost)
            insights = MonthlyInsights(
                energy=energy,
                cost=cost,
                heating_estimate_kwh=heating_kwh,
                heating_estimate_cost_pence=heating_cost_pence,
                equivalent_gas_cost_pence=equiv_gas_pence,
                gas_comparison_ahead_pounds=ahead_pounds,
            )
            chart_data = [{"date": date_str, "import_kwh": energy.import_kwh, "export_kwh": energy.export_kwh, "solar_kwh": energy.solar_kwh, "load_kwh": energy.load_kwh, "charge_kwh": energy.charge_kwh, "discharge_kwh": energy.discharge_kwh}]
            from datetime import datetime as dt
            label = dte.strftime("%d %b %Y")
            heating_analytics = _build_heating_analytics(insights, y, m)
            return PeriodInsights(
                period="day", period_label=label, insights=insights, chart_data=chart_data,
                heating_analytics=heating_analytics,
            )

        # week: Monday-based week containing dte
        week_start = dte - timedelta(days=dte.weekday())
        week_end = week_start + timedelta(days=6)
        if week_end > today:
            week_end = today
        # Fetch daily breakdown for the month(s) that contain this week
        daily_all = []
        months_done = set()
        for delta in range((week_end - week_start).days + 1):
            cur = week_start + timedelta(days=delta)
            key = (cur.year, cur.month)
            if key in months_done:
                continue
            months_done.add(key)
            tot, daily = client.get_energy_month_daily_breakdown(cur.year, cur.month)
            for row in daily:
                row_date = date.fromisoformat(row["date"])
                if week_start <= row_date <= week_end:
                    daily_all.append(row)
        daily_all.sort(key=lambda r: r["date"])
        # Totals for the week
        import_kwh = sum(r["import_kwh"] for r in daily_all)
        export_kwh = sum(r["export_kwh"] for r in daily_all)
        solar_kwh = sum(r["solar_kwh"] for r in daily_all)
        load_kwh = sum(r["load_kwh"] for r in daily_all)
        charge_kwh = sum(r["charge_kwh"] for r in daily_all)
        discharge_kwh = sum(r["discharge_kwh"] for r in daily_all)
        energy = MonthlyEnergySummary(year=week_start.year, month=week_start.month, month_str=week_start.strftime("%Y-%m"), import_kwh=import_kwh, export_kwh=export_kwh, solar_kwh=solar_kwh, load_kwh=load_kwh, charge_kwh=charge_kwh, discharge_kwh=discharge_kwh)
        days_count = max(1, len(daily_all))
        week_from = datetime(week_start.year, week_start.month, week_start.day, 0, 0, 0, tzinfo=UTC)
        week_to = datetime(week_end.year, week_end.month, week_end.day, 23, 59, 59, tzinfo=UTC)
        cost = _period_cost(energy, week_from, week_to, n_days=days_count)
        heating_kwh, heating_cost_pence, equiv_gas_pence, ahead_pounds = _compute_heating_and_gas(energy, cost)
        insights = MonthlyInsights(energy=energy, cost=cost, heating_estimate_kwh=heating_kwh, heating_estimate_cost_pence=heating_cost_pence, equivalent_gas_cost_pence=equiv_gas_pence, gas_comparison_ahead_pounds=ahead_pounds)
        label = f"{week_start.strftime('%d')}–{week_end.strftime('%d %b %Y')}" if week_start.month == week_end.month else f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"
        heating_analytics = _build_heating_analytics(insights, week_start.year, week_start.month)
        return PeriodInsights(
            period="week", period_label=label, insights=insights, chart_data=daily_all,
            heating_analytics=heating_analytics,
        )
    return None
