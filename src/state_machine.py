"""Fail-safe defaults, boot recovery, and action validation (Bulletproof engine)."""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db, dhw_policy
from .config import config
from .daikin.client import DaikinClient, DaikinError
from .daikin_bulletproof import (
    apply_comfort_restore,
    apply_scheduled_daikin_params,
    daikin_device_matches_params,
    detect_user_override,
    user_gesture_still_in_effect,
)
from .foxess.client import FoxESSClient, FoxESSError, scheduler_groups_from_stored_json
from .foxess.models import _group_fingerprint
from .notifier import notify_critical, notify_risk, notify_user_override

logger = logging.getLogger(__name__)

# Phase 4 review C6 — process-local map of action_schedule row id → earliest
# wall-clock time we successfully ran apply_scheduled_daikin_params for that row
# in THIS process. Used as the grace-period anchor for override detection so a
# systemd restart mid-plan cannot cause a false override on the first reconcile
# tick (row.start_time would be ancient, but we haven't actually applied yet).
_FIRST_APPLIED_SESSION: dict[int, datetime] = {}

# #737 — deadband-force HP-trigger guard. The heat pump can be triggered
# sub-cliff only when ``cliff − tank > differential + GUARD``; the guard
# absorbs reheat-differential estimator error so a barely-clearing lift
# doesn't silently fail to fire. The lift itself is always the cliff.
_WARMUP_FORCE_HP_GUARD_C = 0.5

# V12 — per-episode dedup for the user-override notification. Without this set
# the heartbeat fires ``notify_user_override`` every 2-min reconcile while the
# user keeps the manual change in place, drowning Telegram. Cleared when:
#  * the action's override is reconciled (row no longer detected as override), or
#  * the action ends (row drops out of the reconcile window naturally).
_USER_OVERRIDE_NOTIFIED: set[int] = set()

# Epic 14 (#386) — dedup for the inherited-override notification. Keyed on the
# SOURCE override row id (not the suppressed downstream row id), so each
# user gesture produces a single ping even when N subsequent replan rows are
# suppressed against it. Entries linger until the process restarts — set growth
# is bounded by ``USER_OVERRIDE_RESPECT_HOURS`` × user-gesture frequency, well
# below a kilobyte over a service lifetime.
_USER_OVERRIDE_INHERITED_NOTIFIED: set[int] = set()

# Issue #382 — drift-detection dedup. The heartbeat sanity check fires once
# per drift *episode* (tank off when no shutdown was planned) so we don't
# page Telegram every 2 min for the same condition. Cleared when the heartbeat
# observes tank_on == True again — a fresh drift episode then re-pings.
_TANK_DRIFT_NOTIFIED: bool = False
# Last time the negative-boost backstop re-asserted DHW Powerful (cadence gate).
_NEG_BOOST_POWERFUL_LAST_UTC: datetime | None = None
_NEG_BOOST_STALL_COUNT: int = 0
_NEG_BOOST_LAST_TEMP: float | None = None
# 2026-07-04: the stall/cadence state must survive restarts — three same-day
# deploys each reset these in-memory globals, so a 4 h no-progress plateau
# (tank pinned at the hot-day compressor ceiling) never accumulated enough
# consecutive stalls to engage the backoff: ~16 futile Powerful writes.
_NEG_BOOST_STATE_LOADED: bool = False
_NEG_BOOST_STATE_KEY = "neg_boost_stall_state"


def _load_neg_boost_state() -> None:
    """Rehydrate stall/cadence state from runtime_settings (once per boot)."""
    global _NEG_BOOST_POWERFUL_LAST_UTC, _NEG_BOOST_STALL_COUNT
    global _NEG_BOOST_LAST_TEMP, _NEG_BOOST_STATE_LOADED
    if _NEG_BOOST_STATE_LOADED:
        return
    _NEG_BOOST_STATE_LOADED = True
    try:
        raw = db.get_runtime_setting(_NEG_BOOST_STATE_KEY)
        if not raw:
            return
        d = json.loads(raw)
        ts = d.get("last_utc")
        _NEG_BOOST_POWERFUL_LAST_UTC = datetime.fromisoformat(ts) if ts else None
        _NEG_BOOST_STALL_COUNT = int(d.get("stall_count", 0))
        lt = d.get("last_temp")
        _NEG_BOOST_LAST_TEMP = float(lt) if lt is not None else None
    except Exception as e:  # never block the heartbeat on state hydration
        logger.debug("neg-boost state load failed (non-fatal): %s", e)


def _save_neg_boost_state() -> None:
    try:
        db.set_runtime_setting(_NEG_BOOST_STATE_KEY, json.dumps({
            "last_utc": (
                _NEG_BOOST_POWERFUL_LAST_UTC.isoformat()
                if _NEG_BOOST_POWERFUL_LAST_UTC else None
            ),
            "stall_count": _NEG_BOOST_STALL_COUNT,
            "last_temp": _NEG_BOOST_LAST_TEMP,
        }))
    except Exception as e:
        logger.debug("neg-boost state save failed (non-fatal): %s", e)
# #461 — dedup for the LWT-offset drift backstop (same one-page-per-episode
# semantics as the tank flag above). Cleared when the offset is back at 0.
_LWT_DRIFT_NOTIFIED: bool = False

# 2026-06-07 — dedup for the legionella tank stand-off skip log (one telemetry
# row per suppressed action_schedule row per process, not per 5-min tick).
_LEGIONELLA_STANDOFF_LOGGED: set[int] = set()

# #742 review — settle attempts per lift row in THIS process. Bounds the
# retry when Onecta refuses the lowered setpoint (unverified firmware corner):
# without it a persistent READ_ONLY would re-PATCH every ~2-min tick for the
# rest of the window (~40 writes of quota). 3 attempts, then give up — the
# fallback is the pre-#742 cliff coast, which is safe. Success is deduped
# PERSISTENTLY via the warmup_lift_settle audit row, not this dict.
_LIFT_SETTLE_ATTEMPTS: dict[int, int] = {}
_LIFT_SETTLE_MAX_ATTEMPTS = 3

# #741 review — rows whose warmup_deadband_force audit row has been written in
# THIS process. The audit row doubles as the reheat-differential fit's
# exclusion window (#739), so it must exist whenever Powerful may be live:
# the apply path and the failed-apply path always log (a timeout can leave the
# PATCH applied cloud-side), and the pre-fire idempotency path logs once per
# row when the device CONFIRMS the escalation state without us having applied
# it this session (timeout-then-row-completed, or a user's own Powerful).
# Only the pre-fire path is gated by this set — a genuine re-fire after the
# device drifted back must always produce a fresh row.
_DEADBAND_FORCE_LOGGED: set[int] = set()


def _is_tank_action(params: dict[str, Any]) -> bool:
    """True when a row commands the DHW tank (vs. an LWT/space-heating row).

    Legionella stand-off only suppresses tank writes; LWT rows still fire.
    """
    if not isinstance(params, dict):
        return False
    return any(k in params for k in ("tank_temp", "tank_power", "tank_powerful"))


