"""Translate MILP :class:`~src.scheduler.lp_optimizer.LpPlan` into Fox V3 groups and Daikin actions."""
from __future__ import annotations

import dataclasses
import logging
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
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
    _slot_fox_tuple,
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
    # (scenario LP, see src/scheduler/scenarios.py).
    #
    # PR D (2026-05-22) — peak_export only emerges when ``OPTIMIZATION_PRESET=
    # vacation``: in normal/guests the LP carries the constraint
    # ``exp[i] <= pv_use[i]`` (not ``pv_use + dis``), so the ``dis > 0 AND
    # exp > 0`` branch below cannot fire by construction. The scenario filter
    # downstream still applies for vacation-mode safety.

    max_pwr_w = int(config.FOX_FORCE_CHARGE_MAX_PWR)
    min_pwr_w = 200  # floor: prevents inverter from interpreting "0 W" as unlimited

    # Slots where the LP relaxed the export cap to drain the battery ahead of a
    # negative window (1B). These export>pv slots are labelled pre_negative_export
    # (not peak_export) so they bypass the peak_export robustness filter.
    pre_neg_set = set(getattr(plan, "pre_negative_export_slots", []) or [])

    for i in range(n):
        st = plan.slot_starts_utc[i]
        en = st + timedelta(minutes=30)
        price = plan.price_pence[i]
        chg = plan.battery_charge_kwh[i]
        dis = plan.battery_discharge_kwh[i]
        exp = plan.export_kwh[i]
        pv = plan.pv_use_kwh[i] if plan.pv_use_kwh else 0.0
        ed = plan.dhw_electric_kwh[i]
        es = plan.space_electric_kwh[i]

        kind: str = "standard"
        lp_grid_import_w: int | None = None

        if chg > EPS:
            grid_import = plan.import_kwh[i] if plan.import_kwh else 0.0
            # PR E — vacation mode: the LP constraint ``chg <= pv_use`` (see
            # lp_optimizer.py line ~612) guarantees battery charging draws
            # only from PV. ``imp > 0`` in a vacation slot is for base_load
            # coverage, not for chg. The right hardware mode is SelfUse
            # (PV → battery + load); ForceCharge would wrongly grid-charge
            # the battery in addition to base load.
            try:
                from ..presets import OperationPreset
                _vacation = OperationPreset(config.OPTIMIZATION_PRESET) == OperationPreset.VACATION
            except (ValueError, AttributeError):
                _vacation = False
            if _vacation:
                kind = "solar_charge"
            elif price <= 0 and getattr(config, "LP_NEGATIVE_BEATS_SOLAR_CHARGE", True):
                # 2026-07-04 (the REAL 06-28 root cause; recurred live today):
                # price must outrank the PV-only check. A negative-price slot
                # whose planned charge is PV-sourced (grid_import ≈ 0, e.g.
                # the PV-sufficiency guard blocked grid→battery) was labelled
                # solar_charge → SelfUse(minSocOnGrid=100), and the H1
                # firmware does NOT honour that floor as a discharge freeze:
                # observed 06-28 and again 07-04, the battery discharged
                # 1.6-2.8 kW into the (HEM-scheduled) DHW boost instead of
                # the PAID grid. `negative` (→ ForceCharge fill) only when the
                # LP planned real grid import; a PV-only charge is a HOLD from
                # the grid's perspective → `negative_hold` (→ Backup since
                # 2026-07-04): never discharges, house grid-fed at the paid
                # rate, PV/grid top-up toward full is free/paid money.
                kind = "negative" if grid_import >= EPS else "negative_hold"
            elif grid_import < EPS:
                kind = "solar_charge"  # PV-only charging — use SelfUse, not ForceCharge
            elif price <= 0:
                kind = "negative"
            else:
                kind = "cheap"
        elif price <= 0:
            # Negative price + LP chose chg ≈ 0 (battery saturated or PV alone
            # suffices). LP hard-forbids dis during negatives — encode that on
            # hardware via Backup (LP_NEGATIVE_HOLD_FOX_MODE, 2026-07-04),
            # which never discharges to loads; see _slot_fox_tuple.
            kind = "negative_hold"
        elif dis > EPS and exp > pv + EPS:
            # PR D — peak_export only when battery actively dumps to grid
            # (exp exceeds what PV alone could have exported). Without the
            # ``exp > pv`` check the labeller would false-positive on slots
            # where dis goes to self-use AND PV excess naturally exports;
            # in normal/guests mode the LP constraint ``exp <= pv_use``
            # makes ``exp > pv`` mathematically impossible → kind stays
            # ``standard``. Vacation mode (``exp <= pv_use + dis``) and the
            # pre-negative drain relaxation (1B) are where this branch fires.
            kind = "pre_negative_export" if i in pre_neg_set else "peak_export"
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

    # Second pass: mark "tank_idle_overnight" slots.
    #
    # Once we pass a shower-window slot, subsequent "standard" slots represent
    # the post-shower-overnight period where the user doesn't need hot water
    # until next-day's PV abundance. Tank should idle at a low backup target
    # (default 38 °C — backup if someone showers unexpectedly in the morning,
    # but firmware won't reheat to maintain higher temps overnight).
    #
    # Reset when we hit a productive slot (cheap/negative/solar_charge) — that
    # signals "next day's economics are better, tank can heat now."
    if _overnight_tank_idle_enabled():
        _is_vacation = False
        try:
            from .lp_optimizer import _resolve_active_shower_windows, _window_set_slot_mask
            from ..presets import OperationPreset
            try:
                _preset = OperationPreset(config.OPTIMIZATION_PRESET)
            except (ValueError, AttributeError):
                _preset = OperationPreset.NORMAL
            _is_guests = _preset == OperationPreset.GUESTS
            _is_vacation = _preset == OperationPreset.VACATION
            shower_windows = _resolve_active_shower_windows(_is_guests)
            shower_mask = _window_set_slot_mask(plan.slot_starts_utc, TZ(), windows=shower_windows)
        except Exception:  # pragma: no cover — never let the override break dispatch
            shower_mask = [False] * n

        # PR E — vacation: DHW demand is zero (firmware owns the tank).
        # The post-shower idle overlay is semantically moot; skip entirely
        # so a config drift adding shower windows to vacation can't trigger
        # surprise tank_idle_overnight labels.
        if _is_vacation:
            return out

        seen_shower_in_window = False
        # Bug fix (#323): when the LP horizon's first slot starts INSIDE an
        # in-progress idle window (i.e. AFTER the last shower of the day
        # already ended), the loop below would never see a shower slot —
        # so it never flipped seen_shower_in_window to True — so the
        # remaining slots stayed ``standard`` instead of becoming
        # ``tank_idle_overnight``. Pre-arm the flag based on slot 0's local
        # clock position relative to the shower schedule's wraparound idle
        # window: from latest_shower_end through to next-day earliest start.
        if shower_windows and plan.slot_starts_utc:
            first_slot_mid = (
                plan.slot_starts_utc[0] + timedelta(minutes=15)
            ).astimezone(TZ())
            first_min = first_slot_mid.hour * 60 + first_slot_mid.minute
            latest_end = max(e for _, e in shower_windows)
            earliest_start = min(s for s, _ in shower_windows)
            if latest_end > earliest_start:
                # Wraparound idle window: post-latest-end OR pre-earliest-start.
                in_idle_window = first_min >= latest_end or first_min < earliest_start
            else:
                # Clean range, no wraparound (e.g. morning-only schedule).
                in_idle_window = latest_end <= first_min < earliest_start
            if in_idle_window:
                seen_shower_in_window = True
        # Reset overnight tracker ONLY on solar_charge (PV abundance kicks in)
        # or negative-price slots (grid pays us — tank-heat free). NOT on
        # cheap-grid battery-charging slots: a 02:30 cheap-charge slot for
        # the battery doesn't mean "time to start tank heating" — tank
        # should keep idling at low backup target until PV is available.
        # Per user 2026-05-09: "we can heat it again during abundance of PV
        # of the next day" — explicitly PV, not cheap-grid.
        productive_kinds = {"negative", "solar_charge"}
        for i in range(n):
            if shower_mask[i]:
                seen_shower_in_window = True
                continue
            if out[i].kind in productive_kinds:
                # Productive slot resets the overnight tracker — next day's
                # showers reset the cycle.
                seen_shower_in_window = False
                continue
            if seen_shower_in_window and out[i].kind == "standard":
                out[i] = HalfHourSlot(
                    start_utc=out[i].start_utc,
                    end_utc=out[i].end_utc,
                    price_pence=out[i].price_pence,
                    kind="tank_idle_overnight",
                    lp_grid_import_w=out[i].lp_grid_import_w,
                    target_soc_pct=out[i].target_soc_pct,
                    soc_floor_pct=out[i].soc_floor_pct,
                )
    # Third pass (A1, #679): flag positive-price battery-hold slots. Runs AFTER
    # the tank_idle overlay so tank_idle_overnight slots are eligible holds too.
    # Vacation is guarded inside the helper (and the early-return above already
    # short-circuits the tank_idle-enabled vacation case).
    _label_positive_price_holds(plan, out)
    return out


