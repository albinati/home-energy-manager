"""Fail-safe defaults, boot recovery, and action validation (Bulletproof engine)."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import config
from .daikin.client import DaikinClient, DaikinError
from .daikin_bulletproof import (
    apply_comfort_restore,
    apply_scheduled_daikin_params,
    detect_user_override,
)
from .foxess.client import FoxESSClient, FoxESSError, scheduler_groups_from_stored_json
from .notifier import notify_risk, notify_user_override

logger = logging.getLogger(__name__)

# Phase 4 review C6 — process-local map of action_schedule row id → earliest
# wall-clock time we successfully ran apply_scheduled_daikin_params for that row
# in THIS process. Used as the grace-period anchor for override detection so a
# systemd restart mid-plan cannot cause a false override on the first reconcile
# tick (row.start_time would be ancient, but we haven't actually applied yet).
_FIRST_APPLIED_SESSION: dict[int, datetime] = {}

# V12 — per-episode dedup for the user-override notification. Without this set
# the heartbeat fires ``notify_user_override`` every 2-min reconcile while the
# user keeps the manual change in place, drowning Telegram. Cleared when:
#  * the action's override is reconciled (row no longer detected as override), or
#  * the action ends (row drops out of the reconcile window naturally).
_USER_OVERRIDE_NOTIFIED: set[int] = set()


def _parse_utc(s: str) -> datetime:
    x = str(s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _schedule_signature(groups: list[Any]) -> str:
    """Comparable fingerprint for Fox V3 groups (hardware vs SQLite)."""
    payload = []
    for g in groups:
        if hasattr(g, "start_hour"):
            payload.append(
                (
                    g.start_hour,
                    g.start_minute,
                    g.end_hour,
                    g.end_minute,
                    g.work_mode,
                    g.fd_soc,
                    g.fd_pwr,
                )
            )
        elif isinstance(g, dict):
            ep = g.get("extraParam") or g.get("extra_param") or {}
            payload.append(
                (
                    g.get("startHour"),
                    g.get("startMinute"),
                    g.get("endHour"),
                    g.get("endMinute"),
                    g.get("workMode"),
                    ep.get("fdSoc"),
                    ep.get("fdPwr"),
                )
            )
    return json.dumps(payload, sort_keys=True, default=str)


def apply_safe_defaults(
    fox: FoxESSClient | None,
    daikin: DaikinClient | None,
    *,
    trigger: str = "recovery",
) -> None:
    """Force safe house state after arbitrage windows or on fault."""
    if fox and not config.OPENCLAW_READ_ONLY:
        # Isolate each set_* call so action_log + logs identify exactly which
        # endpoint Fox rejects. Historically the bundled try/except masked this:
        # when the API returned 40257, we knew "something" failed but had no
        # signal to drive the fix. Each call is independent — failure of one
        # does not block the others, and the success-row params reflect what
        # actually applied.
        min_soc = max(0, min(100, int(config.MIN_SOC_RESERVE_PERCENT)))
        applied: dict[str, Any] = {}
        failures: dict[str, str] = {}
        steps: list[tuple[str, Any]] = [
            ("scheduler_flag", lambda: fox.set_scheduler_flag(False) if fox.api_key else None),
            ("work_mode", lambda: fox.set_work_mode("Self Use")),
            ("min_soc_on_grid", lambda: fox.set_min_soc(min_soc)),
        ]
        step_value = {"scheduler_flag": False, "work_mode": "Self Use", "min_soc_on_grid": min_soc}
        for name, call in steps:
            try:
                call()
                applied[name] = step_value[name]
            except (FoxESSError, ValueError) as e:
                failures[name] = str(e)
                logger.warning("Fox safe-default step %s failed: %s", name, e)

        if failures:
            db.log_action(
                device="foxess",
                action="apply_safe_defaults",
                params={"applied": applied, "failed": list(failures)},
                result="partial" if applied else "failure",
                trigger=trigger,
                error_msg="; ".join(f"{k}: {v}" for k, v in failures.items()),
            )
        else:
            db.log_action(
                device="foxess",
                action="apply_safe_defaults",
                params=applied,
                result="success",
                trigger=trigger,
            )
    elif fox:
        db.log_action(
            device="foxess",
            action="apply_safe_defaults",
            params={"shadow": True},
            result="skipped",
            trigger=trigger,
            error_msg="read_only",
        )

    if not daikin:
        return
    try:
        devices = daikin.get_devices()
        if not devices:
            return
        dev = devices[0]
        if config.OPENCLAW_READ_ONLY:
            db.log_action(
                device="daikin",
                action="apply_safe_defaults",
                params={"shadow": True},
                result="skipped",
                trigger=trigger,
            )
            return
        apply_scheduled_daikin_params(
            dev,
            daikin,
            {
                "lwt_offset": 0.0,
                "tank_powerful": False,
                "tank_temp": float(config.DHW_TEMP_NORMAL_C),
                "tank_power": True,
                "climate_on": True,
            },
            trigger=trigger,
            skip_if_matches=False,
        )
    except (DaikinError, ValueError, IndexError) as e:
        db.log_action(
            device="daikin",
            action="apply_safe_defaults",
            params={},
            result="failure",
            trigger=trigger,
            error_msg=str(e),
        )
        logger.warning("Daikin safe defaults failed: %s", e)


def should_act(
    action: dict[str, Any],
    *,
    current_soc: float | None,
    room_temp_c: float | None,
    now_local: datetime | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    atype = action.get("action_type", "")
    params = action.get("params") or {}
    if atype in ("force_charge_max", "force_charge") and current_soc is not None:
        target = params.get("fd_soc") or params.get("target_soc")
        if target is not None and current_soc >= float(target) - 0.5:
            return False, "SOC already at target"
    if atype == "shutdown" and room_temp_c is not None:
        if room_temp_c < 5.0:
            return False, "frost risk — room very cold"
    return True, "ok"


def _reconcile_daikin_actions(
    actions: list[dict[str, Any]],
    client: DaikinClient,
    dev: Any,
    now_utc: datetime,
    *,
    trigger: str,
    outdoor_c: float | None = None,
) -> None:
    """Transition statuses and apply params for today's Daikin rows."""
    for act in sorted(actions, key=lambda a: (a["start_time"], int(a["id"]))):
        try:
            start = _parse_utc(act["start_time"])
            end = _parse_utc(act["end_time"])
        except ValueError:
            continue
        aid = int(act["id"])
        status = act["status"]

        # Phase 4.3 — user-override rows skip re-apply but still transition to
        # terminal status when their window ends; otherwise they'd stay 'active'
        # forever and pollute every "latest active row" query.
        if act.get("overridden_by_user_at"):
            if now_utc >= end and status in ("pending", "active"):
                db.mark_action(aid, "completed")
            continue
        atype = act.get("action_type", "")
        params = act.get("params") or {}

        if now_utc < start:
            continue

        if now_utc >= end:
            if status == "pending":
                # Pending past end-time = silently skipped (heartbeat missed
                # the window). Visibly warn so this class of bug surfaces next
                # time instead of being silent. The 2026-04-30 active-mode
                # rollout hit this with a 1-min restore window vs 2-min
                # heartbeat — see lp_dispatch.LP_RESTORE_WINDOW_MINUTES.
                logger.warning(
                    "action_schedule[%s] %s window missed (start=%s end=%s now=%s, "
                    "marking completed without firing) — likely too-narrow window "
                    "or heartbeat lag",
                    aid, atype, act.get("start_time"), act.get("end_time"),
                    now_utc.isoformat(),
                )
            if status in ("pending", "active"):
                db.mark_action(aid, "completed")
            continue

        if status == "pending" and start <= now_utc < end:
            db.mark_action(aid, "active")

        if status in ("pending", "active") and start <= now_utc < end:
            apply_params = dict(params)
            if (
                atype == "shutdown"
                and outdoor_c is not None
                and outdoor_c < float(config.WEATHER_FROST_THRESHOLD_C)
            ):
                lo = float(apply_params.get("lwt_offset", 0.0))
                if lo < -2.0:
                    apply_params["lwt_offset"] = -2.0

            # Phase 4.3 — check for user override before re-applying.
            # Phase 4 review C6: only run override detection after we've had at
            # least one successful apply of this row IN THIS PROCESS, using that
            # first-apply timestamp as the grace anchor. Before that first apply,
            # any divergence is "boot-state, not user intent" and must be
            # reconciled by writing our desired value, not by flagging override.
            first_applied = _FIRST_APPLIED_SESSION.get(aid)
            if first_applied is not None:
                is_override, reason = detect_user_override(
                    dev, apply_params, row_started_utc=first_applied, now_utc=now_utc,
                )
            else:
                is_override, reason = False, None
            if is_override:
                db.mark_action_user_overridden(aid)
                db.log_action(
                    device="daikin",
                    action="user_override_detected",
                    params={"row_id": aid, "reason": reason},
                    result="skipped",
                    trigger=trigger,
                )
                # Per-episode dedup (V12) — fire once when the override is first
                # detected; stay silent for the rest of the override window.
                if aid not in _USER_OVERRIDE_NOTIFIED:
                    _USER_OVERRIDE_NOTIFIED.add(aid)
                    try:
                        notify_user_override(reason or "unknown divergence")
                    except Exception as _exc:
                        logger.debug("notify_user_override failed (non-fatal): %s", _exc)
                continue
            else:
                # Override cleared — drop the dedup token so a new override on
                # this same row would fire a fresh notification.
                _USER_OVERRIDE_NOTIFIED.discard(aid)

            try:
                apply_scheduled_daikin_params(dev, client, apply_params, trigger=trigger)
                # Mark the row as applied-in-session on the first successful call
                # (skip_if_matches=True path also counts — match confirms alignment).
                _FIRST_APPLIED_SESSION.setdefault(aid, now_utc)
            except (DaikinError, ValueError) as e:
                logger.warning("Boot Daikin apply %s: %s", aid, e)
                db.mark_action(aid, "failed", error_msg=str(e))


