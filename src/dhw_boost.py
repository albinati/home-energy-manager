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

Heating mechanism. The Onecta firmware only starts a reheat when
``target - tank`` exceeds its reheat differential (~6 C, measured; see
``resolve_reheat_differential_c``), so merely raising the target from 46 to 50 does
nothing. The boost therefore turns DHW **Powerful** on whenever the gap is within
the differential (+0.5 C margin, the same widening as the #735 deadband force):
Powerful makes the unit heat to the setpoint regardless of that hysteresis (the
mechanism the negative-price boost and the #735/#737 deadband escalation already
rely on in prod), at the price of using the immersion element (COP ~1). When the
gap is larger the plain target suffices and the cheaper heat pump does the work,
so Powerful is left off. The target is clamped to the heat-pump cliff
(``t_hp_max_c``, 50 C): above it every degree is resistance.

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

import sqlite3
import threading  # noqa: F401  (documented lock lives in daikin_bulletproof)

from . import db
from .config import config
from .daikin import service as daikin_service
from .daikin.client import DaikinClient, DaikinError
from .daikin_bulletproof import TANK_WRITE_LOCK, holiday_blocks_params

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
    """Boost target, clamped to the ceiling and to the heat-pump cliff."""
    try:
        from .dhw.params import resolve_tank_params

        cliff = float(resolve_tank_params().t_hp_max_c)
    except Exception:  # noqa: BLE001 - databook default
        cliff = 50.0
    return float(min(float(config.DHW_USER_BOOST_TARGET_C), float(config.DHW_TEMP_MAX_C), cliff))


def reheat_differential_c() -> float:
    try:
        from .dhw.params import resolve_reheat_differential_c

        return float(resolve_reheat_differential_c())
    except Exception:  # noqa: BLE001
        return 6.0


def needs_powerful(dev: Any | None) -> bool:
    """True when the target-tank gap is inside the firmware's reheat deadband
    (or the tank temperature is unknown): only Powerful forces a reheat there."""
    tank = getattr(dev, "tank_temperature", None)
    if tank is None:
        return True
    return (target_c() - float(tank)) <= reheat_differential_c() + 0.5


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
    # A target already at the boost's only matters when Powerful would add
    # nothing either (gap beyond the deadband: the unit is already heating).
    if (
        cur is not None
        and float(cur) >= tgt - float(config.DAIKIN_OVERRIDE_TOLERANCE_TANK_C)
        and not needs_powerful(dev)
    ):
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
        "uses_powerful": needs_powerful(dev),
        "reheat_differential_c": reheat_differential_c(),
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
    powerful = needs_powerful(dev)
    diff = ActionDiff(
        action="dhw.boost",
        before={"tank_target": cur, "tank_temp": getattr(dev, "tank_temperature", None)},
        after={"tank_target": tgt, "powerful": powerful, "hold_until_utc": _iso_z(until)},
        safety_flags=["uses_resistance_heater"] if powerful else [],
        human_summary=(
            f"Banho extra: meta do tanque {cur if cur is not None else '?'} -> {tgt:.0f} C"
            f"{' com Powerful (pode usar a resistencia, mais cara)' if powerful else ''}; "
            f"o plano nao mexe no tanque por {hold_hours():g} h (ate {_iso_z(until)})"
        ),
    )
    return {"diff": diff, "until": until}


def apply(now: datetime | None = None) -> dict[str, Any]:
    """Write the boost: source row (one atomic INSERT) first, then the tank."""
    now = now or datetime.now(UTC)
    with TANK_WRITE_LOCK:
        dev = _cached_device()
        check_gates(dev, now)
        tgt = target_c()
        baseline = getattr(dev, "tank_target", None)
        baseline_c = float(baseline) if baseline is not None else float(config.DHW_TEMP_NORMAL_C)
        powerful = needs_powerful(dev)
        until = now + timedelta(hours=hold_hours())
        try:
            tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
        except Exception:  # noqa: BLE001
            tz = UTC
        try:
            row_id = db.insert_user_boost_row(
                plan_date=now.astimezone(tz).date().isoformat(),
                start_time=_iso_z(now),
                end_time=_iso_z(until),
                params={"tank_temp": baseline_c, "boost_target_c": tgt,
                        "baseline_tank_c": baseline_c, "powerful": powerful},
                overridden_at=now.isoformat(),
            )
        except sqlite3.IntegrityError:
            raise BoostBlocked(
                409, "AlreadyBoosted",
                "Já há um banho extra em andamento (ou sendo encerrado).",
            ) from None
        temp_written = False
        try:
            daikin_service.set_tank_temperature(tgt, actor="api:dhw_boost")
            temp_written = True
            if powerful:
                daikin_service.set_tank_powerful(True, actor="api:dhw_boost")
        except (DaikinError, ValueError) as exc:
            db.finish_user_boost(row_id, end_time_iso=_iso_z(now), note=f"boost write failed: {exc}")
            if temp_written:  # best effort: do not leave a half-applied boost behind
                try:
                    daikin_service.set_tank_temperature(baseline_c, actor="api:dhw_boost")
                except Exception:  # noqa: BLE001
                    logger.warning("boost rollback of the tank target failed", exc_info=True)
            db.log_action(
                device="daikin", action="dhw_user_boost",
                params={"row_id": row_id, "target_c": tgt, "powerful": powerful}, result="failure",
                trigger="api", error_msg=str(exc), actor="api",
            )
            raise
        daikin_service.note_predicted_tank_state(tgt, True if powerful else None)
        db.log_action(
            device="daikin", action="dhw_user_boost",
            params={"row_id": row_id, "target_c": tgt, "baseline_c": baseline_c, "powerful": powerful,
                    "until_utc": _iso_z(until), "hold_hours": hold_hours()},
            result="success", trigger="api", actor="api",
        )
    return {"success": True, "row_id": row_id, "target_c": tgt, "powerful": powerful,
            "until_utc": _iso_z(until)}


def check_undo_gates(dev: Any | None, now: datetime) -> None:
    """Undo writes to the unit too, so the same hard gates apply; refuse with a
    distinct code rather than pretending the tank was handed back."""
    from .api_quota import should_block
    from .state_machine import in_legionella_standoff

    if config.OPENCLAW_READ_ONLY:
        raise BoostBlocked(409, "ReadOnly", "HEM está em modo somente leitura (OPENCLAW_READ_ONLY).")
    if config.DAIKIN_CONTROL_MODE == "passive":
        raise BoostBlocked(409, "PassiveModeLocked", "HEM está só observando o Daikin (passivo).")
    if dev is None:
        raise BoostBlocked(409, "NoDevice", "Sem leitura recente do Daikin.")
    if holiday_blocks_params(dev, {"tank_temp": 0}):
        raise BoostBlocked(409, "HolidayMode", "O Daikin está em modo férias: o tanque está desligado.")
    if in_legionella_standoff(now):
        raise BoostBlocked(409, "LegionellaStandoff", "Ciclo de legionela em andamento: o Daikin controla o tanque.")
    if should_block("daikin"):
        raise BoostBlocked(409, "QuotaExhausted", "Cota diária do Daikin esgotada ou escrita pausada.")


def simulate_undo(now: datetime | None = None) -> dict[str, Any]:
    from .api.simulation import ActionDiff

    now = now or datetime.now(UTC)
    row = active_boost(now)
    if row is None:
        raise BoostBlocked(409, "NoActiveBoost", "Não há banho extra em andamento.")
    dev = _cached_device()
    check_undo_gates(dev, now)
    return {"diff": ActionDiff(
        action="dhw.boost_undo",
        before={"tank_target": getattr(dev, "tank_target", None)},
        after={"tank_target": "segue o plano"},
        human_summary="Desfazer o banho extra: o tanque volta à meta que o plano pede agora e o Powerful é desligado.",
    )}


def undo(now: datetime | None = None) -> dict[str, Any]:
    """End the boost early and hand the tank back to the plan. Never claims
    success it did not achieve: ``restored`` is True only when the plan's state
    was written (or already held)."""
    from .state_machine import end_user_boost

    now = now or datetime.now(UTC)
    with TANK_WRITE_LOCK:
        row = active_boost(now)
        if row is None:
            raise BoostBlocked(409, "NoActiveBoost", "Não há banho extra em andamento.")
        dev = _cached_device()
        check_undo_gates(dev, now)
        out = end_user_boost(row, dev, DaikinClient(), now, reason="undo", trigger="api")
    if out["error"]:
        raise BoostBlocked(502, "RestoreFailed", f"Não consegui devolver o tanque ao plano: {out['error']}")
    return {"success": True, **out}
