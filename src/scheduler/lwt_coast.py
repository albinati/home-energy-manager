"""LWT coast mode support (#838): comfort floor, real-time comfort backstop and
the planned half of the per-slot learning log.

* ``comfort_floor_c`` mirrors the LP's three-level W3 floor (night / peak band /
  setpoint) so the heartbeat judges the live house against the SAME floor the
  plan was solved with.
* ``backstop_tick`` is the live safety net for ``DAIKIN_LWT_COAST_MODE=lp``: when
  a negative ``lwt_preheat`` row is active and the house is under the floor
  (minus a margin) for N consecutive ticks, write offset 0 NOW and replan.
* ``record_planned`` upserts the planned fields into ``lwt_learning_log``; the
  nightly ``analytics.lwt_learning`` job fills the realised fields.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config, cop_at_temperature

logger = logging.getLogger(__name__)

_backstop_ticks: int = 0


def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def _is_night(local_hour: int) -> bool:
    ns = int(getattr(config, "LP_W3_NIGHT_START_HOUR_LOCAL", 22))
    ne = int(getattr(config, "LP_W3_NIGHT_END_HOUR_LOCAL", 7))
    return (local_hour >= ns or local_hour < ne) if ns > ne else (ns <= local_hour < ne)


def comfort_floor_c(
    at_utc: datetime,
    in_peak: bool,
    *,
    night_floor_c: float | None = None,
    setpoint_c: float | None = None,
    peak_delta_c: float | None = None,
) -> float:
    """The W3 comfort floor at ``at_utc``: night floor inside the LP night
    window, ``setpoint − LP_W3_PEAK_COAST_DELTA_C`` in the PEAK band, else the
    setpoint. Same rule as ``lp_optimizer._w3_floor`` (slot-centre local hour)."""
    nf = float(night_floor_c if night_floor_c is not None else getattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5))
    sp = float(setpoint_c if setpoint_c is not None else config.INDOOR_SETPOINT_C)
    pd = float(peak_delta_c if peak_delta_c is not None else getattr(config, "LP_W3_PEAK_COAST_DELTA_C", 1.0))
    if _is_night(at_utc.astimezone(_tz()).hour):
        return nf
    if in_peak:
        return sp - pd
    return sp


# ---------------------------------------------------------------- coast target
def plan_indoor_at(plan: Any, i: int) -> float | None:
    """Predicted indoor over slot ``i`` (mean of the slot's start/end states)."""
    traj = list(getattr(plan, "indoor_temp_c", None) or [])
    if i + 1 < len(traj):
        return (float(traj[i]) + float(traj[i + 1])) / 2.0
    if i < len(traj):
        return float(traj[i])
    return None


def coast_target(
    plan: Any, i: int, live_indoor_c: float | None = None,
) -> dict[str, float | int | None]:
    """Physics-based coast target for slot ``i`` (#838, ``DAIKIN_LWT_COAST_MODE=lp``).

    Water just above the predicted room temperature cannot add heat, so the
    compressor stays off: ``coast_lwt = indoor_pred + DAIKIN_LWT_COAST_DELTA_C``;
    ``offset = round(coast_lwt - curve_lwt)`` (half away from zero) clamped to
    ``[DAIKIN_LWT_LP_OFFSET_MIN, 0]`` (a coast slot never boosts). Indoor falls
    back to the live reading; with neither, ``offset`` is ``None`` (caller uses
    the setback value). ``curve_lwt`` = the weather-curve LWT at the forecast
    outdoor temperature (``physics.get_lwt_base_c``)."""
    from ..physics import get_lwt_base_c

    delta = float(getattr(config, "DAIKIN_LWT_COAST_DELTA_C", 2.0))
    out_c = plan.temp_outdoor_c[i] if i < len(plan.temp_outdoor_c) else None
    curve = None
    if out_c is not None and math.isfinite(float(out_c)):
        curve = float(get_lwt_base_c(float(out_c)))
    indoor = plan_indoor_at(plan, i)
    if indoor is None:
        indoor = live_indoor_c
    res: dict[str, float | int | None] = {
        "curve_lwt_c": curve, "coast_target_lwt_c": None, "coast_delta_c": delta, "offset": None,
    }
    if curve is None or indoor is None:
        return res
    target = float(indoor) + delta
    res["coast_target_lwt_c"] = round(target, 2)
    lo = int(max(-10.0, float(getattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -5))))
    x = target - curve
    off = -int(math.floor(-x + 0.5))
    res["offset"] = max(lo, min(0, off))
    return res


