"""Target VWAP engine, Fox Scheduler V3 builder, Daikin action_schedule writer."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..foxess.client import FoxESSClient
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

# Rolling-window floor. Fewer than this many half-hour slots of Agile data ahead
# of *now* means the LP has nothing meaningful to optimize — Self-Use fallback.
_MIN_USABLE_SLOTS = 4  # 2 hours


@dataclass
class PlanWindow:
    """Describes the rolling time window the optimizer will plan for.

    All datetimes are UTC. The window is computed as
    ``[day_start, horizon_end)`` where ``day_start = now`` rounded up to the
    next half-hour boundary and ``horizon_end = min(day_start + LP_HORIZON_HOURS,
    last known Agile slot end)``. The field name ``day_start`` is preserved for
    historical reasons — it is the *window* start, not a local-midnight anchor.
    """

    plan_date: str           # ISO date of local(day_start) — tag for logs/consent, not a clear key
    day_start: datetime      # UTC — start of rolling window (next HH:30 boundary)
    horizon_end: datetime    # UTC — end of rolling window, truncated to data availability
    rates: list              # raw rate rows covering the window

    @property
    def horizon_hours(self) -> float:
        return (self.horizon_end - self.day_start).total_seconds() / 3600.0


def _now_utc() -> datetime:
    """Wall clock for the rolling-window resolver (monkeypatch target in tests)."""
    return datetime.now(UTC)


def _ceil_to_half_hour_utc(dt: datetime) -> datetime:
    """Round *dt* up to the next :00 or :30 boundary in UTC."""
    dt = dt.astimezone(UTC).replace(second=0, microsecond=0)
    if dt.minute == 0 or dt.minute == 30:
        # Already on a half-hour boundary — advance to the NEXT one so the
        # currently-live slot isn't re-dispatched (Daikin quota integrity).
        return dt + timedelta(minutes=30)
    if dt.minute < 30:
        return dt.replace(minute=30)
    return (dt + timedelta(hours=1)).replace(minute=0)


def _resolve_plan_window(tariff: str) -> PlanWindow | None:
    """Compute a rolling ``now → now + LP_HORIZON_HOURS`` window.

    The window always starts at the *next* half-hour boundary after ``now`` so
    the currently-running slot is never re-dispatched (quota integrity, see
    ADR-002). The end is capped by the last known Agile ``valid_to`` so a 09:00
    call before Octopus publishes tomorrow cleanly produces a shorter window
    ending at today's last slot.

    Returns ``None`` when fewer than ``_MIN_USABLE_SLOTS`` of Agile data remain
    — caller should fall back to Self-Use.
    """
    tz = TZ()
    now_utc = _now_utc()
    start_utc = _ceil_to_half_hour_utc(now_utc)
    target_end_utc = start_utc + timedelta(hours=int(config.LP_HORIZON_HOURS))

    # Query with small overlap so overlapping rate rows at the boundary are included.
    q_from = start_utc - timedelta(minutes=30)
    q_to = target_end_utc + timedelta(hours=1)
    rates = db.get_rates_for_period(tariff, q_from, q_to) or []

    last_valid_to: datetime | None = None
    for r in rates:
        try:
            vt = _parse_ts(str(r["valid_to"]))
        except (ValueError, KeyError, TypeError):
            continue
        if last_valid_to is None or vt > last_valid_to:
            last_valid_to = vt

    if last_valid_to is None or last_valid_to <= start_utc:
        logger.warning(
            "Plan window: no future Agile rates available "
            "(start=%s, last_valid_to=%s) — Self-Use fallback",
            start_utc.strftime("%Y-%m-%dT%H:%MZ"),
            last_valid_to.strftime("%Y-%m-%dT%H:%MZ") if last_valid_to else "None",
        )
        return None

    # Min-usable check operates on REAL rates only — we don't commit to a
    # 48 h plan when the real data is too thin (caller falls back to Self-Use
    # and waits for more rates to arrive before the next MPC re-plan).
    real_horizon_end = min(target_end_utc, last_valid_to)
    real_slots_preview = _build_half_hour_slots(rates, start_utc, real_horizon_end)
    if len(real_slots_preview) < _MIN_USABLE_SLOTS:
        logger.warning(
            "Plan window: only %d real slots usable (< %d minimum) — Self-Use fallback",
            len(real_slots_preview),
            _MIN_USABLE_SLOTS,
        )
        return None

    # S10.2 (#169) horizon extender: when LP_HORIZON_HOURS exceeds the available
    # Octopus rates (typical case before ~16:00 BST when D+1 hasn't been
    # published), fill the tail with synthesized rows from historical median
    # per-hour-of-day. This lets a 48 h LP solve see a reasonable D+1 price
    # curve with vale at midday and peak at 18 h. Once Octopus publishes real
    # D+1 prices the next MPC re-solve picks them up cleanly.
    horizon_end_utc = target_end_utc
    if last_valid_to < target_end_utc:
        priors = db.get_half_hourly_agile_priors(tariff, window_days=28)
        if priors:
            fallback_p = sum(priors.values()) / len(priors)
            synth: list[dict[str, Any]] = []
            t = max(last_valid_to, start_utc)
            while t < target_end_utc:
                t_end = t + timedelta(minutes=30)
                # Half-hour granularity: prior bucket is (hour, minute) per S10.8 (#175)
                p = priors.get((t.hour, t.minute), fallback_p)
                synth.append({
                    "valid_from": t.isoformat().replace("+00:00", "Z"),
                    "valid_to": t_end.isoformat().replace("+00:00", "Z"),
                    "value_inc_vat": p,
                    "tariff_code": tariff,
                    "fetched_at": "prior",  # sentinel: distinguishes synthesised rows
                })
                t = t_end
            rates = list(rates) + synth
            logger.info(
                "Plan window: extended horizon with %d prior slots (last_actual=%s, "
                "target_end=%s) — D+1 priors median over 28 d, mean=%.2fp",
                len(synth),
                last_valid_to.strftime("%Y-%m-%dT%H:%MZ"),
                target_end_utc.strftime("%Y-%m-%dT%H:%MZ"),
                fallback_p,
            )
        else:
            # No history → can't synthesise; truncate as before.
            horizon_end_utc = min(target_end_utc, last_valid_to)
            logger.info(
                "Plan window: no priors available; truncating horizon to %s",
                horizon_end_utc.strftime("%Y-%m-%dT%H:%MZ"),
            )
    else:
        horizon_end_utc = min(target_end_utc, last_valid_to)

    # Final slot list (possibly extended with priors). Min-usable was already
    # enforced on real-rates only above (extension can never reduce slot count).
    slots_preview = _build_half_hour_slots(rates, start_utc, horizon_end_utc)

    start_local = start_utc.astimezone(tz)
    end_local = horizon_end_utc.astimezone(tz)
    horizon_h = (horizon_end_utc - start_utc).total_seconds() / 3600.0
    logger.info(
        "TZ-AUDIT: rolling plan window | %.1fh | UTC %s → %s | local %s → %s | %d slots",
        horizon_h,
        start_utc.strftime("%Y-%m-%dT%H:%MZ"),
        horizon_end_utc.strftime("%Y-%m-%dT%H:%MZ"),
        start_local.strftime("%a %d %b %H:%M %Z"),
        end_local.strftime("%a %d %b %H:%M %Z"),
        len(slots_preview),
    )

    return PlanWindow(
        plan_date=start_local.date().isoformat(),
        day_start=start_utc,
        horizon_end=horizon_end_utc,
        rates=rates,
    )


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
) -> tuple[str, int | None, int | None, int, int | None]:
    """``(work_mode, fd_soc, fd_pwr, min_soc_on_grid, max_soc)`` for Scheduler V3.

    For ForceCharge slots the ``fdPwr`` (W) is taken from ``s.lp_grid_import_w`` when set
    (LP path), or falls back to the configured ``FOX_FORCE_CHARGE_*_PWR`` constants.
    For solar_charge slots (LP import≈0, battery charges from PV) we use SelfUse with an
    elevated minSocOnGrid so the battery is held at the LP's target level without forcing
    any grid import — PV handles the charging naturally. We ALSO set ``maxSoc=100`` to
    explicitly give the firmware the "Solar Sponge" cue: PV-only fill up to 100 % cap.
    """
    min_r = int(config.MIN_SOC_RESERVE_PERCENT)
    if s.kind == "solar_charge":
        # 100%: hold battery fully so PV fills it without the inverter pulling any grid.
        # SelfUse mode forbids active grid import regardless of minSocOnGrid — this only
        # blocks discharge, letting excess PV accumulate. MPC at 06:00/12:00 corrects
        # for cloud shortfalls by switching to ForceCharge if SoC lags the target.
        # maxSoc=100 is the canonical Fox V3 "Solar Sponge" cue (per TonyM1958/FoxESS-Cloud
        # wiki) — explicit cap so the firmware never tries to top via grid past 100 %.
        min_soc = int(getattr(config, "FOX_SOLAR_CHARGE_MIN_SOC_PERCENT", 100))
        return ("SelfUse", None, None, min_soc, 100)
    if s.kind == "negative":
        pwr = s.lp_grid_import_w if s.lp_grid_import_w is not None else config.FOX_FORCE_CHARGE_MAX_PWR
        fds = s.target_soc_pct if s.target_soc_pct is not None else 100
        return ("ForceCharge", fds, pwr, min_r, None)
    if s.kind == "cheap":
        pwr = s.lp_grid_import_w if s.lp_grid_import_w is not None else config.FOX_FORCE_CHARGE_NORMAL_PWR
        fds = s.target_soc_pct if s.target_soc_pct is not None else 95
        return ("ForceCharge", fds, pwr, min_r, None)
    if s.kind == "peak_export":
        return (
            "ForceDischarge",
            int(config.EXPORT_DISCHARGE_FLOOR_SOC_PERCENT),
            config.FOX_EXPORT_MAX_PWR,
            min_r,
            None,
        )
    if s.kind == "peak" and peak_export_discharge:
        return (
            "ForceDischarge",
            int(config.EXPORT_DISCHARGE_FLOOR_SOC_PERCENT),
            config.FOX_EXPORT_MAX_PWR,
            min_r,
            None,
        )
    if s.kind == "negative_hold":
        # Fox "Backup" work mode = native "hold battery": no discharge, no grid
        # charge. Directly enforces the LP's dis = 0 constraint during negative
        # slots when chg is also zero (battery saturated or PV alone suffices).
        return ("Backup", None, None, min_r, None)
    return ("SelfUse", None, None, min_r, None)


def _optimization_preset_away_like() -> bool:
    """True when household preset is travel or away (hibernate / export-friendly)."""
    try:
        p = OperationPreset((config.OPTIMIZATION_PRESET or "normal").strip().lower())
        return p in (OperationPreset.TRAVEL, OperationPreset.AWAY)
    except ValueError:
        return False


def _count_midnight_crossings(
    merged: list[tuple[datetime, datetime, tuple]],
    tz: ZoneInfo,
) -> int:
    """Count merged windows whose local ``[ls, le)`` strictly crosses a local
    midnight boundary — each will become two Fox V3 groups after the split.
    """
    n = 0
    for start_utc, end_utc, _ in merged:
        ls = start_utc.astimezone(tz)
        le = end_utc.astimezone(tz)
        next_midnight = datetime.combine(
            ls.date() + timedelta(days=1), datetime.min.time()
        ).replace(tzinfo=ls.tzinfo)
        if le > next_midnight:
            n += 1
    return n


def _split_at_local_midnight(
    merged: list[tuple[datetime, datetime, tuple]],
    tz: ZoneInfo,
) -> list[tuple[datetime, datetime, tuple]]:
    """Split any merged window that crosses local midnight into two
    same-key halves. Fox V3 groups are ``HH:MM`` within a single 24 h
    cycle (no date), so a range like 22:00 → 02:00 is undefined; emitting
    two groups 22:00 → 23:59 and 00:00 → 02:00 preserves the intent.
    """
    out: list[tuple[datetime, datetime, tuple]] = []
    for start_utc, end_utc, key in merged:
        ls = start_utc.astimezone(tz)
        le = end_utc.astimezone(tz)
        next_midnight = datetime.combine(
            ls.date() + timedelta(days=1), datetime.min.time()
        ).replace(tzinfo=ls.tzinfo)
        if le > next_midnight:
            mid_utc = next_midnight.astimezone(UTC)
            out.append((start_utc, mid_utc, key))
            out.append((mid_utc, end_utc, key))
        else:
            out.append((start_utc, end_utc, key))
    return out


def _merge_fox_groups(
    slots: list[HalfHourSlot],
    max_groups: int = 8,
    *,
    peak_export_discharge: bool = False,
    truncate_horizon: bool = False,
) -> list[SchedulerGroup] | tuple[list[SchedulerGroup], datetime | None]:
    """Build Fox V3 SchedulerGroup list from per-slot LP output, capped at ``max_groups``.

    When ``truncate_horizon`` is False (default, back-compat): returns just the
    list. Excess windows are compressed via the back-bias + peak-guard fallback.

    When ``truncate_horizon`` is True: returns ``(groups, replan_at_utc)``. The
    caller decides whether to dispatch the truncated set or fall back to compression.
    ``replan_at_utc`` is the UTC end-time of the last surviving window (the caller
    should schedule a one-shot MPC re-plan slightly before this); ``None`` if no
    truncation was needed.
    """
    if not slots:
        return ([], None) if truncate_horizon else []
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

    # Camada 1: eager SelfUse-variant merge — collapses adjacent SelfUse blocks
    # whose only difference is minSocOnGrid (e.g. solar_charge=100 next to
    # standard=10). Promotes from overflow-only to always-on so the window count
    # is minimised before any compression decisions.
    merged = _coarse_merge_fox(merged)

    # Camada 0: drop trivial SelfUse windows (work_mode=SelfUse AND
    # min_soc_on_grid == MIN_SOC_RESERVE_PERCENT). The Fox V3 firmware falls
    # back to the inverter's "Remaining Time Work Mode" (Self-use) outside
    # any scheduler group, with the global minSocOnGrid floor — so an explicit
    # group with the same parameters is wasted budget against the 8-group cap.
    # Solar-charge holds (SelfUse minSoc=100) and any other elevated floor are
    # preserved because they DO deviate from the global default.
    if config.FOX_SKIP_TRIVIAL_SELFUSE_GROUPS:
        reserve = int(config.MIN_SOC_RESERVE_PERCENT)
        filtered = [
            w for w in merged
            if not (w[2][0] == "SelfUse" and w[2][3] == reserve)
        ]
        # Defensive: a fully-trivial plan (no FC/FD/Backup/elevated SelfUse) would
        # produce an empty payload, which the Fox firmware may reject. Keep the
        # first window so the upload stays well-formed; the loss is bounded at
        # one wasted slot in that very rare degenerate case.
        merged = filtered if filtered else merged[:1]

    replan_at: datetime | None = None
    if truncate_horizon and (
        len(merged) + _count_midnight_crossings(merged, tz) > max_groups
    ):
        # Camada 2: dispatch only the first windows that fit (after reserving
        # slots for midnight splits) and report the replan boundary. The caller
        # schedules a one-shot MPC fire shortly before the last surviving window
        # ends so the truncated tail is re-planned with full precision.
        kept: list[tuple[datetime, datetime, tuple]] = []
        crossings = 0
        for window in merged:
            tentative = kept + [window]
            tentative_crossings = _count_midnight_crossings(tentative, tz)
            if len(tentative) + tentative_crossings > max_groups:
                break
            kept = tentative
            crossings = tentative_crossings
        if kept:
            merged = kept
            replan_at = merged[-1][1]

    # Camada 3: compression fallback (back-bias + peak guard). Only kicks in if
    # truncation did not run (back-compat callers) or could not bring the count
    # under cap. Squashes pairs from the *tail* in, never destroying the
    # immediate future, and protects ForceDischarge (peak_export) windows.
    guard = 0
    while (
        len(merged) + _count_midnight_crossings(merged, tz) > max_groups
        and len(merged) >= 2
        and guard < 64
    ):
        guard += 1
        merged = _coarse_merge_fox(merged)
        if len(merged) + _count_midnight_crossings(merged, tz) <= max_groups:
            break
        # Same-key adjacent merge, scanning back-to-front so late-day pairs go first.
        merged_pair = False
        for j in range(len(merged) - 2, -1, -1):
            if merged[j][2] == merged[j + 1][2]:
                a, _, k = merged[j]
                _, d, _ = merged[j + 1]
                merged[j] = (a, d, k)
                del merged[j + 1]
                merged_pair = True
                break
        if merged_pair:
            continue
        # Brutal squash: pick the latest pair where neither side is ForceDischarge.
        # ForceDischarge = peak_export revenue; never sacrifice it to fit the cap.
        # If every pair contains a ForceDischarge (degenerate), fall through to tail.
        victim = None
        for j in range(len(merged) - 2, -1, -1):
            if (
                merged[j][2][0] != "ForceDischarge"
                and merged[j + 1][2][0] != "ForceDischarge"
            ):
                victim = j
                break
        if victim is None:
            victim = len(merged) - 2
        a, _, ka = merged[victim]
        _, d, kb = merged[victim + 1]
        ka_max = ka[4] if len(ka) > 4 else None
        kb_max = kb[4] if len(kb) > 4 else None
        if ka[0] == "ForceCharge" and kb[0] == "ForceCharge":
            nk = (
                "ForceCharge",
                max(ka[1] or 0, kb[1] or 0),
                max(ka[2] or 0, kb[2] or 0),
                max(ka[3], kb[3]),
                _max_optional(ka_max, kb_max),
            )
        else:
            nk = ("SelfUse", None, None, int(config.MIN_SOC_RESERVE_PERCENT), None)
        merged[victim] = (a, d, nk)
        del merged[victim + 1]

    merged = _split_at_local_midnight(merged, tz)

    groups: list[SchedulerGroup] = []
    for start_utc, end_utc, key in merged:
        # Tuple is now (wm, fds, fdp, msg, max_soc); accept legacy 4-tuples defensively.
        wm, fds, fdp, msg = key[0], key[1], key[2], key[3]
        max_soc = key[4] if len(key) > 4 else None
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
                max_soc=max_soc,
            )
        )
    if truncate_horizon:
        return groups, replan_at
    return groups


def _merge_adjacent_force_charge_rows(
    merged: list[tuple[datetime, datetime, tuple]],
) -> list[tuple[datetime, datetime, tuple]]:
    """Join consecutive ForceCharge segments even when fdSoc/fdPwr differ (e.g. negative vs cheap slot).

    Tuple convention: ``(work_mode, fd_soc, fd_pwr, min_soc_on_grid, max_soc)``.
    """
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
                _max_optional(k0[4] if len(k0) > 4 else None, k[4] if len(k) > 4 else None),
            )
            out[-1] = (a0, b, nk)
        else:
            out.append((a, b, k))
    return out


def _max_optional(a: int | None, b: int | None) -> int | None:
    """Return max of two optional ints; None if both None."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _coarse_merge_fox(
    merged: list[tuple[datetime, datetime, tuple]],
) -> list[tuple[datetime, datetime, tuple]]:
    """Collapse SelfUse variants; preserve highest minSocOnGrid + maxSoc when merging solar_charge windows."""
    out: list[tuple[datetime, datetime, tuple]] = []
    for a, b, k in merged:
        # Normalise SelfUse-shape entries; preserve the original max_soc (5th element).
        if k[0] == "SelfUse":
            nk = ("SelfUse", None, None, k[3], k[4] if len(k) > 4 else None)
        else:
            nk = k
        if out and out[-1][2][0] == "SelfUse" and nk[0] == "SelfUse" and out[-1][1] == a:
            prev = out[-1][2]
            merged_msg = max(prev[3], nk[3])
            merged_max = _max_optional(prev[4] if len(prev) > 4 else None, nk[4])
            out[-1] = (out[-1][0], b, ("SelfUse", None, None, merged_msg, merged_max))
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
        # Heuristic fallback mirrors the LP policy from issue #50: tank only goes above
        # DHW_TEMP_COMFORT_C when price < 0 (the "negative" kind). For "cheap" (positive
        # price) we stay at the comfort ceiling.
        return {
            "lwt_offset": min(config.LWT_OFFSET_PREHEAT_BOOST, config.LWT_OFFSET_MAX),
            "tank_powerful": False,
            "tank_temp": config.DHW_TEMP_COMFORT_C,
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
    if slots:
        window_start_iso = slots[0].start_utc.isoformat().replace("+00:00", "Z")
        window_end_iso = slots[-1].end_utc.isoformat().replace("+00:00", "Z")
        db.clear_actions_in_range(window_start_iso, window_end_iso, device="daikin")
    else:
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
    from .lp_dispatch import upload_fox_if_operational

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

    # Fox V3 dispatch is daily-cyclic — mirror the LP path's 24 h cap so D+1
    # slots that share an hour-of-day with D+0 don't reach the inverter as
    # overlapping groups (issue #208 / db8a59c). Without this the heuristic
    # uploads all 96 slots from the 48 h plan window and Fox renders duplicates.
    fox_slots = slots
    if fox_slots:
        cutoff = fox_slots[0].start_utc + timedelta(hours=24)
        fox_slots = [s for s in fox_slots if s.start_utc < cutoff]
    fox_ok = False
    groups = _merge_fox_groups(fox_slots, max_groups=8, peak_export_discharge=False)
    fox_ok = upload_fox_if_operational(fox, groups)

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
        "strategy": strategy,
        "dhw_thermal_decay": thermal_info,
        "optimizer_backend": "heuristic",
    }


