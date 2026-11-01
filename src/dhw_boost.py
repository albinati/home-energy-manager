"""#901 - "Banho extra hoje": a one-tap tank boost the plan RESPECTS.

Why not just ``POST /daikin/tank-temperature``: that writes the device and
nothing else. No ``action_schedule`` row records the gesture, so at 16:00 the
pending ``tank_setback`` row (37 C) fires and silently undoes it. The pre-fire
reconciler's override machinery (#386) only protects a change it can *source*
from an ``overridden_by_user_at`` row.

So the boost is written the way a detected manual change would be, but
deterministically: one ``tank_user_boost`` row (born ``active`` + overridden,
window = ``USER_OVERRIDE_RESPECT_HOURS``) whose params hold the tank target the
device had BEFORE the boost. ``user_gesture_still_in_effect`` therefore reads
"live target != that baseline" as the gesture still standing, and the
reconciler holds every tank row while it does (``prefire_boost_hold``). When the
window ends (or on undo) ``state_machine.end_user_boost`` closes the row and
re-commands the target the schedule asks for right now.

Gates (all must pass; each maps to a distinct, user-readable error code):
``OPENCLAW_READ_ONLY``, ``DAIKIN_CONTROL_MODE=passive``, holiday mode (#872),
vacation preset, the weekly legionella stand-off (firmware owns the tank), the
Daikin daily quota, an unknown/off tank, a boost already running, and a tank or
target that is already at least as hot as the boost.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import config
from .daikin import service as daikin_service
from .daikin.client import DaikinClient, DaikinError
from .daikin_bulletproof import holiday_blocks_params

logger = logging.getLogger(__name__)

ACTION_TYPE = db.USER_BOOST_ACTION_TYPE


class BoostBlocked(Exception):
    """The boost cannot (or should not) be applied right now."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def target_c() -> float:
    return float(min(float(config.DHW_USER_BOOST_TARGET_C), float(config.DHW_TEMP_MAX_C)))


def hold_hours() -> float:
    return float(config.USER_OVERRIDE_RESPECT_HOURS)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cached_device() -> Any | None:
    try:
        res = daikin_service.get_cached_devices(allow_refresh=False, actor="dhw_boost")
        return res.devices[0] if res.devices else None
    except Exception:  # noqa: BLE001 - a cold cache reads as "no device"
        return None


