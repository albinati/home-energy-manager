"""Vacation mode v2 (#873) - "away until <return>" as ONE managed action.

``OPTIMIZATION_PRESET=vacation`` used to be a bare LP flag. It is now a trip with
a mandatory return time:

* ``VACATION_RETURN_AT`` (local ISO datetime, ``BULLETPROOF_TIMEZONE``) must be set
  and in the future to enter vacation (:class:`VacationReturnAtError`, mapped to
  422 by the API and to a clear error by the MCP tool).
* Activation (``vacation_start``). ORDER MATTERS: persist ``vacation_end_at``
  (= the recovery fire time) in ``kv_state`` FIRST, arm the recovery job, THEN
  POST the Onecta holiday window, THEN replan. A failed POST therefore still
  leaves a scheduled recovery and an alert.
* Recovery (``vacation_end``): holiday off -> read device -> power climate/tank on
  when off -> read back -> preset normal + clear the return time -> MPC with
  ``force_write_devices`` -> one Telegram. Idempotent; every step is logged and an
  error never blocks the next step.
* Cancel early: leaving vacation while ``vacation_end_at`` is stored runs the same
  recovery immediately.

Kill switch ``VACATION_DAIKIN_HOLIDAY_ENABLED=false``: no Onecta write, no kv, no
recovery job (the pre-#873 behaviour of the bare preset).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import config
from .daikin import service as daikin_service
from .runtime_settings import SettingValidationError

logger = logging.getLogger(__name__)

KV_END_AT = "vacation_end_at"      # ISO UTC of the recovery fire time ("" = none)
KV_STATE = "vacation_state"        # JSON: return_at, recovery_at, start/end date, holiday_until_sent...
JOB_ID = "vacation_end"
_MIN_LEAD = timedelta(seconds=60)

_recovery_lock = threading.Lock()
# Held from begin_activation until finish_activation ends: a recovery / cancel that
# arrives while the holiday-ON POST is still in flight WAITS for it (else holiday-OFF
# could land first and the unit would stay in holiday with the preset back to normal).
_activation_lock = threading.Lock()
_ACTIVATION_WAIT_S = 180.0

# After-write helpers are background threads in production; tests flip this off.
BACKGROUND = True

_MPC_ATTEMPTS = 5            # bulletproof_mpc_job returns False when the dispatch lock is busy
_MPC_RETRY_WAIT_S = 20.0
_HOLIDAY_POLL_ATTEMPTS = 3   # Onecta lags after holiday-off
_HOLIDAY_POLL_WAIT_S = 30.0
_SNAP_MAX_AFTER = timedelta(hours=3)


class VacationReturnAtError(SettingValidationError):
    """Entering vacation without a valid future ``VACATION_RETURN_AT`` (HTTP 422)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(str(config.BULLETPROOF_TIMEZONE or "Europe/London"))
    except Exception:  # pragma: no cover - bad tz name
        return ZoneInfo("Europe/London")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso_utc(raw: Any) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def normalize_preset(value: Any) -> str:
    v = str(value or "normal").strip().lower()
    return "vacation" if v in ("travel", "away") else v


def current_preset() -> str:
    return normalize_preset(config.OPTIMIZATION_PRESET)


