"""Closed-loop auto-tune (#876), story 3: the JUDGE.

Two levels, both acting on the single ``evaluating`` ledger row (``src/auto_tune.py``):

* **Tripwires** (daily, right after the Cosy scorecard): a cheap, data-only check
  per key class. A tripped wire rolls the change back at once through
  ``auto_tune.rollback`` (restore, freeze, one notification, ``action_log``).
* **Day-7 verdict**: once 7 complete local days have been scored after the apply
  day, replay those days twice (OLD value vs NEW value, same prices / weather /
  load / process config, forward mode) and KEEP or ROLLBACK on the paired result.

Why BOTH arms are replayed instead of comparing the new value's replay with the
raw realised cost: the realised cost carries execution noise (device lag, Fox /
Daikin write failures, forecast-revision replans, manual gestures) that the
replay does not, so mixing a realised arm with a replayed arm bakes that
difference into the verdict. Two replays that differ ONLY in the knob are a
paired comparison; the harness does not claim to reproduce the committed plans.

Comfort yardstick: both arms are scored against the OLD value's floors (the
policy the owner lived with before the change), otherwise lowering a floor would
trivially "improve" comfort.

Nothing here applies a setting; the only writes are ``auto_tune.rollback`` /
``keep`` and a deferral counter in ``evidence_json``. Both jobs are single-flight
and never raise.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import auto_tune, db
from .config import config

logger = logging.getLogger(__name__)

VERDICT_DAYS = 7
MIN_VERDICT_DAYS = 4          # fewer common replayed days than this -> defer
MAX_DEFERRALS = 3             # after this many deferrals -> keep as undecidable
COLD_WINDOW_DAYS = 3
COLD_TRIP_COUNT = 2
RESID_WORSE_RATIO = 1.5       # "worse than baseline by > 50 %"
RESID_BASELINE_FLOOR_C = 0.1  # a near-perfect baseline must not make noise trip the wire
RESID_BASELINE_MIN_DAYS = 3

# Key classes (direction derived from old/new in ``classify``).
COMFORT_LOOSEN_DOWN = ("LP_W3_NIGHT_FLOOR_C", "INDOOR_SETPOINT_C", "LP_W3_CEILING_C")
COMFORT_LOOSEN_UP = ("LP_W3_PEAK_COAST_DELTA_C",)
DHW_KEYS = ("DHW_TEMP_NORMAL_C", "DHW_TEMP_COMFORT_C")
PLAN_PATTERN_KEYS = ("LP_LOAD_EXPENSIVE_BAND_QUANTILE",)
LEARNED_KEYS = ("LP_W3_SOLAR_GAIN_KW_PER_PV_KW", "LP_W3_INTERNAL_GAIN_KW", "LP_W3_EMITTER_TAU_MIN")

_job_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def peak_trip_kwh() -> float:
    return _envf("AUTO_TUNE_PEAK_TRIP_KWH", 1.0)


def min_gain_pence() -> float:
    return _envf("AUTO_TUNE_MIN_GAIN_PENCE", 5.0)


def verdict_budget_seconds() -> float:
    return _envf("AUTO_TUNE_VERDICT_BUDGET_SECONDS", 1800.0)


def notify_kept() -> bool:
    return os.getenv("AUTO_TUNE_NOTIFY_KEPT", "false").strip().lower() in ("1", "true", "yes", "on")


def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")))


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _local_day(t: datetime) -> date:
    return t.astimezone(_tz()).date()


def _applied(change: dict[str, Any]) -> datetime:
    t = auto_tune._parse(change.get("applied_at"))
    if t is None:
        raise ValueError("change has no applied_at")
    return t


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _log(action: str, params: dict[str, Any], result: str = "success") -> None:
    auto_tune._log(action, params, result)


def _away_days(rows: list[dict[str, Any]]) -> set[str]:
    """Scorecard dates flagged holiday / vacation (same truthiness as
    ``auto_tune.baseline_metrics``)."""
    out: set[str] = set()
    for r in rows:
        p = r.get("payload") or {}
        if p.get("holiday") or p.get("vacation"):
            out.add(str(r["date"]))
    return out


def _cards(limit: int = 90) -> list[dict[str, Any]]:
    return db.get_cosy_scorecards(limit)


def _is_today_away(day: date, today: date) -> bool:
    return day == today and auto_tune._is_away()


# ----------------------------------------------------------- classification
def classify(change: dict[str, Any]) -> str:
    """``comfort_loosen | dhw_loosen | plan_pattern | learned | tighten | none``
    from the key and the direction of ``old_value -> new_value``."""
    key = str(change.get("key") or "").upper()
    old, new = _f(change.get("old_value")), _f(change.get("new_value"))
    if key in PLAN_PATTERN_KEYS:
        return "plan_pattern"
    if key in LEARNED_KEYS:
        return "learned"
    if old is None or new is None:
        return "none"
    if key in COMFORT_LOOSEN_DOWN:
        return "comfort_loosen" if new < old else "tighten"
    if key in COMFORT_LOOSEN_UP:
        return "comfort_loosen" if new > old else "tighten"
    if key in DHW_KEYS:
        return "dhw_loosen" if new < old else "tighten"
    return "none"


# --------------------------------------------------------------- tripwires
def _cold_feedback(since: datetime, until: datetime, away: set[str], today: date) -> list[datetime]:
    iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[datetime] = []
    for r in db.get_comfort_feedback(since_utc=iso):
        if str(r.get("verdict")) != "cold":
            continue
        t = auto_tune._parse(r.get("at_utc"))
        if t is None or t < since or t > until:
            continue
        d = _local_day(t)
        if d.isoformat() in away or _is_today_away(d, today):
            continue
        out.append(t)
    return sorted(out)


def _events(action: str, since: datetime, until: datetime, away: set[str], today: date,
            *, fired_only: bool = False) -> list[dict[str, Any]]:
    out = []
    for r in db.get_action_logs(device="daikin", action=action, limit=500):
        t = auto_tune._parse(r.get("timestamp"))
        if t is None or t < since or t > until:
            continue
        if fired_only and str(r.get("result")) != "ok":
            continue
        d = _local_day(t)
        if d.isoformat() in away or _is_today_away(d, today):
            continue
        out.append({"at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "params": r.get("params") or {}})
    return out


def _resid_err(payload: dict[str, Any]) -> float | None:
    """Mean ABS ``pred_resid`` (deg C) of one ``lwt_learning_daily`` day: the
    day and night means weighted by their slot counts."""
    pr = payload.get("pred_resid") if isinstance(payload, dict) else None
    if not isinstance(pr, dict):
        return None
    num = den = 0.0
    for part in ("day", "night"):
        seg = pr.get(part)
        if not isinstance(seg, dict):
            continue
        m, n = _f(seg.get("mean_c")), _f(seg.get("n"))
        if m is None or not n:
            continue
        num += abs(m) * n
        den += n
    return num / den if den else None


def _evaluate_tripwires(change: dict[str, Any], now: datetime) -> tuple[dict[str, Any] | None, list[str]]:
    notes: list[str] = []
    if change.get("source") == "manual" or change.get("status") != "evaluating":
        return None, ["not_evaluating"]
    cls = classify(change)
    if cls in ("tighten", "none"):
        return None, [f"no_tripwire_for_class:{cls}"]
    t0 = _applied(change)
    apply_day = _local_day(t0)
    today = _local_day(now)
    cards = _cards()
    away = _away_days(cards)

    if cls == "comfort_loosen":
        cold = _cold_feedback(t0, now, away, today)
        for i in range(len(cold)):
            win = [c for c in cold[i:] if c - cold[i] <= timedelta(days=COLD_WINDOW_DAYS)]
            if len(win) >= COLD_TRIP_COUNT:
                return {"name": "cold_feedback", "evidence": {
                    "cold_feedback_at": [c.strftime("%Y-%m-%dT%H:%M:%SZ") for c in win],
                    "window_days": COLD_WINDOW_DAYS}}, notes
        emerg = _events("lwt_emergency", t0, now, away, today)
        if emerg:
            return {"name": "lwt_emergency", "evidence": {"events": emerg[:5], "n": len(emerg)}}, notes
        bk = _events("lwt_comfort_backstop", t0, now, away, today, fired_only=True)
        if bk:
            return {"name": "comfort_backstop", "evidence": {"events": bk[:5], "n": len(bk)}}, notes
        return None, notes

    # Scorecard-day based classes: complete local days AFTER the apply day.
    post = [r for r in cards if date.fromisoformat(r["date"]) > apply_day and r["date"] not in away]
    if cls == "dhw_loosen":
        for r in sorted(post, key=lambda r: r["date"]):
            tank = (r.get("payload") or {}).get("tank")
            if isinstance(tank, dict) and tank.get("any_below_floor") is True:
                return {"name": "shower_floor", "evidence": {"date": r["date"]}}, notes
        return None, notes
    if cls == "plan_pattern":
        lim = peak_trip_kwh()
        for r in sorted(post, key=lambda r: r["date"]):
            v = _f(r.get("peak_import_kwh"))
            if v is not None and v > lim:
                return {"name": "peak_import", "evidence": {
                    "date": r["date"], "peak_import_kwh": round(v, 3), "limit_kwh": lim}}, notes
        return None, notes
    if cls == "learned":
        rows = db.get_lwt_learning_daily(60)
        errs = {str(r["date"]): _resid_err(r.get("payload") or {}) for r in rows}
        base = [e for d, e in errs.items() if e is not None and d not in away
                and 1 <= (apply_day - date.fromisoformat(d)).days <= 7]
        if len(base) < RESID_BASELINE_MIN_DAYS:
            return None, ["no_pred_resid_baseline"]
        baseline = max(sum(base) / len(base), RESID_BASELINE_FLOOR_C)
        bad_prev: str | None = None
        for d in sorted(d for d in errs if date.fromisoformat(d) > apply_day and d not in away):
            e = errs[d]
            if e is not None and e > baseline * RESID_WORSE_RATIO:
                if bad_prev and (date.fromisoformat(d) - date.fromisoformat(bad_prev)).days == 1:
                    return {"name": "pred_resid", "evidence": {
                        "days": [bad_prev, d], "baseline_abs_c": round(baseline, 3),
                        "errors_abs_c": [round(errs[bad_prev], 3), round(e, 3)],
                        "ratio_limit": RESID_WORSE_RATIO}}, notes
                bad_prev = d
            else:
                bad_prev = None
        return None, notes
    return None, notes


def tripwire_check(change: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """The tripped wire (``{"name", "evidence"}``) for an ``evaluating`` change, or
    None. Pure given the data readers (scorecards, comfort feedback, action_log,
    lwt_learning_daily); never raises."""
    try:
        return _evaluate_tripwires(change, _now(now))[0]
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune_judge.tripwire_check failed", exc_info=True)
        return None


def _record_check(change_id: int, now: datetime, trip: dict[str, Any] | None, notes: list[str]) -> None:
    try:
        db.set_kv(f"auto_tune_tripwire_{change_id}", json.dumps({
            "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tripped": trip["name"] if trip else None, "notes": notes}))
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune_judge: kv write failed", exc_info=True)


def scorecard_fields(change: dict[str, Any]) -> dict[str, Any]:
    """``tripwire_checked`` (ISO of the last check) / ``tripped`` (wire name) for the
    scorecard's ``auto_tune.active_change``. Reflects the state at build time."""
    out: dict[str, Any] = {"tripwire_checked": None, "tripped": None}
    try:
        raw = db.get_kv(f"auto_tune_tripwire_{change['id']}")
        if raw:
            d = json.loads(raw)
            out["tripwire_checked"] = d.get("checked_at")
            out["tripped"] = d.get("tripped")
        reason = str((change.get("evidence") or {}).get("reason") or "")
        if change.get("status") == "rolled_back" and reason.startswith("tripwire:"):
            out["tripped"] = reason.split(":", 1)[1]
    except Exception:  # noqa: BLE001
        pass
    return out


