"""DHW tank thermal learner — UA_tank / COP_dhw / evening draw, measured.

The tank's physics has never been measured. ``DHW_TANK_UA_W_PER_K=2.5`` and
``physics.HEAT_LOSS_C_PER_HOUR=0.3`` are ASSUMED constants, and the LP's DHW
COP comes entirely off the parametric curve. That was tolerable while the K1
fixed schedule owned the tank (the LP only had to *predict* an exogenous load),
but the moment the LP owns tank TIMING those three numbers become the decision:
how long the tank coasts through the peak, how much a warmup costs, and how
much heat the evening showers actually pull.

What 21 days of prod telemetry actually say (2026-06-23..07-13, 19 clean decay
episodes) — and note the first two are the OPPOSITE of what a naive
setback-to-morning temperature delta suggests, which is why this fits episodes
rather than differencing endpoints:

* **UA is roughly as assumed** — 2.0 W/K fitted vs 2.5 assumed (τ ≈ 114 h). The
  tank coasts at 0.1–0.5 °C/h. A crude 22:00→07:00 delta reads much faster only
  because it swallows the setback transient and any late draw.
* **the DHW COP is badly wrong** — three independent clean warmups measure
  2.55–2.62 while the LP's curve claims **4.70** at the same outdoor temperature.
  The LP believes tank heat is nearly twice as cheap as it is. This is the
  decision-relevant error, and it is invisible today only because the K2 pin
  makes ``e_dhw`` exogenous.
* **the evening draw is lumpy** — median ~1.0 kWh thermal, p75 ~3.5. Most
  evenings barely move the tank; a few take 8–17 °C out of it.

So this module learns, from data the system already collects:

* **UA_tank (W/K)** — from unheated overnight decay episodes in
  ``daikin_telemetry.tank_temp_c``, via the INTEGRAL form of the cooling ODE
  (same estimator as the W2 building learner: exact under a time-varying
  ambient, where the log-linearisation biases high).
* **COP_dhw (measured) → ``cop_mult``** — a heat EVENT is a run of consecutive
  2h Onecta buckets with ``kwh_dhw > 0`` bounded by quiet buckets, so the
  measured electric energy pairs EXACTLY with the observed temperature rise
  (no sub-bucket attribution — the honest granularity limit, cf. db.py's
  refusal to fabricate 30-min DHW splits). ``COP = (C·ΔT + standing) / kWh``.
  Persisted as a MULTIPLIER on the existing curve, not a replacement: the
  curve keeps owning the T_out dependence, the multiplier corrects the level
  (same division of labour as the DHW autoscale-vs-bucket-bias split).
* **Evening draw (kWh thermal)** — how much heat the showers actually remove,
  from the energy balance over the drawdown window (the tank's temperature
  drop UNDERSTATES it whenever the firmware reheats mid-draw, so the measured
  reheat energy is added back). The LP's litres-based model (``dhw_demand``)
  stays the fallback.

Same shape as :mod:`src.analytics.thermal_learning` (W2, #641): PURE fitters
(tests drive them with synthetic curves of known UA), quality-gated aggregation
that reports ``skipped`` rather than raising, a single-row calibration table,
and bounded readers with env fallback as the ONLY surface consumers touch. A
component that skips PRESERVES its previous value — one contaminated week must
not erase a good fit.

DECONTAMINATION (what the physics can't forgive):

* decay episodes are overnight, gap-free, and blocked by any 2h bucket with
  measured ``kwh_dhw`` (plus a settle tail) — a reheat inside the window turns
  a decay into a sawtooth;
* a per-sample DROP steeper than standing loss can explain is a DRAW: it breaks
  the episode, and (the W2 lesson) the clean stretch BEFORE it survives as its
  own episode rather than the whole night being thrown away;
* ``tank_negative_boost`` windows and user-overridden tank actions are excluded
  wholesale (``db.get_dhw_boost_windows``), as is the Sunday legionella
  stand-off — the firmware owns the tank there, and a 60 °C cycle is not the
  schedule's doing;
* ``source='live'`` telemetry ONLY: the physics-estimator rows written when the
  Daikin quota is exhausted model smooth decay FROM these very constants, so
  learning from them would be learning our own echo (the ``k_per_degc`` lesson).

Nothing here changes behaviour: this module only writes ``dhw_tank_calibration``
and exposes readers. The LP wiring is the next PR, behind ``DHW_LP_OWNED_ENABLED``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import config, cop_at_temperature

logger = logging.getLogger(__name__)

_J_PER_KWH = 3.6e6


# ---------------------------------------------------------------------------
# Pure data shapes
# ---------------------------------------------------------------------------


@dataclass
class TankDecayEpisode:
    """An unheated stretch of tank cooling. ``points`` = (hours since episode
    start, tank °C); the ambient is the room the tank sits in (constant)."""

    start_utc: datetime
    end_utc: datetime
    points: list[tuple[float, float]]
    t_ambient_c: float
    t_start_c: float


@dataclass
class TankHeatEvent:
    """A run of consecutive 2h buckets with measured DHW energy, bounded by
    quiet buckets, paired with the tank temperatures at the run's edges.

    The bucket run — not a sub-bucket window — is the unit BECAUSE the Onecta
    energy counter is 2-hourly: pairing a finer temperature window with a
    coarser energy figure would attribute energy the event never used.
    """

    start_utc: datetime
    end_utc: datetime
    points: list[tuple[float, float]]  # (hours since start, tank °C)
    kwh_dhw: float
    t_outdoor_c: float | None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _bucket_window_utc(day_local: date, bucket_idx: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC span of LOCAL 2h bucket ``bucket_idx`` on ``day_local``."""
    start = datetime.combine(day_local, time(bucket_idx * 2, 0), tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(hours=2)).astimezone(UTC)


