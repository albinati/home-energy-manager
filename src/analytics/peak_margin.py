"""Peak-band conformal margin for the pessimistic scenario (#923, epic #920).

Summing per-slot p75 spreads over the expensive band covers the band SUM on
only ~57-72 % of days (slot errors are correlated across the band; quantiles
do not add). The margin computed here is taken directly on the band sum: the
q-quantile of the last N days of ``measured band load - committed total
forecast``, clamped to ``[0, LP_PEAK_MARGIN_MAX_KWH]``.

The optimizer adds it to the NOMINAL band load of the pessimistic scenario
(spread over the band slots in proportion to the nominal shape). The
peak-entry charge floor is derived from that pessimistic SoC trajectory, so
the re-solve sees it with no further plumbing.

Read-only; every number comes from ``load_error_log`` (or, for days it does not
cover yet, the same committed-forecast stitch + load integrator it is built
from) and the stored tariff rates.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)

PEAK_KEYS = ("expensive", "severe_peak", "band_peak")
MIN_USABLE_DAYS = 10
_CACHE_TTL_S = 3600.0
_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}


def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def quantile(values: list[float], q: float) -> float:
    """Linear-interpolation quantile (type 7); ``q`` clamped to [0, 1]."""
    if not values:
        raise ValueError("quantile of empty list")
    xs = sorted(float(v) for v in values)
    q = min(1.0, max(0.0, float(q)))
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _slot_key(iso: str, tz: ZoneInfo) -> tuple[date, int, int] | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(tz)
    except (ValueError, TypeError):
        return None
    return (t.date(), t.hour, 30 if t.minute >= 30 else 0)


def peak_local_slots(day: date, tz: ZoneInfo | None = None) -> set[tuple[int, int]]:
    """Local ``(hour, minute)`` slots of ``day``'s expensive band: Cosy peak, or
    the Agile ``expensive`` / ``severe_peak`` windows. Empty when unknown."""
    from .load_expected import band_windows_for_day

    windows, _kind = band_windows_for_day(day, tz or _tz())
    out: set[tuple[int, int]] = set()
    for w in windows:
        if w.key in PEAK_KEYS:
            out |= set(w.local_slots)
    return out


def excluded_days(first_day: date, tz: ZoneInfo | None = None) -> set[str]:
    """Local ISO dates to leave out of the history: away / holiday / return day
    (hard signals, reusing #922's ``_hard_away_dates``) and non-normal presets."""
    tz = tz or _tz()
    cutoff = datetime(first_day.year, first_day.month, first_day.day, tzinfo=tz).astimezone(UTC)
    try:
        away, ret, non_rep = db._hard_away_dates(cutoff.isoformat().replace("+00:00", "Z"), tz)
    except Exception:  # noqa: BLE001 - a missing signal must never break the solve
        logger.warning("peak margin: away dates read failed", exc_info=True)
        return set()
    return set(away) | set(ret) | set(non_rep)


def peak_band_error_history(
    as_of_day: date | None = None,
    window_days: int | None = None,
    *,
    tz: ZoneInfo | None = None,
) -> list[dict[str, Any]]:
    """Per past LOCAL day in ``[as_of-N, as_of-1]``: the expensive-band sum of
    ``actual - committed total forecast``. Newest first is NOT guaranteed; sorted
    by date ascending. Days that are away/return/non-normal, have no band, or
    lack full slot coverage are skipped.

    Each item: ``{date, error_kwh, actual_kwh, forecast_kwh, n_slots}``.
    """
    tz = tz or _tz()
    n = int(window_days if window_days is not None else config.LP_PEAK_MARGIN_WINDOW_DAYS)
    if as_of_day is None:
        as_of_day = datetime.now(UTC).astimezone(tz).date()
    first = as_of_day - timedelta(days=n)
    skip = excluded_days(first, tz)

    def _mid(d: date) -> datetime:
        return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)

    span_start, span_end = _mid(first), _mid(as_of_day)

    # 1) load_error_log rows (nightly): (forecast, actual) per slot
    rows_by_day: dict[date, dict[tuple[int, int], tuple[float, float]]] = {}
    try:
        s_iso = span_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        e_iso = span_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        for r in db.get_load_error_log_range(s_iso, e_iso):
            if r.get("forecast_kwh") is None or r.get("actual_kwh") is None:
                continue
            k = _slot_key(str(r["slot_time_utc"]), tz)
            if k is None:
                continue
            rows_by_day.setdefault(k[0], {})[(k[1], k[2])] = (
                float(r["forecast_kwh"]), float(r["actual_kwh"]))
    except Exception:  # noqa: BLE001
        logger.warning("peak margin: load_error_log read failed", exc_info=True)

    actual_all: dict[str, float] | None = None  # lazy: only if some day misses the log

    out: list[dict[str, Any]] = []
    d = first
    while d < as_of_day:
        day_iso = d.isoformat()
        cur = d
        d += timedelta(days=1)
        if day_iso in skip:
            continue
        band = peak_local_slots(cur, tz)
        if not band:
            continue
        have = rows_by_day.get(cur, {})
        pairs = {k: have[k] for k in band if k in have}
        if len(pairs) < len(band):
            # Not (fully) in load_error_log yet: rebuild from the same sources.
            if actual_all is None:
                try:
                    actual_all = db.half_hourly_kwh_for_utc_range(span_start, span_end, "load_power_kw")
                except Exception:  # noqa: BLE001
                    actual_all = {}
            fc: dict[tuple[int, int], float] = {}
            for dd in (cur - timedelta(days=1), cur):
                try:
                    for iso, (tot, _b) in db.committed_load_forecast_by_slot(dd).items():
                        k = _slot_key(iso, tz)
                        if k is not None and k[0] == cur:
                            fc[(k[1], k[2])] = float(tot)
                except Exception:  # noqa: BLE001
                    continue
            ac: dict[tuple[int, int], float] = {}
            for iso, v in (actual_all or {}).items():
                k = _slot_key(iso, tz)
                if k is not None and k[0] == cur:
                    ac[(k[1], k[2])] = float(v)
            pairs = {k: (fc[k], ac[k]) for k in band if k in fc and k in ac}
        if len(pairs) < len(band):
            continue
        f = sum(v[0] for v in pairs.values())
        a = sum(v[1] for v in pairs.values())
        out.append({"date": day_iso, "error_kwh": a - f, "actual_kwh": a,
                    "forecast_kwh": f, "n_slots": len(pairs)})
    return out


