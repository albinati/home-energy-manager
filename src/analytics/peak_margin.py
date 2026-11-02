"""Peak-band conformal margin for the pessimistic scenario (#923, epic #920).

Summing per-slot p75 spreads over the expensive band covers the band SUM on
only ~57-72 % of days (slot errors are correlated across the band; quantiles
do not add). The margin computed here is taken directly on the band sum, on a
RESIDUAL basis:

    daily error = sum over band slots of (measured load - measured heat pump)
                  - sum over band slots of the committed BASE forecast

so it is immune to heat-pump model changes (the LP's HP is a decision
variable). The measured heat pump is #922's hybrid split (Onecta buckets,
daily remainder, water-fill; never ``telemetry_integral``), reused from
``db.measured_residual_by_local_slot``.

The margin is the conformal order statistic ``ceil((n+1) q)`` (capped at n) of
the last N days' errors, clamped to ``[0, LP_PEAK_MARGIN_MAX_KWH]``. Only days
whose committed base came from the SAME base-forecaster settings as the current
one (v3 flag, statistic, dow shrink, recent bias) are used; the key is stored
per run in ``exogenous_snapshot["base_forecaster_key"]``. Runs without a key
count as the legacy v2 forecaster. Coverage is reported OUT-OF-SAMPLE (each
day against the margin computed from the days before it).

The optimizer applies it to the NOMINAL band load of the pessimistic scenario
as a floor (never lowering the summed slot spread). The peak-entry charge floor
is read from that pessimistic SoC trajectory, so the re-solve sees it.

Agile note: on a dynamic tariff the band is the day's expensive/severe_peak
windows, whose length varies day to day; a day's band is the set of local
slots it had. The per-day cache stores that set.
"""
from __future__ import annotations

import bisect
import logging
import math
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)

PEAK_KEYS = ("expensive", "severe_peak", "band_peak")
MIN_USABLE_DAYS = 14
LEGACY_KEY = "v2|bias=0"
_CACHE_TTL_S = 3600.0
_NEG_TTL_S = 3600.0
_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
# per local day: complete days never change -> computed once per process
_day_cache: dict[tuple[str, str], dict[str, Any]] = {}
_day_miss: dict[tuple[str, str], float] = {}


def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def current_forecaster_key() -> str:
    """Identity of the base-load forecaster as configured NOW."""
    bias = 1 if getattr(config, "LOAD_RECENT_BIAS_ENABLED", False) else 0
    if not bool(getattr(config, "LP_RESIDUAL_PROFILE_V2", True)):
        return f"v1|bias={bias}"
    if bool(getattr(config, "LP_RESIDUAL_PROFILE_V3", False)):
        stat = str(getattr(config, "LP_RESIDUAL_STATISTIC", "mean")).strip().lower()
        shrink = float(getattr(config, "LP_RESIDUAL_DOW_SHRINK", 0.5))
        away = 1 if getattr(config, "LP_RESIDUAL_AWAY_HARD_EXCLUDE", True) else 0
        return f"v3|{stat}|{shrink:.2f}|away={away}|bias={bias}"
    return f"v2|bias={bias}"


def conformal_quantile(values: list[float], q: float) -> float:
    """Split-conformal order statistic: the ceil((n+1) q)-th smallest value,
    capped at n (so with few points it is the max)."""
    if not values:
        raise ValueError("quantile of empty list")
    xs = sorted(float(v) for v in values)
    n = len(xs)
    k = min(n, max(1, math.ceil((n + 1) * min(1.0, max(0.0, q)) - 1e-12)))
    return xs[k - 1]


def _clamp(x: float, cap: float) -> float:
    return min(max(0.0, x), max(0.0, float(cap)))


