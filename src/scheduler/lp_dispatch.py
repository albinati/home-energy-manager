"""Translate MILP :class:`~src.scheduler.lp_optimizer.LpPlan` into Fox V3 groups and Daikin actions."""
from __future__ import annotations

import dataclasses
import logging
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .. import db
from ..config import config
from ..foxess.client import FoxESSClient, FoxESSError
from ..foxess.models import SchedulerGroup
from ..weather import HourlyForecast, get_forecast_for_slot
from .lp_optimizer import LpPlan
from .optimizer import (
    TZ,
    HalfHourSlot,
    _merge_fox_groups,
    _optimization_preset_away_like,
)

if TYPE_CHECKING:
    from .scenarios import Scenario

logger = logging.getLogger(__name__)

EPS = 0.05


def _compute_rank_percentiles(values: list[float]) -> list[float]:
    """Per-slot percentile rank of ``values`` (0–100, higher = bigger value).

    Used to audit ``where in the LP horizon's Outgoing-rate distribution did
    this slot sit?`` — the metric tracked on every ``dispatch_decisions`` row
    per #274. Ties get the average rank so a flat day where every slot ties
    still resolves to 50, not 0 or 100. Empty input → empty list.
    """
    if not values:
        return []
    n = len(values)
    sorted_pairs = sorted((float(v), i) for i, v in enumerate(values))
    rank_sum: dict[int, float] = {}
    rank_count: dict[int, int] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_pairs[j + 1][0] == sorted_pairs[i][0]:
            j += 1
        # Ranks 1-indexed; ties share the average.
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            orig_idx = sorted_pairs[k][1]
            rank_sum[orig_idx] = avg_rank
            rank_count[orig_idx] = 1
        i = j + 1
    # Convert 1..n rank to 0..100 percentile (rank 1 → 0, rank n → 100, mid → 50).
    if n == 1:
        return [50.0]
    out: list[float] = []
    for idx in range(n):
        r = rank_sum.get(idx, 1.0)
        out.append(round(((r - 1.0) / (n - 1)) * 100.0, 2))
    return out


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
    # No live-SoC gate. The LP itself decides exp[i] > 0 only when arbitrage is
    # profitable under its forecast; dispatch trusts the solver. Robustness
    # against forecast error is enforced separately by ``filter_robust_peak_export``
    # (scenario LP, see src/scheduler/scenarios.py). ``ENERGY_STRATEGY_MODE``
    # ``strict_savings`` is the kill switch and is honoured at filter time.
    strict_savings = (config.ENERGY_STRATEGY_MODE or "savings_first").strip().lower() == "strict_savings"

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
        elif (not strict_savings) and dis > EPS and exp > EPS:
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
            # PR 3 of plan: solar_charge → solar_preheat. The LP's PV-abundance
            # ceiling lift means tank_temp_c[i+1] is already raised toward
            # DHW_TEMP_MAX_C on these slots; dispatch translates that into a
            # tank-target write + powerful boost. Restore row at end-of-window
            # drops back to DHW_TEMP_NORMAL_C — same safety scaffolding as
            # negative/cheap kinds.
            "solar_charge": "solar_preheat",
        }.get(kind, "normal")

        mid = start_utc + timedelta(minutes=15)
        fc = get_forecast_for_slot(mid, forecast)
        outdoor = fc.temperature_c if fc else (plan.temp_outdoor_c[i0] if i0 < len(plan.temp_outdoor_c) else 0.0)
        peak_frost = kind == "peak" and outdoor < float(config.WEATHER_FROST_THRESHOLD_C)

        # The merge in _merge_half_hour_slots_for_daikin groups slots by KIND
        # (battery state), not by heat-pump activity. For solar_charge, the LP
        # can plan e_dhw > 0 in just one slot inside a long merged window
        # (e.g. one 30-min heat across a 3.5h window). Reading params from
        # only the first slot would lose that — emitting tank_power=False
        # for the whole window when the LP wanted heat in the middle.
        #
        # Scan the merged window for the MAX e_dhw and MAX tank_temp target.
        # If any slot wants heat → tank_pow=True with the peak target. This
        # is safe because Daikin's tolerance check skips the write when target
        # is already met, so non-heating slots within the window become
        # natural no-ops.
        n_slots_in_window = max(1, round((end_utc - start_utc).total_seconds() / 1800))
        i_end = min(i0 + n_slots_in_window, len(plan.dhw_electric_kwh)) if plan.dhw_electric_kwh else i0 + 1
        j = min(i0, len(plan.dhw_electric_kwh) - 1) if plan.dhw_electric_kwh else 0
        ed_max = max(
            (plan.dhw_electric_kwh[k] for k in range(i0, i_end)),
            default=0.0,
        ) if plan.dhw_electric_kwh else 0.0
        es_max = max(
            (plan.space_electric_kwh[k] for k in range(i0, i_end)),
            default=0.0,
        ) if plan.space_electric_kwh else 0.0
        # tank_temp_c is len N+1 (state at end of slot k is index k+1).
        tt_max = max(
            (plan.tank_temp_c[min(k + 1, len(plan.tank_temp_c) - 1)]
             for k in range(i0, i_end)),
            default=float(config.DHW_TEMP_NORMAL_C),
        ) if plan.tank_temp_c else float(config.DHW_TEMP_NORMAL_C)
        # Use first-slot params for the LWT offset (climate-curve adjustments
        # don't accumulate the same way as DHW heat).
        ed = ed_max
        es = es_max
        tt = tt_max
        # LP-derived LWT offset: continuous value back-computed from e_space[j] via the
        # inverse climate curve.  This replaces the old fixed-tier overrides so the solver's
        # energy decision directly controls the radiator temperature rather than a lookup table.
        lwt = _lwt_offset_from_plan(j, plan)
        if peak_frost:
            lwt = max(-2.0, float(config.OPTIMIZATION_LWT_OFFSET_MIN))
        tank_pow = ed > EPS
        tank_powful = ed >= max_b - 1e-3
        # Powerful boost is enabled on negative *and* solar_preheat slots —
        # both want to dump as much energy into the tank as possible while
        # the slot lasts (negative: paid to import; solar: free PV otherwise
        # exported / curtailed). All other kinds keep powerful off.
        powerful_kinds = ("negative", "solar_charge")
        params: dict[str, Any] = {
            "lwt_offset": round(lwt, 1),  # Daikin rejects sub-0.1 precision; rounds float epsilon to 0.0
            "tank_powerful": tank_powful if kind in powerful_kinds else False,
            "tank_power": tank_pow,
            "climate_on": es > EPS or ed > EPS or kind in ("negative", "cheap", "solar_charge"),
            "lp_optimizer": True,
        }
        # Only set tank_temp when the tank will be on — Daikin rejects temperatureControl on a powered-off tank.
        # Floor at DHW_TEMP_COMFORT_C (48 °C). Ceiling depends on slot kind:
        #   negative      → DHW_TEMP_MAX_C (65 °C). Grid pays us; load all the kWh.
        #   solar_charge  → DHW_TEMP_PV_ABUNDANCE_TARGET_C (55 °C). PV is free but
        #                   holding 65 °C through afternoon bleeds standing losses
        #                   before evening showers. Cap at 55 °C captures with
        #                   margin without that bleed-back. Hard clamp here even
        #                   though LP only soft-prefers it — Onecta write must
        #                   reflect operator intent regardless of solver slack.
        #   else (cheap)  → DHW_TEMP_MAX_C (65 °C). Cheap-grid imports may be
        #                   marginal but the LP only emits cheap kind when it's
        #                   chosen to charge → ride the LP plan.
        if tank_pow:
            if kind == "solar_charge":
                ceiling = float(config.DHW_TEMP_PV_ABUNDANCE_TARGET_C)
            else:
                ceiling = float(config.DHW_TEMP_MAX_C)
            params["tank_temp"] = round(
                min(ceiling, max(float(config.DHW_TEMP_COMFORT_C), tt)),
                1,
            )
        if kind == "peak" or kind == "peak_export":
            params["tank_power"] = False
            params.pop("tank_temp", None)  # tank off — no point setting target temp

        st = start_utc.isoformat().replace("+00:00", "Z")
        en = end_utc.isoformat().replace("+00:00", "Z")
        # Wider than the heartbeat tick so restores can't be silently skipped
        # by the state machine when a tick lands just past the window. See
        # ``LP_RESTORE_WINDOW_MINUTES`` docstring for the 2026-04-30 incident.
        restore_window = max(2, int(getattr(config, "LP_RESTORE_WINDOW_MINUTES", 5)))
        restore_end = (
            end_utc + timedelta(minutes=restore_window)
        ).isoformat().replace("+00:00", "Z")
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