def parse_return_at(raw: Any, tz: ZoneInfo | None = None) -> datetime | None:
    """Local ISO datetime (naive = ``BULLETPROOF_TIMEZONE``) -> aware UTC; None when unset/invalid."""
    s = str(raw or "").strip()
    if not s:
        return None
    if s[-1] in "zZ":  # the settings layer lower-cases strings: "...t18:00:00z"
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive local time: resolve in BULLETPROOF_TIMEZONE (fold=0: an ambiguous
        # autumn hour reads as the first occurrence; a nonexistent spring-forward
        # hour is shifted forward by zoneinfo).
        dt = dt.replace(tzinfo=tz or _tz())
    return dt.astimezone(UTC)


def require_return_at(raw: Any = None, now: datetime | None = None) -> datetime:
    """Return the validated future return time (UTC) or raise :class:`VacationReturnAtError`."""
    now = now or datetime.now(UTC)
    raw = config.VACATION_RETURN_AT if raw is None else raw
    dt = parse_return_at(raw)
    if dt is None:
        raise VacationReturnAtError(
            "OPTIMIZATION_PRESET=vacation requires VACATION_RETURN_AT: a local ISO datetime "
            "in the future (e.g. 2026-11-07T18:00)"
        )
    if dt <= now:
        raise VacationReturnAtError(
            f"VACATION_RETURN_AT ({raw}) must be in the future"
        )
    return dt


def check_preset_changes(changes: dict[str, Any], now: datetime | None = None) -> None:
    """Validate a settings change set; raises :class:`VacationReturnAtError`.

    * ``OPTIMIZATION_PRESET`` -> vacation needs a future return time (taken from the
      same change set when present, else the stored setting).
    * A new ``VACATION_RETURN_AT`` while already in vacation must also be future.
    """
    preset = changes.get("OPTIMIZATION_PRESET")
    if preset is not None:
        if normalize_preset(preset) == "vacation":
            require_return_at(changes.get("VACATION_RETURN_AT", config.VACATION_RETURN_AT), now)
        return
    if "VACATION_RETURN_AT" in changes and current_preset() == "vacation":
        require_return_at(changes["VACATION_RETURN_AT"], now)


# ---------------------------------------------------------------------------
# Timing: recovery fire time and the firmware fallback window
# ---------------------------------------------------------------------------

def _snap_to_cheap_band_start(target: datetime, now: datetime | None = None) -> datetime:
    """Start of the latest CHEAP band at or before ``target`` on a banded tariff -
    but ONLY when the target is inside that band or within 3 h after its start
    (a longer snap would heat through the day/peak bands for hours), and never to
    a time before ``now + 60 s``. Dynamic tariff / failure / no match: unchanged.
    """
    try:
        from .analytics.load_expected import band_windows_for_day

        tz = _tz()
        day = target.astimezone(tz).date()
        cands: list[datetime] = []
        for d in (day - timedelta(days=1), day):
            wins, kind = band_windows_for_day(d, tz)
            if kind != "banded":
                return target
            cands.extend(w.start_utc for w in wins if w.key == "band_cheap" and w.start_utc <= target)
        if not cands:
            return target
        start = max(cands)
        if target - start > _SNAP_MAX_AFTER:
            return target
        if now is not None and start < now + _MIN_LEAD:
            return target
        return start
    except Exception as exc:  # noqa: BLE001 - snapping is an optimisation
        logger.debug("vacation: cheap-band snap failed (%s); using raw target", exc)
        return target


def compute_recovery_at(return_at_utc: datetime, now: datetime | None = None) -> datetime:
    """``return_at - VACATION_RECOVERY_HOURS``, snapped (bounded) to a cheap-band start."""
    hours = max(0, int(config.VACATION_RECOVERY_HOURS))
    return _snap_to_cheap_band_start(return_at_utc - timedelta(hours=hours), now)


def recovery_cutoff() -> datetime | None:
    """Stored recovery fire time (UTC) or None - the LP load scale stops applying there."""
    try:
        return _parse_iso_utc(db.get_kv(KV_END_AT))
    except Exception:  # noqa: BLE001
        return None


def holiday_dates(recovery_at_utc: datetime, now_utc: datetime) -> tuple[str, str, bool]:
    """``(startDate, endDate, clamped)`` for the Onecta window (both ``YYYY-MM-DD``, local).

    ``endDate`` is INCLUSIVE (the firmware resumes at 00:00 the next day), so the
    fallback is the recovery job's local day minus one: the firmware resumes at most
    24 h BEFORE the HEM job, never after it. If that is before today, both dates are
    today (``clamped=True``; the caller warns).
    """
    tz = _tz()
    start = now_utc.astimezone(tz).date()
    job_day = recovery_at_utc.astimezone(tz).date()
    end = job_day - timedelta(days=1)
    clamped = end < start
    if clamped:
        end = start
    return start.isoformat(), end.isoformat(), clamped


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    raw = db.get_kv(KV_STATE)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict[str, Any]) -> None:
    db.set_kv(KV_STATE, json.dumps(state))


