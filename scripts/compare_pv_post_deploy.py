#!/usr/bin/env python
"""One-shot T+14 comparison: did the PV-trust guard rail + daily calibration
refresh deployed 2026-05-15 actually shift the LP off cheap-grid-charging
on sunny days?

Reads the prod SQLite DB (must be inside the hem container — uses the
project's own `src.db` helpers) and compares:

  • dispatch_decisions: count of committed slots with lp_kind='cheap' per day
    — expected to drop on days with high forecast PV (≥ 12 kWh / day).
  • lp_inputs_snapshot.exogenous_snapshot_json.pv_sufficiency_guard.applied
    — expected to be True on ~3-7 days in the post window.
  • pv_realtime_history: daily kWh exported + auto-consumed
    (auto-consumed = solar - export - charge_into_battery)
  • execution_log realised £ vs fixed shadow (when available)

Periods (UTC dates):
  pre  = 2026-05-01 .. 2026-05-14   (14 days; before guard rail deploy)
  post = 2026-05-15 .. 2026-05-29   (14 days; first full window post-deploy)

Output: markdown summary posted to Telegram via
``src.telegram_transport.send_message``. Also prints the same body to
stdout so the systemd timer's journal carries the result.

Safe to re-run: only reads the DB; no writes. Telegram send failures are
logged but don't fail the script (matches the rest of the system's
"telegram outage never breaks anything" contract).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import traceback
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("compare_pv_post_deploy")

# --- Constants pinned at script-write time ---
PRE_START = date(2026, 5, 1)
PRE_END = date(2026, 5, 14)
POST_START = date(2026, 5, 15)
POST_END = date(2026, 5, 29)
FORECAST_PV_HIGH_KWH_DAY = 12.0  # threshold for "high forecast PV" day classification


def _dates_in(start: date, end: date) -> list[str]:
    out = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d = date.fromordinal(d.toordinal() + 1)
    return out


def _cheap_dispatch_count_per_day(conn: sqlite3.Connection, start: date, end: date) -> dict[str, int]:
    """Count of committed slots with lp_kind='cheap', grouped by local date.

    Uses ``date(slot_time_utc)`` directly — close enough for trend analysis
    even though it doesn't quite align with local-day boundaries.
    """
    rows = conn.execute(
        """SELECT date(slot_time_utc) AS d, COUNT(*) AS n
             FROM dispatch_decisions
            WHERE date(slot_time_utc) BETWEEN ? AND ?
              AND committed = 1
              AND lp_kind = 'cheap'
            GROUP BY d
            ORDER BY d""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {str(r["d"]): int(r["n"]) for r in rows}


