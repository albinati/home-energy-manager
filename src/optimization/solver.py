"""48-block solver: target mean price + per-slot LWT / Fox hints (V7 §6).

Weather-aware: when WEATHER_LAT/LON are configured the solver fetches a 48h
forecast and uses it to:
  - Boost LWT pre-heating before cold slots even if rates are only STANDARD
  - Estimate PV generation to skip grid battery charging when solar will fill it
  - Annotate each slot with heating demand and solar context
"""
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


def _is_away_like(preset: OperationPreset) -> bool:
    """AWAY is an alias for TRAVEL in the solver (both = hibernate mode)."""
    return preset in (OperationPreset.TRAVEL, OperationPreset.AWAY)


def _fetch_weather_forecast_safe():
    """Fetch 48h weather forecast; returns [] on any error (graceful degradation)."""
    try:
        from ..weather import fetch_forecast
        return fetch_forecast()
    except Exception:
        return []


def solve_plan(
    rates: list[dict],
    *,
    preset: OperationPreset = OperationPreset.NORMAL,
    tariff_code: Optional[str] = None,
    cheap_threshold_pence: Optional[float] = None,
    peak_start: Optional[str] = None,
    peak_end: Optional[str] = None,
    preheat_boost: Optional[float] = None,
    target_price_pence: Optional[float] = None,
    export_rates: Optional[list[dict]] = None,
) -> SolverPlan:
    """Build a :class:`SolverPlan` from Agile rates (structural / rule-based baseline).

    This is the first-pass solver: classifies each half-hour, sets LWT delta vs baseline 0,
    and assigns Fox mode *hints* (dispatcher applies safeties and may skip writes).

    When target_price_pence > 0, the solver ranks slots by price and marks enough of the
    cheapest ones as CHEAP to bring the weighted average toward the target. This drives
    how aggressively the system shifts load.

    BOOST preset ignores price classification and always uses max LWT / comfort settings.
    AWAY/TRAVEL are treated identically: hibernate mode.

    When export_rates are provided, peak slots with a high export price will use
    FORCE_DISCHARGE to sell battery energy to grid (only for travel/away or when export
    price significantly exceeds the import standing value).
    """
    code = (tariff_code or config.OCTOPUS_TARIFF_CODE or "").strip()
    thr = cheap_threshold_pence if cheap_threshold_pence is not None else config.OPTIMIZATION_CHEAP_THRESHOLD_PENCE
    ps = peak_start or config.OPTIMIZATION_PEAK_START
    pe = peak_end or config.OPTIMIZATION_PEAK_END
    boost = preheat_boost if preheat_boost is not None else config.OPTIMIZATION_PREHEAT_LWT_BOOST
    target = target_price_pence if target_price_pence is not None else config.TARGET_PRICE_PENCE

    limits = load_limits()

    slots_raw = build_slot_list(rates, num_slots=48)
    now = datetime.now(timezone.utc)

    # Fetch weather forecast (graceful: returns [] if not configured or fails)
    forecast = _fetch_weather_forecast_safe()

    # Build export rate lookup: map slot valid_from ISO string -> export price p/kWh
    export_rate_map: dict[str, float] = {}
    if export_rates:
        for er in export_rates:
            vf_str = er.get("valid_from") or ""
            exp_val = er.get("value_inc_vat")
            if vf_str and exp_val is not None:
                export_rate_map[vf_str] = float(exp_val)

    # When a target price is set, determine a dynamic cheap threshold that is aggressive
    # enough to reach that target. We rank slots cheapest-first and mark as CHEAP as many
    # as needed so that the resulting mean import cost sits at or below target.
    if target > 0 and slots_raw:
        sorted_prices = sorted(float(r.get("value_inc_vat") or 0) for r in slots_raw)
        total = len(sorted_prices)
        mean_all = sum(sorted_prices) / total
        if mean_all > target:
            for i in range(total):
                candidate_thr = sorted_prices[i]
                cheap_count = sum(1 for p in sorted_prices if p <= candidate_thr)
                if cheap_count > 0:
                    cheap_mean = sum(p for p in sorted_prices if p <= candidate_thr) / cheap_count
                    effective_mean = (cheap_mean * cheap_count + sum(p for p in sorted_prices if p > candidate_thr)) / total
                    if effective_mean <= target:
                        thr = max(thr, candidate_thr)
                        break

    # Look-ahead battery strategy: scan all 48 slots to find the cheapest overnight
    # window and most expensive peak window. If the price spread is significant,
    # force charge during the cheapest slots even if they don't hit the threshold.
    # Also skip grid charging in a slot when solar PV is expected to exceed 2kW.
    slot_prices = [float(r.get("value_inc_vat") or 0) for r in slots_raw]
    max_price = max(slot_prices) if slot_prices else thr
    min_price = min(slot_prices) if slot_prices else 0
    price_spread = max_price - min_price
    # Look-ahead force-charge threshold: if spread > 15p, we can profit from charging
    # at the cheapest slots to discharge during peak.
    lookahead_charge_thr = min_price + price_spread * 0.25 if price_spread > 15 else thr

    plans: list[HalfHourSlotPlan] = []
    prices: list[float] = []

    cheap_n = peak_n = 0
    for row in slots_raw:
        vf = row["_from_dt"]
        vt = row["_to_dt"]
        price = float(row.get("value_inc_vat") or 0)
        prices.append(price)

        # Get weather context for this slot
        weather_note = ""
        solar_strong = False
        cold_slot = False
        if forecast:
            from ..weather import get_forecast_for_slot
            fcast = get_forecast_for_slot(vf, forecast)
            if fcast is not None:
                solar_strong = fcast.estimated_pv_kw >= 2.0
                cold_slot = fcast.heating_demand_factor >= 0.7
                weather_note = (
                    f" | {fcast.temperature_c:.0f}°C "
                    f"solar~{fcast.estimated_pv_kw:.1f}kW"
                )

        # BOOST preset: always full comfort, ignore tariff classification
        if preset == OperationPreset.BOOST:
            kind = SlotKind.CHEAP  # treat every slot as cheap for max comfort
            lwt_d = float(boost)
            fox = FoxESSWorkModeHint.SELF_USE
            note = f"boost: full-comfort override — ignoring tariff{weather_note}"

        elif _slot_is_peak(vf, ps, pe):
            kind = SlotKind.PEAK
            peak_n += 1
            lwt_d = -min(2.0, float(boost))
            # Determine if we should force-discharge to export during this peak slot.
            # Use Agile export rate if available, else fall back to manual export pence.
            vf_str = vf.isoformat()
            export_price = export_rate_map.get(vf_str) or config.MANUAL_TARIFF_EXPORT_PENCE
            should_export = export_price > 0 and (
                _is_away_like(preset)
                or export_price >= price * 0.7  # export is at least 70% of import price
            )
            if should_export:
                fox = FoxESSWorkModeHint.FORCE_DISCHARGE
                note = f"peak: export discharge {export_price:.1f}p{weather_note}"
            else:
                fox = FoxESSWorkModeHint.SELF_USE
                note = f"peak: soften LWT{weather_note}"

        elif price <= thr:
            kind = SlotKind.CHEAP
            cheap_n += 1
            # Weather-aware LWT: extra boost before cold slots
            lwt_d = float(boost) + (1.0 if cold_slot else 0.0)
            lwt_d = min(lwt_d, limits.target_room_temp_max_c - 18.0)  # safety cap

            if preset == OperationPreset.GUESTS:
                # Skip grid charge if solar will fill battery anyway
                fox = FoxESSWorkModeHint.SELF_USE if solar_strong else FoxESSWorkModeHint.FORCE_CHARGE
                note = f"guests: cheap slot {'solar-skip' if solar_strong else 'charge bias'}{weather_note}"
            elif _is_away_like(preset):
                fox = FoxESSWorkModeHint.SELF_USE
                note = f"travel/away: hibernate{weather_note}"
            else:
                if solar_strong:
                    # Solar will charge battery — no need to force charge from grid
                    fox = FoxESSWorkModeHint.SELF_USE
                    note = f"cheap: solar expected ~{forecast[0].estimated_pv_kw if forecast else 0:.1f}kW — skip grid charge{weather_note}"
                elif price <= thr * 0.5:
                    fox = FoxESSWorkModeHint.FORCE_CHARGE
                    note = f"cheap: preheat / force charge{weather_note}"
                else:
                    fox = FoxESSWorkModeHint.SELF_USE
                    note = f"cheap: preheat{weather_note}"

        elif price <= lookahead_charge_thr and not _is_away_like(preset) and not solar_strong and price_spread > 15:
            # Look-ahead: cheap enough relative to today's peak to charge battery
            kind = SlotKind.CHEAP
            cheap_n += 1
            lwt_d = float(boost) * 0.5  # partial preheat
            fox = FoxESSWorkModeHint.FORCE_CHARGE
            note = f"look-ahead: cheap vs peak spread {price_spread:.0f}p — opportunistic charge{weather_note}"

        else:
            kind = SlotKind.STANDARD
            # Weather-aware: soften LWT when it's warm outside (no heating needed)
            if forecast:
                from ..weather import get_forecast_for_slot
                fcast = get_forecast_for_slot(vf, forecast)
                if fcast is not None and fcast.heating_demand_factor < 0.1:
                    lwt_d = -1.0  # mild weather: reduce LWT slightly
                    note = f"standard: mild weather ({fcast.temperature_c:.0f}°C) — reduce LWT{weather_note}"
                else:
                    lwt_d = 0.0
                    note = f"standard{weather_note}"
            else:
                lwt_d = 0.0
                note = "standard"
            fox = FoxESSWorkModeHint.SELF_USE

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