def _active_change() -> tuple[dict[str, Any] | None, str]:
    if not auto_tune.enabled():
        ch = db.get_active_auto_tune_change()
        if ch is not None:
            _log("auto_tune_judge_disabled", {"id": ch["id"], "key": ch["key"]}, "skipped")
        return None, "disabled"
    ch = db.get_active_auto_tune_change()
    if ch is None:
        return None, "no_change"
    if ch.get("source") == "manual":
        return None, "manual"
    return ch, "ok"


def tripwire_job(now: datetime | None = None) -> dict[str, Any]:
    """Daily job (scorecard time + 10 min). Never raises."""
    if not _job_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _tripwire_locked(_now(now))
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune_tripwires failed (non-fatal)", exc_info=True)
        return {"status": "error"}
    finally:
        _job_lock.release()


def _tripwire_locked(now: datetime) -> dict[str, Any]:
    ch, why = _active_change()
    if ch is None:
        return {"status": why}
    trip, notes = _evaluate_tripwires(ch, now)
    _record_check(ch["id"], now, trip, notes)
    if trip is None:
        return {"status": "ok", "id": ch["id"], "tripped": None, "notes": notes}
    res = auto_tune.rollback(ch["id"], reason=f"tripwire:{trip['name']}",
                             evidence={**trip["evidence"], "tripwire": trip["name"]}, actor="judge")
    _log("auto_tune_tripwire", {"id": ch["id"], "key": ch["key"], "tripwire": trip["name"],
                                "rollback": res})
    return {"status": "tripped", "id": ch["id"], "tripped": trip["name"], "rollback": res}


