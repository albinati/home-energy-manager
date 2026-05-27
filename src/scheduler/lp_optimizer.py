"""PuLP MILP home energy optimizer (V9): battery, grid, PV, DHW tank, space heating.

State-of-the-art features vs V8:
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
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pulp

from ..config import config, cop_at_temperature
from ..physics import (
    apply_cop_lift_multiplier,
    get_daikin_heating_kw,
    get_lwt_base_c,
    lwt_offset_from_space_kw,
    predict_passive_daikin_load,
)
from ..weather import WeatherLpSeries
from .pv_trust import PvSufficiencyGuardDiag, evaluate_pv_sufficiency_guard

logger = logging.getLogger(__name__)


@dataclass
class LpInitialState:
    """Physical state at the start of slot 0.

    PR Phase B (#306 follow-up): ``indoor_temp_c`` was removed because the
    Daikin Altherma exposes no room sensor (0% coverage in heartbeat) and the
    LP's old comfort-band variable was modelling fiction. Space-heating demand
    is now driven by ``get_daikin_heating_kw(t_outdoor)`` directly — the
    physics floor + ceiling derived from the configured Daikin weather curve.
    Tank thermal loss uses ``INDOOR_SETPOINT_C`` as a constant ambient.
    """

    soc_kwh: float
    tank_temp_c: float
    # Provenance strings ("fox_realtime_cache", "db_realtime_snapshot",
    # "daikin_live", "daikin_cache", "daikin_estimate", "execution_log",
    # "default"). Persisted to lp_inputs_snapshot so the History view can show
    # "SoC came from Fox live cache (1m ago)" etc. Defaults preserve
    # backwards-compat for callers that construct LpInitialState directly.
    soc_source: str = "unknown"
    tank_source: str = "unknown"


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
    lwt_offset_c: list[float] = field(default_factory=list)  # back-computed per slot
    tank_temp_c: list[float] = field(default_factory=list)   # len N+1
    soc_kwh: list[float] = field(default_factory=list)       # len N+1
    temp_outdoor_c: list[float] = field(default_factory=list)
    peak_threshold_pence: float = 0.0
    cheap_threshold_pence: float = 0.0
    pv_sufficiency_guard: PvSufficiencyGuardDiag | None = None
    """Audit data for the strict_savings PV-sufficiency guard rail. ``None``
    when the rail was not evaluated (legacy callers / pre-#incident-2026-05-15
    snapshots)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# PR Phase B: ``_slot_occupancy_bounds`` deleted — comfort-band logic depended
# on the indoor_temp state variable, which was based on a non-existent room
# sensor. Heating demand is now driven by the outdoor-temp physics floor +
# LWT-offset choice variable; comfort is the firmware's job under its weather
# curve, not the LP's.


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


def _parse_shower_schedule(schedule: str) -> list[tuple[int, int]]:
    """Parse a ``DHW_SHOWER_SCHEDULE`` string ``"HH:MM-HH:MM,HH:MM-HH:MM"`` into
    a list of ``(start_minutes_local, end_minutes_local)`` pairs.

    Skips malformed entries silently — empty or invalid strings yield ``[]``.
    """
    out: list[tuple[int, int]] = []
    if not schedule:
        return out
    for part in schedule.split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        try:
            start_s, end_s = part.split("-", 1)
            sh, sm = start_s.strip().split(":")
            eh, em = end_s.strip().split(":")
            s_min = int(sh) * 60 + int(sm)
            e_min = int(eh) * 60 + int(em)
            if 0 <= s_min < 24 * 60 and 0 < e_min <= 24 * 60 and s_min < e_min:
                out.append((s_min, e_min))
        except (ValueError, AttributeError):
            continue
    return out


def _window_set_slot_mask(
    slot_starts_utc: list[datetime],
    tz: ZoneInfo,
    *,
    windows: list[tuple[int, int]],
) -> list[bool]:
    """Multi-window generalisation of :func:`_shower_slot_mask`.

    ``windows`` is a list of ``(start_minutes_local, end_minutes_local)`` pairs
    (e.g. ``[(19*60, 22*60)]`` for 19:00–22:00). True when slot midpoint (local)
    falls inside any window. Empty list → all-False mask.
    """
    if not windows:
        return [False] * len(slot_starts_utc)
    out: list[bool] = []
    for st in slot_starts_utc:
        mid_local = (st + timedelta(minutes=15)).astimezone(tz)
        m = mid_local.hour * 60 + mid_local.minute
        out.append(any(s <= m < e for s, e in windows))
    return out


def _resolve_active_shower_windows(guests_preset: bool) -> list[tuple[int, int]]:
    """Pick which shower-window list applies for this solve.

    Resolution order:
    1. ``DHW_SHOWER_SCHEDULE_GUESTS`` (if guests preset and value non-empty).
    2. ``DHW_SHOWER_SCHEDULE`` (if value non-empty).
    3. Backward-compat: derive from the legacy
       ``LP_SHOWER_MORNING_LOCAL``/``LP_SHOWER_EVENING_LOCAL`` scalars +
       ``LP_SHOWER_WINDOW_MINUTES``.

    Returns ``[]`` when no schedule is configured (LP applies no DHW floor —
    matches the prior code path with both LP_SHOWER_*_LOCAL empty).
    """
    if guests_preset:
        guests_str = (getattr(config, "DHW_SHOWER_SCHEDULE_GUESTS", "") or "").strip()
        if guests_str:
            return _parse_shower_schedule(guests_str)
    schedule_str = (getattr(config, "DHW_SHOWER_SCHEDULE", "") or "").strip()
    if schedule_str:
        return _parse_shower_schedule(schedule_str)
    # Back-compat: derive from the legacy scalar pair.
    half = int(config.LP_SHOWER_WINDOW_MINUTES) / 2
    out: list[tuple[int, int]] = []
    for hhmm_attr in ("LP_SHOWER_MORNING_LOCAL", "LP_SHOWER_EVENING_LOCAL"):
        hhmm = (getattr(config, hhmm_attr, "") or "").strip()
        if not hhmm:
            continue
        try:
            sh, sm = hhmm.split(":")
            centre = int(sh) * 60 + int(sm)
        except ValueError:
            continue
        s = max(0, int(centre - half))
        e = min(24 * 60, int(centre + half))
        if s < e:
            out.append((s, e))
    return out


def _make_solver() -> pulp.LpSolver:
    """Return the configured PuLP solver backend.

    We standardize on CBC. The HiGHS branch was removed: it was implemented
    but never used in this system, and caused native aborts in some test/
    runtime environments. ``LP_SOLVER`` is kept for forward-compat — any
    non-cbc value logs an info line and falls through to CBC.
    """
    solver_pref = (getattr(config, "LP_SOLVER", "cbc") or "cbc").lower()
    cbc_limit = getattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 30)
    if solver_pref != "cbc":
        logger.info(
            "LP_SOLVER=%s requested, but only CBC is supported; falling back to CBC",
            solver_pref,
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
    micro_climate_offset_c: float = 0.0,
    micro_climate_offset_by_hour_c: dict[int, float] | None = None,
    export_price_pence: list[float] | None = None,
) -> LpPlan:
    """Build and solve the MILP. Raises ``ValueError`` on dimension mismatch.

    ``micro_climate_offset_c`` (default 0.0) and the optional per-hour
    ``micro_climate_offset_by_hour_c`` map are interpreted as
    ``actual − forecast`` (matching ``db.get_micro_climate_offset_c`` /
    ``…_by_hour_c``). The calibrated outdoor temperature is therefore
    ``forecast + offset`` — a positive offset means the local microclimate
    runs warmer than forecast and the LP sees the warmer figure for both the
    heat-loss curve and the COP curve. Hour-specific entries (UTC hour key)
    take precedence over the flat default. Each offset is clamped to ±5 °C.

    ``export_price_pence`` (optional): half-hourly Octopus Outgoing Agile
    rates. When provided, the objective uses the per-slot value so the LP
    correctly weighs export revenue at the actual time-of-use rate. When
    ``None`` (no export tariff configured / not yet fetched) we fall back to
    the flat ``EXPORT_RATE_PENCE`` constant.
    """
    n = len(slot_starts_utc)
    if n == 0:
        raise ValueError("LP: empty horizon")
    if len(price_pence) != n or len(base_load_kwh) != n:
        raise ValueError("LP: price/base_load length mismatch")
    if export_price_pence is not None and len(export_price_pence) != n:
        raise ValueError("LP: export_price_pence length mismatch")
    if len(weather.pv_kwh_per_slot) != n:
        raise ValueError("LP: weather horizon mismatch")

    pv_avail = list(weather.pv_kwh_per_slot)
    # Apply micro-climate offset and recompute COPs from the calibrated
    # temperature. Earlier versions of this code subtracted the offset (which
    # inverted the sign convention) and left the COP arrays as the
    # pre-offset values from ``forecast_to_lp_inputs`` — so a negative
    # microclimate offset in winter actually pushed t_out *higher* and the
    # COP curve was evaluated against the un-calibrated forecast. The
    # combined effect was a systematic morning over-heat / afternoon
    # under-heat bias. Adding the offset and recomputing the COPs in lockstep
    # keeps t_out, the heat-loss curve, and the COP curve internally
    # consistent.
    offset_by_hour = micro_climate_offset_by_hour_c or {}
    offset_default = float(micro_climate_offset_c or 0.0)
    curve = config.DAIKIN_COP_CURVE
    dhw_pen = float(config.COP_DHW_PENALTY)
    lift_pen = float(getattr(config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.0))
    lwt_off_max_for_cop = float(getattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0))
    lwt_ceiling = float(getattr(config, "LP_COP_SPACE_LWT_CEILING_C", 50.0))
    lwt_dhw = float(getattr(config, "LP_COP_DHW_LIFT_SUPPLY_C", 45.0))
    ref_k = float(getattr(config, "LP_COP_LIFT_REFERENCE_DELTA_K", 25.0))
    min_m = float(getattr(config, "LP_COP_LIFT_MIN_MULTIPLIER", 0.5))
    t_out: list[float] = []
    cop_space: list[float] = []
    cop_dhw: list[float] = []
    for i, raw_temp in enumerate(weather.temperature_outdoor_c):
        slot_hour = slot_starts_utc[i].hour if i < len(slot_starts_utc) else i % 24
        offset = float(offset_by_hour.get(slot_hour, offset_default))
        offset = max(-5.0, min(5.0, offset))
        temp_c = float(raw_temp) + offset
        t_out.append(temp_c)
        base_cop = max(1.0, cop_at_temperature(curve, temp_c))
        if lift_pen > 0.0:
            lwt_space = min(lwt_ceiling, get_lwt_base_c(temp_c) + lwt_off_max_for_cop)
            cop_s = apply_cop_lift_multiplier(
                base_cop,
                temp_c,
                lwt_space,
                penalty_per_k=lift_pen,
                reference_delta_k=ref_k,
                min_mult=min_m,
            )
            cop_d = max(
                1.0,
                apply_cop_lift_multiplier(
                    max(1.0, base_cop - dhw_pen),
                    temp_c,
                    lwt_dhw,
                    penalty_per_k=lift_pen,
                    reference_delta_k=ref_k,
                    min_mult=min_m,
                ),
            )
        else:
            cop_s = base_cop
            cop_d = max(1.0, cop_s - dhw_pen)
        cop_space.append(cop_s)
        cop_dhw.append(cop_d)

    slot_h = 0.5  # 30-minute slots
    max_hp_kwh_per_slot = float(getattr(config, "DAIKIN_MAX_HP_KW", 2.0)) * slot_h
    lwt_offset_max = float(getattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0))

    # Per-slot physics-consistent bounds for e_space from the climate curve.
    # floor: compressor draw at zero offset (natural curve point).
    # ceiling: maximum achievable draw with LWT_OFFSET_MAX applied, capped at 50 °C LWT.
    # These replace the fictional fixed max_hp_kwh upper bound on e_space.
    space_floor_kwh = [
        min(get_daikin_heating_kw(t) * slot_h, max_hp_kwh_per_slot)
        for t in t_out
    ]
    space_ceil_kwh = [
        min(get_daikin_heating_kw(t, lwt_offset_delta=lwt_offset_max) * slot_h, max_hp_kwh_per_slot)
        for t in t_out
    ]

    # Price quantization (reduces solver sensitivity to tiny rate differences).
    # Conservative rounding: negatives → floor (more negative, so the LP plans
    # AS IF the slot is even cheaper than it actually is — won't import less
    # than realised); positives → ceil (more expensive, won't pay more than
    # planned). The previous symmetric ``round(...)`` could collapse small
    # negatives like -0.2p to 0p, removing the import incentive (audit #5).
    qp = float(config.LP_PRICE_QUANTIZE_PENCE)
    if qp > 0:
        price_line = [
            (math.floor(float(p) / qp) * qp) if float(p) < 0 else (math.ceil(float(p) / qp) * qp)
            for p in price_pence
        ]
    else:
        price_line = list(price_pence)
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
    j_per_kwh = 3.6e6
    # PR Phase B: ua_bld / c_bld / q_int_j / sg removed — building thermal
    # dynamics no longer modelled in the LP (no indoor temp state variable).

    # Grid import cap per slot, derived from the inverter's AC import rating
    # (FoxESS app: "max charge from grid"). 10500 W → 5.25 kWh per 30 min slot.
    fuse_kwh = float(config.FOX_FORCE_CHARGE_MAX_PWR) / 2_000.0
    # G98 single-phase export cap = 16 A × 230 V ≈ 3.68 kW → 1.84 kWh per 30 min slot.
    # Matches FOX_EXPORT_MAX_PWR; raise if on G99 or G98 multi-phase.
    export_cap_kwh = float(config.FOX_EXPORT_MAX_PWR) / 2_000.0
    max_inv_kw = float(config.MAX_INVERTER_KW)
    max_batt_kwh = max_inv_kw * slot_h
    soc_min = float(config.BATTERY_CAPACITY_KWH) * float(config.MIN_SOC_RESERVE_PERCENT) / 100.0
    soc_max = float(config.BATTERY_CAPACITY_KWH)
    tank_lo = 20.0
    tank_hi = float(config.DHW_TEMP_MAX_C)
    try:
        from ..presets import OperationPreset
        _preset = OperationPreset(config.OPTIMIZATION_PRESET)
        t_min_dhw = float(
            config.TARGET_DHW_TEMP_MIN_GUESTS_C
            if _preset == OperationPreset.GUESTS
            else config.TARGET_DHW_TEMP_MIN_NORMAL_C
        )
    except (ValueError, AttributeError):
        t_min_dhw = float(config.TARGET_DHW_TEMP_MIN_NORMAL_C)

    # Simplified HP model: max power from config, continuous between [0, max_hp_kwh]
    hp_max_kw = float(getattr(config, "DAIKIN_MAX_HP_KW", 2.0))
    max_hp_kwh = hp_max_kw * slot_h  # max kWh per slot

    # Passive mode (def1): the LP no longer *decides* Daikin energy — the firmware
    # is autonomous. We predict the per-slot autonomous draw and clamp e_dhw and
    # e_space to that vector below, so the LP correctly attributes the thermal
    # load when allocating PV/battery/grid. Active mode keeps v9 free variables.
    passive_daikin = config.DAIKIN_CONTROL_MODE == "passive"
    if passive_daikin:
        passive_e_space, passive_e_dhw = predict_passive_daikin_load(
            t_out, cop_dhw, cop_space,
            slot_h=slot_h,
            max_kwh_per_slot=max_hp_kwh,
            slot_starts_utc=slot_starts_utc,
            tz=tz,
        )
    else:
        passive_e_space = passive_e_dhw = []

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

    # HP: 1 binary (on/off) + continuous power — simpler, tighter LP relaxation.
    # Audit 2026-05-19: dropped the per-mode binaries (m_dhw, m_space) and the
    # ``m_dhw + m_space <= 1`` mutex they enforced. The Daikin Altherma
    # firmware interleaves DHW and space heating within a 30-min slot
    # (e.g. 10 min DHW lift then 20 min radiator), so the strict
    # single-mode-per-slot model misrepresented the hardware and stacked
    # with the shower-floor + space-floor + tank-hi constraints to push the
    # LP infeasible under tight conditions. Aggregate cap ``e_dhw + e_space
    # <= max_hp_kwh * hp_on`` still enforces total electrical draw; per-mode
    # physics caps (``e_space <= space_ceil_kwh``) are applied directly below.
    hp_on = pulp.LpVariable.dicts("hp_on", range(n), cat="Binary")
    e_dhw = pulp.LpVariable.dicts("dhw_kwh", range(n), lowBound=0, upBound=max_hp_kwh)
    e_space = pulp.LpVariable.dicts("space_kwh", range(n), lowBound=0, upBound=max_hp_kwh)

    a_grid = pulp.LpVariable.dicts("grid_import_mode", range(n), cat="Binary")
    b_bat = pulp.LpVariable.dicts("bat_charge_mode", range(n), cat="Binary")

    # Forward slots ``soc[1..n]`` keep the operational reserve as a hard
    # lower bound (``soc_min``). Slot 0 is relaxed below to ``[0, soc_max]``
    # so the hard equality ``soc[0] == initial.soc_kwh`` (line 491) remains
    # satisfiable when realtime SoC has slipped below the operational
    # reserve. Previously a single ``lowBound=soc_min`` for ALL slots —
    # combined with the hard equality — made every solve Infeasible whenever
    # realtime SoC dipped below reserve, which then fell back to the
    # heuristic that destructively grid-overcharged the battery (see
    # [[project_heuristic_fox_dispatch_bug]] + PR #338). Observed 4× on
    # 2026-05-18 when realtime SoC was 12-15 % overnight on prod (10.36 kWh
    # battery, 15 % reserve = 1.55 kWh, realtime down to 1.04 kWh).
    soc = pulp.LpVariable.dicts("soc", range(n + 1), lowBound=soc_min, upBound=soc_max)
    soc[0].lowBound = 0.0
    tank = pulp.LpVariable.dicts("tank", range(n + 1), lowBound=tank_lo, upBound=tank_hi)
    # PR Phase B: t_in / s_lo / s_hi removed. Heating demand is now bounded by
    # space_floor_kwh / space_ceil_kwh (physics from get_daikin_heating_kw),
    # not by indoor-temp tracking against a phantom room sensor.

    # Piecewise stress auxiliary variables (one per battery power slot)
    stress_aux: dict[int, pulp.LpVariable] = {}
    if use_stress:
        stress_aux = pulp.LpVariable.dicts("bat_stress", range(n), lowBound=0)

    # -----------------------------------------------------------------------
    # Initial conditions
    # -----------------------------------------------------------------------
    prob += soc[0] == initial.soc_kwh
    prob += tank[0] == initial.tank_temp_c

    # PR K2 (2026-05-23) — DHW pinning. When the deterministic schedule
    # from dhw_policy owns the tank, the LP must NOT optimize e_dhw or
    # tank_temp as free variables. Otherwise the LP's planned PV
    # consumption (which includes e_dhw) drifts from reality and the
    # battery scheduling sub-problem makes choices based on phantom DHW
    # load (e.g. over-aggressive Force Charge because LP thinks PV will
    # be eaten by tank heating that K1 already turned off).
    #
    # We pin e_dhw[i] to a forecast derived from the dhw_policy schedule
    # and pin tank[i] to the policy's target trajectory for audit honesty.
    # The tank-thermodynamic constraint below is conditionally skipped so
    # the pinned values don't over-constrain the LP into infeasibility.
    _dhw_pinned = bool(getattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False))
    _pinned_e_dhw: list[float] = []
    _pinned_tank: list[float] = []
    if _dhw_pinned:
        from .. import dhw_policy as _dhw_pol
        try:
            _mode = (config.OPTIMIZATION_PRESET or "normal").strip().lower()
            _pinned_e_dhw, _pinned_tank = _dhw_pol.forecast_dhw_load_per_slot(
                list(slot_starts_utc), mode=_mode,
                initial_tank_c=float(initial.tank_temp_c),
            )
        except Exception as _exc:  # pragma: no cover - defensive
            # Fall back to legacy free-variable behavior if the forecast
            # helper fails so we never break the solver.
            _pinned_e_dhw = []
            _pinned_tank = []
            _dhw_pinned = False
    if _dhw_pinned and len(_pinned_e_dhw) == n and len(_pinned_tank) == n + 1:
        # Pin e_dhw values (drop subscript 0 of tank because that's already
        # pinned to initial.tank_temp_c above).
        for i in range(n):
            prob += e_dhw[i] == _pinned_e_dhw[i]
            prob += tank[i + 1] == _pinned_tank[i + 1]
    else:
        # Disable pinning for this solve (forecast unavailable / shape mismatch).
        _dhw_pinned = False

    # -----------------------------------------------------------------------
    # Per-slot constraints
    # -----------------------------------------------------------------------
    cycle_pen = float(config.LP_CYCLE_PENALTY_PENCE_PER_KWH)
    # PR Phase B: comfort_pen unused — comfort slack vars removed.
    flat_export_rate = float(config.EXPORT_RATE_PENCE)
    # Per-slot export prices (Octopus Outgoing Agile) when supplied; flat fallback otherwise.
    export_rate_line: list[float] = (
        list(export_price_pence) if export_price_pence is not None
        else [flat_export_rate] * n
    )

    # Resolve shower windows + DHW draw model EARLY (before the per-slot loop)
    # so the tank thermo equation can subtract the realistic shower-time draw
    # from each slot's energy balance. Without this, the LP only sees standing
    # loss (~0.5°C/h) and misses the much bigger drop from someone showering
    # (~6°C per shower for a 200L tank). Result without it: LP plans no
    # heating during the day, evening tank is technically ≥ 45°C in the LP's
    # math but reality has tank dropping below 45°C mid-shower → firmware
    # reheats at unfavorable rates the LP didn't predict.
    #
    # PR B: explicit shower demand model via :mod:`src.dhw_demand`. Replaces
    # the legacy ``DHW_DAILY_SHOWER_LITRES`` aggregate with per-mode count ×
    # duration × flow × mixer-temp. The legacy env, if set > 0, still wins
    # (escape hatch). See ``plans/groovy-singing-flute.md`` PR B section.
    from .. import dhw_demand as _dhw
    from ..presets import OperationPreset
    try:
        _preset_enum = OperationPreset((config.OPTIMIZATION_PRESET or "normal").strip().lower())
    except (ValueError, AttributeError):
        _preset_enum = OperationPreset.NORMAL
    guests_preset = _preset_enum == OperationPreset.GUESTS
    shower_windows = _resolve_active_shower_windows(guests_preset)
    shower_mask = _window_set_slot_mask(slot_starts_utc, tz, windows=shower_windows)
    daily_shower_litres = _dhw.daily_shower_litres_drawn(_preset_enum)
    cold_inlet_c = float(getattr(config, "DHW_SHOWER_COLD_INLET_TEMP_C",
                                 getattr(config, "DHW_COLD_INLET_TEMP_C", 10.0)))
    # Mixer-out temp drives the hot-fraction math. PR B prefers the new
    # ``DHW_SHOWER_MIXER_TEMP_C`` (default 38 °C) but falls back to the
    # legacy ``DHW_USAGE_TEMP_C`` (40 °C) when the new setting is absent.
    use_temp_c = float(getattr(config, "DHW_SHOWER_MIXER_TEMP_C",
                               getattr(config, "DHW_USAGE_TEMP_C", 40.0)))
    # Linearised hot-water draw per slot (kWh thermal). Hot litres drawn
    # from tank = mix_litres × (mixer - cold) / (tank_storage - cold). The
    # divisor uses ``t_min_dhw`` (the LP's lower-bound representative tank
    # temperature) as a stable proxy, matching the prior model.
    #
    # CRITICAL: divide daily_shower_litres by the number of shower slots
    # IN THAT SLOT'S LOCAL DAY, not by the horizon-wide total. A 48 h horizon
    # with two daily shower windows has 12 shower slots total — using 12 as
    # the divisor would split each day's draw across the OTHER day's slots
    # too, under-modelling per-day draw by ~50%. Group by local date so
    # each day's daily_litres distributes correctly across that day's slots.
    from collections import defaultdict
    slots_per_day: dict[Any, int] = defaultdict(int)
    if daily_shower_litres > 0 and t_min_dhw > cold_inlet_c:
        for i in range(n):
            if shower_mask[i]:
                local_date = slot_starts_utc[i].astimezone(tz).date()
                slots_per_day[local_date] += 1

    def _draw_j_for_slot(i: int) -> float:
        if not shower_mask[i] or daily_shower_litres <= 0 or t_min_dhw <= cold_inlet_c:
            return 0.0
        local_date = slot_starts_utc[i].astimezone(tz).date()
        n_today = slots_per_day.get(local_date, 0)
        if n_today <= 0:
            return 0.0
        litres_per_slot = daily_shower_litres / n_today
        hot_litres = litres_per_slot * (use_temp_c - cold_inlet_c) / (t_min_dhw - cold_inlet_c)
        # Energy in J = L × CP_J/L/K × ΔT
        return hot_litres * float(config.DHW_WATER_CP) * (t_min_dhw - cold_inlet_c)

    shower_draw_j: list[float] = [_draw_j_for_slot(i) for i in range(n)]

    # PR C — Vacation: bateria carrega SÓ a partir de PV usada localmente
    # (chg ≤ pv_use). Ninguém em casa → LP nunca importa pra carregar a
    # bateria. Slots que normalmente fariam ForceCharge da grid (cheap /
    # negative) viram standard/solar_charge no labeller.
    _vacation_mode = _preset_enum == OperationPreset.VACATION

    for i in range(n):
        e_hp_i = e_dhw[i] + e_space[i]

        # PV split
        prob += pv_use[i] + pv_curt[i] == pv_avail[i]

        # Energy balance
        prob += imp[i] + pv_use[i] + dis[i] == base_load_kwh[i] + exp[i] + chg[i] + e_hp_i

        # Export source — mode-derived:
        #
        # * vacation: bateria pode descarregar pro grid (peak_export arbitrage)
        #   → ``exp <= pv_use + dis``.
        # * normal / guests: bateria SÓ alimenta self-use (load / DHW); PV
        #   excedente ainda exporta passivamente pelo Fox V3 SelfUse mode.
        #   → ``exp <= pv_use``. Como ``dis`` não pode contribuir pra export,
        #     o ramo ``dis > 0 AND exp > 0`` em ``lp_plan_to_slots`` nunca
        #     dispara → nenhum slot vira ``peak_export`` → nenhuma
        #     ForceDischarge group no Fox V3.
        #
        # PR D arquitetural (2026-05-22): substitui a regra dropped-at-dispatch
        # do ENERGY_STRATEGY_MODE=strict_savings (removido em PR C). Em modo
        # vacation o LP planeja arbitragem normalmente; em normal/guests, o LP
        # não tem solução viável que envolve descarga pro grid.
        if _vacation_mode:
            prob += exp[i] <= pv_use[i] + dis[i]
            # Vacation: bateria carrega só de PV (sem grid charging)
            prob += chg[i] <= pv_use[i]
        else:
            prob += exp[i] <= pv_use[i]

        # Battery SoC dynamics
        prob += soc[i + 1] == soc[i] + chg[i] * sqrt_eta - dis[i] / sqrt_eta

        # Import/export mutual exclusion
        prob += imp[i] <= fuse_kwh * a_grid[i]
        prob += exp[i] <= export_cap_kwh * (1 - a_grid[i])

        # Charge/discharge mutual exclusion
        prob += chg[i] <= max_batt_kwh * b_bat[i]
        prob += dis[i] <= max_batt_kwh * (1 - b_bat[i])

        if passive_daikin:
            # Passive: clamp Daikin draw to firmware-predicted values + bind
            # hp_on to a consistent value so other constraints (min-on,
            # objective) stay feasible. The firmware can run both DHW and
            # space heating inside a single 30-min slot, so e_dhw + e_space
            # may both be positive.
            prob += e_dhw[i] == passive_e_dhw[i]
            prob += e_space[i] == passive_e_space[i]
            on_val = 1 if (passive_e_dhw[i] + passive_e_space[i]) > 1e-6 else 0
            prob += hp_on[i] == on_val
        else:
            # Active mode: aggregate HP electrical draw bounded by the on/off
            # binary. NO mode mutex — both DHW and space heating can be active
            # in the same slot, matching the Altherma firmware. e_dhw is capped
            # by ``max_hp_kwh`` via its LpVariable upper bound; e_space is
            # additionally capped by the climate-curve physics ceiling.
            prob += e_dhw[i] + e_space[i] <= max_hp_kwh * hp_on[i]
            prob += e_space[i] <= space_ceil_kwh[i]

        # DHW tank thermodynamics — PR Phase B: indoor temp is no longer a
        # state variable. Tank loss uses INDOOR_SETPOINT_C as the constant
        # ambient (tank usually sits in a heated utility space at ~setpoint;
        # if it's outside that's a fixed offset already absorbed into
        # ``DHW_TANK_STANDING_LOSS_W_PER_K`` calibration).
        q_heat_dhw = e_dhw[i] * cop_dhw[i] * j_per_kwh
        loss_tank_j = ua_tank * (tank[i] - float(config.INDOOR_SETPOINT_C)) * dt_s
        # DHW draw on shower-window slots (static-physics model from #299).
        # Pre-computed in shower_draw_j[i]; zero outside shower windows.
        # PR #313: in PASSIVE mode the LP doesn't control DHW, so it can't
        # respond to shower draws by heating more — but the firmware DOES
        # reheat after showers. Subtracting shower draw without granting the
        # LP a way to compensate makes the tank crash below the 20°C floor
        # → infeasibility on shower-window replays. Skip the draw term in
        # passive: the firmware handles reheat opaque to the LP.
        draw_j_i = 0.0 if passive_daikin else shower_draw_j[i]
        # PR K2 — skip the tank thermodynamic equation when DHW pinning is
        # active. e_dhw and tank[i+1] are already pinned to the dhw_policy
        # forecast above; layering this constraint on top would over-
        # constrain the system (LP cannot satisfy both pinned values AND
        # the physics-derived relation, since the forecast is a simple
        # phase-based model, not a thermal sim).
        if not _dhw_pinned:
            prob += tank[i + 1] == tank[i] + (q_heat_dhw - loss_tank_j - draw_j_i) / c_tank

        # PR Phase B: building thermodynamics + comfort constraints removed.
        # Active mode now relies on the same physics floor as passive: the
        # heat pump WILL run on its weather curve. The LP's only choice is
        # WHEN within the (floor, ceil) corridor + lwt_offset — bounded by
        # ``space_floor_kwh[i]`` / ``space_ceil_kwh[i]`` already enforced
        # below in the active branch.
        if not passive_daikin:
            prob += e_space[i] * cop_space[i] <= float(config.RADIATOR_MAX_KW) * slot_h

            # Climate-curve floor: the Daikin compressor draws at least this much when
            # climate control is running. Prevents the LP from scheduling zero space heating
            # on cold overnight slots (which the heuristic can't fix after the fact).
            if space_floor_kwh[i] > 0:
                prob += e_space[i] + e_dhw[i] >= space_floor_kwh[i]

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
    # Skip in passive mode: hp_on[i] is forced by equality to the prediction, which can
    # legitimately alternate 0/1 across slots when outdoor temp crosses the curve cutoff
    # — enforcing min-on would make the LP infeasible. The Daikin firmware handles its
    # own short-cycling; the LP doesn't need to constrain something it isn't deciding.
    if hp_min_on > 1 and n >= hp_min_on and not passive_daikin:
        for i in range(n - hp_min_on + 1):
            # y_i - y_{i-1} = startup event; constrain sum of next min_on to >= min_on * startup
            # Simplified: if hp_on[i]=1 and hp_on[i-1]=0 (startup), force sum >= min_on
            # Since (hp_on[i] - prev) can be negative (shutdown), use max(0,.) → auxiliary
            startup_i = pulp.LpVariable(f"hp_startup_{i}", cat="Binary")
            prev = hp_on[i - 1] if i > 0 else pulp.LpVariable("hp_on_neg1_dummy", lowBound=0, upBound=0)
            prob += startup_i >= hp_on[i] - prev
            prob += (
                pulp.lpSum(hp_on[j] for j in range(i, min(i + hp_min_on, n)))
                >= hp_min_on * startup_i
            )

    # -----------------------------------------------------------------------
    # DHW hard constraints (showers — legionella is owned by Daikin firmware)
    # -----------------------------------------------------------------------
    # Resolve the active shower schedule (PR 4 of plan). New env
    # ``DHW_SHOWER_SCHEDULE`` supersedes ``LP_SHOWER_MORNING_LOCAL`` /
    # ``LP_SHOWER_EVENING_LOCAL``; legacy scalars are still honoured as a
    # backward-compat fallback when DHW_SHOWER_SCHEDULE is empty. Guests preset
    # picks DHW_SHOWER_SCHEDULE_GUESTS instead so morning showers are
    # re-enabled when a guest is staying.
    #
    # (``shower_mask`` was resolved earlier with the draw model around
    # line 535; ``_preset_enum`` is the canonical preset enum.)
    # Skip shower hard constraint in passive mode — the LP can't decide e_dhw
    # to make this happen (firmware controls the tank). Enforcing would make
    # the solve infeasible whenever tank starts low.
    #
    # Soft floor with high penalty (PR #344): the previous hard floor
    # ``tank[i+1] >= t_min_dhw`` on every shower-window slot drove an
    # infeasibility class observed empirically — 8 of 9 above-reserve
    # infeasibilities in the 60-day audit fired at the 21:25 BST tier-boundary
    # MPC trigger where slot 0 lands inside the evening shower window and
    # the tank is too cold to lift the required °C in a single 30-min slot
    # (physics floor: ~10 K/slot at max HP draw + COP 2.5, vs. e.g. 45-28
    # = 17 K required). With a hard constraint these solves returned
    # Infeasible and PR #338 then held the previous schedule. With this
    # soft floor + heavy penalty (default 50 p / K-slot, well above any
    # marginal kWh saving), the LP heats as fast as physically possible
    # and surfaces the unavoidable deficit as positive slack, instead of
    # going Infeasible.
    #
    # PR B: the floor is now PER-SLOT, derived from the mode-aware demand
    # via :mod:`src.dhw_demand`. Evening slots float the
    # ``required_tank_temp_for_n_showers(evening_count)`` constraint;
    # guests-mode morning slots float the morning-extras constraint;
    # normal-mode morning slots get an additional reserve-only soft floor
    # (no draw modelled) at the configured morning hour.
    s_shower_lo: dict[int, pulp.LpVariable] = {}
    shower_lo_penalty_p = float(
        getattr(config, "LP_SHOWER_LO_PENALTY_PENCE_PER_DEGC_SLOT", 50.0)
    )

    def _floor_for_window(window: str) -> float:
        """Per-window required tank temp; capped at ``tank_hi`` so the
        soft-floor constraint can always be made feasible by enough slack."""
        try:
            req = _dhw.required_tank_temp_for_window(window, _preset_enum)
        except (ValueError, AttributeError):
            req = float(t_min_dhw)
        return min(req, tank_hi)

    evening_floor_c = _floor_for_window("evening")
    morning_floor_c = _floor_for_window("morning")

    def _slot_window_kind(slot_utc: datetime) -> str | None:
        """Return 'evening', 'morning', or None for a slot start time
        using local hour-of-day; assumes the shower_windows tuple was
        sorted to evening = the later range, morning = the earlier."""
        local = slot_utc.astimezone(tz)
        hod_min = local.hour * 60 + local.minute
        in_morning = False
        in_evening = False
        for start, end in shower_windows:
            if start <= hod_min < end:
                if start < 12 * 60:
                    in_morning = True
                else:
                    in_evening = True
        if in_evening:
            return "evening"
        if in_morning:
            return "morning"
        return None

    # PR C — Vacation mode: no shower floor at all. The tank is allowed
    # to coast freely (down to ``tank_lo=20`` anti-freeze) and the Daikin
    # firmware owns the weekly legionella cycle. The household is away;
    # there's nothing to deliver hot water to.
    # PR K2 — when DHW is pinned, the shower floor would over-constrain
    # against the pinned tank trajectory (37 °C overnight < typical
    # evening floor of 40+ °C). Tank temp is fully owned by dhw_policy
    # now; this floor is irrelevant.
    if not passive_daikin and not _vacation_mode and not _dhw_pinned:
        for i in range(n):
            if shower_mask[i]:
                kind = _slot_window_kind(slot_starts_utc[i])
                if kind == "morning":
                    floor_c = morning_floor_c
                else:
                    floor_c = evening_floor_c
                s_shower_lo[i] = pulp.LpVariable(f"shower_lo_slack_{i}", lowBound=0)
                prob += tank[i + 1] + s_shower_lo[i] >= floor_c

        # PR B — normal-mode morning reserve. Single slot at the configured
        # morning hour (default 07:00 local) with a floor of
        # ``required_tank_temp_for_n_showers(reserve_count)``. No additional
        # draw is subtracted: the household typically doesn't shower in the
        # morning, this is the safety reserve for backup. Skipped in guests
        # mode (the morning shower window already enforces a higher floor)
        # and vacation mode (no floor at all).
        if _preset_enum == OperationPreset.NORMAL:
            reserve_count = _dhw.total_morning_showers(OperationPreset.NORMAL)
            if reserve_count > 0:
                from .. import runtime_settings as _rts
                try:
                    morning_hour = int(_rts.get_setting("DHW_MORNING_RESERVE_HOUR_LOCAL"))
                except (TypeError, ValueError):
                    morning_hour = 7
                reserve_floor_c = min(
                    _dhw.required_tank_temp_for_n_showers(reserve_count), tank_hi,
                )
                for i, st in enumerate(slot_starts_utc):
                    local = st.astimezone(tz)
                    if local.hour != morning_hour or local.minute >= 30:
                        continue
                    # Reuse the same slack variable mechanism so the LP can
                    # surface a deficit instead of going infeasible.
                    if i not in s_shower_lo:
                        s_shower_lo[i] = pulp.LpVariable(
                            f"shower_lo_slack_{i}", lowBound=0,
                        )
                    prob += tank[i + 1] + s_shower_lo[i] >= reserve_floor_c

        # Weekly legionella thermal-shock cycle — FIRMWARE-OWNED.
        #
        # PR E (2026-05-22, user clarification): Daikin Onecta firmware runs
        # the cycle autonomously on Sunday ~11:00 local. HEM does NOT control
        # it. The LP only needs to ACCOUNT FOR THE kWh LOAD on those slots so
        # the rest of the plan (battery charge, grid import) is sized
        # correctly.
        #
        # The previous hard constraint ``tank[i+1] >= leg_target`` made the
        # LP plan active pre-heating to 60 °C as if HEM were driving the
        # cycle — wasted compressor cycles overlapping with firmware's
        # autonomous heat.
        #
        # New approach: add a fixed firmware-load floor on ``e_dhw[i]`` for
        # slots inside the cycle window. The LP allocates AT LEAST the
        # firmware load (matching what really happens on the hardware) but
        # doesn't try to lift the tank itself. Mirrors the approach in
        # ``physics.predict_passive_daikin_load:248-273`` which already does
        # this for passive mode. Disabled when ``DHW_LEGIONELLA_DAY`` = -1.
        from .. import runtime_settings as _rts
        try:
            _leg_day = int(_rts.get_setting("DHW_LEGIONELLA_DAY"))
        except (TypeError, ValueError):
            _leg_day = -1
        # PR K2 — when DHW is pinned to dhw_policy forecast, the e_dhw
        # values are fixed; layering a legionella floor on top would force
        # infeasibility (pinned 0.04 < floor 0.5). Daikin firmware still
        # runs the cycle autonomously; the slight LP under-estimate of
        # Sunday-afternoon DHW load is a known acceptable cost (~£0.05/week).
        if 0 <= _leg_day <= 6 and not _dhw_pinned:
            try:
                _leg_hour = int(_rts.get_setting("DHW_LEGIONELLA_HOUR_LOCAL"))
                _leg_minutes = int(_rts.get_setting("DHW_LEGIONELLA_DURATION_MIN"))
                _leg_target_c = float(_rts.get_setting("DHW_LEGIONELLA_TANK_TARGET_C"))
            except (TypeError, ValueError):
                _leg_hour, _leg_minutes, _leg_target_c = 13, 60, 60.0
            # Slot duration in hours (assumes 30-min slots per the LP).
            _slot_h = 0.5
            _slots_per_cycle = max(
                1, (_leg_minutes + int(_slot_h * 60) - 1) // int(_slot_h * 60)
            )
            _cycle_window_h = _slots_per_cycle * _slot_h
            # Thermal energy to lift the tank from the LP's normal target to
            # the legionella target. Modeled as a fixed exogenous draw the
            # firmware imposes — independent of where the LP's tank state
            # actually was at slot start.
            _normal_target = float(config.DHW_TEMP_NORMAL_C)
            _delta_c = max(0.0, _leg_target_c - _normal_target)
            _thermal_kwh = (
                float(config.DHW_TANK_LITRES) * float(config.DHW_WATER_CP)
                * _delta_c / 3.6e6
            )
            for i, _st in enumerate(slot_starts_utc):
                _local = _st.astimezone(tz)
                if _local.weekday() != _leg_day:
                    continue
                _hour_frac = _local.hour + _local.minute / 60.0
                if not (_leg_hour <= _hour_frac < _leg_hour + _cycle_window_h):
                    continue
                _cop_i = max(1.0, float(cop_dhw[i]))
                _firmware_load_kwh = _thermal_kwh / _slots_per_cycle / _cop_i
                # Cap at the heat-pump's per-slot ceiling so the constraint
                # stays feasible. Real firmware also obeys this physical
                # bound — undersized estimate is fine; the LP just won't
                # over-allocate.
                _floor = min(_firmware_load_kwh, max_hp_kwh)
                prob += e_dhw[i] >= _floor

    # Per-slot DHW ceiling — three tiers:
    #   negative-price → DHW_TEMP_MAX_C (default 65 °C). Grid pays us; load all the kWh in.
    #   PV-abundant   → DHW_TEMP_PV_ABUNDANCE_TARGET_C (default 55 °C). Lower than negative
    #                   because (a) the user's empirical manual schedule lifts to 45 °C and
    #                   (b) holding 65 °C through the day bleeds back via standing losses
    #                   before the evening shower window arrives.
    #   else          → DHW_TEMP_COMFORT_C (default 48 °C).
    # PV abundance per slot = (pv_avail − base_load) > threshold.
    # The original formula in PR #287 also subtracted ``max_batt_kwh`` (the
    # inverter's per-slot charge cap, ~2.5 kWh). That made abundance only
    # trigger when PV > base_load + 2.5 + threshold ≈ 3.3 kWh/slot — basically
    # peak-summer-noon territory only. The intent was "PV that would otherwise
    # be exported / curtailed", but ``max_batt_kwh`` is a constant cap, not
    # remaining battery capacity, so a full battery looked the same as an
    # empty one.
    #
    # Dropping the battery term lets abundance trigger on more realistic
    # sunny days. The LP's natural preference (cycle penalty + battery
    # objective) still picks battery-charge over tank-heat when both are
    # profitable; the threshold change just gives the LP the *option* to
    # heat the tank when battery is full or cycle-penalty makes it the
    # cheaper marginal sink. Soft constraint: heavy penalty on breach so an
    # initial tank already above the ceiling (inherited from a prior lift)
    # stays feasible.
    pv_abundance_threshold = float(getattr(config, "DHW_PV_ABUNDANCE_THRESHOLD_KWH", 0.5))
    pv_abundance: list[bool] = [
        (pv_avail[i] - base_load_kwh[i]) > pv_abundance_threshold
        for i in range(n)
    ]
    pv_abundance_target = float(getattr(config, "DHW_TEMP_PV_ABUNDANCE_TARGET_C", 55.0))
    # Negative-price wins when both conditions are true (always more aggressive).
    tank_hi_slot = [
        float(config.DHW_TEMP_MAX_C) if price_line[i] < 0
        else (pv_abundance_target if pv_abundance[i] else float(config.DHW_TEMP_COMFORT_C))
        for i in range(n)
    ]
    s_tank_hi = pulp.LpVariable.dicts("tank_hi_slack", range(n), lowBound=0)
    for i in range(n):
        prob += tank[i + 1] <= tank_hi_slot[i] + s_tank_hi[i]

    # Pre-plunge discipline: when a negative slot is in the upcoming
    # ``LP_PLUNGE_PREP_HOURS`` window, disallow grid→battery flow during
    # positive-priced slots so import capacity is reserved for the negative
    # window. PV→battery ("solar_charge") stays allowed.
    #
    # Bounded to N hours (default 12) instead of the entire horizon: with the
    # 48 h horizon, an unbounded look-ahead can lock the first 24 h of cheap
    # slots when negatives don't arrive until D+1 evening — discovered in the
    # 2026-05-02 LP audit (only 33% of charge slots in the cheap quartile on
    # a day where the negative slots were >24 h away).
    plunge_prep_hours = max(0, int(getattr(config, "LP_PLUNGE_PREP_HOURS", 12)))
    if plunge_prep_hours > 0:
        plunge_window_slots = int(plunge_prep_hours * 2)  # 30-min slots
        next_neg_within_window: list[bool] = [False] * n
        for i in range(n):
            j_end = min(n, i + plunge_window_slots)
            next_neg_within_window[i] = any(
                price_line[j] < 0 for j in range(i, j_end)
            )
        for i in range(n):
            if next_neg_within_window[i] and price_line[i] >= 0:
                prob += chg[i] <= pv_use[i]

    # PV-sufficiency guard rail (issue: 2026-05-15 incident). When forecast
    # PV for today ≥ battery headroom + remaining daytime load ×
    # ``LP_PV_SUFFICIENCY_MARGIN``, block grid → battery for every today-
    # slot strictly before the first peak-tariff slot. Constraint shape
    # mirrors the pre-plunge rule above (PV → battery stays allowed via
    # ``pv_use[i]``). See ``src/scheduler/pv_trust.py`` and
    # ``docs/PV_TRUST_GUARDRAIL.md`` for the design.
    #
    # PR C — promoted to always-on (previously only fired under
    # ``ENERGY_STRATEGY_MODE=strict_savings``). The economic argument
    # is mode-agnostic: when forecast PV would already fill the battery,
    # grid-charging before the first peak slot is wasteful.
    pv_guard_diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=slot_starts_utc,
        pv_avail=pv_avail,
        base_load_kwh=base_load_kwh,
        price_line=price_line,
        peak_threshold_p=peak_thr,
        initial_soc_kwh=float(initial.soc_kwh),
        soc_max_kwh=float(soc_max),
    )
    if pv_guard_diag.applied:
        for i in pv_guard_diag.pre_peak_slot_indices:
            prob += chg[i] <= pv_use[i]
        logger.info(
            "PV-sufficiency guard rail applied: forecast=%.2f kWh, demand=%.2f kWh, "
            "margin=%.2f, blocking grid→battery on %d pre-peak slots",
            pv_guard_diag.forecast_pv_today_kwh,
            pv_guard_diag.demand_kwh,
            pv_guard_diag.margin,
            len(pv_guard_diag.pre_peak_slot_indices),
        )

    # Negative-price discharge lock: dis = 0 when price < 0. Discharging while
    # the grid is paying us to import is strictly dominated — surplus import
    # capacity must flow into chg, e_hp, or exp. Also guarantees the dispatcher
    # can trust plan.battery_discharge_kwh[i] < EPS for every negative slot.
    for i in range(n):
        if price_line[i] < 0:
            prob += dis[i] == 0

    # -----------------------------------------------------------------------
    # Terminal constraints
    # -----------------------------------------------------------------------
    # SoC: hard floor if configured, else soft (soc ≥ initial)
    if soc_final_kwh > soc_min:
        prob += soc[n] >= min(soc_final_kwh, soc_max)
    else:
        prob += soc[n] >= initial.soc_kwh

    # Skip terminal tank/indoor floors in passive mode — same reason as shower
    # hard constraint above. Firmware controls comfort; the LP only optimises
    # battery/grid/PV around the predicted Daikin draw.
    #
    # Also skip when DHW is pinned (PR K2): ``tank[n]`` is already pinned by
    # ``dhw_policy.forecast_dhw_load_per_slot``, and the policy can target a
    # value below ``terminal_dhw_floor`` (e.g. guests mode pins 45 °C while
    # ``t_min_dhw=55`` → floor 53 °C → infeasible). This mirrors the K2
    # guard on the soft shower floor above.
    if not passive_daikin and not _dhw_pinned:
        # PR 4 of plan: when the last slot lands inside a shower window, keep
        # the tight terminal floor so the LP must finish with a hot tank for
        # in-progress showers. When it lands OUTSIDE shower windows (e.g. a
        # 48 h horizon ending at 04:00 local), fall back to a much lower
        # ``DHW_TEMP_MIN_FLOOR_C`` so the LP isn't forced into expensive
        # overnight reheat just to satisfy a horizon-end constraint.
        last_in_shower = bool(shower_mask[-1]) if shower_mask else False
        if last_in_shower:
            terminal_dhw_floor = t_min_dhw - 2.0
        else:
            terminal_dhw_floor = float(getattr(config, "DHW_TEMP_MIN_FLOOR_C", 30.0))
        prob += tank[n] >= terminal_dhw_floor
        # PR Phase B: terminal indoor-temp floor removed (no t_in variable).

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
    obj_grid = pulp.lpSum(
        imp[i] * price_line[i] - exp[i] * export_rate_line[i]
        for i in range(n)
    )
    # Rank-based export-timing bonus (#274). On flat Outgoing-rate days the
    # absolute spread between top-quartile and median can be ~1–2 p/kWh, so
    # ``-exp[i] × export_rate[i]`` alone doesn't strongly prefer the top
    # quartile when those slots happen to coincide with low PV. Add a small
    # extra revenue term on slots whose Outgoing rate sits at or above the
    # ``LP_PEAK_EXPORT_TOP_QUARTILE_PERCENT`` threshold of the LP horizon's
    # distribution. The bonus is a tie-breaker — it must be small enough
    # never to cause curtailment when prices are uniformly low.
    rank_bonus_p = float(getattr(config, "LP_PEAK_EXPORT_RANK_BONUS_PENCE_PER_KWH", 0.0))
    if rank_bonus_p > 0 and export_price_pence is not None:
        positive_rates = [r for r in export_rate_line if r is not None and r > 0]
        if len(positive_rates) >= 4:
            pct = max(0.0, min(100.0, float(getattr(config, "LP_PEAK_EXPORT_TOP_QUARTILE_PERCENT", 25.0))))
            sorted_rates = sorted(positive_rates)
            cutoff_idx = int((1.0 - pct / 100.0) * len(sorted_rates))
            cutoff_idx = min(max(cutoff_idx, 0), len(sorted_rates) - 1)
            top_q_threshold = sorted_rates[cutoff_idx]
            top_q_indices = [i for i in range(n) if export_rate_line[i] >= top_q_threshold]
            if top_q_indices:
                obj_grid -= rank_bonus_p * pulp.lpSum(exp[i] for i in top_q_indices)
    obj_cycle = cycle_pen * pulp.lpSum(chg[i] + dis[i] for i in range(n))
    # PR Phase B: obj_comfort removed (no s_lo / s_hi slack variables).
    obj_comfort = 0.0
    # DHW overshoot above the comfort ceiling is not a comfort issue — it's just stored
    # hot water that will drift back naturally via tank losses. A *tiny* penalty
    # (default 0.01 p/°C-slot, configurable via ``LP_TANK_HI_SLACK_PENCE_PER_DEGC_SLOT``;
    # closes #225 item 1) breaks ties toward the lower tank target without blocking the LP
    # from filling the tank to 65 °C during negative-price windows (the whole point of
    # #50). Positive-price DHW heating is already discouraged by obj_grid, so no extra
    # penalty is needed to prevent gratuitous overshoot.
    tank_hi_slack_p = float(getattr(config, "LP_TANK_HI_SLACK_PENCE_PER_DEGC_SLOT", 0.01))
    obj_tank_hi = tank_hi_slack_p * pulp.lpSum(s_tank_hi[i] for i in range(n))
    # PR #344 shower-floor slack penalty. Heavy by default (50 p / K-slot)
    # so the LP only breaches when physically forced — the slack is the
    # "we couldn't reach 45 °C in time" diagnostic. Setting the penalty to
    # zero would degenerate to "ignore shower floor", which is wrong; tune
    # downward only with care.
    obj_shower_lo: Any = 0
    if s_shower_lo:
        obj_shower_lo = shower_lo_penalty_p * pulp.lpSum(
            s_shower_lo[i] for i in s_shower_lo
        )
    # PV-abundance DHW reward: when PV exceeds self-use + battery headroom, every kWh
    # the LP routes into the tank instead of curtailing earns a small reward. Tied to
    # the same per-slot bool used for the ceiling lift above.
    #
    # Per user 2026-05-09: prefer tank-store over export when at home (household
    # will use the stored hot water). Default 10 p/kWh × cop ≈ 30 p stored
    # value, well above 15 p export → tank wins. ZEROED here when preset is
    # travel/away — household isn't there to use stored hot water, revert to
    # export-priority economics.
    pv_abundance_reward_p = float(getattr(config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 0.0))
    try:
        from ..presets import OperationPreset
        _preset_value = OperationPreset(config.OPTIMIZATION_PRESET)
        if _preset_value == OperationPreset.VACATION:
            pv_abundance_reward_p = 0.0
    except (ValueError, AttributeError):
        pass
    # PR I (2026-05-22) — DYNAMIC per-slot reward. Without this, the LP
    # picks export over tank whenever the slot's export rate exceeds the
    # static reward (observed in prod 2026-05-22: Outgoing Agile ~15p
    # beat the static 10p, so PV got exported with tank at 45 °C).
    # Formula: ``slot_reward = max(static_reward, export_rate + buffer)``.
    # The buffer keeps tank > export by a small margin (default 2 p) so
    # the LP definitively prefers thermal storage over grid export. Battery
    # charging still beats tank because its future-value (peak discharge
    # or self-use avoidance) is computed via the energy balance and
    # typically exceeds 25 p/kWh in evening peak hours, well above any
    # plausible export+buffer combination. So priority order remains:
    # battery → tank → export.
    abundance_beat_export_buffer_p = float(
        getattr(config, "LP_PV_ABUNDANCE_TANK_BEAT_EXPORT_BUFFER_PENCE", 2.0)
    )
    obj_pv_abundance_dhw: Any = 0
    if pv_abundance_reward_p > 0:
        abundant_indices = [i for i in range(n) if pv_abundance[i]]
        if abundant_indices:
            obj_pv_abundance_dhw = -pulp.lpSum(
                max(
                    pv_abundance_reward_p,
                    export_rate_line[i] + abundance_beat_export_buffer_p,
                ) * e_dhw[i]
                for i in abundant_indices
            )
    # PV curtailment penalty: prevents the LP from "happily curtailing" solar during
    # ForceCharge slots when the chg cap binds. Without this, pv_curt has zero objective
    # coefficient and the LP picks max-imp + curtail because grid imp at -7p ties or
    # beats PV's zero direct value. Penalty = EXPORT_RATE_PENCE makes curtailment
    # cost-equivalent to "would have exported", restoring the correct ranking: prefer
    # pv_use → battery over grid → battery when both compete. See prod audit 2026-04-30:
    # 74% of one day's PV (6.34 kWh, ~£0.95) curtailed under the legacy zero-penalty
    # objective. Set ``LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH=0`` to revert.
    pv_curt_pen = float(getattr(config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 0.0))
    obj_pv_curt = (
        pv_curt_pen * pulp.lpSum(pv_curt[i] for i in range(n)) if pv_curt_pen > 0 else 0
    )
    objective = (
        obj_grid + obj_cycle + obj_comfort + obj_tank_hi
        + obj_pv_curt + obj_pv_abundance_dhw + obj_shower_lo
    )

    if use_stress and stress_aux:
        # Stress cost is suppressed during negative-price slots: every kWh of
        # chg earns revenue, so imaginary inverter-wear penalty must not bias
        # the LP toward under-charging. Positive-price slots keep the smoothing.
        stress_gate = [1.0 if price_line[i] >= 0 else 0.0 for i in range(n)]
        objective += pulp.lpSum(
            stress_aux[i] * slot_h * stress_gate[i] for i in range(n)
        )

    if w_bat_tv > 0 and tv_chg:
        objective += w_bat_tv * (
            pulp.lpSum(tv_chg[i] for i in tv_chg)
            + pulp.lpSum(tv_dis[i] for i in tv_dis)
        )
    if w_hp_tv > 0 and tv_hp:
        objective += w_hp_tv * pulp.lpSum(tv_hp[i] for i in tv_hp)
    if w_imp_tv > 0 and tv_imp:
        objective += w_imp_tv * pulp.lpSum(tv_imp[i] for i in tv_imp)

    # Soft-cost on terminal SoC above the floor (S10.1, #168). Without this
    # the LP only has the hard LP_SOC_FINAL_KWH constraint and treats any kWh
    # above the floor as zero-value — biasing toward draining the battery for
    # marginal arbitrage that a small overnight import then "fixes". Each kWh
    # at horizon end is worth N pence (avoided next-horizon import cost), so
    # marginal arbitrage with spread < N pence/kWh stops winning. The constant
    # offset (-N × floor) is dropped — it doesn't change the optimum.
    soc_terminal_value = float(getattr(config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0))
    if soc_terminal_value > 0:
        objective -= soc_terminal_value * soc[n]

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
        pv_sufficiency_guard=pv_guard_diag,
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
        es_val = _v(e_space[i])
        plan.space_electric_kwh.append(es_val)
        # Back-compute the LWT offset the Daikin must apply to deliver this energy draw.
        plan.lwt_offset_c.append(lwt_offset_from_space_kw(es_val / slot_h, t_out[i]))
    for i in range(n + 1):
        plan.soc_kwh.append(_v(soc[i]))
        plan.tank_temp_c.append(_v(tank[i]))

    return plan