def _label_positive_price_holds(plan: LpPlan, slots: list[HalfHourSlot]) -> None:
    """A1 (#679): set ``soc_floor_pct`` on positive-price battery-hold slots.

    The LP already emits ``dis=0 / chg=0 / imp>0`` "cover load from grid, hold
    the battery for the evening peak" flows (driven by the pessimistic charge
    floor, #673). Legacy dispatch fell those to SelfUse(reserve) and the battery
    discharged into any load spike — the 2026-07-10 incident. Here we mark such
    slots so :func:`_slot_fox_tuple` maps them to pinned Backup, the only proven
    0%-discharge hold on the H1 (A0 finding: the per-group SelfUse floor is
    ignored by the firmware).

    A slot is flagged when ALL hold: kind in {standard, peak,
    tank_idle_overnight}; ``dis<EPS and chg<EPS and imp>EPS and price>0``;
    planned end-of-slot SoC% > reserve + margin; and a later slot in the horizon
    is a genuine peak (``price >= peak_threshold``) with future-max uplift over
    the current price >= the uplift threshold.

    Contiguous flagged slots form a RUN; the whole run shares its MINIMUM
    quantized (up to a multiple of 5) floor so it maps to one byte-identical
    Backup tuple and merges to a single Fox V3 group. Only the top
    ``LP_POSITIVE_HOLD_MAX_GROUPS`` runs (by protected value) are kept — the
    rest are cleared, protecting the 8-group scheduler cap.

    No-op when ``LP_POSITIVE_HOLD_ENABLED`` is false (mapping stays byte-
    identical to legacy) or in the vacation preset (its LP forbids
    grid->battery, so a hold there is moot).
    """
    if not config.LP_POSITIVE_HOLD_ENABLED:
        return
    n = len(slots)
    if n == 0:
        return
    # Vacation: LP forbids grid->battery; a hold there is moot and Backup would
    # mis-map. Use the SAME defensive normalization as the A2 solar_charge guard
    # (_optimization_preset_away_like → .strip().lower() + legacy alias handling
    # via OperationPreset._missing_) so a whitespace/mixed-case/"travel" preset
    # cannot leak A1 holds into vacation.
    if _optimization_preset_away_like():
        return

    cap = float(config.BATTERY_CAPACITY_KWH)
    if cap <= 0:
        return
    soc = plan.soc_kwh or []
    if len(soc) < n + 1:
        return  # heuristic plan without a full SoC trajectory — no hold
    prices = plan.price_pence
    chg = plan.battery_charge_kwh
    dis = plan.battery_discharge_kwh
    imp = plan.import_kwh or []
    peak_thr = plan.peak_threshold_pence

    reserve_pct = float(config.MIN_SOC_RESERVE_PERCENT)
    margin = float(config.LP_POSITIVE_HOLD_MIN_SOC_MARGIN_PCT)
    uplift_thr = float(config.LP_POSITIVE_HOLD_MIN_UPLIFT_PENCE)
    max_groups = int(config.LP_POSITIVE_HOLD_MAX_GROUPS)
    reserve_kwh = reserve_pct / 100.0 * cap
    hold_set = ("standard", "peak", "tank_idle_overnight")

    # Suffix stats over j > i: max future price and whether any future peak.
    suffix_max = [float("-inf")] * (n + 1)
    suffix_has_peak = [False] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_max[i] = max(suffix_max[i + 1], prices[i])
        suffix_has_peak[i] = suffix_has_peak[i + 1] or (prices[i] >= peak_thr)

    candidate = [False] * n
    quant_floor = [0] * n
    future_max = [0.0] * n
    for i in range(n):
        if slots[i].kind not in hold_set:
            continue
        imp_i = imp[i] if i < len(imp) else 0.0
        if not (dis[i] < EPS and chg[i] < EPS and imp_i > EPS and prices[i] > 0):
            continue
        end_soc_pct = soc[i + 1] / cap * 100.0
        if end_soc_pct <= reserve_pct + margin:
            continue
        fmax = suffix_max[i + 1]
        if not (suffix_has_peak[i + 1] and (fmax - prices[i]) >= uplift_thr):
            continue
        candidate[i] = True
        quant_floor[i] = min(100, int(math.ceil(end_soc_pct / 5.0) * 5))
        future_max[i] = fmax

    # Coalesce contiguous candidate runs; keep the top-value ones.
    runs: list[tuple[int, int, int, float]] = []  # (start, end_excl, floor, score)
    i = 0
    while i < n:
        if not candidate[i]:
            i += 1
            continue
        j = i
        while j < n and candidate[j]:
            j += 1
        run_floor = min(quant_floor[k] for k in range(i, j))
        score = sum(
            (future_max[k] - prices[k]) * max(0.0, soc[k + 1] - reserve_kwh)
            for k in range(i, j)
        )
        runs.append((i, j, run_floor, score))
        i = j

    if not runs or max_groups <= 0:
        return
    for (start, end_excl, run_floor, _score) in sorted(
        runs, key=lambda r: r[3], reverse=True
    )[:max_groups]:
        for k in range(start, end_excl):
            slots[k].soc_floor_pct = run_floor


def _overnight_tank_idle_enabled() -> bool:
    """Toggle for the post-shower overnight tank idle override (default on).

    Set ``DHW_TANK_OVERNIGHT_IDLE_ENABLED=false`` to disable — overnight slots
    fall back to plain "standard" (no Daikin write, firmware does whatever it
    wants based on its own schedule).
    """
    val = (getattr(config, "DHW_TANK_OVERNIGHT_IDLE_ENABLED", "true") or "true").strip().lower()
    return val not in ("false", "0", "no", "off")


# NOTE: the LP-derived per-slot LWT offset (`plan.lwt_offset_c`) is recorded in
# lp_solution_snapshot for audit but is deliberately NOT dispatched from the
# per-window loop below — Daikin window actions are TANK-ONLY (see the comment
# block inside daikin_dispatch_preview). The only offset writer is
# _write_lwt_preheat_actions, behind DAIKIN_LWT_PREHEAT_ENABLED and the
# measured demand gate (#540). The old `_lwt_offset_from_plan` helper was dead
# (computed, never written) and was removed in #541 review cleanup to keep the
# tank-only invariant grep-provable.


def _preheat_lwt_offset(
    price_p: float,
    outdoor_c: float,
    *,
    cheap_thr: float,
    peak_thr: float,
    indoor_c: float | None = None,
) -> int | None:
    """Heuristic LWT offset (integer, clamped to the device range) for a slot.

    Open-loop space-heating pre-heat (#481): boost the leaving-water
    temperature in cheap slots (pre-heat the house, store heat in the thermal
    mass) and set it back in peak slots (coast). Returns:

    * ``None`` when the feature is disabled.
    * an ``int`` offset otherwise: ``+BOOST`` (price ≤ cheap tier),
      ``PEAK_SETBACK`` (price ≥ peak tier), else ``0`` (neutral — let the
      firmware's natural weather curve run).

    The offset is the only thing HEM writes; the firmware curve still decides
    *when* to heat — we only nudge the water temperature when it already is.

    **Outdoor cutoff (Tracked by #540):** ``outdoor_c`` (the micro-climate-
    calibrated forecast temperature for the slot) gates the POSITIVE offsets
    only. At/above ``DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C`` a heat-pump house
    needs little/no space heat, so a positive offset would only WAKE the
    compressor for nothing (the June-2026 phantom-heating self-loop). This is
    an EXOGENOUS gate the measured-demand gate's own output cannot fool. Since
    2026-07-04 the NEGATIVE peak setback is ALSO cut when too warm to heat
    (DAIKIN_LWT_SETBACK_OUTDOOR_GATE, default true): a setback can only let
    the unit coast, but with no space heating running it is a pure waste of
    Daikin writes (2 per window) and heating-plan noise. Below the cutoff
    (real winter) the setback is untouched.

    ``indoor_c`` is a forward-looking hook for a future room sensor. While no
    sensor exists it is ``None`` and the comfort guard is a no-op. Once wired,
    it suppresses boosting when the room is already warm (≥ setpoint + band)
    and suppresses setback when it's already cold (≤ setpoint − band).
    """
    if not config.DAIKIN_LWT_PREHEAT_ENABLED:
        return None

    boost = int(config.DAIKIN_LWT_PREHEAT_BOOST_C)
    neg_boost = int(getattr(config, "DAIKIN_LWT_PREHEAT_NEGATIVE_BOOST_C", boost))
    setback = int(config.DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C)
    band = float(config.DAIKIN_LWT_PREHEAT_COMFORT_BAND_C)
    setpoint = float(config.INDOOR_SETPOINT_C)
    # Exogenous outdoor cutoff — suppresses POSITIVE offsets only (see docstring).
    # A non-finite temp (sensor/forecast glitch) fails SAFE → suppress, since the
    # cutoff IS the anti-phantom-heat guard; a transient self-corrects next plan.
    cutoff = float(getattr(config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0))
    too_warm_for_heat = (not math.isfinite(outdoor_c)) or outdoor_c >= cutoff

    if price_p < 0:
        # PAID to import — push space heating to the TOP of the operating range
        # (clamped below) to bank the most thermal mass while we're paid for it,
        # not the modest cheap-slot nudge. Suppressed when it's warm enough that
        # the compressor would only wake to waste it (#540 phantom-heat guard).
        off = 0 if too_warm_for_heat else neg_boost
        # Comfort guard (sensor-ready): don't over-heat an already-warm room.
        if indoor_c is not None and indoor_c >= setpoint + band:
            off = 0
    elif price_p <= cheap_thr:
        off = 0 if too_warm_for_heat else boost
        # Comfort guard (sensor-ready): don't pre-heat an already-warm room.
        if indoor_c is not None and indoor_c >= setpoint + band:
            off = 0
    elif price_p >= peak_thr:
        # 2026-07-04 (owner report): the setback used to be exempt from the
        # outdoor cutoff ("can only let the unit coast, never wake it") — true
        # thermally, but in summer the unit isn't space-heating at all, so
        # every peak window still burned 2 Daikin writes (offset + restore)
        # for zero effect and cluttered the heating plan. Gate it on the same
        # cutoff: no expected heating → no offset writes of either sign.
        # DAIKIN_LWT_SETBACK_OUTDOOR_GATE=false restores the old behaviour.
        if too_warm_for_heat and getattr(config, "DAIKIN_LWT_SETBACK_OUTDOOR_GATE", True):
            off = 0
        else:
            off = setback
        # Comfort guard (sensor-ready): don't set back an already-cold room.
        if indoor_c is not None and indoor_c <= setpoint - band:
            off = 0
    else:
        off = 0

    lo = int(config.OPTIMIZATION_LWT_OFFSET_MIN)
    hi = int(config.OPTIMIZATION_LWT_OFFSET_MAX)
    return max(lo, min(hi, int(off)))


