"""Closed-loop auto-tune (#876), story 2: the weekly APPLY path.

The Sunday tuning review (``tuning_review``) writes suggestions only. This module
turns AT MOST ONE of them per ISO week into a ledger change through
``auto_tune.propose_change`` (which owns every safety rule: allowlist, one step,
drift cap, freeze, away, baseline, one change at a time). Nothing here writes a
setting directly.

Selection (``select_weekly_change``, pure):

=============  ==========================================================
verdict        eligible when
=============  ==========================================================
recommended    always (top weekly saving; ties: smaller comfort cost)
trade-off      the week had >= 1 comfort feedback and NO ``cold`` one, it
               saves money, loosens comfort only (hours below up, never a
               worse shower day)
comfort-first  the control had hours-below > 0 AND >= 1 ``cold`` feedback
=============  ==========================================================

A candidate is also skipped when: its key is not allowlisted, frozen, the review's
``current_value`` differs from the live one (``stale``: the owner or the ledger
moved it since Sunday), or the move is more than one ``auto_tune`` step.

Weekly quota: ``weekly_quota_used`` counts every non-manual ledger row applied in
the current LOCAL ISO week, so the Sunday 08:45 comfort-feedback source and this
09:20 source share ONE change per week.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import auto_tune, db
from . import runtime_settings as rts
from .config import config

logger = logging.getLogger(__name__)

# pt-BR labels of the 10 allowlisted keys (mirrors ui/src/components/settings/groups.ts
# PLAIN_COPY / KEY_LABELS) and their display unit.
KEY_LABELS_PT: dict[str, str] = {
    "DHW_TEMP_NORMAL_C": "Temperatura normal do tanque",
    "DHW_TEMP_COMFORT_C": "Meta do tanque quando aquece com energia barata",
    "LP_W3_NIGHT_FLOOR_C": "Mínimo à noite",
    "LP_W3_PEAK_COAST_DELTA_C": "Quanto pode esfriar no pico",
    "INDOOR_SETPOINT_C": "Temperatura da casa de dia",
    "LP_W3_CEILING_C": "Máximo (não aquecer acima)",
    "LP_LOAD_EXPENSIVE_BAND_QUANTILE": "Folga da previsão nas faixas caras",
    "LP_W3_SOLAR_GAIN_KW_PER_PV_KW": "Ganho solar por kW de painel",
    "LP_W3_INTERNAL_GAIN_KW": "Calor interno da casa",
    "LP_W3_EMITTER_TAU_MIN": "Atraso dos radiadores",
}
KEY_UNITS: dict[str, str] = {
    "DHW_TEMP_NORMAL_C": " °C", "DHW_TEMP_COMFORT_C": " °C", "LP_W3_NIGHT_FLOOR_C": " °C",
    "LP_W3_PEAK_COAST_DELTA_C": " °C", "INDOOR_SETPOINT_C": " °C", "LP_W3_CEILING_C": " °C",
    "LP_W3_INTERNAL_GAIN_KW": " kW", "LP_W3_EMITTER_TAU_MIN": " min",
}
SKIP_NOTIFY_MIN_SAVING_PENCE = 20.0
_WEEKDAY_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


# ------------------------------------------------------------------ helpers
def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _pt(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{f:.3f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _week_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[Monday 00:00 local, next Monday 00:00 local) around ``now``, as UTC."""
    tz = _tz()
    loc = now.astimezone(tz)
    monday = datetime(loc.year, loc.month, loc.day, tzinfo=tz) - timedelta(days=loc.weekday())
    nxt = datetime(monday.year, monday.month, monday.day, tzinfo=tz) + timedelta(days=7)
    return monday.astimezone(UTC), nxt.astimezone(UTC)


def iso_week_label(now: datetime) -> str:
    y, w, _ = now.astimezone(_tz()).isocalendar()
    return f"{y}-W{w:02d}"


def weekly_quota_used(now: datetime | None = None) -> int:
    """Automated (non-manual) ledger changes applied in the current local ISO
    week. Shared by every source; >= 1 means the week's change is spent."""
    try:
        t = _now(now)
        a, b = _week_bounds(t)
        n = 0
        for r in db.get_auto_tune_changes(200, exclude_status="manual"):
            at = auto_tune._parse(r.get("applied_at"))
            if at is not None and a <= at < b:
                n += 1
        return n
    except Exception:  # noqa: BLE001
        # Fail CLOSED: an unreadable ledger must not license a second change.
        logger.warning("auto_tune_apply.weekly_quota_used failed; assuming spent", exc_info=True)
        return 1


