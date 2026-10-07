"""Weekly fine-tuning review (#832, epic #830) — SUGGESTIONS ONLY.

Replays the last N complete local days (forward mode, the live DB) with each
runtime knob at its current value (the control) and at +/-1 step, scores every
variant with the replay harness's day cost under ACTUAL prices (the same scorer
``scripts/check_lp_regression.py`` uses) plus a comfort read of the replayed
plans, and ranks the result.

HARD RULE: this module NEVER writes a setting. Variants are evaluated through
``lp_overrides.patched_config`` (in-memory, restored on exit — property-backed
runtime keys are cleared, not pinned, #790). The only persistence is the
``tuning_suggestions`` table, each row carrying a PUT-ready payload the human
applies through the normal simulate -> PUT flow.

Comfort yardstick: every variant is judged against the CONTROL's floors (current
night floor, current setpoint - coast delta), not its own — otherwise lowering
the floor would trivially "improve" comfort.

Ranking (``rank_variants``):
  * recommended — saves >= TUNING_REVIEW_MIN_SAVING_PENCE per week AND comfort
    not worse (hours-below up by <= tolerance, no extra shower-shortfall day);
  * trade-off   — saves money but costs comfort;
  * comfort-first — when the control has hours-below > 0, the cheapest variant
    with zero hours-below (annotated on the recommended row if it already is one);
  * everything else is dropped.

Story 3 hook: ``external_comfort_signal(week_start)`` returns None today. It is
called once per review; a non-None dict (owner complaints, sensor-derived
discomfort, ...) is stored under ``payload["external_comfort"]`` of every row.
Story 3 only has to implement that function — ranking code stays untouched.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import db
from .config import config

logger = logging.getLogger(__name__)

SLOT_H = 0.5
COMFORT_TOL_C = 0.1  # predicted-temperature noise below the floor that is not counted
BUDGET_SECONDS = 540.0  # stop starting new variants after this (whole review < ~10 min)
LOCK_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class Knob:
    key: str
    step: float
    lo: float
    hi: float
    kind: str = "float"  # float | int | enum
    enum: tuple[str, ...] = ()


KNOBS: tuple[Knob, ...] = (
    Knob("LP_W3_NIGHT_FLOOR_C", 0.5, 14.0, 22.0),
    Knob("LP_W3_PEAK_COAST_DELTA_C", 0.5, 0.0, 4.0),
    Knob("INDOOR_SETPOINT_C", 0.5, 16.0, 26.0),
    Knob("DHW_TEMP_NORMAL_C", 1.0, 40.0, 50.0),
    Knob("LP_LOAD_EXPENSIVE_BAND_QUANTILE", 0, 0, 0, kind="enum", enum=("p75", "p90")),
    Knob("DHW_DYNAMIC_BOOST_HOLD_HOURS", 1, 1, 4, kind="int"),
)


def external_comfort_signal(week_start: str) -> dict | None:
    """Story-3 plug point: owner/sensor comfort feedback for the week.

    Contract: return ``None`` (no signal) or a JSON-serialisable dict. It is
    attached verbatim to every suggestion payload as ``external_comfort``; the
    ranking rules do not branch on it yet. Must never raise (callers guard)."""
    return None


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------

def _fmt(v: Any) -> Any:
    return round(float(v), 3) if isinstance(v, float) else v


def candidate_variants(current: dict[str, Any]) -> list[dict[str, Any]]:
    """[{key, value}] — each knob at current +/- 1 step (clipped to bounds,
    skipped when it would equal the current value). Enum knob: the other value."""
    out: list[dict[str, Any]] = []
    for k in KNOBS:
        cur = current.get(k.key)
        if cur is None:
            continue
        if k.kind == "enum":
            for v in k.enum:
                if v != str(cur).strip().lower():
                    out.append({"key": k.key, "value": v})
            continue
        for sign in (-1, 1):
            v = float(cur) + sign * k.step
            v = min(max(v, k.lo), k.hi)
            v = int(round(v)) if k.kind == "int" else round(v, 3)
            if abs(float(v) - float(cur)) < 1e-9:
                continue
            out.append({"key": k.key, "value": v})
    return out


def current_values() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in KNOBS:
        try:
            out[k.key] = getattr(config, k.key)
        except Exception as e:  # noqa: BLE001
            logger.warning("tuning_review: cannot read %s: %s", k.key, e)
    return out


# ---------------------------------------------------------------------------
# Scoring one replayed day
# ---------------------------------------------------------------------------

def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")))


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _shower_floor_at_20(preset: str) -> float | None:
    try:
        from .dhw.comfort import shower_windows
        for w in shower_windows(preset=preset):
            if w.start_hour <= 20.0 < w.end_hour:
                return float(w.floor_c)
    except Exception as e:  # noqa: BLE001
        logger.debug("tuning_review: shower_windows failed: %s", e)
    return None


def yardstick() -> dict[str, Any]:
    """Comfort floors taken from the CURRENT config (the control)."""
    return {
        "night_floor_c": float(config.LP_W3_NIGHT_FLOOR_C),
        "peak_floor_c": float(config.INDOOR_SETPOINT_C) - float(config.LP_W3_PEAK_COAST_DELTA_C),
        "night_start_h": int(getattr(config, "LP_W3_NIGHT_START_HOUR_LOCAL", 22)),
        "night_end_h": int(getattr(config, "LP_W3_NIGHT_END_HOUR_LOCAL", 7)),
        "shower_floor_c": _shower_floor_at_20(str(getattr(config, "OPTIMIZATION_PRESET", "normal"))),
    }


def day_comfort(day: Any, yard: dict[str, Any]) -> dict[str, float]:
    """Comfort read of one ``LpDayReplayResult``: hours the PREDICTED indoor
    trajectory sits below the night / peak floors, and the tank shortfall at the
    20:00 shower. Each recalc's plan owns the slots up to the next recalc."""
    tz = _tz()
    starts = [_parse(t) for t in day.recalc_timestamps_utc]
    try:
        d = date.fromisoformat(day.plan_date)
        day_end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(UTC)
    except Exception:  # noqa: BLE001
        day_end = starts[-1] + timedelta(days=1) if starts else datetime.now(UTC)
    bounds = starts + [day_end]
    night_h = peak_h = 0.0
    shower_short = 0.0
    shower_seen = False
    ns, ne = yard["night_start_h"], yard["night_end_h"]
    for k, run in enumerate(day.runs):
        plan = getattr(run, "_replayed_plan", None)
        if plan is None or k >= len(starts):
            continue
        lo, hi = bounds[k], bounds[k + 1]
        ind = list(getattr(plan, "indoor_temp_c", []) or [])
        tank = list(getattr(plan, "tank_temp_c", []) or [])
        band = list(getattr(plan, "price_band", []) or [])
        for i, st in enumerate(plan.slot_starts_utc):
            if not (lo <= st < hi):
                continue
            loc = st.astimezone(tz)
            h = loc.hour
            if i < len(ind):
                t = float(ind[i])
                is_night = (h >= ns or h < ne) if ns > ne else (ns <= h < ne)
                if is_night and t < yard["night_floor_c"] - COMFORT_TOL_C:
                    night_h += SLOT_H
                is_peak = (band[i] == "peak") if band and i < len(band) else (16 <= h < 19)
                if is_peak and t < yard["peak_floor_c"] - COMFORT_TOL_C:
                    peak_h += SLOT_H
            if loc.hour == 20 and loc.minute == 0 and yard.get("shower_floor_c") is not None and i < len(tank):
                shower_seen = True
                shower_short += max(0.0, float(yard["shower_floor_c"]) - float(tank[i]))
    return {
        "night_below_h": night_h,
        "peak_below_h": peak_h,
        "hours_below": night_h + peak_h,
        "shower_short_c": shower_short,
        "shower_day_below": 1.0 if (shower_seen and shower_short > 0.25) else 0.0,
    }