# ----------------------------------------------------------------- verdict
def _verdict_window(change: dict[str, Any]) -> tuple[date, date]:
    apply_day = _local_day(_applied(change))
    return apply_day + timedelta(days=1), apply_day + timedelta(days=VERDICT_DAYS)


def due(change: dict[str, Any], now: datetime) -> bool:
    """7 complete local days exist after the apply day."""
    return _local_day(now) - timedelta(days=1) >= _verdict_window(change)[1]


def _cold_counts(change: dict[str, Any], away: set[str], today: date) -> dict[str, int]:
    t0 = _applied(change)
    apply_day = _local_day(t0)
    first, last = _verdict_window(change)
    pre_first = apply_day - timedelta(days=VERDICT_DAYS)
    since = datetime.combine(pre_first, datetime.min.time(), tzinfo=_tz()).astimezone(UTC)
    until = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=_tz()).astimezone(UTC)
    rows = _cold_feedback(since, until, away, today)
    after = [t for t in rows if first <= _local_day(t) <= last]
    before = [t for t in rows if pre_first <= _local_day(t) < apply_day]
    return {"after": len(after), "before": len(before)}


def decide(old: dict[str, float], new: dict[str, float], cold: dict[str, int], n_days: int,
           *, min_gain_p: float | None = None) -> tuple[str, str, dict[str, Any]]:
    """Pure rule. ``old`` / ``new`` are ``tuning_review._aggregate`` dicts over the
    SAME ``n_days`` days. Returns ``(decision, rule, details)`` with decision in
    keep | rollback. The cost and comfort bands scale with ``n_days / 7`` so a
    partial verdict is not judged against a full week's allowance."""
    scale = max(0.0, min(1.0, n_days / float(VERDICT_DAYS)))
    gain = float(min_gain_pence() if min_gain_p is None else min_gain_p) * scale
    hrs_band = 0.5 * scale
    d_cost = new["cost_p"] - old["cost_p"]
    d_hours = new["hours_below"] - old["hours_below"]
    d_shower = new["shower_day_below"] - old["shower_day_below"]
    d_cold = cold["after"] - cold["before"]
    fails = []
    if d_cost > gain:
        fails.append("cost")
    if d_hours > hrs_band:
        fails.append("hours_below_floor")
    if d_shower > 0:
        fails.append("shower_shortfall_days")
    if d_cold > 1:
        fails.append("cold_feedback")
    details = {"delta_cost_pence": round(d_cost, 2), "cost_band_pence": round(gain, 2),
               "delta_hours_below": round(d_hours, 2), "hours_band": round(hrs_band, 2),
               "delta_shower_days": d_shower, "delta_cold_feedback": d_cold, "failed": fails}
    if fails:
        return "rollback", "failed:" + "+".join(fails), details
    return "keep", "all_clauses_passed", details