# ----------------------------------------------------------------- schedule
def apply_schedule() -> tuple[int, int, int]:
    """(day_of_week Mon=0, hour, minute) of the weekly apply job: the review's
    slot plus ``AUTO_TUNE_APPLY_DELAY_MINUTES`` (rolling into the next weekday
    when it crosses local midnight)."""
    dow = int(config.TUNING_REVIEW_DOW)
    total = int(config.TUNING_REVIEW_HOUR_LOCAL) * 60 + max(0, int(config.AUTO_TUNE_APPLY_DELAY_MINUTES))
    dow = (dow + total // 1440) % 7
    total %= 1440
    return dow, total // 60, total % 60


def next_apply_at(now: datetime | None = None) -> str:
    """ISO UTC time of the next scheduled apply run (DST-safe: built on the
    local wall clock)."""
    tz = _tz()
    loc = _now(now).astimezone(tz)
    dow, hh, mm = apply_schedule()
    for add in range(0, 8):
        d = (loc + timedelta(days=add)).date()
        if d.weekday() != dow:
            continue
        cand = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
        if cand.astimezone(UTC) > loc.astimezone(UTC):
            return cand.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""  # unreachable


def ledger_extras(now: datetime | None = None) -> dict[str, Any]:
    """Fields added to ``GET /api/v1/tuning/ledger``."""
    try:
        on = auto_tune.enabled() and bool(getattr(config, "TUNING_REVIEW_ENABLED", True))
        return {"next_apply_at": next_apply_at(now) if on else None, "apply_enabled": on,
                "weekly_quota_used": weekly_quota_used(now)}
    except Exception:  # noqa: BLE001
        return {"next_apply_at": None, "apply_enabled": False, "weekly_quota_used": 0}


# ---------------------------------------------------------------- selection
def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pence(row: dict[str, Any]) -> float:
    return -float(row.get("delta_pence_per_week") or 0.0)  # positive = saves


def _canon(spec: Any, v: Any) -> Any:
    return rts._validate(spec, v)


def _check_candidate(row: dict[str, Any], comfort: dict[str, Any], now: datetime) -> str | None:
    """None = eligible; otherwise the skip reason."""
    key = str(row.get("key") or "")
    verdict = str(row.get("verdict") or "")
    spec = rts.SCHEMA.get(key)
    if spec is None or not auto_tune.is_allowlisted(key):
        return "not_allowlisted"
    pl = row.get("payload") or {}
    cold, n = int(comfort.get("cold") or 0), int(comfort.get("n") or 0)
    hrs = row.get("delta_comfort_hours")
    if cold >= 1 and hrs is not None and float(hrs) > 0:
        return "cold_feedback"  # a cold week never loosens comfort, whatever the verdict
    if verdict == "trade-off":
        if n < 1:
            return "no_comfort_feedback"
        if int(comfort.get("n_days") or 0) < 2:
            return "feedback_on_one_day"
        if _pence(row) <= 0 or hrs is None or float(hrs) <= 0:
            return "not_a_loosening_saving"
        if float(hrs) > 2.0 * float(config.TUNING_REVIEW_COMFORT_TOLERANCE_H) + 1e-9:
            return "comfort_cost_too_large"
        sc, sn = pl.get("shower_days_below_current"), pl.get("shower_days_below_suggested")
        if sc is not None and sn is not None and int(sn) > int(sc):
            return "worse_shower_days"
    elif verdict == "comfort-first":
        if cold < 1:
            return "no_cold_feedback"
        hb = pl.get("hours_below_current")
        if hb is None or float(hb) <= 0:
            return "control_not_below_floor"
    elif verdict != "recommended":
        return "verdict_not_applicable"
    try:
        live = _canon(spec, auto_tune.current_value(key))
        review_cur = _canon(spec, row.get("current_value"))
        new = _canon(spec, row.get("suggested_value"))
    except Exception:  # noqa: BLE001
        return "invalid"
    if live != review_cur:
        return "stale"
    if live == new:
        return "unchanged"
    prop = comfort.get("comfort_job_proposal")
    if isinstance(prop, dict) and str(prop.get("key")) == key:
        try:
            pdir = float(prop["suggested"]) - float(prop["current"])
            if (float(new) - float(live)) * pdir < 0:
                return "conflicts_comfort"
        except (KeyError, TypeError, ValueError):
            pass
    if spec.enum is not None:
        en = list(spec.enum)
        if abs(en.index(new) - en.index(live)) != 1:
            return "step_too_large"
    elif spec.type_name in ("float", "int"):
        if abs(float(new) - float(live)) > auto_tune.step_for(key) + 1e-9:
            return "step_too_large"
    if db.auto_tune_frozen_until(key, now):
        return "frozen"
    return None


def evaluate_candidates(suggestions: list[dict[str, Any]], comfort_signal: dict[str, Any] | None,
                        now: datetime | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(chosen row or None, per-candidate decisions). Pure apart from reads."""
    t = _now(now)
    comfort = comfort_signal if isinstance(comfort_signal, dict) else {}
    order = {"recommended": 0, "comfort-first": 1, "trade-off": 2}
    rows = sorted(
        (r for r in suggestions if r.get("verdict") in order),
        key=lambda r: (order[r["verdict"]], -_pence(r) if r["verdict"] != "comfort-first" else 0.0,
                       float(r.get("delta_comfort_hours") or 0.0)))
    considered: list[dict[str, Any]] = []
    chosen: dict[str, Any] | None = None
    for r in rows:
        why = _check_candidate(r, comfort, t)
        considered.append({"key": r.get("key"), "verdict": r.get("verdict"),
                           "from": r.get("current_value"), "to": r.get("suggested_value"),
                           "saving_pence_per_week": round(_pence(r), 2),
                           "delta_comfort_hours": r.get("delta_comfort_hours"),
                           "skipped": why})
        if why is None and chosen is None:
            chosen = r
    return chosen, considered


def select_weekly_change(suggestions: list[dict[str, Any]], comfort_signal: dict[str, Any] | None,
                         now: datetime | None = None) -> dict[str, Any] | None:
    """The one ``tuning_suggestions`` row to apply this week, or None.
    ``comfort_signal`` is the week's ``comfort_feedback.weekly_summary`` dict
    (``n`` / ``cold`` / ``ok`` / ``hot``)."""
    return evaluate_candidates(suggestions, comfort_signal, now)[0]


# -------------------------------------------------------------------- apply
def _log(params: dict[str, Any], result: str) -> None:
    try:
        db.log_action(device="auto_tune", action="weekly_apply", params=params, result=result,
                      trigger="cron", actor="auto_tune")
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune_apply: action_log failed", exc_info=True)


def _expected_week_start(now: datetime) -> str:
    # The review's own local date: the apply may run past local midnight
    # (late TUNING_REVIEW_HOUR_LOCAL + delay), so subtract the delay first.
    review_at = now - timedelta(minutes=max(0, int(config.AUTO_TUNE_APPLY_DELAY_MINUTES)))
    end_day = review_at.astimezone(_tz()).date() - timedelta(days=1)
    return (end_day - timedelta(days=int(config.TUNING_REVIEW_DAYS) - 1)).isoformat()


def _fmt_val(key: str, v: Any) -> str:
    return _pt(v) + KEY_UNITS.get(key, "")


def applied_message(row: dict[str, Any], res: dict[str, Any]) -> str:
    key = res["key"]
    pence = row.get("delta_pence_per_week")
    hrs = row.get("delta_comfort_hours")
    pl = row.get("payload") or {}
    n_days = pl.get("n_days") or 7
    bits = []
    if pence is not None:
        bits.append(f"{'−' if float(pence) < 0 else '+'}{_pt(abs(round(float(pence))))}p/sem")
    if hrs is not None and abs(float(hrs)) >= 0.05:
        bits.append(f"{'+' if float(hrs) >= 0 else '−'}{_pt(abs(round(float(hrs), 1)))} h abaixo do piso")
    partial = " (parcial)" if pl.get("status") == "partial" else ""
    why = f" Simulação da revisão ({n_days} dias){partial}: {', '.join(bits)}." if bits else ""
    return (f"{KEY_LABELS_PT.get(key, key)} {_fmt_val(key, res['old_value'])} → "
            f"{_fmt_val(key, res['new_value'])}.{why} Avalio por 7 dias e reverto sozinho se piorar. "
            f"Reverter agora: POST /api/v1/tuning/ledger/{res['id']}/rollback")


def _review_running() -> bool:
    try:
        from . import tuning_review

        return bool(tuning_review._running)
    except Exception:  # noqa: BLE001
        return False


def _feedback_days(week_start: str) -> int:
    """Distinct local days with comfort feedback in the review's week."""
    try:
        from datetime import date as _date

        d0 = _date.fromisoformat(week_start)
        tz = _tz()
        a = datetime(d0.year, d0.month, d0.day, tzinfo=tz)
        b = a + timedelta(days=7)
        rows = db.get_comfort_feedback(
            since_utc=a.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            until_utc=b.astimezone(UTC).isoformat().replace("+00:00", "Z"))
        days = set()
        for r in rows:
            t = auto_tune._parse(r.get("at_utc"))
            if t is not None:
                days.add(t.astimezone(tz).date())
        return len(days)
    except Exception:  # noqa: BLE001
        return 0


def _comfort_job_proposal(now: datetime) -> dict[str, Any] | None:
    """The proposal the 08:45 comfort job logged within the last day (if any)."""
    try:
        for r in db.get_action_logs(device="comfort", action="weekly_summary", limit=3):
            ts = r.get("timestamp")
            try:
                t = datetime.fromtimestamp(float(ts), UTC)
            except (TypeError, ValueError):
                t = auto_tune._parse(ts)
            if t is None or not (timedelta(minutes=-5) <= now - t <= timedelta(hours=24)):
                continue
            prop = (r.get("params") or {}).get("proposal")
            return prop if isinstance(prop, dict) else None
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune_apply: comfort proposal lookup failed", exc_info=True)
    return None


def _notify(fn_name: str, *args: Any, **kw: Any) -> None:
    try:
        from . import notifier

        getattr(notifier, fn_name)(*args, **kw)
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune_apply: notify failed", exc_info=True)


def _maybe_notify_held(top: dict[str, Any] | None, reason: str, res: dict[str, Any] | None,
                       now: datetime) -> None:
    if not getattr(config, "AUTO_TUNE_NOTIFY_SKIPPED", False) or top is None:
        return
    if reason not in ("busy", "frozen") or _pence(top) < SKIP_NOTIFY_MIN_SAVING_PENCE:
        return
    key = str(top["key"])
    until = (res or {}).get("frozen_until") or db.auto_tune_frozen_until(key, now)
    if reason == "frozen" and until:
        t = auto_tune._parse(until)
        when = t.astimezone(_tz()).strftime("%d/%m") if t else str(until)
        tail = f"chave congelada até {when}"
    else:
        tail = "outra mudança ainda está em avaliação"
    _notify("notify_auto_tune_held",
            f"Sugestão de −{_pt(round(_pence(top)))}p/semana em espera "
            f"({KEY_LABELS_PT.get(key, key)}): {tail}.",
            week_start=str(top.get("week_start")), key=key)


def apply_weekly(now: datetime | None = None) -> dict[str, Any]:
    """Pick and apply the week's single change. Never raises."""
    try:
        return _apply_weekly(now)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_tune_apply.apply_weekly failed (non-fatal)", exc_info=True)
        out = {"applied": False, "reason": "error", "detail": str(e)}
        _log(out, "error")
        return out


def _apply_weekly(now: datetime | None) -> dict[str, Any]:
    t = _now(now)

    def done(reason: str, result: str = "skipped", **extra: Any) -> dict[str, Any]:
        out = {"applied": False, "reason": reason, **extra}
        _log(out, result)
        return out

    if not auto_tune.enabled():
        return done("disabled")
    if not getattr(config, "TUNING_REVIEW_ENABLED", True):
        return done("review_disabled")
    if weekly_quota_used(t) > 0:
        return done("weekly_quota", week=iso_week_label(t))

    if _review_running():
        return done("review_running")

    rows = db.list_tuning_suggestions(1)
    if not rows or rows[0]["week_start"] != _expected_week_start(t):
        return done("no_review", expected_week_start=_expected_week_start(t),
                    latest_week_start=rows[0]["week_start"] if rows else None)
    week_start = rows[0]["week_start"]
    build = rows[0].get("built_at_utc")
    rows = [r for r in rows if r["week_start"] == week_start and r.get("built_at_utc") == build]
    review_status = (rows[0].get("payload") or {}).get("status") if rows else None

    comfort: dict[str, Any] = {}
    try:
        from datetime import date as _date

        from .analytics import comfort_feedback as cf

        comfort = dict(cf.weekly_summary(_date.fromisoformat(week_start)) or {})
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune_apply: comfort summary unavailable", exc_info=True)
    comfort["n_days"] = _feedback_days(week_start)
    comfort["comfort_job_proposal"] = _comfort_job_proposal(t)
    comfort_brief = {k: comfort.get(k) for k in ("n", "cold", "ok", "hot", "n_days")}

    chosen, considered = evaluate_candidates(rows, comfort, t)
    base = {"week_start": week_start, "candidates": considered, "comfort": comfort_brief,
            "review_status": review_status}

    if db.get_active_auto_tune_change():
        out = done("busy", **base)
        _maybe_notify_held(chosen, "busy", None, t)  # the held ping names an ELIGIBLE row
        return out
    if chosen is None:
        frozen_top = next((c for c in considered if c["skipped"] == "frozen"), None)
        if frozen_top is not None:
            held = next((r for r in rows if r["key"] == frozen_top["key"]), None)
            _maybe_notify_held(held, "frozen", None, t)
        return done("nothing_eligible", **base)

    pen = _pence(chosen)
    money = f"saves {round(pen)}p/week" if pen >= 0 else f"costs {round(-pen)}p/week"
    note = (f"review {week_start}: {chosen['verdict']}, {money}"
            + (", partial review" if review_status == "partial" else ""))
    res = auto_tune.propose_change(str(chosen["key"]), chosen["suggested_value"], "tuning_review",
                                   note=note, now=t)
    if not res.get("applied"):
        out = done(str(res.get("reason")), chosen={"key": chosen["key"], "to": chosen["suggested_value"],
                                                   "verdict": chosen["verdict"]},
                   ledger=res, **base)
        _maybe_notify_held(chosen, str(res.get("reason")), res, t)
        return out
    try:
        db.set_kv("auto_tune_last_apply_week", iso_week_label(t))
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune_apply: kv write failed", exc_info=True)
    _log({"applied": True, "id": res["id"], "key": res["key"], "old": res["old_value"],
          "new": res["new_value"], "verdict": chosen["verdict"], "note": note, **base}, "success")
    _notify("notify_auto_tune_applied", applied_message(chosen, res),
            change_id=int(res["id"]), key=str(res["key"]))
    return {"applied": True, "id": res["id"], "key": res["key"], "old_value": res["old_value"],
            "new_value": res["new_value"], "verdict": chosen["verdict"]}


def weekly_apply_job(retry: bool = False) -> None:
    """APScheduler entry (review slot + ``AUTO_TUNE_APPLY_DELAY_MINUTES``).
    Registered unconditionally; decides at run time. If the review is still
    running, backs off 5 minutes ONCE (one-shot job) instead of ``no_review``."""
    if not retry and _review_running():
        try:
            from apscheduler.triggers.date import DateTrigger

            from .scheduler import runner

            sched = runner._background_scheduler
            if sched is not None:
                sched.add_job(weekly_apply_job, DateTrigger(run_date=datetime.now(UTC) + timedelta(minutes=5)),
                              id="auto_tune_weekly_apply_retry", replace_existing=True,
                              kwargs={"retry": True})
                logger.info("auto_tune weekly apply: review still running; retry in 5 min")
                return
        except Exception:  # noqa: BLE001
            logger.warning("auto_tune weekly apply: retry scheduling failed", exc_info=True)
    res = apply_weekly()
    logger.info("auto_tune weekly apply: applied=%s reason=%s", res.get("applied"), res.get("reason"))


# -------------------------------------------------------------- recommendations
def ledger_for_item(key: str, new_value: Any, now: datetime | None = None,
                    days: int = 14, *, today: Any = None, tz: Any = None) -> dict[str, Any] | None:
    """Ledger row summary for the Insights tuning item of ``key``: the newest
    ``evaluating`` / ``kept`` / ``rolled_back`` row applied in the last ``days``
    days. ``is_active_suggestion`` = that row is the evaluating change that set
    ``new_value`` (so the item is a status line, not an Apply button)."""
    try:
        t = _now(now)
        for r in db.get_auto_tune_changes(10, key=key):
            if r["status"] not in ("evaluating", "kept", "rolled_back"):
                continue
            at = auto_tune._parse(r.get("applied_at"))
            if at is None or t - at > timedelta(days=days):
                continue
            tz = tz or _tz()
            day_n = ((today or t.astimezone(tz).date()) - at.astimezone(tz).date()).days + 1
            spec = rts.SCHEMA.get(key)
            same = False
            try:
                same = spec is not None and _canon(spec, r["new_value"]) == _canon(spec, new_value)
            except Exception:  # noqa: BLE001
                same = False
            return {"status": r["status"], "applied_at": r["applied_at"], "day_n": day_n,
                    "id": r["id"], "is_active_suggestion": bool(r["status"] == "evaluating" and same),
                    "weekday_pt": _WEEKDAY_PT[at.astimezone(tz).weekday()],
                    "applied_local": at.astimezone(tz).strftime("%d/%m")}
        return None
    except Exception:  # noqa: BLE001
        return None
