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
* **the DHW COP is badly wrong** — the clean warmups measure 2.55–2.62 while the
  LP's curve claims **4.70** at the same outdoor temperature. The LP believes tank
  heat is nearly twice as cheap as it is. This is the decision-relevant error, and
  it is invisible today only because the K2 pin makes ``e_dhw`` exogenous.
* **the hot water goes in the MORNING**, not the evening. The draw profile peaks
  in the 08:00–10:00 bucket (the tank visibly falls 41 → 37 °C around 09:00) with a
  second, smaller peak at 20:00–22:00. ``dhw_demand``'s model — four evening
  showers, one in the morning — has it backwards, and the fixed schedule warms the
  tank at 13:00 for showers that largely already happened.

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
* **Draw profile (kWh thermal per local 2 h bucket)** — how much heat the
  household actually takes out, and WHEN, from a closed energy balance over each
  bucket (the tank's temperature drop UNDERSTATES the draw whenever the firmware
  reheats mid-draw, so the measured reheat energy is added back). The LP's
  litres-based model (``dhw_demand``) stays the fallback.

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

import json
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
    c_tank_j_per_k: float = 837_200.0,
    ua_prior_w_per_k: float = 2.5,
    gap_drop_tolerance: float = 2.0,
    quantisation_c: float = 1.5,
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
            gap_h = gap_min / 60.0
            drop = cur[-1][1] - temp
            rate = drop / max(gap_h, 1e-6)
            is_draw = drop >= draw_drop_c and rate > draw_drop_rate_c_per_h
            # A draw hidden INSIDE a long gap is the one contaminant the bucket
            # blocks cannot see: drawing hot water burns no electricity, so it
            # leaves no trace in the energy counter. Across a 6 h hole the rate
            # test alone is toothless (it would need a 12 °C fall to fire) while
            # the honest decay is only ~3 °C — so a quiet 2-3 °C night-time draw
            # would sail through and DOUBLE the fitted UA. Gate the step against
            # what the prior physics says the tank can even lose: more than
            # ``gap_drop_tolerance`` times the expected coast (plus a degree of
            # quantisation slack) is not decay, whatever the average rate says.
            expected = (
                ua_prior_w_per_k * max(cur[-1][1] - t_ambient_c, 0.0)
                * gap_h * 3600.0 / max(c_tank_j_per_k, 1e-9)
            )
            implausible = drop > gap_drop_tolerance * expected + quantisation_c
            if gap_min > max_gap_minutes or is_draw or implausible:
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
    edge_tolerance_minutes: float = 60.0,
    settle_hours: float = 1.0,
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

    def _temp_before(ts: datetime) -> float | None:
        """Last sample AT OR BEFORE ``ts`` (within tolerance) — the tank's
        temperature when the run's energy started flowing.

        Direction matters. A nearest-in-any-direction lookup happily returns a
        sample 40 min INSIDE a 2 h warmup, by which point the tank has already
        climbed ~2.7 °C: the observed rise shrinks, and the COP it implies falls
        by a third. That biases the measurement in exactly the direction of this
        module's headline finding, which is the last place a lazy lookup is
        acceptable. The preceding bucket is known-quiet, so a sample slightly
        before the boundary is a safe stand-in for the boundary itself."""
        best: tuple[float, float] | None = None
        for t, v in series:
            if t > ts:
                break
            gap_min = (ts - t).total_seconds() / 60.0
            if best is None or gap_min < best[0]:
                best = (gap_min, v)
        if best is None or best[0] > edge_tolerance_minutes:
            return None
        return best[1]

    def _peak_after(ts: datetime, settle: timedelta) -> tuple[datetime, float] | None:
        """PEAK sample in ``[ts, ts + settle]`` — where the run's heat ended up,
        and when.

        The settle tail is the same physics the decay selector respects: the
        plate exchanger keeps handing heat to the tank after the compressor
        stops. Sampling exactly at the bucket edge throws that tail away, and
        since the NEXT bucket is known-quiet, any rise inside the tail is THIS
        event's heat. Take the peak, not the nearest sample."""
        window = [(t, v) for t, v in series if ts <= t <= ts + settle]
        if not window:
            return None
        if (window[0][0] - ts).total_seconds() / 60.0 > edge_tolerance_minutes:
            return None
        return max(window, key=lambda p: (p[1], -p[0].timestamp()))

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

        t_start = _temp_before(start)
        peak = _peak_after(end, timedelta(hours=settle_hours))
        if t_start is None or peak is None:
            continue
        peak_ts, t_end = peak
        if (t_end - t_start) < min_rise_c:
            continue
        inner = [(ts, v) for ts, v in series if start < ts < peak_ts]
        pts = [(start, t_start)] + inner + [(peak_ts, t_end)]
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
            end_utc=peak_ts,
            points=[((ts - start).total_seconds() / 3600.0, v) for ts, v in dedup],
            kwh_dhw=sum(k for _, _, k in run),
            t_outdoor_c=_outdoor_at(start, peak_ts),
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
    temps: list[float] = []
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
                temps.append(float(ev.t_outdoor_c))
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
    clamped = max(mult_min, min(mult_max, mult))
    if abs(clamped - mult) > 1e-9:
        # A clamp that binds is a silent symptom, not a fix — say so out loud.
        logger.warning(
            "tank_thermal: measured COP ratio %.3f clamped to %.3f (bounds %.2f–%.2f) "
            "— the curve and reality are further apart than the bounds allow",
            mult, clamped, mult_min, mult_max,
        )
    temps.sort()
    return {
        "status": "ok",
        "cop_median": cop_med,
        # Dispersion, persisted: the median of a handful of events is fragile,
        # and the NEXT reader (the LP) deserves to see the spread rather than
        # inherit a point estimate with no error bars.
        "cop_p25": float(cops[max(0, int(round(0.25 * (len(cops) - 1))))]),
        "cop_p75": float(cops[min(len(cops) - 1, int(round(0.75 * (len(cops) - 1))))]),
        "cop_mult": clamped,
        "cop_mult_raw": mult,
        # The outdoor band the multiplier was MEASURED over. A flat multiplier
        # fitted in July does not necessarily hold in January (the curve's error
        # is a function of the DHW-vs-space supply-temperature lift, which moves
        # with T_out), and #540 is a WINTER epic. Recording the band is what lets
        # the consumer refuse to extrapolate far outside it.
        "cop_t_outdoor_median": float(temps[len(temps) // 2]),
        "cop_t_outdoor_min": float(temps[0]),
        "cop_t_outdoor_max": float(temps[-1]),
        "samples": len(cops),
        "samples_rejected": rejected,
    }


# ---------------------------------------------------------------------------
# Evening draw — how much heat the showers actually remove
# ---------------------------------------------------------------------------


def estimate_draw_profile(
    tank_rows: list[tuple[float, float]],
    consumption_rows: list[dict[str, Any]],
    boost_windows: list[tuple[str, str]],
    *,
    tz: ZoneInfo,
    c_tank_j_per_k: float,
    ua_w_per_k: float,
    t_ambient_c: float,
    cop_dhw: float,
    min_days: int = 7,
    max_draw_kwh: float = 8.0,
    edge_tolerance_minutes: float = 60.0,
) -> dict[str, Any]:
    """PURE energy-balance estimate of hot-water draw, per LOCAL 2 h bucket.

    For each bucket the balance is CLOSED — everything entering or leaving the
    tank is either measured or modelled, so the draw is what's left:

        Q_draw = Q_reheat·COP − Q_standing − C·(T_end − T_start)

    Working per bucket (rather than over one hand-picked "evening" window) is
    what makes this both correct and useful:

    * **correct** — the energy counter only resolves 2 h buckets, so a balance
      whose ΔT window doesn't coincide with a bucket edge charges the draw for
      heat that is already inside ``T_start``. Measuring peak-to-trough did
      exactly that and inflated the answer several-fold (a 3.5 kWh p75 against a
      ~1 kWh median). Pinned to bucket edges, every reported kWh belongs to the
      balance by construction and nothing is attributed sub-bucket.
    * **useful** — the LP needs to know WHEN the hot water goes, not just how
      much. And it is not where the model assumes: ``dhw_demand`` is built around
      4 evening showers, but this household's tank visibly drops around 08:30–
      09:30 (41 → 37 °C) while most evenings barely move it. A profile finds that;
      an evening-window estimator would have measured a small number and called
      it the answer.

    Reheat is ADDED BACK because the firmware routinely tops the tank up DURING a
    drawdown: the temperature drop alone then understates the heat drawn — and
    that reheat, when it lands at peak price, is exactly what the LP is being
    asked to move, so measuring it low would hide the prize.

    Buckets overlapping a boost/override are skipped, as is one whose counter is
    NULL (missing ≠ zero) or which lacks a temperature sample near either edge.
    Per bucket, returns the median and p75 across days — the median is the
    expected-cost input, the p75 sizes comfort (a cold shower costs more than a
    few pence of heat). Negative residuals are clamped to zero: they are
    quantisation noise (1 °C ≈ 0.23 kWh on a 200 L tank), not water flowing
    backwards.
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

    def _temp_at_boundary(ts: datetime) -> float | None:
        """Tank temperature at a bucket edge — nearest sample within tolerance.
        Symmetric (unlike the COP fit's edges): both ends of a CLOSED balance are
        plain state readings, and an error at either enters ΔT linearly rather
        than truncating a rise."""
        near = [(abs((t - ts).total_seconds()) / 60.0, v) for t, v in series]
        if not near:
            return None
        gap, val = min(near)
        return val if gap <= edge_tolerance_minutes else None

    per_bucket: dict[int, list[float]] = {b: [] for b in range(12)}
    per_day_total: dict[date, float] = {}
    per_day_buckets: dict[date, int] = {}
    days_seen: set[date] = set()
    buckets_skipped = 0
    for day in sorted(by_day):
        for b in range(12):
            b_start, b_end = _bucket_window_utc(day, b, tz)
            if any(ws < b_end and b_start < we for ws, we in boosts):
                buckets_skipped += 1
                continue
            k = kwh_by_key.get((day.isoformat(), b), "missing")
            if k == "missing" or k is None:
                buckets_skipped += 1  # missing ≠ zero
                continue
            t_start = _temp_at_boundary(b_start)
            t_end = _temp_at_boundary(b_end)
            if t_start is None or t_end is None:
                buckets_skipped += 1
                continue
            inner = [
                ((ts - b_start).total_seconds() / 3600.0, v)
                for ts, v in series
                if b_start < ts < b_end
            ]
            window = sorted(
                [(0.0, t_start)] + inner
                + [((b_end - b_start).total_seconds() / 3600.0, t_end)]
            )
            standing_j = _standing_loss_j(
                window, ua_w_per_k=ua_w_per_k, t_ambient_c=t_ambient_c
            )
            q_draw_kwh = (
                float(k) * cop_dhw * _J_PER_KWH
                - standing_j
                - c_tank_j_per_k * (t_end - t_start)
            ) / _J_PER_KWH
            if q_draw_kwh > max_draw_kwh:
                buckets_skipped += 1  # not a draw — something unmodelled happened
                continue
            draw = max(0.0, q_draw_kwh)
            per_bucket[b].append(draw)
            per_day_total[day] = per_day_total.get(day, 0.0) + draw
            per_day_buckets[day] = per_day_buckets.get(day, 0) + 1
            days_seen.add(day)

    if len(days_seen) < min_days:
        return {
            "status": "skipped",
            "reason": f"only {len(days_seen)} usable day(s); need >= {min_days}",
            "days": len(days_seen),
            "buckets_skipped": buckets_skipped,
        }

    def _pct(vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return float(s[min(len(s) - 1, int(round(q * (len(s) - 1))))])

    median = [_pct(per_bucket[b], 0.5) for b in range(12)]
    p75 = [_pct(per_bucket[b], 0.75) for b in range(12)]
    # The daily figures come from per-DAY totals, not from summing the per-bucket
    # percentiles. Σ p75(bucket) is an upper bound on the p75 of the daily total,
    # not an estimate of it — no single day is simultaneously at its 75th
    # percentile in all twelve buckets. (Measured here: Σp75 = 3.5 kWh against a
    # true daily p75 nearer 2.) Only whole days are comparable, so days missing
    # buckets are excluded from the daily stat while still feeding the shape.
    full_days = [t for d, t in per_day_total.items() if per_day_buckets.get(d) == 12]
    return {
        "status": "ok",
        "profile_kwh_median": median,
        "profile_kwh_p75": p75,
        "samples_per_bucket": [len(per_bucket[b]) for b in range(12)],
        "daily_kwh_median": _pct(full_days, 0.5),
        "daily_kwh_p75": _pct(full_days, 0.75),
        "daily_full_days": len(full_days),
        "days": len(days_seen),
        "buckets_skipped": buckets_skipped,
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
    # The COP needs its OWN, much longer window. Clean heat events are rare —
    # a run of DHW-active buckets fenced by KNOWN-quiet ones — and prod yields
    # roughly one every five days. At the τ/UA window (21 d) the sample gate
    # could never be met, so the component would be dead code, silently leaving
    # the LP on a curve this module has measured to be ~2× optimistic. The COP
    # also drifts far more slowly than the weather, so a long window is honest.
    cop_window = int(getattr(config, "DHW_TANK_LEARN_COP_WINDOW_DAYS", 60))
    start = now - timedelta(days=max(window, cop_window))
    start_day, end_day = start.date(), now.date()

    c_tank = float(config.DHW_TANK_LITRES) * float(config.DHW_WATER_CP)
    t_ambient = float(config.INDOOR_SETPOINT_C)

    prev = None
    try:
        prev = db.get_dhw_tank_calibration()
    except Exception:  # pragma: no cover
        pass

    try:
        tank_rows = db.get_tank_temps_range(start.timestamp(), now.timestamp())
    except Exception:  # pragma: no cover - defensive
        logger.exception("tank_thermal: telemetry read failed")
        return {"status": "error", "reason": "telemetry read failed"}
    if not tank_rows:
        # Preserve every learned component. A telemetry outage is not evidence
        # about the tank's physics, and this row is about to drive the LP.
        return _persist(
            {"status": "skipped", "reason": "no live tank telemetry"}, dict(prev or {})
        )

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
    ua_cutoff = (now - timedelta(days=window)).timestamp()
    ua_rows = [r for r in tank_rows if float(r[0]) >= ua_cutoff]

    episodes = select_tank_decay_episodes(
        ua_rows, consumption, boosts,
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
        c_tank_j_per_k=c_tank,
        # Bootstrap the plausibility gate from what we already believe: the
        # previously learned UA if we have one, else the env constant. It only
        # has to be the right order of magnitude to catch a hidden draw.
        ua_prior_w_per_k=float(
            (prev or {}).get("ua_w_per_k") or config.DHW_TANK_UA_W_PER_K
        ),
        gap_drop_tolerance=float(getattr(config, "DHW_TANK_LEARN_GAP_DROP_TOLERANCE", 2.0)),
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
        settle_hours=float(getattr(config, "DHW_TANK_LEARN_SETTLE_HOURS", 1.0)),
        legionella=leg,
    )
    cop_fit = fit_dhw_cop(
        events,
        c_tank_j_per_k=c_tank,
        ua_w_per_k=ua_for_losses,
        t_ambient_c=t_ambient,
        min_samples=int(getattr(config, "DHW_TANK_LEARN_COP_MIN_SAMPLES", 4)),
        mult_min=_COP_MULT_BOUNDS[0],
        mult_max=_COP_MULT_BOUNDS[1],
    )

    # Converting the draw window's reheat kWh back to heat needs a COP — and it
    # must NOT be the parametric curve. That curve is the thing this module has
    # measured to be ~2× optimistic for DHW, so using it here would inflate every
    # reheat (and therefore every draw) by the very error we are trying to
    # correct. Measured (this run, then the stored one), else an explicit
    # measured-grade constant.
    cop_for_draw = float(
        cop_fit.get("cop_median")
        or (prev or {}).get("cop_dhw_median")
        or getattr(config, "DHW_MEASURED_COP_FALLBACK", 2.5)
    )
    draw_cutoff = (now - timedelta(days=draw_window)).timestamp()
    draw_fit = estimate_draw_profile(
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
            cop_dhw_p25=float(cop_fit["cop_p25"]),
            cop_dhw_p75=float(cop_fit["cop_p75"]),
            cop_mult=float(cop_fit["cop_mult"]),
            cop_mult_raw=float(cop_fit["cop_mult_raw"]),
            cop_t_outdoor_median=float(cop_fit["cop_t_outdoor_median"]),
            cop_t_outdoor_min=float(cop_fit["cop_t_outdoor_min"]),
            cop_t_outdoor_max=float(cop_fit["cop_t_outdoor_max"]),
            cop_samples=int(cop_fit["samples"]),
            cop_window_days=cop_window,
            cop_computed_at=now_iso,
        )
    if draw_fit.get("status") == "ok":
        row.update(
            draw_profile_median_json=json.dumps(draw_fit["profile_kwh_median"]),
            draw_profile_p75_json=json.dumps(draw_fit["profile_kwh_p75"]),
            draw_days=int(draw_fit["days"]),
            draw_window_days=draw_window,
            draw_computed_at=now_iso,
        )
        # The daily TOTAL only publishes off whole days (all 12 buckets present),
        # and only once there are enough of them. Summing the per-bucket
        # percentiles instead would be a bound, not an estimate. A thin run of
        # complete days is worse than none: the shape is what the LP needs, and
        # it survives partial days perfectly well.
        if int(draw_fit.get("daily_full_days", 0)) >= int(
            getattr(config, "DHW_TANK_LEARN_DRAW_MIN_DAYS", 7)
        ):
            row.update(
                draw_daily_kwh_median=float(draw_fit["daily_kwh_median"]),
                draw_daily_kwh_p75=float(draw_fit["daily_kwh_p75"]),
                draw_full_days=int(draw_fit["daily_full_days"]),
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


def get_draw_profile_kwh_thermal(*, percentile: str = "p75") -> list[float] | None:
    """Measured hot-water draw per LOCAL 2 h bucket (12 values, kWh thermal), or
    None when unlearned — the caller then falls back to the litres/showers model
    in :mod:`src.dhw_demand`.

    ``p75`` sizes comfort (a cold shower costs more than a few pence of heat);
    ``median`` is the honest expected-cost figure. Read the SHAPE, not just the
    total: this household draws its hot water in the MORNING, which is not what
    ``dhw_demand``'s four-evening-showers model assumes.
    """
    row = _calibration_row()
    if row is None:
        return None
    key = "draw_profile_p75_json" if percentile == "p75" else "draw_profile_median_json"
    raw = row.get(key)
    if not raw:
        return None
    try:
        profile = [float(v) for v in json.loads(raw)]
    except (TypeError, ValueError):
        return None
    if len(profile) != 12 or any(v < 0 for v in profile):
        return None
    total = sum(profile)
    return profile if _DRAW_BOUNDS[0] <= total <= _DRAW_BOUNDS[1] else None


def get_daily_draw_kwh_thermal(*, percentile: str = "p75") -> float | None:
    """Total measured daily draw (kWh thermal), or None when unlearned.

    Read from the stored whole-day statistic — NOT by summing the profile. The
    sum of per-bucket p75s is an upper bound on a day's p75, not an estimate of
    it: no day sits at its 75th percentile in all twelve buckets at once.
    """
    row = _calibration_row()
    if row is None:
        return None
    key = "draw_daily_kwh_p75" if percentile == "p75" else "draw_daily_kwh_median"
    val = row.get(key)
    if val is None:
        return None
    draw = float(val)
    return draw if _DRAW_BOUNDS[0] <= draw <= _DRAW_BOUNDS[1] else None
