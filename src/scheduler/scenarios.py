"""Scenario-LP: re-run the canonical solve under perturbed forecast inputs.

Three solves per dispatch event (optimistic, nominal, pessimistic) — used by
``filter_robust_peak_export`` to drop ``peak_export`` slots that don't survive
a stressed forecast. The LP itself is invoked unchanged: each scenario only
shifts outdoor temperature (which adjusts heating-energy bounds AND heat-pump
COP) and scales the base-load profile.

Design rationale and the maximin / robust-optimisation framing live in
``docs/DISPATCH_DECISIONS.md``. This module is intentionally side-effect
free; persistence happens in ``src.scheduler.lp_dispatch.filter_robust_peak_export``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from ..config import config, cop_at_temperature
from ..weather import WeatherLpSeries
from .lp_optimizer import LpInitialState, LpPlan, solve_lp

logger = logging.getLogger(__name__)

Scenario = Literal["optimistic", "nominal", "pessimistic"]
SCENARIOS: tuple[Scenario, ...] = ("optimistic", "nominal", "pessimistic")


@dataclass
class _Perturbation:
    """Fixed forecast deltas for one scenario."""

    temp_delta_c: float
    load_factor: float


def _perturbation_for(scenario: Scenario) -> _Perturbation:
    if scenario == "nominal":
        return _Perturbation(0.0, 1.0)
    if scenario == "optimistic":
        return _Perturbation(
            float(config.LP_SCENARIO_OPTIMISTIC_TEMP_DELTA_C),
            float(config.LP_SCENARIO_OPTIMISTIC_LOAD_FACTOR),
        )
    if scenario == "pessimistic":
        return _Perturbation(
            float(config.LP_SCENARIO_PESSIMISTIC_TEMP_DELTA_C),
            float(config.LP_SCENARIO_PESSIMISTIC_LOAD_FACTOR),
        )
    raise ValueError(f"Unknown scenario: {scenario!r}")


def perturb_weather(weather: WeatherLpSeries, temp_delta_c: float) -> WeatherLpSeries:
    """Return a new ``WeatherLpSeries`` with shifted outdoor temp + recomputed COP.

    PV (irradiance-driven) and cloud cover are NOT shifted by temperature; air
    temperature is decoupled from solar generation in the model. Heat-pump COP
    is recomputed via ``cop_at_temperature`` so cold-snap perturbations also
    capture efficiency loss, not just heating-demand growth.
    """
    if temp_delta_c == 0.0:
        return weather

    curve = config.DAIKIN_COP_CURVE
    dhw_pen = float(config.COP_DHW_PENALTY)
    new_t = [t + temp_delta_c for t in weather.temperature_outdoor_c]
    new_cop_space = [max(1.0, cop_at_temperature(curve, t)) for t in new_t]
    new_cop_dhw = [max(1.0, c - dhw_pen) for c in new_cop_space]

    return WeatherLpSeries(
        slot_starts_utc=list(weather.slot_starts_utc),
        temperature_outdoor_c=new_t,
        shortwave_radiation_wm2=list(weather.shortwave_radiation_wm2),
        cloud_cover_pct=list(weather.cloud_cover_pct),
        pv_kwh_per_slot=list(weather.pv_kwh_per_slot),
        cop_space=new_cop_space,
        cop_dhw=new_cop_dhw,
    )


def perturb_base_load(base_load_kwh: list[float], factor: float) -> list[float]:
    """Multiplicative perturbation of the residual (non-Daikin) base-load profile."""
    if factor == 1.0:
        return list(base_load_kwh)
    return [max(0.0, x * factor) for x in base_load_kwh]


def solve_scenarios(
    *,
    slot_starts_utc,
    price_pence: list[float],
    base_load_kwh: list[float],
    weather: WeatherLpSeries,
    initial: LpInitialState,
    tz,
    micro_climate_offset_c: float = 0.0,
    export_price_pence: list[float] | None = None,
    scenarios: tuple[Scenario, ...] = SCENARIOS,
) -> dict[Scenario, LpPlan]:
    """Solve the LP under each scenario; return a mapping name → plan.

    ``nominal`` is the canonical solve; if the caller already has it computed,
    they can short-circuit by injecting it via ``solve_scenarios_with_nominal``.
    Failures in any single scenario are logged and that scenario maps to an
    ``LpPlan`` with ``ok=False`` so callers can detect partial results.
    """
    out: dict[Scenario, LpPlan] = {}
    for s in scenarios:
        p = _perturbation_for(s)
        w = perturb_weather(weather, p.temp_delta_c)
        bl = perturb_base_load(base_load_kwh, p.load_factor)
        try:
            plan = solve_lp(
                slot_starts_utc=slot_starts_utc,
                price_pence=price_pence,
                base_load_kwh=bl,
                weather=w,
                initial=initial,
                tz=tz,
                micro_climate_offset_c=micro_climate_offset_c,
                export_price_pence=export_price_pence,
            )
            out[s] = plan
            logger.info(
                "scenario %s: status=%s objective=%.0fp Δt=%+.1f load×%.2f",
                s, plan.status, plan.objective_pence,
                p.temp_delta_c, p.load_factor,
            )
        except Exception as e:
            logger.warning(
                "scenario %s solve failed (Δt=%+.1f load×%.2f): %s",
                s, p.temp_delta_c, p.load_factor, e,
            )
            out[s] = LpPlan(ok=False, status=f"error: {e}", objective_pence=0.0)
    return out


def solve_scenarios_with_nominal(
    *,
    nominal: LpPlan,
    slot_starts_utc,
    price_pence: list[float],
    base_load_kwh: list[float],
    weather: WeatherLpSeries,
    initial: LpInitialState,
    tz,
    micro_climate_offset_c: float = 0.0,
    export_price_pence: list[float] | None = None,
) -> dict[Scenario, LpPlan]:
    """Same as ``solve_scenarios`` but reuses an already-computed nominal plan.

    Saves one LP solve (~1-3 s) when the canonical run has already happened
    upstream — the common path inside ``run_optimizer``.
    """
    extras = solve_scenarios(
        slot_starts_utc=slot_starts_utc,
        price_pence=price_pence,
        base_load_kwh=base_load_kwh,
        weather=weather,
        initial=initial,
        tz=tz,
        micro_climate_offset_c=micro_climate_offset_c,
        export_price_pence=export_price_pence,
        scenarios=("optimistic", "pessimistic"),
    )
    extras["nominal"] = nominal
    return extras


def trigger_runs_scenarios(trigger_reason: str) -> bool:
    """True when the configured allow-list includes this trigger reason.

    ``LP_SCENARIOS_ON_TRIGGER_REASONS`` (default ``cron,plan_push``) controls
    which optimizer invocations get the full 3-pass scenario solve. Triggers
    not in the list (drift, forecast_revision, dynamic_replan, …) keep using
    only the nominal solve so re-plan latency stays low.
    """
    raw = (config.LP_SCENARIOS_ON_TRIGGER_REASONS or "").strip()
    if not raw:
        return False
    allowed = {x.strip().lower() for x in raw.split(",") if x.strip()}
    return (trigger_reason or "").strip().lower() in allowed