def compute_margin(errors: list[float], q: float, cap: float) -> dict[str, Any]:
    """Margin + coverage from a list of band-sum errors. Pure."""
    if len(errors) < MIN_USABLE_DAYS:
        return {"margin_kwh": 0.0, "raw_kwh": None, "coverage_pct": None,
                "n_days": len(errors), "fallback": "too_few_days"}
    raw = quantile(errors, q)
    margin = min(max(0.0, raw), max(0.0, float(cap)))
    cov = 100.0 * sum(1 for e in errors if e <= margin + 1e-9) / len(errors)
    return {"margin_kwh": margin, "raw_kwh": raw, "coverage_pct": cov,
            "n_days": len(errors), "fallback": None}


def peak_band_margin(
    *,
    as_of_day: date | None = None,
    use_cache: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Current margin diagnostics (cached for an hour). ``margin_kwh`` is what the
    optimizer applies when ``LP_PEAK_MARGIN_ENABLED``; the dict is always
    computable (the read endpoint shows it even with the switch off).

    Keys: enabled, margin_kwh, raw_kwh, coverage_pct, n_days, quantile,
    window_days, cap_kwh, fallback ("too_few_days"|"error"|None), band_slots,
    as_of.
    """
    tz = _tz()
    q = min(0.95, max(0.5, float(config.LP_PEAK_MARGIN_QUANTILE)))
    n = max(14, min(90, int(config.LP_PEAK_MARGIN_WINDOW_DAYS)))
    cap = float(config.LP_PEAK_MARGIN_MAX_KWH)
    day = as_of_day or datetime.now(UTC).astimezone(tz).date()
    ckey = (str(config.DB_PATH), day.isoformat(), q, n, cap)
    mono = time.monotonic()
    if use_cache and not force:
        hit = _cache.get(ckey)
        if hit is not None and mono - hit[0] < _CACHE_TTL_S:
            return dict(hit[1])
    try:
        hist = peak_band_error_history(day, n, tz=tz)
        res = compute_margin([h["error_kwh"] for h in hist], q, cap)
        band_slots = len(peak_local_slots(day, tz))
    except Exception:  # noqa: BLE001 - never break a solve; fall back to legacy
        logger.warning("peak margin: computation failed", exc_info=True)
        res = {"margin_kwh": 0.0, "raw_kwh": None, "coverage_pct": None,
               "n_days": 0, "fallback": "error"}
        band_slots = 0
    res.update({
        "enabled": bool(config.LP_PEAK_MARGIN_ENABLED),
        "quantile": q, "window_days": n, "cap_kwh": cap,
        "band_slots": band_slots, "as_of": day.isoformat(),
    })
    for k in ("margin_kwh", "raw_kwh", "coverage_pct"):
        if res.get(k) is not None:
            res[k] = round(float(res[k]), 3)
    if use_cache:
        if len(_cache) > 32:
            _cache.clear()
        _cache[ckey] = (mono, dict(res))
    return res


def peak_band_slot_groups(
    slot_starts_utc: list[datetime], prices: list[float]
) -> list[list[int]]:
    """Slot indices of the expensive band, one group per LOCAL day (the margin is
    a per-day band-sum quantity). Same classifier the charge floor uses."""
    from ..google_calendar.tiers import Slot, classify_day

    tz = _tz()
    n = min(len(slot_starts_utc), len(prices))
    by_day: dict[date, list[Slot]] = {}
    for i in range(n):
        st = slot_starts_utc[i]
        by_day.setdefault(st.astimezone(tz).date(), []).append(
            Slot(start_utc=st, end_utc=st + timedelta(minutes=30), price_p=float(prices[i])))
    idx = {slot_starts_utc[i]: i for i in range(n)}
    groups: list[list[int]] = []
    for _d, slots in sorted(by_day.items()):
        g: list[int] = []
        for w in classify_day(slots):
            if w.tier.key in PEAK_KEYS:
                t = w.start_utc
                while t < w.end_utc:
                    j = idx.get(t)
                    if j is not None:
                        g.append(j)
                    t += timedelta(minutes=30)
        if g:
            groups.append(sorted(g))
    return groups


def band_extra_by_slot(
    slot_starts_utc: list[datetime],
    prices: list[float],
    base_load_kwh: list[float],
    margin_kwh: float,
    *,
    band_slots: int = 0,
) -> list[float | None] | None:
    """Per-slot extra kWh (``None`` outside the band) that, added to the nominal
    load, makes each day's band sum = nominal + margin, distributed in proportion
    to the nominal shape (equal split if the nominal band is ~0). A day whose
    band is only partly inside the horizon (mid-day replan) gets the margin
    scaled by ``slots_in_horizon / band_slots`` when the typical band length is
    known. ``None`` when there is nothing to add."""
    if margin_kwh <= 1e-9:
        return None
    groups = peak_band_slot_groups(slot_starts_utc, prices)
    if not groups:
        return None
    extra: list[float | None] = [None] * len(base_load_kwh)
    for g in groups:
        g = [i for i in g if i < len(base_load_kwh)]
        if not g:
            continue
        m = margin_kwh
        if band_slots and len(g) < band_slots:
            m = margin_kwh * len(g) / band_slots
        tot = sum(max(0.0, base_load_kwh[i]) for i in g)
        for i in g:
            share = (max(0.0, base_load_kwh[i]) / tot) if tot > 1e-9 else 1.0 / len(g)
            extra[i] = m * share
    return extra


def clear_cache() -> None:
    _cache.clear()
