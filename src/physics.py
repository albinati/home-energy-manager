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


def get_daikin_heating_kw(temp_outdoor_c: float) -> float:
    """Estimate continuous space-heating electrical draw from outdoor temperature.

    Uses the physical climate (weather-compensation) curve configured on the Daikin panel
    to derive the leaving-water temperature (LWT), then converts LWT to compressor draw
    via the empirically calibrated factor.  Returns 0.0 when the compressor is off
    (outdoor temp above the curve's warm cutoff point).

    Config-driven via DAIKIN_WEATHER_CURVE_* env vars; call site must import config.
    """
    from .config import config  # local import avoids circular deps at module load

    high_c = config.DAIKIN_WEATHER_CURVE_HIGH_C
    high_lwt = config.DAIKIN_WEATHER_CURVE_HIGH_LWT_C
    low_c = config.DAIKIN_WEATHER_CURVE_LOW_C
    low_lwt = config.DAIKIN_WEATHER_CURVE_LOW_LWT_C
    user_offset = config.DAIKIN_WEATHER_CURVE_OFFSET_C

    if temp_outdoor_c >= high_c:
        return 0.0

    # Linear interpolation between the two panel-configured points
    span_temp = high_c - low_c  # e.g. 18 - (-5) = 23
    span_lwt = low_lwt - high_lwt  # e.g. 45 - 22 = 23
    slope = span_lwt / span_temp if span_temp != 0 else 0.0  # e.g. -1.0 °C LWT / °C outdoor
    lwt = high_lwt + slope * (high_c - temp_outdoor_c) + user_offset
    lwt = min(50.0, max(18.0, lwt))

    return (lwt - 18.0) * _KW_PER_DEGC_LWT


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    x = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
