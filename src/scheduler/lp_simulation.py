"""Run the PuLP MILP with live SQLite rates, weather, and telemetry — no Fox/Daikin writes."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..weather import (
    HourlyForecast,
    compute_pv_calibration_factor,
    fetch_forecast,
    forecast_to_lp_inputs,
)
from .lp_initial_state import read_lp_initial_state
from .lp_optimizer import LpInitialState, LpPlan, solve_lp
from .optimizer import TZ, _build_half_hour_slots, _resolve_plan_window

logger = logging.getLogger(__name__)


@dataclass
class LpSimulationResult:
    """Outcome of :func:`run_lp_simulation` (read-only)."""

    ok: bool
    error: str | None = None
    plan_date: str = ""
    plan_window: str = ""        # "rolling_24h" | "rolling_partial"
    plan: LpPlan | None = None
    initial: LpInitialState | None = None
    mu_load_kwh: float = 0.0
    slot_count: int = 0
    actual_mean_agile_pence: float = 0.0
    forecast_solar_kwh_horizon: float = 0.0
    forecast: list[HourlyForecast] | None = None
    pv_scale_factor: float = 1.0
    # kept for compat — mirrors plan.slot_starts_utc
    slot_starts_utc: list = None  # type: ignore[assignment]
    objective_pence: float = 0.0
    status: str = ""


def _build_load_profile(slot_starts_utc: list[datetime]) -> list[float]:
    """Per-slot base load using half-hourly profile from execution_log.

    Half-hour granularity (S10.8 / #175) preserves intra-hour variance.
    Falls back to Fox daily mean (load_kwh / 48) when execution_log is cold,
    then to a hardcoded default of 0.4 kWh/slot.
    """
    limit = int(getattr(config, "LP_LOAD_PROFILE_SLOTS", 2016))
    # Daikin-subtracted residual profile (S10.13 / #179) — avoids LP double-count
    profile = db.half_hourly_residual_load_profile_kwh()
    flat_from_log = db.mean_consumption_kwh_from_execution_logs(limit=limit)
    fox_mean = db.mean_fox_load_kwh_per_slot(limit=60)
    flat = fox_mean if fox_mean is not None else flat_from_log
    out: list[float] = []
    for s in slot_starts_utc:
        local = s.astimezone(ZoneInfo(config.BULLETPROOF_TIMEZONE))
        bucket = (local.hour, 30 if local.minute >= 30 else 0)
        out.append(profile.get(bucket, flat))
    return out


def run_lp_simulation(
    *,
    daikin: Any | None = None,
    allow_daikin_refresh: bool = True,
) -> LpSimulationResult:
    """Build the same inputs as ``_run_optimizer_lp``, solve, return plan — **no DB/Fox/Daikin writes**.

    Uses the rolling ``now → now + LP_HORIZON_HOURS`` window (truncated to the
    last published Agile slot). Returns an error result if no rates exist or
    fewer than the minimum usable slot count remain.

    Phase 4 review: ``allow_daikin_refresh=False`` forbids any cache refresh that
    would burn Daikin quota. The ``simulate_plan`` MCP tool relies on this to
    keep the "no quota burn" guarantee even when the process cache is cold.
    """
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return LpSimulationResult(ok=False, error="OCTOPUS_TARIFF_CODE not set in environment")

    tz = TZ()
    window = _resolve_plan_window(tariff)
    if window is None:
        return LpSimulationResult(
            ok=False,
            error="No Agile rates in SQLite for today or tomorrow — run the Octopus fetch job or `octopus_fetch` first.",
        )

    plan_date = window.plan_date
    plan_window_label = "rolling_24h" if window.horizon_hours >= 23.5 else "rolling_partial"
    day_start = window.day_start
    horizon_end = window.horizon_end

    slots = _build_half_hour_slots(window.rates, day_start, horizon_end)
    if not slots:
        return LpSimulationResult(
            ok=False,
            error="No half-hour slots in horizon — check rate coverage for the product window.",
            plan_date=plan_date,
            plan_window=plan_window_label,
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
    initial = read_lp_initial_state(daikin, allow_daikin_refresh=allow_daikin_refresh)

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
            plan_window=plan_window_label,
            plan=plan,
            initial=initial,
            mu_load_kwh=mu_load,
            slot_count=len(slots),
            actual_mean_agile_pence=actual_mean,
            forecast_solar_kwh_horizon=solar_kwh,
            forecast=forecast,
            pv_scale_factor=pv_scale,
            slot_starts_utc=starts,
            objective_pence=plan.objective_pence,
            status=plan.status,
        )

    return LpSimulationResult(
        ok=True,
        plan_date=plan_date,
        plan_window=plan_window_label,
        plan=plan,
        initial=initial,
        mu_load_kwh=mu_load,
        slot_count=len(slots),
        actual_mean_agile_pence=actual_mean,
        forecast_solar_kwh_horizon=solar_kwh,
        forecast=forecast,
        pv_scale_factor=pv_scale,
        slot_starts_utc=starts,
        objective_pence=plan.objective_pence,
        status=plan.status,
    )
