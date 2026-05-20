"""LP scorecard — per-day evaluation of how well the LP optimised against reality.

The LP runs N times per day (event-driven MPC). For any day D it produces a
sequence of plans, each based on the inputs available at that solve time:
PV forecast, outdoor temperature forecast, household load profile, current
SoC, tank temp, Octopus prices. This module evaluates, for day D:

1. **Forecast accuracy** — how good were the LP's INPUTS?
   * PV forecast MAE + bias (from forecast_skill_log)
   * Outdoor temp forecast MAE (from forecast_skill_log)
   * Load forecast MAE per slot (from forecast_skill_log)

2. **Dispatch accuracy** — did the OUTPUTS happen as planned?
   * Per-slot operative plan (from lp_solution_snapshot, most-recent solve
     before each slot start) vs realised (from pv_realtime_history rollup).
   * Import / export / charge totals planned vs real.

3. **Economic value** — was the LP cheaper than a naive baseline?
   * lp_realised_cost_p: what the household actually paid (from
     compute_daily_pnl — net of standing + export revenue).
   * naive_self_use_shadow_p: what plain SelfUse + Agile-as-published
     would have cost (battery covers load opportunistically, no LP
     pre-planning, no peak_export). Computed from per-slot real load
     × per-slot Agile rate, with SoC trajectory simulated under SelfUse.
   * lp_avoided_cost_p: positive means LP saved money vs naive.

4. **Composite grade** — single A/B/C/D letter combining dispatch accuracy
   + economic value, so the user sees one glance signal.

All three feed structured fields, NOT markdown — the brief / MCP /
REST endpoint each compose their own rendering. The scorecard never
calls the Daikin API (read-only over DB).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from .pnl import compute_daily_pnl

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SlotKwh:
    """One slot's planned + realised key kWh + cost (in pence)."""
    slot_time_utc: str
    plan_import_kwh: float
    plan_export_kwh: float
    plan_charge_kwh: float
    real_import_kwh: float
    real_export_kwh: float
    real_charge_kwh: float
    import_price_p: float
    export_price_p: float


