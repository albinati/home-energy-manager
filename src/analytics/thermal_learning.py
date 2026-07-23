"""W2 thermal learner (#540) — building τ / UA / C from indoor sensor data.

The house's thermal constants have never been measured from the inside:
``BUILDING_UA_W_PER_K=600`` came from a 120-day HDD regression against an
ASSUMED base temperature (docs/WINTER_THERMAL_MODEL.md §2.1) and
``BUILDING_THERMAL_MASS_KWH_PER_K=12`` is a placeholder. Once the user's room
sensors push into ``room_temperature_history`` (W1a, #572), this module learns:

* **τ (hours)** — from unheated overnight decay episodes,
  ``T_in(t) = T_out + (T_in(0) − T_out)·e^(−t/τ)``. COP-free, measurable in
  any season with a cool night (needs ΔT(in−out) ≥ ~5 °C).
* **UA (W/K)** — the §2.1 HDD regression re-fit with MEASURED indoor daily
  means as the base temperature instead of an assumed 15.5 °C. Needs
  heating-season HDD spread → its gate reports ``skipped`` until winter,
  by design.
* **C = τ·UA (kWh/K)** — with the UA source flagged (learned vs env).

Results persist to the single-row ``building_thermal_calibration`` table
(clone of ``daikin_lwt_kw_calibration``), quality-gated: R², episode/sample
counts, physical bounds. Graceful no-op while the sensor table is empty —
every consumer falls back to the env constants through the bounded readers
at the bottom of this module.

Decay-episode DECONTAMINATION (the parts the physics can't forgive — several
added by adversarial review):

* any overlapping 2h bucket with ``kwh_heating`` above a floor → the decay
  isn't natural. Activity BLOCKS only its own span plus a
  ``THERMAL_TAU_SETTLE_HOURS`` tail (radiators and the hydronic loop keep
  emitting after the compressor stops — owner-flagged inertia); the CLEAN
  stretch BEFORE a mid-night heating burst is kept and fit separately, so
  winter's pre-dawn cheap-slot warmups don't starve the learner of the
  22:00-04:00 decay it needs most;
* HEM-commanded LWT offset windows (``get_nonzero_lwt_offset_windows``) —
  the k_per_degc lesson: never learn from your own echo;
* **room-composition changes split the episode**: the house mean jumps when a
  sensor dies or joins mid-night, faking a decay/rise that R² cannot catch
  (verified ±25% τ error in review) — the resampler tracks the room set per
  bin and the selector treats a composition change like a gap;
* DHW-only activity is KEPT (separate circuit; the tank sits in the utility
  space) except heavy boosts (``kwh_dhw`` above a threshold — a 60 °C
  negative-price boost leaks real heat into the envelope).

Outdoor-temperature honesty (review M1/M2): the series is the freshest
forecast per slot CORRECTED by the learned per-hour microclimate offset
(``get_micro_climate_offset_by_hour_c`` — the same sensor-vs-forecast
residual the LP uses), and the fit uses PER-POINT outdoor temps rather than
the episode mean (a falling night — the common case — biased τ high ~10%
with a mean; systematic, so the median never washed it out).

The fitters are PURE functions (data in → fit out) so tests drive them with
synthetic decay curves of known τ — the whole point of shipping this before
the sensors exist: when readings arrive, the learner just starts working.

LP wiring is deliberately NOT here — that's W3 (t_in restore + comfort band +
gentle-recovery cap). This module only feeds the estimator and the readers.
"""
from __future__ import annotations

import logging
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure data shapes
# ---------------------------------------------------------------------------


