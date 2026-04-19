"""PuLP MILP home energy optimizer (V9): battery, grid, PV, DHW tank, space heating.

State-of-the-art features vs V8:
  - HiGHS solver (≫ CBC in speed and solution quality; falls back to CBC if unavailable)
  - Simplified HP model: 1 binary hp_on[i] + continuous e_hp[i] in [0, max_hp_kw×0.5]
    instead of 4-bucket SOS1 → fewer binaries, tighter LP relaxation, faster solve
  - Minimum ON-time constraint for HP (anti short-cycling)
  - Piecewise-linear inverter-stress cost on battery power (approximates quadratic penalty)
    → discourages "bang-bang" at max power even when marginal cost is identical
  - Terminal SoC hard constraint (LP_SOC_FINAL_KWH)
  - Per-hour-of-day base-load accepted directly (caller already provides it)
  - TV penalties, price quantization, cycle penalty retained

Pure model — no I/O. Call :func:`solve_lp` with rates, weather series, and initial state.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pulp
from zoneinfo import ZoneInfo

from ..config import config
from ..weather import WeatherLpSeries

logger = logging.getLogger(__name__)


@dataclass
class LpInitialState:
    """Physical state at the start of slot 0."""

    soc_kwh: float
    tank_temp_c: float
    indoor_temp_c: float


@dataclass
class LpPlan:
    """Feasible MILP solution (per half-hour slot)."""

    ok: bool
    status: str
    objective_pence: float
    slot_starts_utc: list[datetime] = field(default_factory=list)
    price_pence: list[float] = field(default_factory=list)
    import_kwh: list[float] = field(default_factory=list)
    export_kwh: list[float] = field(default_factory=list)
    battery_charge_kwh: list[float] = field(default_factory=list)
    battery_discharge_kwh: list[float] = field(default_factory=list)
    pv_use_kwh: list[float] = field(default_factory=list)
    pv_curtail_kwh: list[float] = field(default_factory=list)
    dhw_electric_kwh: list[float] = field(default_factory=list)
    space_electric_kwh: list[float] = field(default_factory=list)
    tank_temp_c: list[float] = field(default_factory=list)   # len N+1
    indoor_temp_c: list[float] = field(default_factory=list)  # len N+1
    soc_kwh: list[float] = field(default_factory=list)       # len N+1
    temp_outdoor_c: list[float] = field(default_factory=list)
    peak_threshold_pence: float = 0.0
    cheap_threshold_pence: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_hhmm_to_minutes(s: str) -> int:
    parts = (s or "00:00").strip().split(":")
    h = int(parts[0]) if parts else 0
    m = int(parts[1]) if len(parts) > 1 else 0
    return h * 60 + m


def _slot_occupancy_bounds(
    slot_start_utc: datetime,
    tz: ZoneInfo,
) -> tuple[float, float]:
    """Return (T_in_min, T_in_max) comfort bounds for this slot's *end* state."""
    local = slot_start_utc.astimezone(tz)
    minutes = local.hour * 60 + local.minute
    ms = _parse_hhmm_to_minutes(config.LP_OCCUPIED_MORNING_START)
    me = _parse_hhmm_to_minutes(config.LP_OCCUPIED_MORNING_END)
    es = _parse_hhmm_to_minutes(config.LP_OCCUPIED_EVENING_START)
    ee = _parse_hhmm_to_minutes(config.LP_OCCUPIED_EVENING_END)
    sp = float(config.INDOOR_SETPOINT_C)
    band = float(config.INDOOR_COMFORT_BAND_C)
    if ms <= minutes < me or es <= minutes < ee:
        return sp - band, sp + band
    return 16.0, 24.0


def _shower_slot_mask(
    slot_starts_utc: list[datetime],
    tz: ZoneInfo,
    *,
    shower_hhmm: str,
    window_minutes: int,
) -> list[bool]:
    """True when slot midpoint is inside the shower window (local)."""
    sh, sm = shower_hhmm.split(":")
    st_shower = int(sh) * 60 + int(sm)
    half = window_minutes / 2.0
    out: list[bool] = []
    for st in slot_starts_utc:
        mid = st + timedelta(minutes=15)
        mloc = mid.astimezone(tz).hour * 60 + mid.astimezone(tz).minute
        d = abs(mloc - st_shower)
        d = min(d, 24 * 60 - d)
        out.append(d <= half)
    return out