def smooth_lwt_offsets(offsets: list[int | None], min_block: int) -> list[int | None]:
    """Make a per-slot LWT-offset sequence thermally coherent (#481 follow-up).

    A building's thermal mass has a multi-hour time constant, so a per-slot
    price-tier offset chatters (e.g. ``+3, 0, +3, 0, +3`` when the price hovers
    at the cheap threshold) — thermally pointless and a waste of Daikin writes.
    Two passes, in order:

    1. **Bridge** short ``0`` gaps (``< min_block`` slots) that sit between two
       runs of the SAME non-zero offset — a brief price blip shouldn't fracture
       a sustained boost/setback block.
    2. **Drop** any non-zero run shorter than ``min_block`` to ``0`` — a boost
       or setback too short to move the thermal mass isn't worth a write.

    ``None`` slots (not heating / feature off) are inert: never bridged across,
    never counted in a run. With ``min_block <= 1`` the input is returned as-is.
    """
    out = list(offsets)
    n = len(out)
    if min_block <= 1 or n == 0:
        return out

    # Pass 1 — bridge short same-value 0-gaps.
    i = 0
    while i < n:
        if out[i] != 0:
            i += 1
            continue
        j = i
        while j < n and out[j] == 0:
            j += 1
        left = out[i - 1] if i > 0 else None
        right = out[j] if j < n else None
        if (j - i) < min_block and left is not None and left != 0 and left == right:
            for k in range(i, j):
                out[k] = left
        i = j

    # Pass 2 — drop sub-threshold non-zero blocks to neutral.
    i = 0
    while i < n:
        v = out[i]
        if not v:  # None or 0
            i += 1
            continue
        j = i
        while j < n and out[j] == v:
            j += 1
        if (j - i) < min_block:
            for k in range(i, j):
                out[k] = 0
        i = j

    return out