@dataclass
class DecayEpisode:
    start_utc: datetime
    end_utc: datetime
    # (hours since episode start, house mean temp °C, outdoor °C at that
    # point) — per-point outdoor, not the episode mean (review M2)
    points: list[tuple[float, float, float]]
    t_out_mean_c: float
    t_in_start_c: float


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _resample_house_mean(
    readings: list[dict[str, Any]], *, bin_minutes: int = 30
) -> list[tuple[datetime, float, frozenset[str]]]:
    """Collapse multi-room readings into one house series: (bin centre, mean,
    ROOM SET) per time bin.

    Mirrors ``get_latest_indoor_reading``'s mean-across-rooms semantics
    (per-room learning is future work). The room set is load-bearing (review
    H2): a sensor dying or joining mid-night shifts the mean by the rooms'
    temperature spread — a fake decay/rise the fit gates can't see — so the
    selector splits episodes whenever the composition changes.

    Bin width 30 min (round-2 verification): with 10-min bins, two rooms on
    MIXED report cadences (10 vs 15 min) flickered the composition every
    other bin and shattered a clean 9 h night into zero usable episodes.
    30 min absorbs any cadence ≥ one report per half hour; τ ≈ 20 h loses
    nothing at that resolution.
    """
    bins: dict[int, dict[str, list[float]]] = {}
    for r in readings:
        try:
            ts = datetime.fromisoformat(str(r["captured_at"]).replace("Z", "+00:00"))
            t = float(r["temp_c"])
        except (ValueError, TypeError, KeyError):
            continue
        room = str(r.get("room") or "home")
        key = int(ts.timestamp()) // (bin_minutes * 60)
        bins.setdefault(key, {}).setdefault(room, []).append(t)
    out: list[tuple[datetime, float, frozenset[str]]] = []
    for key in sorted(bins):
        centre = datetime.fromtimestamp(key * bin_minutes * 60 + bin_minutes * 30, tz=UTC)
        per_room = bins[key]
        room_means = [sum(v) / len(v) for v in per_room.values()]
        out.append((centre, sum(room_means) / len(room_means), frozenset(per_room)))
    return out


