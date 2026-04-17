"""Fail-safe defaults, boot recovery, and action validation (Bulletproof engine)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from zoneinfo import ZoneInfo

from .config import config
from . import db
from .daikin.client import DaikinClient, DaikinError
from .daikin_bulletproof import apply_comfort_restore, apply_scheduled_daikin_params
from .foxess.client import FoxESSClient, FoxESSError, scheduler_groups_from_stored_json
from .notifier import notify_risk

logger = logging.getLogger(__name__)


def _parse_utc(s: str) -> datetime:
    x = str(s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
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
    fox: Optional[FoxESSClient],
    daikin: Optional[DaikinClient],
    *,
    trigger: str = "recovery",
) -> None:
    """Force safe house state after arbitrage windows or on fault."""
    if fox and config.OPERATION_MODE == "operational" and not config.OPENCLAW_READ_ONLY:
        try:
            if fox.api_key:
                try:
                    fox.set_scheduler_flag(False)
                except FoxESSError:
                    pass
            fox.set_work_mode("Self Use")
            fox.set_min_soc(10)
            db.log_action(
                device="foxess",
                action="apply_safe_defaults",
                params={"work_mode": "Self Use", "min_soc_on_grid": 10, "scheduler_flag": False},
                result="success",
                trigger=trigger,
            )
        except (FoxESSError, ValueError) as e:
            db.log_action(
                device="foxess",
                action="apply_safe_defaults",
                params={},
                result="failure",
                trigger=trigger,
                error_msg=str(e),
            )
            logger.warning("Fox safe defaults failed: %s", e)
    elif fox:
        db.log_action(
            device="foxess",
            action="apply_safe_defaults",
            params={"shadow": True},
            result="skipped",
            trigger=trigger,
            error_msg="read_only or simulation",
        )

    if not daikin:
        return
    try:
        devices = daikin.get_devices()
        if not devices:
            return
        dev = devices[0]
        if config.OPERATION_MODE != "operational" or config.OPENCLAW_READ_ONLY:
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
    current_soc: Optional[float],
    room_temp_c: Optional[float],
    now_local: Optional[datetime] = None,
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
    if atype == "shutdown" and params.get("tank_power") is False:
        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        nl = now_local or datetime.now(tz)
        if nl.weekday() == config.DHW_LEGIONELLA_DAY:
            if config.DHW_LEGIONELLA_HOUR_START <= nl.hour < config.DHW_LEGIONELLA_HOUR_END:
                if params.get("legionella_override"):
                    return True, "legionella override active"
                return False, "Legionella window — do not turn off DHW"
    return True, "ok"


def _reconcile_daikin_actions(
    actions: list[dict[str, Any]],
    client: DaikinClient,
    dev: Any,
    now_utc: datetime,
    *,
    trigger: str,
    outdoor_c: Optional[float] = None,
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
        atype = act.get("action_type", "")
        params = act.get("params") or {}

        if now_utc < start:
            continue

        if now_utc >= end:
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
            try:
                apply_scheduled_daikin_params(dev, client, apply_params, trigger=trigger)
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
    outdoor_c: Optional[float] = None,
) -> None:
    """Full-day Daikin reconciliation (status transitions + live apply)."""
    actions = db.get_actions_for_plan_date(plan_date, device="daikin")
    _reconcile_daikin_actions(
        actions, client, dev, now_utc, trigger=trigger, outdoor_c=outdoor_c
    )


def heartbeat_repair_fox_scheduler(fox: FoxESSClient) -> None:
    """Re-enable Fox time-scheduler and re-upload V3 if SQLite plan differs (operational)."""
    if not fox.api_key or config.OPERATION_MODE != "operational":
        return
    try:
        flag_on = fox.get_scheduler_flag()
        hw = fox.get_scheduler_v3()
        if not flag_on or not hw.enabled:
            notify_risk("Fox ESS scheduler flag disabled — check inverter app.")
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
    fox: Optional[FoxESSClient],
    daikin: Optional[DaikinClient],
) -> None:
    """Reconcile SQLite, Daikin windows, Fox V3 vs stored plan; survival / empty fallbacks."""
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now_utc = datetime.now(timezone.utc)
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

    if fox and fox.api_key and config.OPERATION_MODE == "operational":
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
