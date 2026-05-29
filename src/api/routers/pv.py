"""PV planned-vs-realised endpoint.

``GET /api/v1/pv/today`` returns, per 30-min UTC slot for the day, the
*planned* PV (the calibrated forecast the LP plans against) and the *realised*
PV (trapezoidal roll-up of ``pv_realtime_history.solar_power_kw``), plus a
running accuracy summary over the slots already elapsed. Powers the Home
"Today's plan" overlay (planned vs realised solar). Read-only, SQLite +
forecast cache only — no cloud writes.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from ... import db
from ...config import config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pv"])

_SLOTS_PER_DAY = 48  # 30-min slots
_SLOT_MINUTES = 30


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _norm_z(iso: str) -> str:
    """Normalise any UTC ISO string to the ``...Z`` form used as the slot key."""
    try:
        return _iso_z(datetime.fromisoformat(iso.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return iso


@router.get("/api/v1/pv/today")
async def get_pv_today(date: str | None = None) -> dict[str, Any]:
    """Per-slot planned (forecast) vs realised PV for the UTC calendar day.

    Query params:
      * ``date`` — optional ``YYYY-MM-DD`` (UTC day). Defaults to today UTC.

    Response:
      * ``date`` — the UTC day rendered.
      * ``now_utc`` — server time (the boundary between realised and future).
      * ``slots[]`` — one per 30-min slot: ``{slot_utc, pv_forecast_kwh,
        pv_actual_kwh}``. ``pv_actual_kwh`` is ``null`` for future slots (and
        for past slots with no telemetry) so the UI doesn't plot a zero.
      * ``accuracy`` — over elapsed slots: ``{slots_compared, forecast_kwh,
        actual_kwh, mae_kwh, bias_kwh}`` (bias = actual − forecast; positive =
        forecast under-predicted). ``null`` when nothing has elapsed yet.
      * ``forecast_kwh_day_total`` — full-day forecast sum (kWh).
    """
    # Resolve the target UTC day (mirrors /api/v1/execution/today).
    if date:
        try:
            d = _date_from_iso(date)
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    else:
        d = datetime.now(UTC).date()

    day_start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    slot_starts = [day_start + timedelta(minutes=_SLOT_MINUTES * i) for i in range(_SLOTS_PER_DAY)]
    now = datetime.now(UTC)

    # --- Planned PV: the LP's exact forecast→kWh conversion (calibration
    # tables + W/m²→kW + 0.5h). Pass pv_scale=1.0 EXPLICITLY:
    #  * the default (None) resolves to PV_FORECAST_SCALE_FACTOR, which is 0
    #    ("auto-calibrate") on prod → would zero every slot (the flat line bug);
    #  * compute_pv_calibration_factor() would DOUBLE-apply — forecast_to_lp_inputs
    #    already folds that factor in as `flat_cal` when no cloud/hourly tables
    #    exist, so passing it again squares it (see optimizer.py _pv_scale_callable,
    #    which returns only the today-factor for exactly this reason).
    #  1.0 lets the function apply its own calibration exactly once.
    # NOTE: slots before "now" may be approximate — fetch_forecast is forward-
    # looking; overnight radiation is ~0 so the impact is negligible.
    forecast_kwh: list[float] = [0.0] * _SLOTS_PER_DAY
    try:
        from ... import weather

        fc = weather.fetch_forecast(hours=48)
        series = weather.forecast_to_lp_inputs(fc, slot_starts, pv_scale=1.0)
        pv = series.pv_kwh_per_slot
        for i in range(min(_SLOTS_PER_DAY, len(pv))):
            forecast_kwh[i] = round(float(pv[i]), 4)
    except Exception as e:  # forecast provider down / no cache — degrade gracefully
        logger.warning("pv/today: forecast unavailable (%s); planned line will be zero", e)

    # --- Realised PV: trapezoidal roll-up of pv_realtime_history (same helper
    # the PnL export valuation uses), keyed by UTC half-hour slot ISO.
    try:
        actual_map = db.half_hourly_solar_kwh_for_day(d)
    except Exception as e:
        logger.warning("pv/today: realised roll-up failed (%s)", e)
        actual_map = {}

    # --- Full-day overlays so the chart needs only this one endpoint (no
    # client-side key-matching across differently-formatted ISO strings):
    #   import price (Agile, per slot), residual load forecast (per slot),
    #   and the dispatch kind (charge/discharge classification per slot).
    price_by_start: dict[str, float] = {}
    try:
        tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
        if tariff:
            for r in db.get_rates_for_period(tariff, day_start, day_start + timedelta(days=1)):
                price_by_start[_norm_z(r["valid_from"])] = float(r["value_inc_vat"])
    except Exception as e:
        logger.warning("pv/today: import-rate lookup failed (%s)", e)

    load_profile: dict[tuple[int, int], float] = {}
    try:
        load_profile = db.half_hourly_residual_load_profile_kwh()
    except Exception as e:
        logger.warning("pv/today: load profile failed (%s)", e)
    try:
        tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    except Exception:
        tz = UTC  # type: ignore[assignment]

    kind_by: dict[str, str] = {}
    try:
        rid = db.find_latest_optimizer_run_id()
        if rid is not None:
            for dec in db.get_dispatch_decisions(rid):
                k = dec.get("dispatched_kind") or dec.get("lp_kind")
                stu = dec.get("slot_time_utc")
                if k and stu:
                    kind_by[_norm_z(stu)] = k
    except Exception as e:
        logger.warning("pv/today: dispatch kinds failed (%s)", e)

    # --- Heating plan: the deterministic dhw_policy tank trajectory the LP
    # pins to (warmup rise during the day, setback fall after the evening
    # showers). tank_target_c[i] = predicted tank °C at the START of slot i;
    # dhw_load_kwh[i] = predicted heat-pump electric draw for DHW that slot.
    # Lets the Home "Today's plan" overlay the heating plan without a 2nd call.
    tank_c_per_slot: list[float | None] = [None] * _SLOTS_PER_DAY
    dhw_kwh_per_slot: list[float | None] = [None] * _SLOTS_PER_DAY
    try:
        from ... import dhw_policy

        e_dhw, tank_traj = dhw_policy.forecast_dhw_load_per_slot(slot_starts)
        for i in range(_SLOTS_PER_DAY):
            if i < len(tank_traj):
                tank_c_per_slot[i] = round(float(tank_traj[i]), 2)
            if i < len(e_dhw):
                dhw_kwh_per_slot[i] = round(float(e_dhw[i]), 4)
    except Exception as e:  # vacation mode / policy off → no heating-plan line
        logger.warning("pv/today: dhw policy forecast unavailable (%s)", e)

    slots_out: list[dict[str, Any]] = []
    acc_f = acc_a = 0.0
    acc_abs = 0.0
    compared = 0
    for i, st in enumerate(slot_starts):
        key = _iso_z(st)
        f = forecast_kwh[i]
        slot_end = st + timedelta(minutes=_SLOT_MINUTES)
        elapsed = slot_end <= now
        # Realised only exists for elapsed slots with telemetry; future slots
        # (and gaps) stay null so the chart draws a clean planned-only line.
        a_raw = actual_map.get(key)
        a: float | None = round(float(a_raw), 4) if (elapsed and a_raw is not None) else None
        local = st.astimezone(tz)
        bl = load_profile.get((local.hour, local.minute))
        slots_out.append({
            "slot_utc": key,
            "pv_forecast_kwh": f,
            "pv_actual_kwh": a,
            "import_price_p": price_by_start.get(key),
            "base_load_kwh": round(float(bl), 4) if bl is not None else None,
            "kind": kind_by.get(key),
            "tank_target_c": tank_c_per_slot[i],
            "dhw_load_kwh": dhw_kwh_per_slot[i],
        })
        if elapsed and a is not None:
            compared += 1
            acc_f += f
            acc_a += a
            acc_abs += abs(a - f)

    accuracy: dict[str, Any] | None = None
    if compared > 0:
        accuracy = {
            "slots_compared": compared,
            "forecast_kwh": round(acc_f, 3),
            "actual_kwh": round(acc_a, 3),
            "mae_kwh": round(acc_abs / compared, 4),
            "bias_kwh": round(acc_a - acc_f, 3),  # +ve = forecast under-predicted
        }

    return {
        "date": d.isoformat(),
        "now_utc": _iso_z(now),
        "slots": slots_out,
        "accuracy": accuracy,
        "forecast_kwh_day_total": round(sum(forecast_kwh), 3),
    }


def _date_from_iso(s: str) -> date:
    return date.fromisoformat(s)