def evaluate(
    overrides: dict[str, Any],
    days: list[str],
    yard: dict[str, Any],
    *,
    cadence: str,
    replay: Callable[..., Any],
) -> dict[str, dict[str, float]]:
    """Per-day {date: {cost_p, hours_below, ...}} for one variant (failed days omitted)."""
    from .scheduler.lp_overrides import patched_config
    out: dict[str, dict[str, float]] = {}
    with patched_config(overrides):
        for d in days:
            try:
                r = replay(d, cadence=cadence, mode="forward")
            except Exception as e:  # noqa: BLE001
                logger.warning("tuning_review: replay %s %s raised: %s", d, overrides, e)
                continue
            if not getattr(r, "ok", False):
                continue
            row = {"cost_p": float(r.total_replayed_cost_p)}
            row.update(day_comfort(r, yard))
            out[d] = row
    return out


def _aggregate(per_day: dict[str, dict[str, float]], days: list[str]) -> dict[str, float]:
    sel = [per_day[d] for d in days if d in per_day]
    keys = ("cost_p", "night_below_h", "peak_below_h", "hours_below", "shower_short_c", "shower_day_below")
    agg = {k: sum(r[k] for r in sel) for k in keys}
    agg["n_days"] = float(len(sel))
    return agg


# ---------------------------------------------------------------------------
# Ranking (pure — unit-tested on synthetic results)
# ---------------------------------------------------------------------------