def _legionella_slot_mask(slot_starts_utc: list[datetime], tz: ZoneInfo) -> list[bool]:
    out: list[bool] = []
    for st in slot_starts_utc:
        loc = (st + timedelta(minutes=15)).astimezone(tz)
        if loc.weekday() != int(config.DHW_LEGIONELLA_DAY):
            out.append(False)
            continue
        out.append(
            int(config.DHW_LEGIONELLA_HOUR_START) <= loc.hour < int(config.DHW_LEGIONELLA_HOUR_END)
        )
    return out


def _make_solver() -> pulp.LpSolver:
    """Return HiGHS (Python API) if available, else CBC. Configured via LP_SOLVER env var."""
    solver_pref = (getattr(config, "LP_SOLVER", "highs") or "highs").lower()
    time_limit = getattr(config, "LP_HIGHS_TIME_LIMIT_SECONDS", 30)
    cbc_limit = getattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 30)

    available = pulp.listSolvers(onlyAvailable=True)

    if solver_pref != "cbc" and "HiGHS" in available:
        logger.debug("LP solver: HiGHS Python API (time_limit=%ds)", time_limit)
        return pulp.HiGHS(
            msg=False,
            timeLimit=int(time_limit),
            threads=0,  # 0 = auto (use all available cores)
        )

    logger.debug("LP solver: CBC (time_limit=%ds)", cbc_limit)
    return pulp.PULP_CBC_CMD(msg=False, timeLimit=int(cbc_limit))


def _piecewise_stress_breakpoints(max_power: float, n_seg: int) -> list[tuple[float, float]]:
    """
    Piecewise-linear approximation of f(p) = (p / max_power)^2 * cost_per_kwh.

    Returns list of (power_kwh, cost_per_kwh) breakpoints for the upper envelope.
    The LP adds auxiliary variables to enforce the piecewise constraints.
    """
    pts: list[tuple[float, float]] = []
    for k in range(n_seg + 1):
        p = max_power * k / n_seg
        c = (p / max_power) ** 2  # normalised; caller scales by stress_cost
        pts.append((p, c))
    return pts


# ---------------------------------------------------------------------------
# Main solver
# ---------------------------------------------------------------------------