def reconcile_daikin_schedule_for_date(
    plan_date: str,
    client: DaikinClient,
    dev: Any,
    now_utc: datetime,
    *,
    trigger: str,
    outdoor_c: float | None = None,
) -> None:
    """Full-day Daikin reconciliation (status transitions + live apply).

    Blocked when a plan_consent row for *plan_date* is in ``pending_approval``
    status and the expiry has not yet elapsed.  On expiry, the plan is
    auto-approved and execution proceeds (or self-use if PLAN_AUTO_APPROVE=false
    and no approval within the window).
    """
    import time as _time
    consent = db.get_plan_consent(plan_date)
    if consent and consent["status"] == "pending_approval":
        if _time.time() > float(consent["expires_at"]):
            # Expired without user response → auto-approve so the system doesn't stall
            db.approve_plan(consent["plan_id"])
            elapsed = int(_time.time() - float(consent["proposed_at"]))
            logger.info(
                "Plan %s auto-approved on expiry (%ds elapsed)",
                consent["plan_id"],
                elapsed,
            )
            # Notify user that expiry auto-approval occurred
            try:
                from .notifier import notify_action_confirmation
                notify_action_confirmation(
                    f"Plan {consent['plan_id']} auto-approved after {elapsed//60}m timeout "
                    f"— Daikin is now executing the scheduled actions."
                )
            except Exception as _exc:
                logger.debug("notify expiry auto-approve failed (non-fatal): %s", _exc)
        else:
            logger.debug(
                "Plan %s pending user approval — Daikin execution held (expires in %ds)",
                consent["plan_id"],
                int(float(consent["expires_at"]) - _time.time()),
            )
            return  # hard gate: no Daikin actions until approved

    actions = db.get_actions_for_plan_date(plan_date, device="daikin")
    _reconcile_daikin_actions(
        actions, client, dev, now_utc, trigger=trigger, outdoor_c=outdoor_c
    )