def _persist_lp_snapshots(
    *,
    run_id: int,
    run_at_iso: str,
    plan_date: str,
    plan: Any,  # LpPlan — typed-as-Any to avoid an import cycle at module load
    initial: Any,  # LpInitialState
    base_load: list[float],
    micro_climate_offset: float,
) -> None:
    """Build and persist the inputs + per-slot rows for this LP run.

    The shape of ``lp_inputs_snapshot`` + ``lp_solution_snapshot`` is the
    cockpit History replay source of truth. Config fields captured here are
    the LP-relevant tunables; other knobs can be recovered from
    ``config_audit`` joined by timestamp if needed.
    """
    # Config snapshot — everything the LP meaningfully conditioned on. Keep
    # compact; dashboards read this directly from the JSON.
    cfg_snap = {
        "LP_HORIZON_HOURS": int(config.LP_HORIZON_HOURS),
        "BATTERY_CAPACITY_KWH": float(config.BATTERY_CAPACITY_KWH),
        "MIN_SOC_RESERVE_PERCENT": float(config.MIN_SOC_RESERVE_PERCENT),
        "BATTERY_RT_EFFICIENCY": float(config.BATTERY_RT_EFFICIENCY),
        "MAX_INVERTER_KW": float(config.MAX_INVERTER_KW),
        "FOX_FORCE_CHARGE_MAX_PWR": int(config.FOX_FORCE_CHARGE_MAX_PWR),
        "FOX_EXPORT_MAX_PWR": int(config.FOX_EXPORT_MAX_PWR),
        "DAIKIN_MAX_HP_KW": float(config.DAIKIN_MAX_HP_KW),
        "DHW_TANK_LITRES": float(config.DHW_TANK_LITRES),
        "DHW_TANK_UA_W_PER_K": float(config.DHW_TANK_UA_W_PER_K),
        "BUILDING_UA_W_PER_K": float(config.BUILDING_UA_W_PER_K),
        "BUILDING_THERMAL_MASS_KWH_PER_K": float(config.BUILDING_THERMAL_MASS_KWH_PER_K),
        "DHW_TEMP_COMFORT_C": float(config.DHW_TEMP_COMFORT_C),
        "DHW_TEMP_MAX_C": float(config.DHW_TEMP_MAX_C),
        "LP_CYCLE_PENALTY_PENCE_PER_KWH": float(config.LP_CYCLE_PENALTY_PENCE_PER_KWH),
        "LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT": float(config.LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT),
        "LP_INVERTER_STRESS_COST_PENCE": float(config.LP_INVERTER_STRESS_COST_PENCE),
        "LP_PRICE_QUANTIZE_PENCE": float(getattr(config, "LP_PRICE_QUANTIZE_PENCE", 0.0)),
        "LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA": float(
            getattr(config, "LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA", 0.0)
        ),
        "LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA": float(
            getattr(config, "LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA", 0.0)
        ),
    }

    inputs_row = {
        "run_at_utc": run_at_iso,
        "plan_date": plan_date,
        "horizon_hours": int(config.LP_HORIZON_HOURS),
        "soc_initial_kwh": float(initial.soc_kwh),
        "tank_initial_c": float(initial.tank_temp_c),
        "indoor_initial_c": float(initial.indoor_temp_c),
        "soc_source": getattr(initial, "soc_source", "unknown"),
        "tank_source": getattr(initial, "tank_source", "unknown"),
        "indoor_source": getattr(initial, "indoor_source", "unknown"),
        "base_load_json": json.dumps([round(float(x), 4) for x in base_load]),
        "micro_climate_offset_c": float(micro_climate_offset or 0.0),
        "forecast_fetch_at_utc": None,
        "config_snapshot_json": json.dumps(cfg_snap),
        "price_quantize_p": float(getattr(config, "LP_PRICE_QUANTIZE_PENCE", 0.0)),
        "peak_threshold_p": float(plan.peak_threshold_pence),
        "cheap_threshold_p": float(plan.cheap_threshold_pence),
        "daikin_control_mode": str(config.DAIKIN_CONTROL_MODE),
        "optimization_preset": str(config.OPTIMIZATION_PRESET),
        "energy_strategy_mode": str(config.ENERGY_STRATEGY_MODE),
    }

    # tank_temp_c, indoor_temp_c, soc_kwh are length N+1 (include initial);
    # we persist the end-of-slot state (index i+1) to match how slot results
    # are naturally interpreted. lwt_offset_c / temp_outdoor_c are length N.
    n = len(plan.slot_starts_utc)
    solution_rows: list[dict[str, Any]] = []
    for i in range(n):
        solution_rows.append({
            "slot_index": i,
            "slot_time_utc": plan.slot_starts_utc[i].isoformat(),
            "price_p": float(plan.price_pence[i]) if i < len(plan.price_pence) else None,
            "import_kwh": float(plan.import_kwh[i]) if i < len(plan.import_kwh) else None,
            "export_kwh": float(plan.export_kwh[i]) if i < len(plan.export_kwh) else None,
            "charge_kwh": float(plan.battery_charge_kwh[i]) if i < len(plan.battery_charge_kwh) else None,
            "discharge_kwh": float(plan.battery_discharge_kwh[i]) if i < len(plan.battery_discharge_kwh) else None,
            "pv_use_kwh": float(plan.pv_use_kwh[i]) if i < len(plan.pv_use_kwh) else None,
            "pv_curtail_kwh": float(plan.pv_curtail_kwh[i]) if i < len(plan.pv_curtail_kwh) else None,
            "dhw_kwh": float(plan.dhw_electric_kwh[i]) if i < len(plan.dhw_electric_kwh) else None,
            "space_kwh": float(plan.space_electric_kwh[i]) if i < len(plan.space_electric_kwh) else None,
            "soc_kwh": float(plan.soc_kwh[i + 1]) if (i + 1) < len(plan.soc_kwh) else None,
            "tank_temp_c": float(plan.tank_temp_c[i + 1]) if (i + 1) < len(plan.tank_temp_c) else None,
            "indoor_temp_c": float(plan.indoor_temp_c[i + 1]) if (i + 1) < len(plan.indoor_temp_c) else None,
            "outdoor_temp_c": float(plan.temp_outdoor_c[i]) if i < len(plan.temp_outdoor_c) else None,
            "lwt_offset_c": float(plan.lwt_offset_c[i]) if i < len(plan.lwt_offset_c) else None,
        })

    db.save_lp_snapshots(run_id=int(run_id), inputs_row=inputs_row, solution_rows=solution_rows)


