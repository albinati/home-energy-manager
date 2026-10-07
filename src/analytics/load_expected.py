"""Expected household consumption per tariff band — the "consumption
probability" the owner asked for (#818).

The LP plans against per-slot medians and insures the expensive blocks with a
per-slot upper quantile (see ``residual_load_profile_v2``). This module answers
the household-level question instead: *how much will the house pull in the
04–07 / 07–13 / 13–16 / 16–19 / 19–22 / 22–24 blocks today, with what
probability?* — computed as **band-sum quantiles over same-day-type history**
(weekday / weekend), not as sums of per-slot quantiles (sum of p90s ≫ p90 of
the sum when the load is spiky). Alongside, what the committed plan assumed
for each block, what has been realised so far, and how the committed forecast
has erred in that block historically.

Read-only; every number comes from ``pv_realtime_history`` (measured load),
``load_error_log`` (committed vs realised) and the stored tariff rates.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import mean, median, quantiles
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..energy.tariff_structure import band_profile_local, detect, display_name, is_tou_family
from ..google_calendar.tiers import Slot, classify_day

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 600.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class BandWindow:
    key: str                 # tier key: band_cheap / band_day / band_peak / Agile tier keys
    label: str               # tier title ("cheap" / "day" / "peak" / "Expensive" …)
    start_utc: datetime
    end_utc: datetime
    price_p: float           # mean price over the window
    local_slots: set[tuple[int, int]] = field(default_factory=set)  # (h, m) inside the window

    @property
    def hours(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds() / 3600.0


def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def _q(vs: list[float], p: float) -> float | None:
    if not vs:
        return None
    if len(vs) == 1:
        return float(vs[0])
    if p == 0.5:
        return float(median(vs))
    # statistics.quantiles (exclusive) on n=20 gives 5 % steps; clamp to data range.
    qs = quantiles(vs, n=20)
    idx = min(len(qs) - 1, max(0, int(round(p * 20)) - 1))
    return float(min(max(qs[idx], min(vs)), max(vs)))


def _slot_local_key(iso: str, tz: ZoneInfo) -> tuple[date, int, int] | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(tz)
    except (ValueError, TypeError):
        return None
    return (t.date(), t.hour, 30 if t.minute >= 30 else 0)


def band_windows_for_day(day: date, tz: ZoneInfo | None = None) -> tuple[list[BandWindow], str]:
    """The day's tariff windows (merged same-tier runs) and the structure kind.

    Uses the stored rates for the local day; when the day has no rates yet (a
    D+1 the fetch has not published) and the tariff is a TOU family, the local
    band profile of the stored rows fills the day (same source as the LP
    horizon filler). Returns ``([], "unknown")`` when nothing is known.
    """
    tz = tz or _tz()
    code = str(getattr(config, "OCTOPUS_TARIFF_CODE", "") or "")
    rows = db.get_agile_rates_slots_for_local_day(code, day, tz_name=str(tz.key)) if code else []
    slots: list[Slot] = []
    for r in rows:
        try:
            vf = datetime.fromisoformat(str(r["valid_from"]).replace("Z", "+00:00"))
            vt = datetime.fromisoformat(str(r["valid_to"]).replace("Z", "+00:00"))
            slots.append(Slot(start_utc=vf.astimezone(UTC), end_utc=vt.astimezone(UTC),
                              price_p=float(r["value_inc_vat"])))
        except (KeyError, ValueError, TypeError):
            continue
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    if len(slots) < 40 and is_tou_family(code):
        prof = band_profile_local(code, tz_name=str(tz.key))
        if prof:
            slots = []
            t = local_midnight
            end = local_midnight + timedelta(days=1)
            while t < end:
                p = prof.get((t.hour, 30 if t.minute >= 30 else 0))
                if p is not None:
                    slots.append(Slot(start_utc=t.astimezone(UTC),
                                      end_utc=(t + timedelta(minutes=30)).astimezone(UTC),
                                      price_p=float(p)))
                t += timedelta(minutes=30)
    if not slots:
        return [], "unknown"
    structure = detect([s.price_p for s in slots], short_ok=is_tou_family(code))
    windows: list[BandWindow] = []
    for w in classify_day(slots):
        bw = BandWindow(key=w.tier.key, label=w.tier.title, start_utc=w.start_utc,
                        end_utc=w.end_utc, price_p=float(w.price_mean))
        t = w.start_utc
        while t < w.end_utc:
            lt = t.astimezone(tz)
            bw.local_slots.add((lt.hour, 30 if lt.minute >= 30 else 0))
            t += timedelta(minutes=30)
        windows.append(bw)
    return windows, ("banded" if structure.is_banded else "dynamic")


def _same_group_history_days(day: date, n_days: int) -> list[date]:
    group_we = day.weekday() >= 5
    out: list[date] = []
    d = day - timedelta(days=1)
    scanned = 0
    while scanned < n_days:
        if (d.weekday() >= 5) == group_we:
            out.append(d)
        d -= timedelta(days=1)
        scanned += 1
    return out


def _window_sum(slot_kwh: dict[str, float], window: BandWindow, tz: ZoneInfo,
                *, day: date, until_utc: datetime | None = None) -> tuple[float, int]:
    """Sum of ``slot_kwh`` (UTC-ISO keyed) over the window's local slots on
    ``day``; returns ``(kwh, n_slots_seen)``."""
    tot = 0.0
    n = 0
    for iso, kwh in slot_kwh.items():
        k = _slot_local_key(iso, tz)
        if k is None or k[0] != day or (k[1], k[2]) not in window.local_slots:
            continue
        if until_utc is not None:
            try:
                st = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if st + timedelta(minutes=30) > until_utc:
                continue
        tot += float(kwh)
        n += 1
    return tot, n


def expected_load_by_band(
    day: date | None = None,
    *,
    history_days: int | None = None,
    now_utc: datetime | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Per-window expected consumption (band-sum quantiles over same-day-type
    history), committed plan, realised-so-far and committed-forecast error
    history for ``day`` (local, default today)."""
    tz = _tz()
    now_utc = now_utc or datetime.now(UTC)
    day = day or now_utc.astimezone(tz).date()
    history_days = int(history_days or getattr(config, "LOAD_EXPECTED_HISTORY_DAYS", 60) or 60)
    ckey = f"{config.DB_PATH}:{day.isoformat()}:{history_days}"
    mono = time.monotonic()
    if use_cache:
        hit = _cache.get(ckey)
        if hit is not None and mono - hit[0] < _CACHE_TTL_S:
            return hit[1]

    windows, structure = band_windows_for_day(day, tz)
    group = "weekend" if day.weekday() >= 5 else "weekday"
    hist_days = _same_group_history_days(day, history_days)

    # Per-history-day half-hourly load (one query per day, trapezoid-integrated).
    hist_slots: dict[date, dict[str, float]] = {}
    for d in hist_days:
        try:
            hist_slots[d] = db._half_hourly_grid_kwh_for_day(d, "load_power_kw")
        except Exception:  # noqa: BLE001 — one bad day must not kill the read
            hist_slots[d] = {}

    today_slots: dict[str, float] = {}
    try:
        today_slots = db._half_hourly_grid_kwh_for_day(day, "load_power_kw")
    except Exception:  # noqa: BLE001
        today_slots = {}
    committed: dict[str, float] = {}
    try:
        for iso, (tot, _base) in db.committed_load_forecast_by_slot(day).items():
            committed[iso] = float(tot)
    except Exception:  # noqa: BLE001
        committed = {}

    # Committed-forecast error history (actual − forecast) per history day.
    err_rows_by_day: dict[date, dict[str, float]] = {}
    try:
        start = (min(hist_days) if hist_days else day).isoformat() + "T00:00:00Z"
        end = (day + timedelta(days=1)).isoformat() + "T00:00:00Z"
        for r in db.get_load_error_log_range(start, end):
            k = _slot_local_key(str(r["slot_time_utc"]), tz)
            if k is None:
                continue
            err_rows_by_day.setdefault(k[0], {})[str(r["slot_time_utc"])] = float(r["error_kwh"] or 0.0)
    except Exception:  # noqa: BLE001
        err_rows_by_day = {}

    out_bands: list[dict[str, Any]] = []
    day_sums: list[float] = []
    for w in windows:
        n_expected_slots = max(1, len(w.local_slots))
        sums: list[float] = []
        for d in hist_days:
            s, n = _window_sum(hist_slots.get(d, {}), w, tz, day=d)
            if n >= 0.8 * n_expected_slots:  # skip days with telemetry gaps in the window
                sums.append(s)
        realised, n_real = _window_sum(today_slots, w, tz, day=day, until_utc=now_utc)
        committed_kwh, n_comm = _window_sum(committed, w, tz, day=day)
        errs: list[float] = []
        for d in hist_days:
            rows = err_rows_by_day.get(d)
            if not rows:
                continue
            e, n = _window_sum(rows, w, tz, day=d)
            if n >= 0.8 * n_expected_slots:
                errs.append(e)
        if now_utc >= w.end_utc:
            status = "done"
        elif now_utc >= w.start_utc:
            status = "ongoing"
        else:
            status = "upcoming"
        elapsed = max(0.0, min(1.0, (now_utc - w.start_utc).total_seconds()
                               / max(1.0, (w.end_utc - w.start_utc).total_seconds())))
        out_bands.append({
            "key": w.key,
            "label": w.label,
            "start_utc": w.start_utc.isoformat().replace("+00:00", "Z"),
            "end_utc": w.end_utc.isoformat().replace("+00:00", "Z"),
            "start_local": w.start_utc.astimezone(tz).strftime("%H:%M"),
            "end_local": w.end_utc.astimezone(tz).strftime("%H:%M"),
            "hours": round(w.hours, 2),
            "price_p": round(w.price_p, 3),
            "status": status,
            "progress": round(elapsed, 3),
            "expected_kwh": {
                "p50": None if not sums else round(_q(sums, 0.5) or 0.0, 3),
                "p75": None if not sums else round(_q(sums, 0.75) or 0.0, 3),
                "p90": None if not sums else round(_q(sums, 0.9) or 0.0, 3),
                "max": None if not sums else round(max(sums), 3),
                "n_days": len(sums),
            },
            "committed_kwh": round(committed_kwh, 3) if n_comm else None,
            "realised_kwh": round(realised, 3) if n_real else None,
            "forecast_error_kwh": {
                "mean": round(mean(errs), 3) if errs else None,
                "p90": round(_q(errs, 0.9) or 0.0, 3) if errs else None,
                "under_forecast_days": sum(1 for e in errs if e > 0.0),
                "n_days": len(errs),
            },
        })

    # Whole-day totals over the same history (band-agnostic).
    for d in hist_days:
        sl = hist_slots.get(d, {})
        if len(sl) >= 40:
            day_sums.append(sum(float(v) for v in sl.values()))
    realised_today = sum(float(v) for iso, v in today_slots.items()
                         if (lambda k: k is not None and k[0] == day)(_slot_local_key(iso, tz)))
    result = {
        "date": day.isoformat(),
        "now_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "tariff_display_name": display_name(),
        "tariff_structure": structure,
        "day_type": group,
        "history_days": history_days,
        "bands": out_bands,
        "day": {
            "expected_kwh": {
                "p50": None if not day_sums else round(_q(day_sums, 0.5) or 0.0, 2),
                "p75": None if not day_sums else round(_q(day_sums, 0.75) or 0.0, 2),
                "p90": None if not day_sums else round(_q(day_sums, 0.9) or 0.0, 2),
                "n_days": len(day_sums),
            },
            "committed_kwh": round(sum(committed.values()), 2) if committed else None,
            "realised_kwh": round(realised_today, 2) if today_slots else None,
        },
    }
    if use_cache:
        if len(_cache) > 32:
            _cache.clear()
        _cache[ckey] = (mono, result)
    return result


def clear_cache() -> None:
    _cache.clear()
