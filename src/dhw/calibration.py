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
    """An unheated stretch of tank cooling, with the outdoor context needed to
    place its ambient. ``points`` = (hours since start, tank °C)."""

    start_utc: datetime
    end_utc: datetime
    points: list[tuple[float, float]]
    # MEASURED outdoor mean over the episode. The airing cupboard's effective
    # ambient tracks the house, which tracks outdoors — so this is what lets a
    # July night and a January night share one fit instead of fighting over a
    # single constant. None when no live outdoor telemetry covers the episode.
    t_out_mean_c: float | None = None


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
    *,
    tz: ZoneInfo,
    outdoor_by_utc: list[tuple[datetime, float]] | None = None,
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

    def _in_night(ts: datetime) -> bool:
        h = ts.astimezone(tz).hour
        if night_start_hour_local > night_end_hour_local:
            return h >= night_start_hour_local or h < night_end_hour_local
        return night_start_hour_local <= h < night_end_hour_local

    outdoor = sorted(outdoor_by_utc or [])

    def _outdoor_mean(a: datetime, b: datetime) -> float | None:
        vals = [v for ts, v in outdoor if a <= ts <= b]
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
            t_out_mean_c=_outdoor_mean(start, end),
        ))
    return episodes


# ---------------------------------------------------------------------------
# The joint fit (pure)
# ---------------------------------------------------------------------------