def status_block() -> dict[str, Any]:
    """Operator-facing snapshot (``/cockpit/now`` + ``space_heating_gate_state``)."""
    tz = _tz()
    state = load_state()
    ret = parse_return_at(config.VACATION_RETURN_AT) or _parse_iso_utc(state.get("return_at"))
    rec = _parse_iso_utc(db.get_kv(KV_END_AT))
    return {
        "active": current_preset() == "vacation",
        "return_at": ret.astimezone(tz).isoformat() if ret else None,
        "recovery_at": rec.astimezone(tz).isoformat() if rec else None,
        "daikin_holiday_until_sent": state.get("holiday_until_sent"),
        "holiday_enabled": bool(config.VACATION_DAIKIN_HOLIDAY_ENABLED),
    }


def _log(action: str, params: dict[str, Any], result: str = "ok", trigger: str = "vacation") -> None:
    try:
        db.log_action(device="system", action=action, params=params, result=result, trigger=trigger)
    except Exception as exc:  # noqa: BLE001 - telemetry never blocks
        logger.debug("vacation: action_log failed: %s", exc)


def _notify(msg: str) -> None:
    try:
        from . import notifier

        notifier.notify_action_confirmation(msg)
    except Exception as exc:  # noqa: BLE001
        logger.debug("vacation: notify failed: %s", exc)


def _notify_risk(msg: str) -> None:
    try:
        from . import notifier

        notifier.notify_risk(msg)
    except Exception as exc:  # noqa: BLE001
        logger.debug("vacation: notify_risk failed: %s", exc)


