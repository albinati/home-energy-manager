"""PV planned-vs-realised endpoint.

``GET /api/v1/pv/today`` returns, per 30-min UTC slot for the day, the
*planned* PV (the calibrated forecast the LP plans against) and the *realised*
PV (trapezoidal roll-up of ``pv_realtime_history.solar_power_kw``), plus a
running accuracy summary over the slots already elapsed. Powers the Home
"Today's plan" overlay (planned vs realised solar). Read-only, SQLite +
forecast cache only — no cloud writes.
"""
from __future__ import annotations

import asyncio
import logging
import time
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


# In-process TTL cache for the committed-load stitch: /pv/today is polled every
# 5 min by the cockpit and the stitch joins the day's lp snapshots per request.
# A past UTC day's snapshots are immutable → long TTL; today keeps a short TTL
# so a fresh solve surfaces within a poll cycle. Keyed by DB_PATH so isolated
# test databases never share entries.
_LOAD_STITCH_TTL_TODAY_S = 240.0
_LOAD_STITCH_TTL_PAST_S = 3600.0
_load_stitch_cache: dict[str, tuple[float, dict[str, tuple[float, float]]]] = {}


def _committed_load_by_slot_cached(d: date, today: date) -> dict[str, tuple[float, float]]:
    key = f"{config.DB_PATH}:{d.isoformat()}"
    ttl = _LOAD_STITCH_TTL_PAST_S if d < today else _LOAD_STITCH_TTL_TODAY_S
    hit = _load_stitch_cache.get(key)
    mono = time.monotonic()
    if hit is not None and mono - hit[0] < ttl:
        return hit[1]
    out: dict[str, tuple[float, float]] = {}
    for stu, (lt, lb) in db.committed_load_forecast_by_slot(d).items():
        out[_norm_z(stu)] = (float(lt), float(lb))
    if len(_load_stitch_cache) > 64:  # bound: one entry per viewed day
        _load_stitch_cache.clear()
    _load_stitch_cache[key] = (mono, out)
    return out


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

        # Open-Meteo HTTP — offload so it doesn't block the event loop and
        # serialize the other dashboard requests behind it. TTL-cached so the
        # cockpit's /weather + /pv/today share one fetch.
        fc = await asyncio.to_thread(weather.fetch_forecast_cached, hours=48)
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

    load_prof: dict[str, Any] | None = None
    try:
        load_prof = db.residual_load_profile_v2()
    except Exception as e:
        logger.warning("pv/today: load profile failed (%s)", e)

    # Committed-plan LOAD: the per-slot household-load forecast the LP actually
    # committed to, stitched across the day's solves exactly like the PV path
    # below. ``total`` = base + dhw + space (comparable to the measured
    # total-demand stack); ``base`` = the LP's base_load_json input — the
    # residual forecast PLUS planned appliance dispatch and any scale/bias
    # correctors (optimizer.py folds appliance kWh in before persisting), so
    # don't build residual-error math on it. Without this block, past days
    # rendered TODAY's static dow×hour profile as "Forecast" — a shape that
    # never matches what was planned on that day (the "forecast history
    # disappears when navigating back" report). TTL-cached + off the event
    # loop: the cockpit polls this endpoint every 5 min.
    committed_load_by: dict[str, tuple[float, float]] = {}
    try:
        committed_load_by = await asyncio.to_thread(
            _committed_load_by_slot_cached, d, now.date()
        )
    except Exception as e:
        logger.warning("pv/today: committed-plan load lookup failed (%s)", e)
    try:
        tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    except Exception:
        tz = UTC  # type: ignore[assignment]

    # The run that governs this day: the last solve committed at/over the day
    # (for a past day) or the latest solve (today). Drives both the dispatch
    # kinds and the committed-plan PV line.
    rid: int | None = None
    try:
        when = _iso_z(min(day_start + timedelta(days=1), now))
        rid = db.find_run_for_time(when) or db.find_latest_optimizer_run_id()
    except Exception as e:
        logger.warning("pv/today: run lookup failed (%s)", e)

    kind_by: dict[str, str] = {}
    try:
        if rid is not None:
            for dec in db.get_dispatch_decisions(rid):
                k = dec.get("dispatched_kind") or dec.get("lp_kind")
                stu = dec.get("slot_time_utc")
                if k and stu:
                    kind_by[_norm_z(stu)] = k
    except Exception as e:
        logger.warning("pv/today: dispatch kinds failed (%s)", e)

    # Committed-plan PV: the per-slot PV-generation forecast the LP committed to,
    # STITCHED across every solve of the day (#462). A single snapshot only
    # covers run_at→horizon, so by evening the morning slots would be missing —
    # the stitch back-fills each elapsed slot with the forecast that was current
    # when it began, and resolves future slots to the latest plan. This is what
    # gives the accuracy block a real "expected by now" baseline (the live
    # forecast above is forward-only → 0 for elapsed slots). slot_time_utc is
    # stored in +00:00 form, so normalise to the ...Z key via _norm_z.
    planned_by: dict[str, float] = {}
    plan_committed_at: str | None = None
    try:
        for stu, pf in db.committed_pv_forecast_by_slot(d).items():
            planned_by[_norm_z(stu)] = float(pf)
        if rid is not None:
            inp = db.get_lp_inputs(rid)
            if inp and inp.get("run_at_utc"):
                plan_committed_at = _norm_z(inp["run_at_utc"])
    except Exception as e:
        logger.warning("pv/today: committed-plan PV lookup failed (%s)", e)

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
        committed = committed_load_by.get(key)
        # base_load_kwh: prefer the committed residual for this slot (true
        # history, frozen at solve time); fall back to the live profile only
        # where no solve covered the slot (pre-logging days, gaps).
        if committed is not None:
            bl: float | None = committed[1]
        else:
            bl = (
                db.lookup_residual_kwh(load_prof, local.weekday(), local.hour, 30 if local.minute >= 30 else 0)
                if load_prof is not None else None
            )
        slots_out.append({
            "slot_utc": key,
            "pv_forecast_kwh": f,  # live forecast (revises through the day)
            "pv_planned_kwh": planned_by.get(key),  # committed plan (frozen since last solve)
            "pv_actual_kwh": a,
            "import_price_p": price_by_start.get(key),
            "base_load_kwh": round(float(bl), 4) if bl is not None else None,
            # committed TOTAL household load (base + dhw + space) — what the
            # Consumption chart's "Forecast" line should draw against the
            # total-demand stack. Null where no solve covered the slot.
            "load_forecast_kwh": round(committed[0], 4) if committed is not None else None,
            "kind": kind_by.get(key),
        })
        if elapsed and a is not None:
            # Compare against the COMMITTED plan (frozen at solve time, stitched
            # so elapsed slots have a real value). The live forecast `f` is
            # forward-only → 0 for elapsed slots, which is why "expected by now"
            # used to collapse to 0 by evening (#462). Fall back to `f` only if a
            # slot has no committed forecast at all.
            base = planned_by.get(key)
            if base is None:
                base = f
            compared += 1
            acc_f += base
            acc_a += a
            acc_abs += abs(a - base)

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
        "plan_committed_at": plan_committed_at,
        "plan_run_id": rid,
    }