def _parse_windows(windows: list[tuple[str, str]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in windows:
        try:
            ws = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            we = datetime.fromisoformat(str(e).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ws.tzinfo is None:
            ws = ws.replace(tzinfo=UTC)
        if we.tzinfo is None:
            we = we.replace(tzinfo=UTC)
        out.append((ws, we))
    return out


def dhw_blocked_intervals(
    consumption_rows: list[dict[str, Any]],
    boost_windows: list[tuple[str, str]],
    tz: ZoneInfo,
    *,
    dhw_contam_kwh: float,
    settle_hours: float,
) -> list[tuple[datetime, datetime]]:
    """UTC spans during (or shortly after) which heat went INTO the tank.

    Any 2h bucket with ``kwh_dhw`` above the floor blocks its own span plus a
    settle tail (the plate exchanger keeps handing heat to the tank after the
    compressor stops). Time BEFORE an activity window is clean by definition,
    which is what lets a night whose tail contains a 04:00 negative-price boost
    still donate its clean early stretch.
    """
    windows: list[tuple[datetime, datetime]] = []
    tail = timedelta(hours=settle_hours)
    for r in consumption_rows:
        try:
            d = date.fromisoformat(str(r["date"]))
            b = int(r["bucket_idx"])
        except (ValueError, TypeError, KeyError):
            continue
        dhw = r.get("kwh_dhw")
        if dhw is None or float(dhw) <= dhw_contam_kwh:
            continue
        ws, we = _bucket_window_utc(d, b, tz)
        windows.append((ws, we + tail))
    for ws, we in _parse_windows(boost_windows):
        windows.append((ws, we + tail))
    return sorted(windows)


def _in_windows(ts: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    return any(ws <= ts < we for ws, we in windows)


def in_legionella_window(ts: datetime, *, dow: int, start_hour_utc: int,
                         start_minute_utc: int, duration_minutes: int) -> bool:
    """The firmware-owned Sunday thermal-shock window (UTC-anchored, same
    convention as ``state_machine.in_legionella_standoff``)."""
    if ts.weekday() != dow:
        return False
    start = ts.replace(hour=start_hour_utc, minute=start_minute_utc, second=0, microsecond=0)
    return start <= ts < start + timedelta(minutes=duration_minutes)


def _tank_series(rows: list[tuple[float, float]]) -> list[tuple[datetime, float]]:
    """``(epoch, °C)`` → ``(aware datetime, °C)``, ascending, de-duplicated."""
    out: list[tuple[datetime, float]] = []
    seen: set[float] = set()
    for epoch, temp in rows:
        try:
            e = float(epoch)
            t = float(temp)
        except (TypeError, ValueError):
            continue
        if e in seen:
            continue
        seen.add(e)
        out.append((datetime.fromtimestamp(e, tz=UTC), t))
    return sorted(out)


# ---------------------------------------------------------------------------
# UA_tank — overnight decay
# ---------------------------------------------------------------------------


def select_tank_decay_episodes(
    tank_rows: list[tuple[float, float]],
    consumption_rows: list[dict[str, Any]],
    boost_windows: list[tuple[str, str]],
    *,
    tz: ZoneInfo,
    t_ambient_c: float,
    night_start_hour_local: int = 23,
    night_end_hour_local: int = 10,
    min_episode_hours: float = 4.0,
    min_points: int = 4,
    max_gap_minutes: float = 420.0,
    settle_hours: float = 1.0,
    min_delta_t_c: float = 8.0,
    draw_drop_c: float = 2.0,
    draw_drop_rate_c_per_h: float = 2.0,
    max_rise_c: float = 1.0,
    dhw_contam_kwh: float = 0.1,
    legionella: dict[str, int] | None = None,
) -> list[TankDecayEpisode]:
    """PURE selector: overnight, heat-free, draw-free decay stretches.

    The night window opens AFTER the static setback (default 23:00 local — an
    hour of settle past the 22:00 transition) and closes before the earliest
    possible warmup (10:00 local vs the [11,16) price-aware window), so the
    stretch is genuinely unheated by construction as well as by the bucket
    blocks.

    Two properties of the REAL telemetry drive the tolerances, and getting them
    wrong yields zero episodes on perfectly good nights (measured):

    * **the overnight polling hole.** The heartbeat stops asking Onecta between
      roughly midnight and 05:00 to protect the 200-call daily quota (#308), so
      a normal clean night is one sample at ~23:50, a 5-6 h gap, then hourly
      samples. ``max_gap_minutes`` therefore tolerates the hole rather than
      shattering the night. This is safe HERE and nowhere else: the tank's time
      constant is tens of hours, so the trapezoidal integral across a 6 h gap of
      a near-linear decay carries well under 1% error — and the 2h consumption
      buckets TILE the gap, so any reheat hiding inside it still blocks the
      episode.
    * **1 °C quantisation.** Onecta reports whole degrees, so a flat tank
      "rises" a degree on noise alone. ``max_rise_c`` allows exactly one step
      (a real reheat moves several degrees and shows up in the energy buckets).

    A DRAW is distinguished from decay by RATE, not by absolute drop: natural
    coasting measures 0.1–1.1 °C/h here, a shower dumps 6 °C in half an hour.
    Requiring BOTH a real drop (``draw_drop_c``) and a steep rate
    (``draw_drop_rate_c_per_h``) means a single quantisation step can't fake a
    draw, and a 6 h gap's worth of honest decay can't either.
    """
    series = _tank_series(tank_rows)
    if not series:
        return []
    blocked = dhw_blocked_intervals(
        consumption_rows, boost_windows, tz,
        dhw_contam_kwh=dhw_contam_kwh, settle_hours=settle_hours,
    )
    leg = legionella or {}

    def _in_night(ts: datetime) -> bool:
        h = ts.astimezone(tz).hour
        if night_start_hour_local > night_end_hour_local:  # wraps midnight
            return h >= night_start_hour_local or h < night_end_hour_local
        return night_start_hour_local <= h < night_end_hour_local

    def _excluded(ts: datetime) -> bool:
        if not _in_night(ts) or _in_windows(ts, blocked):
            return True
        if leg and in_legionella_window(
            ts,
            dow=int(leg.get("dow", 6)),
            start_hour_utc=int(leg.get("start_hour_utc", 11)),
            start_minute_utc=int(leg.get("start_minute_utc", 0)),
            duration_minutes=int(leg.get("duration_minutes", 120)),
        ):
            return True
        return False

    segments: list[list[tuple[datetime, float]]] = []
    cur: list[tuple[datetime, float]] = []
    for ts, temp in series:
        if _excluded(ts):
            if cur:
                segments.append(cur)
                cur = []
            continue
        if cur:
            gap_min = (ts - cur[-1][0]).total_seconds() / 60.0
            drop = cur[-1][1] - temp
            rate = drop / max(gap_min / 60.0, 1e-6)
            is_draw = drop >= draw_drop_c and rate > draw_drop_rate_c_per_h
            if gap_min > max_gap_minutes or is_draw:
                segments.append(cur)
                cur = []
        cur.append((ts, temp))
    if cur:
        segments.append(cur)

    episodes: list[TankDecayEpisode] = []
    for seg in segments:
        if len(seg) < min_points:
            continue
        start, end = seg[0][0], seg[-1][0]
        if (end - start).total_seconds() / 3600.0 < min_episode_hours:
            continue
        t0 = seg[0][1]
        if (t0 - t_ambient_c) < min_delta_t_c:
            continue  # UA unidentifiable — tank already near room temperature
        if max(v for _, v in seg) > t0 + max_rise_c:
            continue  # temperature rose — unlogged heating
        episodes.append(TankDecayEpisode(
            start_utc=start,
            end_utc=end,
            points=[((ts - start).total_seconds() / 3600.0, v) for ts, v in seg],
            t_ambient_c=float(t_ambient_c),
            t_start_c=float(t0),
        ))
    return episodes


def fit_tank_ua_for_episode(
    ep: TankDecayEpisode, *, c_tank_j_per_k: float
) -> tuple[float, float] | None:
    """PURE fit of one decay episode via the INTEGRAL form of the cooling ODE:

        T_0 − T_i = (1/τ) · ∫₀^{t_i} (T(s) − T_ambient) ds

    (trapezoidal integral over the measured points; least squares through the
    origin on ``Y = X/τ``). ``UA = C_tank / τ``. Returns ``(ua_w_per_k, r2)``,
    or None when the episode degenerates.

    This is the same estimator as the W2 building learner for the same reason:
    it is exact for a time-varying gap, whereas taking logs of the exponential
    solution biases τ high on the (common) falling-gap night.
    """
    if len(ep.points) < 3:
        return None
    t0 = ep.points[0][1]
    if (t0 - ep.t_ambient_c) <= 0.5:
        return None
    pts: list[tuple[float, float]] = []
    x = 0.0
    prev_t, prev_gap = ep.points[0][0], t0 - ep.t_ambient_c
    for t_h, temp in ep.points[1:]:
        gap = temp - ep.t_ambient_c
        x += 0.5 * (prev_gap + gap) * (t_h - prev_t)
        prev_t, prev_gap = t_h, gap
        if x <= 0:
            continue  # tank at/below ambient — no signal
        pts.append((x, t0 - temp))
    if len(pts) < 3:
        return None
    sxx = sum(xv * xv for xv, _ in pts)
    sxy = sum(xv * yv for xv, yv in pts)
    if sxx <= 0 or sxy <= 0:  # non-positive slope → not a decay
        return None
    slope = sxy / sxx  # = 1/τ  (per hour)
    tau_h = 1.0 / slope
    if tau_h <= 0:
        return None
    y_mean = sum(yv for _, yv in pts) / len(pts)
    ss_tot = sum((yv - y_mean) ** 2 for _, yv in pts)
    ss_res = sum((yv - slope * xv) ** 2 for xv, yv in pts)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    ua = c_tank_j_per_k / (tau_h * 3600.0)  # J/K ÷ s = W/K
    return ua, r2


def fit_tank_ua(
    episodes: list[TankDecayEpisode],
    *,
    c_tank_j_per_k: float,
    min_episodes: int = 5,
    min_r2: float = 0.8,
    min_ua_w_per_k: float = 0.5,
    max_ua_w_per_k: float = 15.0,
) -> dict[str, Any]:
    """Aggregate per-episode UA fits. Median over quality-passing episodes;
    ``status='skipped'`` (never raises) below the gate."""
    fits: list[tuple[float, float]] = []
    rejected = 0
    for ep in episodes:
        fit = fit_tank_ua_for_episode(ep, c_tank_j_per_k=c_tank_j_per_k)
        if fit is None:
            rejected += 1
            continue
        ua, r2 = fit
        if r2 < min_r2 or not (min_ua_w_per_k <= ua <= max_ua_w_per_k):
            rejected += 1
            continue
        fits.append((ua, r2))
    if len(fits) < min_episodes:
        return {
            "status": "skipped",
            "reason": f"only {len(fits)} quality decay episode(s); need >= {min_episodes}",
            "episodes": len(fits),
            "episodes_rejected": rejected,
        }
    uas = sorted(u for u, _ in fits)
    r2s = sorted(r for _, r in fits)
    ua_med = float(uas[len(uas) // 2])
    return {
        "status": "ok",
        "ua_w_per_k": ua_med,
        "tau_hours": float(c_tank_j_per_k / (ua_med * 3600.0)),
        "r2_median": float(r2s[len(r2s) // 2]),
        "episodes": len(fits),
        "episodes_rejected": rejected,
    }


# ---------------------------------------------------------------------------
# COP_dhw — measured heat events
# ---------------------------------------------------------------------------


def select_tank_heat_events(
    tank_rows: list[tuple[float, float]],
    consumption_rows: list[dict[str, Any]],
    boost_windows: list[tuple[str, str]],
    outdoor_series: list[tuple[datetime, float]],
    *,
    tz: ZoneInfo,
    min_bucket_kwh: float = 0.15,
    quiet_kwh: float = 0.05,
    max_buckets: int = 3,
    min_rise_c: float = 4.0,
    max_drop_c: float = 0.6,
    min_points: int = 3,
    edge_tolerance_minutes: float = 45.0,
    legionella: dict[str, int] | None = None,
) -> list[TankHeatEvent]:
    """PURE selector: runs of consecutive DHW-active 2h buckets, bounded by
    QUIET buckets on both sides, whose tank temperature rises monotonically.

    Bounding by quiet buckets is what makes the energy pairing exact: all the
    electric energy the counter attributes to those buckets went into the rise
    we observe, with nothing spilling in from a neighbour. A bucket that is
    MISSING (no row) is not quiet — it's unknown — so it fails the boundary and
    the run is dropped (missing ≠ zero, the ``dhw_error_log`` rule).

    A drop inside the run means a draw overlapped the reheat: the energy then
    covers heat we can't see, so the event is rejected rather than fitted low.
    """
    series = _tank_series(tank_rows)
    if not series:
        return []
    by_key: dict[tuple[str, int], float | None] = {}
    for r in consumption_rows:
        try:
            d = str(r["date"])
            b = int(r["bucket_idx"])
        except (ValueError, TypeError, KeyError):
            continue
        kwh = r.get("kwh_dhw")
        by_key[(d, b)] = None if kwh is None else float(kwh)

    boosts = _parse_windows(boost_windows)
    leg = legionella or {}
    outdoor_sorted = sorted(outdoor_series)

    def _kwh(d: date, b: int) -> Any:
        """Bucket energy, ``None`` when the row exists but the counter was
        null, and the ``"missing"`` sentinel when there is no row at all —
        the two are different facts and only one of them is quiet."""
        if b < 0:
            d, b = d - timedelta(days=1), 11
        elif b > 11:
            d, b = d + timedelta(days=1), 0
        return by_key.get((d.isoformat(), b), "missing")

    def _temp_at(ts: datetime) -> float | None:
        best: tuple[float, float] | None = None
        for t, v in series:
            dt_min = abs((t - ts).total_seconds()) / 60.0
            if best is None or dt_min < best[0]:
                best = (dt_min, v)
        if best is None or best[0] > edge_tolerance_minutes:
            return None
        return best[1]

    def _outdoor_at(start: datetime, end: datetime) -> float | None:
        vals = [v for ts, v in outdoor_sorted if start <= ts < end]
        return sum(vals) / len(vals) if vals else None

    # Walk the (date, bucket) grid in order, grouping active runs.
    keys = sorted({k for k in by_key})
    events: list[TankHeatEvent] = []
    i = 0
    while i < len(keys):
        d_str, b = keys[i]
        kwh = by_key.get((d_str, b))
        if kwh is None or kwh <= min_bucket_kwh:
            i += 1
            continue
        d = date.fromisoformat(d_str)
        run = [(d, b, float(kwh))]
        j = i + 1
        while j < len(keys):
            nd_str, nb = keys[j]
            nd = date.fromisoformat(nd_str)
            prev_d, prev_b, _ = run[-1]
            expect = (prev_d, prev_b + 1) if prev_b < 11 else (prev_d + timedelta(days=1), 0)
            if (nd, nb) != expect:
                break
            nk = by_key.get((nd_str, nb))
            if nk is None or nk <= min_bucket_kwh:
                break
            run.append((nd, nb, float(nk)))
            j += 1
        i = j
        if len(run) > max_buckets:
            continue
        # Boundaries must be KNOWN-quiet on both sides.
        before = _kwh(run[0][0], run[0][1] - 1)
        after = _kwh(run[-1][0], run[-1][1] + 1)
        if before == "missing" or after == "missing":
            continue
        if before is None or after is None:
            continue
        if float(before) > quiet_kwh or float(after) > quiet_kwh:
            continue

        start, _ = _bucket_window_utc(run[0][0], run[0][1], tz)
        _, end = _bucket_window_utc(run[-1][0], run[-1][1], tz)
        if any(ws < end and start < we for ws, we in boosts):
            continue
        if leg and any(
            in_legionella_window(
                start + timedelta(minutes=m),
                dow=int(leg.get("dow", 6)),
                start_hour_utc=int(leg.get("start_hour_utc", 11)),
                start_minute_utc=int(leg.get("start_minute_utc", 0)),
                duration_minutes=int(leg.get("duration_minutes", 120)),
            )
            for m in range(0, int((end - start).total_seconds() // 60) + 1, 30)
        ):
            continue

        t_start = _temp_at(start)
        t_end = _temp_at(end)
        if t_start is None or t_end is None:
            continue
        if (t_end - t_start) < min_rise_c:
            continue
        inner = [(ts, v) for ts, v in series if start <= ts <= end]
        pts = [(start, t_start)] + inner + [(end, t_end)]
        # De-duplicate near-identical edge samples, keep ascending order.
        dedup: list[tuple[datetime, float]] = []
        for ts, v in sorted(pts):
            if dedup and abs((ts - dedup[-1][0]).total_seconds()) < 60:
                continue
            dedup.append((ts, v))
        if len(dedup) < min_points:
            continue
        if any(dedup[k][1] - dedup[k + 1][1] > max_drop_c for k in range(len(dedup) - 1)):
            continue  # a draw overlapped the reheat
        events.append(TankHeatEvent(
            start_utc=start,
            end_utc=end,
            points=[((ts - start).total_seconds() / 3600.0, v) for ts, v in dedup],
            kwh_dhw=sum(k for _, _, k in run),
            t_outdoor_c=_outdoor_at(start, end),
        ))
    return events


def modelled_cop_dhw(temp_outdoor_c: float) -> float:
    """The COP the LP would have assumed for a DHW slot at this outdoor
    temperature — mirrors ``lp_optimizer``'s curve + DHW penalty + lift
    multiplier exactly, so the learned ratio is a correction OF the LP's own
    number, not of a lookalike."""
    from ..physics import apply_cop_lift_multiplier

    base = max(1.0, cop_at_temperature(config.DAIKIN_COP_CURVE, temp_outdoor_c))
    dhw_pen = float(config.COP_DHW_PENALTY)
    lift_pen = float(getattr(config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.0))
    if lift_pen <= 0.0:
        return max(1.0, base - dhw_pen)
    return max(1.0, apply_cop_lift_multiplier(
        max(1.0, base - dhw_pen),
        temp_outdoor_c,
        float(getattr(config, "LP_COP_DHW_LIFT_SUPPLY_C", 45.0)),
        penalty_per_k=lift_pen,
        reference_delta_k=float(getattr(config, "LP_COP_LIFT_REFERENCE_DELTA_K", 25.0)),
        min_mult=float(getattr(config, "LP_COP_LIFT_MIN_MULTIPLIER", 0.5)),
    ))


def _standing_loss_j(
    points: list[tuple[float, float]], *, ua_w_per_k: float, t_ambient_c: float
) -> float:
    """∫ UA·(T − T_ambient) dt over the event (trapezoid, J)."""
    total = 0.0
    for k in range(len(points) - 1):
        t0_h, v0 = points[k]
        t1_h, v1 = points[k + 1]
        gap_mean = ((v0 - t_ambient_c) + (v1 - t_ambient_c)) / 2.0
        total += ua_w_per_k * gap_mean * (t1_h - t0_h) * 3600.0
    return total


def fit_dhw_cop(
    events: list[TankHeatEvent],
    *,
    c_tank_j_per_k: float,
    ua_w_per_k: float,
    t_ambient_c: float,
    min_samples: int = 8,
    min_cop: float = 1.0,
    max_cop: float = 6.0,
    mult_min: float = 0.5,
    mult_max: float = 1.5,
) -> dict[str, Any]:
    """PURE fit: per-event ``COP = (C·ΔT + standing losses) / kWh_electric``,
    aggregated as a MEDIAN, plus the median ratio against the modelled curve.

    Standing loss is added back because it is heat the compressor paid for and
    the thermometer never shows — omitting it biases the measured COP low by
    roughly the coast rate over the event.

    The output that consumers use is ``cop_mult`` (measured ÷ modelled), so the
    curve keeps owning the outdoor-temperature dependence — a handful of events
    per day cannot re-fit a curve, but they can absolutely tell us the level is
    off. And it IS off: three independent clean warmups in prod (2026-06-29,
    07-08, 07-09) measure COP 2.56–2.62 while the LP's curve claims 4.70 at the
    same outdoor temperature — the curve is a SPACE-heating curve, and the lift
    penalty that was supposed to correct it for DHW's much higher supply
    temperature (``LP_COP_LIFT_PENALTY_PER_KELVIN``) is 0 in prod. Hence the
    ``mult_min`` floor of 0.5 rather than a tighter band: the honest correction
    here is ~0.55, and clamping it away would keep the LP believing tank heat is
    twice as cheap as it is — the single most decision-relevant error in a
    regime where the LP times the tank.
    """
    cops: list[float] = []
    ratios: list[float] = []
    rejected = 0
    for ev in events:
        if ev.kwh_dhw <= 0 or len(ev.points) < 2:
            rejected += 1
            continue
        d_t = ev.points[-1][1] - ev.points[0][1]
        thermal_j = c_tank_j_per_k * d_t + _standing_loss_j(
            ev.points, ua_w_per_k=ua_w_per_k, t_ambient_c=t_ambient_c
        )
        cop = (thermal_j / _J_PER_KWH) / ev.kwh_dhw
        if not (min_cop <= cop <= max_cop):
            rejected += 1
            continue
        cops.append(cop)
        if ev.t_outdoor_c is not None:
            modelled = modelled_cop_dhw(float(ev.t_outdoor_c))
            if modelled > 0:
                ratios.append(cop / modelled)
    if len(cops) < min_samples:
        return {
            "status": "skipped",
            "reason": f"only {len(cops)} quality heat event(s); need >= {min_samples}",
            "samples": len(cops),
            "samples_rejected": rejected,
        }
    cops.sort()
    cop_med = float(cops[len(cops) // 2])
    if not ratios:
        return {
            "status": "skipped",
            "reason": "no event had outdoor coverage — cannot form the curve ratio",
            "samples": len(cops),
            "cop_median": cop_med,
        }
    ratios.sort()
    mult = float(ratios[len(ratios) // 2])
    return {
        "status": "ok",
        "cop_median": cop_med,
        "cop_mult": max(mult_min, min(mult_max, mult)),
        "cop_mult_raw": mult,
        "samples": len(cops),
        "samples_rejected": rejected,
    }


# ---------------------------------------------------------------------------
# Evening draw — how much heat the showers actually remove
# ---------------------------------------------------------------------------


def estimate_evening_draws(
    tank_rows: list[tuple[float, float]],
    consumption_rows: list[dict[str, Any]],
    boost_windows: list[tuple[str, str]],
    *,
    tz: ZoneInfo,
    c_tank_j_per_k: float,
    ua_w_per_k: float,
    t_ambient_c: float,
    cop_dhw: float,
    hold_start_hour_local: int = 19,
    draw_start_hour_local: int = 20,
    draw_end_hour_local: int = 23,
    min_days: int = 7,
    min_draw_kwh: float = 0.2,
    max_draw_kwh: float = 8.0,
) -> dict[str, Any]:
    """PURE energy-balance estimate of the evening draw, per day.

        Q_draw = Q_reheat − Q_standing − C·ΔT

    where ΔT is the (negative) tank change from the pre-shower hold to the
    post-shower trough. Reheat energy is ADDED BACK because the firmware often
    tops the tank up mid-drawdown: on those evenings the temperature drop alone
    understates the heat actually drawn — which is precisely the reheat the LP
    is being asked to move out of the peak, so measuring it low would hide the
    prize.

    Days with a boost or a user override are skipped. Returns the median (the
    LP's expected-cost input) AND the p75 (the comfort-sizing input): showers
    are lumpy, and under-sizing the draw shows up as a cold shower while
    over-sizing costs a few pence.
    """
    series = _tank_series(tank_rows)
    if not series:
        return {"status": "skipped", "reason": "no tank telemetry", "days": 0}
    boosts = _parse_windows(boost_windows)
    kwh_by_key: dict[tuple[str, int], float | None] = {}
    for r in consumption_rows:
        try:
            kwh_by_key[(str(r["date"]), int(r["bucket_idx"]))] = (
                None if r.get("kwh_dhw") is None else float(r["kwh_dhw"])
            )
        except (ValueError, TypeError, KeyError):
            continue

    by_day: dict[date, list[tuple[datetime, float]]] = {}
    for ts, v in series:
        by_day.setdefault(ts.astimezone(tz).date(), []).append((ts, v))

    draws: list[float] = []
    skipped = 0
    for day, pts in sorted(by_day.items()):
        hold_start = datetime.combine(day, time(hold_start_hour_local, 0), tzinfo=tz)
        draw_start = datetime.combine(day, time(draw_start_hour_local, 0), tzinfo=tz)
        draw_end = datetime.combine(day, time(draw_end_hour_local, 0), tzinfo=tz)
        if any(ws < draw_end.astimezone(UTC) and hold_start.astimezone(UTC) < we
               for ws, we in boosts):
            skipped += 1
            continue
        hold = [(ts, v) for ts, v in pts if hold_start <= ts.astimezone(tz) < draw_start]
        drawn = [(ts, v) for ts, v in pts if draw_start <= ts.astimezone(tz) < draw_end]
        if not hold or len(drawn) < 2:
            skipped += 1
            continue
        t_hold = max(v for _, v in hold)
        trough_ts, t_trough = min(drawn, key=lambda p: p[1])
        hold_ts = max(hold, key=lambda p: p[1])[0]
        if t_trough >= t_hold:
            skipped += 1  # no drawdown that evening (nobody showered)
            continue
        # Reheat energy the firmware put in between the hold peak and the trough.
        reheat_kwh = 0.0
        missing = False
        for b in range(hold_start_hour_local // 2, (draw_end_hour_local + 1) // 2):
            k = kwh_by_key.get((day.isoformat(), b), "missing")
            if k == "missing" or k is None:
                missing = True
                break
            reheat_kwh += float(k)
        if missing:
            skipped += 1
            continue
        window = [
            ((ts - hold_ts).total_seconds() / 3600.0, v)
            for ts, v in pts
            if hold_ts <= ts <= trough_ts
        ]
        standing_j = _standing_loss_j(
            window, ua_w_per_k=ua_w_per_k, t_ambient_c=t_ambient_c
        ) if len(window) >= 2 else 0.0
        q_draw_j = (
            reheat_kwh * cop_dhw * _J_PER_KWH
            - standing_j
            - c_tank_j_per_k * (t_trough - t_hold)
        )
        q_draw_kwh = q_draw_j / _J_PER_KWH
        if not (min_draw_kwh <= q_draw_kwh <= max_draw_kwh):
            skipped += 1
            continue
        draws.append(q_draw_kwh)

    if len(draws) < min_days:
        return {
            "status": "skipped",
            "reason": f"only {len(draws)} usable evening(s); need >= {min_days}",
            "days": len(draws),
            "days_skipped": skipped,
        }
    draws.sort()
    med = draws[len(draws) // 2]
    p75 = draws[min(len(draws) - 1, int(round(0.75 * (len(draws) - 1))))]
    return {
        "status": "ok",
        "draw_kwh_median": float(med),
        "draw_kwh_p75": float(p75),
        "days": len(draws),
        "days_skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Orchestration (thin, best-effort, never raises to the cron)
# ---------------------------------------------------------------------------


def _legionella_cfg() -> dict[str, int]:
    return {
        "dow": int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", 6)),
        "start_hour_utc": int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 11)),
        "start_minute_utc": int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", 0)),
        "duration_minutes": int(
            getattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120)
        ),
    }


def _outdoor_series(start_day: date, end_day: date) -> list[tuple[datetime, float]]:
    """Freshest outdoor temp per slot over the range, microclimate-corrected —
    the same series (and the same one-range-query discipline) the W2 learner
    uses."""
    from .. import db

    try:
        rows = db.get_meteo_temps_range(start_day.isoformat(), end_day.isoformat())
    except Exception:  # pragma: no cover - defensive
        logger.debug("tank_thermal: meteo range read failed", exc_info=True)
        return []
    try:
        micro = db.get_micro_climate_offset_by_hour_c()
    except Exception:  # noqa: BLE001 — a refinement, not a dependency
        micro = {}
    out: list[tuple[datetime, float]] = []
    for slot_iso, temp in rows:
        try:
            ts = datetime.fromisoformat(str(slot_iso).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        off = max(-3.0, min(3.0, float(micro.get(ts.hour, 0.0))))
        out.append((ts, float(temp) + off))
    return sorted(out)


def refresh_tank_thermal_calibration() -> dict[str, Any]:
    """Gather data, run the three fits, merge into the single-row calibration.

    Merge semantics mirror W2: a component that reports ``skipped`` PRESERVES
    the previous row's value. The row is written on every run (even an all-skip
    one) so the last-run summary is inspectable while the learner warms up.
    Never raises — the cron must survive a bad night of data.
    """
    from .. import db

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now = datetime.now(UTC)
    window = int(getattr(config, "DHW_TANK_LEARN_WINDOW_DAYS", 21))
    draw_window = int(getattr(config, "DHW_TANK_LEARN_DRAW_WINDOW_DAYS", 14))
    start = now - timedelta(days=window)
    start_day, end_day = start.date(), now.date()

    c_tank = float(config.DHW_TANK_LITRES) * float(config.DHW_WATER_CP)
    t_ambient = float(config.INDOOR_SETPOINT_C)

    try:
        tank_rows = db.get_tank_temps_range(start.timestamp(), now.timestamp())
    except Exception:  # pragma: no cover - defensive
        logger.exception("tank_thermal: telemetry read failed")
        return {"status": "error", "reason": "telemetry read failed"}
    if not tank_rows:
        return _persist({"status": "skipped", "reason": "no live tank telemetry"}, {})

    try:
        consumption = db.get_daikin_consumption_2hourly_range(
            start_day.isoformat(), end_day.isoformat()
        )
    except Exception:
        consumption = []
    try:
        boosts = db.get_dhw_boost_windows(start_day.isoformat(), end_day.isoformat())
    except Exception:
        boosts = []
    outdoor = _outdoor_series(start_day, end_day)
    leg = _legionella_cfg()

    episodes = select_tank_decay_episodes(
        tank_rows, consumption, boosts,
        tz=tz,
        t_ambient_c=t_ambient,
        night_start_hour_local=int(
            getattr(config, "DHW_TANK_LEARN_NIGHT_START_HOUR_LOCAL", 23)
        ),
        night_end_hour_local=int(getattr(config, "DHW_TANK_LEARN_NIGHT_END_HOUR_LOCAL", 10)),
        min_episode_hours=float(getattr(config, "DHW_TANK_LEARN_MIN_EPISODE_HOURS", 4.0)),
        max_gap_minutes=float(getattr(config, "DHW_TANK_LEARN_MAX_GAP_MINUTES", 420.0)),
        settle_hours=float(getattr(config, "DHW_TANK_LEARN_SETTLE_HOURS", 1.0)),
        min_delta_t_c=float(getattr(config, "DHW_TANK_LEARN_MIN_DELTA_T_C", 8.0)),
        draw_drop_c=float(getattr(config, "DHW_TANK_LEARN_DRAW_DROP_C", 2.0)),
        draw_drop_rate_c_per_h=float(
            getattr(config, "DHW_TANK_LEARN_DRAW_DROP_RATE_C_PER_H", 2.0)
        ),
        legionella=leg,
    )
    ua_fit = fit_tank_ua(
        episodes,
        c_tank_j_per_k=c_tank,
        min_episodes=int(getattr(config, "DHW_TANK_LEARN_MIN_EPISODES", 5)),
        min_r2=float(getattr(config, "DHW_TANK_LEARN_MIN_R2", 0.8)),
        min_ua_w_per_k=_UA_BOUNDS[0],
        max_ua_w_per_k=_UA_BOUNDS[1],
    )

    prev = None
    try:
        prev = db.get_dhw_tank_calibration()
    except Exception:  # pragma: no cover
        pass
    # The COP + draw fits need a UA to account for standing losses. Prefer the
    # UA learned THIS run, then the previous row's, then the env constant.
    ua_for_losses = float(
        ua_fit.get("ua_w_per_k")
        or (prev or {}).get("ua_w_per_k")
        or config.DHW_TANK_UA_W_PER_K
    )

    events = select_tank_heat_events(
        tank_rows, consumption, boosts, outdoor,
        tz=tz,
        min_bucket_kwh=float(getattr(config, "DHW_TANK_LEARN_COP_MIN_BUCKET_KWH", 0.15)),
        min_rise_c=float(getattr(config, "DHW_TANK_LEARN_COP_MIN_RISE_C", 4.0)),
        legionella=leg,
    )
    cop_fit = fit_dhw_cop(
        events,
        c_tank_j_per_k=c_tank,
        ua_w_per_k=ua_for_losses,
        t_ambient_c=t_ambient,
        min_samples=int(getattr(config, "DHW_TANK_LEARN_COP_MIN_SAMPLES", 8)),
        mult_min=_COP_MULT_BOUNDS[0],
        mult_max=_COP_MULT_BOUNDS[1],
    )

    cop_for_draw = float(
        cop_fit.get("cop_median")
        or (prev or {}).get("cop_dhw_median")
        or modelled_cop_dhw(10.0)
    )
    draw_cutoff = (now - timedelta(days=draw_window)).timestamp()
    draw_fit = estimate_evening_draws(
        [r for r in tank_rows if float(r[0]) >= draw_cutoff],
        consumption, boosts,
        tz=tz,
        c_tank_j_per_k=c_tank,
        ua_w_per_k=ua_for_losses,
        t_ambient_c=t_ambient,
        cop_dhw=cop_for_draw,
        min_days=int(getattr(config, "DHW_TANK_LEARN_DRAW_MIN_DAYS", 7)),
    )

    row: dict[str, Any] = dict(prev or {})
    now_iso = now.isoformat()
    if ua_fit.get("status") == "ok":
        row.update(
            ua_w_per_k=float(ua_fit["ua_w_per_k"]),
            tau_hours=float(ua_fit["tau_hours"]),
            ua_r2_median=float(ua_fit["r2_median"]),
            ua_episodes=int(ua_fit["episodes"]),
            ua_window_days=window,
            ua_computed_at=now_iso,
        )
    if cop_fit.get("status") == "ok":
        row.update(
            cop_dhw_median=float(cop_fit["cop_median"]),
            cop_mult=float(cop_fit["cop_mult"]),
            cop_samples=int(cop_fit["samples"]),
            cop_computed_at=now_iso,
        )
    if draw_fit.get("status") == "ok":
        row.update(
            draw_evening_kwh_median=float(draw_fit["draw_kwh_median"]),
            draw_evening_kwh_p75=float(draw_fit["draw_kwh_p75"]),
            draw_days=int(draw_fit["days"]),
            draw_window_days=draw_window,
            draw_computed_at=now_iso,
        )

    result = {
        "status": "ok" if any(
            f.get("status") == "ok" for f in (ua_fit, cop_fit, draw_fit)
        ) else "skipped",
        "ua": ua_fit,
        "cop": cop_fit,
        "draw": draw_fit,
    }
    logger.info(
        "tank_thermal: ua=%s W/K (eps=%s) cop=%s (mult=%s, n=%s) draw_p75=%s kWh — %s",
        _fmt(row.get("ua_w_per_k")), row.get("ua_episodes"),
        _fmt(row.get("cop_dhw_median")), _fmt(row.get("cop_mult")), row.get("cop_samples"),
        _fmt(row.get("draw_evening_kwh_p75")), result["status"],
    )
    return _persist(result, row)


def _persist(result: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    from .. import db

    try:
        db.upsert_dhw_tank_calibration({**row, "last_run_json": result})
    except Exception:  # pragma: no cover — observability must not break the cron
        logger.exception("tank_thermal: upsert failed")
        return {"status": "error", "reason": "upsert failed"}
    return result


def _fmt(v: Any) -> str:
    return f"{float(v):.2f}" if v is not None else "-"


# ---------------------------------------------------------------------------
# Bounded readers (env fallback) — the ONLY surface consumers touch
# ---------------------------------------------------------------------------

_UA_BOUNDS = (0.5, 15.0)
_COP_MULT_BOUNDS = (0.5, 1.5)
_DRAW_BOUNDS = (0.2, 8.0)


def _calibration_row() -> dict[str, Any] | None:
    if not bool(getattr(config, "DHW_TANK_LEARNED_VALUES_ENABLED", True)):
        return None
    from .. import db

    try:
        return db.get_dhw_tank_calibration()
    except Exception:  # noqa: BLE001 — calibration must never break a consumer
        return None


def get_tank_ua_w_per_k() -> float:
    """Learned tank UA when present + in bounds; ``DHW_TANK_UA_W_PER_K`` otherwise."""
    fallback = float(config.DHW_TANK_UA_W_PER_K)
    row = _calibration_row()
    if row is None or row.get("ua_w_per_k") is None:
        return fallback
    ua = float(row["ua_w_per_k"])
    return ua if _UA_BOUNDS[0] <= ua <= _UA_BOUNDS[1] else fallback


def get_dhw_cop_multiplier() -> float:
    """Learned level correction on the DHW COP curve; 1.0 (neutral) otherwise."""
    row = _calibration_row()
    if row is None or row.get("cop_mult") is None:
        return 1.0
    mult = float(row["cop_mult"])
    return mult if _COP_MULT_BOUNDS[0] <= mult <= _COP_MULT_BOUNDS[1] else 1.0


def get_evening_draw_kwh_thermal(*, percentile: str = "p75") -> float | None:
    """Measured evening shower draw (kWh thermal), or None when unlearned —
    the caller then falls back to the litres model in :mod:`src.dhw_demand`.

    ``p75`` sizes comfort (a cold shower costs more than a few pence of heat);
    ``median`` is the honest expected-cost figure.
    """
    row = _calibration_row()
    if row is None:
        return None
    key = "draw_evening_kwh_p75" if percentile == "p75" else "draw_evening_kwh_median"
    val = row.get(key)
    if val is None:
        return None
    draw = float(val)
    return draw if _DRAW_BOUNDS[0] <= draw <= _DRAW_BOUNDS[1] else None
