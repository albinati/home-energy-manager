"""Monthly energy summary, cost, heating estimate and gas comparison.

Uses Fox ESS monthly data + manual tariff + config for heating share and gas comparison.
When available, heating kWh is taken from Daikin (Onecta) instead of load-share estimate.
Supports day, week, and month periods with chart_data for the UI.
Optional weather (WEATHER_LAT/LON) enables degree-days and spend-by-temperature analytics.
"""
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from ..config import config
from ..foxess import get_cached_energy_month
from ..foxess.client import FoxESSClient


# Temperature bands for "spend by outdoor temp" (°C)
TEMP_BANDS = [
    ("<0°C", lambda t: t is not None and t < 0),
    ("0–5°C", lambda t: t is not None and 0 <= t < 5),
    ("5–10°C", lambda t: t is not None and 5 <= t < 10),
    ("10–15°C", lambda t: t is not None and 10 <= t < 15),
    ("15°C+", lambda t: t is not None and t >= 15),
]


@dataclass
class TempBandSummary:
    """Heating spend/kWh for one temperature band."""
    band: str
    days: int
    heating_kwh: float
    cost_pounds: float
    avg_temp_c: Optional[float] = None


@dataclass
class HeatingAnalytics:
    """Heating share and weather-based analytics for the period."""
    heating_percent_of_cost: Optional[float] = None  # heating cost / net cost * 100
    heating_percent_of_consumption: Optional[float] = None  # heating_kwh / load_kwh * 100
    avg_outdoor_temp_c: Optional[float] = None
    degree_days: Optional[float] = None  # sum max(0, base - daily_temp)
    cost_per_degree_day_pounds: Optional[float] = None  # heating_cost / degree_days
    heating_kwh_per_degree_day: Optional[float] = None
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

    @property
    def net_cost_pounds(self) -> float:
        return self.net_cost_pence / 100

    @property
    def import_cost_pounds(self) -> float:
        return self.import_cost_pence / 100

    @property
    def export_earnings_pounds(self) -> float:
        return self.export_earnings_pence / 100


@dataclass
class MonthlyInsights:
    """Full monthly view: energy, cost, heating estimate, gas comparison."""
    energy: MonthlyEnergySummary
    cost: MonthlyCostSummary
    heating_estimate_kwh: Optional[float] = None
    heating_estimate_cost_pence: Optional[float] = None
    equivalent_gas_cost_pence: Optional[float] = None
    gas_comparison_ahead_pounds: Optional[float] = None  # positive = ahead with solar+ASHP

    @property
    def equivalent_gas_cost_pounds(self) -> Optional[float]:
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


def _compute_cost(energy: MonthlyEnergySummary) -> MonthlyCostSummary:
    """Apply manual tariff and standing charge."""
    import_rate = config.MANUAL_TARIFF_IMPORT_PENCE or 0.0
    export_rate = config.MANUAL_TARIFF_EXPORT_PENCE or 0.0
    standing_per_day = config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY or 0.0
    days = monthrange(energy.year, energy.month)[1]
    import_cost_pence = energy.import_kwh * import_rate
    export_earnings_pence = energy.export_kwh * export_rate
    standing_charge_pence = days * standing_per_day
    net_cost_pence = import_cost_pence + standing_charge_pence - export_earnings_pence
    return MonthlyCostSummary(
        import_cost_pence=round(import_cost_pence, 2),
        export_earnings_pence=round(export_earnings_pence, 2),
        standing_charge_pence=round(standing_charge_pence, 2),
        net_cost_pence=round(net_cost_pence, 2),
    )


def _get_daikin_heating_kwh(year: int, month: int) -> Optional[float]:
    """Get heating electrical consumption (kWh) for the month from Daikin when available. Returns None if not configured or not exposed."""
    try:
        from ..daikin.client import DaikinClient
        client = DaikinClient()
        return client.get_heating_consumption_kwh(year, month)
    except Exception:
        return None


def _get_daikin_heating_daily_kwh(year: int, month: int) -> Optional[list[float]]:
    """Get daily heating (kWh) for the month from Daikin when available. List length = days in month."""
    try:
        from ..daikin.client import DaikinClient
        client = DaikinClient()
        return client.get_heating_daily_kwh(year, month)
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


def _heating_analytics_percent_only(insights: "MonthlyInsights") -> Optional[HeatingAnalytics]:
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
    daikin_heating_kwh: Optional[float] = None,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
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


def get_monthly_insights(year: int, month: int) -> Optional[MonthlyInsights]:
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
    cost = _compute_cost(energy)
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
    heating_analytics: Optional[HeatingAnalytics] = None


def _client():
    return FoxESSClient(**config.foxess_client_kwargs())


def get_period_insights(
    period: str,
    date_str: Optional[str] = None,
    month_str: Optional[str] = None,
    year: Optional[int] = None,
) -> Optional[PeriodInsights]:
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
            c = _compute_cost(e)
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
        cost = _compute_cost(energy)
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
            standing_per_day = config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY or 0
            import_rate = config.MANUAL_TARIFF_IMPORT_PENCE or 0
            export_rate = config.MANUAL_TARIFF_EXPORT_PENCE or 0
            imp = energy.import_kwh * import_rate
            exp = energy.export_kwh * export_rate
            cost = MonthlyCostSummary(
                import_cost_pence=round(imp, 2),
                export_earnings_pence=round(exp, 2),
                standing_charge_pence=round(standing_per_day, 2),
                net_cost_pence=round(imp + standing_per_day - exp, 2),
            )
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
        standing_per_day = config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY or 0
        imp_w = import_kwh * (config.MANUAL_TARIFF_IMPORT_PENCE or 0)
        exp_w = export_kwh * (config.MANUAL_TARIFF_EXPORT_PENCE or 0)
        stand_w = days_count * standing_per_day
        cost = MonthlyCostSummary(
            import_cost_pence=round(imp_w, 2),
            export_earnings_pence=round(exp_w, 2),
            standing_charge_pence=round(stand_w, 2),
            net_cost_pence=round(imp_w + stand_w - exp_w, 2),
        )
        heating_kwh, heating_cost_pence, equiv_gas_pence, ahead_pounds = _compute_heating_and_gas(energy, cost)
        insights = MonthlyInsights(energy=energy, cost=cost, heating_estimate_kwh=heating_kwh, heating_estimate_cost_pence=heating_cost_pence, equivalent_gas_cost_pence=equiv_gas_pence, gas_comparison_ahead_pounds=ahead_pounds)
        label = f"{week_start.strftime('%d')}–{week_end.strftime('%d %b %Y')}" if week_start.month == week_end.month else f"{week_start.strftime('%d %b')} – {week_end.strftime('%d %b %Y')}"
        heating_analytics = _build_heating_analytics(insights, week_start.year, week_start.month)
        return PeriodInsights(
            period="week", period_label=label, insights=insights, chart_data=daily_all,
            heating_analytics=heating_analytics,
        )
    return None