def oos_coverage(
    hist: list[tuple[str, float]], q: float, cap: float, window_days: int,
    min_prior: int = MIN_USABLE_DAYS,
) -> tuple[float | None, int]:
    """Rolling out-of-sample coverage. ``hist`` = [(date_iso, error)] sorted by
    date. For each day d with >= ``min_prior`` earlier days inside the window,
    the margin is computed from those days only; covered if error_d <= margin.
    Returns (coverage % or None, number of evaluated days)."""
    covered = 0
    evaluated = 0
    for i, (d_iso, err) in enumerate(hist):
        d0 = date.fromisoformat(d_iso) - timedelta(days=window_days)
        prior = [e for (dj, e) in hist[:i] if date.fromisoformat(dj) >= d0]
        if len(prior) < min_prior:
            continue
        m = _clamp(conformal_quantile(prior, q), cap)
        evaluated += 1
        if err <= m + 1e-9:
            covered += 1
    return (100.0 * covered / evaluated if evaluated else None), evaluated


def compute_margin(
    hist: list[tuple[str, float]], q: float, cap: float, window_days: int,
) -> dict[str, Any]:
    """Margin + out-of-sample coverage from ``[(date_iso, error)]`` sorted by date."""
    errs = [e for _d, e in hist]
    if len(errs) < MIN_USABLE_DAYS:
        return {"margin_kwh": 0.0, "raw_kwh": None, "coverage_oos_pct": None,
                "n_oos": 0, "n_days": len(errs), "fallback": "too_few_days"}
    raw = conformal_quantile(errs, q)
    cov, n_oos = oos_coverage(hist, q, cap, window_days)
    return {"margin_kwh": _clamp(raw, cap), "raw_kwh": raw, "coverage_oos_pct": cov,
            "n_oos": n_oos, "n_days": len(errs), "fallback": None}


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
    """Local ISO dates to leave out: away / holiday / return day (hard signals,
    #922's ``_hard_away_dates``) and non-normal presets."""
    tz = tz or _tz()
    cutoff = datetime(first_day.year, first_day.month, first_day.day, tzinfo=tz).astimezone(UTC)
    try:
        away, ret, non_rep = db._hard_away_dates(cutoff.isoformat().replace("+00:00", "Z"), tz)
    except Exception:  # noqa: BLE001 - a missing signal must never break the solve
        logger.warning("peak margin: away dates read failed", exc_info=True)
        return set()
    return set(away) | set(ret) | set(non_rep)