def _warmup_deadband_force_reason(
    dev: Any, apply_params: dict[str, Any], now_utc: datetime
) -> dict[str, Any] | None:
    """Should this warmup be escalated to punch through the firmware reheat
    deadband, and HOW? (#735, #737)

    Returns a decision dict when YES (delta inside the deadband AND the coast
    projection at some declared shower window falls short of its floor), None
    when the plain command suffices — either the firmware will heat on its own
    (delta beyond the deadband) or the coast still delivers the declared
    comfort (the skip is deliberate and cheaper).

    The mechanism matters for cost. The commanded target (e.g. 47) is BELOW
    the resistance cliff (t_hp_max_c, 50 °C), so the heat pump alone can do
    the lift at its certified COP (~2.5). The firmware just won't START,
    because tank − target is inside the deadband. So the cheap fix is to raise
    the COMMANDED target to the CLIFF (``mechanism='hp_target_lift'``,
    ``lift_target_c`` = the cliff): the firmware's ordinary thermostat then
    fires the HEAT PUMP and heats the tank up to the cliff, all sub-resistance.

    Commanding the cliff (not a minimal kick) is deliberate — it makes the
    escalation STABLE across the ~50-min heat-up (review of #737): the review
    showed the reconciler completes the row at whatever target the device
    holds (the idempotency check, not a later "settle" tick), so there is no
    self-settle back to the goal. Left alone the tank would therefore end
    ~3 °C above the goal at the cliff — bounded since #742 by the heartbeat
    settle (``_check_warmup_lift_settle``), which re-commands the goal once
    the tank reaches it, so the lift ends at ~goal+0.5 instead. ``already_lifted``
    keeps the decision pinned to the HP once the device is at the cliff, so a
    tick mid-heat-up (tank now in the would-be-Powerful band) can't flip the
    mechanism and drop COP-1 resistance onto the top few degrees.

    Powerful is the FALLBACK only for the corner where the tank starts so warm
    that no sub-cliff target can clear the deadband (``cliff − tank ≤
    differential``): there the immersion heater (COP ~1) is the only way to
    add heat at all, and comfort requires it. That's ``mechanism='powerful'``.
    Using Powerful for a plain sub-cliff lift would burn ~2.5× the electricity
    for the same heat — the wrong instinct on a heat-pump system (#737).
    """
    from .dhw.comfort import shower_windows
    from .dhw.model import coast_to
    from .dhw.params import resolve_reheat_differential_c, resolve_tank_params

    target = apply_params.get("tank_temp")
    tank = getattr(dev, "tank_temperature", None)
    if target is None or tank is None:
        return None
    target = float(target)
    tank = float(tank)
    delta = target - tank
    if delta <= 0.25:
        return None  # already there
    differential = resolve_reheat_differential_c()
    if delta > differential:
        # Beyond the deadband — the firmware heats unaided. No grace band
        # here (review): with the fallback 6.0 against a measured 6-7 °C
        # deadband, a grace would leave the uncertain (6, 7] range uncovered
        # — the exact #735 failure — while forcing inside it is near-free
        # (Powerful merely accelerates a lift the firmware was doing anyway).
        return None
    # Inside the deadband: project the coast to EVERY declared shower window
    # within the next 24 h (review: judging only the soonest window scored a
    # cheap-night warmup against the morning 40 °C floor and ignored the
    # evening 45 °C one; and a row firing INSIDE a window was scored against
    # tomorrow). A window we are currently inside projects at hours = 0 —
    # the floor is owed NOW.
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    now_local = now_utc.astimezone(tz)
    preset = (config.OPTIMIZATION_PRESET or "normal").strip().lower()
    p = resolve_tank_params()

    def _at_hour(h: float):
        return now_local.replace(
            hour=int(h) % 24, minute=int(round((h % 1) * 60)), second=0, microsecond=0,
        )

    worst: dict[str, Any] | None = None
    for w in shower_windows(preset=preset):
        if float(w.end_hour) == float(w.start_hour):
            # A zero-length window is a DISABLED window, not "always": the
            # end <= start normalisation below would otherwise promote it to
            # a 24 h always-owed floor (review of #748). Only the raw hours
            # can tell x-x apart from 0.0-24.0 — both map to start == end.
            continue
        start = _at_hour(w.start_hour)
        end = _at_hour(w.end_hour)
        if end <= start:
            # ``end_hour=24.0`` lands on 00:00 (int(24) % 24) and a
            # cross-midnight window declares end < start — both mean the end
            # is on the NEXT day. Without this, "inside the window" is
            # unsatisfiable and a fire inside it projects ~21 h of phantom
            # coast to tomorrow's start (#748).
            end += timedelta(days=1)
        if now_local < end - timedelta(days=1):
            # Inside the tail of YESTERDAY's instance of a cross-midnight
            # window (e.g. 22:00-01:00 at 00:30) — judge that instance.
            start -= timedelta(days=1)
            end -= timedelta(days=1)
        if start <= now_local < end:
            hours = 0.0
        else:
            entry = start if start > now_local else start + timedelta(days=1)
            hours = (entry - now_local).total_seconds() / 3600.0
        projected = coast_to(tank, hours, p)
        if projected >= float(w.floor_c):
            continue  # this window's declared floor is still delivered
        shortfall = float(w.floor_c) - projected
        if worst is None or shortfall > worst["shortfall_c"]:
            worst = {
                "window": w.label,
                "hours_to_window": round(hours, 1),
                "projected_c": round(projected, 1),
                "floor_c": float(w.floor_c),
                "shortfall_c": round(shortfall, 1),
            }
    if worst is None:
        return None  # every declared floor survives the coast — keep the free skip

    # Pick the mechanism. The heat pump can be TRIGGERED sub-cliff only while a
    # commanded target above the deadband still fits under the cliff, i.e.
    # ``cliff − tank > differential``. When it can, command the CLIFF (stable
    # across the heat-up; see docstring) — ``int(cliff)`` floors so a
    # fractional cliff never rounds up into resistance. ``already_lifted`` pins
    # the decision to the HP once the device is at the cliff, so warming into
    # the would-be-Powerful band mid-lift can't flip the mechanism.
    cliff = float(p.t_hp_max_c)
    dev_target = getattr(dev, "tank_target", None)
    already_lifted = dev_target is not None and float(dev_target) >= cliff - 0.6
    if already_lifted or (cliff - tank > differential + _WARMUP_FORCE_HP_GUARD_C):
        lift = int(cliff)
        mechanism = "hp_target_lift"
    else:
        lift = None
        mechanism = "powerful"
    return {
        "tank_c": tank,
        "target_c": target,
        "delta_c": round(delta, 1),
        "differential_c": differential,
        "cliff_c": cliff,
        "already_lifted": already_lifted,
        "mechanism": mechanism,
        "lift_target_c": lift,
        **worst,
    }


def in_legionella_standoff(now_utc: datetime) -> bool:
    """True when ``now_utc`` is inside the configured weekly legionella
    thermal-shock window, during which the Onecta firmware owns the DHW tank
    and HEM must not write to it (see ``DHW_LEGIONELLA_STANDOFF_*``).

    The window is defined in UTC on one weekday and must not cross midnight
    (the default Sunday 11:00 + 120 min does not).
    """
    if not getattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", False):
        return False
    if now_utc.weekday() != int(config.DHW_LEGIONELLA_STANDOFF_DOW):
        return False
    start = now_utc.replace(
        hour=int(config.DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC),
        minute=int(config.DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC),
        second=0, microsecond=0,
    )
    end = start + timedelta(minutes=int(config.DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES))
    return start <= now_utc < end