def _operative_plan_for_window(
    db_conn: sqlite3.Connection,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[datetime, dict[str, Any]]:
    """For each 30-min slot in [start, end), pick the latest LP run whose
    parent ``run_at_utc`` is BEFORE the slot start (i.e. the plan that was
    LIVE when the slot fired). Mirrors the audit script's logic.
    """
    out: dict[datetime, dict[str, Any]] = {}
    t = start_utc
    while t < end_utc:
        row = db_conn.execute(
            """SELECT s.run_id, s.import_kwh, s.export_kwh, s.charge_kwh,
                      s.discharge_kwh, s.pv_use_kwh,
                      i.run_at_utc, dd.dispatched_kind
               FROM lp_solution_snapshot s
               JOIN lp_inputs_snapshot i ON i.run_id = s.run_id
               LEFT JOIN dispatch_decisions dd
                 ON dd.run_id = s.run_id AND dd.slot_time_utc = s.slot_time_utc
               WHERE s.slot_time_utc = ? AND i.run_at_utc < ?
               ORDER BY i.run_at_utc DESC LIMIT 1""",
            (t.isoformat(), t.isoformat()),
        ).fetchone()
        if row:
            out[t] = dict(row)
        t += timedelta(minutes=30)
    return out


def _realised_for_window(
    db_conn: sqlite3.Connection,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[datetime, dict[str, float]]:
    """Bin pv_realtime_history into 30-min slot averages → kWh (mean kW × 0.5)."""
    from collections import defaultdict
    from statistics import mean

    rows = db_conn.execute(
        """SELECT captured_at, solar_power_kw, load_power_kw, grid_import_kw,
                  grid_export_kw, battery_charge_kw, battery_discharge_kw
           FROM pv_realtime_history
           WHERE captured_at >= ? AND captured_at < ?
           ORDER BY captured_at""",
        (start_utc.isoformat(), end_utc.isoformat()),
    ).fetchall()

    bins: dict[datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        ts = datetime.fromisoformat(r["captured_at"].replace("Z", "+00:00")).astimezone(UTC)
        minute = 0 if ts.minute < 30 else 30
        slot = ts.replace(minute=minute, second=0, microsecond=0)
        for k in ("solar_power_kw", "load_power_kw", "grid_import_kw",
                  "grid_export_kw", "battery_charge_kw", "battery_discharge_kw"):
            v = r[k]
            if v is not None:
                bins[slot][k].append(v)

    out: dict[datetime, dict[str, float]] = {}
    for slot, samples in bins.items():
        out[slot] = {
            k.replace("_kw", "_kwh"): (mean(vs) * 0.5)
            for k, vs in samples.items() if vs
        }
    return out


def _effective_plan_export(plan_row: dict[str, Any]) -> float:
    """Strict_savings / scenario filter may downgrade LP export → standard.
    The dispatched_kind in dispatch_decisions tells us what actually shipped."""
    if (plan_row.get("dispatched_kind") or "").strip() == "peak_export":
        return float(plan_row.get("export_kwh") or 0)
    return 0.0


def _forecast_accuracy_section(
    day: date,
) -> dict[str, Any]:
    """PV + outdoor + load forecast skill for the day (from forecast_skill_log)."""
    try:
        rows = db.get_forecast_skill_rows(day.isoformat(), day.isoformat())
    except Exception as e:
        logger.warning("lp_scorecard: forecast_skill_log read failed: %s", e)
        return {"available": False}
    if not rows:
        return {"available": False}

    def _stats(diffs: list[float]) -> tuple[float, float] | None:
        if not diffs:
            return None
        mae = sum(abs(d) for d in diffs) / len(diffs)
        bias = sum(diffs) / len(diffs)
        return round(mae, 3), round(bias, 3)

    pv_diffs = [
        float(r["predicted_pv_kwh"]) - float(r["actual_pv_kwh"])
        for r in rows
        if r.get("predicted_pv_kwh") is not None and r.get("actual_pv_kwh") is not None
    ]
    temp_diffs = [
        float(r["predicted_temp_c"]) - float(r["actual_temp_c"])
        for r in rows
        if r.get("predicted_temp_c") is not None and r.get("actual_temp_c") is not None
    ]
    load_diffs = [
        float(r["predicted_load_kwh"]) - float(r["actual_load_kwh"])
        for r in rows
        if r.get("predicted_load_kwh") is not None and r.get("actual_load_kwh") is not None
    ]

    out: dict[str, Any] = {"available": True, "n_hours": len(rows)}
    pv = _stats(pv_diffs)
    if pv:
        out["pv_kwh_mae"] = pv[0]
        out["pv_kwh_bias"] = pv[1]  # positive = over-forecast
    temp = _stats(temp_diffs)
    if temp:
        out["outdoor_temp_c_mae"] = temp[0]
        out["outdoor_temp_c_bias"] = temp[1]
    load = _stats(load_diffs)
    if load:
        out["load_kwh_mae"] = load[0]
        out["load_kwh_bias"] = load[1]
    return out


def _dispatch_accuracy_section(
    db_conn: sqlite3.Connection,
    day: date,
    tz: ZoneInfo,
) -> dict[str, Any]:
    """Per-slot operative plan vs realised, aggregated for the day."""
    local_start = datetime.combine(day, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(UTC)
    utc_end = local_end.astimezone(UTC)

    plan = _operative_plan_for_window(db_conn, utc_start, utc_end)
    real = _realised_for_window(db_conn, utc_start, utc_end)

    plan_import = sum(float(p.get("import_kwh") or 0) for p in plan.values())
    plan_export = sum(_effective_plan_export(p) for p in plan.values())
    plan_charge = sum(float(p.get("charge_kwh") or 0) for p in plan.values())

    real_import = sum(float(r.get("grid_import_kwh") or 0) for r in real.values())
    real_export = sum(float(r.get("grid_export_kwh") or 0) for r in real.values())
    real_charge = sum(float(r.get("battery_charge_kwh") or 0) for r in real.values())

    def _accuracy_pct(planned: float, real_val: float) -> float | None:
        """100 % when real == planned, drops as they diverge. Returns None
        when planned is ~0 (denominator instability)."""
        if planned <= 0.01:
            return None
        gap = abs(planned - real_val) / planned
        return round(max(0.0, 100.0 * (1.0 - gap)), 1)

    return {
        "n_slots_with_plan": len(plan),
        "n_slots_with_real": len(real),
        "import_planned_kwh": round(plan_import, 2),
        "import_real_kwh": round(real_import, 2),
        "import_accuracy_pct": _accuracy_pct(plan_import, real_import),
        "export_planned_kwh": round(plan_export, 2),
        "export_real_kwh": round(real_export, 2),
        "export_accuracy_pct": _accuracy_pct(plan_export, real_export),
        "charge_planned_kwh": round(plan_charge, 2),
        "charge_real_kwh": round(real_charge, 2),
        "charge_accuracy_pct": _accuracy_pct(plan_charge, real_charge),
    }


def _naive_self_use_shadow_pence(
    db_conn: sqlite3.Connection,
    day: date,
    tz: ZoneInfo,
) -> float | None:
    """Pence the household would have paid under PLAIN SelfUse (no LP):
    battery covers load opportunistically (charge when PV > load, discharge
    when load > PV); grid import only when battery hits reserve; no
    peak_export, no overnight pre-charge, no anticipation. Computed
    per-slot from realised load + PV + per-slot Agile rate.

    Simulation uses SoC starting from the actual day-start SoC (or 5 kWh
    default if unavailable), reserve = MIN_SOC_RESERVE_PERCENT, capacity
    = BATTERY_CAPACITY_KWH, round-trip efficiency = BATTERY_RT_EFFICIENCY.

    Returns None when realised data is too sparse (< 20 slots).
    """
    local_start = datetime.combine(day, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(UTC)
    utc_end = local_end.astimezone(UTC)

    real = _realised_for_window(db_conn, utc_start, utc_end)
    if len(real) < 20:
        return None

    # Pull per-slot Agile import rates for the day.
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return None
    rate_rows = db_conn.execute(
        "SELECT valid_from, value_inc_vat FROM agile_rates "
        "WHERE valid_from >= ? AND valid_from < ? AND tariff_code = ?",
        (utc_start.isoformat(), utc_end.isoformat(), tariff),
    ).fetchall()
    rates: dict[datetime, float] = {}
    for r in rate_rows:
        try:
            t = datetime.fromisoformat(r["valid_from"].replace("Z", "+00:00")).astimezone(UTC)
            rates[t] = float(r["value_inc_vat"])
        except (ValueError, KeyError, TypeError):
            continue
    if not rates:
        return None

    cap = float(config.BATTERY_CAPACITY_KWH)
    reserve = cap * float(config.MIN_SOC_RESERVE_PERCENT) / 100.0
    eta_sqrt = (float(config.BATTERY_RT_EFFICIENCY) ** 0.5)
    soc = cap * 0.5  # start mid-charge; sim is comparative, not absolute

    total_p = 0.0
    for slot_utc in sorted(real.keys()):
        if slot_utc not in rates:
            continue
        bin_data = real[slot_utc]
        load = float(bin_data.get("load_power_kwh") or 0)  # mean kW × 0.5h = kWh
        pv = float(bin_data.get("solar_power_kwh") or 0)
        # SelfUse logic: PV serves load first; surplus charges battery up
        # to cap; deficit drawn from battery down to reserve; grid covers
        # the remainder.
        net = pv - load
        if net >= 0:
            # PV surplus → charge (capped)
            chg_room = max(0.0, cap - soc)
            chg = min(net * eta_sqrt, chg_room)
            soc += chg
            # Anything beyond chg_room exports (no revenue in naive model)
            # — we're shadowing imports only.
            grid_imp = 0.0
        else:
            need = -net
            avail = max(0.0, soc - reserve)
            dis = min(need * eta_sqrt, avail)
            soc -= dis
            grid_imp = need - (dis / eta_sqrt if eta_sqrt > 0 else dis)
        total_p += max(0.0, grid_imp) * rates[slot_utc]
    return round(total_p, 1)


def _compute_grade(
    dispatch: dict[str, Any],
    economic: dict[str, Any],
) -> str:
    """Composite grade A/B/C/D based on dispatch accuracy + value-add.

    A — accuracy ≥ 90 % and lp_avoided ≥ 0
    B — accuracy ≥ 75 % and lp_avoided ≥ 0
    C — accuracy ≥ 60 % OR lp_avoided ≥ 0
    D — accuracy < 60 % AND lp_avoided < 0

    "Accuracy" here = average of the per-channel pcts that are not None.
    """
    pcts = [
        dispatch.get(k) for k in
        ("import_accuracy_pct", "export_accuracy_pct", "charge_accuracy_pct")
        if dispatch.get(k) is not None
    ]
    if not pcts:
        return "N/A"
    avg_acc = sum(pcts) / len(pcts)
    avoided = economic.get("lp_avoided_cost_p")
    avoided_ok = avoided is not None and avoided >= 0
    if avg_acc >= 90 and avoided_ok:
        return "A"
    if avg_acc >= 75 and avoided_ok:
        return "B"
    if avg_acc >= 60 or avoided_ok:
        return "C"
    return "D"


def build_lp_scorecard(day: date) -> dict[str, Any]:
    """Per-day evaluation of LP optimisation quality.

    Returns a dict shaped for MCP / brief / REST consumption. Each section
    independently None-able when its data source is sparse:
    ``forecast_accuracy``, ``dispatch_accuracy``, ``economic_value``,
    ``grade`` (composite), ``day`` (echo).

    Pure read over DB; no Daikin API call, no LP solve. Safe to call
    mid-day or from any process with DB access.
    """
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)

    with db._lock:
        conn = db.get_connection()
        try:
            dispatch_section = _dispatch_accuracy_section(conn, day, tz)
            naive_p = _naive_self_use_shadow_pence(conn, day, tz)
        finally:
            conn.close()

    forecast_section = _forecast_accuracy_section(day)

    economic_section: dict[str, Any] = {}
    try:
        pnl = compute_daily_pnl(day)
    except Exception as e:
        logger.warning("lp_scorecard: compute_daily_pnl failed: %s", e)
        pnl = None
    if pnl:
        # The LP's realised cost = realised_net_cost_gbp × 100 (pence).
        net_gbp = pnl.get("realised_net_cost_gbp")
        if net_gbp is None:
            net_gbp = pnl.get("realised_cost_gbp")
        if net_gbp is not None:
            lp_cost_p = round(float(net_gbp) * 100.0, 1)
            economic_section["lp_realised_cost_p"] = lp_cost_p
            if naive_p is not None:
                # Naive doesn't include standing or export; remove them
                # from the LP side too to compare apples-to-apples.
                lp_import_p = (
                    (float(pnl.get("import_cost_gbp") or 0)) * 100.0
                )
                economic_section["naive_self_use_shadow_p"] = naive_p
                economic_section["lp_avoided_cost_p"] = round(naive_p - lp_import_p, 1)
                economic_section["comparison_basis"] = (
                    "import-only (naive excludes standing + export; lp_realised_cost_p "
                    "is the full real-money figure for reference)"
                )

    return {
        "day": day.isoformat(),
        "forecast_accuracy": forecast_section,
        "dispatch_accuracy": dispatch_section,
        "economic_value": economic_section,
        "grade": _compute_grade(dispatch_section, economic_section),
    }
