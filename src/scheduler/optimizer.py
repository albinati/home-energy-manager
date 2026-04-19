"""Target VWAP engine, Fox Scheduler V3 builder, Daikin action_schedule writer."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..config import config
from .. import db
from ..foxess.client import FoxESSClient, FoxESSError
from ..foxess.models import SchedulerGroup
from ..foxess.service import get_cached_realtime
from ..physics import calculate_dhw_setpoint, find_dhw_heat_end_utc, build_shower_target_iso
from ..notifier import push_cheap_window_start, push_peak_window_start
from ..presets import OperationPreset
from ..weather import HourlyForecast, estimate_pv_kw, fetch_forecast, forecast_to_lp_inputs, get_forecast_for_slot

logger = logging.getLogger(__name__)

TZ = lambda: ZoneInfo(config.BULLETPROOF_TIMEZONE)


@dataclass
class HalfHourSlot:
    start_utc: datetime
    end_utc: datetime
    price_pence: float
    kind: str  # negative, cheap, standard, peak


def _parse_ts(s: str) -> datetime:
    x = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _build_half_hour_slots(
    rates: list[dict[str, Any]],
    window_start_local: datetime,
    window_end_local: datetime,
) -> list[HalfHourSlot]:
    """Expand DB rate rows into half-hour slots overlapping the local window."""
    tz = TZ()
    slots: list[HalfHourSlot] = []
    ws = window_start_local.astimezone(timezone.utc)
    we = window_end_local.astimezone(timezone.utc)
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
    return slots


def _classify_slots(slots: list[HalfHourSlot], forecast: list[HourlyForecast]) -> None:
    if not slots:
        return
    prices = [s.price_pence for s in slots]
    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    q25 = prices_sorted[max(0, n // 4 - 1)]
    q75 = prices_sorted[min(n - 1, (3 * n) // 4)]
    bottom10 = prices_sorted[max(0, n // 10 - 1)]
    cheap_thr = min(mean(prices) * 0.85, q25) if n else 0
    peak_thr = max(q75, config.OPTIMIZATION_PEAK_THRESHOLD_PENCE)

    for s in slots:
        fc = get_forecast_for_slot(s.start_utc, forecast)
        solar_boost_skip = fc and fc.estimated_pv_kw > 2.0

        if s.price_pence <= 0 or s.price_pence <= bottom10:
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
) -> tuple[str, Optional[int], Optional[int]]:
    """work_mode, fd_soc, fd_pwr for Scheduler V3 (API uses SelfUse, ForceCharge, ForceDischarge)."""
    if s.kind == "negative":
        return ("ForceCharge", 100, config.FOX_FORCE_CHARGE_MAX_PWR)
    if s.kind == "cheap":
        return ("ForceCharge", 95, config.FOX_FORCE_CHARGE_NORMAL_PWR)
    if s.kind == "peak_export":
        return (
            "ForceDischarge",
            int(config.EXPORT_DISCHARGE_FLOOR_SOC_PERCENT),
            config.FOX_FORCE_CHARGE_MAX_PWR,
        )
    if s.kind == "peak" and peak_export_discharge:
        return (
            "ForceDischarge",
            int(config.EXPORT_DISCHARGE_FLOOR_SOC_PERCENT),
            config.FOX_FORCE_CHARGE_MAX_PWR,
        )
    return ("SelfUse", None, None)


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
            nk: tuple[str, Optional[int], Optional[int]] = (
                "ForceCharge",
                max(ka[1] or 0, kb[1] or 0),
                max(ka[2] or 0, kb[2] or 0),
            )
        else:
            nk = ("SelfUse", None, None)
        merged[0] = (a, d, nk)
        del merged[1]

    groups: list[SchedulerGroup] = []
    for start_utc, end_utc, (wm, fds, fdp) in merged:
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
                min_soc_on_grid=10,
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
            nk: tuple[str, Optional[int], Optional[int]] = (
                "ForceCharge",
                max(k0[1] or 0, k[1] or 0),
                max(k0[2] or 0, k[2] or 0),
            )
            out[-1] = (a0, b, nk)
        else:
            out.append((a, b, k))
    return out


def _coarse_merge_fox(
    merged: list[tuple[datetime, datetime, tuple]],
) -> list[tuple[datetime, datetime, tuple]]:
    """Collapse SelfUse variants."""
    out: list[tuple[datetime, datetime, tuple]] = []
    for a, b, k in merged:
        nk = ("SelfUse", None, None) if k[0] == "SelfUse" else k
        if out and out[-1][2] == nk and out[-1][1] == a:
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
) -> Optional[dict[str, Any]]:
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


def _legionella_active_local(dt_local: datetime) -> bool:
    if dt_local.weekday() != config.DHW_LEGIONELLA_DAY:
        return False
    return config.DHW_LEGIONELLA_HOUR_START <= dt_local.hour < config.DHW_LEGIONELLA_HOUR_END


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
        loc_mid = (start_utc + timedelta(minutes=15)).astimezone(tz)
        # Travel/away: skip cheap/negative preheat unless Legionella window still needs DHW
        if away_like and kind in ("cheap", "negative") and not _legionella_active_local(loc_mid):
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
        if _legionella_active_local(loc_mid):
            params["tank_power"] = True
            params["tank_temp"] = config.DHW_LEGIONELLA_TEMP_C
            params["legionella_override"] = True
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


def _run_optimizer_heuristic(fox: Optional[FoxESSClient], daikin: Optional[Any] = None) -> dict[str, Any]:
    """Legacy price-quantile classifier + Fox/Daikin writers."""
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return {"ok": False, "error": "OCTOPUS_TARIFF_CODE not set"}

    tz = TZ()
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    plan_date = tomorrow.isoformat()
    day_start = datetime.combine(tomorrow, datetime.min.time()).replace(tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    rates = db.get_rates_for_period(
        tariff,
        day_start.astimezone(timezone.utc) - timedelta(hours=1),
        day_end.astimezone(timezone.utc) + timedelta(hours=1),
    )
    if not rates:
        return {"ok": False, "error": "No rates in SQLite for tomorrow — run Octopus fetch first"}

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

    try:
        cheap_slots_local = [s for s in slots if s.kind in ("cheap", "negative")]
        if cheap_slots_local:
            first_cheap = min(cheap_slots_local, key=lambda s: s.start_utc)
            push_cheap_window_start(fox_mode=None)
            logger.info(
                "Push: CHEAP_WINDOW_START first slot %s",
                first_cheap.start_utc.astimezone(tz).strftime("%H:%M"),
            )
        peak_slots_local = [s for s in slots if s.kind == "peak"]
        if peak_slots_local:
            push_peak_window_start(soc=None)
    except Exception as exc:
        logger.debug("Push webhook error (non-fatal): %s", exc)

    db.log_optimizer_run(
        {
            "run_at": datetime.now(timezone.utc).isoformat(),
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


def _run_optimizer_lp(fox: Optional[FoxESSClient], daikin: Optional[Any] = None) -> dict[str, Any]:
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
        return {"ok": False, "error": "No rates in SQLite for tomorrow — run Octopus fetch first"}

    slots = _build_half_hour_slots(rates, day_start, horizon_end)
    if not slots:
        return {"ok": False, "error": "No half-hour slots in LP horizon — check Agile rates coverage"}

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
    # PV calibration: Fox actual vs Open-Meteo archive to correct systematic bias
    from ..weather import compute_pv_calibration_factor
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
    if not plan.ok:
        logger.warning("PuLP status %s — falling back to heuristic classifier", plan.status)
        return _run_optimizer_heuristic(fox, daikin)

    lp_slots = lp_plan_to_slots(plan)
    counts = {"negative": 0, "cheap": 0, "standard": 0, "peak": 0, "peak_export": 0}
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

    try:
        cheap_slots_local = [s for s in lp_slots if s.kind in ("cheap", "negative")]
        if cheap_slots_local:
            push_cheap_window_start(fox_mode=None)
        peak_slots_local = [s for s in lp_slots if s.kind in ("peak", "peak_export")]
        if peak_slots_local:
            push_peak_window_start(soc=None)
    except Exception as exc:
        logger.debug("Push webhook error (non-fatal): %s", exc)

    db.log_optimizer_run(
        {
            "run_at": datetime.now(timezone.utc).isoformat(),
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


def run_optimizer(fox: Optional[FoxESSClient], daikin: Optional[Any] = None) -> dict[str, Any]:
    """Fetch rates from DB, plan (PuLP or heuristic), upload Fox V3, write Daikin actions."""
    backend = (config.OPTIMIZER_BACKEND or "lp").strip().lower()
    if backend == "heuristic":
        return _run_optimizer_heuristic(fox, daikin)
    return _run_optimizer_lp(fox, daikin)