@router.get("/api/v1/grid/today")
async def get_grid_today(date: str | None = None) -> dict[str, Any]:
    """Per-slot planned-vs-realised GRID import/export for the UTC day.

    The Home "Grid" widget's data: for each 30-min UTC slot, the *committed
    plan's* grid import/export (stitched across the day's solves, like the PV
    line) and the *realised* import/export (trapezoidal roll-up of
    ``pv_realtime_history.grid_import_kw`` / ``grid_export_kw``). Closes the gap
    where ``/execution/today`` carried load but no grid traffic.

    Query params:
      * ``date`` — optional ``YYYY-MM-DD`` (UTC day). Defaults to today UTC.

    Response mirrors ``/pv/today``:
      * ``slots[]`` — ``{slot_utc, import_planned_kwh, export_planned_kwh,
        import_actual_kwh, export_actual_kwh, import_price_p, kind}``. Actuals
        are ``null`` for future slots (and elapsed slots with no telemetry) so
        the chart draws a clean planned-only line ahead of now.
      * ``totals`` — day sums for planned + (elapsed-so-far) realised.
      * ``now_utc``, ``plan_run_id``.
    """
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

    # --- Committed plan: stitched per-slot grid import/export from the LP
    # snapshots (same stitch the PV line uses, so elapsed slots keep the plan
    # that was current when they began rather than a hole). Keys are stored
    # (+00:00) form → normalise to the ...Z slot key.
    plan_imp: dict[str, float] = {}
    plan_exp: dict[str, float] = {}
    try:
        plan_imp = {_norm_z(k): float(v) for k, v in db.committed_lp_field_by_slot(d, "import_kwh").items()}
        plan_exp = {_norm_z(k): float(v) for k, v in db.committed_lp_field_by_slot(d, "export_kwh").items()}
    except Exception as e:
        logger.warning("grid/today: committed-plan grid lookup failed (%s)", e)

    # --- Realised import/export: trapezoidal roll-up of pv_realtime_history
    # (the same helpers the PnL engine costs against). Keys are ...Z form.
    act_imp: dict[str, float] = {}
    act_exp: dict[str, float] = {}
    act_dis: dict[str, float] = {}
    try:
        act_imp = {_norm_z(k): float(v) for k, v in db.half_hourly_grid_import_kwh_for_day(d).items()}
        act_exp = {_norm_z(k): float(v) for k, v in db.half_hourly_grid_export_kwh_for_day(d).items()}
        # Battery discharge — for the Consumption "by source" view (how much of
        # the load the battery covered vs the grid).
        act_dis = {_norm_z(k): float(v) for k, v in db.half_hourly_battery_discharge_kwh_for_day(d).items()}
    except Exception as e:
        logger.warning("grid/today: realised grid roll-up failed (%s)", e)

    # --- Import price + dispatch kind overlays (one endpoint feeds the chart).
    price_by_start: dict[str, float] = {}
    try:
        tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
        if tariff:
            for r in db.get_rates_for_period(tariff, day_start, day_start + timedelta(days=1)):
                price_by_start[_norm_z(r["valid_from"])] = float(r["value_inc_vat"])
    except Exception as e:
        logger.warning("grid/today: import-rate lookup failed (%s)", e)

    rid: int | None = None
    kind_by: dict[str, str] = {}
    try:
        when = _iso_z(min(day_start + timedelta(days=1), now))
        rid = db.find_run_for_time(when) or db.find_latest_optimizer_run_id()
        if rid is not None:
            for dec in db.get_dispatch_decisions(rid):
                k = dec.get("dispatched_kind") or dec.get("lp_kind")
                stu = dec.get("slot_time_utc")
                if k and stu:
                    kind_by[_norm_z(stu)] = k
    except Exception as e:
        logger.warning("grid/today: run/kind lookup failed (%s)", e)

    slots_out: list[dict[str, Any]] = []
    t_pimp = t_pexp = t_aimp = t_aexp = 0.0
    for st in slot_starts:
        key = _iso_z(st)
        slot_end = st + timedelta(minutes=_SLOT_MINUTES)
        elapsed = slot_end <= now
        pi = plan_imp.get(key)
        pe = plan_exp.get(key)
        ai_raw = act_imp.get(key)
        ae_raw = act_exp.get(key)
        ad_raw = act_dis.get(key)
        ai = round(float(ai_raw), 4) if (elapsed and ai_raw is not None) else None
        ae = round(float(ae_raw), 4) if (elapsed and ae_raw is not None) else None
        ad = round(float(ad_raw), 4) if (elapsed and ad_raw is not None) else None
        slots_out.append({
            "slot_utc": key,
            "import_planned_kwh": round(pi, 4) if pi is not None else None,
            "export_planned_kwh": round(pe, 4) if pe is not None else None,
            "import_actual_kwh": ai,
            "export_actual_kwh": ae,
            "discharge_actual_kwh": ad,
            "import_price_p": price_by_start.get(key),
            "kind": kind_by.get(key),
        })
        if pi is not None:
            t_pimp += pi
        if pe is not None:
            t_pexp += pe
        if ai is not None:
            t_aimp += ai
        if ae is not None:
            t_aexp += ae

    return {
        "date": d.isoformat(),
        "now_utc": _iso_z(now),
        "slots": slots_out,
        "totals": {
            "import_planned_kwh": round(t_pimp, 3),
            "export_planned_kwh": round(t_pexp, 3),
            "import_actual_kwh": round(t_aimp, 3),
            "export_actual_kwh": round(t_aexp, 3),
        },
        "plan_run_id": rid,
    }