def rank_variants(
    control: dict[str, float],
    variants: list[dict[str, Any]],
    *,
    min_saving_p: float | None = None,
    tol_h: float | None = None,
) -> list[dict[str, Any]]:
    """``variants``: [{key, value, current_value, cost_p, hours_below, shower_day_below, ...}]
    aggregated over the SAME days as ``control``. Returns rows with ``verdict`` in
    recommended | trade-off | comfort-first, best first. Dropped variants are omitted."""
    min_saving = float(config.TUNING_REVIEW_MIN_SAVING_PENCE if min_saving_p is None else min_saving_p)
    tol = float(config.TUNING_REVIEW_COMFORT_TOLERANCE_H if tol_h is None else tol_h)
    scored: list[dict[str, Any]] = []
    for v in variants:
        dp = float(v["cost_p"]) - float(control["cost_p"])
        dh = float(v["hours_below"]) - float(control["hours_below"])
        ds = float(v.get("shower_day_below", 0.0)) - float(control.get("shower_day_below", 0.0))
        comfort_ok = dh <= tol and ds <= 0.0
        if dp <= -min_saving and comfort_ok:
            verdict = "recommended"
        elif dp <= -min_saving:
            verdict = "trade-off"
        else:
            verdict = "dropped"
        scored.append({**v, "delta_pence_per_week": dp, "delta_comfort_hours": dh,
                       "delta_shower_days": ds, "verdict": verdict})

    # Best variant per (key, verdict): +/- both qualifying keeps the cheaper.
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for s in scored:
        if s["verdict"] == "dropped":
            continue
        k = (s["key"], s["verdict"])
        if k not in best or s["delta_pence_per_week"] < best[k]["delta_pence_per_week"]:
            best[k] = s
    rows = list(best.values())
    # A key already recommended does not also appear as a trade-off.
    rec_keys = {r["key"] for r in rows if r["verdict"] == "recommended"}
    rows = [r for r in rows if not (r["verdict"] == "trade-off" and r["key"] in rec_keys)]

    if float(control["hours_below"]) > 0:
        zero = [s for s in scored if float(s["hours_below"]) <= 1e-9]
        if zero:
            cf = min(zero, key=lambda s: s["delta_pence_per_week"])
            hit = next((r for r in rows if r["key"] == cf["key"] and r["value"] == cf["value"]), None)
            if hit is not None:
                hit["comfort_first"] = True
            else:
                rows.append({**cf, "verdict": "comfort-first", "comfort_first": True})

    order = {"recommended": 0, "trade-off": 1, "comfort-first": 2}
    rows.sort(key=lambda r: (order[r["verdict"]], r["delta_pence_per_week"]))
    return rows