def _solve3(m: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a 3x3 system.

    Kept local and dependency-free for the same reason the 2x2 below is solved
    by explicit determinant: this module runs inside a nightly cron in a slim
    container and must not grow a numeric dependency for nine coefficients.
    """
    a = [row[:] + [rhs[i]] for i, row in enumerate(m)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        for r in range(3):
            if r == col:
                continue
            f = a[r][col] / a[col][col]
            for c in range(col, 4):
                a[r][c] -= f * a[col][c]
    return [a[i][3] / a[i][i] for i in range(3)]


def _fit_linear_ambient(
    episodes: list[CoastEpisode], *, c_tank_j_per_k: float, min_episodes: int = 8
) -> dict[str, Any] | None:
    """Same ODE, but with the ambient allowed to track outdoors: ``A = a + b·T_out``.

    Substituting into ``T0 − Ti = k·∫T ds − k·A·ti`` gives three linear terms —
    ``[∫T ds, ti, T_out·ti]`` with coefficients ``[k, −k·a, −k·b]`` — so it is the
    same regression with one more column, not a different model.

    Returns None (rather than a skip dict) when it cannot be attempted at all:
    this is the OPTIONAL refinement, and the constant-ambient fit remains the
    primary. Needs outdoor spread to be identifiable — fitting a slope through
    three nights that were all 18 °C would produce a confident-looking number
    with no information in it.
    """
    rows: list[tuple[float, float, float, float]] = []  # (∫T ds, ti, T_out·ti, y)
    t_outs: list[float] = []
    n_used = 0
    for ep in episodes:
        if ep.t_out_mean_c is None or len(ep.points) < 3:
            continue
        n_used += 1
        t_outs.append(float(ep.t_out_mean_c))
        t0 = ep.points[0][1]
        integral = 0.0
        for i in range(1, len(ep.points)):
            dt_h = ep.points[i][0] - ep.points[i - 1][0]
            integral += 0.5 * (ep.points[i][1] + ep.points[i - 1][1]) * dt_h
            ti = ep.points[i][0]
            rows.append((integral, ti, float(ep.t_out_mean_c) * ti, t0 - ep.points[i][1]))

    # At least as strict as the 2-parameter fit. It was briefly looser (6 vs 8),
    # which is backwards — more parameters need more data, not less — and it
    # bit exactly where outdoor coverage is partial: episodes without measured
    # outdoor are dropped here but counted there, so a 20-episode constant fit
    # could be compared against a 6-episode linear one and lose. Measured: UA
    # 2.196 / ambient 20.2 over 20 nights vs UA 2.44 / ambient 26.1 over the
    # last 6, with the linear R² higher purely because it saw less weather.
    if n_used < min_episodes or len(rows) < 3 * min_episodes:
        return None
    spread = max(t_outs) - min(t_outs)
    if spread < 5.0:
        return {"status": "skipped", "reason": f"outdoor spread only {spread:.1f} °C; need >= 5",
                "episodes": n_used, "t_out_spread_c": spread}

    def s(i: int, j: int) -> float:
        return sum(r[i] * r[j] for r in rows)

    m = [[s(0, 0), s(0, 1), s(0, 2)],
         [s(0, 1), s(1, 1), s(1, 2)],
         [s(0, 2), s(1, 2), s(2, 2)]]
    sol = _solve3(m, [s(0, 3), s(1, 3), s(2, 3)])
    if sol is None:
        return {"status": "skipped", "reason": "degenerate linear-ambient regression",
                "episodes": n_used}
    k, c2, c3 = sol
    if k <= 0:
        return {"status": "skipped", "reason": "non-positive decay slope (linear ambient)",
                "episodes": n_used}
    a_int = -c2 / k
    b_slope = -c3 / k
    ua = k * c_tank_j_per_k / 3600.0

    y_mean = sum(r[3] for r in rows) / len(rows)
    ss_tot = sum((r[3] - y_mean) ** 2 for r in rows)
    ss_res = sum((r[3] - (k * r[0] + c2 * r[1] + c3 * r[2])) ** 2 for r in rows)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    t_out_median = sorted(t_outs)[len(t_outs) // 2]
    return {
        "status": "ok",
        "ua_w_per_k": float(ua),
        "ambient_intercept_c": float(a_int),
        "ambient_slope_per_c": float(b_slope),
        "t_out_median_c": float(t_out_median),
        "t_out_spread_c": float(spread),
        "ambient_c": float(a_int + b_slope * t_out_median),
        "tau_hours": float(c_tank_j_per_k / (ua * 3600.0)),
        "r2": float(r2),
        "episodes": n_used,
    }


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

    # The linear-ambient alternative is computed ONCE, up front, so every exit
    # below can consult it. Doing it lazily at the ambient-bounds gate — as the
    # first cut did — made it unreachable on exactly the failures it exists to
    # rescue: a constant ambient forced across a seasonal range does not fail
    # one specific bound, it corrupts `k`, and so UA, R² and the ambient go
    # together. Measured on synthetic data with a true slope of 0.25 °C/°C, the
    # constant fit returns UA 0.39 (out of bounds) while the linear one recovers
    # UA 2.44 / slope 0.25 / R² 1.0 — the right answer, previously discarded
    # because the run happened to trip the UA gate rather than the ambient one.
    linear = _fit_linear_ambient(
        episodes, c_tank_j_per_k=c_tank_j_per_k, min_episodes=min_episodes
    )

    def _skip(reason: str, **extra: Any) -> dict[str, Any]:
        """A skip that still carries everything both models produced."""
        out: dict[str, Any] = {"status": "skipped", "reason": reason,
                               "episodes": n_used, **extra}
        if linear is not None:
            out["linear_ambient"] = linear
        return out

    def _maybe_linear(reason: str, *, quality_only: bool = False,
                      **extra: Any) -> dict[str, Any]:
        """Promote the linear model if it genuinely rescues this failure.

        ``quality_only`` marks the one rejection that leaves the constant fit a
        credible reference (its parameters were plausible, it just did not
        explain the data well enough). Every other rejection is physical, and a
        model claiming an impossible ambient forfeits its standing as a bar to
        clear — see ``_promote_linear``.
        """
        promoted = _promote_linear(
            linear, ua_bounds, ambient_bounds,
            constant_r2=float(extra.get("r2") or 0.0), min_r2=min_r2,
            require_improvement=quality_only,
        )
        if promoted is not None:
            return {"status": "ok", "ambient_model": "linear", **promoted,
                    "constant_fit": {"reason": reason, "episodes": n_used, **extra}}
        return _skip(reason, **extra)

    if n_used < min_episodes or len(rows) < 3 * min_episodes:
        return _skip(f"only {n_used} usable episode(s); need >= {min_episodes}")

    s11 = sum(r[0] * r[0] for r in rows)
    s22 = sum(r[1] * r[1] for r in rows)
    s12 = sum(r[0] * r[1] for r in rows)
    s1y = sum(r[0] * r[2] for r in rows)
    s2y = sum(r[1] * r[2] for r in rows)
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-9:
        return _maybe_linear("degenerate regression")

    k = (s22 * s1y - s12 * s2y) / det          # UA/C, per hour
    b2 = (-s12 * s1y + s11 * s2y) / det        # = −k·A
    if k <= 0:
        # THE seasonal failure mode: an ambient that really tracks outdoors,
        # pooled under one constant, can drive the decay slope negative. This is
        # "the constant model is the problem, not the data" in its purest form,
        # and it used to exit before the linear fit was even computed.
        return _maybe_linear("non-positive decay slope", k=float(k))
    ambient = -b2 / k
    ua = k * c_tank_j_per_k / 3600.0

    y_mean = sum(r[2] for r in rows) / len(rows)
    ss_tot = sum((r[2] - y_mean) ** 2 for r in rows)
    ss_res = sum((r[2] - (k * r[0] + b2 * r[1])) ** 2 for r in rows)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Everything the fit produced, carried on EVERY exit path. The old skip
    # payloads dropped whichever fields the failing gate did not name — an
    # out-of-bounds ambient was stored without its UA or R², so prod could see
    # that the fit was rejected but not how close it came, and the obvious next
    # question ("was the UA sane?") was unanswerable from the record.
    fit = {
        "ua_w_per_k": float(ua),
        "ambient_c": float(ambient),
        "tau_hours": float(c_tank_j_per_k / (ua * 3600.0)),
        "r2": float(r2),
        "episodes": n_used,
    }

    if linear is not None:
        fit["linear_ambient"] = linear

    # Each of these three is the same symptom wearing a different mask: a
    # constant ambient forced across a seasonal range corrupts k, and so UA, R²
    # and the ambient fail together. All three therefore get the same chance to
    # be rescued by letting the ambient track outdoors.
    if r2 < min_r2:
        return _maybe_linear(f"R²={r2:.2f} below {min_r2}", quality_only=True, **fit)
    if not (ua_bounds[0] <= ua <= ua_bounds[1]):
        return _maybe_linear(f"UA {ua:.2f} out of bounds", **fit)
    if not (ambient_bounds[0] <= ambient <= ambient_bounds[1]):
        return _maybe_linear(f"ambient {ambient:.1f} out of bounds", **fit)

    return {"status": "ok", "ambient_model": "constant", **fit}


def _promote_linear(
    linear: dict[str, Any] | None,
    ua_bounds: tuple[float, float],
    ambient_bounds: tuple[float, float],
    constant_r2: float,
    min_r2: float,
    *,
    require_improvement: bool = True,
) -> dict[str, Any] | None:
    """The linear-ambient fit, if it is physically sane — and, when the constant
    model is still a credible reference, genuinely better than it.

    The physical gates are unconditional. A third parameter can only ever raise
    in-sample R², so the model must also imply a cupboard that responds to
    outdoors in the right direction and *less than 1:1* — one inside a heated
    house cannot swing further than the weather. The 2026-07 prod replay
    produced slope 6.9 with intercept −180 °C; that is noise wearing a physics
    costume and these bounds are what refuse it.

    ``require_improvement`` is the subtle part. Comparing R² between the two
    models only means something when the constant fit is a *credible* model. It
    usually is not: when it is rejected for an impossible ambient or a negative
    decay slope, it can still carry a high R² — the synthetic seasonal case
    reaches R² 0.991 while claiming an ambient of −155.9 °C. Demanding the
    linear model beat *that* by a margin is demanding it beat 1.011, which is
    unreachable, and it is how the first cut discarded a fit that recovered the
    truth exactly. So a constant fit rejected on PHYSICAL grounds forfeits its
    standing as a reference, and the linear model need only clear ``min_r2`` on
    its own. Only a constant fit rejected purely for fit QUALITY (``r2 <
    min_r2``, parameters otherwise plausible) still has to be beaten.
    """
    if not linear or linear.get("status") != "ok":
        return None
    ua = float(linear["ua_w_per_k"])
    amb = float(linear["ambient_c"])
    slope = float(linear["ambient_slope_per_c"])
    r2 = float(linear["r2"])
    if not (ua_bounds[0] <= ua <= ua_bounds[1]):
        return None
    if not (ambient_bounds[0] <= amb <= ambient_bounds[1]):
        return None
    if not (0.0 <= slope <= 1.0):
        return None
    if r2 < min_r2:
        return None
    if require_improvement and r2 < constant_r2 + 0.02:
        return None
    return dict(linear)


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


def fit_reheat_differential(
    rows: list[tuple[float, float, float]],
    *,
    max_warmup_target_c: float = 52.0,
    heat_rise_c: float = 1.5,
    draw_drop_c: float = 1.5,
    observe_minutes: float = 100.0,
    max_step_gap_minutes: float = 45.0,
    powerful_windows_utc: list[tuple[datetime, datetime]] | None = None,
) -> dict[str, Any]:
    """Bracket the firmware's DHW reheat deadband from target-step episodes (#732).

    The Altherma starts a reheat only when ``tank ≤ target − differential``. That
    differential is a field setting on the unit we cannot read via Onecta — but the
    thermometer sees it: every time the commanded target STEPS UP while the tank
    temperature is known, the firmware either heats (Δ = target − tank was outside
    the deadband) or doesn't (inside). Observed 2026-07-17: Δ=9 heated, Δ=5 did
    not — the schedule's warmup silently became a no-op on warm-tank days.

    Method: scan for upward target steps to warmup-class targets (≤
    ``max_warmup_target_c`` — Powerful boosts to 60 °C force heating regardless of
    the deadband and would contaminate the heated set with small deltas). For each
    step, watch ``observe_minutes``: a rise ≥ ``heat_rise_c`` above the step-time
    tank temp = heated; a drop ≥ ``draw_drop_c`` BEFORE any rise = a shower draw
    contaminates the episode → discarded (the draw itself may trigger the reheat,
    so the step-time Δ is no longer what fired it).

    Estimation is a robust threshold sweep ("heats iff Δ ≥ t"), because real
    telemetry contains unlabellable contamination (a precool day with tank power
    off reads as "huge Δ, no heat"). status='ok' needs both outcomes present,
    ≥5 clean episodes and ≤25 % misclassified at the best threshold; outliers
    are reported in ``misclassified``, never averaged in. The estimate is the
    best threshold, clamped to [2, 12] °C.
    """
    series = [(datetime.fromtimestamp(t, tz=UTC), tank, tgt) for t, tank, tgt in rows]
    pwin = powerful_windows_utc or []
    episodes: list[dict[str, Any]] = []
    discarded = 0
    for i in range(1, len(series)):
        (at0, _, tgt0), (at, tank, tgt1) = series[i - 1], series[i]
        if tgt1 <= tgt0 + 0.5 or tgt1 > max_warmup_target_c:
            continue
        # Gap guard (#732 review): the step is only OBSERVED at row i — with a
        # long polling gap the firmware may have stepped near the gap's start
        # and part-heated already, so Δ measured here would be against a
        # mid-heat tank ("true Δ9 recorded as Δ4 heated").
        if (at - at0).total_seconds() / 60.0 > max_step_gap_minutes:
            discarded += 1
            continue
        # Powerful screen (#732 review): Powerful forces the lift at ANY Δ.
        # Boosts are excluded by the ≤52 target class, but guests-mode warmups
        # and manual overrides command warmup-class targets WITH Powerful —
        # telemetry can't see the flag, so the caller passes HEM's own
        # commanded-Powerful windows (action_schedule + #739 deadband-force
        # audit rows) to exclude. The test is OVERLAP with the episode's whole
        # observation interval, not just the step instant (#741 review): a
        # #735 Powerful fallback re-evaluates every heartbeat and can fire up
        # to ``observe_minutes`` AFTER the step it then contaminates — its
        # resistance rise would be credited to the step's small Δ. Dropping
        # the occasional legitimate episode near a Powerful window is the
        # accepted cost; poisoned episodes are worse than fewer episodes.
        obs_end = at + timedelta(minutes=observe_minutes)
        if any(s <= obs_end and e >= at for s, e in pwin):
            discarded += 1
            continue
        delta = tgt1 - tank
        if delta <= 0.25:
            continue  # already at/above target — the deadband is unobservable here
        heated = False
        contaminated = False
        observed_min = 0.0
        for at2, tank2, tgt2 in series[i + 1:]:
            elapsed = (at2 - at).total_seconds() / 60.0
            # Any target RE-step ends the observation — an upward re-step (boost
            # chain 47→60 + Powerful) would otherwise leak its heat into this
            # episode and label the smaller Δ "heated" (#732 review).
            if elapsed > observe_minutes or abs(tgt2 - tgt1) > 0.5:
                break
            observed_min = elapsed
            if tank2 <= tank - draw_drop_c:
                contaminated = True  # a draw may itself trigger the reheat
                break
            if tank2 >= tank + heat_rise_c:
                heated = True
                break
        # A "did not heat" verdict needs a real observation window — the compressor
        # takes ~10 min to show at the sensor, and a target re-step (boost chain)
        # can cut the window short.
        if contaminated or (not heated and observed_min < 30.0):
            discarded += 1
            continue
        episodes.append({"at_utc": at.isoformat(), "delta_c": round(delta, 1), "heated": heated})

    no_heat = [e["delta_c"] for e in episodes if not e["heated"]]
    heat = [e["delta_c"] for e in episodes if e["heated"]]
    out: dict[str, Any] = {
        "n_episodes": len(episodes),
        "n_heated": len(heat),
        "n_no_heat": len(no_heat),
        "n_discarded_draw": discarded,
        "episodes": episodes[-40:],
    }
    if not heat or not no_heat or len(episodes) < 5:
        out["status"] = "insufficient"
        return out

    # Robust threshold, not a strict bracket: real telemetry carries episodes the
    # thermometer cannot label (measured 2026-06-26: tank held at the pre-negative
    # precool 30 °C with target 45 shown but tank POWER off → "Δ15 didn't heat").
    # A strict bracket dies on one such day. Sweep candidate thresholds at the
    # midpoints between adjacent observed deltas and keep the one that
    # misclassifies fewest episodes ("heats iff Δ ≥ threshold"); accept only if
    # the survivors are ≤ 25 % — the deadband must EXPLAIN the data, outliers
    # are reported, never averaged in.
    deltas = sorted({e["delta_c"] for e in episodes})
    candidates = [deltas[0] - 0.5] + [
        (a + b) / 2.0 for a, b in zip(deltas, deltas[1:])
    ] + [deltas[-1] + 0.5]
    best_t, best_errors = None, None
    for t in candidates:
        errors = sum(
            1 for e in episodes if (e["delta_c"] >= t) != e["heated"]
        )
        # <= : on ties the HIGHEST candidate wins — the conservative direction
        # for the shadow gate (a lower threshold makes the sim heat more, over-
        # costing the incumbent in the LP-owned comparison; #732 review).
        if best_errors is None or errors <= best_errors:
            best_t, best_errors = t, errors
    n = len(episodes)
    out["threshold_c"] = round(float(best_t), 1)
    out["n_misclassified"] = int(best_errors)
    out["misclassified"] = [
        e for e in episodes if (e["delta_c"] >= best_t) != e["heated"]
    ]
    if best_errors / n > 0.25:
        out["status"] = "inconsistent"
        return out
    if not (2.0 <= best_t <= 12.0):
        # A threshold outside the physically plausible range means the data is
        # telling a different story — refuse rather than clamp it into "ok"
        # (a clamped value would sail through the reader's own gate unnoticed).
        out["status"] = "out_of_range"
        return out
    out["status"] = "ok"
    out["differential_c"] = round(float(best_t), 1)
    return out


# Exclusion pad around a #735 Powerful-fallback fire (#739). The PATCH lands
# seconds BEFORE the action_log write (the audit row is only written after the
# apply attempted writes), so pad slightly backwards; forwards, 60 min covers
# both a late first-poll observation of the step and the resistance heat-up
# itself. A fire that lands up to ``observe_minutes`` AFTER the step it
# contaminates (the escalation re-evaluates every heartbeat) is handled by the
# fit's overlap test against the episode's observation interval (#741 review),
# not by widening this pad.
_FORCE_WIN_PRE_MIN = 5.0
_FORCE_WIN_POST_MIN = 60.0


def deadband_force_windows(times_iso: list[str]) -> list[tuple[datetime, datetime]]:
    """Exclusion windows for reheat-differential fitting from #735
    Powerful-fallback fire timestamps (#739).

    Those fires force a lift at Δ < deadband but leave the stored row's
    ``tank_powerful: false`` (deliberate — no crosstalk with #619/#386), so the
    action_schedule-based exclusion cannot see them. Left unexcluded, each one
    reads as "heated at Δ < deadband" and drags the fitted threshold down — the
    exact #735 incident direction. ``hp_target_lift`` fires stay IN the fit:
    they heat via the firmware's real thermostat, so they are informative.
    """
    out: list[tuple[datetime, datetime]] = []
    for t in times_iso:
        try:
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        out.append((
            dt - timedelta(minutes=_FORCE_WIN_PRE_MIN),
            dt + timedelta(minutes=_FORCE_WIN_POST_MIN),
        ))
    return out


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

    # The UA fit needs a LONGER window than 21 days, for the same reason the
    # reheat-differential fit below does: coast episodes are rare (an unheated,
    # draw-free, ≥4 h overnight stretch), and two free parameters need more than
    # the ~20 the short window yields.
    #
    # This is measured, not assumed. Replayed against the prod DB on 2026-07-28:
    #   21 d -> 20 episodes -> ambient  0.5 °C  => REJECTED (out of bounds)
    #   45 d -> 27 episodes -> ambient 13.2 °C, UA 1.96 W/K, tau 114 h, R² 0.71 => ok
    # Same data, same model, same outdoor range — seven more episodes is the
    # whole difference between a rejected fit and a physically sane one. The
    # airing cupboard's UA does not change month to month, so a longer window
    # costs nothing in staleness.
    ua_window = max(window, int(getattr(config, "DHW_UA_CALIBRATION_WINDOW_DAYS", 45)))
    start = now - timedelta(days=ua_window)

    try:
        tank_rows = db.get_tank_temps_range(start.timestamp(), now.timestamp())
    except Exception:  # pragma: no cover — defensive
        logger.exception("dhw.calibration: tank telemetry read failed")
        return {"status": "error", "reason": "telemetry read failed"}
    if not tank_rows:
        db.upsert_dhw_calibration("ua_ambient", status="skipped",
                                  payload={"reason": "no live tank telemetry"},
                                  window_days=ua_window)
        return {"status": "skipped", "reason": "no live tank telemetry"}

    # MEASURED outdoor (#540 W2 discipline): the cupboard's effective ambient
    # tracks the house, which tracks outdoors. Without it a July night and a
    # January night have to share one constant ambient, and the fit lands
    # wherever splits the difference — which is how this returned 2.9 °C for an
    # airing cupboard and got itself rejected.
    try:
        outdoor = [
            (datetime.fromtimestamp(ts, UTC), float(v))
            for ts, v in db.get_outdoor_temps_range(start.timestamp(), now.timestamp())
        ]
    except Exception:  # noqa: BLE001 — outdoor is a refinement, not a dependency
        outdoor = []

    # C must be the SAME heat capacity the consumer uses, or every tau derived
    # downstream is wrong by the ratio. `TankParams.litres` (the databook, 192)
    # is what `dhw/model.py` integrates with, while this fit used
    # `config.DHW_TANK_LITRES` (200) — a silent 4 % inflation of every learned
    # UA. Which litreage is TRUE is a separate question for the tank's spec
    # plate; consistency is right either way.
    _p = TankParams()
    c_tank = float(_p.litres) * float(_p.cp_j_per_kg_k)
    episodes = select_coast_episodes(tank_rows, tz=tz, outdoor_by_utc=outdoor)
    ua_fit = fit_ua_and_ambient(episodes, c_tank_j_per_k=c_tank)
    db.upsert_dhw_calibration(
        "ua_ambient", status=ua_fit["status"], payload=ua_fit,
        n_samples=ua_fit.get("episodes"), r2=ua_fit.get("r2"), window_days=ua_window,
    )

    from .params import resolve_tank_params

    p = resolve_tank_params()

    # Draw events — observability only. Fit params from what we just learned (or the
    # databook), so the standing-loss subtraction uses the best UA available.
    events = detect_draw_events(tank_rows, p, tz=tz)
    hours = summarise_draw_hours(events, tz)
    db.upsert_dhw_calibration(
        "draw_events", status="ok", payload={"by_hour": hours, "n_events": len(events)},
        # `ua_window`, not `window`: detect_draw_events consumes the WIDENED
        # tank_rows, so recording 21 here made "draws per day" read ~2x high.
        n_samples=len(events), window_days=ua_window,
    )

    # Reheat differential (#732) — the firmware's deadband, observable at target
    # steps. Steps are rare (~1 clean episode per day at best), so this fit uses
    # a LONGER window than the UA fit — 21 days rarely clears the ≥5-episode
    # gate and each thin night would overwrite a good row with 'insufficient'.
    # NB `window`, not `ua_window`: the reheat fit's 45 days has its own
    # rationale (target steps are rare), independent of the UA window.
    diff_window = max(window, 45)
    diff_start = now - timedelta(days=diff_window)
    try:
        tt_rows = db.get_tank_temp_targets_range(diff_start.timestamp(), now.timestamp())
        pwin_raw = db.get_powerful_action_windows(
            diff_start.strftime("%Y-%m-%dT%H:%M:%S"), now.strftime("%Y-%m-%dT%H:%M:%S"))
        pwin = []
        for s, e in pwin_raw:
            try:
                sdt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                edt = datetime.fromisoformat(str(e).replace("Z", "+00:00"))
                if sdt.tzinfo is None:
                    sdt = sdt.replace(tzinfo=UTC)
                if edt.tzinfo is None:
                    edt = edt.replace(tzinfo=UTC)
                pwin.append((sdt, edt))
            except ValueError:
                continue
        # #739 — Powerful-fallback deadband-force fires are invisible to the
        # action_schedule query above (the stored row keeps powerful=false);
        # exclude them from the fit via their audit-log timestamps.
        force_win = deadband_force_windows(
            db.get_deadband_force_powerful_times(
                diff_start.strftime("%Y-%m-%dT%H:%M:%S"),
                now.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        )
        n_schedule_windows = len(pwin)
        pwin.extend(force_win)
        diff_fit = fit_reheat_differential(tt_rows, powerful_windows_utc=pwin)
        # Keep the pre-existing field's meaning: action_schedule windows only
        # (#741 review) — the force windows get their own counter.
        diff_fit["n_powerful_windows"] = n_schedule_windows
        diff_fit["n_deadband_force_windows"] = len(force_win)
    except Exception:  # pragma: no cover — defensive; never break the cron
        logger.exception("dhw.calibration: reheat differential fit failed")
        diff_fit = {"status": "error"}
    db.upsert_dhw_calibration(
        "reheat_differential", status=diff_fit.get("status", "error"), payload=diff_fit,
        n_samples=diff_fit.get("n_episodes"), window_days=diff_window,
    )

    logger.info(
        "dhw.calibration: ua_ambient=%s (UA=%.2f ambient=%.1f r2=%.2f) | %d draws, peak hour %s"
        " | reheat_diff=%s (%s°C, %s/%s misfit, n=%s)",
        ua_fit["status"], ua_fit.get("ua_w_per_k") or 0.0, ua_fit.get("ambient_c") or 0.0,
        ua_fit.get("r2") or 0.0, len(events),
        max(hours, key=hours.get) if hours else "-",
        diff_fit.get("status"), diff_fit.get("differential_c"),
        diff_fit.get("n_misclassified"), diff_fit.get("n_episodes"),
        diff_fit.get("n_episodes"),
    )
    return {"status": "ok", "ua_ambient": ua_fit, "draw_hours": hours,
            "reheat_differential": diff_fit}
