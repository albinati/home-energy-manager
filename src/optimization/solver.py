"""48-block solver: target mean price + per-slot LWT / Fox hints (V7 §6)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..config import config
from .blocks import build_slot_list
from .models import (
    FoxESSWorkModeHint,
    HalfHourSlotPlan,
    OperationPreset,
    SlotKind,
    SolverPlan,
)
from .safeties import load_limits


def _parse_hhmm(s: str):
    m = re.match(r"(\d{1,2}):(\d{2})", str(s).strip())
    if not m:
        return None
    from datetime import time

    return time(int(m.group(1)), int(m.group(2)))


def _slot_is_peak(slot_start: datetime, peak_start: str, peak_end: str) -> bool:
    """Peak window uses local wall clock (London) per V7 / UK Agile operation."""
    tz = ZoneInfo(config.OPTIMIZATION_TIMEZONE)
    local = slot_start.astimezone(tz)
    t = local.time()
    ps = _parse_hhmm(peak_start)
    pe = _parse_hhmm(peak_end)
    if not ps or not pe:
        return False
    if ps <= pe:
        return ps <= t <= pe
    return t >= ps or t <= pe


def solve_plan(
    rates: list[dict],
    *,
    preset: OperationPreset = OperationPreset.NORMAL,
    tariff_code: Optional[str] = None,
    cheap_threshold_pence: Optional[float] = None,
    peak_start: Optional[str] = None,
    peak_end: Optional[str] = None,
    preheat_boost: Optional[float] = None,
) -> SolverPlan:
    """Build a :class:`SolverPlan` from Agile rates (structural / rule-based baseline).

    This is the first-pass solver: classifies each half-hour, sets LWT delta vs baseline 0,
    and assigns Fox mode *hints* (dispatcher applies safeties and may skip writes).
    """
    code = (tariff_code or config.OCTOPUS_TARIFF_CODE or "").strip()
    thr = cheap_threshold_pence if cheap_threshold_pence is not None else config.OPTIMIZATION_CHEAP_THRESHOLD_PENCE
    ps = peak_start or config.OPTIMIZATION_PEAK_START
    pe = peak_end or config.OPTIMIZATION_PEAK_END
    boost = preheat_boost if preheat_boost is not None else config.OPTIMIZATION_PREHEAT_LWT_BOOST

    limits = load_limits()
    _ = limits  # reserved for future volume / comfort coupling

    slots_raw = build_slot_list(rates, num_slots=48)
    now = datetime.now(timezone.utc)
    plans: list[HalfHourSlotPlan] = []
    prices: list[float] = []

    cheap_n = peak_n = 0
    for row in slots_raw:
        vf = row["_from_dt"]
        vt = row["_to_dt"]
        price = float(row.get("value_inc_vat") or 0)
        prices.append(price)

        if _slot_is_peak(vf, ps, pe):
            kind = SlotKind.PEAK
            peak_n += 1
            lwt_d = -min(2.0, float(boost))
            if preset == OperationPreset.TRAVEL and config.MANUAL_TARIFF_EXPORT_PENCE > 0:
                fox = FoxESSWorkModeHint.FORCE_DISCHARGE
                note = "travel: peak export window hint"
            else:
                fox = FoxESSWorkModeHint.SELF_USE
                note = "peak: soften LWT"
        elif price <= thr:
            kind = SlotKind.CHEAP
            cheap_n += 1
            lwt_d = float(boost)
            if preset == OperationPreset.GUESTS:
                fox = FoxESSWorkModeHint.FORCE_CHARGE
                note = "guests: cheap slot charge bias"
            elif preset == OperationPreset.TRAVEL:
                fox = FoxESSWorkModeHint.SELF_USE
                note = "travel: hibernate — minimal heat charge"
            else:
                fox = FoxESSWorkModeHint.FORCE_CHARGE if price <= thr * 0.5 else FoxESSWorkModeHint.SELF_USE
                note = "cheap: preheat / opportunistic charge"
        else:
            kind = SlotKind.STANDARD
            lwt_d = 0.0
            fox = FoxESSWorkModeHint.SELF_USE
            note = "standard"

        plans.append(
            HalfHourSlotPlan(
                valid_from=vf,
                valid_to=vt,
                import_price_pence=price,
                slot_kind=kind,
                lwt_offset_delta=lwt_d,
                fox_mode_hint=fox,
                notes=note,
            )
        )

    mean_price = sum(prices) / len(prices) if prices else 0.0
    return SolverPlan(
        computed_at=now,
        preset=preset,
        tariff_code=code,
        slots=plans,
        target_mean_price_pence=round(mean_price, 4),
        cheap_slot_count=cheap_n,
        peak_slot_count=peak_n,
    )


def current_slot_plan(plan: SolverPlan, at: Optional[datetime] = None) -> Optional[HalfHourSlotPlan]:
    """Return the half-hour plan row covering ``at`` (UTC), or None."""
    t = at or datetime.now(timezone.utc)
    for s in plan.slots:
        if s.valid_from <= t < s.valid_to:
            return s
    return None