def heartbeat_repair_fox_scheduler(fox: FoxESSClient) -> None:
    """Re-enable Fox time-scheduler and re-upload V3 if SQLite plan differs.

    V12: gates the "Fox scheduler flag disabled" warning behind a per-day
    ``acknowledged_warnings`` row so a stuck-False flag doesn't ping every
    2-min heartbeat. The warning auto-clears when the flag recovers, so a
    fresh failure on the same day re-pings exactly once.
    """
    if not fox.api_key:
        return
    try:
        from datetime import date as _date
        flag_on = fox.get_scheduler_flag()
        hw = fox.get_scheduler_v3()
        plan_date = _date.today().isoformat()
        warning_key = f"fox_scheduler_disabled_{plan_date}"
        if not flag_on or not hw.enabled:
            if not db.is_warning_acknowledged(warning_key):
                notify_risk("Fox ESS scheduler flag disabled — check inverter app.")
                db.acknowledge_warning(warning_key)
        else:
            # Flag is healthy — drop any prior ack so a fresh failure today
            # re-pings exactly once.
            db.clear_warning(warning_key)
        if not config.OPENCLAW_READ_ONLY:
            if not flag_on or not hw.enabled:
                fox.set_scheduler_flag(True)
            latest = db.get_latest_fox_schedule_state()
            if latest and latest.get("groups"):
                stored_groups = scheduler_groups_from_stored_json(latest["groups"])
                if stored_groups and _schedule_signature(hw.groups) != _schedule_signature(
                    stored_groups
                ):
                    logger.info("Fox V3 differs from SQLite plan — re-uploading (heartbeat)")
                    fox.set_scheduler_v3(stored_groups, is_default=False)
                    fox.warn_if_scheduler_v3_mismatch(stored_groups)
                    fox.set_scheduler_flag(True)
                    db.log_action(
                        device="foxess",
                        action="heartbeat_reupload_scheduler_v3",
                        params={"groups": len(stored_groups)},
                        result="success",
                        trigger="heartbeat",
                    )
    except FoxESSError as e:
        logger.warning("Heartbeat Fox Scheduler V3 repair failed: %s", e)


