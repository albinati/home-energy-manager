"""Run the PuLP MILP with live SQLite rates, weather, and telemetry — no Fox/Daikin writes."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Optional

from .. import db
from ..config import config
from ..weather import HourlyForecast, fetch_forecast, forecast_to_lp_inputs, compute_pv_calibration_factor
from .lp_initial_state import read_lp_initial_state
from .lp_optimizer import LpInitialState, LpPlan, solve_lp
from .optimizer import TZ, _build_half_hour_slots

logger = logging.getLogger(__name__)


@dataclass
class LpSimulationResult:
    """Outcome of :func:`run_lp_simulation` (read-only)."""

    ok: bool
    error: Optional[str] = None
    plan_date: str = ""
    plan: Optional[LpPlan] = None
    initial: Optional[LpInitialState] = None
    mu_load_kwh: float = 0.0
    slot_count: int = 0
    actual_mean_agile_pence: float = 0.0
    forecast_solar_kwh_horizon: float = 0.0
    forecast: Optional[list[HourlyForecast]] = None
    pv_scale_factor: float = 1.0


def _build_load_profile(slot_starts_utc: list[datetime]) -> list[float]:
    """Per-slot base load using hourly profile from execution_log.

    Falls back to Fox daily mean (load_kwh / 48) when execution_log is cold,
    then to a hardcoded default of 0.4 kWh/slot.
    """
    limit = int(getattr(config, "LP_LOAD_PROFILE_SLOTS", 2016))
    profile = db.hourly_load_profile_kwh(limit=limit)
    flat_from_log = db.mean_consumption_kwh_from_execution_logs(limit=limit)
    fox_mean = db.mean_fox_load_kwh_per_slot(limit=60)
    flat = fox_mean if fox_mean is not None else flat_from_log
    out: list[float] = []
    for s in slot_starts_utc:
        h = s.astimezone().hour
        out.append(profile.get(h, flat))
    return out


def run_lp_simulation(*, daikin: Optional[Any] = None) -> LpSimulationResult:
    """Build the same inputs as ``_run_optimizer_lp``, solve, return plan — **no DB/Fox/Daikin writes**.

    Requires ``OCTOPUS_TARIFF_CODE``, rates in SQLite for tomorrow's horizon (run Octopus fetch if empty).
    """
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return LpSimulationResult(ok=False, error="OCTOPUS_TARIFF_CODE not set in environment")

    tz = TZ()
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    plan_date = tomorrow.isoformat()
    day_start = datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=tz)
    horizon_end = day_start + timedelta(hours=int(config.LP_HORIZON_HOURS))

    rates = db.get_rates_for_period(
        tariff,
        day_start.astimezone(timezone.utc) - timedelta(hours=1),
        horizon_end.astimezone(timezone.utc) + timedelta(hours=2),
    )
    if not rates:
        return LpSimulationResult(
            ok=False,
            error="No Agile rates in SQLite for the LP horizon — run the Octopus fetch job or `octopus_fetch` first.",
        )

    slots = _build_half_hour_slots(rates, day_start, horizon_end)
    if not slots:
        return LpSimulationResult(
            ok=False,
            error="No half-hour slots in horizon — check rate coverage for the product window.",
        )

    prices = [s.price_pence for s in slots]
    starts = [s.start_utc for s in slots]

    # Per-slot load profile (hour-of-day bins); Fox daily mean when execution_log is cold
    base_load = _build_load_profile(starts)
    mu_load = sum(base_load) / len(base_load) if base_load else 0.4

    forecast = fetch_forecast(hours=max(48, int(config.LP_HORIZON_HOURS) + 24))

    # PV calibration: Fox actual solar vs Open-Meteo archive to correct systematic overestimate
    pv_scale = compute_pv_calibration_factor()

    weather = forecast_to_lp_inputs(forecast, starts, pv_scale=pv_scale)
    initial = read_lp_initial_state(daikin)

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=initial,
        tz=tz,
    )

    solar_kwh = sum(weather.pv_kwh_per_slot) if weather.pv_kwh_per_slot else 0.0
    actual_mean = mean(prices) if prices else 0.0

    if not plan.ok:
        return LpSimulationResult(
            ok=False,
            error=f"LP solver status: {plan.status} (infeasible or timed out — try raising LP_HIGHS_TIME_LIMIT_SECONDS)",
            plan_date=plan_date,
            plan=plan,
            initial=initial,
            mu_load_kwh=mu_load,
            slot_count=len(slots),
            actual_mean_agile_pence=actual_mean,
            forecast_solar_kwh_horizon=solar_kwh,
            forecast=forecast,
            pv_scale_factor=pv_scale,
        )

    return LpSimulationResult(
        ok=True,
        plan_date=plan_date,
        plan=plan,
        initial=initial,
        mu_load_kwh=mu_load,
        slot_count=len(slots),
        actual_mean_agile_pence=actual_mean,
        forecast_solar_kwh_horizon=solar_kwh,
        forecast=forecast,
        pv_scale_factor=pv_scale,
    )
