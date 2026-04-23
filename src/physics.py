"""Thermodynamic physics calculations for the Home Energy Manager.

Provides deterministic Daikin DHW setpoint calculation using thermal decay modelling,
preventing LLM hallucination and ensuring physics-based setpoints.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

HEAT_LOSS_C_PER_HOUR: float = 0.3  # Daikin Altherma typical standing loss °C/h
# kW drawn per °C of LWT above the 18 °C compressor-off threshold.
# Calibrated empirically: LWT 36 °C at 4 °C outdoor → 0.60 kW → (36-18)*k = 0.60 → k = 0.0333
_KW_PER_DEGC_LWT: float = 0.0333
MARGIN_OF_SAFETY_C: float = 0.5    # Pipe-loss / measurement margin
DHW_SETPOINT_MAX_C: float = 65.0   # Absolute safe ceiling (no boiling stress)
DHW_SETPOINT_MIN_C: float = 35.0   # Never go below legionella risk floor


def calculate_dhw_setpoint(
    target_temp_c: float,
    target_time_iso: str,
    heat_end_time_iso: str,
    heat_loss_c_per_hour: float = HEAT_LOSS_C_PER_HOUR,
    margin_c: float = MARGIN_OF_SAFETY_C,
) -> float:
    """Calculate the Daikin tank setpoint needed to hit *target_temp_c* at *target_time_iso*
    given that active heating ends at *heat_end_time_iso*.

    Example:
        Target 45 °C shower at 09:30, heating ends at 04:00 → 5.5 h decay
        → setpoint = 45 + (5.5 × 0.3) + 0.5 = 47.15 °C → rounded to 47.2 °C

    Both ISO strings may include timezone info (e.g. ``Z``, ``+00:00``, or local offset).
    Naive datetimes are treated as UTC.

    Returns a value clamped to [DHW_SETPOINT_MIN_C, DHW_SETPOINT_MAX_C].
    """
    end_dt = _parse_iso(heat_end_time_iso)
    target_dt = _parse_iso(target_time_iso)

    hours_diff = (target_dt - end_dt).total_seconds() / 3600.0
    if hours_diff <= 0:
        return float(target_temp_c)

    dynamic_setpoint = target_temp_c + (hours_diff * heat_loss_c_per_hour) + margin_c
    return float(max(DHW_SETPOINT_MIN_C, min(DHW_SETPOINT_MAX_C, round(dynamic_setpoint, 1))))


def find_dhw_heat_end_utc(
    cheap_slots: list,
    overnight_start_h: int = 2,
    overnight_end_h: int = 7,
    tz: ZoneInfo | None = None,
) -> datetime | None:
    """Return the end-UTC of the latest cheap/negative ForceCharge slot in the early-morning
    cheap window (``overnight_start_h``..``overnight_end_h`` local time).

    ``cheap_slots`` are ``HalfHourSlot`` instances (from the optimizer).  The caller filters
    to the overnight window before passing in, but this function applies the hour check again
    for safety.

    Returns ``None`` when no overnight cheap slots exist.
    """
    if tz is None:
        tz = ZoneInfo("Europe/London")

    best_end: datetime | None = None
    for s in cheap_slots:
        if s.kind not in ("cheap", "negative"):
            continue
        local_start = s.start_utc.astimezone(tz)
        if overnight_start_h <= local_start.hour < overnight_end_h:
            if best_end is None or s.end_utc > best_end:
                best_end = s.end_utc
    return best_end


def build_shower_target_iso(plan_date_iso: str, hour: int = 9, minute: int = 30, tz: ZoneInfo | None = None) -> str:
    """Build an ISO timestamp for the shower target time on *plan_date_iso* in local time.

    Returns a UTC ISO-8601 string ending in ``Z``.
    """
    if tz is None:
        tz = ZoneInfo("Europe/London")
    from datetime import date
    from datetime import datetime as dt
    from datetime import time as dtime
    d = date.fromisoformat(plan_date_iso)
    local_dt = dt.combine(d, dtime(hour, minute)).replace(tzinfo=tz)
    utc_dt = local_dt.astimezone(UTC)
    return utc_dt.isoformat().replace("+00:00", "Z")


def get_lwt_base_c(temp_outdoor_c: float) -> float:
    """Return the base LWT (°C) the Daikin targets at this outdoor temp with lwt_offset=0.

    Derived from the physical climate curve configured on the panel.  This is the
    'natural' LWT before any user or LP-applied offset.
    """
    from .config import config  # local import avoids circular deps at module load

    high_c = config.DAIKIN_WEATHER_CURVE_HIGH_C
    high_lwt = config.DAIKIN_WEATHER_CURVE_HIGH_LWT_C
    low_c = config.DAIKIN_WEATHER_CURVE_LOW_C
    low_lwt = config.DAIKIN_WEATHER_CURVE_LOW_LWT_C
    user_offset = config.DAIKIN_WEATHER_CURVE_OFFSET_C

    span_temp = high_c - low_c
    span_lwt = low_lwt - high_lwt
    slope = span_lwt / span_temp if span_temp != 0 else 0.0
    lwt = high_lwt + slope * (high_c - temp_outdoor_c) + user_offset
    return min(50.0, max(18.0, lwt))


def get_daikin_heating_kw(temp_outdoor_c: float, lwt_offset_delta: float = 0.0) -> float:
    """Estimate continuous space-heating electrical draw from outdoor temperature.

    Uses the physical climate (weather-compensation) curve configured on the Daikin panel
    to derive the leaving-water temperature (LWT), then converts LWT to compressor draw
    via the empirically calibrated factor.  Returns 0.0 when the compressor is off
    (outdoor temp above the curve's warm cutoff point).

    ``lwt_offset_delta`` shifts the LWT above or below the curve's natural value —
    use ``config.OPTIMIZATION_LWT_OFFSET_MAX`` to compute the physics ceiling, or
    ``config.OPTIMIZATION_LWT_OFFSET_MIN`` for the floor at minimum boost.

    Config-driven via DAIKIN_WEATHER_CURVE_* env vars; call site must import config.
    """
    from .config import config  # local import avoids circular deps at module load

    if temp_outdoor_c >= config.DAIKIN_WEATHER_CURVE_HIGH_C:
        return 0.0

    lwt = get_lwt_base_c(temp_outdoor_c) + lwt_offset_delta
    lwt = min(50.0, max(18.0, lwt))

    return (lwt - 18.0) * _KW_PER_DEGC_LWT


def apply_cop_lift_multiplier(
    cop_base: float,
    temp_outdoor_c: float,
    lwt_supply_c: float,
    *,
    penalty_per_k: float,
    reference_delta_k: float,
    min_mult: float,
) -> float:
    """Scale COP down when supply LWT is far above outdoor temp (Python pre-processing only; #29).

    ``COP_eff = COP_base * mult`` with ``mult`` linear in ``max(0, lift − ref)`` where
    ``lift = max(0, LWT_supply − T_out)``. ``penalty_per_k <= 0`` ⇒ returns ``max(1, cop_base)``.
    """
    if penalty_per_k <= 0.0:
        return max(1.0, float(cop_base))
    lift = max(0.0, float(lwt_supply_c) - float(temp_outdoor_c))
    excess = max(0.0, lift - float(reference_delta_k))
    mult = max(float(min_mult), 1.0 - float(penalty_per_k) * excess)
    return max(1.0, float(cop_base) * mult)


def predict_passive_daikin_load(
    temp_outdoor_c: list[float],
    cop_dhw: list[float],
    cop_space: list[float],
    *,
    slot_h: float = 0.5,
    max_kwh_per_slot: float | None = None,
) -> tuple[list[float], list[float]]:
    """Predict the Daikin's autonomous electrical draw per slot in passive mode.

    Used by the LP to clamp e_space[t] and e_dhw[t] when the service does NOT
    control the Daikin (DAIKIN_CONTROL_MODE=passive). The firmware runs its own
    weather-compensation curve, so the LP must attribute that draw as fixed load.

    - Space: natural climate-curve compressor draw at zero LWT offset, same as
      the LP's existing ``space_floor_kwh`` floor (so equality is feasible by
      construction in the LP's own physics).
    - DHW: steady-state top-up to maintain ``DHW_TEMP_NORMAL_C`` against tank
      standing loss, divided by the slot's COP.

    Returns ``(e_space_kwh_per_slot, e_dhw_kwh_per_slot)`` — both clipped into
    ``[0, max_kwh_per_slot]`` when ``max_kwh_per_slot`` is provided.
    """
    from .config import config  # local import avoids circular deps

    n = len(temp_outdoor_c)
    if not (len(cop_dhw) == len(cop_space) == n):
        raise ValueError(
            f"predict_passive_daikin_load: length mismatch "
            f"(t_out={n}, cop_dhw={len(cop_dhw)}, cop_space={len(cop_space)})"
        )

    space_kwh = [max(0.0, get_daikin_heating_kw(t) * slot_h) for t in temp_outdoor_c]

    ua_tank_w_per_k = float(config.DHW_TANK_UA_W_PER_K)
    tank_target_c = float(config.DHW_TEMP_NORMAL_C)
    indoor_c = float(config.INDOOR_SETPOINT_C)
    tank_loss_kw = max(0.0, ua_tank_w_per_k * (tank_target_c - indoor_c) / 1000.0)
    dhw_kwh = [
        tank_loss_kw * slot_h / max(1.0, float(cop_dhw[i]))
        for i in range(n)
    ]

    if max_kwh_per_slot is not None:
        cap = float(max_kwh_per_slot)
        space_kwh = [min(cap, v) for v in space_kwh]
        dhw_kwh = [min(cap, v) for v in dhw_kwh]

    return space_kwh, dhw_kwh


def lwt_offset_from_space_kw(space_kw: float, temp_outdoor_c: float) -> float:
    """Back-compute the LWT offset that would produce ``space_kw`` electrical draw.

    This is the inverse of ``get_daikin_heating_kw``.  Used by the LP dispatch layer
    to translate the solver's ``e_space[i]`` decision into a concrete Daikin command.

    Returns a value clamped to ``[OPTIMIZATION_LWT_OFFSET_MIN, OPTIMIZATION_LWT_OFFSET_MAX]``.
    """
    from .config import config

    if space_kw <= 0.0:
        return float(config.OPTIMIZATION_LWT_OFFSET_MIN)

    # kW = (lwt_actual - 18.0) * _KW_PER_DEGC_LWT  →  lwt_actual = kW / k + 18
    lwt_actual = space_kw / _KW_PER_DEGC_LWT + 18.0
    lwt_base = get_lwt_base_c(temp_outdoor_c)
    offset = lwt_actual - lwt_base

    lo = float(config.OPTIMIZATION_LWT_OFFSET_MIN)
    hi = float(config.OPTIMIZATION_LWT_OFFSET_MAX)
    return max(lo, min(hi, offset))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    x = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
