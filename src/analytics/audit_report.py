"""Structured daily audit — held-schedule events + plan-vs-execution.

Originally lived as ``/srv/hem/data/audit_held_schedule.py`` (host-side, not
in repo, Telegram-only). Story A1 of Epic 13a (#352) lifts the analytics
into this module so:

* MCP tools and briefs can pull the same data structurally (A2).
* The prod-side script becomes a thin markdown shim around
  :func:`build_audit_report` — behavioural diff zero.
* The logic gets test coverage instead of relying on prod data + grepping
  Telegram history to verify.

Two sections, both returned as plain ``dict`` (no markdown, no I/O beyond
SQLite):

A) **Held-schedule events** — LP runs that returned ``Infeasible`` and the
   defensive fallback from PR #338 kept the previously-uploaded schedule
   running. Each event is annotated with the closest pv_realtime + Daikin
   tank temperature so callers can split "SoC below reserve" (harmless,
   soft-handled by #339) from "SoC at/above reserve" (the residual class,
   typically shower-floor-in-slot-0 — fixed in PR #344).

B) **Plan vs execution** — for every 30 min slot in the trailing window:
   the *operative* LP plan (latest snapshot whose parent run was before
   the slot started) vs the realised pv_realtime per-slot binning. The
   audit reports coverage (LP runs → Fox uploads), totals (kWh + cost),
   and the top-N per-slot disparities by absolute cost delta. A
   robustness-filtered-export sub-section tallies battery export the LP
   planned as peak_export but the pessimistic scenario filter downgraded.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from ..config import config

DISPARITY_TOP_N = 4
DISPARITY_MIN_PENCE = 5.0
COVERAGE_MIN_PCT = 90.0

# Slot kinds that ship battery energy TO THE GRID (Fox ForceDischarge). Both map
# to the same hardware action in ``optimizer._slot_fox_tuple``. PV-surplus export
# is NOT here: it leaves passively via SelfUse and needs no discharge group.
_BATTERY_EXPORT_KINDS = frozenset({"peak_export", "pre_negative_export"})
HELD_BASELINE_PER_DAY = 110.0 / 30.0  # ≈3.67/day pre-PR #338 measured rate


# ---------------------------------------------------------------------------
# Slot helpers
# ---------------------------------------------------------------------------

def _parse_utc(s: str) -> dt.datetime:
    s = s.replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _slot_floor(t: dt.datetime) -> dt.datetime:
    """Round *t* down to the half-hour boundary (00 or 30)."""
    minute = 0 if t.minute < 30 else 30
    return t.replace(minute=minute, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Plan & realised window builders
# ---------------------------------------------------------------------------

def _operative_plan_for_window(
    db: sqlite3.Connection,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> dict[dt.datetime, dict[str, Any]]:
    """For each 30-min slot in ``[start_utc, end_utc)``, find the latest LP
    solution whose parent run was *before* the slot start — that's the plan
    actually live when the slot was dispatched. Joined to
    ``dispatch_decisions`` so callers can distinguish the LP variable's raw
    ``export_kwh`` from the EFFECTIVE export the scenario robustness filter
    actually allowed onto Fox V3."""
    out: dict[dt.datetime, dict[str, Any]] = {}
    t = start_utc
    while t < end_utc:
        row = db.execute(
            """
            SELECT s.run_id, s.import_kwh, s.export_kwh, s.charge_kwh, s.discharge_kwh,
                   s.pv_use_kwh, s.dhw_kwh, s.space_kwh, i.run_at_utc,
                   dd.lp_kind, dd.dispatched_kind, dd.reason
            FROM lp_solution_snapshot s
            JOIN lp_inputs_snapshot i ON i.run_id = s.run_id
            LEFT JOIN dispatch_decisions dd
              ON dd.run_id = s.run_id AND dd.slot_time_utc = s.slot_time_utc
            WHERE s.slot_time_utc = ? AND i.run_at_utc < ?
            ORDER BY i.run_at_utc DESC LIMIT 1
            """,
            (t.isoformat(), t.isoformat()),
        ).fetchone()
        if row:
            out[t] = dict(row)
        t += dt.timedelta(minutes=30)
    return out


def _effective_plan_export_kwh(plan_slot: dict[str, Any]) -> float:
    """Battery export kWh that ACTUALLY shipped to Fox V3 as a ForceDischarge.

    BOTH battery→grid kinds count: ``peak_export`` (arbitrage, vacation preset)
    and ``pre_negative_export`` (drain ahead of a negative window, live in
    normal/guests). ``_slot_fox_tuple`` maps them to the SAME hardware action —
    ``ForceDischarge`` to the export SoC floor — so counting only the former
    under-reported the export credit and turned every pre-negative drain into a
    phantom top-N "disparity" row in the 07:30 audit.

    The raw ``export_kwh`` column is the LP variable's value, not the dispatched
    amount. PV surplus export is deliberately NOT counted: it leaves via Fox
    SelfUse passively, not via a discharge group.
    """
    if (plan_slot.get("dispatched_kind") or "").strip() in _BATTERY_EXPORT_KINDS:
        return float(plan_slot.get("export_kwh") or 0)
    return 0.0


def _forgone_export_kwh(plan_slot: dict[str, Any]) -> float:
    """Battery export the LP planned but the robustness filter downgraded.

    ONLY counts slots the LP itself labelled ``peak_export`` (battery → grid
    arbitrage) that did not ship as one — i.e. ``filter_robust_peak_export``
    dropped it, either because the pessimistic scenario disagreed or because the
    economic margin was too thin. That is the only forgone revenue in the CURRENT
    dispatch path. (Pre-PR-C rows are not recoverable this way: under the old
    ``strict_savings`` policy the labeller itself suppressed the kind, so those
    slots carry ``lp_kind='standard'`` and read as zero here.)

    ``pre_negative_export`` is never forgone: it is labelled distinctly precisely
    to bypass the robustness filter, so it always ships.

    It must NOT count a slot's ``export_kwh`` just because the slot isn't
    ``peak_export``. In the normal/guests presets the LP constrains
    ``exp <= pv_use`` (lp_optimizer), so ``export_kwh`` there is PV SURPLUS —
    which Fox V3 SelfUse exports passively and which already earns money in
    ``export_revenue_gbp``. Counting it as "forgone" reported a phantom loss on
    every sunny day. (Pre-PR-C fossil: this used to measure what the removed
    ``ENERGY_STRATEGY_MODE=strict_savings`` policy declined to sell.)
    """
    if (plan_slot.get("lp_kind") or "").strip() != "peak_export":
        return 0.0
    if (plan_slot.get("dispatched_kind") or "").strip() == "peak_export":
        return 0.0
    return float(plan_slot.get("export_kwh") or 0)


_FORGONE_REASON_KNOB = {
    # filter_robust_peak_export's two real drop reasons → the knob that governs
    # each. Telling the user "the pessimistic scenario disagreed" when the drop
    # was actually an economic-margin call points them at the wrong dial.
    "pessimistic_disagrees": "pessimistic scenario disagreed "
                             "(<code>LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH</code>)",
    "economic_margin": "margin too thin "
                       "(<code>LP_PEAK_EXPORT_MIN_MARGIN_PENCE_PER_KWH</code>)",
    "pessimistic_failed": "pessimistic solve failed",
    "no_scenarios_run": "no scenarios ran for this solve",
}


def _forgone_reason_text(reasons: dict[str, int]) -> str:
    """Render the drop-reason histogram, most common first."""
    if not reasons:
        return "reason not recorded"
    parts = [
        f"{_FORGONE_REASON_KNOB.get(k, k)} ×{n}"
        for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])
    ]
    return "; ".join(parts)


def _realised_for_window(
    db: sqlite3.Connection,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> dict[dt.datetime, dict[str, float]]:
    """Bin ``pv_realtime_history`` rows into 30-min slots (mean power × 0.5 h
    → kWh) for each channel."""
    rows = db.execute(
        "SELECT captured_at, solar_power_kw, load_power_kw, grid_import_kw, "
        "grid_export_kw, battery_charge_kw, battery_discharge_kw "
        "FROM pv_realtime_history WHERE captured_at >= ? AND captured_at < ? "
        "ORDER BY captured_at",
        (start_utc.isoformat(), end_utc.isoformat()),
    ).fetchall()
    bins: dict[dt.datetime, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        b = _slot_floor(_parse_utc(r["captured_at"]))
        for k in (
            "solar_power_kw", "load_power_kw", "grid_import_kw",
            "grid_export_kw", "battery_charge_kw", "battery_discharge_kw",
        ):
            v = r[k]
            if v is not None:
                bins[b][k].append(v)
    out: dict[dt.datetime, dict[str, float]] = {}
    for b, d in bins.items():
        out[b] = {
            (k.replace("_kw", "_kwh")): mean(v) * 0.5
            for k, v in d.items() if v
        }
    return out


# ---------------------------------------------------------------------------
# Section A — held-schedule
# ---------------------------------------------------------------------------

def _build_held_schedule_section(
    db: sqlite3.Connection,
    *,
    now_utc: dt.datetime,
    window_hours: int,
    cap_kwh: float,
    reserve_pct: float,
) -> dict[str, Any]:
    soc_reserve_kwh = cap_kwh * reserve_pct / 100.0
    cutoff = now_utc - dt.timedelta(hours=window_hours)

    held_rows = db.execute(
        """
        SELECT id, run_at, strategy_summary
        FROM optimizer_log
        WHERE run_at >= ?
          AND strategy_summary LIKE '%held previous schedule%'
        ORDER BY run_at
        """,
        (cutoff.isoformat(),),
    ).fetchall()

    events: list[dict[str, Any]] = []
    for r in held_rows:
        run_at_iso = r["run_at"]
        soc_row = db.execute(
            "SELECT soc_pct FROM pv_realtime_history WHERE captured_at >= ? "
            "ORDER BY captured_at LIMIT 1",
            (run_at_iso,),
        ).fetchone()
        try:
            ts = dt.datetime.fromisoformat(run_at_iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
        tank_row = None
        if ts is not None:
            tank_row = db.execute(
                "SELECT fetched_at, tank_temp_c FROM daikin_telemetry "
                "WHERE fetched_at < ? ORDER BY fetched_at DESC LIMIT 1",
                (ts,),
            ).fetchone()
        soc_pct = soc_row["soc_pct"] if soc_row else None
        soc_kwh = (soc_pct / 100.0 * cap_kwh) if soc_pct is not None else None
        below = (soc_kwh is not None) and (soc_kwh < soc_reserve_kwh - 1e-3)
        events.append({
            "run_at": run_at_iso,
            "soc_pct": soc_pct,
            "soc_kwh": soc_kwh,
            "tank_temp_c": tank_row["tank_temp_c"] if tank_row else None,
            "below_reserve": below,
        })

    n_below = sum(1 for e in events if e["below_reserve"])
    n_above = sum(
        1 for e in events
        if not e["below_reserve"] and e["soc_pct"] is not None
    )
    n_unknown = sum(1 for e in events if e["soc_pct"] is None)

    return {
        "events": events,
        "total": len(events),
        "soc_below_reserve": n_below,
        "soc_above_reserve": n_above,
        "soc_unknown": n_unknown,
        "reserve_pct": reserve_pct,
        "soc_reserve_kwh": round(soc_reserve_kwh, 3),
        "battery_capacity_kwh": cap_kwh,
        "baseline_per_day": round(HELD_BASELINE_PER_DAY, 2),
    }


# ---------------------------------------------------------------------------
# Section B — plan vs execution + robustness-filtered forgone export
# ---------------------------------------------------------------------------

def _build_plan_vs_execution_section(
    db: sqlite3.Connection,
    *,
    now_utc: dt.datetime,
    window_hours: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns ``(plan_vs_execution_section, forgone_export_section)``.

    They share the same SQL pass, so we build both at once to avoid a
    duplicate plan-window scan."""
    window_start = _slot_floor(now_utc - dt.timedelta(hours=window_hours))
    plan_end = _slot_floor(now_utc)  # exclusive — only past slots scored

    # Coverage: every LP run should be paired with a Fox upload within 1 min.
    lp_runs = db.execute(
        "SELECT run_id, run_at_utc FROM lp_inputs_snapshot "
        "WHERE run_at_utc >= ? ORDER BY run_at_utc",
        (window_start.isoformat(),),
    ).fetchall()
    n_lp = len(lp_runs)
    n_paired = 0
    for r in lp_runs:
        fu = db.execute(
            "SELECT id FROM fox_schedule_state "
            "WHERE julianday(uploaded_at) BETWEEN julianday(?) - 0.0007 "
            "                                AND julianday(?) + 0.0007 "
            "LIMIT 1",
            (r["run_at_utc"], r["run_at_utc"]),
        ).fetchone()
        if fu:
            n_paired += 1
    coverage_pct = (100.0 * n_paired / n_lp) if n_lp else 100.0

    plan = _operative_plan_for_window(db, window_start, plan_end)
    real = _realised_for_window(db, window_start, plan_end)

    rate_rows = db.execute(
        "SELECT valid_from, value_inc_vat FROM agile_rates "
        "WHERE valid_from >= ? AND valid_from < ?",
        (window_start.isoformat(), plan_end.isoformat()),
    ).fetchall()
    import_p_by_slot = {_parse_utc(r["valid_from"]): r["value_inc_vat"] for r in rate_rows}
    exp_rate_rows = db.execute(
        "SELECT valid_from, value_inc_vat FROM agile_export_rates "
        "WHERE valid_from >= ? AND valid_from < ?",
        (window_start.isoformat(), plan_end.isoformat()),
    ).fetchall()
    export_p_by_slot = {_parse_utc(r["valid_from"]): r["value_inc_vat"] for r in exp_rate_rows}

    plan_imp = sum((d.get("import_kwh") or 0) for d in plan.values())
    plan_exp = sum(_effective_plan_export_kwh(d) for d in plan.values())
    real_imp = sum((d.get("grid_import_kwh") or 0) for d in real.values())
    real_exp = sum((d.get("grid_export_kwh") or 0) for d in real.values())

    plan_cost_p = sum(
        (plan[t].get("import_kwh") or 0) * (import_p_by_slot.get(t) or 0)
        - _effective_plan_export_kwh(plan[t]) * (export_p_by_slot.get(t) or 0)
        for t in plan if t in import_p_by_slot
    )
    real_cost_p = sum(
        (real[t].get("grid_import_kwh") or 0) * (import_p_by_slot.get(t) or 0)
        - (real[t].get("grid_export_kwh") or 0) * (export_p_by_slot.get(t) or 0)
        for t in real if t in import_p_by_slot
    )
    cost_delta_p = real_cost_p - plan_cost_p

    forgone_kwh = 0.0
    forgone_p = 0.0
    forgone_slots = 0
    # filter_robust_peak_export drops for TWO distinct reasons
    # (`pessimistic_disagrees` and `economic_margin`) and the reason is already on
    # the row — report it rather than asserting one and pointing the user at the
    # wrong knob.
    forgone_reasons: dict[str, int] = defaultdict(int)
    for t, slot in plan.items():
        f_kwh = _forgone_export_kwh(slot)
        if f_kwh <= 0:
            continue
        ep = export_p_by_slot.get(t) or 0
        forgone_kwh += f_kwh
        forgone_p += f_kwh * ep
        forgone_slots += 1
        forgone_reasons[(slot.get("reason") or "unknown").strip()] += 1

    disparities: list[dict[str, Any]] = []
    for t in plan:
        if t not in real or t not in import_p_by_slot:
            continue
        p = plan[t]
        r = real[t]
        ip = import_p_by_slot[t]
        ep = export_p_by_slot.get(t, 0) or 0
        eff_exp = _effective_plan_export_kwh(p)
        d_imp = (r.get("grid_import_kwh") or 0) - (p.get("import_kwh") or 0)
        d_exp = (r.get("grid_export_kwh") or 0) - eff_exp
        d_cost = d_imp * ip - d_exp * ep
        if abs(d_cost) < DISPARITY_MIN_PENCE:
            continue
        disparities.append({
            "slot_time_utc": t.isoformat(),
            "delta_cost_p": round(d_cost, 2),
            "delta_import_kwh": round(d_imp, 3),
            "delta_export_kwh": round(d_exp, 3),
            "import_price_p": round(float(ip), 2),
            "export_price_p": round(float(ep), 2),
            "plan_import_kwh": round(float(p.get("import_kwh") or 0), 3),
            "real_import_kwh": round(float(r.get("grid_import_kwh") or 0), 3),
            "plan_charge_kwh": round(float(p.get("charge_kwh") or 0), 3),
            "real_charge_kwh": round(float(r.get("battery_charge_kwh") or 0), 3),
        })
    disparities.sort(key=lambda d: abs(d["delta_cost_p"]), reverse=True)

    pve = {
        "lp_runs": n_lp,
        "paired_uploads": n_paired,
        "coverage_pct": round(coverage_pct, 1),
        "coverage_threshold_pct": COVERAGE_MIN_PCT,
        "plan_kwh": {
            "import": round(plan_imp, 2),
            "export_effective": round(plan_exp, 2),
        },
        "real_kwh": {
            "import": round(real_imp, 2),
            "export": round(real_exp, 2),
        },
        "plan_cost_p": round(plan_cost_p, 1),
        "real_cost_p": round(real_cost_p, 1),
        "cost_delta_p": round(cost_delta_p, 1),
        "disparities": disparities[:DISPARITY_TOP_N],
        "disparity_count_total": len(disparities),
        "disparity_min_pence": DISPARITY_MIN_PENCE,
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": plan_end.isoformat(),
    }
    forgone = {
        "kwh": round(forgone_kwh, 2),
        "pence": round(forgone_p, 1),
        "slot_count": forgone_slots,
        "reasons": dict(forgone_reasons),
    }
    return pve, forgone


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_audit_report(
    window_hours: int = 24,
    *,
    now_utc: dt.datetime | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the structured audit report.

    ``now_utc`` defaults to wall-clock UTC; tests inject a fixed timestamp.
    ``db_path`` defaults to ``config.DB_PATH``; tests point at a tmp DB.
    Returns a dict with sections ``held_schedule``, ``plan_vs_execution``,
    and ``forgone_export`` — plus ``window_hours`` and ``now_utc``
    on the top level so consumers can attribute the snapshot.
    """
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    if db_path is None:
        db_path = config.DB_PATH
    cap_kwh = float(config.BATTERY_CAPACITY_KWH)
    reserve_pct = float(config.MIN_SOC_RESERVE_PERCENT)

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    try:
        held = _build_held_schedule_section(
            db, now_utc=now_utc, window_hours=window_hours,
            cap_kwh=cap_kwh, reserve_pct=reserve_pct,
        )
        pve, forgone = _build_plan_vs_execution_section(
            db, now_utc=now_utc, window_hours=window_hours,
        )
    finally:
        db.close()

    return {
        "window_hours": window_hours,
        "now_utc": now_utc.isoformat(),
        "held_schedule": held,
        "plan_vs_execution": pve,
        "forgone_export": forgone,
    }


# ---------------------------------------------------------------------------
# Markdown renderer (for the Telegram-shim path)
# ---------------------------------------------------------------------------

def render_audit_markdown(
    report: dict[str, Any],
    *,
    silent_cost_delta_threshold_p: float = 30.0,
) -> str | None:
    """Render *report* as the Telegram HTML body, or ``None`` if nothing
    is notable enough to warrant a push.

    The prod cron at 07:30 UTC was deliberately silent on quiet days:
    no held-schedule events AND a small |cost delta| AND coverage above
    threshold = no message. Preserved here so the shim's behavioural
    diff vs the prior in-tree script stays zero.
    """
    held = report["held_schedule"]
    pve = report["plan_vs_execution"]
    forgone = report["forgone_export"]

    coverage_warn = pve["coverage_pct"] < pve["coverage_threshold_pct"] and pve["lp_runs"]
    silent_b = (
        abs(float(pve["cost_delta_p"])) < silent_cost_delta_threshold_p
        and not coverage_warn
    )
    if held["total"] == 0 and silent_b:
        return None

    parts: list[str] = ["<b>📊 HEM daily audit (last 24 h)</b>", ""]

    if held["total"] > 0:
        baseline = held["baseline_per_day"]
        parts.extend([
            "<b>A) Held-schedule events (24 h)</b>",
            f"  total: <b>{held['total']}</b>  (baseline ≈ {baseline:.1f}/day pre-#338)",
            f"  reserve: {held['reserve_pct']:.0f}% = {held['soc_reserve_kwh']:.2f} kWh",
            f"  SoC&lt;reserve: <b>{held['soc_below_reserve']}</b>  <i>(harmless post-#339)</i>",
            f"  SoC≥reserve: <b>{held['soc_above_reserve']}</b>  <i>(follow-up — legionella/shower suspect)</i>",
        ])
        if held["soc_unknown"]:
            parts.append(f"  SoC unknown: {held['soc_unknown']}")
        for e in held["events"][:6]:
            ts_short = e["run_at"][:16].replace("T", " ") + "Z"
            soc_s = f"{e['soc_pct']:5.1f}%" if e["soc_pct"] is not None else " --- "
            tank_s = f"{e['tank_temp_c']:4.1f}°C" if e["tank_temp_c"] is not None else "---"
            flag = "⬇" if e["below_reserve"] else ("⬆" if e["soc_pct"] is not None else "?")
            parts.append(f"  {flag} {ts_short}  SoC={soc_s}  tank={tank_s}")
        if held["total"] > 6:
            parts.append(f"  ... and {held['total'] - 6} more")
        parts.append("")

    parts.append("<b>B) Plan vs execution (24 h)</b>")
    cov_flag = " ⚠️" if coverage_warn else ""
    parts.append(
        f"  LP runs: {pve['lp_runs']}  →  paired uploads: {pve['paired_uploads']}  "
        f"({pve['coverage_pct']:.0f}% coverage{cov_flag})"
    )
    parts.append(
        f"  plan kWh:  imp={pve['plan_kwh']['import']:5.1f}  "
        f"exp={pve['plan_kwh']['export_effective']:5.1f}  (effective, post-filter)"
    )
    d_imp = pve["real_kwh"]["import"] - pve["plan_kwh"]["import"]
    d_exp = pve["real_kwh"]["export"] - pve["plan_kwh"]["export_effective"]
    parts.append(
        f"  real kWh:  imp={pve['real_kwh']['import']:5.1f}  "
        f"exp={pve['real_kwh']['export']:5.1f}  "
        f"(Δimp={d_imp:+.1f}, Δexp={d_exp:+.1f})"
    )
    parts.append(
        f"  cost (excl standing): plan={pve['plan_cost_p']:6.1f}p  "
        f"real={pve['real_cost_p']:6.1f}p  <b>Δ={pve['cost_delta_p']:+6.1f}p</b>"
    )
    if forgone["slot_count"] > 0:
        parts.append(
            f"  robustness-filtered export: "
            f"~£{forgone['pence'] / 100.0:.2f} ({forgone['kwh']:.1f} kWh over "
            f"{forgone['slot_count']} slot{'s' if forgone['slot_count'] != 1 else ''}) "
            f"— the LP planned peak_export; the robustness filter dropped it "
            f"({_forgone_reason_text(forgone.get('reasons') or {})})"
        )
    if pve["disparities"]:
        parts.append(
            f"  Top {len(pve['disparities'])} slot disparities "
            f"(|Δcost|≥{pve['disparity_min_pence']:.0f}p):"
        )
        for d in pve["disparities"]:
            t = _parse_utc(d["slot_time_utc"])
            hh = t.strftime("%H:%M")
            parts.append(
                f"    {hh}Z (ip={d['import_price_p']:5.1f}p)  Δ={d['delta_cost_p']:+5.1f}p  "
                f"imp p={d['plan_import_kwh']:.2f}/r={d['real_import_kwh']:.2f}  "
                f"chg p={d['plan_charge_kwh']:.2f}/r={d['real_charge_kwh']:.2f}"
            )
    return "\n".join(parts)
