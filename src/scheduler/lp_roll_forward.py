"""Roll the LP's measured initial state forward to slot 0 (#929).

Slot 0 of the LP is the NEXT half-hour boundary (``optimizer._ceil_to_half_hour_utc``)
but SoC and the tank temperature are read at run time, so they are up to ~29 min
(+ the cache age) stale. Audit of 782 October runs: SoC |err| mean 0.43 kWh /
p95 1.86 / max 3.32 (worst in cheap-band charging), tank |err| mean 0.61 C /
p95 4.0 / max 15.3 (during reheats).

Pure functions only (no I/O) so tests and the validation script share them.

Design choices
--------------
* SoC uses the MEASURED battery power (Fox realtime ``batChargePower`` -
  ``batDischargePower``, positive = charging), not the schedule group: the
  group says what Fox was asked to do, the measurement says what it is doing
  (a ForceCharge capped by the house fuse, a SelfUse group that is idle...).
  The power is held constant over the gap and clamped to the inverter limit;
  the result is clamped to [reserve, capacity] (or to the current SoC when
  already below the reserve - never roll *away* from reality).
* The reading must be fresh (<= ``MAX_POWER_AGE_S``); otherwise no roll.
* Tank: when the pump is visibly reheating (rise between the two latest live
  samples), extrapolate that rate capped at the setpoint; otherwise apply the
  standing-loss ODE of the tank model (#819 UA / ambient).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MAX_POWER_AGE_S = 300.0  # battery power older than 5 min is not trusted
MAX_ROLL_S = 45 * 60.0  # never extrapolate more than 45 min
# A rise of at least this many deg C between the two latest live tank samples
# marks an active reheat (standing loss is ~0.01 C/min; sensor step is 0.5-1 C).
REHEAT_MIN_RISE_C = 0.4
REHEAT_MAX_SAMPLE_GAP_S = 40 * 60.0
REHEAT_MIN_SAMPLE_GAP_S = 60.0
REHEAT_MAX_RATE_C_PER_MIN = 0.6  # ~8 kW thermal into 200 L
# The Daikin tank sensor is quantised to 1 C and the thermostat keeps topping the
# tank up, so a standing-loss correction below this is noise: on the October audit
# (620 non-reheat runs) applying the raw ODE made the slot-0 error WORSE (MAE 0.39 ->
# 0.46 C). Only roll for loss when it is larger than half the sensor step.
STANDING_LOSS_MIN_C = 0.5


@dataclass(frozen=True)
class SocRoll:
    soc_kwh: float
    delta_kwh: float
    applied: bool
    reason: str


def roll_soc_forward(
    soc_kwh: float,
    battery_power_kw: float | None,
    power_age_s: float | None,
    gap_s: float,
    *,
    capacity_kwh: float,
    reserve_kwh: float,
    max_power_kw: float,
    max_power_age_s: float = MAX_POWER_AGE_S,
) -> SocRoll:
    """SoC at slot 0. ``battery_power_kw`` positive = charging. ``gap_s`` is the
    time from the SoC reading to slot 0."""
    if battery_power_kw is None or power_age_s is None:
        return SocRoll(soc_kwh, 0.0, False, "no_power")
    if not (math.isfinite(battery_power_kw) and math.isfinite(power_age_s)):
        return SocRoll(soc_kwh, 0.0, False, "non_finite")
    if power_age_s > max_power_age_s or power_age_s < 0:
        return SocRoll(soc_kwh, 0.0, False, "stale_power")
    if gap_s <= 0:
        return SocRoll(soc_kwh, 0.0, False, "no_gap")
    gap = min(float(gap_s), MAX_ROLL_S)
    p = max(-max_power_kw, min(max_power_kw, float(battery_power_kw)))
    target = soc_kwh + p * gap / 3600.0
    lo = min(soc_kwh, reserve_kwh)  # already below reserve: do not push further down
    hi = capacity_kwh if soc_kwh <= capacity_kwh else soc_kwh
    target = max(lo, min(hi, target))
    return SocRoll(target, target - soc_kwh, True, "ok")


@dataclass(frozen=True)
class TankRoll:
    tank_c: float
    delta_c: float
    applied: bool
    mode: str  # "reheat" | "standing_loss" | "none:<why>"


def standing_loss_tank_c(
    tank_c: float, gap_s: float, *, ua_w_per_k: float, ambient_c: float,
    litres: float, cp_j_per_kg_k: float,
) -> float:
    """Exact solution of C dT/dt = -UA (T - ambient)."""
    c_j_per_k = litres * cp_j_per_kg_k
    if c_j_per_k <= 0 or gap_s <= 0:
        return tank_c
    return ambient_c + (tank_c - ambient_c) * math.exp(-ua_w_per_k * gap_s / c_j_per_k)


def roll_tank_forward(
    tank_c: float,
    gap_s: float,
    *,
    prev_tank_c: float | None,
    sample_gap_s: float | None,
    tank_on: bool | None,
    target_c: float | None,
    ua_w_per_k: float,
    ambient_c: float,
    litres: float,
    cp_j_per_kg_k: float,
) -> TankRoll:
    """Tank temperature at slot 0.

    ``prev_tank_c`` / ``sample_gap_s``: the live sample before the seed and the
    seconds between the two. A rise >= REHEAT_MIN_RISE_C marks a reheat: the
    rate is extrapolated, capped at ``target_c`` - the CALLER passes the cap: the
    telemetry setpoint when it is above the reading, else the hard tank ceiling
    (a rise with the setpoint at the 37 C setback means Powerful/boost is
    running, so the setpoint is not the ceiling). Otherwise standing loss (only
    when above the sensor resolution, see STANDING_LOSS_MIN_C).
    """
    if gap_s <= 0:
        return TankRoll(tank_c, 0.0, False, "none:no_gap")
    gap = min(float(gap_s), MAX_ROLL_S)
    if (
        prev_tank_c is not None
        and sample_gap_s is not None
        and REHEAT_MIN_SAMPLE_GAP_S <= sample_gap_s <= REHEAT_MAX_SAMPLE_GAP_S
        and tank_on is not False
    ):
        rise = tank_c - prev_tank_c
        if rise >= REHEAT_MIN_RISE_C:
            if target_c is None or target_c <= tank_c:
                return TankRoll(tank_c, 0.0, False, "none:reheat_no_headroom")
            rate = min(rise / (sample_gap_s / 60.0), REHEAT_MAX_RATE_C_PER_MIN)
            new = min(float(target_c), tank_c + rate * gap / 60.0)
            return TankRoll(new, new - tank_c, True, "reheat")
    new = standing_loss_tank_c(
        tank_c, gap, ua_w_per_k=ua_w_per_k, ambient_c=ambient_c,
        litres=litres, cp_j_per_kg_k=cp_j_per_kg_k,
    )
    if abs(new - tank_c) < STANDING_LOSS_MIN_C:
        return TankRoll(tank_c, 0.0, False, "none:below_sensor_resolution")
    return TankRoll(new, new - tank_c, True, "standing_loss")


@dataclass(frozen=True)
class ScheduleOnset:
    """A ForceCharge / ForceDischarge group that STARTS after the last battery
    power reading and before slot 0 - the measured power cannot reflect it."""
    offset_s: float  # seconds from the reading to the group start
    power_kw: float  # signed: + charge, - discharge
    target_soc_pct: float | None  # fdSoc (charge: ceiling, discharge: floor)


def find_schedule_onset(groups, reading_t, slot0_t, tz, *, max_power_kw: float) -> ScheduleOnset | None:
    """First ForceCharge/ForceDischarge group whose start falls in
    (reading_t, slot0_t]. ``groups`` = Fox scheduler groups as stored in
    ``fox_schedule_state.groups_json`` (local-time hours/minutes). Contiguous
    groups are not merged: a group that starts at a slot boundary inside the
    window only matters if it begins strictly after the reading."""
    from datetime import datetime, timedelta

    best: ScheduleOnset | None = None
    for g in groups or []:
        try:
            mode = str(g.get("workMode") or "")
            if mode not in ("ForceCharge", "ForceDischarge"):
                continue
            extra = g.get("extraParam") or {}
            pwr = float(extra.get("fdPwr") or 0.0) / 1000.0
            if pwr <= 0:
                continue
            pwr = min(pwr, max_power_kw)
            fd = extra.get("fdSoc")
            sh, sm = int(g["startHour"]), int(g["startMinute"])
        except (KeyError, TypeError, ValueError):
            continue
        base = reading_t.astimezone(tz)
        for day in (-1, 0, 1):
            d = (base + timedelta(days=day)).date()
            start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=tz)
            if reading_t < start <= slot0_t:
                off = (start - reading_t).total_seconds()
                if best is None or off < best.offset_s:
                    best = ScheduleOnset(
                        off, pwr if mode == "ForceCharge" else -pwr,
                        float(fd) if fd is not None else None,
                    )
    return best


def roll_soc_with_onset(
    soc_kwh: float,
    battery_power_kw: float | None,
    power_age_s: float | None,
    gap_s: float,
    onset: ScheduleOnset | None,
    *,
    capacity_kwh: float,
    reserve_kwh: float,
    max_power_kw: float,
    max_power_age_s: float = MAX_POWER_AGE_S,
) -> SocRoll:
    """Like :func:`roll_soc_forward`, but when a scheduled Force* group starts
    between the reading and slot 0 the measured power is used up to the group
    start and the group's ``fdPwr`` (capped at the inverter limit) after it,
    stopping at its ``fdSoc``."""
    base = roll_soc_forward(
        soc_kwh, battery_power_kw, power_age_s, gap_s, capacity_kwh=capacity_kwh,
        reserve_kwh=reserve_kwh, max_power_kw=max_power_kw, max_power_age_s=max_power_age_s,
    )
    if not base.applied or onset is None or gap_s <= 0:
        return base
    gap = min(float(gap_s), MAX_ROLL_S)
    t1 = max(0.0, min(onset.offset_s, gap))
    p = max(-max_power_kw, min(max_power_kw, float(battery_power_kw)))
    s = soc_kwh + p * t1 / 3600.0
    s += onset.power_kw * max(0.0, gap - t1) / 3600.0
    if onset.target_soc_pct is not None:
        lim = capacity_kwh * onset.target_soc_pct / 100.0
        if onset.power_kw > 0:
            s = min(s, max(lim, soc_kwh))
        else:
            s = max(s, min(lim, soc_kwh))
    lo = min(soc_kwh, reserve_kwh)
    hi = capacity_kwh if soc_kwh <= capacity_kwh else soc_kwh
    s = max(lo, min(hi, s))
    return SocRoll(s, s - soc_kwh, True, "ok+schedule_onset")
