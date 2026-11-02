"""Read physical initial state for the PuLP MILP (battery SoC + DHW tank).

PR Phase B (#306 follow-up): ``indoor_temp_c`` removed. The Daikin Altherma
exposes no room sensor (0% heartbeat coverage observed) and the LP no longer
carries an indoor-temp decision variable — heating demand is bound by
``get_daikin_heating_kw(t_outdoor)`` directly. This file no longer reads
``room_temperature`` from Daikin.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import db
from ..config import config
from ..daikin import service as daikin_service
from ..foxess.service import get_cached_realtime, get_cached_realtime_age_seconds
from .lp_optimizer import LpInitialState
from .lp_roll_forward import (
    find_schedule_onset,
    roll_soc_with_onset,
    roll_tank_forward,
)

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    """Wall clock (monkeypatch target in tests)."""
    return datetime.now(UTC)


def read_lp_initial_state(
    daikin: Any | None = None,
    *,
    allow_daikin_refresh: bool = True,
    slot0_utc: datetime | None = None,
    now_utc: datetime | None = None,
) -> LpInitialState:
    """SoC from FoxESS cache (or DB realtime snapshot); tank from Daikin cache.

    Priority order for SoC:
    1. Live Fox realtime cache (``get_cached_realtime``) — fresh within seconds
    2. DB ``fox_realtime_snapshot`` — fresh within 15 min (set by MPC job)
    3. Config default (50% capacity)

    Daikin telemetry is sourced from the cached service (``get_cached_devices``)
    — the ``daikin`` parameter is kept for backwards-compat but no longer issues
    HTTP calls. Quota is preserved.

    ``allow_daikin_refresh=False`` forbids any cache-miss refresh that would
    burn Daikin quota. Simulation paths pass this to honor the "no quota burn"
    guarantee even when the MCP-process cache is cold.

    ``slot0_utc`` (#929): start of LP slot 0. When given and
    ``LP_INITIAL_STATE_ROLL_FORWARD_ENABLED`` is on, SoC and tank are rolled
    forward from their reading time to slot 0 (``lp_roll_forward``); the audit
    trail lands in ``LpInitialState.roll_forward``. Callers that omit it keep
    the legacy "state at read time" behaviour.
    """
    now = now_utc or _now_utc()
    roll: dict[str, Any] = {}
    soc_kwh = float(config.BATTERY_CAPACITY_KWH) * 0.5
    soc_source = "default"
    rt = None
    try:
        rt = get_cached_realtime()
        soc_kwh = float(config.BATTERY_CAPACITY_KWH) * float(rt.soc) / 100.0
        soc_source = "fox_realtime_cache"
    except Exception as e:
        logger.debug("LP initial SoC live fallback: %s", e)
        try:
            snap = db.get_fox_realtime_snapshot()
            if snap and snap.get("soc_pct") is not None:
                soc_kwh = float(config.BATTERY_CAPACITY_KWH) * float(snap["soc_pct"]) / 100.0
                soc_source = "db_realtime_snapshot"
        except Exception as e2:
            logger.debug("LP initial SoC DB snapshot fallback: %s", e2)

    logger.debug("LP initial SoC=%.2f kWh (source=%s)", soc_kwh, soc_source)

    roll_on = slot0_utc is not None and bool(
        getattr(config, "LP_INITIAL_STATE_ROLL_FORWARD_ENABLED", False)
    )
    if roll_on and soc_source == "fox_realtime_cache" and rt is not None:
        try:
            soc_kwh, roll["soc"] = _roll_soc(soc_kwh, rt, now, slot0_utc)
        except Exception as e:  # pragma: no cover - never break the solve
            logger.warning("LP SoC roll-forward failed (%s) - using reading as is", e)

    tank = float(config.DHW_TEMP_NORMAL_C)
    tank_source = "default"

    try:
        if allow_daikin_refresh:
            state = daikin_service.get_lp_state_cached_or_estimated(actor="lp_init")
        else:
            # Simulation: cache-only path; never burns Daikin quota. Uses the
            # canonical device-cache TTL (the per-caller LP_INIT knob was retired).
            result = daikin_service.get_cached_devices(
                allow_refresh=False,
                actor="lp_init",
            )
            state = {
                "tank_temp_c": result.devices[0].tank_temperature
                if result.devices
                else None,
                "source": result.source,
            }
        src = str(state.get("source") or "daikin_unknown")
        if state.get("tank_temp_c") is not None:
            tank = float(state["tank_temp_c"])
            tank_source = src
            if roll_on:
                try:
                    tank, roll["tank"] = _roll_tank(tank, now, slot0_utc)
                except Exception as e:  # pragma: no cover - never break the solve
                    logger.warning("LP tank roll-forward failed (%s) - using reading as is", e)
        logger.debug("LP Daikin seed: tank=%.1f°C source=%s", tank, state.get("source"))
    except Exception as e:
        logger.debug("LP Daikin telemetry fallback: %s", e)

    # W3 (#540): freshest room-sensor reading → t_in[0] seed. None when no fresh
    # sensor (staleness enforced by get_latest_indoor_reading); W3 stays off then.
    indoor_c: float | None = None
    try:
        s = db.get_latest_indoor_reading(
            max_age_minutes=int(getattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30))
        )
        if s is not None and s.get("temp_c") is not None:
            indoor_c = float(s["temp_c"])
    except Exception as e:  # pragma: no cover — never break the solve
        logger.debug("LP initial indoor read failed: %s", e)

    # #929: W3 silently turned itself off when the indoor reading was missing. Keep the
    # behaviour, but say so (log + snapshot via LpInitialState.w3_disabled_reason).
    w3_reason: str | None = None
    if indoor_c is None and bool(getattr(config, "LP_W3_TIN_ENABLED", False)):
        w3_reason = "no_fresh_indoor_reading"
        logger.warning(
            "LP initial state: no indoor reading within %s min - W3 (indoor-temperature "
            "model) is OFF for this solve",
            getattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30),
        )

    # #866: freshest LIVE leaving-water temperature (<= 30 min old) seeds the emitter-lag
    # state h[0] (only read by the LP when LP_W3_EMITTER_TAU_MIN > 0).
    lwt_c: float | None = None
    try:
        import time as _time

        row = db.get_latest_daikin_telemetry(source="live")
        if (
            row is not None
            and row.get("lwt_actual_c") is not None
            and row.get("fetched_at") is not None
            and _time.time() - float(row["fetched_at"]) <= 30 * 60
        ):
            lwt_c = float(row["lwt_actual_c"])
    except Exception as e:  # pragma: no cover — never break the solve
        logger.debug("LP initial LWT read failed: %s", e)

    lwt_off: float | None = None
    try:
        lwt_off = db.get_active_lwt_offset()
    except Exception as e:  # pragma: no cover — never break the solve
        logger.debug("LP initial LWT offset read failed: %s", e)

    return LpInitialState(
        soc_kwh=soc_kwh,
        tank_temp_c=tank,
        indoor_temp_c=indoor_c,
        lwt_actual_c=lwt_c,
        lwt_offset_c=lwt_off,
        soc_source=soc_source,
        tank_source=tank_source,
        roll_forward=roll or None,
        w3_disabled_reason=w3_reason,
    )


def _roll_soc(soc_kwh: float, rt: Any, now: datetime, slot0_utc: datetime) -> tuple[float, dict]:
    """SoC at slot 0 from the cached Fox realtime reading (measured battery power)."""
    cap = float(config.BATTERY_CAPACITY_KWH)
    age = get_cached_realtime_age_seconds()
    gap_s = (slot0_utc - now).total_seconds() + (age or 0.0)  # reading time -> slot 0
    power = float(rt.battery_power)  # + charging, - discharging
    reading_t = now - timedelta(seconds=age or 0.0)
    onset = None
    try:
        from zoneinfo import ZoneInfo  # group hours are local time

        st = db.get_latest_fox_schedule_state()
        if st and st.get("enabled", 1) and st.get("groups"):
            onset = find_schedule_onset(
                st["groups"], reading_t, slot0_utc, ZoneInfo(config.BULLETPROOF_TIMEZONE),
                max_power_kw=float(config.MAX_INVERTER_KW),
            )
    except Exception as e:  # pragma: no cover - schedule is an optional refinement
        logger.debug("LP SoC roll: schedule onset unavailable (%s)", e)
    res = roll_soc_with_onset(
        soc_kwh, power, age, gap_s, onset,
        capacity_kwh=cap,
        reserve_kwh=cap * float(config.MIN_SOC_RESERVE_PERCENT) / 100.0,
        max_power_kw=float(config.MAX_INVERTER_KW),
    )
    info = {
        "applied": res.applied, "reason": res.reason,
        "from_kwh": round(soc_kwh, 3), "to_kwh": round(res.soc_kwh, 3),
        "battery_power_kw": round(power, 3),
        "reading_age_s": None if age is None else round(age, 1),
        "gap_s": round(gap_s, 1),
        "schedule_onset": None if onset is None else {
            "offset_s": round(onset.offset_s, 1), "power_kw": onset.power_kw,
            "target_soc_pct": onset.target_soc_pct,
        },
    }
    return res.soc_kwh, info


def _roll_tank(tank_c: float, now: datetime, slot0_utc: datetime) -> tuple[float, dict]:
    """Tank temperature at slot 0: reheat extrapolation or (above sensor noise) standing loss.

    The reading time is the newest ``source='live'`` telemetry row whose tank temperature
    equals the seed; estimates/degraded seeds are not rolled.
    """
    from ..dhw.params import live_indoor_ambient_c, resolve_tank_params

    rows = db.get_recent_daikin_telemetry(source="live", limit=2)
    if not rows or rows[0].get("tank_temp_c") is None or abs(float(rows[0]["tank_temp_c"]) - tank_c) > 0.05:
        return tank_c, {"applied": False, "mode": "none:seed_not_live_row"}
    last = rows[0]
    reading_t = datetime.fromtimestamp(float(last["fetched_at"]), UTC)
    gap_s = (slot0_utc - reading_t).total_seconds()
    prev = rows[1] if len(rows) > 1 and rows[1].get("tank_temp_c") is not None else None
    sample_gap = None if prev is None else float(last["fetched_at"]) - float(prev["fetched_at"])
    tgt = last.get("tank_target_c")
    cap_c = float(tgt) if tgt is not None and float(tgt) > tank_c else float(config.DHW_TEMP_MAX_C)
    tp = resolve_tank_params(ambient_c=live_indoor_ambient_c())
    tank_on = last.get("tank_on")
    res = roll_tank_forward(
        tank_c, gap_s,
        prev_tank_c=None if prev is None else float(prev["tank_temp_c"]),
        sample_gap_s=sample_gap,
        tank_on=None if tank_on is None else bool(tank_on),
        target_c=cap_c,
        ua_w_per_k=tp.ua_w_per_k, ambient_c=tp.ambient_c,
        litres=tp.litres, cp_j_per_kg_k=tp.cp_j_per_kg_k,
    )
    return res.tank_c, {
        "applied": res.applied, "mode": res.mode,
        "from_c": round(tank_c, 2), "to_c": round(res.tank_c, 2),
        "gap_s": round(gap_s, 1), "cap_c": cap_c,
    }
