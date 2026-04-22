"""Physics-based Daikin state estimator (#55).

Walks forward from the last live telemetry row using passive thermal dynamics
(tank standing-loss + building UA loss toward outdoor). Used as a fallback
when the Onecta daily quota (200 req/day) is exhausted, so the LP can still
run with a sensible tank / indoor seed without a live fetch.

Accuracy target: within ~0.5 °C of live readings over a 3–6 hour horizon
under idle conditions (household absent or comfort steady). When the planner
has committed active heating during the estimation window, the current MVP
ignores the scheduled heat contribution — accepting a slight undershoot
(tank / indoor reported cooler than reality). That is the safe-by-default
error direction for the LP: it will plan slightly more heating than strictly
necessary rather than too little.

Physics (mirrors ``lp_optimizer.py``; same symbols):

- Tank:   C_tank  = DHW_TANK_LITRES * DHW_WATER_CP            [J/K]
          UA_tank = DHW_TANK_UA_W_PER_K                       [W/K]
          T_amb   = indoor_temp_c                             [°C]
          T(t)    = T_amb + (T0 - T_amb) * exp(-UA_tank * t / C_tank)

- Indoor: C_bld   = BUILDING_THERMAL_MASS_KWH_PER_K * 3.6e6   [J/K]
          UA_bld  = BUILDING_UA_W_PER_K                       [W/K]
          T_out   = mean outdoor over the horizon             [°C]
          T(t)    = T_out + (T0 - T_out) * exp(-UA_bld * t / C_bld)

Continuous-time exponential decay is exact for a constant ambient and avoids
dt-sensitivity over short horizons — no Euler stepping needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from math import exp
from typing import Any

from ..config import config

logger = logging.getLogger(__name__)


@dataclass
class EstimatedState:
    tank_temp_c: float
    indoor_temp_c: float
    outdoor_temp_c: float | None
    source: str
    seed_age_seconds: float


def _c_tank_j_per_k() -> float:
    return float(config.DHW_TANK_LITRES) * float(config.DHW_WATER_CP)


def _c_bld_j_per_k() -> float:
    return float(config.BUILDING_THERMAL_MASS_KWH_PER_K) * 3.6e6


def _mean_outdoor(
    meteo_rows: list[dict[str, Any]] | None, fallback: float | None
) -> float | None:
    if not meteo_rows:
        return fallback
    temps = [float(r["temp_c"]) for r in meteo_rows if r.get("temp_c") is not None]
    if not temps:
        return fallback
    return sum(temps) / len(temps)


def estimate_state(
    last_live: dict[str, Any],
    now_utc: datetime,
    *,
    meteo_rows: list[dict[str, Any]] | None = None,
    default_outdoor_c: float | None = None,
) -> EstimatedState:
    """Return an ``EstimatedState`` walked forward from *last_live* to *now_utc*.

    ``last_live`` must carry at minimum ``fetched_at`` (epoch seconds) and one
    of ``tank_temp_c`` / ``indoor_temp_c`` — missing fields fall back to
    ``config.DHW_TEMP_NORMAL_C`` / ``config.INDOOR_SETPOINT_C`` so the walk
    always produces a plausible seed instead of raising.

    ``meteo_rows`` — optional list of ``{'temp_c': float, ...}`` dicts (from
    ``db.get_meteo_forecast``); the mean outdoor over the horizon is used for
    the indoor decay target. ``default_outdoor_c`` is the fallback when meteo
    data is absent (caller can pass the last known outdoor from the seed row).
    """
    fetched_at = float(last_live["fetched_at"])
    seed_age = max(0.0, now_utc.timestamp() - fetched_at)

    tank0 = last_live.get("tank_temp_c")
    if tank0 is None:
        tank0 = float(config.DHW_TEMP_NORMAL_C)
    else:
        tank0 = float(tank0)

    indoor0 = last_live.get("indoor_temp_c")
    if indoor0 is None:
        indoor0 = float(config.INDOOR_SETPOINT_C)
    else:
        indoor0 = float(indoor0)

    outdoor_seed = last_live.get("outdoor_temp_c")
    if default_outdoor_c is None and outdoor_seed is not None:
        default_outdoor_c = float(outdoor_seed)
    outdoor = _mean_outdoor(meteo_rows, fallback=default_outdoor_c)

    ua_tank = float(config.DHW_TANK_UA_W_PER_K)
    ua_bld = float(config.BUILDING_UA_W_PER_K)
    c_tank = _c_tank_j_per_k()
    c_bld = _c_bld_j_per_k()

    k_tank = ua_tank / c_tank
    k_bld = ua_bld / c_bld

    tank_final = indoor0 + (tank0 - indoor0) * exp(-k_tank * seed_age)
    if outdoor is not None:
        indoor_final = outdoor + (indoor0 - outdoor) * exp(-k_bld * seed_age)
    else:
        # No outdoor data at all — assume indoor temp holds (conservative;
        # LP will request a touch more heat than strictly needed).
        indoor_final = indoor0

    return EstimatedState(
        tank_temp_c=round(tank_final, 2),
        indoor_temp_c=round(indoor_final, 2),
        outdoor_temp_c=round(outdoor, 1) if outdoor is not None else None,
        source="estimate",
        seed_age_seconds=seed_age,
    )
