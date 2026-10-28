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

REFUSALS = ("disabled", "not_allowlisted", "denylisted", "busy", "frozen",
            "step_too_large", "drift_cap", "away", "unchanged", "invalid")
# Extra, only if the ledger insert itself fails after the setting was written
# (the setting is restored first): "ledger_error".

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
    """What the system really uses: honours a pinned in-memory ``config`` override."""
    try:
        from .config import config

        return config._rt_get(key)
    except Exception:  # noqa: BLE001
        return rts.get_setting(key)


def _is_away() -> bool:
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
def baseline_metrics(end_day: date | None = None) -> dict[str, Any]:
    """7-day metrics ending at ``end_day`` (default yesterday UTC) from
    ``cosy_scorecard_daily``: net GBP/day mean, hours below floor per day
    (night + peak), shower-shortfall days, peak import kWh/day. Guarded: ``{}``
    when no rows."""
    try:
        end = end_day or (datetime.now(UTC).date() - timedelta(days=1))
        rows = [r for r in db.get_cosy_scorecards(7, up_to=end.isoformat())
                if (end - date.fromisoformat(r["date"])).days < 7]
        if not rows:
            return {}

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
            if t.get("any_below_floor") is True and not (p.get("holiday") or p.get("vacation")):
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
    autos = [r for r in db.get_auto_tune_changes(500, key=key) if r["status"] != "manual"]
    return autos[-1]["old_value"] if autos else None


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
    (allowlisted keys and keys with ledger rows only). Two cheap queries."""
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = db.get_auto_tune_changes(500)
        by_key: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_key.setdefault(r["key"], []).append(r)
        for k in set(allowlist()) | set(by_key):
            rs = by_key.get(k, [])
            out[k] = {
                "active": next((r["id"] for r in rs if r["status"] == "evaluating"), None),
                "frozen_until": db.auto_tune_frozen_until(k),
                "last_status": rs[0]["status"] if rs else None,
            }
    except Exception:  # noqa: BLE001
        logger.debug("auto_tune.settings_overlay failed", exc_info=True)
    return out


def scorecard_section(today: date | None = None) -> dict[str, Any]:
    """``payload.auto_tune`` for the Cosy scorecard (as of build time)."""
    a = db.get_active_auto_tune_change()
    if not a:
        return {"active_change": None}
    t = _parse(a["applied_at"])
    d0 = t.date() if t else (today or datetime.now(UTC).date())
    n = max(1, min(7, ((today or datetime.now(UTC).date()) - d0).days + 1))
    return {"active_change": {"id": a["id"], "key": a["key"], "new_value": a["new_value"],
                              "day_n_of_7": n}}


# ------------------------------------------------------------------ propose
def propose_change(key: str, new_value: Any, source: str, *, note: str | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """Apply ONE bounded automated change and open an ``evaluating`` ledger row.

    Returns ``{"applied": True, "id", "key", "old_value", "new_value", ...}`` or
    ``{"applied": False, "reason": <one of REFUSALS>, ...}``. Never raises for a
    policy refusal.
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
    if source not in SOURCES:
        return no("invalid", detail=f"unknown source {source!r}")
    spec = rts.SCHEMA[key]
    try:
        canonical = rts._validate(spec, new_value)
    except rts.SettingValidationError as exc:
        return no("invalid", detail=str(exc))

    with _lock:
        try:
            cur_raw = current_value(key)
            cur = rts._validate(spec, cur_raw)
        except Exception as exc:  # noqa: BLE001
            return no("invalid", detail=f"current value unreadable: {exc}")
        if canonical == cur:
            return no("unchanged")
        if db.get_active_auto_tune_change():
            return no("busy")
        frozen = db.auto_tune_frozen_until(key, t)
        if frozen:
            return no("frozen", frozen_until=frozen)
        numeric = spec.enum is None and spec.type_name in ("float", "int")
        if numeric:
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
            try:
                rts.set_setting(key, cur, actor="auto_tune:rollback")
            except Exception:  # noqa: BLE001
                logger.error("auto_tune: restore of %s failed", key, exc_info=True)
            return no("ledger_error")

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
        if actor.startswith("auto_tune:") or actor in INTERNAL_ACTORS:
            return
        if is_denylisted(key):
            return
        has_rows = bool(db.get_auto_tune_changes(1, key=key))
        if not (key.upper() in allowlist() or has_rows):
            return
        t = _now()
        days = int(rts.get_setting("AUTO_TUNE_MANUAL_FREEZE_DAYS"))
        frozen = _z(t + timedelta(days=days)) if days > 0 else None
        for r in db.get_auto_tune_changes(20, key=key, status="evaluating"):
            db.update_auto_tune_change(
                r["id"], status="superseded_manual", evaluated_at=_z(t),
                evidence={"reason": "manual_change", "actor": actor})
        cid = db.insert_auto_tune_change(
            key=key, old_value=old_value, new_value=new_value, source="manual",
            status="manual", applied_at=_z(t), frozen_until=frozen,
            note=f"actor={actor}")
        _log("auto_tune_manual", {"id": cid, "key": key, "old": old_value, "new": new_value,
                                  "actor": actor, "frozen_until": frozen})
    except Exception:  # noqa: BLE001
        logger.warning("auto_tune.on_manual_change failed (non-fatal)", exc_info=True)


# --------------------------------------------------------------------- verbs
def rollback(change_id: int, *, reason: str, evidence: dict[str, Any] | None = None,
             actor: str = "judge") -> dict[str, Any]:
    """Restore ``old_value``, mark ``rolled_back``, freeze the key, notify once.
    Only ``evaluating`` / ``kept`` automated rows can be rolled back; a
    ``manual`` row is never reverted."""
    row = db.get_auto_tune_change(change_id)
    if row is None:
        return {"rolled_back": False, "reason": "not_found"}
    if row["status"] not in ("evaluating", "kept"):
        return {"rolled_back": False, "reason": f"not_rollbackable:{row['status']}"}
    if row["old_value"] is None:
        return {"rolled_back": False, "reason": "no_old_value"}
    key, t = row["key"], _now()
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