def day_forecaster_keys(first_day: date, last_day: date, tz: ZoneInfo | None = None) -> dict[str, str]:
    """``{local_date_iso: base_forecaster_key}``: the key of the latest run made
    before 16:00 local of that day (the run whose base forecast the band got).
    Days with no keyed run map to ``LEGACY_KEY``."""
    tz = tz or _tz()
    lo = datetime(first_day.year, first_day.month, first_day.day, tzinfo=tz).astimezone(UTC) - timedelta(days=1)
    hi = datetime(last_day.year, last_day.month, last_day.day, 23, tzinfo=tz).astimezone(UTC)
    runs: list[tuple[datetime, str]] = []
    try:
        with db._lock:
            conn = db.get_connection()
            try:
                rows = conn.execute(
                    "SELECT run_at_utc, json_extract(exogenous_snapshot_json, '$.base_forecaster_key') AS k "
                    "FROM lp_inputs_snapshot WHERE run_at_utc >= ? AND run_at_utc <= ? ORDER BY run_at_utc",
                    (lo.isoformat().replace("+00:00", "Z"), hi.isoformat().replace("+00:00", "Z")),
                ).fetchall()
            finally:
                conn.close()
        for r in rows:
            try:
                t = datetime.fromisoformat(str(r["run_at_utc"]).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue
            runs.append((t.astimezone(UTC), str(r["k"]) if r["k"] else LEGACY_KEY))
    except Exception:  # noqa: BLE001
        logger.warning("peak margin: forecaster keys read failed", exc_info=True)
    times = [t for t, _k in runs]
    out: dict[str, str] = {}
    d = first_day
    while d <= last_day:
        at = datetime(d.year, d.month, d.day, 16, tzinfo=tz).astimezone(UTC)
        i = bisect.bisect_right(times, at) - 1
        # only a run made within the 36 h before the band started counts
        out[d.isoformat()] = runs[i][1] if (i >= 0 and at - times[i] <= timedelta(hours=36)) else LEGACY_KEY
        d += timedelta(days=1)
    return out


def _band_utc_days(day: date, band: set[tuple[int, int]], tz: ZoneInfo) -> list[date]:
    """UTC dates the local day's band slots fall in (usually one)."""
    out: set[date] = set()
    for (h, m) in band:
        out.add(datetime(day.year, day.month, day.day, h, m, tzinfo=tz).astimezone(UTC).date())
    return sorted(out)


def _log_base_by_day(days: list[date], bands: dict[str, set[tuple[int, int]]], tz: ZoneInfo
                     ) -> dict[str, dict[tuple[int, int], float]]:
    """Committed BASE forecast per band slot from ``load_error_log`` (one range
    read covering only the UTC days the band slots fall in)."""
    if not days:
        return {}
    utc_days = sorted({u for d in days for u in _band_utc_days(d, bands[d.isoformat()], tz)})
    lo = datetime(utc_days[0].year, utc_days[0].month, utc_days[0].day, tzinfo=UTC)
    hi = datetime(utc_days[-1].year, utc_days[-1].month, utc_days[-1].day, tzinfo=UTC) + timedelta(days=1)
    out: dict[str, dict[tuple[int, int], float]] = {}
    try:
        rows = db.get_load_error_log_range(lo.strftime("%Y-%m-%dT%H:%M:%SZ"), hi.strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Exception:  # noqa: BLE001
        logger.warning("peak margin: load_error_log read failed", exc_info=True)
        return {}
    for r in rows:
        if r.get("forecast_base_kwh") is None:
            continue
        k = _slot_key(str(r["slot_time_utc"]), tz)
        if k is None:
            continue
        out.setdefault(k[0].isoformat(), {})[(k[1], k[2])] = float(r["forecast_base_kwh"])
    return out


def ensure_history(as_of_day: date, window_days: int, *, only_yesterday: bool = False) -> int:
    """Rebuild ``load_error_log`` for window days whose band slots are missing
    (the existing ``db.rebuild_load_error_log_for_date`` per UTC day the band
    overlaps). OFF the solve path: the nightly job / warm() run it for the whole
    window; the solve path only repairs yesterday. Returns UTC days rebuilt."""
    tz = _tz()
    first = as_of_day - timedelta(days=window_days)
    days = [as_of_day - timedelta(days=1)] if only_yesterday else [
        first + timedelta(days=i) for i in range(window_days)]
    bands = {d.isoformat(): peak_local_slots(d, tz) for d in days}
    days = [d for d in days if bands[d.isoformat()]]
    have = _log_base_by_day(days, bands, tz)
    rebuilt = 0
    for d in days:
        b = bands[d.isoformat()]
        if all(k in have.get(d.isoformat(), {}) for k in b):
            continue
        for u in _band_utc_days(d, b, tz):
            try:
                db.rebuild_load_error_log_for_date(u)
                rebuilt += 1
            except Exception:  # noqa: BLE001
                logger.warning("peak margin: rebuild %s failed", u, exc_info=True)
    return rebuilt


def peak_band_error_history(
    as_of_day: date | None = None,
    window_days: int | None = None,
    *,
    tz: ZoneInfo | None = None,
    forecaster_key: str | None = None,
    repair_yesterday: bool = True,
) -> list[dict[str, Any]]:
    """Per past LOCAL day in ``[as_of-N, as_of-1]``: the expensive-band error on a
    residual basis, ``sum(measured load - measured HP) - sum(committed base)``.
    Sorted by date. Skipped: away/return/non-normal days, days without a band,
    days whose committed base came from a different forecaster key, days without
    a measured HP split, days with any band slot missing.

    Each item: ``{date, error_kwh, residual_kwh, base_kwh, hp_kwh, n_slots}``.
    """
    tz = tz or _tz()
    n = int(window_days if window_days is not None else config.LP_PEAK_MARGIN_WINDOW_DAYS)
    if as_of_day is None:
        as_of_day = datetime.now(UTC).astimezone(tz).date()
    first, last = as_of_day - timedelta(days=n), as_of_day - timedelta(days=1)
    cur_key = forecaster_key or current_forecaster_key()
    skip = excluded_days(first, tz)
    keys = day_forecaster_keys(first, last, tz)
    dbp = str(config.DB_PATH)
    if repair_yesterday:
        try:
            ensure_history(as_of_day, n, only_yesterday=True)
        except Exception:  # noqa: BLE001
            logger.warning("peak margin: yesterday repair failed", exc_info=True)

    cand: list[date] = []
    d = first
    while d <= last:
        if d.isoformat() not in skip and keys.get(d.isoformat(), LEGACY_KEY) == cur_key:
            cand.append(d)
        d += timedelta(days=1)

    now_m = time.monotonic()
    todo = [x for x in cand
            if (dbp, x.isoformat()) not in _day_cache
            and now_m - _day_miss.get((dbp, x.isoformat()), -1e9) > _NEG_TTL_S]
    if todo:
        bands = {x.isoformat(): peak_local_slots(x, tz) for x in todo}
        todo = [x for x in todo if bands[x.isoformat()]]
        base_by_day = _log_base_by_day(todo, bands, tz)
        try:
            meas = db.measured_residual_by_local_slot(todo[0], todo[-1]) if todo else {}
        except Exception:  # noqa: BLE001
            logger.warning("peak margin: measured residual failed", exc_info=True)
            meas = {}
        for x in todo:
            iso = x.isoformat()
            band = bands[iso]
            b_ = base_by_day.get(iso, {})
            m_ = meas.get(iso, {})
            if not all(k in b_ and k in m_ for k in band):
                _day_miss[(dbp, iso)] = now_m
                continue
            res = sum(m_[k][0] for k in band)
            hp = sum(m_[k][1] for k in band)
            base = sum(b_[k] for k in band)
            _day_cache[(dbp, iso)] = {
                "date": iso, "error_kwh": res - base, "residual_kwh": res,
                "base_kwh": base, "hp_kwh": hp, "n_slots": len(band)}
    return [dict(_day_cache[(dbp, x.isoformat())]) for x in cand if (dbp, x.isoformat()) in _day_cache]


def peak_band_margin(
    *,
    as_of_day: date | None = None,
    use_cache: bool = True,
    force: bool = False,
    compute: bool = True,
    forecaster_key: str | None = None,
) -> dict[str, Any]:
    """Current margin diagnostics (cached for an hour). With ``compute=False``
    only a cached value is returned (any age, ``fallback="not_computed"`` when
    there is none): the read endpoint uses it so a request never computes.

    Keys: enabled, margin_kwh, raw_kwh, coverage_oos_pct (rolling out-of-sample),
    n_oos, n_days (usable days), min_days, quantile, window_days, cap_kwh,
    fallback ("too_few_days"|"error"|"not_computed"|None), band_slots, as_of,
    forecaster_key.
    """
    tz = _tz()
    q = min(0.95, max(0.5, float(config.LP_PEAK_MARGIN_QUANTILE)))
    n = max(14, min(90, int(config.LP_PEAK_MARGIN_WINDOW_DAYS)))
    cap = float(config.LP_PEAK_MARGIN_MAX_KWH)
    day = as_of_day or datetime.now(UTC).astimezone(tz).date()
    key = forecaster_key or current_forecaster_key()
    ckey = (str(config.DB_PATH), day.isoformat(), q, n, cap, key)
    mono = time.monotonic()
    hit = _cache.get(ckey)
    if hit is not None and use_cache and not force and (not compute or mono - hit[0] < _CACHE_TTL_S):
        res = dict(hit[1])
        res["enabled"] = bool(config.LP_PEAK_MARGIN_ENABLED)   # read AFTER the cache
        return res
    if not compute:
        # newest cached value for the current settings, whatever its day
        cands = [v for k, v in _cache.items() if k[0] == ckey[0] and k[2:] == ckey[2:]]
        if cands:
            res = dict(max(cands, key=lambda v: v[0])[1])
            res["enabled"] = bool(config.LP_PEAK_MARGIN_ENABLED)
            return res
        return {"enabled": bool(config.LP_PEAK_MARGIN_ENABLED), "margin_kwh": 0.0, "raw_kwh": None,
                "coverage_oos_pct": None, "n_oos": 0, "n_days": 0, "min_days": MIN_USABLE_DAYS,
                "quantile": q, "window_days": n, "cap_kwh": cap, "fallback": "not_computed",
                "band_slots": 0, "as_of": day.isoformat(), "forecaster_key": key}
    try:
        hist = peak_band_error_history(day, n, tz=tz, forecaster_key=key)
        res = compute_margin([(h["date"], h["error_kwh"]) for h in hist], q, cap, n)
        band_slots = len(peak_local_slots(day, tz))
    except Exception:  # noqa: BLE001 - never break a solve; fall back to legacy
        logger.warning("peak margin: computation failed", exc_info=True)
        res = {"margin_kwh": 0.0, "raw_kwh": None, "coverage_oos_pct": None,
               "n_oos": 0, "n_days": 0, "fallback": "error"}
        band_slots = 0
    res.update({
        "quantile": q, "window_days": n, "cap_kwh": cap, "min_days": MIN_USABLE_DAYS,
        "band_slots": band_slots, "as_of": day.isoformat(), "forecaster_key": key,
    })
    for k in ("margin_kwh", "raw_kwh", "coverage_oos_pct"):
        if res.get(k) is not None:
            res[k] = round(float(res[k]), 3)
    if use_cache:
        if len(_cache) > 32:
            _cache.clear()
        _cache[ckey] = (mono, dict(res))
    res["enabled"] = bool(config.LP_PEAK_MARGIN_ENABLED)
    return res


def warm(as_of_day: date | None = None) -> dict[str, Any]:
    """Off-solve-path refresh (nightly, after the load_error_log rebuild):
    backfill missing log days over the whole window, then compute and cache the
    margin so the endpoint and the next solves are cheap. Best-effort."""
    tz = _tz()
    day = as_of_day or datetime.now(UTC).astimezone(tz).date()
    n = max(14, min(90, int(config.LP_PEAK_MARGIN_WINDOW_DAYS)))
    try:
        ensure_history(day, n)
    except Exception:  # noqa: BLE001
        logger.warning("peak margin warm: backfill failed", exc_info=True)
    return peak_band_margin(as_of_day=day, force=True)


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


def band_margin_groups(
    slot_starts_utc: list[datetime],
    prices: list[float],
    margin_kwh: float,
    *,
    band_slots: int = 0,
) -> list[tuple[list[int], float]] | None:
    """``[(band slot indices of one local day, margin kWh for that day)]`` for the
    pessimistic scenario, or ``None`` when there is nothing to add. A day whose
    band is only partly inside the horizon (mid-day replan) gets the margin
    scaled by ``slots_in_horizon / band_slots`` when the typical band length is
    known."""
    if margin_kwh <= 1e-9:
        return None
    out: list[tuple[list[int], float]] = []
    for g in peak_band_slot_groups(slot_starts_utc, prices):
        m = margin_kwh
        if band_slots and len(g) < band_slots:
            m = margin_kwh * len(g) / band_slots
        out.append((g, m))
    return out or None


def clear_cache() -> None:
    _cache.clear()
    _day_cache.clear()
    _day_miss.clear()