def _date_from_iso(s: str) -> date:
    return date.fromisoformat(s)


@router.get("/api/v1/load/residual-profile")
async def get_residual_load_profile(
    window_days: int | None = None, end_date: str | None = None
) -> dict[str, Any]:
    """The learned household residual-load profile the LP plans against (#477).

    Returns the per-(day-of-week, half-hour) median + p75 spread (resolved
    through the same fallback hierarchy the LP uses), the plain (h,m) baseline,
    the excluded "away" days, and coverage stats (how many days were calibrated
    against the measured Daikin split). Read-only, viewer-safe.

    ``end_date`` (YYYY-MM-DD, local, inclusive) anchors the trailing window at a
    past day so the Insights navigator can scope the heatmap to an earlier
    period; omitted → the window ends now (#574 item 3).
    """
    # Clamp the caller-supplied window so a bad/huge value can't trigger a
    # full-table scan; None uses the configured default (the LP's window).
    if window_days is not None:
        window_days = max(1, min(int(window_days), 365))
    prof = await asyncio.to_thread(
        db.residual_load_profile_v2, window_days=window_days, end_date=end_date
    )

    def _series(dow: int) -> list[dict[str, Any]]:
        out = []
        for h in range(24):
            for m in (0, 30):
                out.append({
                    "h": h, "m": m,
                    "median": round(db.lookup_residual_kwh(prof, dow, h, m), 4),
                    "p75": round(db.lookup_residual_spread_kwh(prof, dow, h, m), 4),
                })
        return out

    def _hp_series(dow: int) -> list[dict[str, Any]]:
        return [
            {"h": h, "m": m, "median": round(db.lookup_hp_kwh(prof, dow, h, m), 4)}
            for h in range(24) for m in (0, 30)
        ]

    def _hp_component_series(dow: int, component: str) -> list[dict[str, Any]]:
        return [
            {"h": h, "m": m,
             "median": round(db.lookup_hp_component_kwh(prof, dow, h, m, component=component), 4)}
            for h in range(24) for m in (0, 30)
        ]

    p = prof.get("profile", {})
    all_series = [
        {"h": h, "m": m,
         "median": round(float(p.get((h, m), prof.get("flat", 0.0))), 4),
         "p75": round(float(prof.get("spread", {}).get((h, m), 0.0)), 4)}
        for h in range(24) for m in (0, 30)
    ]
    return {
        "by_dow": {str(d): _series(d) for d in range(7)},
        # Heat-pump (Daikin) split — the load the residual profile subtracts.
        "hp_by_dow": {str(d): _hp_series(d) for d in range(7)},
        # TANK (DHW) vs HEATING (space) breakdown of the heat-pump load, from the
        # measured Onecta meters (#574 item 2).
        "hp_dhw_by_dow": {str(d): _hp_component_series(d, "hp_dhw_profile") for d in range(7)},
        "hp_space_by_dow": {str(d): _hp_component_series(d, "hp_space_profile") for d in range(7)},
        "all": all_series,
        "window_days": window_days,
        "end_date": end_date,
        "flat": round(float(prof.get("flat", 0.0)), 4),
        "away_days": prof.get("away_days", []),
        "day_counts": prof.get("day_counts", {}),
        "calibrated_days": prof.get("calibrated_days", 0),
        "physics_only_days": prof.get("physics_only_days", 0),
    }