def active_boost(now: datetime | None = None) -> dict[str, Any] | None:
    """The boost row still holding the tank, else None."""
    now = now or datetime.now(UTC)
    for row in db.get_active_user_boost_rows():
        try:
            end = datetime.fromisoformat(str(row["end_time"]).replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        if end > now:
            return row
    return None


def check_gates(dev: Any | None, now: datetime) -> None:
    """Raise :class:`BoostBlocked` with the first reason the boost cannot run."""
    from .api_quota import should_block
    from .state_machine import in_legionella_standoff

    if config.OPENCLAW_READ_ONLY:
        raise BoostBlocked(409, "ReadOnly", "HEM está em modo somente leitura (OPENCLAW_READ_ONLY).")
    if config.DAIKIN_CONTROL_MODE == "passive":
        raise BoostBlocked(
            409, "PassiveModeLocked",
            "HEM está só observando o Daikin (DAIKIN_CONTROL_MODE=passive).",
        )
    if (config.OPTIMIZATION_PRESET or "normal").strip().lower() == "vacation":
        raise BoostBlocked(409, "VacationMode", "Modo férias ativo: o tanque está desligado de propósito.")
    if dev is None:
        raise BoostBlocked(409, "NoDevice", "Sem leitura recente do Daikin.")
    if holiday_blocks_params(dev, {"tank_temp": 0}):
        raise BoostBlocked(409, "HolidayMode", "O Daikin está em modo férias: o tanque está desligado.")
    if in_legionella_standoff(now):
        raise BoostBlocked(409, "LegionellaStandoff", "Ciclo de legionela em andamento: o Daikin controla o tanque.")
    if should_block("daikin"):
        raise BoostBlocked(409, "QuotaExhausted", "Cota diária do Daikin esgotada ou escrita pausada.")
    if getattr(dev, "tank_on", None) is False:
        raise BoostBlocked(422, "TankOff", "O tanque está desligado.")
    if active_boost(now) is not None:
        raise BoostBlocked(409, "AlreadyBoosted", "Já há um banho extra em andamento.")
    tgt = target_c()
    tank_c = getattr(dev, "tank_temperature", None)
    if tank_c is not None and float(tank_c) >= tgt:
        raise BoostBlocked(422, "TankAlreadyHot", f"O tanque já está a {float(tank_c):.0f} °C.")
    cur = getattr(dev, "tank_target", None)
    if cur is not None and float(cur) >= tgt - float(config.DAIKIN_OVERRIDE_TOLERANCE_TANK_C):
        raise BoostBlocked(422, "TargetAlreadyHigh", f"A meta do tanque já é {float(cur):.0f} °C.")


def status(now: datetime | None = None) -> dict[str, Any]:
    """Viewer-safe snapshot for the Home card: boost state, whether it could be
    applied right now (and why not), and the next legionella window."""
    now = now or datetime.now(UTC)
    dev = _cached_device()
    row = active_boost(now)
    blocked: dict[str, str] | None = None
    if row is None:
        try:
            check_gates(dev, now)
        except BoostBlocked as exc:
            blocked = exc.detail()
    params = (row or {}).get("params") or {}
    return {
        "active": row is not None,
        "row_id": int(row["id"]) if row else None,
        "started_utc": row.get("start_time") if row else None,
        "until_utc": row.get("end_time") if row else None,
        "boost_target_c": float(params["boost_target_c"]) if row and "boost_target_c" in params else None,
        "available": row is None and blocked is None,
        "blocked": blocked,
        "target_c": target_c(),
        "hold_hours": hold_hours(),
        "legionella": next_legionella(now),
    }


def next_legionella(now: datetime) -> dict[str, str] | None:
    """Next weekly stand-off window (the firmware's thermal-shock cycle) within
    8 days, from the same single source the reconciler uses; None when the
    stand-off is not configured."""
    if not getattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", False):
        return None
    from .legionella_window import standoff_windows

    try:
        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    except Exception:  # noqa: BLE001
        tz = UTC
    today: date = now.astimezone(tz).date()
    for start, end in standoff_windows(today - timedelta(days=1), today + timedelta(days=8)):
        if end > now:
            return {"start_utc": _iso_z(start), "end_utc": _iso_z(end)}
    return None


def simulate(now: datetime | None = None) -> dict[str, Any]:
    """Pre-flight for ``/dhw/boost/simulate``: gates + before/after, no writes."""
    from .api.simulation import ActionDiff

    now = now or datetime.now(UTC)
    dev = _cached_device()
    check_gates(dev, now)
    until = now + timedelta(hours=hold_hours())
    tgt = target_c()
    cur = getattr(dev, "tank_target", None)
    diff = ActionDiff(
        action="dhw.boost",
        before={"tank_target": cur, "tank_temp": getattr(dev, "tank_temperature", None)},
        after={"tank_target": tgt, "hold_until_utc": _iso_z(until)},
        human_summary=(
            f"Banho extra: meta do tanque {cur if cur is not None else '?'} -> {tgt:.0f} C, "
            f"o plano não mexe no tanque por {hold_hours():g} h (até {_iso_z(until)})"
        ),
    )
    return {"diff": diff, "until": until}


def apply(now: datetime | None = None) -> dict[str, Any]:
    """Write the boost: source row first, then the tank target."""
    now = now or datetime.now(UTC)
    dev = _cached_device()
    check_gates(dev, now)
    tgt = target_c()
    baseline = getattr(dev, "tank_target", None)
    baseline_c = float(baseline) if baseline is not None else float(config.DHW_TEMP_NORMAL_C)
    until = now + timedelta(hours=hold_hours())
    try:
        tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    except Exception:  # noqa: BLE001
        tz = UTC

    row_id = db.upsert_action(
        plan_date=now.astimezone(tz).date().isoformat(),
        start_time=_iso_z(now),
        end_time=_iso_z(until),
        device="daikin",
        action_type=ACTION_TYPE,
        params={"tank_temp": baseline_c, "boost_target_c": tgt, "baseline_tank_c": baseline_c},
        status="active",
    )
    db.mark_action_user_overridden(row_id, overridden_at=now.isoformat())
    try:
        daikin_service.set_tank_temperature(tgt, actor="api:dhw_boost")
    except (DaikinError, ValueError) as exc:
        db.finish_user_boost(row_id, end_time_iso=_iso_z(now), note=f"boost write failed: {exc}")
        db.log_action(
            device="daikin", action="dhw_user_boost",
            params={"row_id": row_id, "target_c": tgt}, result="failure",
            trigger="api", error_msg=str(exc), actor="api",
        )
        raise
    try:
        dev.tank_target = tgt  # predicted state, same practice as apply_scheduled_daikin_params
    except Exception:  # noqa: BLE001
        pass
    db.log_action(
        device="daikin", action="dhw_user_boost",
        params={"row_id": row_id, "target_c": tgt, "baseline_c": baseline_c,
                "until_utc": _iso_z(until), "hold_hours": hold_hours()},
        result="success", trigger="api", actor="api",
    )
    return {"success": True, "row_id": row_id, "target_c": tgt, "until_utc": _iso_z(until)}


def simulate_undo(now: datetime | None = None) -> dict[str, Any]:
    from .api.simulation import ActionDiff

    now = now or datetime.now(UTC)
    row = active_boost(now)
    if row is None:
        raise BoostBlocked(409, "NoActiveBoost", "Não há banho extra em andamento.")
    dev = _cached_device()
    return {"diff": ActionDiff(
        action="dhw.boost_undo",
        before={"tank_target": getattr(dev, "tank_target", None)},
        after={"tank_target": "segue o plano"},
        human_summary="Desfazer o banho extra: o tanque volta à meta que o plano pede agora.",
    )}


def undo(now: datetime | None = None) -> dict[str, Any]:
    """End the boost early and hand the tank back to the plan."""
    from .state_machine import end_user_boost

    now = now or datetime.now(UTC)
    row = active_boost(now)
    if row is None:
        raise BoostBlocked(409, "NoActiveBoost", "Não há banho extra em andamento.")
    dev = _cached_device()
    if dev is None:
        raise BoostBlocked(409, "NoDevice", "Sem leitura recente do Daikin.")
    out = end_user_boost(row, dev, DaikinClient(), now, reason="undo", trigger="api")
    return {"success": True, **out}