def _build_export_price_line(slot_starts_utc: list[datetime]) -> list[float] | None:
    """Map per-slot start timestamps to the matching ``agile_export_rates`` value.

    Returns ``None`` when no Outgoing tariff is configured or the table has no
    rows in the planning window — caller falls back to the flat constant.
    Missing per-slot rows are filled with the flat constant so the returned
    list always matches the slot count when non-None.
    """
    if not slot_starts_utc:
        return None
    if not (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip():
        return None
    period_from = slot_starts_utc[0].isoformat()
    period_to = (slot_starts_utc[-1] + timedelta(minutes=30)).isoformat()
    rows = db.get_agile_export_rates_in_range(period_from, period_to)
    if not rows:
        return None
    by_start: dict[str, float] = {}
    for r in rows:
        try:
            iso_norm = r["valid_from"].replace("+00:00", "Z")
            by_start[iso_norm] = float(r["value_inc_vat"])
        except (KeyError, TypeError, ValueError):
            continue
    flat = float(config.EXPORT_RATE_PENCE)
    # S10.2 (#169): for missing slots in an extended (48 h) horizon, prefer
    # historical median per-hour-of-day priors over the flat fallback. This
    # gives the LP a realistic export-price curve for D+1 (e.g. midday vale
    # is recognisable so LP doesn't over-export at noon when refill will be
    # at the same price).
    export_tariff = (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip()
    priors = (
        db.get_half_hourly_agile_priors(export_tariff, window_days=28, table="agile_export_rates")
        if export_tariff
        else {}
    )
    out: list[float] = []
    n_matched = 0
    n_prior = 0
    for st in slot_starts_utc:
        key = st.isoformat().replace("+00:00", "Z")
        v = by_start.get(key)
        if v is not None:
            out.append(v)
            n_matched += 1
        elif (st.hour, st.minute) in priors:
            out.append(priors[(st.hour, st.minute)])
            n_prior += 1
        else:
            out.append(flat)
    if n_matched == 0 and n_prior == 0:
        return None  # nothing matched — let caller use flat path entirely
    logger.info(
        "Export prices: matched %d/%d slots from agile_export_rates "
        "(+%d from prior, rest %d use flat %.2fp)",
        n_matched, len(slot_starts_utc), n_prior,
        len(slot_starts_utc) - n_matched - n_prior, flat,
    )
    return out


def _run_optimizer_lp(
    fox: FoxESSClient | None,
    daikin: Any | None = None,
    *,
    trigger_reason: str = "manual",
) -> dict[str, Any]:
    """PuLP MILP horizon planner (V8). Falls back to heuristic if solve is not optimal.

    ``trigger_reason`` is plumbed in so the dispatcher knows whether to run
    scenario LP for peak-export robustness (controlled by
    ``LP_SCENARIOS_ON_TRIGGER_REASONS``). Defaults to ``manual`` for callers
    that haven't been updated to pass a reason yet.
    """
    from . import appliance_dispatch
    from .lp_dispatch import (
        build_fox_groups_from_lp,
        filter_robust_peak_export,
        lp_plan_to_slots,
        upload_fox_if_operational,
        write_daikin_from_lp_plan,
    )
    from .lp_initial_state import read_lp_initial_state
    from .lp_optimizer import solve_lp
    from .scenarios import solve_scenarios_with_nominal, trigger_runs_scenarios

    # Smart appliance scheduling: arm/cancel/replan SmartThings sessions BEFORE
    # the LP solve so the residual-load profile reflects current remote-mode
    # state. Never fatal — failures here log + continue, but surface to the
    # operator via notify_risk so silent regressions don't accumulate.
    # (Per-appliance SmartThings/DB errors are caught inside reconcile() and
    # don't reach here; anything that DOES propagate is unexpected.)
    try:
        appliance_dispatch.reconcile()
    except Exception as _exc:  # noqa: BLE001 — defensive outer
        logger.exception("appliance_dispatch.reconcile failed (LP solve continuing)")
        try:
            from .. import notifier  # local import — avoid cycle

            notifier.notify_risk(
                f"appliance_dispatch.reconcile() raised unexpectedly: {type(_exc).__name__}: {_exc}"
            )
        except Exception:  # noqa: BLE001 — notifier failures must not abort the LP
            logger.warning("notify_risk failed during reconcile error reporting")

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

    # Per-slot residual load (Daikin physics subtracted per S10.13 / #179)
    _profile_limit = int(getattr(config, "LP_LOAD_PROFILE_SLOTS", 2016))
    _load_profile = db.half_hourly_residual_load_profile_kwh()
    # Fall back to Fox daily mean when execution_log is cold
    _fox_mean = db.mean_fox_load_kwh_per_slot(limit=60)
    _flat = _fox_mean if _fox_mean is not None else db.mean_consumption_kwh_from_execution_logs(limit=_profile_limit)
    if _load_profile:
        logger.info(
            "LP base_load: 48 buckets built; min=%.3f max=%.3f early-morning(03-07)=%s",
            min(_load_profile.values()),
            max(_load_profile.values()),
            {f"{h:02d}:{m:02d}": round(_load_profile[(h, m)], 3)
             for h in range(3, 8) for m in (0, 30) if (h, m) in _load_profile},
        )
    base_load = []
    for s in slots:
        _local = s.start_utc.astimezone(tz)
        _bucket = (_local.hour, 30 if _local.minute >= 30 else 0)
        base_load.append(_load_profile.get(_bucket, _flat))

    # Smart appliance scheduling: bump residual load on slots covered by armed
    # sessions so peak_export / charge / DHW plans route around the wash.
    # Zero contribution when no jobs are scheduled — LP regression baseline
    # stays bit-identical.
    try:
        if slots:
            _horizon_start = slots[0].start_utc
            _horizon_end = slots[-1].start_utc + timedelta(minutes=30)
            _appliance_kw = appliance_dispatch.appliance_load_profile_kw(
                _horizon_start, _horizon_end
            )
            if _appliance_kw:
                _added_slots = 0
                for _i, _s in enumerate(slots):
                    _kw = _appliance_kw.get(_s.start_utc, 0.0)
                    if _kw > 0:
                        # Half-hour slot → kWh = kW × 0.5 h.
                        base_load[_i] += _kw * 0.5
                        _added_slots += 1
                if _added_slots:
                    logger.info(
                        "LP base_load: appliance contribution applied to %d slot(s) "
                        "(total +%.3f kWh)",
                        _added_slots,
                        sum(_kw * 0.5 for _kw in _appliance_kw.values()),
                    )
    except Exception as _exc:  # noqa: BLE001 — defensive outer
        logger.exception(
            "LP base_load: appliance load contribution failed (continuing without it)"
        )
        try:
            from .. import notifier  # local import — avoid cycle

            notifier.notify_risk(
                f"appliance_load_profile_kw raised: {type(_exc).__name__}: {_exc} "
                "— LP plan does NOT account for armed appliance loads"
            )
        except Exception:  # noqa: BLE001
            logger.warning("notify_risk failed during base_load error reporting")

    mu_load = sum(base_load) / len(base_load) if base_load else 0.4
    prices = [s.price_pence for s in slots]
    starts = [s.start_utc for s in slots]

    forecast = fetch_forecast(hours=max(48, int(config.LP_HORIZON_HOURS) + 24))
    forecast_fetch_at_utc = datetime.now(UTC).isoformat()
    # Persist the canonical forecast snapshot once so heartbeat + replay can
    # both reference the same fetch instead of duplicating latest/history rows.
    if forecast:
        forecast_rows = [
            {
                "slot_time": f.time_utc.isoformat(),
                "temp_c": f.temperature_c,
                "solar_w_m2": f.shortwave_radiation_wm2,
                "cloud_cover_pct": f.cloud_cover_pct,
            }
            for f in forecast
        ]
        db.save_meteo_forecast_snapshot(
            forecast_fetch_at_utc,
            forecast_rows,
            mark_latest=True,
        )
        inputs_row["forecast_fetch_at_utc"] = forecast_fetch_at_utc
    # PV calibration — fallback chain (best to worst):
    #   1. cloud-aware (hour × cloud-bucket) table  — PR #232
    #   2. per-hour-of-day table                    — PR #186
    #   3. flat rolling factor                      — original
    # Plus the today-aware OCF-style multiplier on top (PR #229).
    from ..weather import (
        compute_pv_calibration_factor,
        compute_today_pv_correction_factor,
        get_pv_calibration_factor_for,
    )
    pv_scale_cloud = db.get_pv_calibration_hourly_cloud()
    pv_scale_hourly = db.get_pv_calibration_hourly()
    flat_scale = (
        compute_pv_calibration_factor()
        if not pv_scale_cloud and not pv_scale_hourly else 1.0
    )
    if pv_scale_cloud:
        logger.info(
            "PV calibration: cloud-aware table (%d cells, mean factor=%.3f) + per-hour fallback (%d hours)",
            len(pv_scale_cloud),
            sum(pv_scale_cloud.values()) / len(pv_scale_cloud),
            len(pv_scale_hourly),
        )
    elif pv_scale_hourly:
        logger.info(
            "PV calibration: per-hour table only (%d hours, mean factor=%.3f) — cloud-aware empty",
            len(pv_scale_hourly),
            sum(pv_scale_hourly.values()) / len(pv_scale_hourly),
        )
    else:
        logger.info("PV calibration: flat factor=%.3f (no per-hour or cloud-aware table)", flat_scale)

    today_factor, today_diag = compute_today_pv_correction_factor()
    if today_factor != 1.0:
        logger.info(
            "PV calibration: today-aware adjuster factor=%.3f (n_hours=%d, "
            "median_ratio=%.3f, clamped=%s)",
            today_factor, today_diag.get("n_hours", 0),
            today_diag.get("median_ratio", 0.0), today_diag.get("clamped", False),
        )
    else:
        logger.info(
            "PV calibration: today-aware adjuster skipped (%s)",
            today_diag.get("reason", "no diagnostic"),
        )

    def _pv_scale_callable(hour_utc: int, cloud_pct: float) -> float:
        return get_pv_calibration_factor_for(
            hour_utc, cloud_pct,
            cloud_table=pv_scale_cloud,
            hourly_table=pv_scale_hourly,
            flat=flat_scale,
        ) * today_factor

    weather = forecast_to_lp_inputs(forecast, starts, pv_scale=_pv_scale_callable)
    initial = read_lp_initial_state(daikin)
    micro_climate_offset = db.get_micro_climate_offset_c(config.DAIKIN_MICRO_CLIMATE_LOOKBACK)

    # Per-slot Octopus Outgoing Agile (export) prices. When the table is empty (no
    # tariff configured / not yet fetched), the LP falls back to the flat
    # EXPORT_RATE_PENCE constant, preserving legacy behaviour.
    export_prices = _build_export_price_line(starts)

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=initial,
        tz=tz,
        micro_climate_offset_c=micro_climate_offset,
        export_price_pence=export_prices,
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

    # Scenario LP — run a 3-pass robustness check on peak-export commits when
    # the trigger reason warrants it (cron, plan_push by default). Skipped on
    # fast-path triggers (drift, forecast_revision, dynamic_replan, manual)
    # to keep MPC re-plan latency low; those committed plans inherit "trust
    # the LP" semantics. Decisions get persisted alongside the run_id below.
    has_peak_export = any(s.kind == "peak_export" for s in lp_slots)
    scenarios_dict: dict[str, Any] | None = None
    if has_peak_export and trigger_runs_scenarios(trigger_reason):
        try:
            scenarios_dict = dict(
                solve_scenarios_with_nominal(
                    nominal=plan,
                    slot_starts_utc=starts,
                    price_pence=prices,
                    base_load_kwh=base_load,
                    weather=weather,
                    initial=initial,
                    tz=tz,
                    micro_climate_offset_c=micro_climate_offset,
                    export_price_pence=export_prices,
                )
            )
        except Exception as e:
            logger.warning(
                "Scenario LP failed (trigger=%s): %s — committing nominal plan as-is",
                trigger_reason, e,
            )
            scenarios_dict = None

    # Run the filter once to capture decisions (they reference the new run_id
    # written below). build_fox_groups_from_lp re-runs the filter internally,
    # which is fine — both invocations are deterministic on the same scenarios.
    _filtered_slots, dispatch_decisions = filter_robust_peak_export(
        plan,
        scenarios_dict,
        export_price_pence=export_prices,
    )

    groups, replan_at = build_fox_groups_from_lp(
        plan,
        scenarios=scenarios_dict,
        export_price_pence=export_prices,
    )
    fox_ok = upload_fox_if_operational(fox, groups)
    if replan_at is not None:
        try:
            from .runner import schedule_dynamic_mpc_replan

            replan_status = schedule_dynamic_mpc_replan(replan_at)
            logger.info(
                "Plan truncated to fit Fox V3 8-group cap; replan status=%s",
                replan_status,
            )
        except Exception as e:
            logger.warning("schedule_dynamic_mpc_replan failed (non-fatal): %s", e)
    daikin_n = write_daikin_from_lp_plan(plan_date, plan, forecast)

    run_at_iso = datetime.now(UTC).isoformat()
    run_id = db.log_optimizer_run(
        {
            "run_at": run_at_iso,
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

    # V11: durable per-slot snapshot so the History view can replay "what the
    # LP decided at this moment". Failures here must not bring down the solve
    # — wrap defensively.
    try:
        _persist_lp_snapshots(
            run_id=run_id,
            run_at_iso=run_at_iso,
            plan_date=plan_date,
            plan=plan,
            initial=initial,
            base_load=base_load,
            micro_climate_offset=micro_climate_offset,
        )
    except Exception as e:
        logger.warning("LP snapshot persistence failed (non-fatal): %s", e)

    # Persist dispatch decisions (per-slot LP-kind → committed kind, with the
    # 3 scenario export values) so the API/MCP/skill can explain why each
    # peak_export was committed or dropped. Only writes when there were
    # decisions to record (no peak_export in plan → empty list, skipped).
    try:
        for d in dispatch_decisions:
            db.upsert_dispatch_decision(run_id=run_id, **d)
        if dispatch_decisions:
            committed = sum(1 for d in dispatch_decisions if d["committed"] and d["lp_kind"] == "peak_export")
            dropped = sum(1 for d in dispatch_decisions if (not d["committed"]) and d["lp_kind"] == "peak_export")
            if committed or dropped:
                logger.info(
                    "dispatch_decisions: peak_export committed=%d dropped=%d (run_id=%d, trigger=%s, scenarios=%s)",
                    committed, dropped, run_id, trigger_reason,
                    "yes" if scenarios_dict else "no",
                )
    except Exception as e:
        logger.warning("dispatch_decisions persistence failed (non-fatal): %s", e)

    # Persist scenario solve summaries — one row per scenario, keyed by
    # batch_id (= nominal_run_id) so the full 3-scenario batch is queryable
    # via db.get_scenario_solve_batch(run_id). Writes nothing when scenarios
    # weren't run (trigger_reason not in allow-list, or plan had no peak_export).
    if scenarios_dict:
        try:
            for kind, result in scenarios_dict.items():
                # ScenarioSolveResult shape; degrade gracefully if a caller
                # ever passes a bare LpPlan (legacy / test path).
                if hasattr(result, "plan"):
                    plan_obj = result.plan
                    temp_delta = float(result.temp_delta_c)
                    load_factor = float(result.load_factor)
                    duration_ms = int(result.duration_ms)
                    error = result.error
                else:  # pragma: no cover — production always uses ScenarioSolveResult
                    plan_obj = result
                    temp_delta = 0.0
                    load_factor = 1.0
                    duration_ms = None
                    error = None
                pe_count = sum(
                    1 for ek in (plan_obj.export_kwh or [])
                    if float(ek) >= float(config.LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH)
                )
                db.upsert_scenario_solve_log(
                    batch_id=run_id,
                    nominal_run_id=run_id,
                    scenario_kind=kind,
                    lp_status=str(plan_obj.status),
                    objective_pence=float(plan_obj.objective_pence) if plan_obj.ok else None,
                    perturbation_temp_delta_c=temp_delta,
                    perturbation_load_factor=load_factor,
                    peak_export_slot_count=pe_count,
                    duration_ms=duration_ms,
                    error=error,
                )
            logger.info(
                "scenario_solve_log: batch_id=%d trigger=%s objs(opt/nom/pess)=%s",
                run_id, trigger_reason,
                "/".join(
                    f"{(scenarios_dict[k].plan.objective_pence if hasattr(scenarios_dict[k], 'plan') else scenarios_dict[k].objective_pence):.0f}p"
                    if scenarios_dict.get(k) else "-"
                    for k in ("optimistic", "nominal", "pessimistic")
                ),
            )
        except Exception as e:
            logger.warning("scenario_solve_log persistence failed (non-fatal): %s", e)

    _write_plan_consent(plan_date, strategy)

    return {
        "ok": True,
        "plan_date": plan_date,
        "target_vwap": target_vwap,
        "counts": counts,
        "fox_uploaded": fox_ok,
        "daikin_actions": daikin_n,
        "battery_warning": battery_warn,
        "scenarios_run": scenarios_dict is not None,
        "trigger_reason": trigger_reason,
        "strategy": strategy,
        "dhw_thermal_decay": None,
        "optimizer_backend": "lp",
        "lp_objective_pence": plan.objective_pence,
        "lp_status": plan.status,
    }


def _self_use_fallback(fox: FoxESSClient | None, reason: str = "No rates") -> dict[str, Any]:
    """Last-resort: set Fox to Self Use and log.  Called when no rates are available."""
    logger.warning("Self-Use fallback triggered: %s", reason)
    if fox and fox.api_key and not config.OPENCLAW_READ_ONLY:
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

    # Debounce: even when the hash changes, suppress the Telegram/Discord ping
    # if we already notified for this plan_date within the min-interval window.
    # The plan still upserts and auto-applies — only the outbound hook is skipped.
    notify_min_interval = int(getattr(config, "PLAN_NOTIFY_MIN_INTERVAL_SECONDS", 3600))
    last_notified = float((existing or {}).get("last_notified_at") or 0)
    within_debounce = (
        notify_min_interval > 0
        and last_notified > 0
        and (time.time() - last_notified) < notify_min_interval
    )

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
        if within_debounce:
            logger.info(
                "Plan %s notification debounced (last ping %.0fs ago < %ds) — hardware applied silently",
                plan_id, time.time() - last_notified, notify_min_interval,
            )
            return
        try:
            actions = db.get_actions_for_plan_date(plan_date)
            notify_plan_proposed(
                plan_id=plan_id,
                plan_date=plan_date,
                summary=f"[AUTO-APPLIED] {strategy}",
                actions=actions,
            )
            db.mark_plan_notified(plan_id)
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
    if within_debounce:
        logger.info(
            "Plan %s notification debounced (last ping %.0fs ago < %ds) — pending approval, no new ping",
            plan_id, time.time() - last_notified, notify_min_interval,
        )
        return
    try:
        actions = db.get_actions_for_plan_date(plan_date)
        notify_plan_proposed(
            plan_id=plan_id,
            plan_date=plan_date,
            summary=strategy,
            actions=actions,
        )
        db.mark_plan_notified(plan_id)
    except Exception as exc:
        logger.warning("notify_plan_proposed failed (non-fatal): %s", exc)


def run_optimizer(
    fox: FoxESSClient | None,
    daikin: Any | None = None,
    *,
    trigger_reason: str = "manual",
) -> dict[str, Any]:
    """Fetch rates from DB, plan (PuLP or heuristic), upload Fox V3, write Daikin actions.

    ``trigger_reason`` controls whether the LP path runs scenario robustness
    checks for peak_export commits (see ``LP_SCENARIOS_ON_TRIGGER_REASONS``).
    Known reasons: ``cron``, ``plan_push``, ``soc_drift``, ``forecast_revision``,
    ``dynamic_replan``, ``manual`` (the default for ad-hoc invocations).
    """
    backend = (config.OPTIMIZER_BACKEND or "lp").strip().lower()
    if backend == "heuristic":
        return _run_optimizer_heuristic(fox, daikin)
    return _run_optimizer_lp(fox, daikin, trigger_reason=trigger_reason)