def _lwt_preheat_pairs(
    plan: LpPlan,
    forecast: list[HourlyForecast],
    *,
    indoor_c: float | None = None,
) -> list[tuple[dict[str, Any] | None, dict[str, Any]]]:
    """``(restore_row, action_row)`` pairs that drive the Daikin LWT offset from
    the price-tier pre-heat heuristic (#481).

    Empty when ``DAIKIN_LWT_PREHEAT_ENABLED`` is false (climate hands-off
    preserved). Consecutive slots with the same non-zero offset merge into one
    window — a handful per day — so the device is written only at offset-change
    boundaries. Each window's ``restore`` returns the offset to ``0`` (the
    natural curve); neutral (``0``/``None``) slots emit nothing. SQLite-free —
    the caller reads the room sensor (if any) and does the upsert.
    """
    if not config.DAIKIN_LWT_PREHEAT_ENABLED:
        return []
    if not plan.slot_starts_utc or not plan.price_pence:
        return []

    # Prefer the plan's own tier thresholds (what the LP actually classified
    # against); fall back to the static config tiers.
    cheap_thr = plan.cheap_threshold_pence or float(config.OPTIMIZATION_CHEAP_THRESHOLD_PENCE)
    peak_thr = plan.peak_threshold_pence or float(config.OPTIMIZATION_PEAK_THRESHOLD_PENCE)

    n = len(plan.slot_starts_utc)
    offsets: list[int | None] = []
    for i in range(n):
        mid = plan.slot_starts_utc[i] + timedelta(minutes=15)
        fc = get_forecast_for_slot(mid, forecast)
        # Prefer the LP's micro-climate-CALIBRATED outdoor temp (what the house
        # feels) over the raw Open-Meteo forecast — the outdoor cutoff in
        # _preheat_lwt_offset is evaluated against this. Fall back to the raw
        # forecast, then 0.0. (The price tiers are price-based, so this choice
        # only affects the cutoff comparison, never the boost/setback tiering.)
        outdoor = (
            plan.temp_outdoor_c[i] if i < len(plan.temp_outdoor_c)
            else (fc.temperature_c if fc else 0.0)
        )
        price = plan.price_pence[i] if i < len(plan.price_pence) else 0.0
        offsets.append(_preheat_lwt_offset(
            price, outdoor, cheap_thr=cheap_thr, peak_thr=peak_thr, indoor_c=indoor_c,
        ))

    # Thermal coherence: collapse per-slot price chatter into sustained blocks
    # so we don't toggle the heat pump for wiggles the thermal mass can't follow
    # (and don't burn Daikin writes doing it). See ``smooth_lwt_offsets``.
    offsets = smooth_lwt_offsets(offsets, int(config.DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS))

    restore_window = max(2, int(getattr(config, "LP_RESTORE_WINDOW_MINUTES", 5)))
    out: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []

    i = 0
    while i < n:
        off = offsets[i]
        if not off:  # None (warm/disabled) or 0 (neutral) → no write
            i += 1
            continue
        j = i
        while j + 1 < n and offsets[j + 1] == off:
            j += 1
        start_utc = plan.slot_starts_utc[i]
        end_utc = plan.slot_starts_utc[j] + timedelta(minutes=30)
        st_iso = start_utc.isoformat().replace("+00:00", "Z")
        en_iso = end_utc.isoformat().replace("+00:00", "Z")
        restore_end = (
            end_utc + timedelta(minutes=restore_window)
        ).isoformat().replace("+00:00", "Z")
        action_row = {
            "device": "daikin",
            "action_type": "lwt_preheat",
            "start_time": st_iso,
            "end_time": en_iso,
            "params": {"lwt_offset": int(off), "lp_optimizer": True},
        }
        restore_row = {
            "device": "daikin",
            "action_type": "restore",
            "start_time": en_iso,
            "end_time": restore_end,
            "params": {"lwt_offset": 0, "lp_optimizer": True},
        }
        out.append((restore_row, action_row))
        i = j + 1

    # Drop a restore immediately superseded by the next action (offset flips
    # boost→setback with no neutral gap) — mirror the tank-path post-process so
    # we don't bounce the offset to 0 for a few minutes between windows.
    if len(out) >= 2:
        deduped: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []
        for k, (rest, act) in enumerate(out):
            if k + 1 < len(out):
                _nrest, nact = out[k + 1]
                if rest is not None and nact.get("start_time", "") <= rest.get("end_time", ""):
                    deduped.append((None, act))
                    continue
            deduped.append((rest, act))
        out = deduped

    return out


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
            # tank_idle_overnight: post-shower-until-next-PV-abundance window.
            # Tank target dropped to DHW_TANK_OVERNIGHT_TARGET_C (default 38°C)
            # so firmware doesn't reheat overnight — but stays slightly above
            # cold so an unexpected morning shower has some warm water.
            "tank_idle_overnight": "tank_idle_overnight",
        }.get(kind, "normal")

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
        tank_pow = ed > EPS
        tank_powful = ed >= max_b - 1e-3
        # Powerful boost is enabled on negative *and* solar_preheat slots —
        # both want to dump as much energy into the tank as possible while
        # the slot lasts (negative: paid to import; solar: free PV otherwise
        # exported / curtailed). All other kinds keep powerful off.
        powerful_kinds = ("negative", "solar_charge")
        # tank_power semantics (per-kind):
        #   peak / peak_export →
        #     Tank stays ON at NORMAL via the existing schedule; LP relies
        #     on natural standing-loss decay across peak (median 0.00 °C/h
        #     per prod telemetry — see Epic 14 / #386 for why the old
        #     SHUTDOWN strategy was removed 2026-05-21).
        #   else with LP-planned heat (tank_pow=True) → tank_power=True with the
        #     peak tank_temp target across the window.
        #   else without LP-planned heat → omit tank_power AND tank_temp entirely.
        #     This is "no operation needed" — leave the tank in whatever state
        #     firmware/prior-action left it. Setting tank_power=False here would
        #     ACTIVELY DISABLE the tank, which is wrong intent.
        is_shutdown = kind in ("peak", "peak_export")
        # TANK-ONLY here. The per-window actions built in this loop never carry
        # `climate_on`/`lwt_offset` — Daikin firmware's own zone scheduling and
        # weather curve decide *when* to heat, and the LP plans `e_space` only
        # as a consumption forecast.
        #
        # Active space-heating control (#481) is opt-in and emitted SEPARATELY
        # by `_write_lwt_preheat_actions` (gated by DAIKIN_LWT_PREHEAT_ENABLED),
        # which nudges the LWT *offset* by a price-tier heuristic. It lives
        # outside this loop because the fixed-DHW (K1) regime returns before
        # this loop ever runs. When the flag is off, no offset is ever written
        # (the original 2026-05-09 climate-hands-off behaviour).
        params: dict[str, Any] = {
            "tank_powerful": tank_powful if kind in powerful_kinds else False,
            "lp_optimizer": True,
        }
        if kind == "tank_idle_overnight":
            # Post-shower-until-next-PV-abundance: tank ON at low backup
            # target (default 38 °C, runtime-tunable). Firmware won't reheat
            # from 50+ down to that value (current > setpoint, no action).
            # If tank cools to it over the long overnight, firmware maintains
            # at the target — backup buffer for unexpected morning shower.
            #
            # Read via runtime_settings so the user can tune this live to
            # match their empirical bedtime habit (e.g. 37 °C) without a
            # restart. Falls back to the env-derived default on lookup error.
            from .. import runtime_settings as _rts
            try:
                _overnight_target = float(_rts.get_setting("DHW_TANK_OVERNIGHT_TARGET_C"))
            except (KeyError, TypeError, ValueError):
                _overnight_target = float(getattr(config, "DHW_TANK_OVERNIGHT_TARGET_C", 38.0))
            params["tank_power"] = True
            # Daikin Onecta tank_temp setpoint is integer (stepValue=1 per
            # device introspection 2026-05-10). Quantise here so a fractional
            # runtime override (e.g. 37.5) doesn't become a 400 from the cloud.
            params["tank_temp"] = int(round(_overnight_target))
        elif is_shutdown:
            # Epic 14 (#386) — single behaviour for peak / peak_export.
            # The former ``DHW_PEAK_TANK_STRATEGY="shutdown"`` branch was
            # removed because (a) prod telemetry across 8 May peak windows
            # showed median tank decay of 0.00 °C/h — the tank coasts
            # essentially perfectly even when held at 45 °C — and (b) the
            # tank_power=False path failed 27% of the time on the Onecta
            # cloud with READ_ONLY_CHARACTERISTIC errors when device state
            # already matched (e.g. after a user override or a previous
            # successful write).
            #
            # Keep tank ON at NORMAL target. If tank is above target from
            # prior solar / cheap charging, firmware doesn't reheat (no grid
            # draw). If at-or-below, firmware maintains at the setpoint.
            # No power cycling, no Onecta state-mismatch failures.
            params["tank_power"] = True
            # Onecta stepValue=1 (see overnight comment above).
            params["tank_temp"] = int(round(
                float(getattr(config, "DHW_TEMP_NORMAL_C", 45.0))
            ))
        elif tank_pow:
            params["tank_power"] = True
            # Floor at DHW_TEMP_COMFORT_C (48 °C). Ceiling depends on slot kind:
            #   negative      → DHW_TEMP_MAX_C (65 °C). Grid pays us; load all the kWh.
            #   solar_charge  → DHW_TEMP_PV_ABUNDANCE_TARGET_C (45 °C default,
            #                   runtime-tunable per household occupancy). PV is free
            #                   but holding 65 °C through afternoon bleeds standing
            #                   losses before evening showers. Cap at 55 °C captures
            #                   with margin without that bleed-back. Hard clamp
            #                   here even though LP only soft-prefers it — Onecta
            #                   write must reflect operator intent regardless of
            #                   solver slack.
            #   else (cheap)  → DHW_TEMP_MAX_C (65 °C). Cheap-grid imports may be
            #                   marginal but the LP only emits cheap kind when
            #                   it's chosen to charge → ride the LP plan.
            if kind == "solar_charge":
                ceiling = float(config.DHW_TEMP_PV_ABUNDANCE_TARGET_C)
            else:
                ceiling = float(config.DHW_TEMP_MAX_C)
            # Onecta stepValue=1 (see overnight comment above).
            params["tank_temp"] = int(round(
                min(ceiling, max(float(config.DHW_TEMP_COMFORT_C), tt))
            ))
        # else: no LP-planned heat in this window AND not a shutdown.
        # Don't touch the tank — omit tank_power + tank_temp from params.
        # The action's other params (lwt_offset, climate_on, tank_powerful=False)
        # still take effect. Restore at end of window resets to NORMAL safety state.

        st = start_utc.isoformat().replace("+00:00", "Z")
        en = end_utc.isoformat().replace("+00:00", "Z")
        # Wider than the heartbeat tick so restores can't be silently skipped
        # by the state machine when a tick lands just past the window. See
        # ``LP_RESTORE_WINDOW_MINUTES`` docstring for the 2026-04-30 incident.
        restore_window = max(2, int(getattr(config, "LP_RESTORE_WINDOW_MINUTES", 5)))
        restore_end = (
            end_utc + timedelta(minutes=restore_window)
        ).isoformat().replace("+00:00", "Z")
        # Restore params: tank-only (per user 2026-05-09 — climate hands-off).
        # No climate_on, no lwt_offset; firmware autonomously manages climate.
        # Onecta tank_temp stepValue=1 → quantise to int.
        restore_params = {
            "tank_powerful": False,
            "tank_temp": int(round(float(config.DHW_TEMP_NORMAL_C))),
            "tank_power": True,
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

    # Post-process: drop restore rows that are immediately superseded by the
    # next action's start. Without this, the restore writes target=45 °C and
    # then the next action immediately overwrites with its own target,
    # causing a brief target-flip that can trigger a firmware reheat in the
    # gap (~£0.07 per occurrence at typical mid-day rates).
    #
    # Specifically: when ``tank_idle_overnight`` ends at the start of the
    # next solar_charge window, the restore→45 + solar_preheat→55 sequence
    # would have firmware briefly target 45 (with tank at 38) → grid reheat
    # 38→45 → then solar_preheat sets 55. The 38→45 grid reheat is wasted.
    # Skipping the restore lets the next action take over directly.
    #
    # We only drop the RESTORE; the action itself is always preserved.
    if len(out) >= 2:
        deduped: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for i, (rest, act) in enumerate(out):
            if i + 1 < len(out):
                next_rest, next_act = out[i + 1]
                # If the next action starts at or before this restore's end,
                # the restore would be immediately overwritten — drop it.
                if next_act.get("start_time", "") <= rest.get("end_time", ""):
                    # Mark restore as None — the action upserter will skip it.
                    deduped.append((None, act))  # type: ignore[arg-type]
                    continue
            deduped.append((rest, act))
        out = deduped

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


def _space_heating_demand_present() -> bool:
    """True when the trailing window shows real measured space-heating demand.

    Reads ``db.measured_space_heating_kwh_excluding_offset_windows`` — the
    exclusion matters: counting offset-induced heating would let the gate
    feed on its own output and never close (June 2026). Fail-open on errors:
    a broken telemetry read must not silently disable winter pre-heat.
    """
    floor = float(getattr(config, "DAIKIN_LWT_PREHEAT_MIN_TRAILING_HEATING_KWH", 0.5))
    if floor <= 0:
        return True  # gate disabled
    lookback = int(getattr(config, "DAIKIN_LWT_PREHEAT_DEMAND_LOOKBACK_HOURS", 48))
    try:
        measured = db.measured_space_heating_kwh_excluding_offset_windows(lookback)
    except Exception:  # pragma: no cover — gate must never break dispatch
        logger.exception("LWT pre-heat demand gate read failed — failing open")
        return True
    return measured >= floor


def space_heating_gate_state() -> dict[str, Any]:
    """Public snapshot of the LWT pre-heat demand gate (#540 quick win) for
    the status API: the INPUTS, not just the verdict, so the cockpit can
    explain WHY pre-heat is suppressed or allowed right now.
    """
    floor = float(getattr(config, "DAIKIN_LWT_PREHEAT_MIN_TRAILING_HEATING_KWH", 0.5))
    lookback = int(getattr(config, "DAIKIN_LWT_PREHEAT_DEMAND_LOOKBACK_HOURS", 48))
    measured: float | None = None
    try:
        measured = round(db.measured_space_heating_kwh_excluding_offset_windows(lookback), 2)
    except Exception:  # pragma: no cover - defensive: status read must not fail
        logger.debug("space_heating_gate_state: measured read failed", exc_info=True)
    demand_present = _space_heating_demand_present()
    preheat_enabled = bool(getattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", False))

    # Exogenous outdoor cutoff (#540): the chip should explain a warm-day
    # suppression even when the (possibly self-fed) demand gate is open. Read
    # the freshest live outdoor temp; None when telemetry is absent.
    cutoff = float(getattr(config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0))
    current_outdoor: float | None = None
    try:
        tel = db.get_latest_daikin_telemetry(source="live")
        if tel and tel.get("outdoor_temp_c") is not None:
            current_outdoor = round(float(tel["outdoor_temp_c"]), 1)
    except Exception:  # pragma: no cover - status read must not fail
        logger.debug("space_heating_gate_state: outdoor telemetry read failed", exc_info=True)
    suppressed_by_outdoor = current_outdoor is not None and current_outdoor >= cutoff

    return {
        "preheat_enabled": preheat_enabled,
        "gate_enabled": floor > 0,
        "demand_present": demand_present,
        "measured_window_kwh": measured,
        "threshold_kwh": floor,
        "lookback_hours": lookback,
        "outdoor_cutoff_c": cutoff,
        "current_outdoor_c": current_outdoor,
        # Positive offsets (boost / neg-boost) are cut when warm REGARDLESS of
        # the demand gate; the −2 peak setback still fires (it can only coast).
        "positive_offset_suppressed_by_outdoor": suppressed_by_outdoor,
        # Demand-gate verdict: when shut, _write_lwt_preheat_actions writes NO
        # rows at all (setback included). Kept scoped to the demand gate so the
        # chip can distinguish "all LWT off" (this) from "positives off, setback
        # still active" (positive_offset_suppressed_by_outdoor) on a warm day.
        "preheat_suppressed": preheat_enabled and floor > 0 and not demand_present,
    }


def _write_lwt_preheat_actions(
    plan_date: str,
    plan: LpPlan,
    forecast: list[HourlyForecast],
) -> int:
    """Upsert the heuristic LWT-offset action rows (#481), if enabled.

    Self-contained so it can run in BOTH dispatch regimes (the fixed-DHW K1
    path and the legacy tank-loop path), since the two return from
    :func:`write_daikin_from_lp_plan` at different points. Returns the number of
    rows written (0 when the feature is off → climate hands-off preserved).

    Quota safety (user hard constraint): the offset rows ride the same fire-time
    idempotency (pre-fire state-match skips a write when the device already
    holds that offset) and integer/range clamping as every other Daikin action.
    On top of that we deterministically cap the number of writes to the live
    quota headroom here, dropping trailing pairs so we can never exceed budget.
    """
    if not config.DAIKIN_LWT_PREHEAT_ENABLED:
        return 0

    # Demand gate (#540 quick win): no pre-heat offsets without measured
    # space-heating demand in the trailing window. A positive offset can WAKE
    # the compressor the firmware would have left off (June 2026: heating went
    # 0 → 3-8 kWh/day within days of enabling pre-heat, with the LP budgeting
    # ~0.2). Pre-heating thermal mass the house isn't draining buys nothing;
    # when a cold snap starts, the firmware heats naturally and the gate opens
    # once that shows in the 2-hourly split — up to ~24 h (02:35 UTC rollup).
    # Comfort-safe: the firmware heats regardless; only HEM's price-shaping is
    # delayed. Negative (setback) offsets are gated too: with the compressor
    # off they are pure quota churn.
    if not _space_heating_demand_present():
        logger.info(
            "LWT pre-heat: skipped — no measured space-heating demand in the "
            "trailing %dh window (floor %.1f kWh)",
            int(getattr(config, "DAIKIN_LWT_PREHEAT_DEMAND_LOOKBACK_HOURS", 48)),
            float(getattr(config, "DAIKIN_LWT_PREHEAT_MIN_TRAILING_HEATING_KWH", 0.5)),
        )
        return 0

    # Indoor temperature for the comfort guard: the house room sensors are the
    # ONLY source (#540 W1). Daikin is NOT consulted — the Altherma has no room
    # stat, so its "room temperature" is always null (that was a bug). A stale
    # sensor is treated as absent (staleness window) so a dead sensor can't wedge
    # the guard on an old value; None → guard is a no-op.
    indoor_c: float | None = None
    try:
        s = db.get_latest_indoor_reading(
            max_age_minutes=int(getattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30))
        )
        if s is not None:
            indoor_c = float(s["temp_c"])
    except Exception:  # pragma: no cover — telemetry read must never break dispatch
        indoor_c = None

    pairs = _lwt_preheat_pairs(plan, forecast, indoor_c=indoor_c)
    if not pairs:
        return 0

    # Deterministic quota cap: 2 writes per pair (action + restore). Keep only
    # as many earliest pairs as fit under headroom = quota_remaining − reserve.
    try:
        from ..api_quota import quota_remaining
        reserve = int(getattr(config, "DAIKIN_RESERVE_FOR_HEARTBEAT", 30))
        headroom = max(0, quota_remaining("daikin") - reserve)
    except Exception:  # pragma: no cover — quota lookup must never break dispatch
        headroom = 9999
    max_pairs = headroom // 2
    if max_pairs <= 0:
        logger.info(
            "LWT pre-heat: skipped all %d offset row(s) — Daikin quota headroom %d too low",
            len(pairs), headroom,
        )
        return 0
    if len(pairs) > max_pairs:
        logger.info(
            "LWT pre-heat: capped offset windows %d→%d for quota headroom %d",
            len(pairs), max_pairs, headroom,
        )
        pairs = pairs[:max_pairs]

    count = 0
    for restore_row, action_row in pairs:
        rid: int | None = None
        if restore_row is not None:
            rid = db.upsert_action(
                plan_date=plan_date,
                start_time=restore_row["start_time"],
                end_time=restore_row["end_time"],
                device="daikin",
                action_type="restore",
                params=restore_row["params"],
                status="pending",
            )
            count += 1
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
        if rid is not None:
            db.update_action_restore_link(aid, rid)
        count += 1

    logger.info(
        "write_daikin_from_lp_plan: LWT pre-heat — wrote %d offset row(s)", count
    )
    return count


def _legionella_windows_utc(slot_starts_utc: list[datetime]) -> list[tuple[datetime, datetime]]:
    """The firmware's legionella stand-off windows intersecting the horizon (UTC)."""
    if not bool(getattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", True)):
        return []
    dow = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", 6))
    hh = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 11))
    mm = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", 0))
    dur = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120))
    days = {st.date() for st in slot_starts_utc}
    out = []
    for d in days:
        if d.weekday() != dow:
            continue
        start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC)
        out.append((start, start + timedelta(minutes=dur)))
    return sorted(out)