def _recover_missed_restores(
    plan_date: str,
    client: DaikinClient,
    dev: Any,
    now_utc: datetime,
) -> None:
    """If a restore window ended while we were down, force comfort baseline."""
    actions = db.get_actions_for_plan_date(plan_date, device="daikin")
    for act in actions:
        if act.get("action_type") != "restore" or act.get("status") != "pending":
            continue
        try:
            end = _parse_utc(act["end_time"])
        except ValueError:
            continue
        if now_utc > end + timedelta(minutes=2):
            try:
                apply_comfort_restore(dev, client, trigger="recovery_missed_restore")
                db.mark_action(
                    int(act["id"]),
                    "completed",
                    error_msg="applied comfort baseline after missed restore window",
                )
                db.log_action(
                    device="daikin",
                    action="missed_restore_recovery",
                    params={"action_id": act["id"]},
                    result="success",
                    trigger="recovery",
                )
            except (DaikinError, ValueError) as e:
                logger.warning("Missed restore recovery failed: %s", e)


def recover_on_boot(
    fox: FoxESSClient | None,
    daikin: DaikinClient | None,
) -> None:
    """Reconcile SQLite, Daikin windows, Fox V3 vs stored plan; survival / empty fallbacks."""
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now_utc = datetime.now(UTC)
    plan_date = datetime.now(tz).date().isoformat()

    dev = None
    if daikin and config.USE_BULLETPROOF_ENGINE:
        try:
            devs = daikin.get_devices()
            dev = devs[0] if devs else None
        except Exception as e:
            logger.debug("recover_on_boot: no Daikin device: %s", e)

    if daikin and dev:
        outdoor = None
        try:
            outdoor = dev.temperature.outdoor_temperature
        except Exception:
            pass
        daikin_actions = db.get_actions_for_plan_date(plan_date, device="daikin")
        _reconcile_daikin_actions(
            daikin_actions,
            daikin,
            dev,
            now_utc,
            trigger="boot_recovery",
            outdoor_c=outdoor,
        )
        _recover_missed_restores(plan_date, daikin, dev, now_utc)

    ofs = db.get_octopus_fetch_state()
    if ofs.survival_mode_since:
        try:
            apply_safe_defaults(fox, daikin, trigger="boot_recovery_survival")
        except Exception as e:
            logger.warning("Boot survival safe defaults: %s", e)

    if fox and fox.api_key:
        try:
            hw = fox.get_scheduler_v3()
            if not hw.enabled and not config.OPENCLAW_READ_ONLY:
                fox.set_scheduler_flag(True)
            latest = db.get_latest_fox_schedule_state()
            if latest and latest.get("groups") and not config.OPENCLAW_READ_ONLY:
                stored_groups = scheduler_groups_from_stored_json(latest["groups"])
                if stored_groups and _schedule_signature(hw.groups) != _schedule_signature(stored_groups):
                    logger.info("Fox V3 differs from SQLite plan — re-uploading")
                    fox.set_scheduler_v3(stored_groups, is_default=False)
                    fox.warn_if_scheduler_v3_mismatch(stored_groups)
                    fox.set_scheduler_flag(True)
                    db.log_action(
                        device="foxess",
                        action="recover_reupload_scheduler_v3",
                        params={"groups": len(stored_groups)},
                        result="success",
                        trigger="boot_recovery",
                    )
        except FoxESSError as e:
            logger.warning("Could not verify/repair Fox Scheduler V3: %s", e)

    has_plan = bool(db.get_latest_fox_schedule_state()) or bool(
        db.get_actions_for_plan_date(plan_date)
    )
    if not has_plan and (fox or daikin) and not ofs.survival_mode_since:
        try:
            apply_safe_defaults(fox, daikin, trigger="boot_empty_schedule")
        except Exception as e:
            logger.warning("Empty schedule safe defaults: %s", e)