def _bucket_window_utc(day_local: date, bucket_idx: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC span of LOCAL 2h bucket ``bucket_idx`` on ``day_local``."""
    start = datetime.combine(day_local, time(bucket_idx * 2, 0), tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(hours=2)).astimezone(UTC)


def sanitize_phantom_heating(
    consumption_rows: list[dict[str, Any]],
    outdoor_series: list[tuple[datetime, float]],
    tz: ZoneInfo,
    *,
    max_plausible_kwh: float = 0.15,
) -> tuple[list[dict[str, Any]], int]:
    """Drop #749-family phantom heating claims from consumption rows (#760).

    Onecta's ``kwh_heating`` counter is whole-kWh quantised and misattributes
    (issue #749): summer prod carries ``source=onecta_cache, kwh_heating=1.0``
    buckets at arbitrary night hours, and every one of them blocked a decay
    episode — 21 days of clean two-sensor coverage yielded ZERO usable τ
    episodes. The rows survive telemetry reconciliation by design (a ≥0.5 kWh
    disagreement keeps Onecta), and ``mode`` telemetry can't corroborate (the
    unit reports ``heating`` year-round).

    A bucket is zeroed only when BOTH hold:

    * the claim is EXACTLY 1.0 kWh — the single-quantum #749 signature.
      Claims ≥ 2 are never touched: adversarial review showed the weather
      curve model is hard-capped (LWT clamp → ~2.1 kWh/bucket) and, thanks to
      the reconciliation rule that keeps Onecta on ≥0.5 kWh disagreement, the
      surviving integer rows are exactly the ones where the curve model
      already lost against the meter — it must not arbitrate real cold-snap
      energy (defrost, recovery ramps) out of existence;
    * the site's own weather curve says the compressor was essentially OFF:
      ``get_daikin_heating_kw(bucket outdoor mean) × 2 h < max_plausible_kwh``
      (default 0.15 — under the prod curve that means outdoor ≳ 14.5 °C).
      In shoulder/winter conditions the curve yields ≥ 0.35 kWh/bucket, so a
      1.0 claim there keeps blocking — the honest conservative outcome.

    Deliberately narrow: only ``onecta_cache`` rows are candidates
    (``telemetry_integral`` rows are decimals by construction); ``kwh_dhw``
    is left untouched (DHW runs in any season and its 0.8 kWh contamination
    floor already absorbs quantisation); HEM-commanded LWT boosts live in
    offset windows, which block unconditionally — so the plausibility check
    uses the BASE curve (``lwt_offset_delta=0``). No outdoor coverage for the
    bucket → keep the row (conservative). NB a cooling-capable site would see
    real cooling folded into ``kwh_heating`` (client.py quirk) and zeroed —
    this installation is heating-only. Returns ``(rows, n_zeroed)`` without
    mutating input.
    """
    from ..physics import get_daikin_heating_kw

    out: list[dict[str, Any]] = []
    n_zeroed = 0
    outdoor_series = sorted(outdoor_series)
    for r in consumption_rows:
        heating = float(r.get("kwh_heating") or 0.0)
        if str(r.get("source")) != "onecta_cache" or heating != 1.0:
            out.append(r)
            continue
        try:
            d = date.fromisoformat(str(r["date"]))
            b = int(r["bucket_idx"])
        except (ValueError, TypeError, KeyError):
            out.append(r)
            continue
        ws, we = _bucket_window_utc(d, b, tz)
        t_out = _mean_outdoor_in_span(outdoor_series, ws, we)
        if t_out is None:
            out.append(r)  # no outdoor data — can't judge, keep blocking
            continue
        plausible_kwh = float(get_daikin_heating_kw(float(t_out))) * 2.0
        if plausible_kwh >= max_plausible_kwh:
            out.append(r)
            continue
        cleaned = dict(r)
        cleaned["kwh_heating"] = 0.0
        out.append(cleaned)
        n_zeroed += 1
    if n_zeroed:
        logger.info(
            "thermal_learning: zeroed %d phantom onecta_cache kwh_heating "
            "bucket(s) implausible under the weather curve (#760)",
            n_zeroed,
        )
    return out, n_zeroed


def _blocked_intervals(
    consumption_rows: list[dict[str, Any]],
    offset_windows: list[tuple[str, str]],
    tz: ZoneInfo,
    *,
    heating_contam_kwh: float,
    dhw_contam_kwh: float,
    settle_hours: float,
) -> list[tuple[datetime, datetime]]:
    """UTC spans during which (or shortly after which) the heat pump was
    putting heat into the envelope. Each activity window blocks its own span
    PLUS ``settle_hours`` of tail — emitters keep radiating after the
    compressor stops. Time BEFORE an activity window is clean by definition
    (the decay was natural up to the moment heating started), which is what
    lets the selector keep the pre-warmup stretch of a winter night (review
    M3: the first cut rejected the whole night when heating ran at 04:00,
    starving τ of exactly the deep-winter decay it exists to measure)."""
    windows: list[tuple[datetime, datetime]] = []
    tail = timedelta(hours=settle_hours)
    for r in consumption_rows:
        try:
            d = date.fromisoformat(str(r["date"]))
            b = int(r["bucket_idx"])
        except (ValueError, TypeError, KeyError):
            continue
        heating = float(r.get("kwh_heating") or 0.0)
        dhw = float(r.get("kwh_dhw") or 0.0)
        if heating > heating_contam_kwh or dhw > dhw_contam_kwh:
            ws, we = _bucket_window_utc(d, b, tz)
            windows.append((ws, we + tail))
    for s, e in offset_windows:
        try:
            ws = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            we = datetime.fromisoformat(str(e).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        windows.append((ws, we + tail))
    return sorted(windows)


def _in_blocked(ts: datetime, blocked: list[tuple[datetime, datetime]]) -> bool:
    return any(ws <= ts < we for ws, we in blocked)


def _outdoor_at(
    outdoor_series: list[tuple[datetime, float]],
    outdoor_times: list[datetime],
    ts: datetime,
    fallback: float,
    *,
    max_gap_hours: float = 2.0,
) -> float:
    """Nearest outdoor sample to ``ts`` (within ``max_gap_hours``), else the
    fallback (episode mean). ``outdoor_times`` = pre-extracted sorted keys."""
    if not outdoor_series:
        return fallback
    i = bisect_left(outdoor_times, ts)
    best: tuple[float, float] | None = None
    for j in (i - 1, i):
        if 0 <= j < len(outdoor_series):
            dt_h = abs((outdoor_series[j][0] - ts).total_seconds()) / 3600.0
            if best is None or dt_h < best[0]:
                best = (dt_h, outdoor_series[j][1])
    if best is None or best[0] > max_gap_hours:
        return fallback
    return best[1]


def _mean_outdoor_in_span(
    outdoor_series: list[tuple[datetime, float]], start: datetime, end: datetime
) -> float | None:
    vals = [v for ts, v in outdoor_series if start <= ts < end]
    if not vals:
        return None
    return sum(vals) / len(vals)


def select_decay_episodes(
    readings: list[dict[str, Any]],
    consumption_rows: list[dict[str, Any]],
    offset_windows: list[tuple[str, str]],
    outdoor_series: list[tuple[datetime, float]],
    *,
    tz: ZoneInfo,
    night_start_hour_local: int = 21,
    night_end_hour_local: int = 8,
    min_episode_hours: float = 4.0,
    min_points: int = 8,
    max_gap_minutes: float = 45.0,
    settle_hours: float = 2.0,
    min_delta_t_c: float = 5.0,
    max_rise_c: float = 0.3,
    heating_contam_kwh: float = 0.1,
    dhw_contam_kwh: float = 0.8,
    bin_minutes: int = 30,
) -> list[DecayEpisode]:
    """PURE selector: overnight, gap-free, composition-stable, heating-free
    (with settle tail), cool-enough decay segments from raw sensor readings.
    All data passed in — no DB."""
    series = _resample_house_mean(readings, bin_minutes=bin_minutes)
    if not series:
        return []
    blocked = _blocked_intervals(
        consumption_rows, offset_windows, tz,
        heating_contam_kwh=heating_contam_kwh, dhw_contam_kwh=dhw_contam_kwh,
        settle_hours=settle_hours,
    )
    outdoor_series = sorted(outdoor_series)
    outdoor_times = [ts for ts, _ in outdoor_series]

    def _in_night(ts: datetime) -> bool:
        h = ts.astimezone(tz).hour
        if night_start_hour_local > night_end_hour_local:  # wraps midnight
            return h >= night_start_hour_local or h < night_end_hour_local
        return night_start_hour_local <= h < night_end_hour_local

    # Split into candidate segments. A new segment starts on: leaving the
    # night window, a data gap, an activity-blocked stretch (heating + settle
    # tail — the CLEAN run before a mid-night warmup survives as its own
    # segment), or a room-composition change (H2).
    segments: list[list[tuple[datetime, float]]] = []
    cur: list[tuple[datetime, float]] = []
    prev_rooms: frozenset[str] | None = None
    for ts, v, rooms in series:
        if not _in_night(ts) or _in_blocked(ts, blocked):
            if cur:
                segments.append(cur)
                cur = []
            prev_rooms = None
            continue
        if cur and (
            (ts - cur[-1][0]) > timedelta(minutes=max_gap_minutes)
            or (prev_rooms is not None and rooms != prev_rooms)
        ):
            segments.append(cur)
            cur = []
        cur.append((ts, v))
        prev_rooms = rooms
    if cur:
        segments.append(cur)

    episodes: list[DecayEpisode] = []
    for seg in segments:
        if len(seg) < 2:
            continue
        start, end = seg[0][0], seg[-1][0]
        # length / density gates
        dur_h = (end - start).total_seconds() / 3600.0
        if dur_h < min_episode_hours or len(seg) < min_points:
            continue
        # physics gates
        t_out = _mean_outdoor_in_span(outdoor_series, start, end)
        if t_out is None:
            continue
        t0 = seg[0][1]
        if (t0 - t_out) < min_delta_t_c:
            continue  # τ unidentifiable — the honest warm-season limitation
        if max(v for _, v in seg) > t0 + max_rise_c:
            continue  # temp rose — window opened / gains / unlogged heating
        episodes.append(DecayEpisode(
            start_utc=start,
            end_utc=end,
            points=[
                (
                    (ts - start).total_seconds() / 3600.0,
                    v,
                    _outdoor_at(outdoor_series, outdoor_times, ts, t_out),
                )
                for ts, v in seg
            ],
            t_out_mean_c=float(t_out),
            t_in_start_c=float(t0),
        ))
    return episodes


def fit_tau_for_episode(ep: DecayEpisode) -> tuple[float, float] | None:
    """PURE fit of one episode via the INTEGRAL form of the RC equation:

        T_0 − T_i = (1/τ) · ∫₀^{t_i} (T(s) − T_out(s)) ds

    (trapezoidal integral over the measured points). Exact for a
    time-varying outdoor temperature — review M2 showed the constant-ambient
    log-linearization biases τ ~+10% on an ordinary falling night, and the
    per-point ratio variant breaks outright when T_out falls faster than the
    house (the ratio exceeds 1). For constant T_out this reduces to the
    exponential solution exactly. Least squares through the origin on
    ``Y = X/τ``; R² on that regression. Returns ``(tau_hours, r_squared)``
    or None when the episode degenerates."""
    if len(ep.points) < 3:
        return None
    t0 = ep.points[0][1]
    if (t0 - ep.points[0][2]) <= 0.5:
        return None
    # X_i = ∫ (T − T_out) ds  (trapezoid, °C·h);  Y_i = T_0 − T_i  (°C)
    pts: list[tuple[float, float]] = []
    x = 0.0
    prev_t, prev_gap = ep.points[0][0], ep.points[0][1] - ep.points[0][2]
    for t_h, temp, tout in ep.points[1:]:
        gap = temp - tout
        x += 0.5 * (prev_gap + gap) * (t_h - prev_t)
        prev_t, prev_gap = t_h, gap
        if x <= 0:
            continue  # house at/below outdoor — no signal in this stretch
        pts.append((x, t0 - temp))
    if len(pts) < 3:
        return None
    sxx = sum(xv * xv for xv, _ in pts)
    sxy = sum(xv * yv for xv, yv in pts)
    if sxx <= 0 or sxy <= 0:  # non-positive slope → not a decay
        return None
    slope = sxy / sxx  # = 1/τ
    tau_h = 1.0 / slope
    y_mean = sum(yv for _, yv in pts) / len(pts)
    ss_tot = sum((yv - y_mean) ** 2 for _, yv in pts)
    ss_res = sum((yv - slope * xv) ** 2 for xv, yv in pts)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return tau_h, r2


def fit_tau(
    episodes: list[DecayEpisode],
    *,
    min_episodes: int = 5,
    min_r2: float = 0.8,
    min_tau_hours: float = 5.0,
    max_tau_hours: float = 100.0,
) -> dict[str, Any]:
    """Aggregate per-episode fits into one τ. Median over quality-passing
    episodes; ``status='skipped'`` (never raises) below the episode gate —
    the graceful no-op that makes this mergeable before the sensors exist."""
    fits: list[tuple[float, float]] = []
    rejected = 0
    for ep in episodes:
        fit = fit_tau_for_episode(ep)
        if fit is None:
            rejected += 1
            continue
        tau_h, r2 = fit
        if r2 < min_r2 or not (min_tau_hours <= tau_h <= max_tau_hours):
            rejected += 1
            continue
        fits.append((tau_h, r2))
    if len(fits) < min_episodes:
        return {
            "status": "skipped",
            "reason": f"only {len(fits)} quality decay episode(s); need >= {min_episodes}",
            "episodes": len(fits),
            "episodes_rejected": rejected,
        }
    taus = sorted(t for t, _ in fits)
    r2s = sorted(r for _, r in fits)
    return {
        "status": "ok",
        "tau_hours": float(taus[len(taus) // 2]),
        "r2_median": float(r2s[len(r2s) // 2]),
        "episodes": len(fits),
        "episodes_rejected": rejected,
    }


def fit_ua_hdd(
    daily_rows: list[tuple[float, float, float]],
    *,
    assumed_cop: float = 3.0,
    min_days: int = 20,
    min_r2: float = 0.5,
    min_hdd: float = 1.0,
) -> dict[str, Any]:
    """PURE §2.1 HDD regression with MEASURED indoor as the base temperature.

    ``daily_rows``: ``(load_kwh_electric, indoor_mean_c, outdoor_mean_c)`` per
    day. ``HDD_day = max(0, indoor − outdoor)`` (°C·day); linear fit
    ``load = a + b·HDD``; ``UA[W/K] = b · COP / 24 · 1000``. Reports
    ``skipped`` until enough heating-season days exist — by design this
    learner stays quiet through summer. Honest caveats live with the result:
    load is whole-house electric (occupancy correlates with cold, inflating
    the slope beyond what the intercept absorbs) and UA scales linearly with
    the assumed COP — both recorded in the row (``ua_assumed_cop``), both
    reasons the consumers stay advisory until W4's replay validation.
    """
    pts = [
        (max(0.0, t_in - t_out), load)
        for load, t_in, t_out in daily_rows
        if load is not None and load > 0
    ]
    pts = [(h, y) for h, y in pts if h > min_hdd]
    if len(pts) < min_days:
        return {
            "status": "skipped",
            "reason": f"only {len(pts)} day(s) with HDD > {min_hdd}; need >= {min_days}",
            "samples": len(pts),
        }
    n = len(pts)
    mx = sum(h for h, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((h - mx) ** 2 for h, _ in pts)
    sxy = sum((h - mx) * (y - my) for h, y in pts)
    if sxx <= 0:
        return {"status": "skipped", "reason": "zero HDD variance", "samples": n}
    slope = sxy / sxx  # kWh-electric / (°C·day)
    if slope <= 0:
        return {"status": "skipped", "reason": "non-positive HDD slope", "samples": n}
    ss_tot = sum((y - my) ** 2 for _, y in pts)
    ss_res = sum((y - (my + slope * (h - mx))) ** 2 for h, y in pts)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    if r2 < min_r2:
        return {
            "status": "skipped",
            "reason": f"R²={r2:.2f} below {min_r2}",
            "samples": n,
            "r2": float(r2),
        }
    ua_w_per_k = slope * assumed_cop / 24.0 * 1000.0
    return {
        "status": "ok",
        "ua_w_per_k": float(ua_w_per_k),
        "slope_kwh_per_hdd": float(slope),
        "r2": float(r2),
        "samples": n,
        "assumed_cop": float(assumed_cop),
    }


# ---------------------------------------------------------------------------
# Orchestration (thin, best-effort, never raises to the cron)
# ---------------------------------------------------------------------------


def refresh_building_thermal_calibration() -> dict[str, Any]:
    """Gather data, run both fits, merge into the single-row calibration.

    Merge semantics: a fit that reports ``skipped`` PRESERVES the previous
    row's component (a bad-weather week must not erase a good τ). ``C = τ·UA``
    is recomputed from the best available pair, source-flagged. Quiet when
    nothing changes; never raises.
    """
    from .. import db

    def _done(result: dict[str, Any]) -> dict[str, Any]:
        """Persist the run summary for the W2 progress UI, then return it."""
        try:
            db.set_thermal_learning_progress(result)
        except Exception:  # pragma: no cover — observability must never break the cron
            logger.debug("thermal_learning: progress persist failed", exc_info=True)
        return result

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now = datetime.now(UTC)
    tau_window = int(getattr(config, "THERMAL_TAU_WINDOW_DAYS", 21))
    ua_window = int(getattr(config, "THERMAL_UA_WINDOW_DAYS", 30))

    start = now - timedelta(days=max(tau_window, ua_window))
    try:
        readings = db.get_indoor_readings_range(
            start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("thermal_learning: indoor read failed")
        return _done({"status": "error", "reason": "indoor read failed"})
    if not readings:
        # The pre-sensor steady state — one quiet skip, no table writes.
        return _done({"status": "skipped", "reason": "no indoor sensor data yet"})

    # Outdoor series over the FULL learner window (review H1: the first cut
    # covered only the τ window, silently starving the UA fit and recording
    # false provenance), one range query, microclimate-corrected.
    start_day = (now - timedelta(days=max(tau_window, ua_window))).date()
    end_day = now.date()
    outdoor = _outdoor_series(start_day, end_day)

    tau_start_day = (now - timedelta(days=tau_window)).date()
    try:
        consumption = db.get_daikin_consumption_2hourly_range(
            tau_start_day.isoformat(), end_day.isoformat()
        )
    except Exception:
        consumption = []
    try:
        offsets = db.get_nonzero_lwt_offset_windows(
            tau_start_day.isoformat(), end_day.isoformat()
        )
    except Exception:
        offsets = []

    n_phantom = 0
    if getattr(config, "THERMAL_PHANTOM_HEATING_GUARD_ENABLED", True):
        consumption, n_phantom = sanitize_phantom_heating(
            consumption, outdoor, tz,
            max_plausible_kwh=float(
                getattr(config, "THERMAL_PHANTOM_MAX_PLAUSIBLE_KWH", 0.15)
            ),
        )

    tau_cutoff = (now - timedelta(days=tau_window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tau_readings = [r for r in readings if str(r.get("captured_at", "")) >= tau_cutoff]
    episodes = select_decay_episodes(
        tau_readings, consumption, offsets, outdoor,
        tz=tz,
        night_start_hour_local=int(getattr(config, "THERMAL_TAU_NIGHT_START_HOUR_LOCAL", 21)),
        night_end_hour_local=int(getattr(config, "THERMAL_TAU_NIGHT_END_HOUR_LOCAL", 8)),
        min_episode_hours=float(getattr(config, "THERMAL_TAU_MIN_EPISODE_HOURS", 4.0)),
        settle_hours=float(getattr(config, "THERMAL_TAU_SETTLE_HOURS", 2.0)),
        min_delta_t_c=float(getattr(config, "THERMAL_TAU_MIN_DELTA_T_C", 5.0)),
        heating_contam_kwh=float(getattr(config, "THERMAL_HEATING_CONTAM_KWH", 0.1)),
        dhw_contam_kwh=float(getattr(config, "THERMAL_DHW_CONTAM_KWH", 0.8)),
    )
    tau_fit = fit_tau(
        episodes,
        min_episodes=int(getattr(config, "THERMAL_TAU_MIN_EPISODES", 5)),
        min_r2=float(getattr(config, "THERMAL_TAU_MIN_R2", 0.8)),
        min_tau_hours=float(getattr(config, "THERMAL_TAU_MIN_HOURS", 5.0)),
        max_tau_hours=float(getattr(config, "THERMAL_TAU_MAX_HOURS", 100.0)),
    )
    tau_fit["phantom_heating_zeroed"] = n_phantom

    ua_fit = _ua_fit_from_db(readings, outdoor, now, ua_window)

    prev = None
    try:
        prev = db.get_building_thermal_calibration()
    except Exception:  # pragma: no cover
        pass
    if tau_fit.get("status") != "ok" and ua_fit.get("status") != "ok":
        if prev is None:
            logger.info(
                "thermal_learning: skipped (tau: %s; ua: %s) — readers use env "
                "constants until enough clean data accumulates",
                tau_fit.get("reason"), ua_fit.get("reason"),
            )
        return _done({"status": "skipped", "tau": tau_fit, "ua": ua_fit})

    # Merge with the previous row: skipped components keep their prior values.
    row: dict[str, Any] = dict(prev or {})
    now_iso = now.isoformat()
    if tau_fit.get("status") == "ok":
        row.update(
            tau_hours=float(tau_fit["tau_hours"]),
            tau_r2_median=float(tau_fit["r2_median"]),
            tau_episodes=int(tau_fit["episodes"]),
            tau_window_days=tau_window,
            tau_computed_at=now_iso,
        )
    if ua_fit.get("status") == "ok":
        row.update(
            ua_w_per_k=float(ua_fit["ua_w_per_k"]),
            ua_r2=float(ua_fit["r2"]),
            ua_samples=int(ua_fit["samples"]),
            ua_window_days=ua_window,
            ua_assumed_cop=float(ua_fit["assumed_cop"]),
            ua_source="hdd_regression",
            ua_computed_at=now_iso,
        )
    tau_h = row.get("tau_hours")
    if tau_h is not None:
        if row.get("ua_w_per_k") is not None:
            ua_for_c = float(row["ua_w_per_k"])
            row["c_source"] = "tau_x_learned_ua"
        else:
            ua_for_c = float(config.BUILDING_UA_W_PER_K)
            row["c_source"] = "tau_x_env_ua"
        row["c_kwh_per_k"] = float(tau_h) * ua_for_c / 1000.0
    try:
        db.upsert_building_thermal_calibration(row)
    except Exception:  # pragma: no cover
        logger.exception("thermal_learning: upsert failed")
        return _done({"status": "error", "reason": "upsert failed"})
    logger.info(
        "thermal_learning: tau=%s h (eps=%s r2=%s) ua=%s W/K (src=%s) c=%s kWh/K",
        _fmt(row.get("tau_hours")), row.get("tau_episodes"),
        _fmt(row.get("tau_r2_median")), _fmt(row.get("ua_w_per_k")),
        row.get("ua_source") or "env", _fmt(row.get("c_kwh_per_k")),
    )
    return _done({"status": "ok", "tau": tau_fit, "ua": ua_fit, "row": row})


def _fmt(v: Any) -> str:
    return f"{float(v):.1f}" if v is not None else "-"


def _outdoor_series(start_day: date, end_day: date) -> list[tuple[datetime, float]]:
    """Freshest outdoor temp per slot over the WHOLE range (one range query —
    review H1/L3: the per-day correlated-subquery loop cost ~0.4 s × N days
    under the global DB lock), corrected by the learned per-UTC-hour
    microclimate offset (review M1: raw forecast temps carry a systematic
    night residual; at ΔT near the gate, ±1 °C of outdoor bias is ∓20% of τ).
    Coverage is bounded by METEO_FORECAST_HISTORY_RETENTION_DAYS (~30) —
    THERMAL_UA_WINDOW_DAYS defaults accordingly."""
    from .. import db

    try:
        rows = db.get_meteo_temps_range(start_day.isoformat(), end_day.isoformat())
    except Exception:  # pragma: no cover - defensive
        logger.debug("thermal_learning: meteo range read failed", exc_info=True)
        return []
    try:
        micro = db.get_micro_climate_offset_by_hour_c()
    except Exception:  # noqa: BLE001 — offset is a refinement, not a dependency
        micro = {}
    out: list[tuple[datetime, float]] = []
    for slot_iso, temp in rows:
        try:
            ts = datetime.fromisoformat(str(slot_iso).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        # Clamp the offset: a badly-seeded skill-log day must not be able to
        # push ΔT across the episode gate systematically (round-2 hardening).
        off = max(-3.0, min(3.0, float(micro.get(ts.hour, 0.0))))
        out.append((ts, float(temp) + off))
    return sorted(out)


def _ua_fit_from_db(
    readings: list[dict[str, Any]],
    outdoor: list[tuple[datetime, float]],
    now: datetime,
    ua_window: int,
) -> dict[str, Any]:
    """Assemble (load, indoor_mean, outdoor_mean) day rows and run the pure
    UA fit. A day needs Fox load, outdoor coverage, and ≥ 12 indoor readings
    SPREAD across ≥ 4 distinct hours (review M4: "≥12 readings" alone is
    satisfiable by 2 h of one morning — an unrepresentative mean paired with
    a full-day load is a leverage point)."""
    from .. import db

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    start_day = (now - timedelta(days=ua_window)).date()
    end_day = now.date() - timedelta(days=1)
    try:
        fox = db.get_fox_energy_daily_range(start_day.isoformat(), end_day.isoformat())
    except Exception:
        return {"status": "skipped", "reason": "fox daily read failed", "samples": 0}

    indoor_by_day: dict[str, list[tuple[int, float]]] = {}
    for r in readings:
        try:
            ts = datetime.fromisoformat(str(r["captured_at"]).replace("Z", "+00:00"))
            loc = ts.astimezone(tz)
            indoor_by_day.setdefault(loc.date().isoformat(), []).append(
                (loc.hour, float(r["temp_c"]))
            )
        except (ValueError, TypeError, KeyError):
            continue
    outdoor_by_day: dict[str, list[float]] = {}
    for ts, v in outdoor:
        outdoor_by_day.setdefault(ts.astimezone(tz).date().isoformat(), []).append(v)

    daily_rows: list[tuple[float, float, float]] = []
    for row in fox:
        d = str(row.get("date"))
        load = row.get("load_kwh")
        ins = indoor_by_day.get(d) or []
        outs = outdoor_by_day.get(d) or []
        hours_covered = {h for h, _ in ins}
        if load is None or len(ins) < 12 or len(hours_covered) < 4 or not outs:
            continue
        daily_rows.append((
            float(load),
            sum(t for _, t in ins) / len(ins),
            sum(outs) / len(outs),
        ))
    return fit_ua_hdd(
        daily_rows,
        assumed_cop=float(getattr(config, "THERMAL_UA_ASSUMED_COP", 3.0)),
        min_days=int(getattr(config, "THERMAL_UA_MIN_HDD_DAYS", 20)),
        min_r2=float(getattr(config, "THERMAL_UA_MIN_R2", 0.5)),
    )


# ---------------------------------------------------------------------------
# Bounded readers (env fallback) — the ONLY surface consumers touch
# ---------------------------------------------------------------------------

_TAU_BOUNDS = (5.0, 100.0)
_UA_BOUNDS = (100.0, 1500.0)
_C_BOUNDS = (3.0, 60.0)


def _calibration_row() -> dict[str, Any] | None:
    if not bool(getattr(config, "THERMAL_LEARNED_VALUES_ENABLED", True)):
        return None
    from .. import db
    try:
        return db.get_building_thermal_calibration()
    except Exception:  # noqa: BLE001 — calibration must never break a consumer
        return None


def get_building_ua_w_per_k() -> float:
    """Learned UA when present + in bounds; env constant otherwise."""
    fallback = float(config.BUILDING_UA_W_PER_K)
    row = _calibration_row()
    if row is None or row.get("ua_w_per_k") is None:
        return fallback
    ua = float(row["ua_w_per_k"])
    return ua if _UA_BOUNDS[0] <= ua <= _UA_BOUNDS[1] else fallback


def get_building_thermal_mass_kwh_per_k() -> float:
    """Learned C = τ·UA when present + in bounds; env constant otherwise."""
    fallback = float(config.BUILDING_THERMAL_MASS_KWH_PER_K)
    row = _calibration_row()
    if row is None or row.get("c_kwh_per_k") is None:
        return fallback
    c = float(row["c_kwh_per_k"])
    return c if _C_BOUNDS[0] <= c <= _C_BOUNDS[1] else fallback


def get_building_tau_hours() -> float:
    """Learned τ when present + in bounds; env-derived C/UA otherwise."""
    fallback = (
        float(config.BUILDING_THERMAL_MASS_KWH_PER_K) * 1000.0
        / max(1e-9, float(config.BUILDING_UA_W_PER_K))
    )
    row = _calibration_row()
    if row is None or row.get("tau_hours") is None:
        return fallback
    tau = float(row["tau_hours"])
    return tau if _TAU_BOUNDS[0] <= tau <= _TAU_BOUNDS[1] else fallback
