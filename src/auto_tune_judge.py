"""Closed-loop auto-tune (#876), story 3: the JUDGE.

Two levels, both acting on the single ``evaluating`` ledger row (``src/auto_tune.py``):

* **Tripwires** (daily, right after the Cosy scorecard): a cheap, data-only check
  per key class. A tripped wire rolls the change back at once through
  ``auto_tune.rollback`` (restore, freeze, one notification, ``action_log``).
* **Day-7 verdict**: once the 7 local days ``apply_day .. apply_day+6`` are over
  (``local_today >= apply_day + 7``), replay those days twice (OLD vs NEW value,
  same prices / weather / load / process config, forward mode) and KEEP or
  ROLLBACK on the paired result. The rule depends on the change's ``intent``
  (``decide``): a saving, a trade-off, a comfort raise / lowering and a learned
  parameter are different bets and are judged against different clauses.

Why BOTH arms are replayed instead of comparing the new value's replay with the
raw realised cost: the realised cost carries execution noise (device lag, Fox /
Daikin write failures, forecast-revision replans, manual gestures) that the
replay does not, so mixing a realised arm with a replayed arm bakes that
difference into the verdict. Two replays that differ ONLY in the knob are a
paired comparison; the harness does not claim to reproduce the committed plans.
The apply day is replayed on both arms (a valid counterfactual over the same
inputs), so the Sunday 09:20 apply is judged the next Sunday 07:40 and the
ledger is free before the 08:45 comfort job.

Comfort yardstick: both arms are scored against the OLD value's floors (the
policy the owner lived with before the change), otherwise lowering a floor would
trivially "improve" comfort. The exception is ``comfort_down`` (the owner said
"hot": the new, lower floor IS the policy), which uses the NEW value's floors.

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
FEEDBACK_MIN_GAP = timedelta(hours=1)   # reports closer than this are ONE report
PRED_ERR_WORSE_RATIO = 1.5    # "worse than baseline by > 50 %"
PRED_ERR_WORSE_ABS_C = 0.2    # ... AND by at least this many deg C
PRED_ERR_BASELINE_MIN_DAYS = 3
BUDGET_CAP_SECONDS = 45 * 60
COMFORT_JOB_MINUTE_OF_DAY = 8 * 60 + 45   # Sunday 08:45 local (runner._comfort_weekly_job)

COMFORT_LOOSEN_DOWN = auto_tune.COMFORT_LOOSEN_DOWN
COMFORT_LOOSEN_UP = auto_tune.COMFORT_LOOSEN_UP
DHW_KEYS = auto_tune.DHW_KEYS
LEARNED_KEYS = auto_tune.LEARNED_KEYS
PLAN_PATTERN_KEYS = ("LP_LOAD_EXPENSIVE_BAND_QUANTILE",)

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
    """``AUTO_TUNE_VERDICT_BUDGET_SECONDS`` (1800), clamped to 45 min so the Sunday
    verdict always ends before the 08:45 comfort job."""
    return max(0.0, min(_envf("AUTO_TUNE_VERDICT_BUDGET_SECONDS", 1800.0), float(BUDGET_CAP_SECONDS)))


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


def _z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """``comfort_loosen | warm_side | dhw_loosen | plan_pattern | learned | tighten |
    none`` from the key and the direction of ``old_value -> new_value``.
    ``warm_side`` = ``LP_W3_CEILING_C`` raised (the house may run hotter);
    ``plan_pattern`` = the load quantile LOOSENED (p90 -> p75, less insurance);
    the inverse is ``tighten`` (it only adds insurance)."""
    key = str(change.get("key") or "").upper()
    if key in PLAN_PATTERN_KEYS:
        new = str(change.get("new_value") or "").strip().lower()
        return "plan_pattern" if new == "p75" else "tighten"
    if key in LEARNED_KEYS:
        return "learned"
    old, new_f = _f(change.get("old_value")), _f(change.get("new_value"))
    if old is None or new_f is None:
        return "none"
    if key == "LP_W3_CEILING_C" and new_f > old:
        return "warm_side"
    if key in COMFORT_LOOSEN_DOWN:
        return "comfort_loosen" if new_f < old else "tighten"
    if key in COMFORT_LOOSEN_UP:
        return "comfort_loosen" if new_f > old else "tighten"
    if key in DHW_KEYS:
        return "dhw_loosen" if new_f < old else "tighten"
    return "none"


def intent_of(change: dict[str, Any]) -> str:
    i = change.get("intent")
    if i in auto_tune.INTENTS:
        return str(i)
    return auto_tune.derive_intent(str(change.get("key")), str(change.get("source")),
                                   change.get("old_value"), change.get("new_value"))


# --------------------------------------------------------------- tripwires
def _dedupe(times: list[datetime]) -> list[datetime]:
    """Reports closer than ``FEEDBACK_MIN_GAP`` to the previous COUNTED one are one
    report (per-room double reports, double taps)."""
    out: list[datetime] = []
    for t in sorted(times):
        if not out or t - out[-1] >= FEEDBACK_MIN_GAP:
            out.append(t)
    return out


def _feedback(verdict_name: str, since: datetime, until: datetime, away: set[str], today: date) -> list[datetime]:
    out: list[datetime] = []
    for r in db.get_comfort_feedback(since_utc=_z(since)):
        if str(r.get("verdict")) != verdict_name:
            continue
        t = auto_tune._parse(r.get("at_utc"))
        if t is None or t < since or t > until:
            continue
        d = _local_day(t)
        if d.isoformat() in away or _is_today_away(d, today):
            continue
        out.append(t)
    return _dedupe(out)


def _burst(times: list[datetime]) -> list[datetime]:
    """>= ``COLD_TRIP_COUNT`` reports inside any ``COLD_WINDOW_DAYS`` window, else []."""
    for i in range(len(times)):
        win = [c for c in times[i:] if c - times[i] <= timedelta(days=COLD_WINDOW_DAYS)]
        if len(win) >= COLD_TRIP_COUNT:
            return win
    return []


def _events(action: str, since: datetime, until: datetime, away: set[str], today: date,
            *, fired_only: bool = False) -> list[dict[str, Any]]:
    out = []
    # since= bounds the scan; result="ok" (fired) keeps a long run of passive-mode
    # ``skipped`` backstop ticks from pushing an earlier fire out of the row limit.
    for r in db.get_action_logs(device="daikin", action=action, limit=500, since=since.isoformat(),
                                result="ok" if fired_only else None):
        t = auto_tune._parse(r.get("timestamp"))
        if t is None or t < since or t > until:
            continue
        if fired_only and str(r.get("result")) != "ok":
            continue
        d = _local_day(t)
        if d.isoformat() in away or _is_today_away(d, today):
            continue
        out.append({"at": _z(t), "params": r.get("params") or {}})
    return out


def _pred_err(payload_row: dict[str, Any]) -> float | None:
    v = _f(payload_row.get("pred_err_mean_c"))
    return abs(v) if v is not None else None


def _pred_err_series(away: set[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in db.get_lwt_learning_daily(60):
        e = _pred_err(r)
        if e is not None and str(r["date"]) not in away:
            out[str(r["date"])] = e
    return out


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
    baseline = change.get("baseline") or {}

    if cls == "comfort_loosen":
        win = _burst(_feedback("cold", t0, now, away, today))
        if win:
            return {"name": "cold_feedback", "evidence": {
                "cold_feedback_at": [_z(c) for c in win], "window_days": COLD_WINDOW_DAYS}}, notes
        emerg = _events("lwt_emergency", t0, now, away, today)
        if emerg:
            return {"name": "lwt_emergency", "evidence": {"events": emerg[:5], "n": len(emerg)}}, notes
        bk = _events("lwt_comfort_backstop", t0, now, away, today, fired_only=True)
        if bk:
            return {"name": "comfort_backstop", "evidence": {"events": bk[:5], "n": len(bk)}}, notes
        return None, notes

    if cls == "warm_side":
        win = _burst(_feedback("hot", t0, now, away, today))
        if win:
            return {"name": "hot_feedback", "evidence": {
                "hot_feedback_at": [_z(c) for c in win], "window_days": COLD_WINDOW_DAYS}}, notes
        wb = _events("lwt_warm_backstop", t0, now, away, today, fired_only=True)
        if wb:
            return {"name": "warm_backstop", "evidence": {"events": wb[:5], "n": len(wb)}}, notes
        return None, notes

    # Scorecard-day based classes: complete local days AFTER the apply day (the apply
    # day, and in particular its evening shower, is half old policy and is not counted).
    post = sorted((r for r in cards if date.fromisoformat(r["date"]) > apply_day and r["date"] not in away),
                  key=lambda r: r["date"])
    if cls == "dhw_loosen":
        misses = [r["date"] for r in post
                  if isinstance(((r.get("payload") or {}).get("tank")), dict)
                  and r["payload"]["tank"].get("any_below_floor") is True]
        n_base = _f(baseline.get("n_days")) or 0.0
        rate = (_f(baseline.get("shower_shortfall_days")) or 0.0) / n_base if n_base else 0.0
        expected = rate * len(post)
        if misses and len(misses) > expected:
            return {"name": "shower_floor", "evidence": {
                "dates": misses, "baseline_rate_per_day": round(rate, 3), "expected": round(expected, 2)}}, notes
        return None, notes
    if cls == "plan_pattern":
        lim = peak_trip_kwh()
        base_peak = _f(baseline.get("peak_import_kwh_per_day")) or 0.0
        for r in post:
            v = _f(r.get("peak_import_kwh"))
            if v is not None and v > lim and v > base_peak + lim:
                return {"name": "peak_import", "evidence": {
                    "date": r["date"], "peak_import_kwh": round(v, 3), "limit_kwh": lim,
                    "baseline_kwh_per_day": round(base_peak, 3)}}, notes
        return None, notes
    if cls == "learned":
        errs = _pred_err_series(away)
        base = [e for d, e in errs.items() if 1 <= (apply_day - date.fromisoformat(d)).days <= 7]
        if len(base) < PRED_ERR_BASELINE_MIN_DAYS:
            return None, ["no_pred_err_baseline"]
        baseline_e = sum(base) / len(base)
        limit = max(baseline_e * PRED_ERR_WORSE_RATIO, baseline_e + PRED_ERR_WORSE_ABS_C)
        bad_prev: str | None = None
        for d in sorted(d for d in errs if date.fromisoformat(d) > apply_day):
            e = errs[d]
            if e > limit:
                if bad_prev and (date.fromisoformat(d) - date.fromisoformat(bad_prev)).days == 1:
                    return {"name": "pred_err", "evidence": {
                        "days": [bad_prev, d], "baseline_abs_c": round(baseline_e, 3),
                        "limit_c": round(limit, 3), "errors_abs_c": [round(errs[bad_prev], 3), round(e, 3)]}}, notes
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
            "checked_at": _z(now), "tripped": trip["name"] if trip else None, "notes": notes}))
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


def _rollback_or_close(ch: dict[str, Any], reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """``auto_tune.rollback``; when it REFUSES (value moved by someone else, newer row,
    invalid restore...) the row would stay ``evaluating`` forever, replayed daily and
    blocking the ledger. Close it terminally as ``superseded_manual``, log it and tell
    the owner once."""
    res = auto_tune.rollback(ch["id"], reason=reason, evidence=evidence, actor="judge")
    if res.get("rolled_back"):
        return res
    why = str(res.get("reason") or "unknown")
    ev = {**(ch.get("evidence") or {}), **evidence, "reason": f"rollback_refused:{why}",
          "wanted": reason, "refusal": {k: v for k, v in res.items() if k != "rolled_back"}}
    db.update_auto_tune_change(ch["id"], status="superseded_manual", evaluated_at=_z(_now()), evidence=ev)
    _log("auto_tune_rollback_refused", {"id": ch["id"], "key": ch["key"], "why": why, "wanted": reason}, "failure")
    wkey = f"auto_tune_rollback_refused_{ch['id']}"
    try:
        if not db.is_warning_acknowledged(wkey):
            from .notifier import notify_risk

            notify_risk(f"Auto-tune nao conseguiu reverter {ch['key']} ({reason}): {why}. "
                        "Registro encerrado; confira o valor atual em Ajustes.",
                        extra={"warning_key": wkey, "evidence": ev})
            db.acknowledge_warning(wkey)
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune_judge: rollback-refused notify failed", exc_info=True)
    return res


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
    res = _rollback_or_close(ch, f"tripwire:{trip['name']}", {**trip["evidence"], "tripwire": trip["name"]})
    _log("auto_tune_tripwire", {"id": ch["id"], "key": ch["key"], "tripwire": trip["name"], "rollback": res})
    return {"status": "tripped", "id": ch["id"], "tripped": trip["name"], "rollback": res}


# ----------------------------------------------------------------- verdict
def _verdict_window(change: dict[str, Any]) -> tuple[date, date]:
    """The 7 local days ``apply_day .. apply_day+6``."""
    apply_day = _local_day(_applied(change))
    return apply_day, apply_day + timedelta(days=VERDICT_DAYS - 1)


def due(change: dict[str, Any], now: datetime) -> bool:
    """The 7-day window is over: ``local_today >= apply_day + 7``."""
    return _local_day(now) >= _local_day(_applied(change)) + timedelta(days=VERDICT_DAYS)


def _cold_counts(change: dict[str, Any], now: datetime, away: set[str], today: date) -> dict[str, Any]:
    """Cold reports (>= 1 h apart) AFTER ``applied_at`` (up to ``now``) vs the 7x24 h
    BEFORE it. The pre-week is normalised for away days in it (``before`` is scaled by
    7 / usable days; no usable day -> the clause is skipped, ``before = None``)."""
    t0 = _applied(change)
    apply_day = _local_day(t0)
    after = _feedback("cold", t0, now, away, today)
    before = _feedback("cold", t0 - timedelta(days=VERDICT_DAYS), t0 - timedelta(seconds=1), away, today)
    pre_days = [apply_day - timedelta(days=i) for i in range(1, VERDICT_DAYS + 1)]
    n_away = sum(1 for d in pre_days if d.isoformat() in away)
    usable = VERDICT_DAYS - n_away
    before_norm = (len(before) * VERDICT_DAYS / usable) if usable > 0 else None
    return {"after": len(after), "before": len(before),
            "before_normalised": round(before_norm, 2) if before_norm is not None else None,
            "pre_week_away_days": n_away}


def _pred_err_lived(change: dict[str, Any], now: datetime, away: set[str]) -> dict[str, float | None]:
    t0 = _applied(change)
    apply_day = _local_day(t0)
    errs = _pred_err_series(away)
    before = [e for d, e in errs.items() if 1 <= (apply_day - date.fromisoformat(d)).days <= VERDICT_DAYS]
    after = [e for d, e in errs.items()
             if 0 <= (date.fromisoformat(d) - apply_day).days <= VERDICT_DAYS - 1]
    mean = lambda v: round(sum(v) / len(v), 3) if len(v) >= PRED_ERR_BASELINE_MIN_DAYS else None  # noqa: E731
    return {"before": mean(before), "after": mean(after)}


def decide(old: dict[str, float], new: dict[str, float], cold: dict[str, Any], n_days: int, *,
           intent: str = "saving", expected_comfort_hours: float | None = None,
           pred_err: dict[str, float | None] | None = None,
           min_gain_p: float | None = None, tol_h: float | None = None) -> tuple[str, str, dict[str, Any]]:
    """Pure rule. ``old`` / ``new`` are ``tuning_review._aggregate`` dicts over the SAME
    ``n_days`` days. Returns ``(decision, rule, details)`` with decision in keep |
    rollback. Bands scale with ``n_days / 7`` (a partial verdict is not judged against a
    full week's allowance). Clauses per ``intent``:

    * ``saving`` - cost <= old + min gain, hours <= old + 0.5, no extra shower day, cold ok.
    * ``trade_off`` - must still SAVE >= min gain; hours band = the row's expected
      comfort cost (``expected_comfort_hours``, default 2x tolerance) + tolerance;
      shower day and cold clauses guard.
    * ``comfort_up`` - hours must not get worse, cost <= old + 2x min gain, cold not
      worse, no extra shower day.
    * ``comfort_down`` - (hours judged under the NEW floors by the caller) hours band,
      cost not worse, cold clause, no extra shower day.
    * ``learned`` - cost <= old + min gain AND the prediction error not worse by > 50 %.
    """
    scale = max(0.0, min(1.0, n_days / float(VERDICT_DAYS)))
    gain = float(min_gain_pence() if min_gain_p is None else min_gain_p) * scale
    tol = float(config.TUNING_REVIEW_COMFORT_TOLERANCE_H if tol_h is None else tol_h) * scale
    d_cost = new["cost_p"] - old["cost_p"]
    d_hours = new["hours_below"] - old["hours_below"]
    d_shower = new["shower_day_below"] - old["shower_day_below"]
    before = cold.get("before_normalised")
    d_cold = None if before is None else cold["after"] - before
    cold_band = 1.0  # default clause: after <= before + 1
    fails: list[str] = []
    details: dict[str, Any] = {"intent": intent}

    def cold_fails(band: float) -> bool:
        return d_cold is not None and d_cold > band + 1e-9

    if intent == "trade_off":
        exp_h = float(expected_comfort_hours) if expected_comfort_hours is not None else 2.0 * tol
        hrs_band = max(0.0, exp_h) * scale + tol
        details["expected_comfort_hours"] = exp_h
        if d_cost > -gain:
            fails.append("cost_not_saved")
        if d_hours > hrs_band:
            fails.append("hours_below_floor")
        if d_shower > 0:
            fails.append("shower_shortfall_days")
        if cold_fails(cold_band):
            fails.append("cold_feedback")
        details["hours_band"] = round(hrs_band, 2)
    elif intent == "comfort_up":
        hrs_band = 0.0
        if d_cost > 2.0 * gain:
            fails.append("cost")
        if d_hours > 1e-6:
            fails.append("hours_below_floor")
        if d_shower > 0:
            fails.append("shower_shortfall_days")
        if cold_fails(0.0):
            fails.append("cold_feedback")
        details["cost_band_pence"] = round(2.0 * gain, 2)
        details["hours_band"] = hrs_band
    elif intent == "comfort_down":
        hrs_band = 0.5 * scale
        if d_cost > 1e-6:
            fails.append("cost")
        if d_hours > hrs_band:
            fails.append("hours_below_floor")
        if d_shower > 0:
            fails.append("shower_shortfall_days")
        if cold_fails(cold_band):
            fails.append("cold_feedback")
        details["hours_band"] = round(hrs_band, 2)
    elif intent == "learned":
        pe = pred_err or {}
        if d_cost > gain:
            fails.append("cost")
        b, a = pe.get("before"), pe.get("after")
        details["pred_err"] = {"before": b, "after": a}
        if b is not None and a is not None and a > max(b * PRED_ERR_WORSE_RATIO, b + PRED_ERR_WORSE_ABS_C):
            fails.append("pred_err")
        details["cost_band_pence"] = round(gain, 2)
    else:  # saving
        hrs_band = 0.5 * scale
        if d_cost > gain:
            fails.append("cost")
        if d_hours > hrs_band:
            fails.append("hours_below_floor")
        if d_shower > 0:
            fails.append("shower_shortfall_days")
        if cold_fails(cold_band):
            fails.append("cold_feedback")
        details["cost_band_pence"] = round(gain, 2)
        details["hours_band"] = round(hrs_band, 2)
    details.update({"delta_cost_pence": round(d_cost, 2), "delta_hours_below": round(d_hours, 2),
                    "delta_shower_days": d_shower,
                    "delta_cold_feedback": None if d_cold is None else round(d_cold, 2), "failed": fails})
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
    enough common replayed days, or the replay itself failed), ``keep`` or
    ``rollback``; ``evidence`` carries both arms. Does NOT write to the ledger
    (``verdict_job`` does)."""
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
    intent = intent_of(change)
    cadence = cadence or str(config.TUNING_REVIEW_CADENCE)
    budget = min(float(verdict_budget_seconds() if budget_seconds is None else budget_seconds),
                 float(BUDGET_CAP_SECONDS))
    if replay is None:
        from .scheduler.lp_replay import replay_day as replay
    first, last = _verdict_window(change)
    cards = _cards()
    away = _away_days(cards)
    today = _local_day(now)
    cand = [d for d in tr.replayable_days(last, VERDICT_DAYS, list_runs) if date.fromisoformat(d) >= first]
    days_away = [d for d in cand if d in away]
    days = [d for d in cand if d not in away]
    cold = _cold_counts(change, now, away, today)
    pred_err = _pred_err_lived(change, now, away) if intent == "learned" else None
    expected_h = _f((change.get("evidence") or {}).get("expected_comfort_hours"))

    try:
        tr._acquire()
    except tr.ReviewBusy:
        return {"decision": "busy"}
    t0 = time.monotonic()
    deadline = t0 + budget
    ev: dict[str, Any] = {
        "cadence": cadence, "key": key, "old_value": old_v, "new_value": new_v, "intent": intent,
        "days_skipped_away": days_away, "window": [first.isoformat(), last.isoformat()],
        "cold_feedback": cold, "budget_seconds": budget,
        "yardstick": "new_value" if intent == "comfort_down" else "old_value",
    }
    try:
        # The comfort floors that judge BOTH arms: the OLD policy, except for a
        # "hot" lowering where the new, lower floor is the policy.
        with patched_config({key: new_v if intent == "comfort_down" else old_v}):
            yard = tr.yardstick()
        per_old: dict[str, dict[str, float]] = {}
        per_new: dict[str, dict[str, float]] = {}
        budget_cut: list[str] = []
        for i, d in enumerate(days):  # interleaved: a budget stop leaves the same days on both arms
            if time.monotonic() > deadline:
                budget_cut = days[i:]
                break
            o = tr.evaluate({key: old_v}, [d], yard, cadence=cadence, replay=replay, deadline=deadline)
            n = tr.evaluate({key: new_v}, [d], yard, cadence=cadence, replay=replay, deadline=deadline)
            if d in o and d in n:
                per_old.update(o)
                per_new.update(n)
        common = sorted(set(per_old) & set(per_new))
        ev.update({"days_replayed": common, "partial": bool(budget_cut), "days_budget_cut": budget_cut,
                   "days_failed": [d for d in days if d not in common and d not in budget_cut],
                   "elapsed_seconds": round(time.monotonic() - t0, 1)})
        if len(common) < MIN_VERDICT_DAYS:
            return {"decision": "defer", "evidence": {**ev, "reason": f"only_{len(common)}_common_days"}}
        old_a, new_a = tr._aggregate(per_old, common), tr._aggregate(per_new, common)
    except Exception as exc:  # noqa: BLE001 - a broken replay/comfort read must defer, not loop as "error"
        logger.warning("auto_tune_judge: replay failed for %s: %s", key, exc, exc_info=True)
        return {"decision": "defer", "evidence": {**ev, "reason": f"replay_error:{type(exc).__name__}"}}
    finally:
        tr._release()

    decision, rule, details = decide(old_a, new_a, cold, len(common), intent=intent,
                                     expected_comfort_hours=expected_h, pred_err=pred_err)
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


def _notify_kept(change: dict[str, Any], ev: dict[str, Any]) -> None:
    try:
        from .notifier import notify_action_confirmation

        msg = (f"Auto-tune confirmou {change['key']}={change['new_value']} apos 7 dias: custo "
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
    """Daily job (scorecard time + 10 min): tripwires first, then, when the 7-day
    window is over, the replay verdict. Never raises; single-flight. Every outcome
    writes an ``auto_tune_verdict`` row to ``action_log``."""
    if not _job_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        out = _verdict_locked(_now(now), replay, list_runs)
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune_verdict failed (non-fatal)", exc_info=True)
        out = {"status": "error"}
    finally:
        _job_lock.release()
    try:
        if out.get("status") not in ("no_change", "manual", "disabled", "busy"):
            _log("auto_tune_verdict", {k: v for k, v in out.items() if k not in ("evidence", "rollback")}
                 | {"rule": (out.get("evidence") or {}).get("rule")},
                 "failure" if out.get("status") in ("error", "rollback_refused", "keep_refused") else "success")
    except Exception:  # noqa: BLE001
        pass
    return out


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
        return {"status": "deferred", "id": ch["id"], "deferred": deferred, "reason": ev.get("reason")}
    ev = {**prior, **ev, "deferred": int(prior.get("deferred", 0)),
          "busy_retries": int(prior.get("busy_retries", 0))}
    if dec == "keep":
        if not auto_tune.keep(ch["id"], ev):
            return {"status": "keep_refused", "id": ch["id"]}
        if notify_kept():
            _notify_kept(ch, ev)
        return {"status": "kept", "id": ch["id"], "evidence": ev}
    res = _rollback_or_close(ch, f"verdict:{v.get('rule')}", ev)
    return {"status": "rolled_back" if res.get("rolled_back") else "rollback_refused",
            "id": ch["id"], "rollback": res, "evidence": ev}


def _undecidable(ch: dict[str, Any], prior: dict[str, Any], ev: dict[str, Any], deferred: int) -> dict[str, Any]:
    """3 deferrals: KEEP (the change passed the tripwires for ~10 days; blocking the
    ledger forever, or reverting on missing data, are both worse) and say so once."""
    evidence = {**prior, **ev, "reason": "undecidable", "deferred": deferred - 1}
    if not auto_tune.keep(ch["id"], evidence):
        return {"status": "keep_refused", "id": ch["id"]}
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


def budget_ends_before_comfort_job(budget_s: float | None = None) -> bool:
    """False when the verdict (judge time + budget) could still be running at the
    Sunday 08:45 comfort job - that job would get ``busy`` and the cadence halves."""
    h, m = judge_time()
    b = verdict_budget_seconds() if budget_s is None else min(float(budget_s), float(BUDGET_CAP_SECONDS))
    return h * 60 + m + b / 60.0 < COMFORT_JOB_MINUTE_OF_DAY


def register_jobs(scheduler: Any, tz: ZoneInfo) -> bool:
    """Two daily cron jobs on the LOCAL clock (APScheduler's CronTrigger with a
    ZoneInfo timezone is DST-safe), 10 min after the scorecard."""
    from apscheduler.triggers.cron import CronTrigger

    h, m = judge_time()
    scheduler.add_job(tripwire_job, CronTrigger(hour=h, minute=m, timezone=tz),
                      id="auto_tune_tripwires", misfire_grace_time=3600)
    scheduler.add_job(verdict_job, CronTrigger(hour=h, minute=m, second=30, timezone=tz),
                      id="auto_tune_verdict", misfire_grace_time=3600)
    if not budget_ends_before_comfort_job():
        logger.warning(
            "Auto-tune judge: %02d:%02d + %.0f s verdict budget reaches the Sunday %02d:%02d comfort job; "
            "a late verdict would make it 'busy' and halve the change cadence. Lower "
            "AUTO_TUNE_VERDICT_BUDGET_SECONDS or move the scorecard earlier.",
            h, m, verdict_budget_seconds(), COMFORT_JOB_MINUTE_OF_DAY // 60, COMFORT_JOB_MINUTE_OF_DAY % 60)
    logger.info("Auto-tune judge cron: %02d:%02d (%s)", h, m, tz)
    return True