def put_payload(key: str, value: Any) -> dict[str, Any]:
    """PUT-ready payload: ``PUT /api/v1/settings/{key}/simulate`` then
    ``PUT /api/v1/settings/{key}`` with ``X-Simulation-Id`` (same body)."""
    return {
        "simulate": {"method": "PUT", "path": f"/api/v1/settings/{key}/simulate", "body": {"value": value}},
        "apply": {"method": "PUT", "path": f"/api/v1/settings/{key}", "body": {"value": value},
                  "headers": {"X-Simulation-Id": "<simulation_id from the simulate call>"}},
        "body": {"value": value},
    }


# ---------------------------------------------------------------------------
# Single-flight + driver
# ---------------------------------------------------------------------------

class ReviewBusy(RuntimeError):
    """A review is already running (single-flight, 10-min TTL)."""


_state_lock = threading.Lock()
_running_since: float | None = None


def _acquire() -> None:
    global _running_since
    with _state_lock:
        now = time.monotonic()
        if _running_since is not None and now - _running_since < LOCK_TTL_SECONDS:
            raise ReviewBusy("a tuning review is already running")
        _running_since = now


def _release() -> None:
    global _running_since
    with _state_lock:
        _running_since = None


def replayable_days(end_day: date, n_days: int, list_runs: Callable[[str], list] | None = None) -> list[str]:
    if list_runs is None:
        from .scheduler.lp_replay import list_run_ids_for_date
        list_runs = list_run_ids_for_date
    out: list[str] = []
    for i in range(n_days - 1, -1, -1):
        iso = (end_day - timedelta(days=i)).isoformat()
        try:
            if list_runs(iso):
                out.append(iso)
        except Exception as e:  # noqa: BLE001
            logger.debug("tuning_review: list_runs(%s) failed: %s", iso, e)
    return out


def run_review(
    *,
    end_day: date | None = None,
    dry_run: bool = False,
    replay: Callable[..., Any] | None = None,
    list_runs: Callable[[str], list] | None = None,
    cadence: str | None = None,
    budget_seconds: float = BUDGET_SECONDS,
) -> dict[str, Any]:
    """Run the review now. Returns ``{status, week_start, days, rows, ...}``.
    ``dry_run`` skips persistence. Raises :class:`ReviewBusy` when one is running."""
    _acquire()
    try:
        return _run_review_locked(end_day, dry_run, replay, list_runs, cadence, budget_seconds)
    finally:
        _release()