def solve_lp(
    *,
    slot_starts_utc: list[datetime],
    price_pence: list[float],
    base_load_kwh: list[float],
    weather: WeatherLpSeries,
    initial: LpInitialState,
    tz: ZoneInfo,
) -> LpPlan:
    """Build and solve the MILP. Raises ``ValueError`` on dimension mismatch."""
    n = len(slot_starts_utc)
    if n == 0:
        raise ValueError("LP: empty horizon")
    if len(price_pence) != n or len(base_load_kwh) != n:
        raise ValueError("LP: price/base_load length mismatch")
    if len(weather.pv_kwh_per_slot) != n:
        raise ValueError("LP: weather horizon mismatch")

    pv_avail = list(weather.pv_kwh_per_slot)
    t_out = list(weather.temperature_outdoor_c)
    cop_dhw = list(weather.cop_dhw)
    cop_space = list(weather.cop_space)

    # Price quantization (reduces solver sensitivity to tiny rate differences)
    qp = float(config.LP_PRICE_QUANTIZE_PENCE)
    price_line = (
        [round(float(p) / qp) * qp for p in price_pence] if qp > 0 else list(price_pence)
    )
    sorted_p = sorted(price_line)
    cheap_thr = sorted_p[max(0, n // 4 - 1)] if n else 0.0
    peak_thr = sorted_p[min(n - 1, (3 * n) // 4)] if n else 0.0

    # Physical constants
    dt_s = 1800.0
    slot_h = 0.5
    eta = float(config.BATTERY_RT_EFFICIENCY)
    sqrt_eta = math.sqrt(max(0.01, min(1.0, eta)))
    c_tank = float(config.DHW_TANK_LITRES) * float(config.DHW_WATER_CP)  # J/K
    ua_tank = float(config.DHW_TANK_UA_W_PER_K)
    ua_bld = float(config.BUILDING_UA_W_PER_K)
    c_bld = float(config.BUILDING_THERMAL_MASS_KWH_PER_K) * 3.6e6  # J/K
    j_per_kwh = 3.6e6
    q_int_j = 0.1 * j_per_kwh
    sg = float(config.SOLAR_GAIN_FRACTION)

    fuse_kwh = 5.0            # max grid import per slot (10 kW)
    export_cap_kwh = 3.0      # 6 kW export cap
    max_inv_kw = float(config.MAX_INVERTER_KW)
    max_batt_kwh = max_inv_kw * slot_h
    soc_min = float(config.BATTERY_CAPACITY_KWH) * float(config.MIN_SOC_RESERVE_PERCENT) / 100.0
    soc_max = float(config.BATTERY_CAPACITY_KWH)
    tank_lo = 20.0
    tank_hi = float(config.DHW_TEMP_MAX_C)
    t_min_dhw = float(config.TARGET_DHW_TEMP_MIN_NORMAL_C)
    t_leg = float(config.DHW_LEGIONELLA_TEMP_C)

    # Simplified HP model: max power from config, continuous between [0, max_hp_kwh]
    hp_max_kw = float(getattr(config, "DAIKIN_MAX_HP_KW", 2.0))
    max_hp_kwh = hp_max_kw * slot_h  # max kWh per slot

    # Minimum HP ON duration (anti short-cycling)
    hp_min_on = int(getattr(config, "LP_HP_MIN_ON_SLOTS", 2))

    # Terminal SoC constraint
    soc_final_kwh = float(getattr(config, "LP_SOC_FINAL_KWH", 0.0))

    # Inverter stress cost (piecewise-linear quadratic approx)
    stress_cost = float(getattr(config, "LP_INVERTER_STRESS_COST_PENCE", 0.10))
    n_stress_seg = max(2, int(getattr(config, "LP_INVERTER_STRESS_SEGMENTS", 8)))
    use_stress = stress_cost > 0

    # -----------------------------------------------------------------------
    # Decision variables
    # -----------------------------------------------------------------------
    prob = pulp.LpProblem("HomeEnergy_V9", pulp.LpMinimize)

    imp = pulp.LpVariable.dicts("grid_import", range(n), lowBound=0, upBound=fuse_kwh)
    exp = pulp.LpVariable.dicts("grid_export", range(n), lowBound=0, upBound=export_cap_kwh)
    chg = pulp.LpVariable.dicts("bat_charge", range(n), lowBound=0, upBound=max_batt_kwh)
    dis = pulp.LpVariable.dicts("bat_discharge", range(n), lowBound=0, upBound=max_batt_kwh)
    pv_use = pulp.LpVariable.dicts("pv_use", range(n), lowBound=0)
    pv_curt = pulp.LpVariable.dicts("pv_curtail", range(n), lowBound=0)

    # HP: 1 binary (on/off) + continuous power — simpler, tighter LP relaxation
    hp_on = pulp.LpVariable.dicts("hp_on", range(n), cat="Binary")
    e_dhw = pulp.LpVariable.dicts("dhw_kwh", range(n), lowBound=0, upBound=max_hp_kwh)
    e_space = pulp.LpVariable.dicts("space_kwh", range(n), lowBound=0, upBound=max_hp_kwh)
    # DHW/space mode selection (still mutually exclusive)
    m_dhw = pulp.LpVariable.dicts("mode_dhw", range(n), cat="Binary")
    m_space = pulp.LpVariable.dicts("mode_space", range(n), cat="Binary")

    a_grid = pulp.LpVariable.dicts("grid_import_mode", range(n), cat="Binary")
    b_bat = pulp.LpVariable.dicts("bat_charge_mode", range(n), cat="Binary")

    soc = pulp.LpVariable.dicts("soc", range(n + 1), lowBound=soc_min, upBound=soc_max)
    tank = pulp.LpVariable.dicts("tank", range(n + 1), lowBound=tank_lo, upBound=tank_hi)
    t_in = pulp.LpVariable.dicts("indoor", range(n + 1), lowBound=10.0, upBound=28.0)

    s_lo = pulp.LpVariable.dicts("comfort_slack_lo", range(n), lowBound=0)
    s_hi = pulp.LpVariable.dicts("comfort_slack_hi", range(n), lowBound=0)

    # Piecewise stress auxiliary variables (one per battery power slot)
    stress_aux: dict[int, pulp.LpVariable] = {}
    if use_stress:
        stress_aux = pulp.LpVariable.dicts("bat_stress", range(n), lowBound=0)

    # -----------------------------------------------------------------------
    # Initial conditions
    # -----------------------------------------------------------------------
    prob += soc[0] == initial.soc_kwh
    prob += tank[0] == initial.tank_temp_c
    prob += t_in[0] == initial.indoor_temp_c

    # -----------------------------------------------------------------------
    # Per-slot constraints
    # -----------------------------------------------------------------------
    cycle_pen = float(config.LP_CYCLE_PENALTY_PENCE_PER_KWH)
    comfort_pen = float(config.LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT)
    export_rate = float(config.EXPORT_RATE_PENCE)

    for i in range(n):
        e_hp_i = e_dhw[i] + e_space[i]

        # PV split
        prob += pv_use[i] + pv_curt[i] == pv_avail[i]

        # Energy balance
        prob += imp[i] + pv_use[i] + dis[i] == base_load_kwh[i] + exp[i] + chg[i] + e_hp_i

        # Export only from PV or battery
        prob += exp[i] <= pv_use[i] + dis[i]

        # Battery SoC dynamics
        prob += soc[i + 1] == soc[i] + chg[i] * sqrt_eta - dis[i] / sqrt_eta

        # Import/export mutual exclusion
        prob += imp[i] <= fuse_kwh * a_grid[i]
        prob += exp[i] <= export_cap_kwh * (1 - a_grid[i])

        # Charge/discharge mutual exclusion
        prob += chg[i] <= max_batt_kwh * b_bat[i]
        prob += dis[i] <= max_batt_kwh * (1 - b_bat[i])

        # HP: continuous power bounded by on/off binary
        prob += e_dhw[i] + e_space[i] <= max_hp_kwh * hp_on[i]
        prob += e_dhw[i] + e_space[i] >= 0  # (implicit from lower bounds)
        prob += m_dhw[i] + m_space[i] <= 1
        prob += m_dhw[i] + m_space[i] >= hp_on[i]  # must pick a mode when on
        # Each mode bounds its share
        prob += e_dhw[i] <= max_hp_kwh * m_dhw[i]
        prob += e_space[i] <= max_hp_kwh * m_space[i]
        # When HP is off, both e_dhw and e_space are 0 (via hp_on bound above +
        # mode bounds below; hp_on=0 → m_dhw+m_space≤1 and ≥0 still allows a
        # mode flag=1 with zero power, so add explicit binding)
        prob += e_dhw[i] + e_space[i] >= 0  # already set; kept for clarity

        # DHW tank thermodynamics
        q_heat_dhw = e_dhw[i] * cop_dhw[i] * j_per_kwh
        loss_tank_j = ua_tank * (tank[i] - t_in[i]) * dt_s
        prob += tank[i + 1] == tank[i] + (q_heat_dhw - loss_tank_j) / c_tank

        # Building thermodynamics
        q_heat_space = e_space[i] * cop_space[i] * j_per_kwh
        loss_bld_j = ua_bld * (t_in[i] - t_out[i]) * dt_s
        q_sol_j = sg * pv_avail[i] * j_per_kwh
        prob += t_in[i + 1] == t_in[i] + (q_heat_space - loss_bld_j + q_sol_j + q_int_j) / c_bld

        # Radiator output cap
        prob += e_space[i] * cop_space[i] <= float(config.RADIATOR_MAX_KW) * slot_h

        # Comfort soft constraints
        t_end_lo, t_end_hi = _slot_occupancy_bounds(slot_starts_utc[i], tz)
        prob += t_in[i + 1] >= t_end_lo - s_lo[i]
        prob += t_in[i + 1] <= t_end_hi + s_hi[i]

        # Piecewise-linear inverter stress cost
        if use_stress:
            # stress_aux[i] approximates (bat_power / max_inv_kw)^2 * max_inv_kw
            # using the upper piecewise-linear envelope; we penalise total kW throughput
            bat_power_kwh = chg[i] + dis[i]
            # Upper piecewise envelope: stress_aux[i] ≥ slope_k * bat_power_kwh + intercept_k
            bpts = _piecewise_stress_breakpoints(max_batt_kwh, n_stress_seg)
            for k in range(1, len(bpts)):
                p0, c0 = bpts[k - 1]
                p1, c1 = bpts[k]
                if abs(p1 - p0) < 1e-9:
                    continue
                slope = (c1 - c0) / (p1 - p0)
                intercept = c0 - slope * p0
                # stress_cost scales the unit-normalised penalty
                prob += stress_aux[i] >= stress_cost * (slope * bat_power_kwh + intercept)

    # HP minimum on-time (anti short-cycling)
    # When hp_on switches from 0→1, it must stay on for at least hp_min_on consecutive slots.
    if hp_min_on > 1 and n >= hp_min_on:
        for i in range(n - hp_min_on + 1):
            # y_i - y_{i-1} = startup event; constrain sum of next min_on to >= min_on * startup
            # Simplified: if hp_on[i]=1 and hp_on[i-1]=0 (startup), force sum >= min_on
            # Since (hp_on[i] - prev) can be negative (shutdown), use max(0,.) → auxiliary
            startup_i = pulp.LpVariable(f"hp_startup_{i}", cat="Binary")
            prev = hp_on[i - 1] if i > 0 else pulp.LpVariable(f"hp_on_neg1_dummy", lowBound=0, upBound=0)
            prob += startup_i >= hp_on[i] - prev
            prob += (
                pulp.lpSum(hp_on[j] for j in range(i, min(i + hp_min_on, n)))
                >= hp_min_on * startup_i
            )

    # -----------------------------------------------------------------------
    # DHW hard constraints (showers + legionella)
    # -----------------------------------------------------------------------
    wm = _shower_slot_mask(
        slot_starts_utc, tz,
        shower_hhmm=config.LP_SHOWER_MORNING_LOCAL,
        window_minutes=int(config.LP_SHOWER_WINDOW_MINUTES),
    )
    we = _shower_slot_mask(
        slot_starts_utc, tz,
        shower_hhmm=config.LP_SHOWER_EVENING_LOCAL,
        window_minutes=int(config.LP_SHOWER_WINDOW_MINUTES),
    )
    leg_m = _legionella_slot_mask(slot_starts_utc, tz)
    for i in range(n):
        if wm[i] or we[i]:
            prob += tank[i + 1] >= t_min_dhw
        if leg_m[i]:
            prob += tank[i + 1] >= t_leg

    # -----------------------------------------------------------------------
    # Terminal constraints
    # -----------------------------------------------------------------------
    # SoC: hard floor if configured, else soft (soc ≥ initial)
    if soc_final_kwh > soc_min:
        prob += soc[n] >= min(soc_final_kwh, soc_max)
    else:
        prob += soc[n] >= initial.soc_kwh

    prob += tank[n] >= t_min_dhw - 2.0
    prob += t_in[n] >= float(config.INDOOR_SETPOINT_C) - 0.5

    # -----------------------------------------------------------------------
    # Total Variation penalties
    # -----------------------------------------------------------------------
    w_bat_tv = float(config.LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA)
    w_hp_tv = float(config.LP_HP_POWER_TV_PENALTY_PENCE_PER_KWH_DELTA)
    w_imp_tv = float(config.LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA)
    tv_chg: dict[int, pulp.LpVariable] = {}
    tv_dis: dict[int, pulp.LpVariable] = {}
    tv_hp: dict[int, pulp.LpVariable] = {}
    tv_imp: dict[int, pulp.LpVariable] = {}
    if n >= 2:
        if w_bat_tv > 0:
            for i in range(1, n):
                tv_chg[i] = pulp.LpVariable(f"bat_chg_tv_{i}", lowBound=0)
                tv_dis[i] = pulp.LpVariable(f"bat_dis_tv_{i}", lowBound=0)
                prob += tv_chg[i] >= chg[i] - chg[i - 1]
                prob += tv_chg[i] >= chg[i - 1] - chg[i]
                prob += tv_dis[i] >= dis[i] - dis[i - 1]
                prob += tv_dis[i] >= dis[i - 1] - dis[i]
        if w_hp_tv > 0:
            for i in range(1, n):
                tv_hp[i] = pulp.LpVariable(f"hp_tv_{i}", lowBound=0)
                prob += tv_hp[i] >= (e_dhw[i] + e_space[i]) - (e_dhw[i - 1] + e_space[i - 1])
                prob += tv_hp[i] >= (e_dhw[i - 1] + e_space[i - 1]) - (e_dhw[i] + e_space[i])
        if w_imp_tv > 0:
            for i in range(1, n):
                tv_imp[i] = pulp.LpVariable(f"imp_tv_{i}", lowBound=0)
                prob += tv_imp[i] >= imp[i] - imp[i - 1]
                prob += tv_imp[i] >= imp[i - 1] - imp[i]

    # -----------------------------------------------------------------------
    # Objective
    # -----------------------------------------------------------------------
    obj_grid = pulp.lpSum(imp[i] * price_line[i] - exp[i] * export_rate for i in range(n))
    obj_cycle = cycle_pen * pulp.lpSum(chg[i] + dis[i] for i in range(n))
    obj_comfort = comfort_pen * pulp.lpSum(s_lo[i] + s_hi[i] for i in range(n))
    objective = obj_grid + obj_cycle + obj_comfort

    if use_stress and stress_aux:
        objective += pulp.lpSum(stress_aux[i] * slot_h for i in range(n))

    if w_bat_tv > 0 and tv_chg:
        objective += w_bat_tv * (
            pulp.lpSum(tv_chg[i] for i in tv_chg)
            + pulp.lpSum(tv_dis[i] for i in tv_dis)
        )
    if w_hp_tv > 0 and tv_hp:
        objective += w_hp_tv * pulp.lpSum(tv_hp[i] for i in tv_hp)
    if w_imp_tv > 0 and tv_imp:
        objective += w_imp_tv * pulp.lpSum(tv_imp[i] for i in tv_imp)

    prob += objective

    # -----------------------------------------------------------------------
    # Solve
    # -----------------------------------------------------------------------
    solver = _make_solver()
    prob.solve(solver)

    status = pulp.LpStatus[prob.status]
    plan = LpPlan(
        ok=status == "Optimal",
        status=status,
        objective_pence=0.0,
        peak_threshold_pence=peak_thr,
        cheap_threshold_pence=cheap_thr,
    )
    plan.slot_starts_utc = list(slot_starts_utc)
    plan.price_pence = list(price_pence)
    plan.temp_outdoor_c = t_out

    if status != "Optimal":
        logger.warning("LP solver returned %s", status)
        return plan

    def _v(x: Any) -> float:
        v = pulp.value(x)
        return float(v) if v is not None else 0.0

    plan.objective_pence = float(pulp.value(prob.objective) or 0.0)
    for i in range(n):
        plan.import_kwh.append(_v(imp[i]))
        plan.export_kwh.append(_v(exp[i]))
        plan.battery_charge_kwh.append(_v(chg[i]))
        plan.battery_discharge_kwh.append(_v(dis[i]))
        plan.pv_use_kwh.append(_v(pv_use[i]))
        plan.pv_curtail_kwh.append(_v(pv_curt[i]))
        plan.dhw_electric_kwh.append(_v(e_dhw[i]))
        plan.space_electric_kwh.append(_v(e_space[i]))
    for i in range(n + 1):
        plan.soc_kwh.append(_v(soc[i]))
        plan.tank_temp_c.append(_v(tank[i]))
        plan.indoor_temp_c.append(_v(t_in[i]))

    return plan
