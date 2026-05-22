"""System health audit — Level 1 (manual one-shot, prototype for L2 MCP tool).

Aggregates the day's behavior across all HEM subsystems into a single
structured dict + human summary. Designed so the dict shape can be
lifted into ``src/analytics/system_health.py:compute_system_health(date)``
later without rework.

Usage (on the prod host):

    scp scripts/system_health.py root@prod:/srv/hem/data/
    ssh root@prod 'docker exec hem python /app/data/system_health.py'

    # Or for a specific date:
    ssh root@prod 'docker exec hem python /app/data/system_health.py 2026-05-23'
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")
from src.config import config  # noqa: E402
from src import db  # noqa: E402

TZ_LOCAL = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    """Convert a local-TZ calendar date to UTC start/end timestamps."""
    start_local = datetime(d.year, d.month, d.day, 0, 0, tzinfo=TZ_LOCAL)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Sub-system collectors
# ---------------------------------------------------------------------------


def collect_diverter(start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
    """PR J — PV diverter activity for the day."""
    conn = _connect()
    try:
        rows = list(conn.execute(
            """SELECT timestamp, action, params FROM action_log
               WHERE device = 'daikin'
                 AND action LIKE 'pv_diverter%'
                 AND timestamp >= ? AND timestamp < ?
               ORDER BY id ASC""",
            (start_utc.isoformat(), end_utc.isoformat()),
        ))
        activates = []
        deactivates = []
        for r in rows:
            params = json.loads(r["params"] or "{}")
            entry = {
                "at": r["timestamp"],
                "export_kw": params.get("export_kw"),
                "soc_pct": params.get("soc_pct"),
                "tank_target_after_c": params.get("tank_target_after_c"),
            }
            if r["action"] == "pv_diverter_activated":
                activates.append(entry)
            elif r["action"] == "pv_diverter_deactivated":
                deactivates.append(entry)

        # Compute time spent diverting (cumulative)
        time_diverting_min = 0.0
        for i, a in enumerate(activates):
            # Pair with the next deactivate after this activate
            a_ts = datetime.fromisoformat(a["at"])
            for d_ent in deactivates:
                d_ts = datetime.fromisoformat(d_ent["at"])
                if d_ts > a_ts:
                    time_diverting_min += (d_ts - a_ts).total_seconds() / 60
                    break
            else:
                # Still diverting at end of day
                end_or_now = min(end_utc, datetime.now(UTC))
                if end_or_now > a_ts:
                    time_diverting_min += (end_or_now - a_ts).total_seconds() / 60

        # Daikin writes triggered by the diverter
        write_rows = list(conn.execute(
            """SELECT COUNT(*) FROM action_log
               WHERE device = 'daikin'
                 AND action = 'scheduled_apply'
                 AND trigger LIKE 'pv_diverter_%'
                 AND timestamp >= ? AND timestamp < ?""",
            (start_utc.isoformat(), end_utc.isoformat()),
        ))
        diverter_writes = int(write_rows[0][0]) if write_rows else 0

        # Tank peak temp during diverting (Daikin telemetry)
        tank_peak_c = None
        if activates and deactivates:
            try:
                tank_rows = list(conn.execute(
                    """SELECT MAX(tank_temperature_c) FROM daikin_telemetry
                       WHERE timestamp >= ? AND timestamp < ?""",
                    (activates[0]["at"], deactivates[-1]["at"]),
                ))
                tank_peak_c = float(tank_rows[0][0]) if tank_rows and tank_rows[0][0] else None
            except sqlite3.OperationalError:
                pass  # table might be named differently

        return {
            "transitions_count": len(activates) + len(deactivates),
            "activates": activates,
            "deactivates": deactivates,
            "time_diverting_min": round(time_diverting_min, 1),
            "daikin_writes_from_diverter": diverter_writes,
            "tank_peak_c_during_diverting": tank_peak_c,
        }
    finally:
        conn.close()


def collect_lp(start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
    """LP solver health. Joins ``optimizer_log`` (run-level) with
    ``lp_inputs_snapshot`` (per-run lp_status). Missing status rows are
    treated as ``unknown`` so the count is honest, not silently dropped."""
    conn = _connect()
    try:
        runs = list(conn.execute(
            """SELECT o.id, o.run_at, lis.lp_status
               FROM optimizer_log o
               LEFT JOIN lp_inputs_snapshot lis ON o.id = lis.run_id
               WHERE o.run_at >= ? AND o.run_at < ?
               ORDER BY o.id DESC""",
            (start_utc.isoformat(), end_utc.isoformat()),
        ))
        by_status: dict[str, int] = {}
        infeasible_ids: list[int] = []
        for r in runs:
            s = (r["lp_status"] or "unknown").lower()
            by_status[s] = by_status.get(s, 0) + 1
            if s == "infeasible":
                infeasible_ids.append(int(r["id"]))
        return {
            "runs_total": len(runs),
            "runs_by_status": by_status,
            "runs_infeasible": len(infeasible_ids),
            "infeasible_run_ids": infeasible_ids,
            "last_run_at": runs[0]["run_at"] if runs else None,
        }
    finally:
        conn.close()


def collect_dispatch(start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
    """Dispatch decisions written today (slot-level commitments by the LP)."""
    conn = _connect()
    try:
        rows = list(conn.execute(
            """SELECT committed, COUNT(*) AS n
               FROM dispatch_decisions
               WHERE slot_time_utc >= ? AND slot_time_utc < ?
               GROUP BY committed""",
            (start_utc.isoformat(), end_utc.isoformat()),
        ))
        kinds = {r["committed"]: int(r["n"]) for r in rows}
        # Count user-overridden action_schedule rows
        overrides = list(conn.execute(
            """SELECT COUNT(*) FROM action_schedule
               WHERE overridden_by_user_at >= ? AND overridden_by_user_at < ?""",
            (start_utc.isoformat(), end_utc.isoformat()),
        ))
        return {
            "decisions_by_kind": kinds,
            "decisions_total": sum(kinds.values()),
            "user_overrides_today": int(overrides[0][0]) if overrides else 0,
        }
    finally:
        conn.close()


def collect_quota() -> dict[str, Any]:
    """Daikin + Fox quota usage (current rolling 24h)."""
    try:
        from src.api_quota import get_quota_status
        return get_quota_status()
    except Exception as e:
        return {"error": str(e)}


def collect_errors(start_utc: datetime, end_utc: datetime) -> dict[str, Any]:
    """Errors and alerts from action_log."""
    conn = _connect()
    try:
        rows = list(conn.execute(
            """SELECT action, result, COUNT(*) AS n
               FROM action_log
               WHERE timestamp >= ? AND timestamp < ?
                 AND result IN ('error', 'alert', 'failed')
               GROUP BY action, result
               ORDER BY n DESC""",
            (start_utc.isoformat(), end_utc.isoformat()),
        ))
        return {
            "events": [dict(r) for r in rows],
            "total_error_count": sum(int(r["n"]) for r in rows
                                     if r["result"] in ("error", "failed")),
            "total_alert_count": sum(int(r["n"]) for r in rows
                                     if r["result"] == "alert"),
        }
    finally:
        conn.close()


def collect_savings(d: date) -> dict[str, Any]:
    """PnL summary — leverages existing analytics."""
    try:
        from src.analytics.pnl import compute_daily_pnl
        result = compute_daily_pnl(d.isoformat())
        # Trim to the structured fields
        keys = (
            "realised_net_cost_gbp", "realised_import_gbp",
            "export_revenue_gbp", "export_kwh",
            "svt_shadow_gbp", "fixed_shadow_gbp",
            "delta_vs_svt_real_gbp", "delta_vs_fixed_real_gbp",
            "fixed_tariff_label", "fixed_tariff_shadow_gbp",
            "delta_vs_fixed_tariff_real_gbp",
            "kwh", "standing_charge_gbp",
        )
        return {k: result.get(k) for k in keys}
    except Exception as e:
        return {"error": str(e)}


def collect_diverter_state_live() -> dict[str, Any]:
    """Snapshot of the diverter state machine right now (live)."""
    try:
        from src import state_machine as sm
        return {
            "state": sm._DIVERTER_STATE,
            "activate_count": sm._DIVERTER_ACTIVATE_COUNT,
            "deactivate_count": sm._DIVERTER_DEACTIVATE_COUNT,
            "lockout_ticks_left": sm._DIVERTER_LOCKOUT_TICKS_LEFT,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Aggregator (the future MCP tool entrypoint)
# ---------------------------------------------------------------------------


def compute_system_health(target_date: date) -> dict[str, Any]:
    """Aggregate all subsystem health metrics for a given local-TZ date.

    THIS IS THE FUNCTION THAT WILL MOVE TO src/analytics/system_health.py
    in L2. The shape returned here is the public contract.
    """
    start_utc, end_utc = _day_bounds_utc(target_date)
    return {
        "date": target_date.isoformat(),
        "tz": str(TZ_LOCAL),
        "window_utc": {
            "start": start_utc.isoformat(),
            "end": end_utc.isoformat(),
        },
        "diverter": collect_diverter(start_utc, end_utc),
        "diverter_live": collect_diverter_state_live(),
        "lp": collect_lp(start_utc, end_utc),
        "dispatch": collect_dispatch(start_utc, end_utc),
        "quota": collect_quota(),
        "errors": collect_errors(start_utc, end_utc),
        "savings": collect_savings(target_date),
    }


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------


def render_summary(health: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"=== HEM system health — {health['date']} ({health['tz']}) ===")
    lines.append("")

    # Diverter
    d = health["diverter"]
    d_live = health["diverter_live"]
    lines.append(f"☀️  DIVERTER (state now: {d_live.get('state', '?')})")
    if d["transitions_count"] == 0:
        lines.append("    No transitions today.")
    else:
        for a in d["activates"]:
            lines.append(f"    ACTIVATE @ {a['at'][11:19]}Z  "
                         f"export={a['export_kw']}kW  SoC={a['soc_pct']}%  "
                         f"→ tank={a['tank_target_after_c']}°C")
        for d2 in d["deactivates"]:
            lines.append(f"    DEACTIV  @ {d2['at'][11:19]}Z  "
                         f"export={d2['export_kw']}kW  SoC={d2['soc_pct']}%  "
                         f"→ tank={d2['tank_target_after_c']}°C")
        lines.append(f"    Time diverting:  {d['time_diverting_min']:.0f} min")
        lines.append(f"    Daikin writes:   {d['daikin_writes_from_diverter']} (target ≤ 4/day)")
        if d["tank_peak_c_during_diverting"]:
            lines.append(f"    Tank peak temp:  {d['tank_peak_c_during_diverting']:.1f}°C")
    lines.append("")

    # LP
    lp = health["lp"]
    lines.append(f"🧮 LP")
    lines.append(f"    Runs:        {lp['runs_total']}  {dict(lp['runs_by_status'])}")
    if lp["runs_infeasible"] > 0:
        lines.append(f"    ⚠️  Infeasible run IDs: {lp['infeasible_run_ids']}")
    lines.append("")

    # Dispatch
    disp = health["dispatch"]
    lines.append(f"📋 DISPATCH")
    lines.append(f"    Decisions:   {disp['decisions_total']}  {dict(disp['decisions_by_kind'])}")
    lines.append(f"    Overrides:   {disp['user_overrides_today']} user gestures")
    lines.append("")

    # Quota
    q = health["quota"]
    if "daikin" in q:
        dk = q["daikin"]
        used_pct = (dk["quota_used_24h"] / dk["daily_budget"] * 100) if dk["daily_budget"] else 0
        lines.append(f"📡 QUOTA")
        lines.append(f"    Daikin: {dk['quota_used_24h']:>3}/{dk['daily_budget']} ({used_pct:.0f}%) "
                     f"{'⚠️  BLOCKED' if dk['blocked'] else 'ok'}")
        if "fox" in q:
            fx = q["fox"]
            fx_pct = (fx["quota_used_24h"] / fx["daily_budget"] * 100) if fx["daily_budget"] else 0
            lines.append(f"    Fox:    {fx['quota_used_24h']:>3}/{fx['daily_budget']} ({fx_pct:.0f}%) "
                         f"{'⚠️  BLOCKED' if fx['blocked'] else 'ok'}")
    lines.append("")

    # Savings
    s = health["savings"]
    if "realised_net_cost_gbp" in s:
        lines.append(f"💰 SAVINGS (net, incl standing charge)")
        lines.append(f"    Realised:    £{s['realised_net_cost_gbp']:+.2f}  ({s.get('kwh', 0):.1f} kWh)")
        if s.get("export_revenue_gbp"):
            lines.append(f"    Export:      £{s['export_revenue_gbp']:.2f} ({s.get('export_kwh', 0):.1f} kWh)")
        if s.get("delta_vs_fixed_real_gbp") is not None:
            lines.append(f"    vs Fixed BG: £{s['delta_vs_fixed_real_gbp']:+.2f}")
        if s.get("delta_vs_svt_real_gbp") is not None:
            lines.append(f"    vs SVT:      £{s['delta_vs_svt_real_gbp']:+.2f}")
        if s.get("fixed_tariff_label") and s.get("delta_vs_fixed_tariff_real_gbp") is not None:
            lines.append(f"    vs {s['fixed_tariff_label']}: £{s['delta_vs_fixed_tariff_real_gbp']:+.2f}")
    lines.append("")

    # Errors
    e = health["errors"]
    if e["total_error_count"] or e["total_alert_count"]:
        lines.append(f"🚨 ERRORS / ALERTS")
        lines.append(f"    Errors: {e['total_error_count']}  Alerts: {e['total_alert_count']}")
        for ev in e["events"][:5]:
            lines.append(f"    [{ev['result']}] {ev['action']}: {ev['n']}")
    else:
        lines.append(f"✅ ERRORS — none")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(arg: str | None) -> date:
    if arg in (None, "", "today"):
        return datetime.now(TZ_LOCAL).date()
    if arg == "yesterday":
        return (datetime.now(TZ_LOCAL) - timedelta(days=1)).date()
    return date.fromisoformat(arg)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "today"
    target = _parse_date(arg)
    health = compute_system_health(target)

    # Default: human summary. With --json: structured dict.
    if "--json" in sys.argv:
        print(json.dumps(health, indent=2, default=str))
    else:
        print(render_summary(health))
        print()
        print("(append --json for the full structured dict)")
