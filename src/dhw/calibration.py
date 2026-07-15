"""Learn the tank's parameters from the thermometer. Nothing else.

This module reads ``daikin_telemetry.tank_temp_c`` (live rows), ``room_temperature_history``
and the weather archive. It does NOT read the energy counter — that is enforced by
``test_no_broken_instrument.py``, and it is the single rule that keeps this rewrite
honest. The counter is quantised to whole kWh and half-synthesised from the tank
temperature itself; a fit against it returns its own assumptions (#719).

So the calibration is deliberately modest. It learns exactly two things the
thermometer can actually see, and it refuses the rest:

* **UA and the tank's effective ambient, JOINTLY.** The cooling ODE is
  ``dT/dt = −(UA/C)(T − A)``. Integrating gives a two-parameter linear form,
  ``T0 − Ti = (UA/C)·∫T ds − (UA/C)·A·ti``, so a least-squares fit over an unheated
  coast recovers BOTH ``UA/C`` and ``A`` with no prior on either. This matters: the
  first attempt assumed the ambient (the living-room setpoint, 21 °C) while the
  cupboard's effective ambient is ~22 °C and the house was at 30 during a heatwave —
  and got UA wrong by half. Fit the ambient; don't assume it.

* **The draw events, for OBSERVABILITY ONLY.** A fall steeper than standing loss can
  explain is a draw — that much the thermometer sees cleanly, free of COP and
  quantisation. But only WHEN, not how much: turning a temperature drop into litres
  needs the mixer state and the reheat that overlapped it, neither of which is
  measurable here. So the events feed a report ("draws detected at 20:00-21:00,
  confirming the household's schedule"), never the LP. If they ever show a morning
  peak, it is the household's declaration that is wrong, not the model — but that is
  a conversation, not an input.

What it will NOT learn, and why, so the temptation doesn't come back:

* **COP.** Not measurable here (see above). The certified EN 16147 curve in
  :mod:`src.dhw.model` is third-party test data and the daily cross-check closes to
  the right order. A fit from eight noisy events would be worse.
* **Draw magnitude in kWh.** Needs a flow meter or a COP. Declared, not learned.
* **Heat-up rate vs outdoor temperature.** Only observable in winter, and even then
  the binding constraint in the LP is the ELECTRICAL cap, not the thermal one — so
  its absence today simply does not matter for v1. Measured as a sanity check that
  logs a warning if it ever contradicts the databook; never auto-applied.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .model import TankParams

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure data shapes
# ---------------------------------------------------------------------------


@dataclass
class CoastEpisode:
    """An unheated stretch of tank cooling, with the outdoor/indoor context needed
    to place its ambient. ``points`` = (hours since start, tank °C)."""

    start_utc: datetime
    end_utc: datetime
    points: list[tuple[float, float]]
    indoor_mean_c: float | None


@dataclass
class DrawEvent:
    """A detected hot-water draw — WHEN and how deep, never how many kWh."""

    at_utc: datetime
    drop_c: float
    from_c: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_series(rows: list[tuple[float, float]]) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    seen: set[float] = set()
    for epoch, temp in rows:
        try:
            e = float(epoch)
        except (TypeError, ValueError):
            continue
        if e in seen:
            continue
        seen.add(e)
        out.append((datetime.fromtimestamp(e, tz=UTC), float(temp)))
    return sorted(out)


def _bucket_window_utc(day_local: date, bucket_idx: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day_local, time(bucket_idx * 2, 0), tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(hours=2)).astimezone(UTC)


# ---------------------------------------------------------------------------
# Episode selection (pure)
# ---------------------------------------------------------------------------


def select_coast_episodes(
    tank_rows: list[tuple[float, float]],
    indoor_by_utc: list[tuple[datetime, float]],
    *,
    tz: ZoneInfo,
    night_start_hour_local: int = 22,
    night_end_hour_local: int = 11,
    min_hours: float = 4.0,
    min_points: int = 4,
    max_gap_minutes: float = 420.0,
    draw_drop_c: float = 2.0,
    draw_rate_c_per_h: float = 2.0,
    max_rise_c: float = 1.0,
) -> list[CoastEpisode]:
    """Overnight, gap-tolerant, draw-free, unheated cooling stretches.

    The window opens after the evening showers and closes before any warmup, so a
    stretch is unheated by construction. Two properties of the REAL telemetry drive
    the tolerances, and getting them wrong yields zero episodes on good nights:

    * the overnight polling hole (~6 h with no Onecta call, protecting the 200/day
      quota) — so ``max_gap_minutes`` tolerates it; the tank's τ is ~95 h, so the
      trapezoid across the hole is nearly exact;
    * 1 °C quantisation — so a draw needs BOTH a real drop and a steep rate, and a
      single rounding step cannot fake one.
    """
    series = _to_series(tank_rows)
    if not series:
        return []
    indoor = sorted(indoor_by_utc)

    def _in_night(ts: datetime) -> bool:
        h = ts.astimezone(tz).hour
        if night_start_hour_local > night_end_hour_local:
            return h >= night_start_hour_local or h < night_end_hour_local
        return night_start_hour_local <= h < night_end_hour_local

    def _indoor_mean(a: datetime, b: datetime) -> float | None:
        vals = [v for ts, v in indoor if a <= ts <= b]
        return sum(vals) / len(vals) if vals else None

    segments: list[list[tuple[datetime, float]]] = []
    cur: list[tuple[datetime, float]] = []
    for ts, v in series:
        if not _in_night(ts):
            if cur:
                segments.append(cur)
                cur = []
            continue
        if cur:
            gap_min = (ts - cur[-1][0]).total_seconds() / 60.0
            drop = cur[-1][1] - v
            rate = drop / max(gap_min / 60.0, 1e-6)
            is_draw = drop >= draw_drop_c and rate > draw_rate_c_per_h
            if gap_min > max_gap_minutes or is_draw:
                segments.append(cur)
                cur = []
        cur.append((ts, v))
    if cur:
        segments.append(cur)

    episodes: list[CoastEpisode] = []
    for seg in segments:
        if len(seg) < min_points:
            continue
        start, end = seg[0][0], seg[-1][0]
        if (end - start).total_seconds() / 3600.0 < min_hours:
            continue
        t0 = seg[0][1]
        if max(v for _, v in seg) > t0 + max_rise_c:
            continue  # temperature rose — unlogged heating
        episodes.append(CoastEpisode(
            start_utc=start,
            end_utc=end,
            points=[((ts - start).total_seconds() / 3600.0, v) for ts, v in seg],
            indoor_mean_c=_indoor_mean(start, end),
        ))
    return episodes


# ---------------------------------------------------------------------------
# The joint fit (pure)
# ---------------------------------------------------------------------------


def fit_ua_and_ambient(
    episodes: list[CoastEpisode],
    *,
    c_tank_j_per_k: float,
    min_episodes: int = 8,
    min_r2: float = 0.6,
    ua_bounds: tuple[float, float] = (1.0, 5.0),
    ambient_bounds: tuple[float, float] = (10.0, 28.0),
) -> dict[str, Any]:
    """Joint least-squares of UA and the effective ambient over all episodes.

    Pools every point from every episode into one regression of ``Y = T0 − Ti``
    against ``[∫T ds, ti]``. The two coefficients are ``k = UA/C`` and ``−k·A``, so
    ``UA = k·C`` and ``A = −b2/k``. Pooling (rather than a median of per-episode
    fits) is what lets the ambient — a single physical constant of the cupboard —
    be identified from many short coasts that individually could not pin it.

    ``status='skipped'`` below the gate; never raises.
    """
    rows: list[tuple[float, float, float]] = []  # (∫T ds, ti, T0−Ti)
    n_used = 0
    for ep in episodes:
        pts = ep.points
        if len(pts) < 3:
            continue
        n_used += 1
        t0 = pts[0][1]
        integral = 0.0
        for i in range(1, len(pts)):
            dt = pts[i][0] - pts[i - 1][0]
            integral += 0.5 * (pts[i][1] + pts[i - 1][1]) * dt
            rows.append((integral, pts[i][0], t0 - pts[i][1]))

    if n_used < min_episodes or len(rows) < 3 * min_episodes:
        return {
            "status": "skipped",
            "reason": f"only {n_used} usable episode(s); need >= {min_episodes}",
            "episodes": n_used,
        }

    s11 = sum(r[0] * r[0] for r in rows)
    s22 = sum(r[1] * r[1] for r in rows)
    s12 = sum(r[0] * r[1] for r in rows)
    s1y = sum(r[0] * r[2] for r in rows)
    s2y = sum(r[1] * r[2] for r in rows)
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-9:
        return {"status": "skipped", "reason": "degenerate regression", "episodes": n_used}

    k = (s22 * s1y - s12 * s2y) / det          # UA/C, per hour
    b2 = (-s12 * s1y + s11 * s2y) / det        # = −k·A
    if k <= 0:
        return {"status": "skipped", "reason": "non-positive decay slope", "episodes": n_used}
    ambient = -b2 / k
    ua = k * c_tank_j_per_k / 3600.0

    y_mean = sum(r[2] for r in rows) / len(rows)
    ss_tot = sum((r[2] - y_mean) ** 2 for r in rows)
    ss_res = sum((r[2] - (k * r[0] + b2 * r[1])) ** 2 for r in rows)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    if r2 < min_r2:
        return {"status": "skipped", "reason": f"R²={r2:.2f} below {min_r2}",
                "episodes": n_used, "r2": r2}
    if not (ua_bounds[0] <= ua <= ua_bounds[1]):
        return {"status": "skipped", "reason": f"UA {ua:.2f} out of bounds",
                "episodes": n_used, "ua_w_per_k": ua}
    if not (ambient_bounds[0] <= ambient <= ambient_bounds[1]):
        return {"status": "skipped", "reason": f"ambient {ambient:.1f} out of bounds",
                "episodes": n_used, "ambient_c": ambient}

    return {
        "status": "ok",
        "ua_w_per_k": float(ua),
        "ambient_c": float(ambient),
        "tau_hours": float(c_tank_j_per_k / (ua * 3600.0)),
        "r2": float(r2),
        "episodes": n_used,
    }


# ---------------------------------------------------------------------------
# Draw-event detection (pure, observability only)
# ---------------------------------------------------------------------------


def detect_draw_events(
    tank_rows: list[tuple[float, float]],
    p: TankParams,
    *,
    tz: ZoneInfo,
    min_drop_c: float = 3.0,
    max_gap_minutes: float = 40.0,
) -> list[DrawEvent]:
    """Falls too steep for standing loss to explain — a hot-water draw.

    OBSERVABILITY ONLY. This never feeds the LP; it answers "does the household draw
    hot water when it says it does?" A draw at 20:00 confirms the declared schedule;
    a morning peak would mean the declaration is wrong. The magnitude is NOT a
    demand figure — a concurrent reheat masks part of every evening draw, and
    unmasking it needs the energy counter we refuse to trust.
    """
    from .model import coast_rate_c_per_h

    series = _to_series(tank_rows)
    events: list[DrawEvent] = []
    for i in range(1, len(series)):
        (t0, v0), (t1, v1) = series[i - 1], series[i]
        gap_min = (t1 - t0).total_seconds() / 60.0
        if gap_min <= 0 or gap_min > max_gap_minutes:
            continue
        drop = v0 - v1
        if drop < min_drop_c:
            continue
        # How much of that drop standing loss alone could account for.
        explained = coast_rate_c_per_h(v0, p) * (gap_min / 60.0)
        if drop - explained >= min_drop_c:
            events.append(DrawEvent(at_utc=t1, drop_c=drop, from_c=v0))
    return events


def summarise_draw_hours(events: list[DrawEvent], tz: ZoneInfo) -> dict[int, int]:
    """Draw events per local hour — the shape that confirms or refutes the schedule."""
    hist: dict[int, int] = {}
    for e in events:
        h = e.at_utc.astimezone(tz).hour
        hist[h] = hist.get(h, 0) + 1
    return hist


# ---------------------------------------------------------------------------
# Orchestration (thin, best-effort, never raises to the cron)
# ---------------------------------------------------------------------------


def refresh_dhw_calibration() -> dict[str, Any]:
    """Nightly: fit UA+ambient, log the draw-event shape, persist both. Best-effort;
    a skipped fit is stored as skipped so the reader falls back to the databook."""
    from .. import db
    from ..config import config

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now = datetime.now(UTC)
    window = int(getattr(config, "DHW_CALIBRATION_WINDOW_DAYS", 21))
    start = now - timedelta(days=window)

    try:
        tank_rows = db.get_tank_temps_range(start.timestamp(), now.timestamp())
    except Exception:  # pragma: no cover — defensive
        logger.exception("dhw.calibration: tank telemetry read failed")
        return {"status": "error", "reason": "telemetry read failed"}
    if not tank_rows:
        db.upsert_dhw_calibration("ua_ambient", status="skipped",
                                  payload={"reason": "no live tank telemetry"},
                                  window_days=window)
        return {"status": "skipped", "reason": "no live tank telemetry"}

    # Indoor sensors (#540) place the ambient's coupling to the house. Absent → the
    # joint fit still recovers a constant ambient; we just cannot yet learn how it
    # tracks the house across seasons (that needs winter variation anyway).
    try:
        indoor_rows = db.get_indoor_readings_range(
            start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        indoor = [
            (datetime.fromisoformat(str(r["captured_at"]).replace("Z", "+00:00")).astimezone(UTC),
             float(r["temp_c"]))
            for r in indoor_rows if r.get("temp_c") is not None
        ]
    except Exception:  # noqa: BLE001 — indoor is a refinement, not a dependency
        indoor = []

    c_tank = float(config.DHW_TANK_LITRES) * float(config.DHW_WATER_CP)
    episodes = select_coast_episodes(tank_rows, indoor, tz=tz)
    ua_fit = fit_ua_and_ambient(episodes, c_tank_j_per_k=c_tank)
    db.upsert_dhw_calibration(
        "ua_ambient", status=ua_fit["status"], payload=ua_fit,
        n_samples=ua_fit.get("episodes"), r2=ua_fit.get("r2"), window_days=window,
    )

    from .params import resolve_tank_params

    p = resolve_tank_params()

    # Draw events — observability only. Fit params from what we just learned (or the
    # databook), so the standing-loss subtraction uses the best UA available.
    events = detect_draw_events(tank_rows, p, tz=tz)
    hours = summarise_draw_hours(events, tz)
    db.upsert_dhw_calibration(
        "draw_events", status="ok", payload={"by_hour": hours, "n_events": len(events)},
        n_samples=len(events), window_days=window,
    )

    logger.info(
        "dhw.calibration: ua_ambient=%s (UA=%.2f ambient=%.1f r2=%.2f) | %d draws, peak hour %s",
        ua_fit["status"], ua_fit.get("ua_w_per_k") or 0.0, ua_fit.get("ambient_c") or 0.0,
        ua_fit.get("r2") or 0.0, len(events),
        max(hours, key=hours.get) if hours else "-",
    )
    return {"status": "ok", "ua_ambient": ua_fit, "draw_hours": hours}