@router.get("/api/v1/forecast/daily")
async def get_forecast_daily(start_date: str, end_date: str) -> dict[str, Any]:
    """Per-LOCAL-day committed forecast vs actual sums for load AND solar,
    from the persisted error logs (load_error_log / pv_error_log, both rebuilt
    by the ~04:2x UTC nightly crons). Powers the forecast overlay on the
    week/month/year Consumption + Generation charts — the aggregated views
    used to render actuals only (#624 item 1). A day missing from a log
    (pre-logging history, or today before the rebuild) simply has nulls for
    that side. Read-only, viewer-safe.

    ``start_date``/``end_date`` — YYYY-MM-DD, LOCAL (Europe/London), inclusive.
    """
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    try:
        s_local = datetime.fromisoformat(str(start_date)).replace(tzinfo=tz)
        e_local = datetime.fromisoformat(str(end_date)).replace(tzinfo=tz) + timedelta(days=1)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="start_date/end_date must be YYYY-MM-DD")
    n_days = (e_local.date() - s_local.date()).days
    if n_days < 1 or n_days > 400:
        raise HTTPException(status_code=400, detail="range must be 1..400 days")
    start = s_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = e_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    load_rows = await asyncio.to_thread(db.get_load_error_log_range, start, end)
    pv_rows = await asyncio.to_thread(db.get_pv_error_log_range, start, end)

    def _bucket(rows: list[dict[str, Any]], f_key: str, a_key: str,
                days: dict[str, dict[str, Any]], prefix: str) -> None:
        for r in rows:
            try:
                ts = datetime.fromisoformat(str(r["slot_time_utc"]).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            dloc = ts.astimezone(tz).date().isoformat()
            d = days.setdefault(dloc, {})
            f, a = r.get(f_key), r.get(a_key)
            if f is not None:
                d[f"{prefix}_forecast_kwh"] = d.get(f"{prefix}_forecast_kwh", 0.0) + float(f)
                d[f"{prefix}_n"] = d.get(f"{prefix}_n", 0) + 1
            if a is not None:
                d[f"{prefix}_actual_kwh"] = d.get(f"{prefix}_actual_kwh", 0.0) + float(a)

    days: dict[str, dict[str, Any]] = {}
    _bucket(load_rows, "forecast_kwh", "actual_kwh", days, "load")
    _bucket(pv_rows, "forecast_kwh", "actual_kwh", days, "pv")

    out = []
    for dloc in sorted(days):
        d = days[dloc]
        out.append({
            "date": dloc,
            "load_forecast_kwh": round(d["load_forecast_kwh"], 3) if "load_forecast_kwh" in d else None,
            "load_actual_kwh": round(d["load_actual_kwh"], 3) if "load_actual_kwh" in d else None,
            "load_n_slots": d.get("load_n", 0),
            "pv_forecast_kwh": round(d["pv_forecast_kwh"], 3) if "pv_forecast_kwh" in d else None,
            "pv_actual_kwh": round(d["pv_actual_kwh"], 3) if "pv_actual_kwh" in d else None,
            "pv_n_slots": d.get("pv_n", 0),
        })
    return {"start_date": str(start_date), "end_date": str(end_date), "days": out}


@router.get("/api/v1/load/error-log")
async def get_load_error_log(
    window_days: int = 30, start_date: str | None = None, end_date: str | None = None
) -> dict[str, Any]:
    """Committed LOAD-forecast-vs-actual summary from the persisted load_error_log
    (Phase-1 measurement). Overall MAE/bias + per-LOCAL-hour bias (load is
    occupancy-driven, so local hour is the meaningful axis — and the axis a
    future recent-bias corrector would act on). Read-only, viewer-safe.

    With ``start_date``/``end_date`` (YYYY-MM-DD, local, inclusive) the summary is
    scoped to that exact range so the Insights navigator can step day/week/month/
    year (#574 item 3); otherwise it's the trailing ``window_days`` from now.
    """
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    if start_date and end_date:
        try:
            s_local = datetime.fromisoformat(str(start_date)).replace(tzinfo=tz)
            e_local = datetime.fromisoformat(str(end_date)).replace(tzinfo=tz) + timedelta(days=1)
            start = s_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = e_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            window_days = max(1, (e_local.date() - s_local.date()).days)
        except (ValueError, TypeError):
            start_date = end_date = None
    if not (start_date and end_date):
        window_days = max(1, min(int(window_days), 365))
        now = datetime.now(UTC)
        start = (now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = await asyncio.to_thread(db.get_load_error_log_range, start, end)

    paired: list[tuple[float, float]] = []  # (forecast_total, actual)
    per_hour: dict[int, list[tuple[float, float]]] = {}
    for r in rows:
        f = r.get("forecast_kwh")
        a = r.get("actual_kwh")
        if f is None or a is None:
            continue
        try:
            ts = datetime.fromisoformat(str(r["slot_time_utc"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        lh = ts.astimezone(tz).hour
        paired.append((float(f), float(a)))
        per_hour.setdefault(lh, []).append((float(f), float(a)))

    def _stats(pairs: list[tuple[float, float]]) -> dict[str, Any]:
        n = len(pairs)
        if n == 0:
            return {"n": 0, "mae_kwh": 0.0, "bias_kwh": 0.0, "mean_forecast_kwh": 0.0, "mean_actual_kwh": 0.0}
        errs = [a - f for f, a in pairs]
        return {
            "n": n,
            "mae_kwh": round(sum(abs(e) for e in errs) / n, 4),
            "bias_kwh": round(sum(errs) / n, 4),  # +ve = actual > forecast (under-forecast)
            "mean_forecast_kwh": round(sum(f for f, _ in pairs) / n, 4),
            "mean_actual_kwh": round(sum(a for _, a in pairs) / n, 4),
        }

    return {
        "window_days": window_days,
        "n_slots_logged": len(rows),
        "overall": _stats(paired),
        "per_hour_local": {str(h): _stats(per_hour[h]) for h in sorted(per_hour)},
    }


@router.get("/api/v1/load/error-log/backtest")
async def get_load_bias_backtest(window_days: int | None = None) -> dict[str, Any]:
    """Offline what-if for the Phase-2 load-bias corrector: would the additive
    per-local-hour correction have reduced the error over the persisted
    load_error_log? Returns MAE/bias before vs after + per-hour corrections.
    Read-only — never writes, never touches the LP. The gate for enabling.
    """
    from ... import load_bias

    if window_days is not None:
        window_days = max(1, min(int(window_days), 365))
    return await asyncio.to_thread(load_bias.backtest_load_recent_bias, window_days)