def _guard_applied_per_day(conn: sqlite3.Connection, start: date, end: date) -> dict[str, int]:
    """Count of LP solves per day where pv_sufficiency_guard.applied=True."""
    rows = conn.execute(
        """SELECT plan_date AS d, exogenous_snapshot_json AS j
             FROM lp_inputs_snapshot
            WHERE date(plan_date) BETWEEN ? AND ?
              AND exogenous_snapshot_json IS NOT NULL""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        d = str(r["d"])[:10]
        try:
            payload = json.loads(r["j"] or "{}")
        except (TypeError, ValueError):
            continue
        guard = (payload or {}).get("pv_sufficiency_guard") or {}
        if guard.get("applied") is True:
            counts[d] += 1
    return dict(counts)


def _kwh_per_day(conn: sqlite3.Connection, start: date, end: date) -> dict[str, dict[str, float]]:
    """Per-day kWh totals from pv_realtime_history (30-min averaged samples → kWh).

    Returns ``{date_iso: {solar, export, charge, load, self_consumed}}`` where
    self_consumed = solar - export - charge (positive ≈ PV directly into load + battery).
    """
    rows = conn.execute(
        """WITH half_hour AS (
              SELECT
                date(captured_at) AS d,
                datetime( (strftime('%s', captured_at) / 1800) * 1800, 'unixepoch') AS slot,
                AVG(solar_power_kw) AS solar_kw,
                AVG(grid_export_kw) AS export_kw,
                AVG(battery_charge_kw) AS charge_kw,
                AVG(load_power_kw) AS load_kw
              FROM pv_realtime_history
              WHERE date(captured_at) BETWEEN ? AND ?
              GROUP BY slot
            )
            SELECT d,
                   SUM(COALESCE(solar_kw, 0)  * 0.5) AS solar_kwh,
                   SUM(COALESCE(export_kw, 0) * 0.5) AS export_kwh,
                   SUM(COALESCE(charge_kw, 0) * 0.5) AS charge_kwh,
                   SUM(COALESCE(load_kw, 0)   * 0.5) AS load_kwh
              FROM half_hour
             GROUP BY d
             ORDER BY d""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        d = str(r["d"])
        solar = float(r["solar_kwh"] or 0.0)
        export = float(r["export_kwh"] or 0.0)
        charge = float(r["charge_kwh"] or 0.0)
        # Self-consumed PV ≈ what didn't export and didn't go into battery.
        # Includes "PV → load" + slight rounding noise. Negative numbers
        # (export + charge > solar) are clipped to 0 for the display.
        self_cons = max(0.0, solar - export - charge)
        out[d] = {
            "solar_kwh": round(solar, 2),
            "export_kwh": round(export, 2),
            "charge_kwh": round(charge, 2),
            "load_kwh": round(float(r["load_kwh"] or 0.0), 2),
            "self_consumed_kwh": round(self_cons, 2),
        }
    return out


def _high_pv_day_set(per_day: dict[str, dict[str, float]]) -> set[str]:
    """Days where actual measured solar_kwh ≥ FORECAST_PV_HIGH_KWH_DAY."""
    return {d for d, v in per_day.items() if v.get("solar_kwh", 0.0) >= FORECAST_PV_HIGH_KWH_DAY}


def _period_summary(
    *,
    label: str,
    start: date,
    end: date,
    cheap_count: dict[str, int],
    guard_count: dict[str, int],
    kwh: dict[str, dict[str, float]],
) -> dict[str, Any]:
    all_dates = _dates_in(start, end)
    n_days = len(all_dates)
    high_pv = _high_pv_day_set(kwh)

    # Totals across all days in window.
    sum_solar = sum(kwh.get(d, {}).get("solar_kwh", 0.0) for d in all_dates)
    sum_export = sum(kwh.get(d, {}).get("export_kwh", 0.0) for d in all_dates)
    sum_self = sum(kwh.get(d, {}).get("self_consumed_kwh", 0.0) for d in all_dates)
    sum_cheap = sum(cheap_count.get(d, 0) for d in all_dates)
    sum_guard = sum(guard_count.get(d, 0) for d in all_dates)

    # Same totals BUT restricted to high-PV days (the days where we expect
    # the rail to have done work).
    sum_cheap_high_pv = sum(cheap_count.get(d, 0) for d in all_dates if d in high_pv)
    n_high_pv = len(high_pv)

    return {
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_days": n_days,
        "n_high_pv_days": n_high_pv,
        "cheap_slots_total": sum_cheap,
        "cheap_slots_per_day_mean": round(sum_cheap / n_days, 2),
        "cheap_slots_on_high_pv_days": sum_cheap_high_pv,
        "cheap_slots_per_high_pv_day": round(sum_cheap_high_pv / max(1, n_high_pv), 2),
        "guard_applied_solves_total": sum_guard,
        "guard_applied_solves_per_day_mean": round(sum_guard / n_days, 2),
        "solar_kwh_total": round(sum_solar, 1),
        "export_kwh_total": round(sum_export, 1),
        "self_consumed_kwh_total": round(sum_self, 1),
        "self_consumed_per_day_mean": round(sum_self / n_days, 2),
        "export_per_day_mean": round(sum_export / n_days, 2),
    }


def _format_markdown(pre: dict[str, Any], post: dict[str, Any]) -> str:
    """Build the Telegram-ready markdown summary.

    Verdict logic:
      - 'pattern improved' iff post.cheap_per_high_pv_day < pre value
        AND post.self_consumed_per_day > pre value
      - 'pattern unchanged' iff both metrics within ±10%
      - 'pattern regressed' iff either metric went the wrong way > 10%
    """
    def pct_delta(a: float, b: float) -> str:
        if a == 0:
            return "n/a"
        d = (b - a) / a * 100.0
        return f"{d:+.1f}%"

    cheap_delta_pct = pct_delta(
        pre["cheap_slots_per_high_pv_day"],
        post["cheap_slots_per_high_pv_day"],
    )
    self_delta_pct = pct_delta(
        pre["self_consumed_per_day_mean"],
        post["self_consumed_per_day_mean"],
    )
    export_delta_pct = pct_delta(
        pre["export_per_day_mean"],
        post["export_per_day_mean"],
    )

    # Verdict
    cheap_dropped = (
        post["cheap_slots_per_high_pv_day"] < pre["cheap_slots_per_high_pv_day"] * 0.9
    )
    self_up = (
        post["self_consumed_per_day_mean"] > pre["self_consumed_per_day_mean"] * 1.05
    )
    if cheap_dropped and self_up:
        verdict = "✅ **MELHOROU** — padrão original (exportar bateria-cheia) está caindo + auto-consumo subiu."
    elif cheap_dropped or self_up:
        verdict = "🟡 **MELHORIA PARCIAL** — uma das duas métricas melhorou, a outra ficou flat."
    elif (
        post["cheap_slots_per_high_pv_day"] > pre["cheap_slots_per_high_pv_day"] * 1.1
        or post["self_consumed_per_day_mean"] < pre["self_consumed_per_day_mean"] * 0.9
    ):
        verdict = "🔴 **REGRESSÃO** — métricas pioraram em > 10%. Investigar."
    else:
        verdict = "⚪ **INCONCLUSIVO** — variação dentro de ±10%, padrão pode não ter mudado significativamente."

    lines = [
        "**T+14 — PV pós-deploy comparison**",
        f"_pre  ({pre['start']} → {pre['end']}, n={pre['n_days']}d)_",
        f"_post ({post['start']} → {post['end']}, n={post['n_days']}d)_",
        "",
        "**Cheap dispatches em dias com PV alto** (≥12 kWh/dia)",
        f"  pre  {pre['n_high_pv_days']}d alta-PV → {pre['cheap_slots_on_high_pv_days']} slots ({pre['cheap_slots_per_high_pv_day']}/dia)",
        f"  post {post['n_high_pv_days']}d alta-PV → {post['cheap_slots_on_high_pv_days']} slots ({post['cheap_slots_per_high_pv_day']}/dia)",
        f"  Δ {cheap_delta_pct}",
        "",
        "**Guard rail aplicado** (em quantos solves o LP bloqueou grid→bateria)",
        f"  pre  0 solves (rail não existia)",
        f"  post {post['guard_applied_solves_total']} solves em {post['n_days']}d ({post['guard_applied_solves_per_day_mean']}/dia)",
        "",
        "**Auto-consumo PV** (kWh/dia)",
        f"  pre  {pre['self_consumed_per_day_mean']}",
        f"  post {post['self_consumed_per_day_mean']}",
        f"  Δ {self_delta_pct}",
        "",
        "**Exportação grid** (kWh/dia)",
        f"  pre  {pre['export_per_day_mean']}",
        f"  post {post['export_per_day_mean']}",
        f"  Δ {export_delta_pct}",
        "",
        verdict,
        "",
        "_Fonte: dispatch_decisions + lp_inputs_snapshot + pv_realtime_history em prod DB._",
        "_Refs: PR #331 (rail+cron), #333 (tf=1.0). Script: scripts/compare_pv_post_deploy.py._",
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        # Late import — script must work even if the project venv changes
        # subtly. Importing here also lets us catch ImportError cleanly.
        from src import db
        from src import telegram_transport

        conn = db.get_connection()
        try:
            cheap_pre = _cheap_dispatch_count_per_day(conn, PRE_START, PRE_END)
            cheap_post = _cheap_dispatch_count_per_day(conn, POST_START, POST_END)
            guard_pre = _guard_applied_per_day(conn, PRE_START, PRE_END)
            guard_post = _guard_applied_per_day(conn, POST_START, POST_END)
            kwh_pre = _kwh_per_day(conn, PRE_START, PRE_END)
            kwh_post = _kwh_per_day(conn, POST_START, POST_END)
        finally:
            conn.close()

        pre = _period_summary(
            label="pre",
            start=PRE_START, end=PRE_END,
            cheap_count=cheap_pre,
            guard_count=guard_pre,
            kwh=kwh_pre,
        )
        post = _period_summary(
            label="post",
            start=POST_START, end=POST_END,
            cheap_count=cheap_post,
            guard_count=guard_post,
            kwh=kwh_post,
        )
        body = _format_markdown(pre, post)
        print(body)
        print()

        if telegram_transport.is_configured():
            sent = telegram_transport.send_message(body, silent=False)
            print(f"telegram send: {'ok' if sent else 'failed'}")
        else:
            print("telegram NOT configured — body printed above; no message sent.")
        return 0

    except Exception as exc:
        # Best-effort: try to send the error to Telegram so the user knows
        # the comparison ran but failed. If Telegram isn't reachable, fall
        # through to stderr.
        tb = traceback.format_exc()
        err_msg = (
            "**T+14 comparison FAILED**\n\n"
            f"`{type(exc).__name__}: {exc}`\n\n"
            "Investigate. Script lives at /app/data/compare_pv_post_deploy.py "
            "in the hem container; ssh + docker exec to retry by hand.\n\n"
            f"```\n{tb[-1500:]}\n```"
        )
        print(err_msg, file=sys.stderr)
        try:
            from src import telegram_transport
            if telegram_transport.is_configured():
                telegram_transport.send_message(err_msg, silent=False)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
