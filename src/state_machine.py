"""Fail-safe defaults, boot recovery, and action validation (Bulletproof engine)."""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db
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


def _is_tank_action(params: dict[str, Any]) -> bool:
    """True when a row commands the DHW tank (vs. an LWT/space-heating row).

    Legionella stand-off only suppresses tank writes; LWT rows still fire.
    """
    if not isinstance(params, dict):
        return False
    return any(k in params for k in ("tank_temp", "tank_power", "tank_powerful"))


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
    # #461 — LWT-offset drift: when HEM owns the offset (pre-heat enabled) but
    # the live device holds a non-zero offset no plan slot justifies, reset it
    # to 0 once the user's grace window has passed. Catches a manual offset with
    # no paired restore.
    _check_lwt_offset_drift(actions, client, dev, now_utc, trigger=trigger)
    # 2026-06-28 — sustain DHW Powerful through a negative-price boost window.
    # The boost row fires once at window start; Daikin auto-clears Powerful,
    # leaving the tank coasting for the rest of a paid window. Re-assert it.
    _check_negative_boost_powerful(actions, client, dev, now_utc, trigger=trigger)
    # PR J diverter removed 2026-05-23 (K2-cleanup) — superseded by K1's
    # dhw_policy fixed schedule. The diverter's "lift tank during PV
    # abundance" goal is now redundant: tank lives at NORMAL=45 °C via
    # dhw_policy, and PV excess goes to the battery (where it has the
    # higher economic value anyway).


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
