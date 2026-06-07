"""DHW (tank) policy — fixed deterministic daily schedule that replaces
the LP-driven tank arbitrage stack (PR G/H/I/J).

Per user 2026-05-23:

    Overnight (22:00 → 13:00 next day):  tank = SETBACK (37 °C)
    Daytime  (13:00 → 22:00):            tank = NORMAL  (45 °C, evening showers)
    Guests mode:                          tank = NORMAL 24 h (no setback —
                                            morning showers possible)
    Vacation mode:                        no actions (Daikin firmware handles)
    Negative-price slots (Outgoing < 0):  override to BOOST (60 °C) for the
                                            duration of the negative window

The policy intentionally does NOT optimize for tariff arbitrage on DHW
(except the negative-price case where the grid is paying us). The user's
explicit constraint: "battery first, tank second; don't drain the battery
overnight just to keep water hot when no shower is happening." The
~£20-50/year of DHW arb savings are sacrificed in exchange for operational
simplicity and removing an entire class of bugs (drift checks, restore
preservation chains, override propagation conflicts).

This module is the SINGLE SOURCE OF TRUTH for tank actions when
``DHW_FIXED_SCHEDULE_ENABLED=True``. The LP still optimizes battery /
forecasts space heating, but emits no tank-write actions.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import config

logger = logging.getLogger(__name__)


def _tz_local() -> ZoneInfo:
    return ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _make_action(
    *,
    action_type: str,
    start_utc: datetime,
    end_utc: datetime,
    tank_temp_c: int,
    tank_powerful: bool = False,
) -> dict[str, Any]:
    """Build an action_schedule-shaped row dict ready for db.upsert_action."""
    return {
        "device": "daikin",
        "action_type": action_type,
        "start_time": _iso_z(start_utc),
        "end_time": _iso_z(end_utc),
        "params": {
            "tank_power": True,
            "tank_temp": int(tank_temp_c),
            "tank_powerful": tank_powerful,
            "dhw_policy": True,  # marker that this row came from dhw_policy
        },
    }


def _shower_in_span(start_utc: datetime, end_utc: datetime, tz: ZoneInfo) -> bool:
    """True if any configured DHW shower window overlaps ``[start, end)`` in
    local time (#tank-precool guard). Parses ``DHW_SHOWER_SCHEDULE``
    (``"HH:MM-HH:MM,…"``). Conservative: any parse trouble → True (don't
    pre-cool when unsure). Empty schedule → False.
    """
    sched = (getattr(config, "DHW_SHOWER_SCHEDULE", "") or "").strip()
    if not sched or end_utc <= start_utc:
        return False
    windows: list[tuple[int, int]] = []
    for part in sched.split(","):
        part = part.strip()
        if "-" not in part:
            continue
        try:
            a, b = part.split("-", 1)
            sh, sm = a.split(":")
            eh, em = b.split(":")
            windows.append((int(sh) * 60 + int(sm), int(eh) * 60 + int(em)))
        except ValueError:
            return True  # unparseable → assume a shower could be there
    if not windows:
        return False
    day = start_utc.astimezone(tz).date()
    last = end_utc.astimezone(tz).date()
    while day <= last:
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
        for ws, we in windows:
            sw = (midnight + timedelta(minutes=ws)).astimezone(UTC)
            ew = (midnight + timedelta(minutes=we)).astimezone(UTC)
            if sw < end_utc and ew > start_utc:
                return True
        day = day + timedelta(days=1)
    return False


def _detect_negative_windows(
    agile_rates: list[dict[str, Any]] | None,
    horizon_start_utc: datetime,
    horizon_end_utc: datetime,
) -> list[tuple[datetime, datetime]]:
    """Group consecutive negative-price 30-min slots into contiguous windows.

    ``agile_rates`` is a list of dicts with at least ``valid_from`` (ISO
    UTC) and ``value_inc_vat`` (pence/kWh). Returns list of (start, end)
    UTC datetime tuples; empty when no negative slots in horizon.
    """
    if not agile_rates:
        return []
    neg_slot_starts: list[datetime] = []
    for r in agile_rates:
        try:
            ts_raw = r.get("valid_from") or r.get("slot_time_utc")
            rate = float(r.get("value_inc_vat", r.get("rate_p", 999)))
        except (TypeError, ValueError):
            continue
        if not ts_raw or rate >= 0:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if not (horizon_start_utc <= ts < horizon_end_utc):
            continue
        neg_slot_starts.append(ts)
    if not neg_slot_starts:
        return []
    neg_slot_starts.sort()
    # Group consecutive 30-min slots
    windows: list[tuple[datetime, datetime]] = []
    cur_start = neg_slot_starts[0]
    cur_end = cur_start + timedelta(minutes=30)
    for ts in neg_slot_starts[1:]:
        if ts == cur_end:  # contiguous
            cur_end = ts + timedelta(minutes=30)
        else:
            windows.append((cur_start, cur_end))
            cur_start = ts
            cur_end = ts + timedelta(minutes=30)
    windows.append((cur_start, cur_end))
    return windows


def generate_daily_tank_schedule(
    target_date_local: date,
    *,
    agile_rates: list[dict[str, Any]] | None = None,
    mode: str | None = None,
    allow_past: bool = False,
    boosts_only_from: datetime | None = None,
) -> list[dict[str, Any]]:
    """Generate tank action rows for the local calendar day starting at
    ``DHW_WARMUP_START_HOUR_LOCAL`` (default 13:00) on ``target_date_local``
    and ending at the same time the following day.

    The window matches the user's mental model: "a day's tank cycle starts
    at the afternoon warmup and ends at the next afternoon warmup". This
    way one call covers an entire warmup → setback → next-warmup cycle.

    Args:
        target_date_local: anchor day in local TZ
        agile_rates: optional list of {valid_from, value_inc_vat}; used
            to detect negative-price windows for boost overrides
        mode: optimization preset; defaults to ``config.OPTIMIZATION_PRESET``
        boosts_only_from: when set, return ONLY this cycle's
            ``tank_negative_boost`` rows, each clipped to start at
            ``max(window_start, boosts_only_from)`` (drop any already ended).
            Used by the writer to recover the early-morning paid boost of the
            *currently-live* cycle (anchored at yesterday's warmup) that the
            cycle-split otherwise drops on an overnight re-plan — the
            2026-06-07 paid-window incident. Structural warmup/setback rows are
            deliberately NOT re-emitted: they already fired when the cycle
            began, and a fresh setback sharing the boost's start would thrash
            against it (neither row ever reaches a state-matched "completed").

    Returns:
        List of action dicts; empty when ``mode='vacation'``.
    """
    if mode is None:
        mode = (config.OPTIMIZATION_PRESET or "normal").strip().lower()

    if mode == "vacation":
        return []

    # Past-date guard (K1.1 bug #5): generating rows for yesterday is
    # always a no-op for the WRITER — the heartbeat would try to fire them
    # immediately and they'd be wasted churn. Tomorrow is fine (advance
    # scheduling). ``allow_past=True`` lets read-only callers (e.g. the
    # heating-plan timeline endpoint) regenerate a past day's deterministic
    # schedule for display without writing anything. ``boosts_only_from``
    # deliberately reaches back into the live (yesterday-anchored) cycle, so it
    # also bypasses this guard — it clips to ``boosts_only_from`` instead.
    today_local = datetime.now(_tz_local()).date()
    if boosts_only_from is None and not allow_past and target_date_local < today_local:
        logger.info(
            "dhw_policy: skipping %s (in the past; today=%s)",
            target_date_local, today_local,
        )
        return []

    tz = _tz_local()
    warmup_hour = int(getattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13))
    setback_hour = int(getattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22))
    normal_c = int(round(float(config.DHW_TEMP_NORMAL_C)))
    setback_c = int(round(float(getattr(config, "DHW_TEMP_SETBACK_C", 37))))
    # Negative-price boost target → MAX (free money to heat). Clamp to DHW_TEMP_MAX_C.
    boost_c = int(round(min(
        float(getattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 65)),
        float(config.DHW_TEMP_MAX_C),
    )))

    # DST-safe anchor construction (K1.1 bug #3 fix). Building each
    # boundary explicitly via ``datetime(..., tzinfo=tz)`` lets ZoneInfo
    # pick the correct UTC offset for that wall-clock moment. Avoid
    # ``.replace(hour=...)`` and ``+timedelta(days=1)`` here because
    # ``timedelta`` is offset-blind and ``.replace`` keeps the source
    # tzinfo even when DST has flipped between the two times.
    next_day = target_date_local + timedelta(days=1)
    warmup_start = datetime(
        target_date_local.year, target_date_local.month, target_date_local.day,
        warmup_hour, 0, tzinfo=tz,
    )
    setback_start = datetime(
        target_date_local.year, target_date_local.month, target_date_local.day,
        setback_hour, 0, tzinfo=tz,
    )
    next_warmup = datetime(
        next_day.year, next_day.month, next_day.day,
        warmup_hour, 0, tzinfo=tz,
    )

    # Negative-price boost windows are the sole permitted exception to the fixed
    # schedule. Detect them FIRST so they can genuinely SUPERSEDE the leading
    # warmup (not just sit alongside it): we must never pre-heat to normal_c at a
    # positive price right before a free, paid-to-import boost to boost_c.
    warmup_start_utc = warmup_start.astimezone(UTC)
    setback_start_utc = setback_start.astimezone(UTC)
    next_warmup_utc = next_warmup.astimezone(UTC)
    neg_windows = _detect_negative_windows(agile_rates, warmup_start_utc, next_warmup_utc)

    # Boosts-only recovery path: emit just the negative-boost rows of this
    # (already-running) cycle, clipped to ``boosts_only_from``. No structural
    # rows — see the param docstring. Keeps the writer idempotent: the next
    # re-plan's range-clear removes these and rewrites with a fresh clip.
    if boosts_only_from is not None:
        boost_rows: list[dict[str, Any]] = []
        for nw_start, nw_end in neg_windows:
            start = max(nw_start, boosts_only_from)
            if start >= nw_end:
                continue  # window already fully elapsed
            boost_rows.append(_make_action(
                action_type="tank_negative_boost",
                start_utc=start,
                end_utc=nw_end,
                tank_temp_c=boost_c,
                tank_powerful=True,
            ))
        return boost_rows

    # If a boost window opens at/near the warmup start, defer the warmup past it
    # (chaining consecutive boosts that each fall within the lead window of the
    # running start). The boost does the heating during the paid window; the
    # warmup then resumes its hold-at-normal_c role afterwards — the tank simply
    # coasts down from boost_c, so no positive-price heating happens until it
    # falls below normal_c. A boost LATER in the day (beyond the lead window of
    # the warmup start) leaves the warmup intact: the afternoon still needs hot
    # water before that boost arrives.
    #
    # The lead window is LP_PRE_NEGATIVE_PRECOOL_HOURS — deliberately the SAME
    # window the LP's energy forecast (forecast_dhw_load_per_slot) uses to
    # pre-cool: the forecast zeroes warmup energy for slots within precool_hours
    # before a negative window, so deferring the fired warmup over exactly that
    # window keeps the actions and the budgeted DHW import consistent. (The LP
    # suppresses the warmup-start slot iff boost_start <= warmup_start +
    # precool — identical to this defer condition.)
    effective_warmup_start_utc = warmup_start_utc
    defer_lead = timedelta(hours=max(
        0.0, float(getattr(config, "LP_PRE_NEGATIVE_PRECOOL_HOURS", 3.0))
    ))
    if neg_windows and defer_lead > timedelta(0):
        for nw_start, nw_end in sorted(neg_windows):
            if nw_start <= effective_warmup_start_utc + defer_lead:
                effective_warmup_start_utc = max(effective_warmup_start_utc, nw_end)
            else:
                break

    # Tank pre-cool into a negative window: drop the setback target toward the
    # device minimum so the paid boost (cold → boost_c) absorbs the most kWh and
    # no positive-price reheat fires just before it. Guarded by no shower in the
    # setback→first-boost span (the boost reheats to boost_c before any later
    # shower). PHYSICS CAVEAT: standing loss (~0.5 °C/h) caps real cooling — for
    # a window soon after the warmup the tank is still coasting well above the
    # target, so the gain is small; the value is mostly far-from-warmup windows
    # + the guaranteed no-reheat-before-paid.
    effective_setback_c = setback_c
    if (
        getattr(config, "DHW_TANK_PRECOOL_ENABLED", False)
        and mode != "guests"
        and neg_windows
    ):
        first_neg_start = min(nw[0] for nw in neg_windows)
        if setback_start_utc <= first_neg_start and not _shower_in_span(
            setback_start_utc, first_neg_start, tz
        ):
            effective_setback_c = min(
                setback_c, int(getattr(config, "DHW_TANK_PRECOOL_TARGET_C", 30))
            )

    rows: list[dict[str, Any]] = []

    if mode == "guests":
        # Single 24h warmup row — no setback during guest visits because of
        # potential morning showers. Skip entirely if a boost chain has deferred
        # the start past the window end.
        if effective_warmup_start_utc < next_warmup_utc:
            rows.append(_make_action(
                action_type="tank_warmup",
                start_utc=effective_warmup_start_utc,
                end_utc=next_warmup_utc,
                tank_temp_c=normal_c,
            ))
    else:
        # Normal mode: warmup → setback → next-day warmup pattern. The warmup is
        # emitted only when it still has positive duration after any boost defer.
        if effective_warmup_start_utc < setback_start_utc:
            rows.append(_make_action(
                action_type="tank_warmup",
                start_utc=effective_warmup_start_utc,
                end_utc=setback_start_utc,
                tank_temp_c=normal_c,
            ))
        rows.append(_make_action(
            action_type="tank_setback",
            start_utc=setback_start_utc,
            end_utc=next_warmup_utc,
            tank_temp_c=effective_setback_c,
        ))

    for nw_start, nw_end in neg_windows:
        rows.append(_make_action(
            action_type="tank_negative_boost",
            start_utc=nw_start,
            end_utc=nw_end,
            tank_temp_c=boost_c,
            tank_powerful=True,  # grid pays us — load all the kWh
        ))

    return rows


def forecast_dhw_load_per_slot(
    slot_starts_utc: list[datetime],
    *,
    mode: str | None = None,
    target_date_local: date | None = None,
    initial_tank_c: float | None = None,
    price_line: list[float] | None = None,
) -> tuple[list[float], list[float]]:
    """Forecast the electric DHW load + tank temperature **trajectory** the
    fixed-schedule policy implies over the given LP horizon.

    Returns ``(e_dhw_kwh_per_slot, tank_temp_c_per_boundary)``:
        * ``e_dhw_kwh_per_slot[i]`` — predicted heat-pump electric draw
          for DHW during slot ``i`` (kWh per 30-min slot).
        * ``tank_temp_c_per_boundary[k]`` — predicted tank °C at slot
          boundary ``k`` (so ``len = N+1``, matching LP's ``tank[]``).

    The model is intentionally simple — we don't optimize anything here,
    just describe what Daikin firmware will plausibly do under the
    dhw_policy schedule. Used by the LP solver to pin its tank/e_dhw
    decision variables instead of letting it drift from reality.

    Energy model (typical 200 L tank, COP ~3.0, ~50 W standing loss):
        * Warmup transition (first slot of warmup window): heat from
          SETBACK→NORMAL = ~0.4 kWh electric (~1.5 kWh thermal)
        * Steady-state warmup at NORMAL, no draws: ~0.04 kWh electric
          per slot (standing-loss replacement only)
        * Evening shower window slots: ~0.5 kWh electric per slot —
          tank reheats the heat lost to taps. ~4 showers × 35 L × ΔT37 °C
          ≈ 6 kWh thermal ≈ 2 kWh electric over ~4 evening slots.
        * Guests-mode morning shower window: same ~0.5 kWh per slot.
        * Setback at 37 °C, no draws: ~0.02 kWh electric per slot
          (smaller standing loss than at 45 °C).
        * Negative-price boost slot: ~0.8 kWh electric (max heating)
        * Vacation: 0 kWh (firmware-only; legionella out of horizon).

    Daily total under normal mode: ~3.7-4 kWh, matching prod Daikin
    telemetry. Without the shower term we under-forecast by ~2 kWh
    (LP would think PV is 2 kWh more available than reality → over-
    aggressive battery arbitrage).
    """
    if mode is None:
        mode = (config.OPTIMIZATION_PRESET or "normal").strip().lower()

    n = len(slot_starts_utc)
    if n == 0:
        return [], []

    tz = _tz_local()
    warmup_hour = int(getattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13))
    setback_hour = int(getattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22))
    normal_c = float(config.DHW_TEMP_NORMAL_C)
    setback_c = float(getattr(config, "DHW_TEMP_SETBACK_C", 37.0))
    boost_c = float(getattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0))

    # Per-slot electric draws (kWh / 30 min). Calibrated to ~3.7-4 kWh
    # daily total which matches prod Daikin telemetry.
    WARMUP_TRANSITION_KWH = 0.40
    WARMUP_MAINTENANCE_KWH = 0.04
    SHOWER_REHEAT_KWH = 0.50            # per slot during shower window
    SETBACK_MAINTENANCE_KWH = 0.02
    BOOST_KWH = 0.80
    VACATION_KWH = 0.00  # firmware-only; legionella cycle excluded from LP horizon

    # Typical evening shower window (local hours, slot-start basis).
    # 20:00→22:00 BST covers the household's "after-dinner shower" pattern.
    # Guests mode adds a morning window 07:00→08:30.
    EVENING_SHOWER_START_H = 20
    EVENING_SHOWER_END_H = 22  # exclusive
    GUESTS_MORNING_SHOWER_START_H = 7
    GUESTS_MORNING_SHOWER_END_H = 9  # exclusive

    def _phase_for_slot(slot_utc: datetime) -> str:
        """Return one of: 'vacation', 'warmup_transition', 'shower_reheat',
        'warmup_maintenance', 'setback'."""
        if mode == "vacation":
            return "vacation"
        slot_local = slot_utc.astimezone(tz)
        h = slot_local.hour

        # Shower windows take priority — biggest load contributor.
        if EVENING_SHOWER_START_H <= h < EVENING_SHOWER_END_H:
            return "shower_reheat"
        if (mode == "guests"
                and GUESTS_MORNING_SHOWER_START_H <= h < GUESTS_MORNING_SHOWER_END_H):
            return "shower_reheat"

        if mode == "guests":
            # Guests: tank always at NORMAL outside shower windows →
            # warmup-level maintenance.
            return "warmup_maintenance"

        # Normal mode: warmup window [warmup_hour, setback_hour), setback otherwise.
        if warmup_hour <= h < setback_hour:
            if h == warmup_hour and slot_local.minute < 30:
                return "warmup_transition"
            return "warmup_maintenance"
        return "setback"

    e_dhw: list[float] = []
    phases: list[str] = []
    for slot in slot_starts_utc:
        phase = _phase_for_slot(slot)
        phases.append(phase)
        if phase == "vacation":
            e_dhw.append(VACATION_KWH)
        elif phase == "warmup_transition":
            e_dhw.append(WARMUP_TRANSITION_KWH)
        elif phase == "shower_reheat":
            e_dhw.append(SHOWER_REHEAT_KWH)
        elif phase == "warmup_maintenance":
            e_dhw.append(WARMUP_MAINTENANCE_KWH)
        else:  # setback
            e_dhw.append(SETBACK_MAINTENANCE_KWH)

    # ----- Initial-tank "warm credit" adjustment ---------------------------
    # If the tank arrives at the LP horizon ABOVE its scheduled target, the
    # heat pump doesn't have to lift it — that stored thermal energy is a
    # gift that offsets the first slots' warmup/reheat load until consumed
    # by standing losses + draws. Without this, the LP forecast would over-
    # estimate e_dhw on transition days (e.g. today after a hot-arrival).
    if initial_tank_c is not None and initial_tank_c > normal_c + 0.5:
        try:
            tank_litres = float(getattr(config, "DHW_TANK_LITRES", 200.0))
            water_cp = float(getattr(config, "DHW_WATER_CP", 4186.0))
            cop_typical = 3.0  # heat-pump average DHW COP; matches lp_optimizer cop_dhw
            excess_thermal_kwh = (
                (initial_tank_c - normal_c) * tank_litres * water_cp / 3.6e6
            )
            excess_electric_kwh = excess_thermal_kwh / cop_typical
            # Spend the credit on the first non-zero slots first (warmup
            # transition + shower reheat are highest-value to offset).
            for i in range(len(e_dhw)):
                if excess_electric_kwh <= 0:
                    break
                if e_dhw[i] <= 0:
                    continue
                reduction = min(e_dhw[i], excess_electric_kwh)
                e_dhw[i] -= reduction
                excess_electric_kwh -= reduction
            logger.debug(
                "dhw_policy: applied %.2f kWh warm-credit (init_tank=%.1f, "
                "normal=%.1f)",
                excess_thermal_kwh / cop_typical, initial_tank_c, normal_c,
            )
        except Exception as _exc:  # pragma: no cover - defensive
            logger.debug("dhw_policy: warm-credit calc failed: %s", _exc)

    # Tank temperature trajectory at slot boundaries. Slot boundary k is
    # the START of slot k (k=0..N-1); boundary N is the END of last slot.
    # Pre-load boundary 0 from the initial state would require it as input;
    # instead we encode the policy's TARGET, not the live state. This is
    # fine for the LP's audit purposes — the actual physical temperature
    # is what dhw_policy commanded via the schedule.
    tank_temps: list[float] = []
    for slot in slot_starts_utc:
        slot_local = slot.astimezone(tz)
        h = slot_local.hour
        if mode == "vacation":
            tank_temps.append(setback_c)  # firmware-owned; setback as proxy
        elif mode == "guests":
            tank_temps.append(normal_c)
        elif warmup_hour <= h < setback_hour:
            tank_temps.append(normal_c)
        else:
            tank_temps.append(setback_c)
    # Boundary N: same as last slot's target (assume flat at end)
    tank_temps.append(tank_temps[-1] if tank_temps else normal_c)

    # ----- Negative-price boost (1A) + pre-cool (1C) -----------------------
    # When paid to import, BUDGET the heat-up energy to drive the tank to MAX so
    # the pinned LP plans the extra import; in the short window before, don't
    # re-warm the tank (let it coast to setback) so it has maximum headroom to
    # absorb. Shower-safe + skipped in vacation (firmware owns the tank).
    if price_line is not None and len(price_line) == n and mode != "vacation":
        boost_target = min(
            float(getattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 65)),
            float(config.DHW_TEMP_MAX_C),
        )
        max_hp_kwh = max(0.05, float(getattr(config, "DAIKIN_MAX_HP_KW", 2.0)) * 0.5)
        tank_litres = float(getattr(config, "DHW_TANK_LITRES", 200.0))
        water_cp = float(getattr(config, "DHW_WATER_CP", 4186.0))
        cop_typical = 3.0  # matches lp_optimizer cop_dhw + warm-credit above
        elec_per_degree = (tank_litres * water_cp / 3.6e6) / cop_typical  # kWh elec / °C
        precool_slots = int(max(0.0, float(getattr(config, "LP_PRE_NEGATIVE_PRECOOL_HOURS", 3.0))) * 2)

        # Contiguous runs of negative-price slots.
        windows: list[tuple[int, int]] = []
        i = 0
        while i < n:
            if price_line[i] < 0:
                j = i
                while j + 1 < n and price_line[j + 1] < 0:
                    j += 1
                windows.append((i, j))
                i = j + 1
            else:
                i += 1

        for ws, we in windows:
            # 1C pre-cool: in the slots just before the window, don't re-warm —
            # coast to setback. Never touch a shower-reheat slot (comfort).
            for k in range(max(0, ws - precool_slots), ws):
                if phases[k] == "shower_reheat":
                    continue
                tank_temps[k] = min(tank_temps[k], setback_c)
                if e_dhw[k] > SETBACK_MAINTENANCE_KWH:
                    e_dhw[k] = SETBACK_MAINTENANCE_KWH
            # 1A boost ramp: heat from the (cooled) entry temp up to boost_target,
            # clamped to the heater's per-slot electric capacity. Short windows
            # only reach what's physically possible.
            entry = float(tank_temps[ws])
            if precool_slots > 0:
                entry = min(entry, setback_c)
                tank_temps[ws] = entry
            cur = entry
            for k in range(ws, we + 1):
                if cur >= boost_target - 1e-6:
                    e_dhw[k] = WARMUP_MAINTENANCE_KWH      # maintain at max
                    tank_temps[k + 1] = boost_target
                    cur = boost_target
                    continue
                lift_deg = (max_hp_kwh / elec_per_degree) if elec_per_degree > 0 else 0.0
                new_temp = min(boost_target, cur + lift_deg)
                e_dhw[k] = min(max_hp_kwh, (new_temp - cur) * elec_per_degree + SETBACK_MAINTENANCE_KWH)
                cur = new_temp
                tank_temps[k + 1] = cur

    return e_dhw, tank_temps


def write_daily_tank_schedule(
    target_date_local: date | None = None,
    *,
    agile_rates: list[dict[str, Any]] | None = None,
    mode: str | None = None,
    clear_existing: bool = True,
    boosts_only_from: datetime | None = None,
) -> int:
    """Write a day's tank schedule into ``action_schedule``.

    The horizon clearing is done by the LP-side ``write_daikin_from_lp_plan``
    when ``DHW_FIXED_SCHEDULE_ENABLED=True``. This function does NOT clear
    by default to allow concurrent LP-side writes (Fox V3 charge actions
    sit in the same horizon but on the ``foxess`` device, not daikin).

    Args:
        target_date_local: defaults to today in local TZ
        agile_rates: optional, for negative-price detection
        mode: optional override; defaults to ``config.OPTIMIZATION_PRESET``
        clear_existing: when True, calls ``db.clear_actions_in_range`` over
            the warmup window before upserting

    Returns:
        Number of rows written.
    """
    if target_date_local is None:
        target_date_local = datetime.now(_tz_local()).date()

    rows = generate_daily_tank_schedule(
        target_date_local,
        agile_rates=agile_rates,
        mode=mode,
        boosts_only_from=boosts_only_from,
    )
    if not rows:
        logger.info("dhw_policy: no rows for %s (mode=%s)", target_date_local, mode)
        return 0

    if clear_existing:
        # Clear daikin actions in the full warmup→next-warmup horizon
        start_iso = rows[0]["start_time"]
        end_iso = rows[-1]["end_time"]
        # tank_negative_boost rows may have earlier start times — use min/max
        start_iso = min(r["start_time"] for r in rows)
        end_iso = max(r["end_time"] for r in rows)
        db.clear_actions_in_range(start_iso, end_iso, device="daikin")

    n_written = 0
    for r in rows:
        try:
            db.upsert_action(
                device=r["device"],
                action_type=r["action_type"],
                start_time=r["start_time"],
                end_time=r["end_time"],
                params=r["params"],
                plan_date=str(target_date_local),
                status="pending",
            )
            n_written += 1
        except Exception as e:
            logger.warning(
                "dhw_policy: upsert failed for %s @ %s: %s",
                r["action_type"], r["start_time"], e,
            )
    logger.info(
        "dhw_policy: wrote %d rows for %s (mode=%s, neg_windows=%d)",
        n_written, target_date_local, mode,
        sum(1 for r in rows if r["action_type"] == "tank_negative_boost"),
    )
    return n_written