def _parse_utc(s: str) -> datetime:
    x = str(s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _schedule_signature(groups: list[Any]) -> str:
    """Comparable, MODE-AWARE fingerprint for Fox V3 groups (hardware vs SQLite).

    Uses the shared canonical fingerprint so a live device read compares equal
    to what we uploaded. A raw field-by-field signature treated the inverter's
    stale fdSoc/fdPwr echo on SelfUse/Backup groups (and its vendor-default
    maxSoc fill) as drift, so the heartbeat re-uploaded forever and wedged Fox
    dispatch for ~41 h on 2026-06-14 — the same vendor-echo class fixed for the
    schedule_diff endpoint in #554.
    """
    payload = []
    for g in groups:
        if hasattr(g, "start_hour"):
            payload.append(
                _group_fingerprint(
                    g.start_hour, g.start_minute, g.end_hour, g.end_minute,
                    g.work_mode, getattr(g, "min_soc_on_grid", None),
                    g.fd_soc, g.fd_pwr, getattr(g, "max_soc", None),
                )
            )
        elif isinstance(g, dict):
            ep = g.get("extraParam") or g.get("extra_param") or {}
            payload.append(
                _group_fingerprint(
                    g.get("startHour"), g.get("startMinute"),
                    g.get("endHour"), g.get("endMinute"), g.get("workMode"),
                    ep.get("minSocOnGrid"), ep.get("fdSoc"), ep.get("fdPwr"),
                    ep.get("maxSoc"),
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

        if fox.api_key and "scheduler_flag" in applied:
            # Persist the scheduler-off state so local derivations (e.g. the
            # heartbeat's execution_log fox_mode, #669) stop walking the last
            # uploaded groups — they are no longer in force on the inverter.
            try:
                db.save_fox_schedule_state([], enabled=False)
            except Exception as e:
                logger.warning("Safe defaults: could not persist scheduler-off state: %s", e)

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
                "tank_powerful": False,
                "tank_temp": float(config.DHW_TEMP_NORMAL_C),
                "tank_power": True,
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
    # Epic 14 follow-up (#388): when more than one row triggers an apply in
    # the SAME heartbeat tick, sleep DAIKIN_VALVE_SETTLE_SECONDS between
    # them so the cloud has propagation time before our next read/write.
    # Idle ticks (all rows pre-fire-skipped) take zero extra time.
    _applied_in_this_tick = False
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

        # Legionella stand-off (2026-06-07): the Onecta firmware owns the DHW
        # tank during its weekly thermal-shock cycle, so any tank PATCH we send
        # is arbitrated/overridden by the firmware (TANK ONLY — LWT / space
        # heating are unaffected). Skip firing tank rows inside the window and
        # leave them PENDING so they resume the moment the window closes (the
        # firmware leaves the tank hot; HEM's next warmup/setback then brings it
        # back to plan). Do not mark completed — that would strand the row.
        if (
            getattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", False)
            and _is_tank_action(params)
            and in_legionella_standoff(now_utc)
        ):
            if aid not in _LEGIONELLA_STANDOFF_LOGGED:
                _LEGIONELLA_STANDOFF_LOGGED.add(aid)
                try:
                    db.log_action(
                        device="daikin",
                        action="legionella_tank_standoff",
                        params={"row_id": aid, "kind": atype},
                        result="skipped",
                        trigger=trigger,
                    )
                except Exception as _exc:
                    logger.debug("legionella stand-off log failed (non-fatal): %s", _exc)
            continue

        if status == "pending" and start <= now_utc < end:
            db.mark_action(aid, "active")

        if status in ("pending", "active") and start <= now_utc < end:
            apply_params = dict(params)
            # Hands-off climate (PR #300, 2026-05-09): strip climate-side
            # fields from any params reaching Daikin, including legacy
            # action_schedule rows persisted before #300 landed (which the
            # 2026-05-11 incident showed could still carry lwt_offset=-5,
            # sabotaging the heat-pump's ability to reheat the tank). The
            # LP only drives tank state — Daikin firmware owns the curve.
            #
            # #481 — active LWT pre-heat: when that feature is ENABLED, HEM
            # deliberately drives the LWT offset (heuristic, clamped integer),
            # so let lwt_offset through. ``climate_on`` is still stripped (we
            # never drive zone on/off). When disabled, both are stripped — the
            # original climate-hands-off behaviour, defending against stale rows.
            if not config.DAIKIN_LWT_PREHEAT_ENABLED:
                apply_params.pop("lwt_offset", None)
            apply_params.pop("climate_on", None)

            # Deadband-aware warmup (#735, #737): the firmware only reheats when
            # tank ≤ target − differential (~6-7 °C measured), so a warm-tank day
            # makes the commanded warmup a silent no-op — measured 2026-07-17:
            # commanded 47, tank 42, nothing happened, showers at ~40.5 °C
            # against the family's DECLARED 45. When the firmware would skip AND
            # the coast projection falls short of a declared shower floor, escalate
            # — preferring an HP target-lift (raise the commanded target past the
            # deadband but under the cliff, so the heat pump does the lift at its
            # certified COP), with Powerful (COP-1 resistance) only for the corner
            # where the tank is too warm to clear the deadband sub-cliff (#737).
            # When the coast still clears the floor, the skip stays — free money.
            _deadband_force: dict[str, Any] | None = None
            if (
                atype == "tank_warmup"
                and apply_params.get("tank_power")
                and not apply_params.get("tank_powerful")
                and getattr(config, "DHW_WARMUP_DEADBAND_FORCE_ENABLED", True)
                # Review (#735): mirror the #619 gate order — never evaluate or
                # advertise an escalation that passive/read-only mode cannot
                # write (a passive-soak day would otherwise log a false
                # "applied" every heartbeat for the whole row window).
                and not config.OPENCLAW_READ_ONLY
                and config.DAIKIN_CONTROL_MODE == "active"
                # Review (#737): the HP-lift commands the CLIFF, so the row's
                # setpoint diverges from its goal until the idempotency check
                # completes it. That completion is what keeps the divergence
                # from later reading as a user override (a spurious 47→50
                # alert). So the escalation DEPENDS on pre-fire idempotency —
                # when it's off (the documented rollback lever) don't escalate;
                # the warm-tank day just reverts to the pre-#735 deadband skip.
                and config.PREFIRE_STATE_MATCH_ENABLED
            ):
                try:
                    _deadband_force = _warmup_deadband_force_reason(
                        dev, apply_params, now_utc
                    )
                except Exception as _exc:  # noqa: BLE001 — never block a fire
                    _deadband_force = None
                    logger.debug("deadband-force check failed (non-fatal): %s", _exc)
                if _deadband_force:
                    if _deadband_force["mechanism"] == "hp_target_lift":
                        # Raise the COMMANDED target to the cliff so the
                        # firmware's own thermostat fires the heat pump. The
                        # idempotency check completes the row once the device
                        # reaches the cliff; the firmware then heats the tank to
                        # the cliff autonomously. The tank ends ~3 °C above the
                        # goal — accepted (still ~2.5× cheaper than resistance,
                        # buys comfort margin; #737).
                        apply_params["tank_temp"] = _deadband_force["lift_target_c"]
                    else:
                        # Fallback corner: tank too warm to clear the deadband
                        # sub-cliff — resistance (Powerful) is the only way to
                        # add heat, and comfort requires it here.
                        apply_params["tank_powerful"] = True
                    # Audit row is written AFTER the apply actually attempts
                    # writes (see below) — logging here would claim "applied"
                    # for fires the idempotency check then skips.

            # Epic 14 (#386) — pre-fire reconcile.
            #
            # (1) Idempotency: if the live device state already matches the
            #     row's params, mark completed without firing. Naturally
            #     de-dupes overlapping replan rows (bug D) and prevents the
            #     READ_ONLY_CHARACTERISTIC errors that come from PATCHing
            #     a characteristic to a value it already holds (bug E).
            #
            #     We must observe *every* field in apply_params on the live
            #     device before declaring a match. ``daikin_device_matches_params``
            #     silently passes unknown fields (e.g. tank_temp when tank_target
            #     is None) which would over-skip. Belt-and-braces here.
            _obs_attr = {
                "tank_temp": "tank_target",
                "tank_power": "tank_on",
                "tank_powerful": "tank_powerful",
                "lwt_offset": "lwt_offset",
                "climate_on": "is_on",
            }
            all_observable = all(
                getattr(dev, _obs_attr[k], None) is not None
                for k in apply_params
                if k in _obs_attr
            )
            if config.PREFIRE_STATE_MATCH_ENABLED and all_observable:
                try:
                    if daikin_device_matches_params(dev, apply_params):
                        db.mark_action(
                            aid, "completed",
                            error_msg="noop (state matched pre-fire)",
                        )
                        db.log_action(
                            device="daikin",
                            action="prefire_state_match",
                            params={"row_id": aid, "kind": atype},
                            result="skipped",
                            trigger=trigger,
                        )
                        # #741 review — the device CONFIRMS the escalation
                        # state is live, but this session never wrote the
                        # audit row (a set_tank_powerful timeout can apply
                        # cloud-side and raise here, or the user pressed
                        # Powerful themselves). The audit row is the #739
                        # fit-exclusion window, so write it once per row.
                        if _deadband_force and aid not in _DEADBAND_FORCE_LOGGED:
                            _DEADBAND_FORCE_LOGGED.add(aid)
                            try:
                                db.log_action(
                                    device="daikin",
                                    action="warmup_deadband_force",
                                    params={"row_id": aid, "via": "prefire_match",
                                            **_deadband_force},
                                    # Not "applied" — HEM wrote nothing here;
                                    # the device state confirms the escalation
                                    # is live (review: honest result values).
                                    result="confirmed",
                                    trigger=trigger,
                                )
                            except Exception as _exc:
                                logger.debug(
                                    "deadband-force log failed (non-fatal): %s", _exc,
                                )
                        _USER_OVERRIDE_NOTIFIED.discard(aid)
                        continue
                except Exception as _exc:
                    # Fail-open: if the comparator misbehaves we still fire.
                    logger.debug("prefire state-match check failed (non-fatal): %s", _exc)

            # (2) Override inheritance: if a recent user gesture is still
            #     pushing the device away from the schedule, suppress fresh
            #     replan rows that would reverse it (bug C). Restore rows are
            #     exempted so the system can return to baseline once the
            #     gesture either ages out or is reverted.
            if atype != "restore":
                try:
                    src = db.find_recent_user_override(
                        device="daikin",
                        within_hours=float(config.USER_OVERRIDE_RESPECT_HOURS),
                        now_utc=now_utc,
                        respect_until_window_end=bool(
                            config.USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END
                        ),
                    )
                    if src is not None and src.get("id") != aid:
                        src_params = src.get("params") or {}
                        if isinstance(src_params, str):
                            try:
                                src_params = json.loads(src_params)
                            except (json.JSONDecodeError, TypeError):
                                src_params = {}
                        # Only suppress if (a) the user's gesture is still
                        # detectable on the live device, AND (b) the row we're
                        # about to fire would actually change device state
                        # (i.e. would reverse the gesture). If (b) is False
                        # the idempotency check already fired and we wouldn't
                        # reach here — but we re-check for safety when the
                        # idempotency feature flag is disabled.
                        gesture_active = user_gesture_still_in_effect(dev, src_params)
                        would_change_state = not daikin_device_matches_params(dev, apply_params)
                        if gesture_active and would_change_state:
                            db.mark_action_user_overridden(aid)
                            src_id = int(src.get("id") or 0)
                            db.log_action(
                                device="daikin",
                                action="prefire_override_inherited",
                                params={
                                    "row_id": aid,
                                    "source_row_id": src_id,
                                    "kind": atype,
                                },
                                result="skipped",
                                trigger=trigger,
                            )
                            # Audit (#386 follow-up): inherited rows themselves
                            # become the "source" for the next replan row's
                            # find_recent_user_override lookup (their
                            # overridden_by_user_at is now the most recent).
                            # Without also deduping ``aid`` here, each step of
                            # the chain (A → B → C → D) fires its own
                            # notification because the src_id rotates
                            # (A, B, C…). Add aid alongside src_id so the
                            # next iteration sees its source pre-deduped.
                            if src_id and src_id not in _USER_OVERRIDE_INHERITED_NOTIFIED:
                                _USER_OVERRIDE_INHERITED_NOTIFIED.add(src_id)
                                _USER_OVERRIDE_INHERITED_NOTIFIED.add(aid)
                                try:
                                    notify_user_override(
                                        f"override inherited from row {src_id} "
                                        f"(user gesture still in effect, "
                                        f"row {aid} suppressed)"
                                    )
                                except Exception as _exc:
                                    logger.debug(
                                        "notify_user_override (inherited) failed (non-fatal): %s",
                                        _exc,
                                    )
                            else:
                                # Source already known — silent inheritance
                                # propagation. Still track ``aid`` so deeper
                                # chain rows continue to be silent.
                                _USER_OVERRIDE_INHERITED_NOTIFIED.add(aid)
                            continue
                except Exception as _exc:
                    logger.debug(
                        "prefire override-inheritance check failed (non-fatal): %s", _exc,
                    )

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

            # Epic 14 follow-up (#388): inter-row settle. If we already
            # called apply_scheduled_daikin_params earlier in this same
            # heartbeat tick for this device, give Onecta time to
            # propagate the previous write before we PATCH again.
            # Reusing DAIKIN_VALVE_SETTLE_SECONDS keeps the knob count low.
            if _applied_in_this_tick:
                inter_row_settle = max(
                    0, int(getattr(config, "DAIKIN_VALVE_SETTLE_SECONDS", 10)),
                )
                if inter_row_settle > 0:
                    logger.debug(
                        "inter-row settle: sleeping %ds before applying row %s",
                        inter_row_settle, aid,
                    )
                    time.sleep(inter_row_settle)

            try:
                applied = apply_scheduled_daikin_params(
                    dev, client, apply_params, trigger=trigger,
                )
                if applied and _deadband_force:
                    _DEADBAND_FORCE_LOGGED.add(aid)
                    try:
                        db.log_action(
                            device="daikin",
                            action="warmup_deadband_force",
                            params={"row_id": aid, **_deadband_force},
                            result="applied",
                            trigger=trigger,
                        )
                    except Exception as _exc:
                        logger.debug("deadband-force log failed (non-fatal): %s", _exc)
                # Mark the row as applied-in-session on the first successful call
                # (skip_if_matches=True path also counts — match confirms alignment).
                _FIRST_APPLIED_SESSION.setdefault(aid, now_utc)
                # Track whether THIS call actually attempted writes. The fn
                # returns False when skip_if_matches catches a match, or when
                # the passive / read-only / disabled gates short-circuit.
                # We only want to settle before the next row if the previous
                # one actually hit the cloud.
                if applied:
                    _applied_in_this_tick = True
            except (DaikinError, ValueError) as e:
                logger.warning("Boot Daikin apply %s: %s", aid, e)
                db.mark_action(aid, "failed", error_msg=str(e))
                # #741 review — a failed row is never reconciled again, but a
                # timed-out set_tank_powerful may still have applied cloud-side.
                # Write the audit row anyway (result='failed') so the #739 fit
                # exclusion covers the window either way — a spurious exclusion
                # costs one episode; a missed one poisons the fit.
                if _deadband_force:
                    _DEADBAND_FORCE_LOGGED.add(aid)
                    try:
                        db.log_action(
                            device="daikin",
                            action="warmup_deadband_force",
                            params={"row_id": aid, **_deadband_force},
                            result="failed",
                            trigger=trigger,
                        )
                    except Exception as _exc:
                        logger.debug("deadband-force log failed (non-fatal): %s", _exc)
                # Even on failure, the attempt was made — wait before the next.
                _applied_in_this_tick = True

    # Issue #382 — heartbeat tank-power sanity check. After the per-row loop,
    # detect "tank is off but no plan slot intended it to be off" drift and
    # either alert or alert+auto-recover. This catches the 2026-05-21
    # scenario where the paired restore was deleted by an MPC re-plan
    # (clear_actions_in_range) and never re-emitted; the new
    # RESTORE_PRESERVE_LEAD_MINUTES guard is the primary fix, this is the
    # belt-and-braces backstop for any drift mechanism we haven't yet
    # imagined.
    _check_tank_power_drift(actions, client, dev, now_utc, trigger=trigger)
    # #742 — finish an HP target-lift (#737) at the row's GOAL, not the cliff:
    # the cliff command exists only to START the heat pump from inside the
    # reheat deadband; once the tank reaches the goal, re-command the goal so
    # the firmware's thermostat stops there instead of paying for the last
    # ~3 °C of unrequested margin.
    _check_warmup_lift_settle(actions, client, dev, now_utc, trigger=trigger)
    # #461 — LWT-offset drift: when HEM owns the offset (pre-heat enabled) but
    # the live device holds a non-zero offset no plan slot justifies, reset it
    # to 0 once the user's grace window has passed. Catches a manual offset with
    # no paired restore.
    _check_lwt_offset_drift(actions, client, dev, now_utc, trigger=trigger)
    # 2026-06-28 — sustain DHW Powerful through a negative-price boost window.
    # The boost row fires once at window start; Daikin auto-clears Powerful,
    # leaving the tank coasting for the rest of a paid window. Re-assert it.
    _check_negative_boost_powerful(actions, client, dev, now_utc, trigger=trigger)
    # Early setback on evening shower drawdown — once the household's showers
    # drain the tank, pull the setback forward so the firmware doesn't reheat
    # the freshly-drawn tank at peak price from the battery. Persist-once per
    # day; dhw_policy regenerations honour the same key (K1+K2 lockstep).
    _check_dhw_shower_drawdown(actions, dev, now_utc, trigger=trigger)
    # PR J diverter removed 2026-05-23 (K2-cleanup) — superseded by K1's
    # dhw_policy fixed schedule. The diverter's "lift tank during PV
    # abundance" goal is now redundant: tank lives at NORMAL=45 °C via
    # dhw_policy, and PV excess goes to the battery (where it has the
    # higher economic value anyway).


def _check_dhw_shower_drawdown(
    actions: list[dict[str, Any]],
    dev: Any,
    now_utc: datetime,
    *,
    trigger: str,
) -> None:
    """Early tank setback on evening shower drawdown.

    When the household's evening showers drain the tank (a fast drop of
    ``DHW_EARLY_SETBACK_TRIGGER_DELTA_C`` below the evening's running max),
    holding the warmup target until the static setback hour makes the Onecta
    firmware reheat the freshly-drawn tank IMMEDIATELY — at peak price, from
    the battery (~1.0-1.6 kWh measured; 2026-07-10 finished a 38→45 °C reheat
    at 21:53, seven minutes before the 22:00 setback). The K2 pin already
    models that reheat as deferred to the next day's warmup, so firing the
    setback at the drawdown aligns the hardware with the plan.

    Mechanism (no direct Daikin write from here):
      1. persist the fire time (``dhw_policy.persist_early_setback``, first
         write wins) — every schedule regeneration honours it from then on;
      2. mark the covering warmup row completed so this reconciler stops
         asserting NORMAL (and can't misread the new state as a user
         override);
      3. upsert the pulled-forward ``tank_setback`` row — the NEXT heartbeat
         tick dispatches it through the standard pre-fire guards
         (idempotency, override inheritance, legionella stand-off).

    Fail-safe bails (silence is OK, a wrong early setback is not):
      * feature off / fixed schedule off / read-only / passive control;
      * mode ≠ normal (guests keeps its 24 h warmup — morning showers);
      * outside the armed local window [ARM_HOUR, setback_hour);
      * already fired today (persist-once key present);
      * no live tank temperature;
      * no active dhw_policy warmup row covering now (boost superseded it,
        vacation, or the tank isn't policy-owned right now);
      * a negative-price boost row overlaps the rest of the evening —
        we're being PAID to heat; never cut that short;
      * a user override is in effect, or the live target diverges from the
        warmup row's target (someone hand-set the tank — respect it);
      * fewer than two confirming telemetry samples below the threshold
        (one glitchy reading must not drop the tank for the night).
    """
    if not getattr(config, "DHW_EARLY_SETBACK_ENABLED", False):
        return
    if not getattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True):
        return
    if config.OPENCLAW_READ_ONLY or config.DAIKIN_CONTROL_MODE != "active":
        return
    mode = (config.OPTIMIZATION_PRESET or "normal").strip().lower()
    if mode != "normal":
        return

    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    now_local = now_utc.astimezone(tz)
    arm_hour = int(getattr(config, "DHW_EARLY_SETBACK_ARM_HOUR_LOCAL", 20))
    setback_hour = int(getattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22))
    if not (arm_hour <= now_local.hour < setback_hour):
        return
    today_local = now_local.date()
    if dhw_policy.read_early_setback(today_local) is not None:
        return  # already fired today

    tank_now = getattr(dev, "tank_temperature", None)
    if tank_now is None:
        return
    tank_now = float(tank_now)

    setback_start_utc = datetime(
        today_local.year, today_local.month, today_local.day,
        setback_hour, 0, tzinfo=tz,
    ).astimezone(UTC)

    # The policy must own the tank right now: an un-overridden dhw_policy
    # warmup row covering now is the thing we'd be pulling forward FROM.
    # COMPLETED rows count — under the #386 pre-fire idempotency (default on)
    # every dhw_policy row goes terminal ("noop (state matched pre-fire)")
    # within a tick or two of firing, so by the evening the covering warmup
    # row is ALWAYS 'completed' (same lifecycle reality that makes
    # _check_negative_boost_powerful target completed rows). Only 'failed'
    # and user-overridden rows don't establish policy ownership; the live-
    # target sanity check below independently confirms the device still
    # holds the row's target.
    warmup_row: dict[str, Any] | None = None
    for act in actions:
        if act.get("action_type") != "tank_warmup":
            continue
        if (act.get("status") or "") not in ("pending", "active", "completed"):
            continue
        if act.get("overridden_by_user_at"):
            continue
        try:
            start = _parse_utc(act["start_time"])
            end = _parse_utc(act["end_time"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (start <= now_utc < end):
            continue
        params = act.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        if not params.get("dhw_policy"):
            continue
        warmup_row = {**act, "params": params}
        break
    if warmup_row is None:
        return

    # Never cut a paid window short: any negative-price boost row touching
    # the remaining evening (now → static setback) wins over the detector.
    # NO status filter — the #386 state-match marks a boost row 'completed'
    # at window start (see _check_negative_boost_powerful), and even a
    # 'failed' or user-overridden boost means someone/something else owns
    # the tank tonight. Fail-safe direction: any boost row → bail.
    for act in actions:
        if act.get("action_type") != "tank_negative_boost":
            continue
        try:
            b_start = _parse_utc(act["start_time"])
            b_end = _parse_utc(act["end_time"])
        except (ValueError, KeyError, TypeError):
            continue
        if b_end > now_utc and b_start < setback_start_utc:
            return

    # Respect a user gesture that's still in effect (same rule as the
    # tank-power drift backstop).
    try:
        src = db.find_recent_user_override(
            device="daikin",
            within_hours=float(config.USER_OVERRIDE_RESPECT_HOURS),
            now_utc=now_utc,
            respect_until_window_end=bool(
                config.USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END
            ),
        )
        if src is not None:
            src_params = src.get("params") or {}
            if isinstance(src_params, str):
                try:
                    src_params = json.loads(src_params)
                except (json.JSONDecodeError, TypeError):
                    src_params = {}
            if user_gesture_still_in_effect(dev, src_params):
                return
    except Exception as _exc:
        logger.debug("early-setback override lookup failed (non-fatal): %s", _exc)

    # Live-target sanity: if the device target diverges from the warmup row's
    # (user cranked it via the app without tripping the override detector yet),
    # leave the tank alone.
    live_target = getattr(dev, "tank_target", None)
    row_target = warmup_row["params"].get("tank_temp")
    if (
        live_target is not None
        and row_target is not None
        and abs(float(live_target) - float(row_target)) > 1.5
    ):
        return

    # Drawdown signature: tank standing loss is ~0.5 °C/h, so a drop of
    # TRIGGER_DELTA below the running max is a draw, not decay. The reference
    # window starts ONE HOUR BEFORE the arm hour (still deep inside the
    # warmup hold) so a draw straddling the arm boundary — shower 19:50→20:10
    # — is measured against the true pre-shower hold, not against already-
    # dropped values. The max is clamped to the warmup target + tolerance so
    # a single glitched-HIGH sample can't manufacture a phantom drawdown and
    # drop the tank before anyone showered (fires must be conservative both
    # ways: the TWO newest samples below threshold guard the low side, the
    # clamp guards the high side).
    delta_c = float(getattr(config, "DHW_EARLY_SETBACK_TRIGGER_DELTA_C", 4.0))
    arm_utc = datetime(
        today_local.year, today_local.month, today_local.day,
        arm_hour, 0, tzinfo=tz,
    ).astimezone(UTC)
    try:
        samples = db.get_tank_temps_since((arm_utc - timedelta(hours=1)).timestamp())
    except Exception as _exc:
        logger.debug("early-setback telemetry read failed (non-fatal): %s", _exc)
        return
    if len(samples) < 2:
        return
    window_max = max([t for _, t in samples] + [tank_now])
    if row_target is not None:
        window_max = min(window_max, float(row_target) + 1.0)
    threshold = window_max - delta_c
    recent = [t for _, t in samples[-2:]]
    if tank_now > threshold or any(t > threshold for t in recent):
        return

    # Fire — persist first (the lockstep key), first write wins.
    if not dhw_policy.persist_early_setback(today_local, now_utc):
        return
    minutes_early = max(0, int((setback_start_utc - now_utc).total_seconds() // 60))
    if (warmup_row.get("status") or "") in ("pending", "active"):
        # Terminal rows stay as they are — only a still-live warmup row needs
        # closing so this reconciler stops asserting NORMAL.
        db.mark_action(
            int(warmup_row["id"]), "completed",
            error_msg="early_setback: shower drawdown detected",
        )
    early_row = dhw_policy.build_early_setback_row(today_local, now_utc)
    db.upsert_action(
        device=early_row["device"],
        action_type=early_row["action_type"],
        start_time=early_row["start_time"],
        end_time=early_row["end_time"],
        params=early_row["params"],
        plan_date=str(today_local),
        status="pending",
    )
    db.log_action(
        device="daikin",
        action="dhw_early_setback",
        params={
            "window_max_c": round(window_max, 1),
            "tank_now_c": round(tank_now, 1),
            "delta_c": round(window_max - tank_now, 1),
            "trigger_delta_c": delta_c,
            "minutes_before_static_setback": minutes_early,
            "warmup_row_id": int(warmup_row["id"]),
        },
        result="applied",
        trigger=trigger,
    )
    logger.info(
        "dhw_early_setback: tank %.1f→%.1f °C (Δ%.1f ≥ %.1f) at %s — setback "
        "pulled forward %d min; warmup row %s completed",
        window_max, tank_now, window_max - tank_now, delta_c,
        now_local.strftime("%H:%M"), minutes_early, warmup_row["id"],
    )


def _check_warmup_lift_settle(
    actions: list[dict[str, Any]],
    client: DaikinClient,
    dev: Any,
    now_utc: datetime,
    *,
    trigger: str,
) -> None:
    """#742 — finish an HP target-lift at the GOAL, not the cliff.

    The #737 escalation commands the resistance cliff (50) because that is the
    only way to make the firmware's thermostat START the heat pump from inside
    the reheat deadband — but the owner's spec is the row's goal (47), and left
    alone the firmware heats all the way to the cliff (measured 2026-07-18:
    commanded 50, stopped at 51 — +4 °C of paid heat above the goal).

    So once the tank has REACHED the goal, re-command the goal. The firmware
    stops heating when tank ≥ target — the one semantic we can rely on without
    assuming anything about how a running cycle reacts to a lowered-but-still-
    above-tank setpoint (which is why the settle waits for the goal instead of
    lowering the target mid-lift). Failure is graceful both ways: a missed
    tick heats a little further toward the cliff (the pre-#742 behaviour); a
    spurious settle commands what the plan wanted anyway.

    Bails on any of (fail-safe — silence is fine):
    * kill switch off, read-only, passive mode, legionella stand-off;
    * live tank temp/target/power unknown, or tank off;
    * device target below the cliff — nothing to settle. This is also the
      natural dedup: after one successful settle the device target reads the
      goal, and after the row window the setback owns the tank;
    * no ``tank_warmup`` row with a sub-cliff goal covering ``now`` (a
      cliff-goal row — e.g. PV-abundance storage — WANTS the cliff);
    * the covering row has no ``hp_target_lift`` audit row — a cliff-level
      target HEM did not command is the user's own gesture; respect it.
    """
    if not getattr(config, "DHW_WARMUP_LIFT_SETTLE_ENABLED", True):
        return
    if config.OPENCLAW_READ_ONLY or config.DAIKIN_CONTROL_MODE != "active":
        return
    if in_legionella_standoff(now_utc):
        return
    tank = getattr(dev, "tank_temperature", None)
    dev_target = getattr(dev, "tank_target", None)
    if tank is None or dev_target is None or not getattr(dev, "tank_on", None):
        return
    try:
        from .dhw.params import resolve_tank_params

        cliff = float(resolve_tank_params().t_hp_max_c)
    except Exception as _exc:  # noqa: BLE001 — never block the heartbeat
        logger.debug("lift-settle: tank params unavailable (non-fatal): %s", _exc)
        return
    if float(dev_target) < cliff - 0.6:
        return  # not lifted (or already settled)

    # #745 — a negative-price boost window owns the tank. The boost commands
    # boost_c (60 °C, Powerful): settling DOWN to a warmup goal mid-window
    # would forfeit the paid import, and nothing would re-raise the target —
    # the boost row completes via idempotency and the #619 backstop re-asserts
    # only Powerful. Any status counts: pending is about to fire, completed
    # already fired, and an overridden boost is still the user's gesture.
    for act in actions:
        if act.get("action_type") != "tank_negative_boost":
            continue
        try:
            b_start = _parse_utc(act["start_time"])
            b_end = _parse_utc(act["end_time"])
        except (ValueError, KeyError, TypeError):
            continue
        if b_start <= now_utc < b_end:
            return

    row = None
    for act in actions:
        if act.get("action_type") != "tank_warmup":
            continue
        if (act.get("status") or "") not in ("pending", "active", "completed"):
            continue  # a lift row is 'completed' via pre-fire idempotency (#386)
        if act.get("overridden_by_user_at"):
            continue
        try:
            start = _parse_utc(act["start_time"])
            end = _parse_utc(act["end_time"])
        except (ValueError, KeyError, TypeError):
            continue
        if start <= now_utc < end:
            row = act
            row_start = start
            break
    if row is None:
        return
    params = row.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            params = {}
    goal = params.get("tank_temp")
    if goal is None:
        return
    goal = float(goal)
    if goal >= cliff - 0.6:
        return  # the plan itself wants the cliff — not a #737 lift
    if float(tank) < goal:
        return  # still lifting — let the heat pump work

    # Only settle a lift WE commanded: the audit row is the ownership proof.
    rid = row.get("id")
    try:
        since = (row_start - timedelta(minutes=1)).isoformat()
        logs = db.get_action_logs(
            device="daikin", action="warmup_deadband_force", since=since, limit=20,
        )
    except Exception as _exc:  # noqa: BLE001
        logger.debug("lift-settle: audit lookup failed (non-fatal): %s", _exc)
        return
    lift_log_params: dict[str, Any] | None = None
    for log in logs:
        lp = log.get("params") or {}
        if lp.get("row_id") == rid and lp.get("mechanism") == "hp_target_lift":
            lift_log_params = lp
            break
    if lift_log_params is None:
        return

    # #745 — settle only the target WE lifted to. The audit row records the
    # commanded lift; a device target meaningfully above it (e.g. a 60 °C user
    # gesture set mid-lift, which the override detector cannot attribute to
    # this already-completed row) is an intent HEM never commanded — leave it.
    # EXCEPT the residue of HEM's OWN finished boost window (review): a
    # tank_negative_boost row that already ended commanded exactly this
    # target, and above the cliff the firmware grinds on at COP-1 resistance
    # at now-positive prices — settling to the goal halts it (what pre-#745
    # behaviour did). A user re-asserting the boost target right after its
    # window is indistinguishable, but "stop paid-window heating when the
    # window is over" is the household's standing intent.
    lift_target = lift_log_params.get("lift_target_c")
    if lift_target is not None and abs(float(dev_target) - float(lift_target)) > 0.6:
        own_boost_residue = False
        for act in actions:
            if act.get("action_type") != "tank_negative_boost":
                continue
            if act.get("overridden_by_user_at"):
                continue
            b_params = act.get("params") or {}
            if isinstance(b_params, str):
                try:
                    b_params = json.loads(b_params)
                except (json.JSONDecodeError, TypeError):
                    continue
            b_temp = b_params.get("tank_temp")
            if b_temp is None or abs(float(dev_target) - float(b_temp)) > 0.6:
                continue
            try:
                b_end = _parse_utc(act["end_time"])
            except (ValueError, KeyError, TypeError):
                continue
            if b_end <= now_utc:
                own_boost_residue = True
                break
        if not own_boost_residue:
            return

    # #743 review — ownership is per EPISODE, not per row: after one
    # successful settle, a cliff-level target re-appearing inside the same
    # window can only be the USER's own gesture (HEM never re-lifts a
    # completed row) — respect it. The settle audit row is the persistent
    # proof, so this survives restarts.
    try:
        settle_logs = db.get_action_logs(
            device="daikin", action="warmup_lift_settle", since=since, limit=20,
        )
    except Exception as _exc:  # noqa: BLE001
        logger.debug("lift-settle: settle-log lookup failed (non-fatal): %s", _exc)
        return
    if any((log.get("params") or {}).get("row_id") == rid for log in settle_logs):
        return

    # #743 review — bounded retry: if Onecta refuses the lowered setpoint,
    # give up after a few attempts; the fallback is the safe cliff coast.
    attempts = _LIFT_SETTLE_ATTEMPTS.get(rid, 0)
    if attempts >= _LIFT_SETTLE_MAX_ATTEMPTS:
        return
    _LIFT_SETTLE_ATTEMPTS[rid] = attempts + 1

    try:
        apply_scheduled_daikin_params(
            dev, client, {"tank_power": True, "tank_temp": goal},
            trigger=f"warmup_lift_settle:{trigger}",
        )
    except (DaikinError, ValueError) as exc:
        logger.warning("warmup lift settle failed (non-fatal): %s", exc)
        return
    try:
        db.log_action(
            device="daikin",
            action="warmup_lift_settle",
            params={
                "row_id": rid,
                "tank_c": float(tank),
                "goal_c": goal,
                "from_target_c": float(dev_target),
            },
            result="applied",
            trigger=trigger,
        )
    except Exception as _exc:  # noqa: BLE001 — never block the heartbeat
        logger.debug("lift-settle log failed (non-fatal): %s", _exc)


def _check_tank_power_drift(
    actions: list[dict[str, Any]],
    client: DaikinClient,
    dev: Any,
    now_utc: datetime,
    *,
    trigger: str,
) -> None:
    """Issue #382 — alert + (optionally) recover when the tank is off and no
    plan slot intends it to be off.

    Bails on any of the following (fail-safe — silence is OK, false-alerts
    are not):
    * Feature flag disabled.
    * Live tank state unknown (Daikin cache miss).
    * Tank is on — no drift.
    * Any active/pending slot covering ``now_utc`` carries
      ``tank_power=False`` (planned shutdown, drift is expected).
    * A recent user override on Daikin is still in effect — the user wants
      the tank off; respect their gesture.

    Dedupes via module-level ``_TANK_DRIFT_NOTIFIED`` so a sustained drift
    only pages once. When the tank comes back on, the flag clears and a
    fresh drift episode pages again.
    """
    global _TANK_DRIFT_NOTIFIED

    if not config.TANK_DRIFT_CHECK_ENABLED:
        return
    # PR C — Vacation mode: tank-off is the intended state, not drift.
    # Skip the check entirely so the heartbeat doesn't ping or force-
    # restore. The Daikin firmware still runs the weekly legionella
    # cycle autonomously.
    if (config.OPTIMIZATION_PRESET or "normal").strip().lower() == "vacation":
        return
    tank_on = getattr(dev, "tank_on", None)
    if tank_on is None:
        return  # unknown live state — fail-safe
    if tank_on:
        # Tank is on — clear the dedup token so any future drift re-pings.
        if _TANK_DRIFT_NOTIFIED:
            _TANK_DRIFT_NOTIFIED = False
        return

    # Tank is off. Is any current slot deliberately requesting it off?
    planned_off = False
    for act in actions:
        try:
            start = _parse_utc(act["start_time"])
            end = _parse_utc(act["end_time"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (start <= now_utc < end):
            continue
        if (act.get("status") or "") not in ("pending", "active"):
            continue
        if act.get("overridden_by_user_at"):
            continue
        params = act.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        if "tank_power" in params and not bool(params["tank_power"]):
            planned_off = True
            break
    if planned_off:
        return

    # Respect a user gesture that's still in effect.
    try:
        src = db.find_recent_user_override(
            device="daikin",
            within_hours=float(config.USER_OVERRIDE_RESPECT_HOURS),
            now_utc=now_utc,
            respect_until_window_end=bool(
                config.USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END
            ),
        )
        if src is not None:
            src_params = src.get("params") or {}
            if isinstance(src_params, str):
                try:
                    src_params = json.loads(src_params)
                except (json.JSONDecodeError, TypeError):
                    src_params = {}
            if user_gesture_still_in_effect(dev, src_params):
                return
    except Exception as _exc:
        logger.debug("tank-drift override lookup failed (non-fatal): %s", _exc)

    # Drift confirmed.
    db.log_action(
        device="daikin",
        action="tank_drift_detected",
        params={
            "tank_on": False,
            "auto_recover": bool(config.TANK_DRIFT_AUTO_RECOVER),
        },
        result="alert",
        trigger=trigger,
    )
    recovered = False
    if (
        config.TANK_DRIFT_AUTO_RECOVER
        and not config.OPENCLAW_READ_ONLY
        and config.DAIKIN_CONTROL_MODE == "active"
    ):
        try:
            apply_comfort_restore(dev, client, trigger=f"tank_drift_recover:{trigger}")
            recovered = True
        except (DaikinError, ValueError) as exc:
            logger.warning("tank-drift auto-recover failed: %s", exc)

    if not _TANK_DRIFT_NOTIFIED:
        _TANK_DRIFT_NOTIFIED = True
        try:
            msg = (
                "Tank power=OFF and no plan slot scheduled it off — "
                f"{'force-restored to NORMAL' if recovered else 'manual recovery may be needed'} "
                f"(trigger={trigger})"
            )
            notify_critical(msg) if not recovered else notify_risk(msg)
        except Exception as _exc:
            logger.debug("tank-drift notify failed (non-fatal): %s", _exc)


def _check_negative_boost_powerful(
    actions: list[dict[str, Any]],
    client: DaikinClient,
    dev: Any,
    now_utc: datetime,
    *,
    trigger: str,
) -> None:
    """Sustain DHW Powerful through a negative-price boost window.

    The ``tank_negative_boost`` row sets ``tank_powerful=True`` once at window
    start, but Daikin Powerful is a one-shot the firmware auto-clears (timeout
    / on reaching setpoint). Confirmed in prod (2026-06-28): mid negative
    window the tank sat at 51 °C (target 60) with Powerful OFF during −2..−5 p
    slots — i.e. we stopped pulling the paid kWh while the tank still had
    headroom. This backstop re-asserts Powerful on a bounded cadence so the
    boost is sustained across the whole window.

    Targets ``status='completed'`` boost rows (review of #606): the #386
    pre-fire idempotency marks the boost row *completed* at window start (live
    setpoint already 60), so for the rest of the window the row is completed
    and the per-row apply loop never revisits it — exactly when Powerful auto-
    clears. Active rows (still firing) are owned by that loop; this backstop
    owns the post-completion gap, which avoids a double-write in the same pass.
    The ``action_type`` filter is required so we don't re-assert Powerful for
    ``solar_charge`` (PV-abundance, no paid import) or user ``pre_heat`` rows
    that also carry ``tank_powerful=True``.

    Bails (fail-safe — a missed re-assert is cheap, a wrong write isn't):
    * feature flag off / read-only / not active control / vacation preset;
    * no completed ``tank_negative_boost`` slot covering ``now``;
    * live tank state unknown, tank off, or already at the boost target
      (Powerful would just expire again — nothing to gain);
    * a user gesture (tank off / override) is still in effect;
    * within the min re-assert interval since the last attempt.
    """
    global _NEG_BOOST_POWERFUL_LAST_UTC

    if not config.DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_ENABLED:
        return
    _load_neg_boost_state()
    if config.OPENCLAW_READ_ONLY or config.DAIKIN_CONTROL_MODE != "active":
        return
    if (config.OPTIMIZATION_PRESET or "normal").strip().lower() == "vacation":
        return

    # Find the negative-boost row whose window covers now. Use the row's own
    # intended target (M4): an overlapping tank_setback row can pull the live
    # ``dev.tank_target`` down to ~45, which would mask the real 60 °C headroom.
    boost_target: float | None = None
    for act in actions:
        if (act.get("action_type") or "") != "tank_negative_boost":
            continue
        if (act.get("status") or "") != "completed":
            continue
        if act.get("overridden_by_user_at"):
            continue
        try:
            start = _parse_utc(act["start_time"])
            end = _parse_utc(act["end_time"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (start <= now_utc < end):
            continue
        params = act.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        if not bool(params.get("tank_powerful")):
            continue
        try:
            boost_target = float(params["tank_temp"])
        except (KeyError, TypeError, ValueError):
            boost_target = None
        break
    if boost_target is None:
        return

    # Need the tank ON and below the boost target to have anything to gain.
    if not getattr(dev, "tank_on", False):
        return
    tank_t = getattr(dev, "tank_temperature", None)
    if tank_t is None:
        return
    if float(tank_t) >= boost_target - 0.6:
        return  # already at target — re-asserting Powerful would just expire

    # Respect a user gesture that's still in effect (mirror tank-power drift).
    try:
        src = db.find_recent_user_override(
            device="daikin",
            within_hours=float(config.USER_OVERRIDE_RESPECT_HOURS),
            now_utc=now_utc,
            respect_until_window_end=bool(config.USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END),
        )
        if src is not None:
            src_params = src.get("params") or {}
            if isinstance(src_params, str):
                try:
                    src_params = json.loads(src_params)
                except (json.JSONDecodeError, TypeError):
                    src_params = {}
            if user_gesture_still_in_effect(dev, src_params):
                return
    except Exception as _exc:
        logger.debug("neg-boost-powerful override lookup failed (non-fatal): %s", _exc)

    # Cadence gate — bound the Daikin 200/day quota cost. We re-assert
    # UNCONDITIONALLY (not gated on the cached powerful flag): the heartbeat
    # device cache is up to DAIKIN_DEVICES_CACHE_TTL stale, so trusting a
    # cached "powerful=on" would mask a firmware auto-clear and miss the boost
    # for the rest of the interval. A redundant "on" write is a harmless no-op
    # on the unit; the headroom gate above makes the writes taper to zero as
    # the tank nears target.
    interval_s = max(60, int(config.DHW_NEGATIVE_BOOST_POWERFUL_REASSERT_MIN_INTERVAL_MINUTES) * 60)
    # No-progress backoff (2026-07-02 window): the unit can arbitrate Powerful
    # away silently (compressor DHW ceiling on hot days) — the tank sat at
    # 50-51 °C for 5 h while we re-wrote Powerful every 15 min (24 writes,
    # ~12% of the Daikin daily quota, zero thermal gain). If the tank hasn't
    # risen ≥0.5 °C over STALL_LIMIT consecutive re-asserts, stretch the
    # interval by STALL_BACKOFF (progress resets both).
    global _NEG_BOOST_STALL_COUNT, _NEG_BOOST_LAST_TEMP
    # New-episode reset: a clearly colder tank (fresh window after a setback)
    # or a long quiet gap means the old stall verdict no longer applies.
    if _NEG_BOOST_LAST_TEMP is not None and float(tank_t) < _NEG_BOOST_LAST_TEMP - 2.0:
        _NEG_BOOST_STALL_COUNT = 0
        _NEG_BOOST_LAST_TEMP = None
    if _NEG_BOOST_POWERFUL_LAST_UTC is not None and (
        now_utc - _NEG_BOOST_POWERFUL_LAST_UTC
    ).total_seconds() > 6 * 3600:
        _NEG_BOOST_STALL_COUNT = 0
        _NEG_BOOST_LAST_TEMP = None
    stall_limit = max(1, int(config.DHW_NEGATIVE_BOOST_REASSERT_STALL_LIMIT))
    if _NEG_BOOST_STALL_COUNT >= stall_limit:
        # Clamp ≥1: a mis-set backoff of 0 would turn "stalled" into a write
        # on every heartbeat — worse than the incident this fixes.
        interval_s = int(
            interval_s * max(1.0, float(config.DHW_NEGATIVE_BOOST_REASSERT_STALL_BACKOFF))
        )
    if _NEG_BOOST_POWERFUL_LAST_UTC is not None:
        if (now_utc - _NEG_BOOST_POWERFUL_LAST_UTC).total_seconds() < interval_s:
            return
    # Stamp the cadence gate up-front (H2): a persistently-failing PATCH (e.g.
    # READ_ONLY when Powerful isn't settable) must be rate-limited to the
    # interval, not retried every heartbeat — failed calls still cost quota.
    _NEG_BOOST_POWERFUL_LAST_UTC = now_utc
    _save_neg_boost_state()
    try:
        client.set_tank_powerful(dev, True)
        dev.tank_powerful = True
        # Stall accounting — successful writes only (a failed PATCH never
        # reached the unit, so "no thermal progress" says nothing about the
        # unit arbitrating Powerful away; the H2 stamp above already rate-
        # limits failures to the base interval). The baseline is the last
        # PROGRESS point, not the last observation — a slow-but-real ramp
        # (e.g. +0.3 °C/interval) accumulates to +0.5 and resets instead of
        # being re-baselined into looking permanently stalled.
        if _NEG_BOOST_LAST_TEMP is None:
            # First write of an episode: no baseline yet — stamp only.
            _NEG_BOOST_LAST_TEMP = float(tank_t)
        elif float(tank_t) >= _NEG_BOOST_LAST_TEMP + 0.5:
            _NEG_BOOST_STALL_COUNT = 0
            _NEG_BOOST_LAST_TEMP = float(tank_t)
        else:
            _NEG_BOOST_STALL_COUNT += 1
        _save_neg_boost_state()
        db.log_action(
            device="daikin",
            action="negative_boost_powerful_reassert",
            params={"tank_temp": float(tank_t), "tank_target": float(boost_target)},
            result="success",
            trigger=trigger,
        )
        logger.info(
            "neg-boost: re-asserted DHW Powerful (tank %.0f < target %.0f °C, trigger=%s)",
            float(tank_t), float(boost_target), trigger,
        )
    except (DaikinError, ValueError) as e:
        db.log_action(
            device="daikin",
            action="negative_boost_powerful_reassert",
            params={"tank_temp": float(tank_t), "tank_target": float(boost_target)},
            result="failure",
            error_msg=str(e),
            trigger=trigger,
        )
        logger.warning("neg-boost: Powerful re-assert failed: %s", e)


def _check_lwt_offset_drift(
    actions: list[dict[str, Any]],
    client: DaikinClient,
    dev: Any,
    now_utc: datetime,
    *,
    trigger: str,
) -> None:
    """#461 — reset a stray non-zero LWT offset to 0 when nothing justifies it.

    Only acts when HEM is actually managing the offset
    (``DAIKIN_LWT_PREHEAT_ENABLED``); otherwise climate is hands-off and a
    non-zero offset is the user's/firmware's to keep. Bails (fail-safe) on:

    * feature/check flag disabled, or vacation preset;
    * live offset unknown (cache miss) or already ≈0 (no drift);
    * an active/pending ``lwt_preheat`` slot covering now carries a non-zero
      offset (the pre-heat plan justifies it);
    * a recent user override is still in effect — respect the gesture for
      ``USER_OVERRIDE_RESPECT_HOURS`` (this is what lets a *manual* offset stand
      for the grace window, then get reset once it ages out — the #461 ask).

    Dedupes via ``_LWT_DRIFT_NOTIFIED`` (one page per episode; cleared at 0).
    """
    global _LWT_DRIFT_NOTIFIED

    if not config.DAIKIN_LWT_PREHEAT_ENABLED or not config.LWT_OFFSET_DRIFT_CHECK_ENABLED:
        return
    if (config.OPTIMIZATION_PRESET or "normal").strip().lower() == "vacation":
        return
    off = getattr(dev, "lwt_offset", None)
    if off is None:
        return  # unknown live state — fail-safe
    if abs(float(off)) < 0.5:
        if _LWT_DRIFT_NOTIFIED:
            _LWT_DRIFT_NOTIFIED = False
        return

    # Does any current slot's lwt_preheat action justify a non-zero offset?
    # NOTE: status is NOT filtered — a lwt_preheat row whose window covers now
    # justifies the offset even when it's ``completed`` (it fired, or the
    # pre-fire idempotency marked it noop because the device already held the
    # value). Filtering to pending/active was a bug (#496-incident): it reset a
    # legitimate live offset to 0 the moment its row completed. The time-window
    # check below is what makes the row "current".
    for act in actions:
        try:
            start = _parse_utc(act["start_time"])
            end = _parse_utc(act["end_time"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (start <= now_utc < end):
            continue
        if act.get("overridden_by_user_at"):
            continue
        if act.get("action_type") != "lwt_preheat":
            continue
        params = act.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        po = params.get("lwt_offset")
        if po is not None and abs(float(po)) >= 0.5:
            return  # a pre-heat window justifies the non-zero offset

    # Respect a still-in-effect user gesture (grace window before reset).
    try:
        src = db.find_recent_user_override(
            device="daikin",
            within_hours=float(config.USER_OVERRIDE_RESPECT_HOURS),
            now_utc=now_utc,
            respect_until_window_end=bool(
                config.USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END
            ),
        )
        if src is not None:
            src_params = src.get("params") or {}
            if isinstance(src_params, str):
                try:
                    src_params = json.loads(src_params)
                except (json.JSONDecodeError, TypeError):
                    src_params = {}
            if user_gesture_still_in_effect(dev, src_params):
                return
    except Exception as _exc:
        logger.debug("lwt-drift override lookup failed (non-fatal): %s", _exc)

    # Drift confirmed — reset the offset to 0 (full settable/zone/read-only
    # guard is inside apply_scheduled_daikin_params).
    db.log_action(
        device="daikin",
        action="lwt_offset_drift_detected",
        params={"lwt_offset": float(off), "auto_recover": bool(config.LWT_OFFSET_DRIFT_AUTO_RECOVER)},
        result="alert",
        trigger=trigger,
    )
    recovered = False
    if (
        config.LWT_OFFSET_DRIFT_AUTO_RECOVER
        and not config.OPENCLAW_READ_ONLY
        and config.DAIKIN_CONTROL_MODE == "active"
    ):
        try:
            apply_scheduled_daikin_params(
                dev, client, {"lwt_offset": 0, "lp_optimizer": True},
                trigger=f"lwt_drift_recover:{trigger}",
            )
            recovered = True
        except (DaikinError, ValueError) as exc:
            logger.warning("lwt-offset-drift auto-recover failed: %s", exc)

    if not _LWT_DRIFT_NOTIFIED:
        _LWT_DRIFT_NOTIFIED = True
        try:
            msg = (
                f"LWT offset = {off:+.0f}°C with no plan slot justifying it — "
                f"{'reset to 0' if recovered else 'manual reset may be needed'} "
                f"(trigger={trigger})"
            )
            notify_risk(msg)
        except Exception as _exc:
            logger.debug("lwt-drift notify failed (non-fatal): %s", _exc)


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