def _fmt_local(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    return dt.astimezone(_tz()).strftime("%a %d/%m %H:%M")


# ---------------------------------------------------------------------------
# Recovery job (APScheduler DateTrigger is in-memory -> re-armed from kv_state)
# ---------------------------------------------------------------------------

def arm_recovery_job(recovery_at: datetime) -> str:
    """(Re)schedule the one-shot ``vacation_end`` job. A past time fires ~30 s from now."""
    try:
        from .scheduler import runner

        sched = runner._background_scheduler
        if sched is None:
            return "inactive"
        from apscheduler.triggers.date import DateTrigger

        run_at = max(recovery_at, datetime.now(UTC) + timedelta(seconds=30))
        sched.add_job(
            run_recovery,
            DateTrigger(run_date=run_at),
            id=JOB_ID,
            replace_existing=True,
            kwargs={"trigger": "scheduled"},
            misfire_grace_time=3600,
        )
        logger.info("vacation: recovery job armed for %s", _iso(run_at))
        return "scheduled"
    except Exception as exc:  # noqa: BLE001
        logger.warning("vacation: arming recovery job failed: %s", exc)
        return f"error: {exc}"


def _disarm_recovery_job() -> None:
    try:
        from .scheduler import runner

        if runner._background_scheduler is not None:
            runner._background_scheduler.remove_job(JOB_ID)
    except Exception:  # noqa: BLE001 - JobLookupError when absent
        pass


def rearm_from_kv() -> str | None:
    """Boot hook (scheduler start): re-arm ``vacation_end`` from ``kv_state``."""
    try:
        recovery_at = _parse_iso_utc(db.get_kv(KV_END_AT))
        if recovery_at is None:
            return None
        # The kill switch gates NEW activations and the holiday-ON POST only: a trip
        # already stored must still end (preset reset at minimum).
        return arm_recovery_job(recovery_at)
    except Exception as exc:  # noqa: BLE001
        logger.warning("vacation: re-arm failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

def begin_activation(*, actor: str = "api", now: datetime | None = None) -> dict[str, Any] | None:
    """Synchronous half of ``vacation_start``: validate, compute, PERSIST, arm.

    Returns the plan dict, or None when the kill switch is off (no kv, no job).
    """
    now = now or datetime.now(UTC)
    if not config.VACATION_DAIKIN_HOLIDAY_ENABLED:
        _log("vacation_start", {"reason": "kill_switch_off", "actor": actor}, result="skipped")
        return None
    return_at = require_return_at(now=now)
    recovery_at = compute_recovery_at(return_at, now)
    immediate = recovery_at <= now + _MIN_LEAD
    if immediate:
        recovery_at = now + _MIN_LEAD
    start_date, end_date, clamped = holiday_dates(recovery_at, now)
    if clamped:
        logger.warning(
            "vacation: recovery %s falls on the activation day - Onecta window clamped to %s",
            _iso(recovery_at), start_date,
        )
    plan: dict[str, Any] = {
        "actor": actor,
        "activated_at": _iso(now),
        "return_at": _iso(return_at),
        "recovery_at": _iso(recovery_at),
        "start_date": start_date,
        "end_date": end_date,
        "clamped": clamped,
        "recovery_immediate": immediate,
        "holiday_post": None,
        "holiday_until_sent": None,
    }
    # Held until finish_activation ends (S1); a recovery arriving meanwhile waits.
    if not _activation_lock.acquire(timeout=_ACTIVATION_WAIT_S):
        logger.warning("vacation: previous activation still in flight - continuing")
        plan["lock"] = False
    else:
        plan["lock"] = True
    try:
        # ORDER: kv FIRST (a failed POST must still leave a scheduled recovery) ...
        db.set_kv(KV_END_AT, _iso(recovery_at))
        _save_state({k: v for k, v in plan.items() if k != "lock"})
        plan["job"] = arm_recovery_job(recovery_at)
    except Exception:
        _release_activation(plan)
        raise
    return plan


def _release_activation(plan: dict[str, Any]) -> None:
    if plan.pop("lock", False):
        try:
            _activation_lock.release()
        except RuntimeError:
            pass


def _run_mpc(reason: str) -> str:
    """``bulletproof_mpc_job`` RETURNS False (no raise) when the dispatch lock is busy,
    the scheduler is paused or the backend is not lp. Retry (bounded) and report it."""
    from .scheduler import runner

    last = "not run"
    for attempt in range(1, _MPC_ATTEMPTS + 1):
        try:
            ok = runner.bulletproof_mpc_job(
                force_write_devices=True, bypass_cooldown=True, trigger_reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            ok = False
            last = f"raised: {exc}"
        else:
            if ok:
                return "ok" if attempt == 1 else f"ok (attempt {attempt})"
            last = "returned False (busy / paused / non-lp)"
        logger.warning("vacation: MPC %s attempt %d/%d: %s", reason, attempt, _MPC_ATTEMPTS, last)
        if attempt < _MPC_ATTEMPTS:
            time.sleep(_MPC_RETRY_WAIT_S)
    return f"failed: {last} after {_MPC_ATTEMPTS} attempts"


def finish_activation(plan: dict[str, Any]) -> dict[str, Any]:
    """Second half: POST the Onecta window, replan, one Telegram. Never raises."""
    try:
        return _finish_activation(plan)
    except Exception as exc:  # noqa: BLE001
        logger.exception("vacation_start failed")
        _notify_risk(f"Ativação das férias falhou: {exc}")
        return {"status": "error", "error": str(exc)}
    finally:
        _release_activation(plan)


def _finish_activation(plan: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    if config.OPENCLAW_READ_ONLY:
        results["holiday_post"] = "skipped_read_only"
    elif plan.get("recovery_immediate"):
        # Recovery fires within a minute: a holiday-ON now would only race the OFF.
        results["holiday_post"] = "skipped_immediate"
    else:
        try:
            daikin_service.set_holiday_mode(
                True, plan["start_date"], plan["end_date"], actor="vacation",
            )
            results["holiday_post"] = "ok"
            plan["holiday_until_sent"] = plan["end_date"]
        except Exception as exc:  # noqa: BLE001
            results["holiday_post"] = f"failed: {exc}"
            logger.warning("vacation: Onecta holiday POST failed: %s", exc)
            _notify_risk(
                "Holiday não ativou na Daikin; aquecimento continua. "
                f"A recuperação segue agendada para {_fmt_local(_parse_iso_utc(plan['recovery_at']))}. ({exc})"
            )
    plan["holiday_post"] = results["holiday_post"]
    if plan.get("job") != "scheduled":
        # Without a job nothing ends the trip: say so loudly.
        results["job"] = plan.get("job")
        _notify_risk(
            f"Recuperação das férias NÃO foi agendada ({plan.get('job')}); "
            "volte para 'normal' manualmente ou reinicie o serviço."
        )
    try:
        persist = {k: v for k, v in plan.items() if k != "lock"}
        _save_state(persist)
    except Exception:  # noqa: BLE001
        pass
    _log("vacation_start", {**{k: plan.get(k) for k in (
        "return_at", "recovery_at", "start_date", "end_date", "clamped", "job",
        "recovery_immediate", "actor")}, **results},
         result="ok" if (results["holiday_post"] in ("ok", "skipped_read_only", "skipped_immediate")
                         and plan.get("job") == "scheduled") else "partial")
    results["replan"] = _run_mpc("vacation_start")
    until = plan.get("holiday_until_sent")
    _notify(
        f"Fora até {_fmt_local(_parse_iso_utc(plan['return_at']))}; "
        + (f"Daikin em holiday até {until}" if until else "Daikin sem holiday (não ativou / desligado)")
        + f"; recuperação agendada {_fmt_local(_parse_iso_utc(plan['recovery_at']))}."
    )
    return results


def activate(*, actor: str = "api", now: datetime | None = None) -> dict[str, Any] | None:
    """Both halves, synchronously (used by tests and the CLI)."""
    plan = begin_activation(actor=actor, now=now)
    if plan is None:
        return None
    plan["results"] = finish_activation(plan)
    return plan


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def _read_devices(*, force: bool) -> list[Any]:
    """Cached/refreshed device list; [] on any failure."""
    try:
        if force:
            res = daikin_service.force_refresh_devices("vacation_end", min_interval_seconds=0)
        else:
            res = daikin_service.get_cached_devices(allow_refresh=True, actor="vacation_end")
        return list(res.devices or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("vacation_end: device read failed: %s", exc)
        return []


def _predicted_indoor_at(return_at: datetime | None) -> float | None:
    """Indoor temperature the latest plan predicts at ``return_at`` (None when unknown)."""
    if return_at is None:
        return None
    try:
        day = datetime.now(UTC).astimezone(_tz()).date().isoformat()
        row = db.get_latest_lp_inputs_for_plan_date(day)
        if not row:
            return None
        best: tuple[float, float] | None = None
        for s in db.get_lp_solution_slots(int(row["run_id"])):
            if s.get("indoor_temp_c") is None:
                continue
            st = _parse_iso_utc(s.get("slot_time_utc"))
            if st is None:
                continue
            d = abs((st - return_at).total_seconds())
            if d <= 1800 and (best is None or d < best[0]):
                best = (d, float(s["indoor_temp_c"]))
        return best[1] if best else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("vacation_end: predicted indoor failed: %s", exc)
        return None


def run_recovery(trigger: str = "scheduled") -> dict[str, Any]:
    """The ``vacation_end`` sequence. Idempotent; one run at a time; never raises.

    ``trigger``: ``scheduled`` (DateTrigger), ``cancel`` (left vacation early) or
    ``manual``.
    """
    if not _recovery_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        # A holiday-ON POST may still be in flight: wait for it (S1).
        if _activation_lock.acquire(timeout=_ACTIVATION_WAIT_S):
            _activation_lock.release()
        else:
            logger.warning("vacation_end: activation still in flight after %.0fs - going ahead", _ACTIVATION_WAIT_S)
        return _run_recovery_locked(trigger)
    except Exception as exc:  # noqa: BLE001
        logger.exception("vacation_end failed")
        return {"status": "error", "error": str(exc)}
    finally:
        _recovery_lock.release()


def _has_failure(steps: dict[str, Any]) -> bool:
    return any("failed" in str(v) for v in steps.values())


def _run_recovery_locked(trigger: str) -> dict[str, Any]:
    end_raw = db.get_kv(KV_END_AT)
    state = load_state()
    if not end_raw and not state:
        return {"status": "noop"}  # already recovered
    steps: dict[str, Any] = {}
    read_only = bool(config.OPENCLAW_READ_ONLY)

    # 1. preset normal + clear the return time FIRST: a concurrent #872 heartbeat
    #    `holiday_end` solve (it fires as soon as the holiday flag drops) must already
    #    see `normal`, not plan the house as still away.
    try:
        from . import runtime_settings as rts

        eff = current_preset()
        target = eff if eff in ("normal", "guests") else "normal"
        if rts.get_setting("OPTIMIZATION_PRESET") != target or config.has_override("OPTIMIZATION_PRESET"):
            config.clear_override("OPTIMIZATION_PRESET")
            rts.set_setting("OPTIMIZATION_PRESET", target, actor="vacation_end")
        config.clear_override("VACATION_RETURN_AT")
        rts.set_setting("VACATION_RETURN_AT", "", actor="vacation_end")
        steps["preset"] = target
    except Exception as exc:  # noqa: BLE001
        steps["preset"] = f"failed: {exc}"
        logger.warning("vacation_end: preset reset failed: %s", exc)

    # 2. holiday off (errors logged, never block). Disabling is always allowed, also
    #    with the kill switch off (it only gates NEW holiday-ON writes).
    if read_only:
        steps["holiday_off"] = "skipped_read_only"
    else:
        try:
            daikin_service.set_holiday_mode(False, actor="vacation_end")
            steps["holiday_off"] = "ok"
        except Exception as exc:  # noqa: BLE001
            steps["holiday_off"] = f"failed: {exc}"
            logger.warning("vacation_end: holiday off failed: %s", exc)

    # 3. read state; Onecta lags after holiday-off, so poll (bounded) until the
    #    holiday flag clears - else the #872 gate rejects the power-on and the MPC
    #    would plan `daikin_state=off`.
    dev = None
    attempts: list[str] = []
    for n in range(1, _HOLIDAY_POLL_ATTEMPTS + 1):
        devices = _read_devices(force=True)
        dev = devices[0] if devices else None
        flags = (getattr(dev, "holiday_active", None), getattr(dev, "tank_holiday_active", None))
        attempts.append(f"{n}:{'no_devices' if dev is None else flags}")
        if dev is None or (flags[0] is not True and flags[1] is not True):
            break
        if n < _HOLIDAY_POLL_ATTEMPTS and not read_only:
            time.sleep(_HOLIDAY_POLL_WAIT_S)
        else:
            break
    steps["read"] = "ok" if dev is not None else "no_devices"
    steps["holiday_poll"] = attempts
    if dev is not None and (
        getattr(dev, "holiday_active", None) is True or getattr(dev, "tank_holiday_active", None) is True
    ) and not read_only:
        steps["holiday_poll_warning"] = "failed: holiday flag still active after polling"
        logger.warning("vacation_end: Daikin still reports holiday after %d reads", len(attempts))

    # 4. power climate / tank on when the cache says off
    powered: dict[str, str] = {}
    if dev is not None and not read_only:
        for label, is_on, fn in (
            ("climate", getattr(dev, "is_on", None), daikin_service.set_power),
            ("tank", getattr(dev, "tank_on", None), daikin_service.set_tank_power),
        ):
            if is_on is False:
                try:
                    fn(True, actor="vacation_end")
                    powered[label] = "ok"
                except Exception as exc:  # noqa: BLE001
                    powered[label] = f"failed: {exc}"
                    logger.warning("vacation_end: power-on (%s) failed: %s", label, exc)
    steps["power_on"] = powered or "not_needed"

    # 5. read back (only when something was written)
    if powered:
        back = _read_devices(force=True)
        if back:
            dev = back[0]
            steps["read_back"] = {
                "climate_on": getattr(dev, "is_on", None),
                "tank_on": getattr(dev, "tank_on", None),
            }

    # 6. replan with device writes (battery + Daikin rows for the recovery).
    #    Retried: the job RETURNS False when the dispatch lock is busy.
    steps["mpc"] = _run_mpc("vacation_end")

    # 7. clear the stored recovery (last: a crash above re-runs on boot, idempotently)
    db.set_kv(KV_END_AT, "")
    db.set_kv(KV_STATE, "")
    _disarm_recovery_job()

    # 8. one Telegram
    return_at = _parse_iso_utc(state.get("return_at"))
    indoor = _predicted_indoor_at(return_at)
    tank = getattr(dev, "tank_temperature", None) if dev is not None else None
    head = (
        "Férias canceladas - voltando ao normal." if trigger == "cancel"
        else "Recuperação pós-férias iniciada."
    )
    _notify(
        f"{head} Previsão interna na volta ({_fmt_local(return_at)}): "
        + (f"{indoor:.1f} C" if indoor is not None else "n/d")
        + "; tanque: "
        + (f"{float(tank):.0f} C" if tank is not None else "n/d")
        + ("." if not _has_failure(steps) else " (houve falhas, ver action_log).")
    )
    _log("vacation_end", {"trigger": trigger, "steps": steps},
         result="ok" if not _has_failure(steps) else "partial",
         trigger=trigger)
    return {"status": "done", "trigger": trigger, "steps": steps}


# ---------------------------------------------------------------------------
# Entry points used by the API / MCP layers
# ---------------------------------------------------------------------------

def _spawn(fn, *args, background: bool | None, name: str, **kwargs) -> None:
    if background is None:
        background = BACKGROUND
    if background:
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True, name=name).start()
    else:
        fn(*args, **kwargs)


def after_settings_applied(
    prev_preset: str,
    changes: dict[str, Any],
    *,
    actor: str = "api",
    background: bool | None = None,
) -> dict[str, Any] | None:
    """Side effects after a preset / return-time change was persisted.

    * now vacation (entered, or the return time moved) -> activation;
    * left vacation while a recovery is stored -> cancel early (same recovery).
    Returns a small status dict (or None when nothing applies).
    """
    prev = normalize_preset(prev_preset)
    new = current_preset()
    if new == "vacation" and (prev != "vacation" or "VACATION_RETURN_AT" in changes):
        try:
            plan = begin_activation(actor=actor)
        except VacationReturnAtError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. a kv write failure
            logger.exception("vacation: activation failed")
            _notify_risk(
                f"Ativação das férias falhou ({exc}): preset=vacation sem recuperação agendada; "
                "volte para 'normal' ou tente de novo."
            )
            return {"status": "error", "error": str(exc)}
        if plan is None:
            return {"status": "disabled"}
        _spawn(finish_activation, plan, background=background, name="vacation-start")
        return {"status": "activating", "return_at": plan["return_at"],
                "recovery_at": plan["recovery_at"], "holiday_end_date": plan["end_date"]}
    if prev == "vacation" and new != "vacation" and db.get_kv(KV_END_AT):
        _spawn(run_recovery, "cancel", background=background, name="vacation-cancel")
        return {"status": "recovering"}
    return None


def set_preset(
    preset: str,
    return_at: str | None = None,
    *,
    actor: str = "api",
    background: bool | None = None,
) -> dict[str, Any]:
    """Persisted preset change for the MCP tool / legacy endpoint (settings-API parity).

    Raises :class:`VacationReturnAtError` for vacation without a future return time.
    """
    from . import runtime_settings as rts

    p = normalize_preset(preset)
    prev = current_preset()
    changes: dict[str, Any] = {"OPTIMIZATION_PRESET": p}
    if return_at is not None:
        changes["VACATION_RETURN_AT"] = return_at
    check_preset_changes(changes)
    if return_at is not None:
        config.clear_override("VACATION_RETURN_AT")
        rts.set_setting("VACATION_RETURN_AT", return_at, actor=actor)
    config.clear_override("OPTIMIZATION_PRESET")
    rts.set_setting("OPTIMIZATION_PRESET", p, actor=actor)
    info = after_settings_applied(prev, changes, actor=actor, background=background)
    return {"preset": p, "vacation": info}


def persist_setting(key: str, value: Any, *, actor: str = "api") -> Any:
    """``rts.set_setting`` that first drops an in-memory ``config`` override of the
    preset / return-time keys (an override shadows the DB for the process lifetime)."""
    from . import runtime_settings as rts

    if key in ("OPTIMIZATION_PRESET", "VACATION_RETURN_AT"):
        config.clear_override(key)
    return rts.set_setting(key, value, actor=actor)