# ---------------------------------------------------------------- planned log
def record_planned(
    plan: Any,
    *,
    source_used: str,
    coast_mode: str,
    written_offsets: list[int | None] | None,
    lp_raw_ok: bool = True,
) -> int:
    """Upsert the horizon's planned fields into ``lwt_learning_log``.

    ``written_offsets`` = the per-slot offsets AFTER smoothing that the chosen
    source will write (``None`` entry = no write; whole list ``None`` when the
    demand gate suppressed every write). Never raises."""
    try:
        if not bool(getattr(config, "LWT_LEARNING_ENABLED", True)):
            return 0
        n = len(plan.slot_starts_utc)
        if n == 0:
            return 0
        run_id = None
        try:
            run_id = db.find_latest_optimizer_run_id()
        except Exception:
            run_id = None
        traj = list(plan.indoor_temp_c or [])
        bands = list(plan.price_band or [])
        curve = config.DAIKIN_COP_CURVE
        rows: list[dict[str, Any]] = []
        for i in range(n):
            st = plan.slot_starts_utc[i]
            band = bands[i] if i < len(bands) else None
            floor = None
            pred = None
            if traj:
                floor = comfort_floor_c(
                    st + timedelta(minutes=15), band == "peak",
                    night_floor_c=plan.w3_night_floor_c, setpoint_c=plan.w3_setpoint_c,
                    peak_delta_c=plan.w3_peak_coast_delta_c,
                )
                if i + 1 < len(traj):
                    pred = (float(traj[i]) + float(traj[i + 1])) / 2.0
                elif i < len(traj):
                    pred = float(traj[i])
            out_c = float(plan.temp_outdoor_c[i]) if i < len(plan.temp_outdoor_c) else None
            cop = None
            if out_c is not None and math.isfinite(out_c):
                try:
                    cop = round(max(1.0, cop_at_temperature(curve, out_c)), 3)
                except Exception:
                    cop = None
            ct = coast_target(plan, i) if (
                i < len(plan.space_electric_kwh) and float(plan.space_electric_kwh[i]) <= 1e-6
            ) else {}
            wo = None
            if written_offsets is not None and i < len(written_offsets):
                wo = written_offsets[i]
            rows.append({
                "slot_time_utc": st.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "run_id": run_id,
                "source": source_used,
                "coast_mode": coast_mode,
                "offset_lp_raw": float(plan.lwt_offset_c[i]) if i < len(plan.lwt_offset_c) else None,
                "offset_written": float(wo) if wo is not None else None,
                "indoor_pred_c": round(pred, 3) if pred is not None else None,
                "floor_c": floor,
                "margin_c": round(pred - floor, 3) if (pred is not None and floor is not None) else None,
                "outdoor_fc_c": out_c if (out_c is not None and math.isfinite(out_c)) else None,
                "e_space_kwh": float(plan.space_electric_kwh[i]) if i < len(plan.space_electric_kwh) else None,
                "cop_space": cop,
                "price_band": band,
                "curve_lwt_c": ct.get("curve_lwt_c"),
                "coast_target_lwt_c": ct.get("coast_target_lwt_c"),
                "coast_delta_c": ct.get("coast_delta_c"),
            })
        return db.upsert_lwt_learning_planned(rows)
    except Exception:  # telemetry must never break dispatch
        logger.debug("lwt_learning planned record failed", exc_info=True)
        return 0


