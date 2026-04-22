"""Target VWAP engine, Fox Scheduler V3 builder, Daikin action_schedule writer."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..foxess.client import FoxESSClient, FoxESSError
from ..foxess.models import SchedulerGroup
from ..foxess.service import get_cached_realtime
from ..physics import build_shower_target_iso, calculate_dhw_setpoint, find_dhw_heat_end_utc
from ..presets import OperationPreset
from ..weather import (
    HourlyForecast,
    estimate_pv_kw,
    fetch_forecast,
    forecast_to_lp_inputs,
    get_forecast_for_slot,
)

logger = logging.getLogger(__name__)

TZ = lambda: ZoneInfo(config.BULLETPROOF_TIMEZONE)

# Octopus publishes tomorrow's rates ~16:00 UK time.  We call the plan "full-day" when
# we have at least this many half-hour slots for the target day.
_MIN_FULL_DAY_SLOTS = 40


@dataclass
class PlanWindow:
    """Describes the time window the optimizer will plan for."""

    plan_date: str           # ISO date string (YYYY-MM-DD)
    day_start: datetime      # local-tz midnight (or now rounded to next HH slot)
    horizon_end: datetime    # end of planning horizon
    is_full_day: bool        # True = tomorrow's full day, False = today-remainder
    rates: list              # raw rate rows covering the window


def _resolve_plan_window(tariff: str) -> PlanWindow | None:
    """Determine the best available planning window given what rates are in the DB.

    Resolution order:
    1. Tomorrow's full-day rates present (≥ _MIN_FULL_DAY_SLOTS) → plan tomorrow midnight to LP_HORIZON_HOURS.
    2. Today's partial/full rates present → plan from *now* (next half-hour boundary) to local midnight.
    3. No usable rates → return None (caller should activate Self-Use fallback).

    This makes the optimizer work correctly whether called at 09:00 (today's rates only),
    16:30 (just after Octopus published tomorrow), or on-demand at any time.
    """
    tz = TZ()
    now_local = datetime.now(tz)

    # ── Try TOMORROW first ────────────────────────────────────────────────────
    tomorrow = (now_local + timedelta(days=1)).date()
    tmr_start = datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=tz)
    tmr_end = tmr_start + timedelta(hours=int(config.LP_HORIZON_HOURS))

    tmr_q_from = tmr_start.astimezone(UTC) - timedelta(hours=1)
    tmr_q_to = tmr_end.astimezone(UTC) + timedelta(hours=2)
    logger.info(
        "TZ-AUDIT: tomorrow DB query | local midnight %s (%s) | UTC query %s → %s",
        tmr_start.strftime("%a %d %b %H:%M %Z"),
        tmr_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ"),
        tmr_q_from.strftime("%Y-%m-%dT%H:%MZ"),
        tmr_q_to.strftime("%Y-%m-%dT%H:%MZ"),
    )
    tmr_rates = db.get_rates_for_period(tariff, tmr_q_from, tmr_q_to)
    tmr_slots_preview = _build_half_hour_slots(tmr_rates or [], tmr_start, tmr_end)
    if len(tmr_slots_preview) >= _MIN_FULL_DAY_SLOTS:
        logger.info(
            "Plan window: tomorrow %s (%d slots, full-day)",
            tomorrow.isoformat(),
            len(tmr_slots_preview),
        )
        return PlanWindow(
            plan_date=tomorrow.isoformat(),
            day_start=tmr_start,
            horizon_end=tmr_end,
            is_full_day=True,
            rates=tmr_rates,
        )

    # ── Fall back to TODAY-REMAINDER ─────────────────────────────────────────
    today = now_local.date()
    # Round up to the next half-hour boundary
    mins = now_local.minute
    if mins < 30:
        today_start = now_local.replace(minute=30, second=0, microsecond=0)
    else:
        today_start = (now_local + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    today_end = datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(tzinfo=tz)

    if today_start >= today_end:
        # Past midnight — nothing left to plan today
        logger.warning("Plan window: no usable rates (past midnight, tomorrow not published yet)")
        return None

    today_q_from = today_start.astimezone(UTC) - timedelta(minutes=30)
    today_q_to = today_end.astimezone(UTC) + timedelta(hours=1)
    logger.info(
        "TZ-AUDIT: today-remainder DB query | local start %s (%s) end %s (%s) | UTC query %s → %s",
        today_start.strftime("%H:%M %Z"),
        today_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ"),
        today_end.strftime("%H:%M %Z"),
        today_end.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ"),
        today_q_from.strftime("%Y-%m-%dT%H:%MZ"),
        today_q_to.strftime("%Y-%m-%dT%H:%MZ"),
    )
    today_rates = db.get_rates_for_period(tariff, today_q_from, today_q_to)
    today_slots_preview = _build_half_hour_slots(today_rates or [], today_start, today_end)
    if today_slots_preview:
        logger.info(
            "Plan window: today-remainder %s from %s (%d slots, partial-day)",
            today.isoformat(),
            today_start.strftime("%H:%M"),
            len(today_slots_preview),
        )
        return PlanWindow(
            plan_date=today.isoformat(),
            day_start=today_start,
            horizon_end=today_end,
            is_full_day=False,
            rates=today_rates,
        )

    logger.warning("Plan window: no usable rates in DB for today or tomorrow — Self-Use fallback")
    return None


@dataclass
class HalfHourSlot:
    start_utc: datetime
    end_utc: datetime
    price_pence: float
    kind: str  # negative, cheap, standard, peak
    # LP-derived grid import power (W) for this slot — set by the LP path so that
    # ForceCharge windows use the exact amount the MILP planned to pull from the grid
    # rather than a static constant.  None means use the configured fallback constant.
    lp_grid_import_w: int | None = None
    # LP planned battery SoC (%) at end of this slot — used as fd_soc for ForceCharge.
    # None: heuristic plans / missing soc_kwh — fall back to legacy 95 (cheap) / 100 (negative).
    target_soc_pct: int | None = None


def _parse_ts(s: str) -> datetime:
    """Parse an ISO timestamp from agile_rates. All DB values should be UTC Z-normalized.
    If a naive timestamp is encountered, it is assumed UTC and a warning is logged.
    """
    x = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        logger.warning("TZ-AUDIT: naive slot timestamp treated as UTC: %s", s)
        return dt.replace(tzinfo=UTC)
    return dt


def _build_half_hour_slots(
    rates: list[dict[str, Any]],
    window_start_local: datetime,
    window_end_local: datetime,
) -> list[HalfHourSlot]:
    """Expand DB rate rows into half-hour slots overlapping the local window."""
    tz = TZ()
    slots: list[HalfHourSlot] = []
    ws = window_start_local.astimezone(UTC)
    we = window_end_local.astimezone(UTC)
    logger.info(
        "TZ-AUDIT: slot window | local %s → %s | UTC %s → %s",
        window_start_local.strftime("%a %d %b %H:%M %Z"),
        window_end_local.strftime("%a %d %b %H:%M %Z"),
        ws.strftime("%Y-%m-%dT%H:%MZ"),
        we.strftime("%Y-%m-%dT%H:%MZ"),
    )
    for r in rates:
        vf = _parse_ts(str(r["valid_from"]))
        vt = _parse_ts(str(r["valid_to"]))
        price = float(r["value_inc_vat"])
        t = max(vf, ws)
        while t < min(vt, we) and t + timedelta(minutes=30) <= we:
            if t + timedelta(minutes=30) > vt:
                break
            slots.append(
                HalfHourSlot(
                    start_utc=t,
                    end_utc=t + timedelta(minutes=30),
                    price_pence=price,
                    kind="standard",
                )
            )
            t += timedelta(minutes=30)
    slots.sort(key=lambda s: s.start_utc)
    if slots:
        first, last = slots[0], slots[-1]
        logger.info(
            "TZ-AUDIT: %d slots built | first %s UTC (%s local) | last %s UTC (%s local)",
            len(slots),
            first.start_utc.strftime("%Y-%m-%dT%H:%MZ"),
            first.start_utc.astimezone(tz).strftime("%H:%M %Z"),
            last.start_utc.strftime("%Y-%m-%dT%H:%MZ"),
            last.start_utc.astimezone(tz).strftime("%H:%M %Z"),
        )
    return slots


def _classify_slots(slots: list[HalfHourSlot], forecast: list[HourlyForecast]) -> None:
    if not slots:
        return
    prices = [s.price_pence for s in slots]
    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    q25 = prices_sorted[max(0, n // 4 - 1)]
    q75 = prices_sorted[min(n - 1, (3 * n) // 4)]
    cheap_thr = min(mean(prices) * 0.85, q25) if n else 0
    peak_thr = max(q75, config.OPTIMIZATION_PEAK_THRESHOLD_PENCE)

    for s in slots:
        fc = get_forecast_for_slot(s.start_utc, forecast)
        solar_boost_skip = fc and fc.estimated_pv_kw > 2.0

        # "negative" = price is genuinely ≤ 0p (matches the LP path definition).
        # Previously this also caught the bottom-10th-percentile of positive prices,
        # which triggered max_heat and sent false "negative window" alerts to Nikola.
        if s.price_pence <= 0:
            s.kind = "negative"
        elif s.price_pence < cheap_thr:
            s.kind = "cheap" if not solar_boost_skip else "standard"
        elif s.price_pence > peak_thr:
            s.kind = "peak"
        else:
            s.kind = "standard"


def _extend_standard_to_cheap_before_peak(slots: list[HalfHourSlot], slots_to_convert: int) -> int:
    """Nudge extra grid charge in the last standard slots before the first peak window."""
    peak_idx = next((i for i, s in enumerate(slots) if s.kind == "peak"), None)
    if peak_idx is None or peak_idx < 1:
        return 0
    changed = 0
    for i in range(peak_idx - 1, -1, -1):
        if changed >= slots_to_convert:
            break
        if slots[i].kind == "standard":
            slots[i].kind = "cheap"
            changed += 1
    return changed


def _slot_fox_tuple(
    s: HalfHourSlot,
    *,
    peak_export_discharge: bool = False,
) -> tuple[str, int | None, int | None, int]:
    """work_mode, fd_soc, fd_pwr, min_soc_on_grid for Scheduler V3.

    For ForceCharge slots the ``fdPwr`` (W) is taken from ``s.lp_grid_import_w`` when set
    (LP path), or falls back to the configured ``FOX_FORCE_CHARGE_*_PWR`` constants.
    For solar_charge slots (LP import≈0, battery charges from PV) we use SelfUse with an
    elevated minSocOnGrid so the battery is held at the LP's target level without forcing
    any grid import — PV handles the charging naturally.
    """
    min_r = int(config.MIN_SOC_RESERVE_PERCENT)
    if s.kind == "solar_charge":
        # 100%: hold battery fully so PV fills it without the inverter pulling any grid.
        # SelfUse mode forbids active grid import regardless of minSocOnGrid — this only
        # blocks discharge, letting excess PV accumulate. MPC at 06:00/12:00 corrects
        # for cloud shortfalls by switching to ForceCharge if SoC lags the target.
        min_soc = int(getattr(config, "FOX_SOLAR_CHARGE_MIN_SOC_PERCENT", 100))
        return ("SelfUse", None, None, min_soc)
    if s.kind == "negative":
        pwr = s.lp_grid_import_w if s.lp_grid_import_w is not None else config.FOX_FORCE_CHARGE_MAX_PWR
        fds = s.target_soc_pct if s.target_soc_pct is not None else 100
        return ("ForceCharge", fds, pwr, min_r)
    if s.kind == "cheap":
        pwr = s.lp_grid_import_w if s.lp_grid_import_w is not None else config.FOX_FORCE_CHARGE_NORMAL_PWR
        fds = s.target_soc_pct if s.target_soc_pct is not None else 95
        return ("ForceCharge", fds, pwr, min_r)
    if s.kind == "peak_export":
        return (
            "ForceDischarge",
            int(config.EXPORT_DISCHARGE_FLOOR_SOC_PERCENT),
            config.FOX_FORCE_CHARGE_MAX_PWR,
            min_r,
        )
    if s.kind == "peak" and peak_export_discharge:
        return (
            "ForceDischarge",
            int(config.EXPORT_DISCHARGE_FLOOR_SOC_PERCENT),
            config.FOX_FORCE_CHARGE_MAX_PWR,
            min_r,
        )
    return ("SelfUse", None, None, min_r)


def _optimization_preset_away_like() -> bool:
    """True when household preset is travel or away (hibernate / export-friendly)."""
    try:
        p = OperationPreset((config.OPTIMIZATION_PRESET or "normal").strip().lower())
        return p in (OperationPreset.TRAVEL, OperationPreset.AWAY)
    except ValueError:
        return False


def _bulletproof_allow_peak_export_discharge() -> bool:
    """True only when not strict_savings, preset travel/away, and cached SoC high enough."""
    if (config.ENERGY_STRATEGY_MODE or "savings_first").strip().lower() == "strict_savings":
        return False
    if not _optimization_preset_away_like():
        return False
    try:
        soc = float(get_cached_realtime().soc)
    except Exception:
        return False
    return soc >= float(config.EXPORT_DISCHARGE_MIN_SOC_PERCENT)


def _merge_fox_groups(
    slots: list[HalfHourSlot],
    max_groups: int = 8,
    *,
    peak_export_discharge: bool = False,
) -> list[SchedulerGroup]:
    if not slots:
        return []
    tz = TZ()
    merged: list[tuple[datetime, datetime, tuple]] = []
    cur_start = slots[0].start_utc
    cur_end = slots[0].end_utc
    cur_key = _slot_fox_tuple(slots[0], peak_export_discharge=peak_export_discharge)
    for s in slots[1:]:
        k = _slot_fox_tuple(s, peak_export_discharge=peak_export_discharge)
        if k == cur_key and s.start_utc == cur_end:
            cur_end = s.end_utc
        else:
            merged.append((cur_start, cur_end, cur_key))
            cur_start = s.start_utc
            cur_end = s.end_utc
            cur_key = k
    merged.append((cur_start, cur_end, cur_key))

    merged = _merge_adjacent_force_charge_rows(merged)

    guard = 0
    while len(merged) > max_groups and len(merged) >= 2 and guard < 64:
        guard += 1
        merged = _coarse_merge_fox(merged)
        if len(merged) <= max_groups:
            break
        merged_pair = False
        for j in range(len(merged) - 1):
            if merged[j][2] == merged[j + 1][2]:
                a, _, k = merged[j]
                _, d, _ = merged[j + 1]
                merged[j] = (a, d, k)
                del merged[j + 1]
                merged_pair = True
                break
        if merged_pair:
            continue
        a, _, ka = merged[0]
        _, d, kb = merged[1]
        if ka[0] == "ForceCharge" and kb[0] == "ForceCharge":
            nk = (
                "ForceCharge",
                max(ka[1] or 0, kb[1] or 0),
                max(ka[2] or 0, kb[2] or 0),
                max(ka[3], kb[3]),
            )
        else:
            nk = ("SelfUse", None, None, int(config.MIN_SOC_RESERVE_PERCENT))
        merged[0] = (a, d, nk)
        del merged[1]

    groups: list[SchedulerGroup] = []
    for start_utc, end_utc, (wm, fds, fdp, msg) in merged:
        ls = start_utc.astimezone(tz)
        le = end_utc.astimezone(tz)
        eh, em = le.hour, le.minute
        if em == 0 and le.second == 0:
            le_adj = le - timedelta(minutes=1)
            eh, em = le_adj.hour, le_adj.minute
        groups.append(
            SchedulerGroup(
                start_hour=ls.hour,
                start_minute=ls.minute,
                end_hour=eh,
                end_minute=em,
                work_mode=wm,
                min_soc_on_grid=msg,
                fd_soc=fds,
                fd_pwr=fdp,
            )
        )
    return groups


def _merge_adjacent_force_charge_rows(
    merged: list[tuple[datetime, datetime, tuple]],
) -> list[tuple[datetime, datetime, tuple]]:
    """Join consecutive ForceCharge segments even when fdSoc/fdPwr differ (e.g. negative vs cheap slot)."""
    out: list[tuple[datetime, datetime, tuple]] = []
    for a, b, k in merged:
        if (
            out
            and out[-1][2][0] == "ForceCharge"
            and k[0] == "ForceCharge"
        ):
            a0, _, k0 = out[-1]
            nk = (
                "ForceCharge",
                max(k0[1] or 0, k[1] or 0),
                max(k0[2] or 0, k[2] or 0),
                max(k0[3], k[3]),
            )
            out[-1] = (a0, b, nk)
        else:
            out.append((a, b, k))
    return out


def _coarse_merge_fox(
    merged: list[tuple[datetime, datetime, tuple]],
) -> list[tuple[datetime, datetime, tuple]]:
    """Collapse SelfUse variants; preserve highest minSocOnGrid when merging solar_charge windows."""
    out: list[tuple[datetime, datetime, tuple]] = []
    for a, b, k in merged:
        nk = ("SelfUse", None, None, k[3]) if k[0] == "SelfUse" else k
        if out and out[-1][2][0] == "SelfUse" and nk[0] == "SelfUse" and out[-1][1] == a:
            prev_msg = out[-1][2][3]
            merged_msg = max(prev_msg, nk[3])
            out[-1] = (out[-1][0], b, ("SelfUse", None, None, merged_msg))
        elif out and out[-1][2] == nk and out[-1][1] == a:
            out[-1] = (out[-1][0], b, nk)
        else:
            out.append((a, b, nk))
    return out


def _consolidate_fox_charge_block(
    slots: list[HalfHourSlot],
    tz: ZoneInfo,
    overnight_start_h: int = 23,
    overnight_end_h: int = 7,
) -> None:
    """Promote isolated SelfUse slots sandwiched inside a ForceCharge run to 'cheap'
    so that the Fox scheduler sees a single solid overnight charging block.

    The overnight window wraps midnight: ``overnight_start_h`` (e.g. 23) through
    ``overnight_end_h`` (e.g. 7) the next morning.

    Only fills gaps of ≤ 3 consecutive SelfUse slots to avoid charging during expensive
    standard hours outside the overnight window.
    """
    _MAX_GAP_SLOTS = 3

    in_overnight = []
    for s in slots:
        local_h = s.start_utc.astimezone(tz).hour
        if local_h >= overnight_start_h or local_h < overnight_end_h:
            in_overnight.append(s)

    if not in_overnight:
        return

    # Find first and last ForceCharge slot within window
    charge_indices = [
        i for i, s in enumerate(in_overnight) if s.kind in ("cheap", "negative")
    ]
    if len(charge_indices) < 2:
        return

    first_ci = charge_indices[0]
    last_ci = charge_indices[-1]

    # Fill isolated standard/SelfUse gaps between first and last charge slot
    gap_run = 0
    for i in range(first_ci, last_ci + 1):
        s = in_overnight[i]
        if s.kind in ("cheap", "negative"):
            gap_run = 0
        else:
            gap_run += 1
            if gap_run <= _MAX_GAP_SLOTS:
                s.kind = "cheap"
            else:
                # Gap too large — stop filling (leave expensive island alone)
                break


def _schedule_dhw_thermal_decay(
    plan_date: str,
    slots: list[HalfHourSlot],
    tz: ZoneInfo,
    *,
    target_temp_c: float = 45.0,
    shower_hour: int = 9,
    shower_minute: int = 30,
) -> dict[str, Any] | None:
    """Calculate the physics-optimal Daikin DHW setpoint for the morning shower target.

    Finds the latest cheap/negative slot in the 02:00–07:00 local window,
    computes thermal decay from heat-end to shower time, and writes a
    dedicated Daikin ``dhw_thermal_target`` action to the schedule.

    Returns a summary dict (or None if no overnight cheap slots found).
    """
    heat_end_utc = find_dhw_heat_end_utc(slots, overnight_start_h=2, overnight_end_h=7, tz=tz)
    if heat_end_utc is None:
        return None

    shower_iso = build_shower_target_iso(plan_date, hour=shower_hour, minute=shower_minute, tz=tz)
    setpoint = calculate_dhw_setpoint(
        target_temp_c=target_temp_c,
        target_time_iso=shower_iso,
        heat_end_time_iso=heat_end_utc.isoformat().replace("+00:00", "Z"),
    )

    # Write a dedicated Daikin action that overrides tank_temp with the computed setpoint.
    # The action covers the last cheap heating slot only (fine-grained override).
    heat_start_utc = heat_end_utc - timedelta(minutes=30)
    start_iso = heat_start_utc.isoformat().replace("+00:00", "Z")
    end_iso = heat_end_utc.isoformat().replace("+00:00", "Z")

    params: dict[str, Any] = {
        "lwt_offset": min(config.LWT_OFFSET_PREHEAT_BOOST, config.LWT_OFFSET_MAX),
        "tank_powerful": False,
        "tank_temp": setpoint,
        "tank_power": True,
        "climate_on": True,
        "dhw_thermal_decay_setpoint": setpoint,
        "shower_target_temp_c": target_temp_c,
        "shower_target_time": shower_iso,
        "heat_end_time": end_iso,
    }
    db.upsert_action(
        plan_date=plan_date,
        start_time=start_iso,
        end_time=end_iso,
        device="daikin",
        action_type="dhw_thermal_target",
        params=params,
        status="pending",
    )
    return {
        "setpoint_c": setpoint,
        "heat_end_utc": end_iso,
        "shower_target_utc": shower_iso,
        "target_temp_c": target_temp_c,
    }


def _daikin_params_for_kind(kind: str, peak_frost: bool) -> dict[str, Any]:
    if kind == "negative":
        return {
            "lwt_offset": config.LWT_OFFSET_MAX,
            "tank_powerful": True,
            "tank_temp": config.DHW_TEMP_MAX_C,
            "tank_power": True,
            "climate_on": True,
        }
    if kind == "cheap":
        return {
            "lwt_offset": min(config.LWT_OFFSET_PREHEAT_BOOST, config.LWT_OFFSET_MAX),
            "tank_powerful": False,  # V2: disable tank_powerful on cheap slots (save demand)
            "tank_temp": config.DHW_TEMP_CHEAP_C,
            "tank_power": True,
            "climate_on": True,
        }
    if kind == "peak":
        return {
            "lwt_offset": -2.0 if peak_frost else config.LWT_OFFSET_MIN,
            "tank_powerful": False,
            "tank_temp": config.DHW_TEMP_NORMAL_C,
            "tank_power": False,
            "climate_on": True,
        }
    return {
        "lwt_offset": 0.0,
        "tank_powerful": False,
        "tank_temp": config.DHW_TEMP_NORMAL_C,
        "tank_power": True,
        "climate_on": True,
    }


def _normal_params() -> dict[str, Any]:
    return _daikin_params_for_kind("standard", False)


def _write_daikin_schedule(plan_date: str, slots: list[HalfHourSlot], forecast: list[HourlyForecast]) -> int:
    db.clear_actions_for_date(plan_date, device="daikin")
    tz = TZ()
    count = 0
    away_like = _optimization_preset_away_like()
    merged: list[tuple[datetime, datetime, str]] = []
    if not slots:
        return 0
    cs, ce, ck = slots[0].start_utc, slots[0].end_utc, slots[0].kind
    for s in slots[1:]:
        if s.kind == ck and s.start_utc == ce:
            ce = s.end_utc
        else:
            merged.append((cs, ce, ck))
            cs, ce, ck = s.start_utc, s.end_utc, s.kind
    merged.append((cs, ce, ck))

    for start_utc, end_utc, kind in merged:
        if kind in ("standard",):
            continue
        # Travel/away: skip cheap/negative preheat entirely (Daikin owns legionella).
        if away_like and kind in ("cheap", "negative"):
            continue
        action_type = {
            "negative": "max_heat",
            "cheap": "pre_heat",
            "peak": "shutdown",
        }.get(kind, "normal")
        fc = get_forecast_for_slot(start_utc + timedelta(minutes=15), forecast)
        outdoor = fc.temperature_c if fc else 0.0
        peak_frost = kind == "peak" and outdoor < config.WEATHER_FROST_THRESHOLD_C
        params = _daikin_params_for_kind(
            "negative" if kind == "negative" else ("cheap" if kind == "cheap" else ("peak" if kind == "peak" else "standard")),
            peak_frost,
        )
        st = start_utc.isoformat().replace("+00:00", "Z")
        en = end_utc.isoformat().replace("+00:00", "Z")
        restore_end = (end_utc + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        restore_params = _normal_params()
        rid = db.upsert_action(
            plan_date=plan_date,
            start_time=en,
            end_time=restore_end,
            device="daikin",
            action_type="restore",
            params=restore_params,
            status="pending",
        )
        aid = db.upsert_action(
            plan_date=plan_date,
            start_time=st,
            end_time=en,
            device="daikin",
            action_type=action_type,
            params=params,
            status="pending",
            restore_action_id=rid,
        )
        db.update_action_restore_link(aid, rid)
        count += 2
    return count


def _run_optimizer_heuristic(fox: FoxESSClient | None, daikin: Any | None = None) -> dict[str, Any]:
    """Legacy price-quantile classifier + Fox/Daikin writers."""
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}

    tz = TZ()
    window = _resolve_plan_window(tariff)
    if window is None:
        return _self_use_fallback(fox, reason="No Agile rates available for today or tomorrow")

    plan_date = window.plan_date
    day_start = window.day_start
    day_end = window.horizon_end

    rates = window.rates
    forecast = fetch_forecast(hours=48)
    slots = _build_half_hour_slots(rates, day_start, day_end)
    _classify_slots(slots, forecast)
    _consolidate_fox_charge_block(slots, tz)

    mu_load = db.mean_consumption_kwh_from_execution_logs()
    peak_hours_pre = sum(1 for s in slots if s.kind == "peak") * 0.5
    est_peak_kwh = peak_hours_pre * mu_load * 1.2
    battery_warn = est_peak_kwh > config.BATTERY_CAPACITY_KWH * 0.85
    extended = 0
    if battery_warn:
        extended = _extend_standard_to_cheap_before_peak(slots, 3)

    counts = {"negative": 0, "cheap": 0, "standard": 0, "peak": 0}
    for s in slots:
        counts[s.kind] = counts.get(s.kind, 0) + 1

    prices = [s.price_pence for s in slots]
    actual_mean = mean(prices) if prices else 0.0
    loads = [mu_load] * len(slots)
    total_kwh = sum(loads)
    target_vwap = sum(p * l for p, l in zip(prices, loads)) / total_kwh if total_kwh else actual_mean

    temps = [f.temperature_c for f in forecast] if forecast else []
    solar_kwh = sum(
        max(0.0, estimate_pv_kw(f.shortwave_radiation_wm2, config.PV_CAPACITY_KWP, config.PV_SYSTEM_EFFICIENCY))
        * (1.0 / 2.0)
        for f in forecast[:24]
    )

    cheap_thr = sorted(prices)[max(0, len(prices) // 4 - 1)] if prices else 0
    peak_thr = sorted(prices)[min(len(prices) - 1, (3 * len(prices)) // 4)] if prices else 0

    strategy = (
        f"{plan_date}: neg={counts['negative']} cheap={counts['cheap']} "
        f"std={counts['standard']} peak={counts['peak']} slots; mean {actual_mean:.1f}p"
    )
    if _optimization_preset_away_like():
        strategy += "; Daikin: travel/away — scheduled setbacks on peak only (no cheap/negative preheat)"
    if extended:
        strategy += f"; pre-peak charge extended +{extended} half-hours (battery margin)"
    peak_export = _bulletproof_allow_peak_export_discharge()
    if peak_export:
        strategy += (
            f"; peak export discharge allowed (travel/away, SoC≥{config.EXPORT_DISCHARGE_MIN_SOC_PERCENT:g}%)"
        )
    if battery_warn:
        strategy += (
            f"; battery warn: est peak load ~{est_peak_kwh:.1f}kWh vs "
            f"~{config.BATTERY_CAPACITY_KWH * 0.85:.1f}kWh usable"
        )
    svt = float(config.SVT_RATE_PENCE)
    naive_svt_cost = total_kwh * svt
    naive_agile_cost = total_kwh * actual_mean
    savings_vs_svt_pence = max(0.0, naive_svt_cost - naive_agile_cost)
    strategy += f"; indicative vs SVT ~{savings_vs_svt_pence / 100:.2f} GBP/day at mean Agile"

    db.save_daily_target(
        {
            "date": plan_date,
            "target_vwap": target_vwap,
            "estimated_total_kwh": total_kwh,
            "estimated_cost_pence": target_vwap * total_kwh,
            "cheap_threshold": cheap_thr,
            "peak_threshold": peak_thr,
            "forecast_min_temp_c": min(temps) if temps else None,
            "forecast_max_temp_c": max(temps) if temps else None,
            "forecast_total_solar_kwh": solar_kwh,
            "strategy_summary": strategy,
        }
    )

    fox_ok = False
    groups = _merge_fox_groups(slots, max_groups=8, peak_export_discharge=peak_export)
    if fox and fox.api_key and config.OPERATION_MODE == "operational" and not config.OPENCLAW_READ_ONLY:
        try:
            fox.set_scheduler_v3(groups, is_default=False)
            fox.warn_if_scheduler_v3_mismatch(groups)
            fox.set_scheduler_flag(True)
            fox_ok = True
            db.save_fox_schedule_state([g.to_api_dict() for g in groups], enabled=True)
        except FoxESSError as e:
            logger.warning("Fox Scheduler V3 upload failed: %s", e)
    elif fox and fox.api_key:
        logger.info("Skipping Fox Scheduler V3 upload (read-only or simulation)")

    daikin_n = _write_daikin_schedule(plan_date, slots, forecast)

    thermal_info = _schedule_dhw_thermal_decay(plan_date, slots, tz)
    if thermal_info:
        strategy += (
            f"; DHW thermal target: {thermal_info['setpoint_c']}°C "
            f"(decay-compensated for 09:30 shower)"
        )
        daikin_n += 1

    db.log_optimizer_run(
        {
            "run_at": datetime.now(UTC).isoformat(),
            "rates_count": len(slots),
            "cheap_slots": counts["cheap"],
            "peak_slots": counts["peak"],
            "standard_slots": counts["standard"],
            "negative_slots": counts["negative"],
            "target_vwap": target_vwap,
            "actual_agile_mean": actual_mean,
            "battery_warning": battery_warn,
            "strategy_summary": strategy,
            "fox_schedule_uploaded": fox_ok,
            "daikin_actions_count": daikin_n,
        }
    )

    _write_plan_consent(plan_date, strategy)

    return {
        "ok": True,
        "plan_date": plan_date,
        "target_vwap": target_vwap,
        "counts": counts,
        "fox_uploaded": fox_ok,
        "daikin_actions": daikin_n,
        "battery_warning": battery_warn,
        "peak_export_discharge": peak_export,
        "strategy": strategy,
        "dhw_thermal_decay": thermal_info,
        "optimizer_backend": "heuristic",
    }


def _run_optimizer_lp(fox: FoxESSClient | None, daikin: Any | None = None) -> dict[str, Any]:
    """PuLP MILP horizon planner (V8). Falls back to heuristic if solve is not optimal."""
    from .lp_dispatch import (
        build_fox_groups_from_lp,
        lp_plan_to_slots,
        upload_fox_if_operational,
        write_daikin_from_lp_plan,
    )
    from .lp_initial_state import read_lp_initial_state
    from .lp_optimizer import solve_lp

    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}

    tz = TZ()
    window = _resolve_plan_window(tariff)
    if window is None:
        return _self_use_fallback(fox, reason="No Agile rates available for today or tomorrow")

    plan_date = window.plan_date
    day_start = window.day_start
    horizon_end = window.horizon_end

    rates = window.rates
    slots = _build_half_hour_slots(rates, day_start, horizon_end)
    if not slots:
        return _self_use_fallback(fox, reason="No half-hour slots in LP horizon — check Agile rates coverage")

    # Per-slot load profile (hour-of-day bins from execution_log)
    _profile_limit = int(getattr(config, "LP_LOAD_PROFILE_SLOTS", 2016))
    _load_profile = db.hourly_load_profile_kwh(limit=_profile_limit)
    # Fall back to Fox daily mean when execution_log is cold
    _fox_mean = db.mean_fox_load_kwh_per_slot(limit=60)
    _flat = _fox_mean if _fox_mean is not None else db.mean_consumption_kwh_from_execution_logs(limit=_profile_limit)
    base_load = [_load_profile.get(s.start_utc.astimezone(tz).hour, _flat) for s in slots]
    mu_load = sum(base_load) / len(base_load) if base_load else 0.4
    prices = [s.price_pence for s in slots]
    starts = [s.start_utc for s in slots]

    forecast = fetch_forecast(hours=max(48, int(config.LP_HORIZON_HOURS) + 24))
    # Persist forecast to DB so heartbeat can read real Open-Meteo temp (vs Daikin sensor)
    if forecast:
        _today = datetime.now(UTC).date().isoformat()
        db.save_meteo_forecast(
            [
                {
                    "slot_time": f.time_utc.isoformat(),
                    "temp_c": f.temperature_c,
                    "solar_w_m2": f.shortwave_radiation_wm2,
                }
                for f in forecast
            ],
            _today,
        )
    # PV calibration: Fox actual vs Open-Meteo archive to correct systematic bias
    from ..weather import compute_pv_calibration_factor
    pv_scale = compute_pv_calibration_factor()
    weather = forecast_to_lp_inputs(forecast, starts, pv_scale=pv_scale)
    initial = read_lp_initial_state(daikin)
    micro_climate_offset = db.get_micro_climate_offset_c(config.DAIKIN_MICRO_CLIMATE_LOOKBACK)

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=initial,
        tz=tz,
        micro_climate_offset_c=micro_climate_offset,
    )
    if not plan.ok:
        logger.warning("PuLP status %s — falling back to heuristic classifier", plan.status)
        return _run_optimizer_heuristic(fox, daikin)

    lp_slots = lp_plan_to_slots(plan)
    counts = {"negative": 0, "cheap": 0, "solar_charge": 0, "standard": 0, "peak": 0, "peak_export": 0}
    for s in lp_slots:
        counts[s.kind] = counts.get(s.kind, 0) + 1

    actual_mean = mean(prices) if prices else 0.0
    total_kwh = sum(base_load)
    target_vwap = float(plan.objective_pence) / total_kwh if total_kwh > 0 else actual_mean

    temps = [f.temperature_c for f in forecast] if forecast else []
    solar_kwh = sum(weather.pv_kwh_per_slot) if weather.pv_kwh_per_slot else 0.0

    peak_hours_pre = sum(1 for s in lp_slots if s.kind == "peak") * 0.5
    est_peak_kwh = peak_hours_pre * mu_load * 1.2
    battery_warn = est_peak_kwh > config.BATTERY_CAPACITY_KWH * 0.85

    strategy = (
        f"{plan_date}: PuLP MILP objective ~{plan.objective_pence:.0f}p; "
        f"neg={counts.get('negative', 0)} cheap={counts.get('cheap', 0)} "
        f"solar={counts.get('solar_charge', 0)} "
        f"std={counts.get('standard', 0)} peak={counts.get('peak', 0)} "
        f"peak_export={counts.get('peak_export', 0)}; mean Agile {actual_mean:.1f}p"
    )
    if _optimization_preset_away_like():
        strategy += "; Daikin: travel/away — setbacks predominate when LP chooses peak slots"
    peak_export = _bulletproof_allow_peak_export_discharge()
    if peak_export:
        strategy += (
            f"; peak export discharge allowed (travel/away, SoC≥{config.EXPORT_DISCHARGE_MIN_SOC_PERCENT:g}%)"
        )
    if battery_warn:
        strategy += (
            f"; battery warn: est peak load ~{est_peak_kwh:.1f}kWh vs "
            f"~{config.BATTERY_CAPACITY_KWH * 0.85:.1f}kWh usable"
        )
    svt = float(config.SVT_RATE_PENCE)
    naive_svt_cost = total_kwh * svt
    naive_agile_cost = total_kwh * actual_mean
    savings_vs_svt_pence = max(0.0, naive_svt_cost - naive_agile_cost)
    strategy += f"; indicative vs SVT ~{savings_vs_svt_pence / 100:.2f} GBP/day at mean Agile"

    db.save_daily_target(
        {
            "date": plan_date,
            "target_vwap": target_vwap,
            "estimated_total_kwh": total_kwh,
            "estimated_cost_pence": plan.objective_pence,
            "cheap_threshold": plan.cheap_threshold_pence,
            "peak_threshold": plan.peak_threshold_pence,
            "forecast_min_temp_c": min(temps) if temps else None,
            "forecast_max_temp_c": max(temps) if temps else None,
            "forecast_total_solar_kwh": solar_kwh,
            "strategy_summary": strategy,
        }
    )

    groups = build_fox_groups_from_lp(plan)
    fox_ok = upload_fox_if_operational(fox, groups)
    daikin_n = write_daikin_from_lp_plan(plan_date, plan, forecast)

    db.log_optimizer_run(
        {
            "run_at": datetime.now(UTC).isoformat(),
            "rates_count": len(slots),
            "cheap_slots": counts.get("cheap", 0),
            "peak_slots": counts.get("peak", 0) + counts.get("peak_export", 0),
            "standard_slots": counts.get("standard", 0),
            "negative_slots": counts.get("negative", 0),
            "target_vwap": target_vwap,
            "actual_agile_mean": actual_mean,
            "battery_warning": battery_warn,
            "strategy_summary": strategy,
            "fox_schedule_uploaded": fox_ok,
            "daikin_actions_count": daikin_n,
        }
    )

    _write_plan_consent(plan_date, strategy)

    return {
        "ok": True,
        "plan_date": plan_date,
        "target_vwap": target_vwap,
        "counts": counts,
        "fox_uploaded": fox_ok,
        "daikin_actions": daikin_n,
        "battery_warning": battery_warn,
        "peak_export_discharge": peak_export,
        "strategy": strategy,
        "dhw_thermal_decay": None,
        "optimizer_backend": "lp",
        "lp_objective_pence": plan.objective_pence,
        "lp_status": plan.status,
    }


def _self_use_fallback(fox: FoxESSClient | None, reason: str = "No rates") -> dict[str, Any]:
    """Last-resort: set Fox to Self Use and log.  Called when no rates are available."""
    logger.warning("Self-Use fallback triggered: %s", reason)
    if fox and fox.api_key and config.OPERATION_MODE == "operational" and not config.OPENCLAW_READ_ONLY:
        try:
            fox.set_work_mode("Self Use")
            fox.set_min_soc(10)
            db.log_action(
                device="foxess",
                action="self_use_fallback",
                params={"reason": reason},
                result="success",
                trigger="optimizer",
            )
        except Exception as e:
            logger.warning("Self-Use fallback Fox call failed: %s", e)
    return {"ok": False, "error": reason, "fallback": "self_use"}


def _write_plan_consent(plan_date: str, strategy: str) -> None:
    """Write a plan_consent row and send the PLAN_PROPOSED notification.

    Idempotency rules:
    - If the plan is already approved/rejected, skip re-notifying and re-upsert only if
      the plan content changed (new hash).
    - If the plan is pending with the same hash, skip (no-op — avoid duplicate notifications).
    - A cooldown (PLAN_REGEN_COOLDOWN_SECONDS) prevents rapid successive re-planning.
    - When PLAN_AUTO_APPROVE=true, plans are immediately approved and notification uses
      "auto-applied" prefix instead of asking for approval.
    """
    import hashlib

    from ..notifier import notify_plan_proposed
    plan_id = f"lp-{plan_date}"

    # Compute a short content hash from the strategy string
    plan_hash = hashlib.sha1(strategy.encode("utf-8")).hexdigest()[:12]

    # Check for an existing consent row
    existing = db.get_plan_consent(plan_date)
    if existing:
        existing_status = existing.get("status", "")
        existing_hash = existing.get("plan_hash")

        # Hard idempotency: don't clobber an already-approved or rejected plan
        # unless the plan content changed meaningfully.
        if existing_status in ("approved", "rejected"):
            if existing_hash == plan_hash:
                logger.info(
                    "Plan %s already %s with same content — skipping re-consent",
                    plan_id, existing_status,
                )
                return
            # Content changed (new rates / re-plan after reject) — proceed normally
            logger.info(
                "Plan %s was %s but content changed (hash %s→%s) — re-proposing",
                plan_id, existing_status, existing_hash, plan_hash,
            )

        # Duplicate suppression: pending + same hash = no-op
        elif existing_status == "pending_approval" and existing_hash == plan_hash:
            logger.info(
                "Plan %s already pending with same content — skipping duplicate notification",
                plan_id,
            )
            return

    # Cooldown guard (in-process, keyed by plan_date)
    cooldown_s = int(getattr(config, "PLAN_REGEN_COOLDOWN_SECONDS", 300))
    if existing and cooldown_s > 0:
        age_s = time.time() - float(existing.get("proposed_at", 0))
        if age_s < cooldown_s and existing.get("plan_hash") == plan_hash:
            logger.info(
                "Plan %s cooldown active (%.0fs remaining) — skipping re-notify",
                plan_id, cooldown_s - age_s,
            )
            return

    expires_at = time.time() + config.PLAN_CONSENT_EXPIRY_SECONDS

    if config.PLAN_AUTO_APPROVE:
        db.upsert_plan_consent(
            plan_id=plan_id,
            plan_date=plan_date,
            summary=strategy,
            expires_at=expires_at,
            plan_hash=plan_hash,
        )
        db.approve_plan(plan_id)
        logger.info("Plan %s auto-approved (PLAN_AUTO_APPROVE=true)", plan_id)
        # Notify with auto-applied prefix so the user knows the plan went live
        try:
            actions = db.get_actions_for_plan_date(plan_date)
            notify_plan_proposed(
                plan_id=plan_id,
                plan_date=plan_date,
                summary=f"[AUTO-APPLIED] {strategy}",
                actions=actions,
            )
        except Exception as exc:
            logger.warning("notify_plan_proposed (auto-applied) failed (non-fatal): %s", exc)
        return

    db.upsert_plan_consent(
        plan_id=plan_id,
        plan_date=plan_date,
        summary=strategy,
        expires_at=expires_at,
        plan_hash=plan_hash,
    )
    try:
        actions = db.get_actions_for_plan_date(plan_date)
        notify_plan_proposed(
            plan_id=plan_id,
            plan_date=plan_date,
            summary=strategy,
            actions=actions,
        )
    except Exception as exc:
        logger.warning("notify_plan_proposed failed (non-fatal): %s", exc)


def run_optimizer(fox: FoxESSClient | None, daikin: Any | None = None) -> dict[str, Any]:
    """Fetch rates from DB, plan (PuLP or heuristic), upload Fox V3, write Daikin actions."""
    backend = (config.OPTIMIZER_BACKEND or "lp").strip().lower()
    if backend == "heuristic":
        return _run_optimizer_heuristic(fox, daikin)
    return _run_optimizer_lp(fox, daikin)
