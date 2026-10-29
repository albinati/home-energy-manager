"""Closed-loop auto-tune (#876), story 1: the change ledger.

This module is the ONE place for ledger semantics:

* ``auto_tune_changes`` (db.py) is the append-mostly history of every automated
  change to a runtime setting, plus ``manual`` rows for owner changes to those
  keys.
* ``propose_change`` is the only way an automated source may change a setting.
  It enforces: kill switch, allowlist/denylist, one change at a time, one spec
  step, seasonal drift cap, per-key freeze, not while away.
* ``on_manual_change`` is called from ``runtime_settings.set_setting`` /
  ``delete_setting``: the owner always wins (supersede + freeze).
* ``rollback`` / ``keep`` / ``freeze`` are the verbs the judge (story 3) and the
  API/MCP use.

Story 1 contains NO automatic apply path of its own and NO judge. The only
caller of ``propose_change`` besides the API is the comfort-feedback weekly job.

Kill switch (``AUTO_TUNE_ENABLED``, code default false): nothing is ever applied
automatically. Manual rows are STILL recorded when off (cheap, and the history is
what the drift cap and the Settings surface need once it is turned on).

Denylist note: legionella keys ARE runtime settings (``DHW_LEGIONELLA_*``), so
they are denied by substring. The stand-off keys (``DHW_LEGIONELLA_STANDOFF_*``)
are env-only today; the substring rule covers them if they ever become runtime.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import db
from . import runtime_settings as rts

logger = logging.getLogger(__name__)

SOURCES = ("tuning_review", "comfort_feedback", "learned_param", "plan_pattern")
STATUSES = ("evaluating", "kept", "rolled_back", "superseded_manual", "manual")

# Keys no setting can ever put on the allowlist. ``AUTO_TUNE_*`` (prefix) and
# anything containing ``LEGIONELLA`` are denied as well (see ``is_denylisted``).
DENYLIST: frozenset[str] = frozenset({
    "LP_W3_EMERGENCY_FLOOR_C",
    "DHW_TEMP_MIN_FLOOR_C",
    "DAIKIN_CONTROL_MODE",
    "OPENCLAW_READ_ONLY",
    "MIN_SOC_RESERVE_PERCENT",
    "OPTIMIZATION_PRESET",
    "VACATION_RETURN_AT",
    "REQUIRE_SIMULATION_ID",
})
DENY_PREFIXES = ("AUTO_TUNE_",)
DENY_SUBSTRINGS = ("LEGIONELLA",)

# One bounded step per change. SettingSpec has no step field, so the ledger owns
# them. A key missing here falls back to (max-min)/20, else 1/20 of |value|.
STEPS: dict[str, float] = {
    "DHW_TEMP_NORMAL_C": 1.0,
    "DHW_TEMP_COMFORT_C": 1.0,
    "LP_W3_NIGHT_FLOOR_C": 0.5,
    "LP_W3_PEAK_COAST_DELTA_C": 0.5,
    "INDOOR_SETPOINT_C": 0.5,
    "LP_W3_CEILING_C": 0.5,
    "LP_W3_SOLAR_GAIN_KW_PER_PV_KW": 0.1,
    "LP_W3_INTERNAL_GAIN_KW": 0.05,
    "LP_W3_EMITTER_TAU_MIN": 10.0,
}

# Max cumulative distance from the owner's last manual value (or the value at the
# first automated change when the owner never touched the key). Comfort keys
# 1.5 C, DHW keys 3 C, everything else: STEPS x 3. Enum keys have no drift cap.
MAX_DRIFT: dict[str, float] = {
    "INDOOR_SETPOINT_C": 1.5,
    "LP_W3_NIGHT_FLOOR_C": 1.5,
    "LP_W3_PEAK_COAST_DELTA_C": 1.5,
    "LP_W3_CEILING_C": 1.5,
    "DHW_TEMP_NORMAL_C": 3.0,
    "DHW_TEMP_COMFORT_C": 3.0,
}

# Writers that are not the owner. They never create ``manual`` rows.
# (``auto_tune:*`` is matched by prefix.) ``vacation_end`` = #873 recovery,
# ``*_rollback`` = batch/workbench undo of the owner's own failed batch.
INTERNAL_ACTORS: frozenset[str] = frozenset({
    "vacation_end",
    "comfort_feedback_auto_tune",
    "api_batch_rollback",
    "workbench_promote_rollback",
})

REFUSALS = ("disabled", "not_allowlisted", "denylisted", "cron_reload", "invalid", "pinned",
            "unchanged", "busy", "frozen", "step_too_large", "drift_cap", "away",
            "no_baseline", "changed")
# Extra, only if the ledger insert itself fails after the setting was written
# (the setting is restored first): "ledger_error".

MIN_BASELINE_DAYS = 5
BATCH_UNDO_ACTORS = {"api_batch_rollback": "api_batch", "workbench_promote_rollback": "workbench_promote"}

_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(iso: Any) -> datetime | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def enabled() -> bool:
    try:
        return str(rts.get_setting("AUTO_TUNE_ENABLED")).strip().lower() == "true"
    except Exception:  # noqa: BLE001
        return False


def allowlist() -> list[str]:
    try:
        raw = str(rts.get_setting("AUTO_TUNE_ALLOWLIST") or "")
    except Exception:  # noqa: BLE001
        raw = ""
    return [k.strip().upper() for k in raw.split(",") if k.strip()]


def is_denylisted(key: str) -> bool:
    k = key.upper()
    return (k in DENYLIST or k.startswith(DENY_PREFIXES)
            or any(s in k for s in DENY_SUBSTRINGS))


def is_allowlisted(key: str) -> bool:
    return key.upper() in allowlist() and not is_denylisted(key)


def step_for(key: str) -> float:
    if key in STEPS:
        return STEPS[key]
    spec = rts.SCHEMA.get(key)
    if spec is not None and spec.min_value is not None and spec.max_value is not None:
        return (float(spec.max_value) - float(spec.min_value)) / 20.0
    return 1.0


def max_drift(key: str) -> float | None:
    if key in MAX_DRIFT:
        return MAX_DRIFT[key]
    spec = rts.SCHEMA.get(key)
    if spec is None or spec.enum is not None or spec.type_name not in ("float", "int"):
        return None
    return step_for(key) * 3.0


def current_value(key: str) -> Any:
    """The PERSISTED value (what a write replaces). A pinned in-memory ``config``
    override is handled by the ``pinned`` refusal, never used as a reference."""
    return rts.get_setting(key)


def _is_pinned(key: str) -> bool:
    try:
        from .config import config

        return bool(config.has_override(key))
    except Exception:  # noqa: BLE001
        return False


def _is_away() -> bool:
    try:
        from . import vacation

        if vacation.current_preset() == "vacation":
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        if (db.get_kv("vacation_end_at") or "").strip():
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        import json

        raw = (db.get_kv("daikin_holiday_active") or "").strip()
        if raw:
            st = json.loads(raw)
            if isinstance(st, dict) and any(v is True for v in st.values()):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


# ----------------------------------------------------------------- baseline
def _local_yesterday() -> date:
    try:
        from zoneinfo import ZoneInfo

        from .config import config

        return (datetime.now(ZoneInfo(config.BULLETPROOF_TIMEZONE)) - timedelta(days=1)).date()
    except Exception:  # noqa: BLE001
        return datetime.now(UTC).date() - timedelta(days=1)


def baseline_metrics(end_day: date | None = None) -> dict[str, Any]:
    """7-day metrics ending at ``end_day`` (default: yesterday in
    ``BULLETPROOF_TIMEZONE``, the key of ``cosy_scorecard_daily``): net GBP/day
    mean, hours below floor per day (night + peak), shower-shortfall days, peak
    import kWh/day. Holiday / vacation days are excluded from EVERY metric (an
    empty house is not a baseline). ``{}`` when there are no rows;
    ``{"no_baseline": True, "n_days": n}`` when fewer than ``MIN_BASELINE_DAYS``
    usable days remain. Guarded."""
    try:
        end = end_day or _local_yesterday()
        rows = [r for r in db.get_cosy_scorecards(7, up_to=end.isoformat())
                if (end - date.fromisoformat(r["date"])).days < 7]
        rows = [r for r in rows
                if not ((r.get("payload") or {}).get("holiday") or (r.get("payload") or {}).get("vacation"))]
        if not rows:
            return {}
        if len(rows) < MIN_BASELINE_DAYS:
            return {"no_baseline": True, "n_days": len(rows), "end_day": end.isoformat()}

        def mean(vals: list[float]) -> float | None:
            return round(sum(vals) / len(vals), 4) if vals else None

        net = [float(r["net_cost_gbp"]) for r in rows if r.get("net_cost_gbp") is not None]
        peak = [float(r["peak_import_kwh"]) for r in rows if r.get("peak_import_kwh") is not None]
        below, short = [], 0
        for r in rows:
            p = r.get("payload") or {}
            c = p.get("comfort") if isinstance(p.get("comfort"), dict) else {}
            hn, hp = c.get("hours_below_night_floor"), c.get("hours_below_peak_floor")
            if hn is not None or hp is not None:
                below.append(float(hn or 0) + float(hp or 0))
            t = p.get("tank") if isinstance(p.get("tank"), dict) else {}
            if t.get("any_below_floor") is True:
                short += 1
        return {
            "end_day": end.isoformat(), "n_days": len(rows),
            "net_gbp_per_day": mean(net),
            "hours_below_floor_per_day": mean(below),
            "shower_shortfall_days": short,
            "peak_import_kwh_per_day": mean(peak),
        }
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune.baseline_metrics failed", exc_info=True)
        return {}


# ------------------------------------------------------------------ queries
def manual_value(key: str) -> Any:
    """Drift reference: newest ``manual`` row's value, else the value before the
    first automated change on the key, else None."""
    manual = db.get_auto_tune_changes(1, key=key, status="manual")
    if manual:
        return manual[0]["new_value"]
    first = db.get_auto_tune_changes(1, key=key, exclude_status="manual", oldest_first=True)
    return first[0]["old_value"] if first else None


def ledger_state(key: str) -> dict[str, Any]:
    rows = db.get_auto_tune_changes(50, key=key)
    active = next((r for r in rows if r["status"] == "evaluating"), None)
    autos = [r for r in rows if r["status"] != "manual"]
    auto_row = next((r for r in autos if r["status"] in ("evaluating", "kept")), None)
    return {
        "key": key,
        "active": active,
        "last": rows[0] if rows else None,
        "frozen_until": db.auto_tune_frozen_until(key),
        "manual_value": manual_value(key),
        "auto_value": auto_row["new_value"] if auto_row else None,
    }


def ledger_summary(limit: int = 20, key: str | None = None) -> dict[str, Any]:
    rows = db.get_auto_tune_changes(limit, key=key)
    keys = sorted(set(allowlist()) | {r["key"] for r in rows})
    if key:
        keys = [key]
    return {
        "enabled": enabled(),
        "allowlist": allowlist(),
        "active": db.get_active_auto_tune_change(),
        "rows": rows,
        "keys": {k: {"frozen_until": db.auto_tune_frozen_until(k)} for k in keys},
    }


def settings_overlay() -> dict[str, dict[str, Any]]:
    """Per-key ``{active, frozen_until, last_status}`` for the Settings list
    (allowlisted keys and keys with ledger rows only). One GROUP BY query."""
    try:
        summ = db.auto_tune_key_summary()
        empty = {"active": None, "frozen_until": None, "last_status": None}
        return {k: summ.get(k, dict(empty)) for k in set(allowlist()) | set(summ)}
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune.settings_overlay failed", exc_info=True)
        return {}


def scorecard_section(day: date, start: datetime, end: datetime, tz: Any) -> dict[str, Any]:
    """``payload.auto_tune`` for the Cosy scorecard of local ``day``
    (``[start, end)`` UTC): the newest automated change that was applied before
    the day ended and not evaluated before it started. ``day_n_of_7`` counts
    local days from the application day to ``day`` (not capped; > 7 means the
    judge has not closed it)."""
    for r in db.get_auto_tune_changes(50):
        if r["status"] == "manual":
            continue
        t = _parse(r["applied_at"])
        if t is None or t >= end:
            continue
        ev = _parse(r.get("evaluated_at"))
        if ev is not None and ev < start:
            continue
        n = (day - t.astimezone(tz).date()).days + 1
        out = {"id": r["id"], "key": r["key"], "new_value": r["new_value"],
               "status": r["status"], "day_n_of_7": n}
        try:
            from . import auto_tune_judge  # story 3: last tripwire check / tripped wire

            out.update(auto_tune_judge.scorecard_fields(r))
        except Exception:  # noqa: BLE001
            logger.debug("auto_tune.scorecard_section: judge fields failed", exc_info=True)
        return {"active_change": out}
    return {"active_change": None}


# ------------------------------------------------------------------ propose
def _same(spec: Any, a: Any, b: Any) -> bool:
    try:
        return rts._validate(spec, a) == rts._validate(spec, b)
    except Exception:  # noqa: BLE001
        return str(a) == str(b)


def propose_change(key: str, new_value: Any, source: str, *, note: str | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """Apply ONE bounded automated change and open an ``evaluating`` ledger row.

    Returns ``{"applied": True, "id", "key", "old_value", "new_value", ...}`` or
    ``{"applied": False, "reason": <one of REFUSALS | ledger_error>, ...}``. Never
    raises for a policy refusal.
    """
    key = str(key).strip()
    t = _now(now)

    def no(reason: str, **extra: Any) -> dict[str, Any]:
        return {"applied": False, "reason": reason, "key": key, **extra}

    if not enabled():
        return no("disabled")
    if is_denylisted(key):
        return no("denylisted")
    if not is_allowlisted(key) or key not in rts.SCHEMA:
        return no("not_allowlisted")
    spec = rts.SCHEMA[key]
    if spec.cron_reload:
        return no("cron_reload")  # a write would need the scheduler re-registered
    if source not in SOURCES:
        return no("invalid", detail=f"unknown source {source!r}")
    try:
        canonical = rts._validate(spec, new_value)
    except rts.SettingValidationError as exc:
        return no("invalid", detail=str(exc))

    with _lock:
        if _is_pinned(key):
            return no("pinned")  # in-memory override (Workbench what-if) shadows the DB
        try:
            cur = rts._validate(spec, current_value(key))
        except Exception as exc:  # noqa: BLE001
            return no("invalid", detail=f"current value unreadable: {exc}")
        if canonical == cur:
            return no("unchanged")
        if db.get_active_auto_tune_change():
            return no("busy")
        frozen = db.auto_tune_frozen_until(key, t)
        if frozen:
            return no("frozen", frozen_until=frozen)
        if spec.enum is not None:
            en = list(spec.enum)
            if abs(en.index(canonical) - en.index(cur)) != 1:
                return no("step_too_large", step=1)
        elif spec.type_name in ("float", "int"):
            if abs(float(canonical) - float(cur)) > step_for(key) + 1e-9:
                return no("step_too_large", step=step_for(key))
            cap = max_drift(key)
            ref = manual_value(key)
            ref_f = float(ref) if ref is not None else float(cur)
            if cap is not None and abs(float(canonical) - ref_f) > cap + 1e-9:
                return no("drift_cap", cap=cap, reference=ref_f)
        if _is_away():
            return no("away")
        baseline = baseline_metrics()
        if baseline.get("no_baseline") or not baseline.get("n_days"):
            return no("no_baseline", n_days=baseline.get("n_days", 0), min_days=MIN_BASELINE_DAYS)

        # Owner wrote between our read and now? (the hook cannot run until we release _lock)
        if not _same(spec, current_value(key), cur):
            return no("changed")
        try:
            rts.set_setting(key, canonical, actor=f"auto_tune:{source}")
        except rts.SettingValidationError as exc:
            return no("invalid", detail=str(exc))
        try:
            cid = db.insert_auto_tune_change(
                key=key, old_value=cur, new_value=canonical, source=source,
                status="evaluating", applied_at=_z(t), baseline=baseline, note=note)
        except Exception:  # noqa: BLE001
            logger.warning("auto_tune: ledger insert failed; restoring %s", key, exc_info=True)
            restored = True
            try:
                rts.set_setting(key, cur, actor="auto_tune:rollback")
            except Exception:  # noqa: BLE001
                restored = False
                logger.error("auto_tune: restore of %s failed", key, exc_info=True)
            if not restored:
                try:
                    from .notifier import notify_risk

                    notify_risk(f"Auto-tune: falha ao gravar o registro E ao restaurar {key} "
                                f"(valor atual {canonical}, anterior {cur}).",
                                extra={"warning_key": f"auto_tune_ledger_error_{key}"})
                except Exception:  # noqa: BLE001
                    logger.warning("auto_tune: ledger_error notify failed", exc_info=True)
            return no("ledger_error", restored=restored)

    _log("auto_tune_apply", {"id": cid, "key": key, "old": cur, "new": canonical,
                             "source": source, "baseline": baseline, "note": note})
    return {"applied": True, "id": cid, "key": key, "old_value": cur,
            "new_value": canonical, "source": source, "baseline": baseline}


def _log(action: str, params: dict[str, Any], result: str = "success") -> None:
    try:
        db.log_action(device="auto_tune", action=action, params=params,
                      result=result, trigger="auto_tune", actor="auto_tune")
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune: action_log failed", exc_info=True)


# ------------------------------------------------------------ manual always wins
def on_manual_change(key: str, old_value: Any, new_value: Any, actor: str) -> None:
    """Hook from ``runtime_settings``. Never raises."""
    try:
        actor = str(actor or "")
        if actor in BATCH_UNDO_ACTORS:
            _undo_batch_manual(key, BATCH_UNDO_ACTORS[actor])
            return
        if actor.startswith("auto_tune:") or actor in INTERNAL_ACTORS:
            return
        if is_denylisted(key) or new_value is None:
            return
        spec = rts.SCHEMA.get(key)
        if spec is not None and _same(spec, old_value, new_value):
            return  # a no-op write is not a decision
        with _lock:  # waits for an in-flight propose_change on this process
            if not (key.upper() in allowlist() or db.get_auto_tune_changes(1, key=key)):
                return
            t = _now()
            # A proposer that wrote AFTER the owner overwrote the owner's value: restore it.
            try:
                if spec is not None and not _same(spec, current_value(key), new_value):
                    rts.set_setting(key, new_value, actor="auto_tune:rollback")
            except Exception:  # noqa: BLE001
                logger.warning("auto_tune: could not restore owner's value for %s", key, exc_info=True)
            days = int(rts.get_setting("AUTO_TUNE_MANUAL_FREEZE_DAYS"))
            frozen = _z(t + timedelta(days=days)) if days > 0 else None
            cid = db.insert_auto_tune_change(
                key=key, old_value=old_value, new_value=new_value, source="manual",
                status="manual", applied_at=_z(t), frozen_until=frozen,
                note=f"actor={actor}")
            for r in db.get_auto_tune_changes(20, key=key, status="evaluating"):
                db.update_auto_tune_change(
                    r["id"], status="superseded_manual", evaluated_at=_z(t),
                    evidence={"reason": "manual_change", "actor": actor, "manual_id": cid})
            _log("auto_tune_manual", {"id": cid, "key": key, "old": old_value, "new": new_value,
                                      "actor": actor, "frozen_until": frozen})
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune.on_manual_change failed (non-fatal)", exc_info=True)


def _undo_batch_manual(key: str, batch_actor: str) -> None:
    """A failed batch / workbench promote is being undone: the manual row (and
    freeze) the hook just recorded for ``key`` was never a real decision. Delete it
    and re-open the ``evaluating`` row it superseded."""
    with _lock:
        for m in db.get_auto_tune_changes(5, key=key, status="manual"):
            if (m.get("note") or "") != f"actor={batch_actor}":
                continue
            for r in db.get_auto_tune_changes(20, key=key, status="superseded_manual"):
                if (r.get("evidence") or {}).get("manual_id") == m["id"]:
                    db.update_auto_tune_change(r["id"], status="evaluating", evaluated_at=None,
                                               evidence={})
            db.delete_auto_tune_change(m["id"])
            _log("auto_tune_manual_undone", {"key": key, "manual_id": m["id"], "actor": batch_actor})
            break


# --------------------------------------------------------------------- verbs
def rollback(change_id: int, *, reason: str, evidence: dict[str, Any] | None = None,
             actor: str = "judge") -> dict[str, Any]:
    """Restore ``old_value``, mark ``rolled_back``, freeze the key, notify once.
    Only ``evaluating`` / ``kept`` automated rows can be rolled back, and only if
    the row is still the NEWEST row of its key and the setting still holds its
    ``new_value`` (otherwise ``superseded``: a later owner/auto change must not be
    overwritten). A ``manual`` row is never reverted."""
    row = db.get_auto_tune_change(change_id)
    if row is None:
        return {"rolled_back": False, "reason": "not_found"}
    if row["status"] not in ("evaluating", "kept"):
        return {"rolled_back": False, "reason": f"not_rollbackable:{row['status']}"}
    if row["old_value"] is None:
        return {"rolled_back": False, "reason": "no_old_value"}
    key, t = row["key"], _now()
    spec = rts.SCHEMA.get(key)
    if spec is None:
        return {"rolled_back": False, "reason": "invalid"}
    with _lock:
        all_rows = db.get_auto_tune_changes(500, key=key)
        max_id = max(r["id"] for r in all_rows) if all_rows else row["id"]
        if row["id"] != max_id or not _same(spec, current_value(key), row["new_value"]):
            return {"rolled_back": False, "reason": "superseded",
                    "newest_id": max_id, "current": current_value(key)}
        try:
            rts.set_setting(key, row["old_value"], actor="auto_tune:rollback")
        except rts.SettingValidationError as exc:
            return {"rolled_back": False, "reason": "invalid", "detail": str(exc)}
    days = int(rts.get_setting("AUTO_TUNE_ROLLBACK_FREEZE_DAYS"))
    ev = {**(evidence or {}), "reason": reason, "actor": actor}
    db.update_auto_tune_change(
        change_id, status="rolled_back", evaluated_at=_z(t), evidence=ev,
        frozen_until=_z(t + timedelta(days=days)) if days > 0 else None)
    _log("auto_tune_rollback", {"id": change_id, "key": key, "restored": row["old_value"],
                                "from": row["new_value"], **ev})
    wkey = f"auto_tune_rollback_{change_id}"
    try:
        if not db.is_warning_acknowledged(wkey):
            from .notifier import notify_risk

            notify_risk(
                f"Auto-tune reverteu {key}: {row['new_value']} -> {row['old_value']} ({reason}).",
                extra={"warning_key": wkey, "evidence": ev})
            db.acknowledge_warning(wkey)
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune: rollback notify failed", exc_info=True)
    return {"rolled_back": True, "id": change_id, "key": key, "restored": row["old_value"]}


def keep(change_id: int, evidence: dict[str, Any] | None = None) -> bool:
    row = db.get_auto_tune_change(change_id)
    if row is None or row["status"] != "evaluating":
        return False
    db.update_auto_tune_change(change_id, status="kept", evaluated_at=_z(_now()),
                               evidence=evidence or {})
    _log("auto_tune_keep", {"id": change_id, "key": row["key"], **(evidence or {})})
    return True


def freeze(change_id: int | None = None, *, key: str | None = None, days: int) -> dict[str, Any]:
    """Freeze a key for ``days`` (from now). By ``change_id`` it stamps that row;
    by ``key`` it stamps the key's newest row, or, when the key has none, records
    a ``manual`` row pinning the current value (the owner's "fix this value")."""
    t = _now()
    until = _z(t + timedelta(days=max(0, int(days))))
    if change_id is not None:
        row = db.get_auto_tune_change(change_id)
        if row is None:
            return {"frozen": False, "reason": "not_found"}
        db.update_auto_tune_change(change_id, frozen_until=until)
        key = row["key"]
    else:
        if not key:
            return {"frozen": False, "reason": "invalid"}
        rows = db.get_auto_tune_changes(1, key=key)
        if rows:
            db.update_auto_tune_change(rows[0]["id"], frozen_until=until)
        else:
            cur = current_value(key) if key in rts.SCHEMA else None
            db.insert_auto_tune_change(key=key, old_value=cur, new_value=cur, source="manual",
                                       status="manual", applied_at=_z(t), frozen_until=until,
                                       note="freeze")
    _log("auto_tune_freeze", {"key": key, "id": change_id, "frozen_until": until})
    return {"frozen": True, "key": key, "frozen_until": until}