def _drop_legionella_window_pairs(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Drop any (restore, action) pair whose action falls inside Sunday
    10:30–12:00 local — Daikin firmware owns the weekly thermal-shock cycle.

    Keeping a manual setpoint write in that window risks fighting the firmware
    (or, worse, racing it). The cycle runs autonomously per CLAUDE.md; we just
    stay out of its way.
    """
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    tz = TZ()
    for rest, act in pairs:
        try:
            start_utc = datetime.fromisoformat(act["start_time"].replace("Z", "+00:00"))
            local = start_utc.astimezone(tz)
            local_min = local.hour * 60 + local.minute
            if local.weekday() == 6 and 10 * 60 + 30 <= local_min < 12 * 60:
                logger.info(
                    "write_daikin_from_lp_plan: skipping pair start=%s — Sunday legionella window",
                    act["start_time"],
                )
                continue
        except (KeyError, ValueError, TypeError):
            pass
        out.append((rest, act))
    return out


def _coalesce_low_value_pairs(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Merge adjacent pairs with the same low-value action_type by extending
    the earlier window's end_time and dropping the later pair.

    Low-value kinds: ``pre_heat`` (cheap-slot tank lift) and ``solar_preheat``.
    High-value kinds (``max_heat`` for negative slots, ``shutdown`` for peak
    and peak_export) are NEVER coalesced — they target distinct windows that
    can't be substituted.
    """
    if not pairs:
        return pairs
    coalescible = {"pre_heat", "solar_preheat"}
    out = [pairs[0]]
    for rest, act in pairs[1:]:
        prev_rest, prev_act = out[-1]
        prev_type = prev_act.get("action_type")
        cur_type = act.get("action_type")
        if (
            prev_type == cur_type
            and cur_type in coalescible
            and prev_act.get("end_time") == act.get("start_time")
        ):
            # Extend previous action + restore to swallow this pair.
            prev_act["end_time"] = act["end_time"]
            prev_rest["start_time"] = rest["start_time"]
            prev_rest["end_time"] = rest["end_time"]
            out[-1] = (prev_rest, prev_act)
            continue
        out.append((rest, act))
    return out


def _apply_write_budget(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    headroom: int,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[str]]:
    """Bound the number of Daikin writes a plan will queue against quota headroom.

    Algorithm (PR 5 of plan):
    1. Each pair is 2 writes (action + restore). If 2*len(pairs) ≤ headroom,
       pass through.
    2. Coalesce adjacent same-kind low-value pairs (``pre_heat``,
       ``solar_preheat``).
    3. If still over budget, drop trailing low-value pairs until we fit.
    4. NEVER drop or coalesce ``max_heat`` (negative-price) or ``shutdown``
       (peak / peak_export) — these are the high-value pairs whose timing
       can't be substituted.

    Returns (filtered_pairs, dropped_action_descriptions). Caller surfaces the
    dropped list via notification.
    """
    if headroom <= 0:
        # Nothing to spend — drop every low-value pair, keep only high-value.
        keep_kinds = {"max_heat", "shutdown"}
        filtered = [(r, a) for r, a in pairs if a.get("action_type") in keep_kinds]
        dropped = [
            f"{a.get('action_type')}@{a.get('start_time')}"
            for r, a in pairs if a.get("action_type") not in keep_kinds
        ]
        return filtered, dropped

    if 2 * len(pairs) <= headroom:
        return pairs, []

    coalesced = _coalesce_low_value_pairs(pairs)
    dropped: list[str] = []
    if 2 * len(coalesced) <= headroom:
        return coalesced, dropped

    # Still over budget — drop trailing low-value pairs (preserve high-value
    # earliest-first; each pair is 2 writes).
    coalescible = {"pre_heat", "solar_preheat"}
    # Walk from the END of the list and drop coalescible pairs first.
    keep: list[tuple[dict[str, Any], dict[str, Any]]] = list(coalesced)
    i = len(keep) - 1
    while 2 * len(keep) > headroom and i >= 0:
        _r, a = keep[i]
        if a.get("action_type") in coalescible:
            dropped.append(f"{a.get('action_type')}@{a.get('start_time')}")
            keep.pop(i)
        i -= 1
    return keep, dropped


def write_daikin_from_lp_plan(
    plan_date: str,
    plan: LpPlan,
    forecast: list[HourlyForecast],
) -> int:
    """Write merged Daikin ``action_schedule`` rows from LP solution.

    Clearing is range-keyed on the LP's UTC slot window so a rolling 24 h plan
    written at, e.g., 18:00 local (``plan_date`` = today, slots spanning into
    tomorrow) correctly removes stale rows from *both* dates while the shared
    in-flight preservation keeps any Daikin action currently executing.

    V12 audit fix: clear ``_USER_OVERRIDE_NOTIFIED`` here. The set tracks
    action_schedule row ids we've already pinged about; once the rows
    are cleared/replaced by this function, those ids are dead and the set
    would otherwise leak entries forever.

    PR 5 of plan: applies the Daikin write-budget guard (coalesce low-value +
    drop tail + notify) and skips any pair landing inside the Sunday legionella
    window before the upsert loop.
    """
    try:
        from .. import state_machine as _sm
        _sm._USER_OVERRIDE_NOTIFIED.clear()
    except Exception:  # pragma: no cover — defensive
        pass

    if config.DAIKIN_CONTROL_MODE == "passive":
        # Passive mode: do not write any Daikin actions and clear any leftovers
        # from a prior active run so the heartbeat never picks them up.
        if plan.slot_starts_utc:
            window_start_iso = plan.slot_starts_utc[0].isoformat().replace("+00:00", "Z")
            window_end = plan.slot_starts_utc[-1] + timedelta(minutes=30)
            window_end_iso = window_end.isoformat().replace("+00:00", "Z")
            db.clear_actions_in_range(window_start_iso, window_end_iso, device="daikin")
        else:
            db.clear_actions_for_date(plan_date, device="daikin")
        logger.info("write_daikin_from_lp_plan: skipped (DAIKIN_CONTROL_MODE=passive)")
        return 0
    if plan.slot_starts_utc:
        window_start_iso = plan.slot_starts_utc[0].isoformat().replace("+00:00", "Z")
        window_end = plan.slot_starts_utc[-1] + timedelta(minutes=30)
        window_end_iso = window_end.isoformat().replace("+00:00", "Z")
        db.clear_actions_in_range(window_start_iso, window_end_iso, device="daikin")
    else:
        db.clear_actions_for_date(plan_date, device="daikin")
    pairs = daikin_dispatch_preview(plan, forecast)
    # PR 5: filter Sunday legionella window before budget guard so it doesn't
    # eat into headroom counting against pairs that get dropped anyway.
    pairs = _drop_legionella_window_pairs(pairs)
    # PR 5: budget guard. quota_remaining returns full budget when api_call_log
    # is empty; subtract a reservation so the heartbeat still has headroom for
    # safe-default reconciles + telemetry reads.
    try:
        from ..api_quota import quota_remaining
        reserve = int(getattr(config, "DAIKIN_RESERVE_FOR_HEARTBEAT", 30))
        headroom = max(0, quota_remaining("daikin") - reserve)
    except Exception:  # pragma: no cover — quota lookup must never break dispatch
        headroom = 9999
    pairs, dropped = _apply_write_budget(pairs, headroom)
    if dropped:
        try:
            from ..notifier import notify_strategy_update
            notify_strategy_update(
                f"Daikin write-budget guard active: dropped {len(dropped)} low-value "
                f"action(s) to fit headroom={headroom}. Consider raising "
                f"DAIKIN_DAILY_BUDGET or lowering DAIKIN_RESERVE_FOR_HEARTBEAT.",
                warnings=dropped,
            )
        except Exception:  # pragma: no cover — notification must never break dispatch
            logger.exception("write-budget guard: notify_strategy_update failed")
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


def filter_robust_peak_export(
    plan: LpPlan,
    scenarios: dict[str, LpPlan] | None,
    export_price_pence: list[float] | None = None,
) -> tuple[list[HalfHourSlot], list[dict[str, Any]]]:
    """Apply scenario-LP robustness filter to ``peak_export`` slots.

    Returns ``(slots, decisions)``:

    * ``slots`` — same as :func:`lp_dispatch_slots_for_hardware` but with any
      ``peak_export`` slot that fails the pessimistic agreement check
      downgraded to ``standard`` (no ForceDischarge group will be uploaded).
      Other kinds pass through unchanged.
    * ``decisions`` — one row per slot, ready to be persisted via
      :func:`db.upsert_dispatch_decision` (the caller injects ``run_id``).
      Includes the per-scenario export values for the audit trail.

    Decision rules (in priority order):

    1. ``ENERGY_STRATEGY_MODE=strict_savings`` → drop every ``peak_export``
       slot. Reason: ``strict_savings``.
    2. ``scenarios is None`` (trigger reason not in
       ``LP_SCENARIOS_ON_TRIGGER_REASONS``) → commit every ``peak_export``
       slot. Reason: ``no_scenarios_run``.
    3. Pessimistic scenario solve failed → commit (degenerate degrade — better
       to ship the LP's nominal plan than nothing). Reason: ``pessimistic_failed``.
    4. ``pessimistic.export_kwh[i] >= LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH``
       → commit. Reason: ``robust``.
    5. Economic margin must clear the future refill + wear shadow. Otherwise
       drop. Reason: ``economic_margin``.
    6. Otherwise → drop. Reason: ``pessimistic_disagrees``.

    Scenarios dict keys are the ``Scenario`` literal type ("optimistic",
    "nominal", "pessimistic") but accepted as plain strings to keep this
    module independent of the scenarios import path.
    """
    raw_slots = lp_dispatch_slots_for_hardware(plan)
    decisions: list[dict[str, Any]] = []
    out_slots: list[HalfHourSlot] = []

    strict_savings = (
        (config.ENERGY_STRATEGY_MODE or "savings_first").strip().lower()
        == "strict_savings"
    )
    floor_kwh = float(config.LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH)
    eta = float(config.BATTERY_RT_EFFICIENCY)
    terminal_value_p = float(getattr(config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0))
    wear_cost_p = float(getattr(config, "LP_BATTERY_WEAR_COST_PENCE_PER_KWH", 0.0))
    min_margin_p = float(getattr(config, "LP_PEAK_EXPORT_MIN_MARGIN_PENCE_PER_KWH", 0.0))
    export_rate_line = (
        list(export_price_pence)
        if export_price_pence is not None and len(export_price_pence) == len(plan.slot_starts_utc)
        else [float(config.EXPORT_RATE_PENCE)] * len(plan.slot_starts_utc)
    )
    # Per-slot Outgoing-rate percentile within the LP horizon (#274).
    # 100 = highest-rate slot in the horizon, 0 = lowest. Used as an audit
    # metric on every dispatch_decisions row so we can answer "where in the
    # horizon's Outgoing distribution did this export sit" without re-querying
    # agile_export_rates. NaN-safe — when all rates equal (flat fallback) the
    # percentile is 50 for every slot.
    rate_percentiles = _compute_rank_percentiles(export_rate_line)

    def _future_refill_shadow_p_kwh(idx: int) -> float:
        future_prices = [float(p) for p in plan.price_pence[idx + 1:]]
        if not future_prices:
            return terminal_value_p
        refill_shadow = min(future_prices) / max(0.01, eta)
        return max(terminal_value_p, refill_shadow)

    def _economic_margin_p_kwh(idx: int) -> tuple[float, float, float]:
        export_price = export_rate_line[idx]
        refill_shadow = _future_refill_shadow_p_kwh(idx)
        wear_shadow = (1.0 + (1.0 / max(0.01, eta))) * wear_cost_p
        return export_price - refill_shadow - wear_shadow, export_price, refill_shadow

    def _unwrap(s):
        """Accept either an ``LpPlan`` (legacy / test convenience) or a
        ``ScenarioSolveResult`` (production path) and return the underlying
        ``LpPlan``. Lets callers keep the simple shape in unit tests while
        the optimizer pipes through richer metadata."""
        if s is None:
            return None
        return s.plan if hasattr(s, "plan") else s

    opt = _unwrap(scenarios.get("optimistic")) if scenarios else None
    nom = _unwrap(scenarios.get("nominal")) if scenarios else None
    pess = _unwrap(scenarios.get("pessimistic")) if scenarios else None

    def _exp(p: LpPlan | None, idx: int) -> float | None:
        if p is None or not p.ok or idx >= len(p.export_kwh):
            return None
        return float(p.export_kwh[idx])

    for i, s in enumerate(raw_slots):
        slot_iso = s.start_utc.isoformat()
        opt_exp = _exp(opt, i)
        nom_exp = _exp(nom, i)
        pess_exp = _exp(pess, i)

        decision: dict[str, Any] = {
            "slot_time_utc": slot_iso,
            "lp_kind": s.kind,
            "dispatched_kind": s.kind,
            "committed": True,
            "reason": "not_peak_export",
            "scen_optimistic_exp_kwh": opt_exp,
            "scen_nominal_exp_kwh": nom_exp,
            "scen_pessimistic_exp_kwh": pess_exp,
            "export_price_p_kwh": None,
            "refill_price_p_kwh": None,
            "economic_margin_p_kwh": None,
            "outgoing_rate_percentile": rate_percentiles[i] if i < len(rate_percentiles) else None,
        }

        if s.kind == "peak_export":
            margin_p, export_price_p, refill_shadow_p = _economic_margin_p_kwh(i)
            decision["export_price_p_kwh"] = export_price_p
            decision["refill_price_p_kwh"] = refill_shadow_p
            decision["economic_margin_p_kwh"] = margin_p
            if strict_savings:
                # Drop: strict_savings is the kill switch for arbitrage discharge.
                s = dataclasses.replace(s, kind="standard", lp_grid_import_w=None)
                decision["dispatched_kind"] = "standard"
                decision["committed"] = False
                decision["reason"] = "strict_savings"
                logger.info(
                    "filter_robust_peak_export: dropped slot=%s reason=strict_savings",
                    slot_iso,
                )
            elif scenarios is None:
                decision["reason"] = "no_scenarios_run"
            elif pess is None or not pess.ok:
                decision["reason"] = "pessimistic_failed"
                logger.warning(
                    "filter_robust_peak_export: committing slot=%s despite pessimistic solve failure (degraded mode)",
                    slot_iso,
                )
            elif pess_exp is not None and pess_exp >= floor_kwh:
                if margin_p >= min_margin_p:
                    decision["reason"] = "robust"
                else:
                    s = dataclasses.replace(s, kind="standard", lp_grid_import_w=None)
                    decision["dispatched_kind"] = "standard"
                    decision["committed"] = False
                    decision["reason"] = "economic_margin"
                    logger.info(
                        "filter_robust_peak_export: dropped slot=%s "
                        "margin=%.2fp/kWh < min=%.2fp/kWh "
                        "(export=%.2fp refill_shadow=%.2fp)",
                        slot_iso,
                        margin_p,
                        min_margin_p,
                        export_price_p,
                        refill_shadow_p,
                    )
            else:
                # Drop: pessimistic scenario does not export here at the required floor.
                s = dataclasses.replace(s, kind="standard", lp_grid_import_w=None)
                decision["dispatched_kind"] = "standard"
                decision["committed"] = False
                decision["reason"] = "pessimistic_disagrees"
                logger.info(
                    "filter_robust_peak_export: dropped slot=%s "
                    "pessimistic_exp=%.2f < floor=%.2f kWh "
                    "(nominal=%.2f, optimistic=%.2f)",
                    slot_iso,
                    pess_exp if pess_exp is not None else 0.0,
                    floor_kwh,
                    nom_exp if nom_exp is not None else 0.0,
                    opt_exp if opt_exp is not None else 0.0,
                )

        decisions.append(decision)
        out_slots.append(s)

    return out_slots, decisions


def build_fox_groups_from_lp(
    plan: LpPlan,
    scenarios: dict[str, LpPlan] | None = None,
    export_price_pence: list[float] | None = None,
) -> tuple[list[SchedulerGroup], datetime | None]:
    """Translate LP plan into Fox V3 groups.

    Returns ``(groups, replan_at_utc)``. When the LP horizon yields more than 8
    distinct windows, the dispatcher truncates to the first 8 (preserving the
    near-future at full precision) and ``replan_at_utc`` reports the end-time of
    the last surviving window — the caller schedules a one-shot MPC re-plan
    shortly before it. ``replan_at_utc`` is ``None`` when no truncation occurred.

    Important: Fox V3 scheduler is daily-cyclic — each group stores only
    hour/minute (no date) and repeats every day. We therefore only dispatch the
    first 24 h of plan slots; D+1 actions that share an hour-of-day with D+0
    actions would otherwise become indistinguishable, overlapping groups in the
    inverter (visible as duplicates in the Fox app). The next MPC re-solve
    handles D+1 dispatch once D+1 becomes "today". The LP itself still plans
    over 48 h (S10.2 / #169) — only the dispatch surface is 24 h.

    ``scenarios``: optional dict of scenario name → LpPlan. When supplied,
    ``filter_robust_peak_export`` runs before the 24h cap so unsafe
    ``peak_export`` slots get downgraded to ``standard``. Decisions produced
    by the filter are NOT persisted here — callers that want the audit trail
    should call ``filter_robust_peak_export`` directly to capture decisions
    alongside the run_id.
    """
    slots, _decisions = filter_robust_peak_export(plan, scenarios, export_price_pence=export_price_pence)
    if slots:
        cutoff = slots[0].start_utc + timedelta(hours=24)
        slots = [s for s in slots if s.start_utc < cutoff]
    # peak_export_discharge=False: kind="peak_export" already maps to
    # ForceDischarge inside _slot_fox_tuple unconditionally; the flag here only
    # controls whether kind="peak" (no LP-planned export) is upgraded to
    # ForceDischarge. We keep that off to honour the LP's discharge plan
    # exactly — SelfUse covers load during peaks the LP didn't mark for export.
    return _merge_fox_groups(
        slots,
        max_groups=8,
        peak_export_discharge=False,
        truncate_horizon=True,
    )


def _detect_overlapping_groups(
    groups: list[SchedulerGroup],
) -> list[tuple[int, int]]:
    """Return index pairs whose minute-of-day ranges intersect.

    Fox V3 stores each group as HH:MM start/end with no date — the inverter
    repeats them daily. Two groups whose intervals overlap produce undefined
    behaviour (firmware appears to honour the last-registered window per
    minute bucket). Catching this at the upload boundary stops bad payloads
    from any source — LP, heuristic fallback, or future regressions — from
    reaching hardware.

    Endpoints are treated as ``[start, end)`` (end exclusive) to match the
    merge code's adjacency convention: a group ``00:00-00:30`` followed by
    ``00:30-01:59`` is a clean back-to-back schedule, NOT an overlap.
    """
    spans: list[tuple[int, int]] = []
    for g in groups:
        start = g.start_hour * 60 + g.start_minute
        end = g.end_hour * 60 + g.end_minute
        spans.append((start, end))
    overlaps: list[tuple[int, int]] = []
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a_start, a_end = spans[i]
            b_start, b_end = spans[j]
            if a_start < b_end and b_start < a_end:
                overlaps.append((i, j))
    return overlaps


def upload_fox_if_operational(fox: FoxESSClient | None, groups: list[SchedulerGroup]) -> bool:
    fox_ok = False
    if fox and fox.api_key and not config.OPENCLAW_READ_ONLY:
        overlaps = _detect_overlapping_groups(groups)
        if overlaps:
            for i, j in overlaps:
                gi, gj = groups[i], groups[j]
                logger.error(
                    "Refusing Fox V3 upload: overlapping groups detected "
                    "[%d] %s %02d:%02d-%02d:%02d  vs  [%d] %s %02d:%02d-%02d:%02d "
                    "(daily-cyclic clock would render duplicates — see #208)",
                    i, gi.work_mode, gi.start_hour, gi.start_minute, gi.end_hour, gi.end_minute,
                    j, gj.work_mode, gj.start_hour, gj.start_minute, gj.end_hour, gj.end_minute,
                )
            return False
        try:
            fox.set_scheduler_v3(groups, is_default=False)
            fox.warn_if_scheduler_v3_mismatch(groups)
            fox.set_scheduler_flag(True)
            fox_ok = True
            db.save_fox_schedule_state([g.to_api_dict() for g in groups], enabled=True)
        except FoxESSError as e:
            logger.warning("Fox Scheduler V3 upload failed: %s", e)
    elif fox and fox.api_key:
        logger.info("Skipping Fox Scheduler V3 upload (read-only)")
    return fox_ok
