"""Translate MILP :class:`~src.scheduler.lp_optimizer.LpPlan` into Fox V3 groups and Daikin actions."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from .. import db
from ..config import config
from ..foxess.client import FoxESSClient, FoxESSError
from ..foxess.models import SchedulerGroup
from ..weather import HourlyForecast, get_forecast_for_slot
from .lp_optimizer import LpPlan
from .optimizer import (
    TZ,
    HalfHourSlot,
    _bulletproof_allow_peak_export_discharge,
    _merge_fox_groups,
    _optimization_preset_away_like,
)

logger = logging.getLogger(__name__)

EPS = 0.05


def lp_dispatch_slots_for_hardware(plan: LpPlan) -> list[HalfHourSlot]:
    """Half-hour kinds for Fox/Daikin — identical to :func:`lp_plan_to_slots` (pure MILP mapping).

    V7-era "gap bridge" heuristics that promoted ``standard`` slots between charge blocks to
    ``cheap`` are removed: they overrode the solver and extended ForceCharge windows.
    """
    return lp_plan_to_slots(plan)


def lp_plan_to_slots(plan: LpPlan) -> list[HalfHourSlot]:
    """Map per-slot LP flows + prices to ``HalfHourSlot`` kinds for Fox/Daikin dispatch.

    For ForceCharge slots (``cheap`` / ``negative``) we also populate
    ``lp_grid_import_w``: the LP-planned grid import converted to Watts and
    rounded up to the nearest 50 W.  This lets the Fox Scheduler V3 pull only
    as much from the grid as the MILP decided — PV generation and home load are
    already factored in, so we don't request more than needed.

    The value is capped at ``FOX_FORCE_CHARGE_MAX_PWR`` (inverter ceiling) and
    floored at a minimum of 200 W so the inverter doesn't stall.
    """
    out: list[HalfHourSlot] = []
    n = len(plan.slot_starts_utc)
    peak_thr = plan.peak_threshold_pence
    allow_exp = _bulletproof_allow_peak_export_discharge()

    max_pwr_w = int(config.FOX_FORCE_CHARGE_MAX_PWR)
    min_pwr_w = 200  # floor: prevents inverter from interpreting "0 W" as unlimited

    for i in range(n):
        st = plan.slot_starts_utc[i]
        en = st + timedelta(minutes=30)
        price = plan.price_pence[i]
        chg = plan.battery_charge_kwh[i]
        dis = plan.battery_discharge_kwh[i]
        exp = plan.export_kwh[i]
        ed = plan.dhw_electric_kwh[i]
        es = plan.space_electric_kwh[i]

        kind: str = "standard"
        lp_grid_import_w: int | None = None

        if chg > EPS:
            grid_import = plan.import_kwh[i] if plan.import_kwh else 0.0
            if grid_import < EPS:
                kind = "solar_charge"  # PV-only charging — use SelfUse, not ForceCharge
            elif price <= 0:
                kind = "negative"
            else:
                kind = "cheap"
        elif price <= 0:
            # Negative price + LP chose chg ≈ 0 (battery saturated or PV alone
            # suffices). LP hard-forbids dis during negatives — encode that on
            # hardware via Fox's native "Backup" mode (see _slot_fox_tuple).
            kind = "negative_hold"
        elif allow_exp and dis > EPS and exp > EPS:
            kind = "peak_export"
        elif ed < EPS and es < EPS and price >= peak_thr:
            kind = "peak"

        if kind in ("cheap", "negative") and plan.import_kwh:
            # LP import for this slot (kWh over 30 min) → kW → W, rounded up to 50 W
            raw_w = plan.import_kwh[i] * 2 * 1000  # kWh/slot × 2 slots/hr × 1000 W/kW
            rounded_w = int(math.ceil(raw_w / 50.0) * 50)
            lp_grid_import_w = max(min_pwr_w, min(max_pwr_w, rounded_w))

        target_soc_pct: int | None = None
        if plan.soc_kwh and i + 1 < len(plan.soc_kwh):
            cap = float(config.BATTERY_CAPACITY_KWH)
            if cap > 0:
                raw_pct = round(plan.soc_kwh[i + 1] / cap * 100.0)
                target_soc_pct = max(
                    int(config.MIN_SOC_RESERVE_PERCENT),
                    min(100, int(raw_pct)),
                )

        out.append(
            HalfHourSlot(
                start_utc=st,
                end_utc=en,
                price_pence=price,
                kind=kind,
                lp_grid_import_w=lp_grid_import_w,
                target_soc_pct=target_soc_pct,
            )
        )
    return out


def _lwt_offset_from_plan(i: int, plan: LpPlan) -> float:
    """Return the LP-derived LWT offset for slot *i*.

    Uses the back-computed ``plan.lwt_offset_c`` list (filled by ``solve_lp`` via
    ``lwt_offset_from_space_kw``).  This translates the solver's continuous ``e_space[i]``
    decision — bounded by the physical climate curve — into the exact Daikin offset command
    needed to deliver that energy draw.

    Falls back to an indoor-error proportional estimate when the LP output is unavailable
    (e.g. heuristic plan that never filled ``lwt_offset_c``).
    """
    if plan.lwt_offset_c and i < len(plan.lwt_offset_c):
        return float(plan.lwt_offset_c[i])
    # Fallback: proportional to indoor temperature error (heuristic / legacy path)
    if i + 1 >= len(plan.indoor_temp_c):
        return 0.0
    err = float(config.INDOOR_SETPOINT_C) - plan.indoor_temp_c[i + 1]
    raw = max(-1.0, min(1.0, err * 0.6))
    lo = float(config.LWT_OFFSET_MIN)
    hi = float(config.LWT_OFFSET_MAX)
    return max(lo, min(hi, raw * (config.LWT_OFFSET_MAX - config.LWT_OFFSET_MIN) / 10.0))


def _merge_half_hour_slots_for_daikin(plan: LpPlan) -> list[tuple[datetime, datetime, str, int]]:
    """Contiguous same-kind slots → merged windows (start, end, kind, first_slot_index).

    After merging adjacent same-kind slots, any non-standard window that is shorter than
    ``DAIKIN_MIN_WINDOW_SLOTS`` is either:
    * **merged** into the immediately following window of the same kind (if adjacent after
      any interleaving standard gap ≤ 1 slot), or
    * **dropped** — converted back to ``standard`` so no Daikin action is scheduled.

    This filters ultra-short Daikin windows so the heat-pump is not toggled every 30 minutes.
    """
    slots = lp_dispatch_slots_for_hardware(plan)
    if not slots:
        return []
    merged2: list[tuple[datetime, datetime, str, int]] = []
    start_i = 0
    cs, ce, ck = slots[0].start_utc, slots[0].end_utc, slots[0].kind
    for i in range(1, len(slots)):
        s = slots[i]
        if s.kind == ck and s.start_utc == ce:
            ce = s.end_utc
        else:
            merged2.append((cs, ce, ck, start_i))
            start_i = i
            cs, ce, ck = s.start_utc, s.end_utc, s.kind
    merged2.append((cs, ce, ck, start_i))

    min_slots = int(getattr(config, "DAIKIN_MIN_WINDOW_SLOTS", 2))
    if min_slots <= 0:
        return merged2

    # Apply minimum-window filter: windows (non-standard) shorter than min_slots are dropped.
    # After dropping, adjacent same-kind windows that are now consecutive get merged.
    filtered: list[tuple[datetime, datetime, str, int]] = []
    for ws, we, wk, wi in merged2:
        n_slots = round((we - ws).total_seconds() / 1800)
        if wk == "standard" or n_slots >= min_slots:
            filtered.append((ws, we, wk, wi))
        else:
            # Convert too-short non-standard window back to standard (drop action)
            logger.debug(
                "daikin_dispatch: dropping %s window %s–%s (%d slots < min %d)",
                wk, ws.isoformat(), we.isoformat(), n_slots, min_slots,
            )
            filtered.append((ws, we, "standard", wi))

    # Re-merge adjacent same-kind runs that the filter may have made contiguous
    remerged: list[tuple[datetime, datetime, str, int]] = []
    for ws, we, wk, wi in filtered:
        if remerged and remerged[-1][2] == wk and remerged[-1][1] == ws:
            prev_s, prev_e, prev_k, prev_i = remerged.pop()
            remerged.append((prev_s, we, wk, prev_i))
        else:
            remerged.append((ws, we, wk, wi))

    return remerged


def daikin_dispatch_preview(
    plan: LpPlan,
    forecast: list[HourlyForecast],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Same restore+action pairs as :func:`write_daikin_from_lp_plan`, without SQLite.

    Each tuple is ``(restore_row, action_row)`` for one merged window — matches DB write order.
    """
    tz = TZ()
    away_like = _optimization_preset_away_like()
    merged2 = _merge_half_hour_slots_for_daikin(plan)
    buckets = [float(x.strip()) for x in config.DAIKIN_POWER_BUCKETS_KW.split(",") if x.strip()]
    max_b = max(buckets) if buckets else 1.5

    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for start_utc, end_utc, kind, i0 in merged2:
        if kind == "standard":
            continue
        if away_like and kind in ("cheap", "negative"):
            continue
        action_type = {
            "negative": "max_heat",
            "cheap": "pre_heat",
            "peak": "shutdown",
            "peak_export": "shutdown",
        }.get(kind, "normal")

        mid = start_utc + timedelta(minutes=15)
        fc = get_forecast_for_slot(mid, forecast)
        outdoor = fc.temperature_c if fc else (plan.temp_outdoor_c[i0] if i0 < len(plan.temp_outdoor_c) else 0.0)
        peak_frost = kind == "peak" and outdoor < float(config.WEATHER_FROST_THRESHOLD_C)

        j = min(i0, len(plan.dhw_electric_kwh) - 1)
        ed = plan.dhw_electric_kwh[j] if plan.dhw_electric_kwh else 0.0
        es = plan.space_electric_kwh[j] if plan.space_electric_kwh else 0.0
        tt = plan.tank_temp_c[min(j + 1, len(plan.tank_temp_c) - 1)] if plan.tank_temp_c else float(config.DHW_TEMP_NORMAL_C)
        # LP-derived LWT offset: continuous value back-computed from e_space[j] via the
        # inverse climate curve.  This replaces the old fixed-tier overrides so the solver's
        # energy decision directly controls the radiator temperature rather than a lookup table.
        lwt = _lwt_offset_from_plan(j, plan)
        if peak_frost:
            lwt = max(-2.0, float(config.OPTIMIZATION_LWT_OFFSET_MIN))
        tank_pow = ed > EPS
        tank_powful = ed >= max_b - 1e-3
        params: dict[str, Any] = {
            "lwt_offset": round(lwt, 1),  # Daikin rejects sub-0.1 precision; rounds float epsilon to 0.0
            "tank_powerful": tank_powful if kind == "negative" else False,
            "tank_power": tank_pow,
            "climate_on": es > EPS or ed > EPS or kind in ("negative", "cheap"),
            "lp_optimizer": True,
        }
        # Only set tank_temp when the tank will be on — Daikin rejects temperatureControl on a powered-off tank.
        # Floor at DHW_TEMP_COMFORT_C (48 °C); ceiling DHW_TEMP_MAX_C (65 °C).
        # The LP already clamps tt ≤ 48 for positive-price slots and ≤ 65 for negative.
        if tank_pow:
            params["tank_temp"] = round(
                min(float(config.DHW_TEMP_MAX_C), max(float(config.DHW_TEMP_COMFORT_C), tt)),
                1,
            )
        if kind == "peak" or kind == "peak_export":
            params["tank_power"] = False
            params.pop("tank_temp", None)  # tank off — no point setting target temp

        st = start_utc.isoformat().replace("+00:00", "Z")
        en = end_utc.isoformat().replace("+00:00", "Z")
        restore_end = (end_utc + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        restore_params = {
            "lwt_offset": 0.0,
            "tank_powerful": False,
            "tank_temp": float(config.DHW_TEMP_NORMAL_C),
            "tank_power": True,
            "climate_on": True,
        }
        restore_row = {
            "device": "daikin",
            "action_type": "restore",
            "start_time": en,
            "end_time": restore_end,
            "params": restore_params,
        }
        action_row = {
            "device": "daikin",
            "action_type": action_type,
            "start_time": st,
            "end_time": en,
            "params": params,
            "lp_slot_kind": kind,
        }
        out.append((restore_row, action_row))

    return out


def write_daikin_from_lp_plan(
    plan_date: str,
    plan: LpPlan,
    forecast: list[HourlyForecast],
) -> int:
    """Write merged Daikin ``action_schedule`` rows from LP solution."""
    db.clear_actions_for_date(plan_date, device="daikin")
    pairs = daikin_dispatch_preview(plan, forecast)
    count = 0
    for restore_row, action_row in pairs:
        rid = db.upsert_action(
            plan_date=plan_date,
            start_time=restore_row["start_time"],
            end_time=restore_row["end_time"],
            device="daikin",
            action_type="restore",
            params=restore_row["params"],
            status="pending",
        )
        aid = db.upsert_action(
            plan_date=plan_date,
            start_time=action_row["start_time"],
            end_time=action_row["end_time"],
            device="daikin",
            action_type=action_row["action_type"],
            params=action_row["params"],
            status="pending",
            restore_action_id=rid,
        )
        db.update_action_restore_link(aid, rid)
        count += 2

    return count


def build_fox_groups_from_lp(plan: LpPlan) -> list[SchedulerGroup]:
    slots = lp_dispatch_slots_for_hardware(plan)
    peak_export = _bulletproof_allow_peak_export_discharge()
    return _merge_fox_groups(slots, max_groups=8, peak_export_discharge=peak_export)


def upload_fox_if_operational(fox: FoxESSClient | None, groups: list[SchedulerGroup]) -> bool:
    fox_ok = False
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
    return fox_ok