# ------------------------------------------------------------------ backstop
def _active_negative_row(plan_date: str, now_utc: datetime) -> dict[str, Any] | None:
    try:
        d0 = datetime.fromisoformat(plan_date).date()
    except ValueError:
        d0 = now_utc.astimezone(_tz()).date()
    best: dict[str, Any] | None = None
    for d in (d0, d0 - timedelta(days=1), d0 + timedelta(days=1)):
        for act in db.get_actions_for_plan_date(d.isoformat(), device="daikin"):
            if act.get("action_type") != "lwt_preheat" or act.get("status") != "active":
                continue
            if act.get("overridden_by_user_at"):
                continue
            try:
                s = datetime.fromisoformat(str(act["start_time"]).replace("Z", "+00:00"))
                e = datetime.fromisoformat(str(act["end_time"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if not (s <= now_utc < e):
                continue
            off = (act.get("params") or {}).get("lwt_offset")
            if off is None or float(off) >= 0:
                continue
            if best is None or str(act["start_time"]) > str(best["start_time"]):
                best = act
    return best


def reset_backstop() -> None:
    global _backstop_ticks
    _backstop_ticks = 0


def backstop_tick(
    *,
    now_utc: datetime,
    plan_date: str,
    dev: Any,
    client: Any,
    in_peak: bool,
    replan_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One heartbeat evaluation of the comfort backstop. Never raises."""
    global _backstop_ticks
    out: dict[str, Any] = {"active_row": None, "ticks": 0, "fired": False}
    try:
        if not bool(getattr(config, "LWT_COMFORT_BACKSTOP_ENABLED", True)):
            return out
        if str(getattr(config, "DAIKIN_CONTROL_MODE", "passive")) != "active" or bool(
            getattr(config, "OPENCLAW_READ_ONLY", False)
        ):
            _backstop_ticks = 0
            return out
        row = _active_negative_row(plan_date, now_utc)
        if row is None:
            _backstop_ticks = 0
            return out
        out["active_row"] = int(row["id"])
        reading = db.get_latest_indoor_reading(
            max_age_minutes=int(getattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30))
        )
        if reading is None or reading.get("temp_c") is None:
            _backstop_ticks = 0
            return out
        indoor = float(reading["temp_c"])
        floor = comfort_floor_c(now_utc, in_peak)
        margin = float(getattr(config, "LWT_COMFORT_BACKSTOP_MARGIN_C", 0.5))
        out.update(indoor=indoor, floor=floor, margin=margin)
        if indoor >= floor - margin:
            _backstop_ticks = 0
            return out
        _backstop_ticks += 1
        out["ticks"] = _backstop_ticks
        if _backstop_ticks < max(1, int(getattr(config, "LWT_COMFORT_BACKSTOP_TICKS", 2))):
            return out

        # --- fire: restore offset 0 now through the normal apply path --------
        from ..daikin.client import DaikinError
        from ..daikin_bulletproof import apply_scheduled_daikin_params

        offset_was = float((row.get("params") or {}).get("lwt_offset"))
        try:
            apply_scheduled_daikin_params(dev, client, {"lwt_offset": 0}, trigger="lwt_backstop")
        except (DaikinError, ValueError) as e:
            logger.warning("lwt comfort backstop write failed: %s", e)
            db.log_action(
                device="daikin", action="lwt_comfort_backstop",
                params={"indoor": indoor, "floor": floor, "margin": margin,
                        "offset_was": offset_was, "row_id": int(row["id"])},
                result="failure", trigger="heartbeat", error_msg=str(e),
            )
            return out  # ticks stay armed → retried next tick
        db.mark_action(int(row["id"]), "completed", error_msg="comfort_backstop")
        _backstop_ticks = 0
        out["fired"] = True

        db.log_action(
            device="daikin", action="lwt_comfort_backstop",
            params={"indoor": indoor, "floor": floor, "margin": margin,
                    "offset_was": offset_was, "row_id": int(row["id"])},
            result="ok", trigger="heartbeat",
        )
        try:
            st = datetime.fromisoformat(str(row["start_time"]).replace("Z", "+00:00")).astimezone(_tz())
            key = f"lwt_backstop_{st:%Y-%m-%d_%H%M}"
            if not db.is_warning_acknowledged(key):
                from ..notifier import notify_risk

                notify_risk(
                    f"House at {indoor:.1f} C, under the {floor:.1f} C comfort floor "
                    f"(margin {margin:.1f}): LWT offset {offset_was:+.0f} cancelled, "
                    "heating restored and plan re-solved.",
                    extra={"warning_key": key},
                )
                db.acknowledge_warning(key)
        except Exception as e:
            logger.debug("lwt backstop notify failed: %s", e)
        replanned: bool | None = None
        if replan_fn is not None:
            try:
                replanned = bool(replan_fn(
                    force_write_devices=True, trigger_reason="lwt_backstop", bypass_cooldown=True,
                ))
            except Exception as e:
                logger.warning("lwt backstop replan failed: %s", e)
                replanned = False
        out["replanned"] = replanned
    except Exception as e:  # the backstop must never break the heartbeat
        logger.warning("lwt comfort backstop error: %s", e, exc_info=True)
    return out