def _arm(agg: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 3) for k, v in agg.items()}


def verdict(
    change: dict[str, Any],
    now: datetime | None = None,
    *,
    replay: Callable[..., Any] | None = None,
    list_runs: Callable[[str], list] | None = None,
    cadence: str | None = None,
    budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Decision for one ``evaluating`` change. ``decision`` is one of ``not_due``,
    ``away_wait``, ``busy`` (replay lock held; retry next day), ``defer`` (not
    enough common replayed days), ``keep`` or ``rollback``; ``evidence`` carries
    both arms. Does NOT write to the ledger (``verdict_job`` does)."""
    from . import tuning_review as tr
    from .scheduler.lp_overrides import WHITELIST, patched_config

    now = _now(now)
    if not due(change, now):
        return {"decision": "not_due"}
    if auto_tune._is_away():
        return {"decision": "away_wait"}
    key = str(change["key"])
    old_v, new_v = change.get("old_value"), change.get("new_value")
    if key not in WHITELIST or old_v is None:
        # Cannot replay the old value: an identical pair of arms would "keep"
        # everything. Treat as unjudgeable (deferral path -> undecidable keep).
        return {"decision": "defer", "evidence": {"reason": "key_not_replayable"}}
    cadence = cadence or str(config.TUNING_REVIEW_CADENCE)
    budget = float(verdict_budget_seconds() if budget_seconds is None else budget_seconds)
    if replay is None:
        from .scheduler.lp_replay import replay_day as replay
    first, last = _verdict_window(change)
    cards = _cards()
    away = _away_days(cards)
    today = _local_day(now)
    cand = tr.replayable_days(last, VERDICT_DAYS, list_runs)
    cand = [d for d in cand if date.fromisoformat(d) >= first]
    days_away = [d for d in cand if d in away]
    days = [d for d in cand if d not in away]
    cold = _cold_counts(change, away, today)

    try:
        tr._acquire()
    except tr.ReviewBusy:
        return {"decision": "busy"}
    t0 = time.monotonic()
    deadline = t0 + budget
    try:
        with patched_config({key: old_v}):
            yard = tr.yardstick()  # the OLD policy's floors judge both arms
        per_old: dict[str, dict[str, float]] = {}
        per_new: dict[str, dict[str, float]] = {}
        partial = False
        for d in days:  # interleaved: a budget stop leaves the same days on both arms
            if time.monotonic() > deadline:
                partial = True
                break
            o = tr.evaluate({key: old_v}, [d], yard, cadence=cadence, replay=replay, deadline=deadline)
            n = tr.evaluate({key: new_v}, [d], yard, cadence=cadence, replay=replay, deadline=deadline)
            if d in o and d in n:
                per_old.update(o)
                per_new.update(n)
        if time.monotonic() > deadline and len(per_old) < len(days):
            partial = True
    finally:
        tr._release()

    common = sorted(set(per_old) & set(per_new))
    ev: dict[str, Any] = {
        "cadence": cadence, "partial": partial, "key": key, "old_value": old_v, "new_value": new_v,
        "days_replayed": common, "days_skipped_away": days_away,
        "days_failed": [d for d in days if d not in common],
        "window": [first.isoformat(), last.isoformat()], "cold_feedback": cold,
        "elapsed_seconds": round(time.monotonic() - t0, 1), "budget_seconds": budget,
        "yardstick": "old_value",
    }
    if len(common) < MIN_VERDICT_DAYS:
        return {"decision": "defer", "evidence": {**ev, "reason": f"only_{len(common)}_common_days"}}
    old_a, new_a = tr._aggregate(per_old, common), tr._aggregate(per_new, common)
    decision, rule, details = decide(old_a, new_a, cold, len(common))
    ev.update({"arm_old": _arm(old_a), "arm_new": _arm(new_a), "rule": rule, **details,
               "lived_arm": "replay_with_new_value"})
    # Re-check the tripwires at the end of the week: a wire that tripped since the
    # last daily run must beat a KEEP.
    if decision == "keep":
        trip = tripwire_check(change, now)
        if trip is not None:
            return {"decision": "rollback", "rule": f"tripwire:{trip['name']}",
                    "evidence": {**ev, **trip["evidence"], "tripwire": trip["name"]}}
    return {"decision": decision, "rule": rule, "evidence": ev}


def _notify_kept(change: dict[str, Any], ev: dict[str, Any], undecidable: bool = False) -> None:
    try:
        from .notifier import notify_action_confirmation

        key, new_v = change["key"], change["new_value"]
        if undecidable:
            msg = f"Auto-tune manteve {key}={new_v} sem veredito (replay indisponivel)."
        else:
            msg = (f"Auto-tune confirmou {key}={new_v} apos 7 dias: custo "
                   f"{ev.get('delta_cost_pence', 0):+.0f} p, conforto "
                   f"{ev.get('delta_hours_below', 0):+.1f} h abaixo do piso.")
        notify_action_confirmation(msg)
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune_judge: kept notify failed", exc_info=True)


def verdict_job(
    now: datetime | None = None, *,
    replay: Callable[..., Any] | None = None,
    list_runs: Callable[[str], list] | None = None,
) -> dict[str, Any]:
    """Daily job (scorecard time + 10 min): tripwires first, then, when 7 days have
    been scored, the replay verdict. Never raises; single-flight."""
    if not _job_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _verdict_locked(_now(now), replay, list_runs)
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune_verdict failed (non-fatal)", exc_info=True)
        return {"status": "error"}
    finally:
        _job_lock.release()


def _verdict_locked(now: datetime, replay, list_runs) -> dict[str, Any]:
    ch, why = _active_change()
    if ch is None:
        return {"status": why}
    out = _tripwire_locked(now)
    if out.get("status") == "tripped":
        return out
    if not due(ch, now):
        return {"status": "not_due", "id": ch["id"]}
    v = verdict(ch, now, replay=replay, list_runs=list_runs)
    dec, ev = v["decision"], v.get("evidence") or {}
    prior = ch.get("evidence") or {}
    if dec in ("not_due", "away_wait"):
        return {"status": dec, "id": ch["id"]}
    if dec == "busy":
        n = int(prior.get("busy_retries", 0)) + 1
        db.update_auto_tune_change(ch["id"], evidence={**prior, "busy_retries": n})
        _log("auto_tune_verdict_busy", {"id": ch["id"], "busy_retries": n}, "skipped")
        return {"status": "busy_replay", "id": ch["id"]}
    if dec == "defer":
        deferred = int(prior.get("deferred", 0)) + 1
        if deferred > MAX_DEFERRALS:
            return _undecidable(ch, prior, ev, deferred)
        db.update_auto_tune_change(ch["id"], evidence={**prior, **ev, "deferred": deferred})
        _log("auto_tune_verdict_deferred", {"id": ch["id"], "deferred": deferred,
                                            "reason": ev.get("reason")}, "skipped")
        return {"status": "deferred", "id": ch["id"], "deferred": deferred}
    ev = {**ev, "deferred": int(prior.get("deferred", 0)), "busy_retries": int(prior.get("busy_retries", 0))}
    if dec == "keep":
        auto_tune.keep(ch["id"], ev)
        if notify_kept():
            _notify_kept(ch, ev)
        return {"status": "kept", "id": ch["id"], "evidence": ev}
    res = auto_tune.rollback(ch["id"], reason=f"verdict:{v.get('rule')}", evidence=ev, actor="judge")
    return {"status": "rolled_back" if res.get("rolled_back") else "rollback_refused",
            "id": ch["id"], "rollback": res, "evidence": ev}


def _undecidable(ch: dict[str, Any], prior: dict[str, Any], ev: dict[str, Any], deferred: int) -> dict[str, Any]:
    """3 deferrals: KEEP (the change passed the tripwires for ~10 days; blocking the
    ledger forever, or reverting on missing data, are both worse) and say so once."""
    evidence = {**prior, **ev, "reason": "undecidable", "deferred": deferred - 1}
    auto_tune.keep(ch["id"], evidence)
    wkey = f"auto_tune_undecidable_{ch['id']}"
    try:
        if not db.is_warning_acknowledged(wkey):
            from .notifier import notify_risk

            notify_risk(
                f"Auto-tune nao conseguiu julgar {ch['key']}={ch['new_value']} (replay indisponivel "
                f"apos {MAX_DEFERRALS} tentativas); mantido sem veredito.",
                extra={"warning_key": wkey, "evidence": evidence})
            db.acknowledge_warning(wkey)
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune_judge: undecidable notify failed", exc_info=True)
    return {"status": "kept_undecidable", "id": ch["id"], "evidence": evidence}


# ---------------------------------------------------------------- scheduling
def judge_time(hour: int | None = None, minute: int | None = None) -> tuple[int, int]:
    """Scorecard local time + 10 minutes (wall-clock, wraps within the hour/day)."""
    h = int(config.COSY_SCORECARD_HOUR_LOCAL if hour is None else hour)
    m = int(config.COSY_SCORECARD_MINUTE if minute is None else minute) + 10
    return (h + m // 60) % 24, m % 60


def register_jobs(scheduler: Any, tz: ZoneInfo) -> bool:
    """Two daily cron jobs on the LOCAL clock (APScheduler's CronTrigger with a
    ZoneInfo timezone is DST-safe), 10 min after the scorecard."""
    from apscheduler.triggers.cron import CronTrigger

    h, m = judge_time()
    scheduler.add_job(tripwire_job, CronTrigger(hour=h, minute=m, timezone=tz),
                      id="auto_tune_tripwires", misfire_grace_time=3600)
    scheduler.add_job(verdict_job, CronTrigger(hour=h, minute=m, second=30, timezone=tz),
                      id="auto_tune_verdict", misfire_grace_time=3600)
    logger.info("Auto-tune judge cron: %02d:%02d (%s)", h, m, tz)
    return True