def _trim_rows_around_legionella(rows, slot_starts_utc):
    """Cut the legionella windows OUT of any overlapping row, keeping the parts
    outside. Checking only a row's start is not enough: run-length-encoded blocks can
    span hours, and a setback that starts Saturday night crosses straight through the
    Sunday window — HEM must not write into a window it declared firmware-owned, but
    the household still needs a target on both sides of it."""
    windows = _legionella_windows_utc(list(slot_starts_utc))
    if not windows:
        return list(rows)
    out = []
    for r in rows:
        pieces = [(r.start_utc, r.end_utc)]
        for ws, we in windows:
            next_pieces = []
            for ps, pe in pieces:
                if pe <= ws or ps >= we:
                    next_pieces.append((ps, pe))  # no overlap
                    continue
                if ps < ws:
                    next_pieces.append((ps, ws))
                if pe > we:
                    next_pieces.append((we, pe))
            pieces = next_pieces
        for ps, pe in pieces:
            if (pe - ps) >= timedelta(minutes=30):
                out.append(type(r)(
                    action_type=r.action_type, start_utc=ps, end_utc=pe,
                    tank_temp_c=r.tank_temp_c, tank_powerful=r.tank_powerful,
                ))
    return out


def _write_lp_owned_tank_schedule(plan_date: str, plan: LpPlan) -> int:
    """Translate the LP-owned tank trajectory into Daikin rows (#714).

    Clears the tank rows over the plan horizon, compresses the trajectory into a few
    setpoint rows, lays the comfort backstop over the shower window, drops rows inside
    the firmware-owned legionella window, and upserts. One batch, one owner.
    """
    from ..dhw import comfort as _dhw_comfort
    from ..dhw import dispatch as _dhw_dispatch

    if not plan.slot_starts_utc:
        return 0
    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))

    # Clear the tank rows over the whole horizon (in-flight preservation lives in
    # clear_actions_in_range). Same range the K1 branch clears — mutual exclusion.
    win_start = plan.slot_starts_utc[0].isoformat().replace("+00:00", "Z")
    win_end = (plan.slot_starts_utc[-1] + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    db.clear_actions_in_range(win_start, win_end, device="daikin")

    rows = _dhw_dispatch.tank_rows_from_plan(
        list(plan.slot_starts_utc),
        list(plan.tank_temp_c),
        list(plan.dhw_electric_kwh),
        list(plan.price_pence),
    )

    # Comfort backstop over the shower window — reads only the declared constant.
    preset = (config.OPTIMIZATION_PRESET or "normal").strip().lower()
    backstop = _dhw_comfort.backstop_floor_c(preset)
    if backstop is not None:
        windows = _dhw_comfort.shower_windows(preset=preset)
        evening = next((w for w in windows if w.label.startswith("evening")), None)
        if evening is not None:
            before = list(rows)
            rows = _dhw_dispatch.apply_comfort_backstop(
                rows, list(plan.slot_starts_utc), tz, backstop_c=backstop,
                window_start_hour=evening.start_hour, window_end_hour=evening.end_hour,
            )
            if rows != before:
                # A backstop that HAD to act means the plan left the tank cold — the
                # regime's health alarm (two days running ⇒ turn LP-owned off).
                try:
                    db.log_action(device="daikin", action="dhw_comfort_backstop_fired",
                                  params={"backstop_c": backstop, "preset": preset},
                                  result="applied", trigger="lp_dispatch")
                except Exception:  # noqa: BLE001 — telemetry must not break dispatch
                    pass

    # Trim rows around the firmware-owned legionella stand-off (the firmware drives the
    # tank there; a HEM write inside is arbitrated/wasted). TRIM, not drop: a long
    # setback row that merely CROSSES the Sunday window must keep its before/after
    # parts — dropping it entirely would leave the tank with no target for hours. The
    # LP already budgeted the cycle's energy.
    kept = _trim_rows_around_legionella(rows, plan.slot_starts_utc)

    count = 0
    for r in kept:
        db.upsert_action(
            plan_date=plan_date,
            start_time=r.start_utc.isoformat().replace("+00:00", "Z"),
            end_time=r.end_utc.isoformat().replace("+00:00", "Z"),
            device="daikin",
            action_type=r.action_type,
            params=r.to_params(),
            status="pending",
        )
        count += 1
    return count


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

    # #714 — LP-OWNED regime. Gated on ``plan.dhw_lp_owned`` (the plan's own record of
    # which regime produced it), NOT on config: a shadow solve that forced the regime
    # on must NEVER reach hardware, and the plan flag is what distinguishes a committed
    # LP-owned solve from a shadow. This branch and the K1 branch below are mutually
    # exclusive and write to the same cleared range, so the tank has exactly one owner.
    if getattr(plan, "dhw_lp_owned", False):
        rows_total = _write_lp_owned_tank_schedule(plan_date, plan)
        rows_total += _write_lwt_preheat_actions(plan_date, plan, forecast)
        logger.info("write_daikin_from_lp_plan: LP-owned — wrote %d rows", rows_total)
        return rows_total

    # PR K1 (2026-05-23) — fixed DHW schedule replaces LP-driven tank.
    # LP still solves over the horizon for battery + space-heating
    # forecasting, but emits no tank-write actions. The deterministic
    # tank schedule comes from :mod:`src.dhw_policy` instead. See
    # ``DHW_FIXED_SCHEDULE_ENABLED`` docstring in config.py for rationale.
    if getattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False):
        from datetime import UTC as _UTC_T
        from datetime import date as _date_t

        from .. import dhw_policy
        from ..db import get_agile_export_rates_in_range
        tz_local_local = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
        try:
            anchor = _date_t.fromisoformat(plan_date)
        except (ValueError, TypeError):
            anchor = datetime.now(tz_local_local).date()

        # K1.1 bug #1 — widen the clear range so it includes today's
        # warmup boundary (12:00 UTC = 13:00 BST). LP runs at, e.g.,
        # 13:30 BST would have clear start = next-half-hour = 14:00 BST,
        # leaving today's tank_warmup row (start 13:00 BST) UNCLEARED
        # while dhw_policy inserts a fresh one → duplicate. Use the
        # dhw_policy horizon (today's warmup → day-after-tomorrow's
        # warmup) as the clear floor + ceiling.
        warmup_hour = int(getattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13))
        clear_floor_local = datetime(
            anchor.year, anchor.month, anchor.day, warmup_hour, 0,
            tzinfo=tz_local_local,
        )
        clear_ceiling_local = clear_floor_local + timedelta(days=2)
        clear_floor_iso = (
            clear_floor_local.astimezone(_UTC_T).isoformat().replace("+00:00", "Z")
        )
        clear_ceiling_iso = (
            clear_ceiling_local.astimezone(_UTC_T).isoformat().replace("+00:00", "Z")
        )
        if plan.slot_starts_utc:
            lp_start_iso = plan.slot_starts_utc[0].isoformat().replace("+00:00", "Z")
            lp_end = plan.slot_starts_utc[-1] + timedelta(minutes=30)
            lp_end_iso = lp_end.isoformat().replace("+00:00", "Z")
            # Widen to cover both LP horizon AND dhw_policy horizon.
            clear_start = min(lp_start_iso, clear_floor_iso)
            clear_end = max(lp_end_iso, clear_ceiling_iso)
        else:
            clear_start = clear_floor_iso
            clear_end = clear_ceiling_iso
        db.clear_actions_in_range(clear_start, clear_end, device="daikin")

        rows_total = 0
        for offset in (0, 1):
            day = anchor + timedelta(days=offset)
            # K1.1 bug #4 — outgoing rates fetch range must match
            # dhw_policy's schedule horizon (warmup → next warmup),
            # NOT the calendar day. Otherwise early-morning negative
            # slots (e.g. 03:00 BST during overnight setback) sit
            # inside day-D's schedule horizon but outside its 00:00
            # calendar-day fetch range → silently skipped.
            day_start_local = datetime(
                day.year, day.month, day.day, warmup_hour, 0,
                tzinfo=tz_local_local,
            )
            day_end_local = day_start_local + timedelta(days=1)
            # Negative-price boost fires on negative IMPORT (Agile) price — "paid
            # to import → load the tank". Must match the LP forecast (1A keys on
            # the import price_line); the export/Outgoing rate is a different
            # tariff and is rarely negative when import plunges.
            import_tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
            try:
                agile = db.get_rates_for_period(
                    import_tariff,
                    day_start_local.astimezone(_UTC_T),
                    day_end_local.astimezone(_UTC_T),
                ) if import_tariff else None
            except Exception as _e:
                logger.debug("dhw_policy: import rates unavailable for %s: %s", day, _e)
                agile = None
            try:
                n = dhw_policy.write_daily_tank_schedule(
                    target_date_local=day,
                    agile_rates=agile,
                    clear_existing=False,  # already cleared widely above
                )
                rows_total += n
            except Exception as e:
                logger.warning("dhw_policy: write for %s failed: %s", day, e)

        # 2026-06-07 paid-window incident — cycle-split boost recovery.
        # The dhw "day" anchors at warmup_hour (13:00 local), so before that
        # hour we are still inside YESTERDAY's cycle. The (0,1) loop above only
        # covers today's + tomorrow's cycles, and the past-date guard drops
        # yesterday's — so a negative-price boost that lands in the live cycle's
        # still-future tail (e.g. an 04:00→12:00 UTC paid window) is silently
        # lost on every overnight re-plan. Re-emit just that cycle's boost rows
        # at their natural window start (a stable upsert key, so re-plans refresh
        # the one row rather than accumulate a fresh one per advancing clip).
        # ``as_of`` only drops windows that have fully ended. No-op when no live
        # negative window.
        now_local_t = datetime.now(tz_local_local)
        if now_local_t.hour < warmup_hour:
            live_anchor = now_local_t.date() - timedelta(days=1)
            live_start_local = datetime(
                live_anchor.year, live_anchor.month, live_anchor.day,
                warmup_hour, 0, tzinfo=tz_local_local,
            )
            live_end_local = live_start_local + timedelta(days=1)
            import_tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
            try:
                agile_live = db.get_rates_for_period(
                    import_tariff,
                    live_start_local.astimezone(_UTC_T),
                    live_end_local.astimezone(_UTC_T),
                ) if import_tariff else None
            except Exception as _e:
                logger.debug("dhw_policy: live-cycle import rates unavailable: %s", _e)
                agile_live = None
            try:
                rows_total += dhw_policy.write_daily_tank_schedule(
                    target_date_local=live_anchor,
                    agile_rates=agile_live,
                    clear_existing=False,  # cleared widely above
                    boosts_only_as_of=datetime.now(_UTC_T),
                    # File under TODAY's plan_date — the heartbeat reconciler
                    # selects rows by today_local; a yesterday-anchored boost
                    # stamped with yesterday's date would never fire.
                    plan_date_override=now_local_t.date().isoformat(),
                )
            except Exception as e:
                logger.warning("dhw_policy: live-cycle boost recovery failed: %s", e)

        # K1.1 bug #6 — log says "0 rows" not "2 days" when vacation
        # silenced both calls. Counting is row-count only now.
        logger.info(
            "write_daikin_from_lp_plan: DHW_FIXED_SCHEDULE — wrote %d rows (no LP tank actions)",
            rows_total,
        )
        # #481 — active space-heating: emit LWT-offset rows alongside the fixed
        # DHW schedule (independent of the tank; gated by DAIKIN_LWT_PREHEAT_ENABLED).
        rows_total += _write_lwt_preheat_actions(plan_date, plan, forecast)
        return rows_total
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
        # Per user pull-based notification preference (memory:
        # feedback_low_push_load.md): the budget guard is an auto-applied
        # FYI — it doesn't require operator action because the guard
        # already handled the situation by dropping low-value actions.
        # Log to journalctl + action_log only; do NOT push to Telegram.
        # The morning brief queries action_log for these rows so the user
        # can see "Daikin quota: dropped N pair(s) today" without manually
        # grepping journalctl.
        logger.info(
            "Daikin write-budget guard: dropped %d low-value action(s) "
            "(headroom=%d). Dropped: %s",
            len(dropped), headroom, ",".join(dropped),
        )
        try:
            db.log_action(
                device="daikin",
                action="budget_guard_drop",
                params={
                    "dropped": dropped,
                    "headroom": headroom,
                    "reserve": reserve,
                    "n_dropped": len(dropped),
                },
                result="dropped",
                trigger="lp_dispatch",
            )
        except Exception as _e:
            logger.warning("budget_guard_drop log_action failed (non-fatal): %s", _e)
    count = 0
    for restore_row, action_row in pairs:
        # Restore may be None when the next action immediately supersedes it
        # (post-process skip in daikin_dispatch_preview avoids 38→45→55 flip).
        rid: int | None = None
        if restore_row is not None:
            rid = db.upsert_action(
                plan_date=plan_date,
                start_time=restore_row["start_time"],
                end_time=restore_row["end_time"],
                device="daikin",
                action_type="restore",
                params=restore_row["params"],
                status="pending",
            )
            count += 1
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
        if rid is not None:
            db.update_action_restore_link(aid, rid)
        count += 1

    # #481 — active space-heating: emit LWT-offset rows (gated by
    # DAIKIN_LWT_PREHEAT_ENABLED). The clear above already removed any prior
    # offset rows in range, so this re-establishes them idempotently.
    count += _write_lwt_preheat_actions(plan_date, plan, forecast)

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

    1. ``scenarios is None`` (trigger reason not in
       ``LP_SCENARIOS_ON_TRIGGER_REASONS``) → commit every ``peak_export``
       slot. Reason: ``no_scenarios_run``.
    2. Pessimistic scenario solve failed → commit (degenerate degrade — better
       to ship the LP's nominal plan than nothing). Reason: ``pessimistic_failed``.
    3. ``pessimistic.export_kwh[i] >= LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH``
       → commit. Reason: ``robust``.
    4. Economic margin must clear the future refill + wear shadow. Otherwise
       drop. Reason: ``economic_margin``.
    5. Otherwise → drop. Reason: ``pessimistic_disagrees``.

    PR C removed the prior ``ENERGY_STRATEGY_MODE=strict_savings`` kill
    switch. Vacation mode (``OPTIMIZATION_PRESET=vacation``) instead goes
    the opposite direction (max arbitrage); normal/guests rely on the
    scenario-LP filter below.

    Scenarios dict keys are the ``Scenario`` literal type ("optimistic",
    "nominal", "pessimistic") but accepted as plain strings to keep this
    module independent of the scenarios import path.
    """
    raw_slots = lp_dispatch_slots_for_hardware(plan)
    decisions: list[dict[str, Any]] = []
    out_slots: list[HalfHourSlot] = []

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
            # PR C — ``ENERGY_STRATEGY_MODE=strict_savings`` was the prior
            # kill switch (drop every peak_export). It is removed in PR C:
            # the household never wants strict_savings (per user
            # 2026-05-22), and vacation mode goes the opposite direction
            # (max arbitrage). The scenario-LP robustness filter below
            # (pessimistic floor + economic margin) is now the sole gate.
            if scenarios is None:
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


def _dispatch_horizon_cutoff(slots: list[HalfHourSlot]) -> datetime:
    """UTC cutoff of the daily-cyclic dispatch horizon (see build_fox_groups_from_lp)."""
    horizon_h = float(getattr(config, "FOX_DISPATCH_HORIZON_HOURS", 23.0))
    horizon_h = min(23.5, max(1.0, horizon_h))
    return slots[0].start_utc + timedelta(hours=horizon_h)


def summarize_plan_dispatch_coherence(
    slots: list[HalfHourSlot],
    groups: list[SchedulerGroup],
    *,
    replan_at_utc: datetime | None = None,
) -> dict[str, Any]:
    """Per-slot audit of LP plan vs the FINAL Fox V3 group list (issue #670).

    ``dispatch_decisions`` only covers the peak-export robustness filter; the
    lossy-but-legitimate translations (8-group compression squash, trivial
    SelfUse drop, 23 h D+1 horizon trim) were invisible. For every LP slot in
    ``slots`` (the post-robustness-filter, PRE-trim list) compare the work mode
    its kind implies (:func:`_slot_fox_tuple`) against the work mode of the
    final group covering its local minute-of-day (no group → firmware SelfUse
    default). Trivial SelfUse slots (mode SelfUse at the global reserve floor)
    count as MATCHES when no group covers them — dropping those groups is
    behaviour-neutral by design.

    ``severe`` flags planned ForceCharge/ForceDischarge that ended up
    SelfUse/absent/Backup inside the dispatched horizon — planned grid-charge
    or export silently lost. Backup counts because the Camada-3 Backup-pin
    squash (see ``_merge_fox_groups``) neither grid-charges (pinned
    minSoc=maxSoc=reserve) nor discharges, so an FC fill or FD export under
    it is lost just like under SelfUse. (Rare benign flag: a
    ``LP_NEGATIVE_HOLD_FOX_MODE=forcecharge`` hold squashed into a Backup
    hold — non-default config AND over-cap, hold behaviour preserved.)
    Horizon-trimmed and cap-truncated tails are never severe: the scheduled
    MPC re-solve re-dispatches them.
    """
    tz = TZ()
    reserve = int(config.MIN_SOC_RESERVE_PERCENT)
    cutoff = _dispatch_horizon_cutoff(slots) if slots else None
    spans = [
        (g.start_hour * 60 + g.start_minute, g.end_hour * 60 + g.end_minute, g.work_mode)
        for g in groups
    ]
    matched = 0
    trivial_drops = 0
    mismatches: dict[tuple[str, str, str], int] = {}
    severe: list[dict[str, Any]] = []
    for s in slots:
        key = _slot_fox_tuple(s, peak_export_discharge=False)
        expected = key[0]
        trivial = expected == "SelfUse" and int(key[3]) == reserve
        # Slots past the horizon cutoff never reached _merge_fox_groups; their
        # minute-of-day may coincide with a D0 group (daily-cyclic clock), so
        # classify them FIRST rather than trusting a group lookup hit.
        if cutoff is not None and s.start_utc >= cutoff:
            if trivial:
                matched += 1
            else:
                k = (expected, "absent", "horizon_trim")
                mismatches[k] = mismatches.get(k, 0) + 1
            continue
        ls = s.start_utc.astimezone(tz)
        minute = ls.hour * 60 + ls.minute
        # Group ends carry TWO conventions: _merge_fox_groups only rewrites
        # on-the-hour ends to :59 (inclusive); half-hour ends stay :30, i.e.
        # EXCLUSIVE (same as _prepend_inflight_group's plan-group check).
        # Strict `< ge` is correct for BOTH: it keeps the :30 boundary slot
        # out of the predecessor group, and a :59-adjusted end still covers
        # its last real slot minute (slot minutes are multiples of 30).
        actual = next((wm for gs, ge, wm in spans if gs <= minute < ge), None)
        if actual == expected:
            matched += 1
            continue
        if actual is None:
            if trivial:
                matched += 1  # trivial-SelfUse drop: firmware default == plan
                trivial_drops += 1
                continue
            if replan_at_utc is not None and s.start_utc >= replan_at_utc:
                reason = "group_cap_truncation"  # replan already scheduled — not severe
            else:
                reason = "other"
            actual = "absent"
        else:
            reason = "group_cap_compression"
        k = (expected, actual, reason)
        mismatches[k] = mismatches.get(k, 0) + 1
        # A planned HOLD (expected Backup — positive-price A1 hold, negative_hold,
        # or solar_charge) that degrades to SelfUse or absent is the exact
        # 2026-07-10 / daily-cyclic-V3-collision incident signature: the hold's
        # discharge-freeze is silently lost and the battery drains into load. It
        # MUST alarm, so Backup→SelfUse/absent is severe alongside the FC/FD
        # grid-charge/export losses. (Backup→ForceCharge is NOT severe — an FC
        # with fdSoc≤SoC also holds; and horizon-trim/truncation stay benign.)
        _hold_lost = (
            expected == "Backup"
            and actual in ("SelfUse", "absent")
            and reason in ("group_cap_compression", "other")
        )
        if _hold_lost or (
            expected in ("ForceCharge", "ForceDischarge")
            and actual in ("SelfUse", "absent", "Backup")
            and reason in ("group_cap_compression", "other")
        ):
            severe.append(
                {
                    "slot_start_utc": s.start_utc.isoformat(),
                    "kind": s.kind,
                    "expected": expected,
                    "actual": actual,
                    "reason": reason,
                    "price_pence": s.price_pence,
                }
            )
    return {
        "total_slots": len(slots),
        "matched": matched,
        "mismatched": len(slots) - matched,
        "trivial_selfuse_drops": trivial_drops,
        # Pre-bridge count: _prepend_inflight_group may add ONE group at
        # upload time for the in-flight slot (before the plan window, so
        # plan-slot coverage above is unaffected).
        "groups_uploaded": len(groups),
        "severe_count": len(severe),
        "severe": severe[:12],
        "mismatches": [
            {"expected": e, "actual": a, "reason": r, "count": c}
            for (e, a, r), c in sorted(mismatches.items())
        ],
    }


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
        # < 24 h — the daily-cyclic collision fix (2026-07-04, the TRUE root
        # cause of the 06-28 + 07-04 negative-window leaks). Fox V3 groups
        # carry only hour:minute and repeat every day, so a full-24 h
        # horizon's LAST slot is tomorrow's slot at the SAME hour-of-day as
        # the current in-flight slot. When tomorrow's slot is solar_charge
        # (SelfUse) and today's in-flight slot is a negative-window fill/hold,
        # the inverter applies tomorrow's SelfUse group TODAY, mid-window —
        # and _prepend_inflight_group sees "a plan group covers the current
        # minute" and declines to bridge. Trimming the horizon leaves the
        # current hour-of-day uncovered so the in-flight bridge re-asserts
        # the previous schedule's FC/FD/Backup group. 23.5 h drops exactly
        # the one colliding slot; the default 23.0 h adds a spare slot of
        # margin (also covers the DST fall-back day, where the hour-of-day
        # mapping shifts by 1 h). Dropped D+1 slots cost nothing — re-solves
        # re-dispatch them dozens of times before they matter, and choice
        # variance shrinks as the window approaches.
        cutoff = _dispatch_horizon_cutoff(slots)
        slots = [s for s in slots if s.start_utc < cutoff]
    # Live SoC (%) the LP was seeded with at solve time — the best available
    # "current SoC" for the no-import-hold invariant (#679): a Backup group at a
    # positive price must not have maxSoc above it, else fw<1.55 grid-imports
    # toward the ceiling. plan.soc_kwh[0] is the initial-state SoC.
    live_soc_pct: float | None = None
    cap = float(config.BATTERY_CAPACITY_KWH)
    if plan.soc_kwh and cap > 0:
        live_soc_pct = plan.soc_kwh[0] / cap * 100.0

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
        live_soc_pct=live_soc_pct,
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


def _prepend_inflight_group(
    groups: list[SchedulerGroup],
    *,
    now_local: datetime | None = None,
) -> list[SchedulerGroup]:
    """Re-assert the in-progress slot's work mode so a mid-slot re-upload can't
    drop a live ForceCharge/ForceDischarge to the firmware's SelfUse default.

    The plan window starts at the NEXT half-hour boundary (``_ceil_to_half_hour_utc``
    — quota integrity for Daikin), but a Fox upload REPLACES the whole schedule.
    So when a re-solve fires *mid-slot* (e.g. the ``negative_window_start``
    trigger), the in-progress slot has no group and the firmware falls back to
    SelfUse for its remainder — silently stopping force-charge during a paid
    negative-price slot (observed 2026-06-04, upload left 13:34–14:00 BST bare).

    We bridge ``[slot_start, next_boundary)`` with the work mode the schedule
    IN FORCE at the slot's start had for *now*, so whatever was decided for the
    live slot survives the re-upload. Only active modes are carried (SelfUse is
    the firmware default anyway). Issue #458 follow-up; ``FOX_PRESERVE_INFLIGHT_GROUP``
    set false to roll back.

    #693 additions: (1) the in-force lookup walks past a boot-time safe-defaults
    wipe (disabled/empty state row saved WITHIN the current slot) to the schedule
    it replaced — `apply_safe_defaults` on recovery used to starve the bridge and
    leave up to 29 min of firmware SelfUse during a paid negative slot (observed
    2026-07-12). (2) When NO authoritative schedule exists at all
    (``FOX_INFLIGHT_EXTEND_FIRST_GROUP``), a bridge is synthesized from the
    plan's first group ONLY when that group starts exactly at the next boundary
    with a ForceCharge/Backup mode AND the current slot's import price is
    NEGATIVE — the one regime where blind charging/holding strictly beats the
    SelfUse default. At positive prices the LP never evaluated the current
    slot, and e.g. extending a cheap-window ForceCharge back into a peak slot
    could cost ~80p in 29 min; SelfUse is the least-bad blind default there.
    """
    if not groups or not getattr(config, "FOX_PRESERVE_INFLIGHT_GROUP", True):
        return groups
    tz = TZ()
    now_local = now_local or datetime.now(tz)
    now_min = now_local.hour * 60 + now_local.minute
    slot_start_min = (now_min // 30) * 30          # floor to half-hour
    slot_end_min = slot_start_min + 30             # exclusive end = next boundary

    # Only bridge a genuine GAP: if ANY plan group already covers the current
    # minute, the plan has explicitly decided this slot — re-asserting the old
    # mode would both fight that decision and OVERLAP that group. Checking every
    # group (not just groups[0]) is the fix for the 2026-06-14 wedge: a later
    # group spanning the live slot let a stale ForceCharge bridge overlap it,
    # the overlap guard refused the whole upload, and the inverter ran an
    # obsolete (grid-force-charging) schedule for ~41 h.
    # Bail if ANY plan group INTERSECTS the current slot window — the plan has
    # decided (part of) this slot, and a bridge spanning the slot would both
    # fight that decision and OVERLAP the group (2026-06-14 wedge: the overlap
    # guard then refused the whole upload → 41 h stale schedule). Intersection
    # (not just covers-now) also makes the check midnight-correct: in the
    # 23:30–00:00 slot a group starting 00:00 (= tomorrow's first slot) does
    # NOT intersect, so the last slot of the day is no longer a dead zone.
    # Group ends: :00/:30 are exclusive, anything else (:29/:59) inclusive.
    for g in groups:
        gs = g.start_hour * 60 + g.start_minute
        ge = g.end_hour * 60 + g.end_minute
        ge_excl = ge if ge % 30 == 0 else ge + 1
        if gs < slot_end_min and slot_start_min < ge_excl:
            return groups

    # #693 — find the schedule IN FORCE at the current slot's start, looking back
    # past a boot-time safe-defaults wipe (a disabled/empty row saved WITHIN the
    # current slot). A disabled row saved BEFORE the slot start means the
    # scheduler was genuinely off when the slot began — nothing to re-assert.
    slot_start_local = now_local.replace(
        hour=slot_start_min // 60, minute=slot_start_min % 60, second=0, microsecond=0
    )
    # NB the enabled row that becomes in_force has no age bound ON PURPOSE:
    # even after days of downtime (crash, no shutdown wipe), the inverter was
    # physically executing that schedule until the boot wipe seconds ago —
    # re-asserting its mode for <=29 min preserves hardware continuity, and the
    # next-boundary re-plan supersedes it. Upload staleness itself is alarmed
    # separately (actuation-health, #562).
    in_force: dict[str, Any] | None = None
    try:
        rows = db.get_recent_fox_schedule_states(limit=12)
        verdict_reached = not rows
        for row in rows:
            if row.get("enabled"):
                in_force = row
                verdict_reached = True
                break
            try:
                wiped_at = datetime.fromisoformat(str(row.get("uploaded_at")))
                stale_wipe = wiped_at < slot_start_local.astimezone(wiped_at.tzinfo)
            except (ValueError, TypeError):
                verdict_reached = True             # unparseable — stop, treat as no authority
                break
            if stale_wipe:
                verdict_reached = True             # off since before this slot started
                break
            # wipe within the current slot (e.g. boot recovery) — keep looking back
        if not verdict_reached:
            logger.warning(
                "Fox in-flight preserve: %d state rows exhausted without a verdict "
                "(restart flapping?) — treating as no in-force schedule", len(rows),
            )
    except Exception as e:                          # never block an upload on this read
        logger.debug("Fox in-flight preserve: prev-schedule read failed: %s", e)
        return groups

    sh, sm = divmod(slot_start_min, 60)
    eh, em = divmod(slot_end_min - 1, 60)          # :59 inclusive-minute convention

    if in_force is not None:
        carry = None
        for pg in in_force.get("groups") or []:
            try:
                ps = int(pg["startHour"]) * 60 + int(pg["startMinute"])
                pe = int(pg["endHour"]) * 60 + int(pg["endMinute"])
            except (KeyError, TypeError, ValueError):
                continue
            # Dual end conventions (see reference above): :00/:30 exclusive,
            # :29/:59 inclusive. An upload landing IN the boundary minute must
            # not carry the group that just ENDED at that boundary.
            pe_excl = pe if pe % 30 == 0 else pe + 1
            if ps <= now_min < pe_excl:
                carry = pg
                break
        if carry is None:
            # In-force schedule has no group for the current slot — a DELIBERATE
            # SelfUse gap (FOX_SKIP_TRIVIAL_SELFUSE_GROUPS elides SelfUse
            # windows); leave it bare.
            return groups
        wm = carry.get("workMode")
        if wm not in ("ForceCharge", "ForceDischarge", "Backup"):
            return groups                          # SelfUse is the default — nothing to preserve
        if len(groups) >= 8:
            return groups                          # no room under the Fox V3 8-group cap
        extra = carry.get("extraParam") or {}
        bridge = SchedulerGroup(
            start_hour=sh, start_minute=sm,
            end_hour=eh, end_minute=em,
            work_mode=wm,
            min_soc_on_grid=int(extra.get("minSocOnGrid", config.MIN_SOC_RESERVE_PERCENT)),
            fd_soc=extra.get("fdSoc"),
            fd_pwr=extra.get("fdPwr"),
            max_soc=extra.get("maxSoc"),
        )
        logger.info(
            "Fox upload: re-asserting in-flight %s for current slot %02d:%02d-%02d:%02d "
            "— prevents mid-slot SelfUse gap",
            wm, sh, sm, eh, em,
        )
        return [bridge] + groups

    # #693 no-authority fallback — nothing was in force at the slot start (fresh
    # install, or scheduler off since before the slot began). Synthesize a bridge
    # from the plan's first-boundary group ONLY when it is ForceCharge/Backup AND
    # the current slot's import price is NEGATIVE: there, importing/holding
    # strictly beats the SelfUse default (which discharges the battery to avoid
    # PAID import). At positive prices the LP never priced this slot — extending
    # a cheap-window ForceCharge back into a peak slot could cost ~80p in 29 min
    # — so SelfUse stays the blind default. ForceDischarge is never synthesized
    # (its value depends on the export rate, unknown here).
    if not getattr(config, "FOX_INFLIGHT_EXTEND_FIRST_GROUP", True):
        return groups
    if len(groups) >= 8:
        return groups
    next_boundary_min = slot_end_min % 1440        # 23:30 slot → tomorrow 00:00
    g0 = next(
        (g for g in groups
         if g.start_hour * 60 + g.start_minute == next_boundary_min),
        None,
    )
    if g0 is None or g0.work_mode not in ("ForceCharge", "Backup"):
        return groups
    try:
        price_p = db.get_agile_rate_at(slot_start_local)
    except Exception as e:
        logger.debug("Fox in-flight preserve: price lookup failed: %s", e)
        return groups
    if price_p is None or price_p >= 0:
        return groups
    bridge = dataclasses.replace(g0, start_hour=sh, start_minute=sm, end_hour=eh, end_minute=em)
    logger.info(
        "Fox upload: no in-force schedule and current slot is negative (%.2fp) — "
        "bridging %02d:%02d-%02d:%02d with the plan's next-boundary %s",
        price_p, sh, sm, eh, em, g0.work_mode,
    )
    return [bridge] + groups


def upload_fox_if_operational(fox: FoxESSClient | None, groups: list[SchedulerGroup]) -> bool:
    fox_ok = False
    if fox and fox.api_key and not config.OPENCLAW_READ_ONLY:
        plan = groups                              # the LP/heuristic plan, no bridge
        candidate = _prepend_inflight_group(plan)
        if _detect_overlapping_groups(candidate) and candidate is not plan:
            # The in-flight bridge collides with a plan group. DROP THE BRIDGE
            # and upload the plan as-is rather than refuse the whole upload — a
            # refusal leaves the STALE schedule live, which is exactly what
            # wedged Fox dispatch (grid-force-charging on an obsolete plan) for
            # ~41 h on 2026-06-14. The plan itself is still overlap-checked below.
            logger.warning(
                "Fox upload: in-flight bridge overlaps the plan — dropping the "
                "bridge and uploading the plan as-is (avoids the stale-schedule wedge)",
            )
            candidate = plan
        groups = candidate
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