def _run_review_locked(end_day, dry_run, replay, list_runs, cadence, budget_seconds) -> dict[str, Any]:
    t0 = time.monotonic()
    if replay is None:
        from .scheduler.lp_replay import replay_day as replay
    cadence = cadence or str(config.TUNING_REVIEW_CADENCE)
    if end_day is None:
        end_day = datetime.now(_tz()).date() - timedelta(days=1)
    days = replayable_days(end_day, int(config.TUNING_REVIEW_DAYS), list_runs)
    week_start = (end_day - timedelta(days=int(config.TUNING_REVIEW_DAYS) - 1)).isoformat()
    base = {"week_start": week_start, "end_day": end_day.isoformat(), "days": days, "cadence": cadence}
    if len(days) < int(config.TUNING_REVIEW_MIN_DAYS):
        return {**base, "status": "skipped", "reason": f"only {len(days)} replayable day(s)", "rows": []}

    cur = current_values()
    yard = yardstick()
    control_days = evaluate({}, days, yard, cadence=cadence, replay=replay)
    ok_days = [d for d in days if d in control_days]
    if len(ok_days) < int(config.TUNING_REVIEW_MIN_DAYS):
        return {**base, "status": "skipped", "reason": f"control replay ok on {len(ok_days)} day(s)", "rows": []}

    variants: list[dict[str, Any]] = []
    partial = False
    for cand in candidate_variants(cur):
        if time.monotonic() - t0 > budget_seconds:
            partial = True
            break
        per_day = evaluate({cand["key"]: cand["value"]}, ok_days, yard, cadence=cadence, replay=replay)
        if len(per_day) < len(ok_days):  # a failed day would bias the comparison
            continue
        variants.append({**cand, "current_value": cur[cand["key"]], **_aggregate(per_day, ok_days)})

    control = _aggregate(control_days, ok_days)
    ranked = rank_variants(control, variants)
    try:
        ext = external_comfort_signal(week_start)
    except Exception as e:  # noqa: BLE001
        logger.warning("tuning_review: external_comfort_signal failed: %s", e)
        ext = None

    rows: list[dict[str, Any]] = []
    for r in ranked:
        rows.append({
            "week_start": week_start,
            "key": r["key"],
            "current_value": _fmt(r["current_value"]),
            "suggested_value": _fmt(r["value"]),
            "delta_pence_per_week": round(r["delta_pence_per_week"], 2),
            "delta_comfort_hours": round(r["delta_comfort_hours"], 2),
            "verdict": r["verdict"],
            "payload": {
                **put_payload(r["key"], r["value"]),
                "n_days": int(control["n_days"]),
                "cost_gbp_current": round(control["cost_p"] / 100.0, 3),
                "cost_gbp_suggested": round(r["cost_p"] / 100.0, 3),
                "hours_below_current": round(control["hours_below"], 2),
                "hours_below_suggested": round(r["hours_below"], 2),
                "shower_days_below_current": int(control["shower_day_below"]),
                "shower_days_below_suggested": int(r["shower_day_below"]),
                "comfort_first": bool(r.get("comfort_first")),
                "external_comfort": ext,
            },
        })
    elapsed = time.monotonic() - t0
    result = {**base, "status": "partial" if partial else "ok", "rows": rows,
              "control": {k: round(v, 3) for k, v in control.items()},
              "n_variants": len(variants),
              "evaluated": [{"key": v["key"], "value": _fmt(v["value"]),
                             "delta_pence_per_week": round(v["cost_p"] - control["cost_p"], 2),
                             "delta_comfort_hours": round(v["hours_below"] - control["hours_below"], 2)}
                            for v in variants], "elapsed_seconds": round(elapsed, 1), "dry_run": dry_run}
    if not dry_run and rows:
        db.save_tuning_suggestions(rows)
    return result


def summary_lines(rows: list[dict[str, Any]], limit: int = 3) -> list[str]:
    lines = []
    for r in rows:
        if r["verdict"] != "recommended":
            continue
        lines.append(
            f"{r['key']}: {r['current_value']} -> {r['suggested_value']} "
            f"({r['delta_pence_per_week']:+.0f} p/semana, conforto {r['delta_comfort_hours']:+.1f} h)"
        )
        if len(lines) >= limit:
            break
    return lines


def weekly_review_job() -> None:
    """APScheduler entry (Sunday 09:00 local). Never applies anything; muted
    Telegram when nothing is recommended. Best-effort."""
    if not config.TUNING_REVIEW_ENABLED:
        return
    try:
        res = run_review()
    except ReviewBusy:
        logger.info("tuning_review: skipped (already running)")
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("tuning_review failed (non-fatal): %s", e)
        return
    logger.info("tuning_review: status=%s rows=%d elapsed=%s", res.get("status"),
                len(res.get("rows", [])), res.get("elapsed_seconds"))
    lines = summary_lines(res.get("rows", []))
    if not lines:
        return
    try:
        from .notifier import notify_tuning_review
        notify_tuning_review(lines, week_start=res["week_start"], n_recommended=len(lines))
    except Exception as e:  # noqa: BLE001
        logger.warning("tuning_review notify failed (non-fatal): %s", e)
