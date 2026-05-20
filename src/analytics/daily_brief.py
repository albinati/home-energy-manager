"""Twice-daily digest: morning forecast + night actuals.

The single 08:00-style brief was split (V12) into two independent crons so the
household sees the day's plan separately from the day's outcome.

* :func:`build_morning_payload` — today's forecast: tariff windows summary,
  planned peak-export commitments (read from ``dispatch_decisions``), expected
  savings vs SVT shadow, weather-driven heating outlook.
* :func:`build_night_payload` — today's actuals: realised cost, savings vs SVT
  shadow, slot summary, peak-export verdicts (committed vs dropped).

Both reuse the existing notifier surface (:func:`notify_morning_report` and the
new :func:`notify_night_brief`). Out-of-digest pings during the day are
restricted to genuine errors + the 🔵 PAID-to-use crossing.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..notifier import notify_morning_report, notify_night_brief
from ..physics import get_daikin_heating_kw, get_kw_per_degc_lwt
from .pnl import (
    compute_arbitrage_efficiency,
    compute_daily_pnl,
    compute_slippage,
    compute_vwap,
)
from .sla import compute_sla_metrics


def datetime_now_local_date(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


# --------------------------------------------------------------------------
# Morning brief — today's forecast
# --------------------------------------------------------------------------

def build_morning_payload() -> str:
    """Return the morning-brief markdown body.

    Anchored on **today** (local). Yesterday's PnL one-liner is included as a
    reminder of how we did, but the meat is forward-looking.
    """
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    today = datetime_now_local_date(tz)
    yesterday = today - timedelta(days=1)

    pnl = compute_daily_pnl(yesterday)
    tgt = db.get_daily_target(today)
    strategy = (tgt or {}).get("strategy_summary") or "No strategy row for today yet."

    # Tier-window summary for today (reuse the same classification the family
    # calendar uses, so the morning brief and the calendar agree word-for-word).
    tier_summary = _today_tier_window_summary(today, tz)

    # Peak-export commitments from the latest LP run with peak_export slots.
    pe_summary = _peak_export_commitments_for_today(today, tz)

    # No "## Morning brief" headline here — the notifier prepends "🌅 Morning
    # brief" (Telegram) and the action_log/journalctl entries already carry the
    # ``[morning_report]`` alert-type tag. Duplicating the title produced a
    # stacked header on Telegram (#330).
    lines: list[str] = [
        f"**Today ({today})**",
        strategy,
        "",
        _mode_status_line(),
        "",
    ]
    if tier_summary:
        lines.extend(["**Tariff windows today:**", tier_summary, ""])
    # Tomorrow's peak windows surfaced separately so a low-action day
    # (no LP `peak` slots) doesn't read as "no peaks tomorrow" when
    # Octopus actually has an expensive evening.
    tomorrow_peaks = _tariff_peak_windows_summary(today + timedelta(days=1), tz)
    if tomorrow_peaks:
        lines.extend([f"**Tomorrow ({today + timedelta(days=1)}):**", tomorrow_peaks, ""])
    if pe_summary:
        lines.extend(["**Peak-export plan:**", pe_summary, ""])

    lines.append(f"**Yesterday ({yesterday}) — financial summary**")
    lines.extend(_format_pnl_block(pnl, day=yesterday, tz=tz))

    overnight_lines = _overnight_plan_vs_actual_lines(today, tz)
    if overnight_lines:
        lines.extend(["", "**Madrugada (plan vs actual):**", *overnight_lines])

    bias_line = _pv_bias_line()
    if bias_line:
        lines.extend(["", bias_line])

    return "\n".join(lines)


def _pv_bias_line() -> str | None:
    """One-line PV forecast bias (am/pm) for the morning brief.

    Returns ``None`` when no paired samples exist yet — callers should skip
    rather than render an empty line. Surfaces the same headline that the
    ``get_pv_forecast_bias`` MCP tool returns, so OpenClaw + the brief agree.
    """
    try:
        from .pv_bias_report import summarise_pv_bias
        report = summarise_pv_bias(window_days=int(config.PV_BIAS_REPORT_WINDOW_DAYS))
    except Exception:  # pragma: no cover — diagnostic line must never break the brief
        return None
    if int(report.get("n_paired", 0)) <= 0:
        return None
    return report.get("headline")


def _overnight_plan_vs_actual_lines(today: date, tz: ZoneInfo) -> list[str]:
    """Return markdown bullets comparing last overnight's plan vs actual.

    Window = yesterday 22:00 local → today 09:00 local (the post-shower,
    pre-morning-shower stretch when the LP runs `tank_idle_overnight` and the
    space-heating climate curve dominates the LP load forecast).

    Each bullet is best-effort — if its data source is missing, that bullet
    is omitted (no error). When ALL sources are missing, returns ``[]`` so
    the caller skips the section header entirely.

    Lines emitted (in order, when data permits):
      - Heating: Σ predicted `space_floor` vs Σ realised `kwh_heating`
      - Battery: SoC at start of overnight vs current
      - Outdoor: forecast min vs Daikin sensor min
      - Calibration k: current vs default (and updated_at)
    """
    bullets: list[str] = []

    yesterday = today - timedelta(days=1)
    start_local = datetime.combine(yesterday, time(22, 0)).replace(tzinfo=tz)
    end_local = datetime.combine(today, time(9, 0)).replace(tzinfo=tz)
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)

    # ── Heating: predicted vs actual ────────────────────────────────────
    heating_bullet = _heating_plan_vs_actual(start_utc, end_utc, yesterday, today)
    if heating_bullet:
        bullets.append(heating_bullet)

    # ── Battery SoC drift ───────────────────────────────────────────────
    bat_bullet = _battery_overnight_drift(start_utc)
    if bat_bullet:
        bullets.append(bat_bullet)

    # ── Outdoor forecast vs Daikin sensor ───────────────────────────────
    out_bullet = _outdoor_forecast_vs_sensor(start_utc, end_utc)
    if out_bullet:
        bullets.append(out_bullet)

    # ── Calibration k status ────────────────────────────────────────────
    k_bullet = _calibration_k_status_line()
    if k_bullet:
        bullets.append(k_bullet)

    return bullets


def _heating_plan_vs_actual(
    start_utc: datetime, end_utc: datetime, yesterday: date, today: date,
) -> str | None:
    """Sum predicted heating energy (LP space_floor) vs realised kwh_heating.

    Predicted comes from re-running the same physics the LP used:
    ``Σ get_daikin_heating_kw(t_outdoor) × Δt`` over the overnight slots,
    using the FRESHEST meteo snapshot per slot_time (latest fetched value
    for each hour). Realised comes from ``daikin_consumption_2hourly``
    bucket sums covering the same window.

    Returns None when EITHER source is too sparse to be meaningful (< 6 h
    of meteo coverage OR < 4 of the 5 expected 2h-buckets present), so the
    bullet doesn't lie about non-existent comparison.
    """
    try:
        with db._lock:
            conn = db.get_connection()
            try:
                # Predicted: per-hour outdoor temps from the freshest snapshot
                # covering each slot_time in the window. Same UNION pattern
                # as compute_daikin_lwt_kw_calibration to span both meteo
                # tables (history retains longer than value).
                cur = conn.execute(
                    """SELECT slot_time, temp_c FROM (
                           SELECT slot_time, temp_c, forecast_fetch_at_utc
                             FROM meteo_forecast_history
                            WHERE slot_time >= ? AND slot_time < ?
                              AND temp_c IS NOT NULL
                           UNION ALL
                           SELECT slot_time, temp_c, forecast_fetch_at_utc
                             FROM meteo_forecast_value
                            WHERE slot_time >= ? AND slot_time < ?
                              AND temp_c IS NOT NULL
                       ) AS u
                       WHERE forecast_fetch_at_utc = (
                           SELECT MAX(f) FROM (
                               SELECT forecast_fetch_at_utc AS f
                                 FROM meteo_forecast_history
                                WHERE slot_time = u.slot_time AND temp_c IS NOT NULL
                               UNION ALL
                               SELECT forecast_fetch_at_utc AS f
                                 FROM meteo_forecast_value
                                WHERE slot_time = u.slot_time AND temp_c IS NOT NULL
                           )
                       )
                       GROUP BY slot_time
                       ORDER BY slot_time""",
                    (start_utc.isoformat(), end_utc.isoformat(),
                     start_utc.isoformat(), end_utc.isoformat()),
                )
                slot_temps = cur.fetchall()
                # Realised heating from 2-hourly Daikin consumption.
                # Yesterday's last bucket (idx 11 = 22:00–24:00 local) plus
                # today's morning buckets (idx 0–4 = 00:00–10:00 local).
                # Local time matches the 2h-bucket convention per CLAUDE.md.
                cur = conn.execute(
                    """SELECT date, bucket_idx, kwh_heating
                       FROM daikin_consumption_2hourly
                       WHERE (date = ? AND bucket_idx >= 11)
                          OR (date = ? AND bucket_idx <= 4)""",
                    (yesterday.isoformat(), today.isoformat()),
                )
                heat_rows = cur.fetchall()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001 — brief must never error
        return None

    # Predicted side
    if len(slot_temps) < 12:  # need ≥ 6 h of slot coverage
        return None
    slot_h = (end_utc - start_utc).total_seconds() / 3600.0 / max(1, len(slot_temps))
    predicted_kwh = sum(
        get_daikin_heating_kw(float(r["temp_c"])) * slot_h for r in slot_temps
    )

    # Realised side
    realised_buckets = [
        float(r["kwh_heating"]) for r in heat_rows if r["kwh_heating"] is not None
    ]
    if len(realised_buckets) < 4:  # missing > 1 bucket → no honest comparison
        return None
    realised_kwh = sum(realised_buckets)

    delta_kwh = realised_kwh - predicted_kwh
    delta_pct = (delta_kwh / predicted_kwh * 100.0) if predicted_kwh > 0.01 else 0.0
    k_now = get_kw_per_degc_lwt()
    return (
        f"- Heating: predicted {predicted_kwh:.1f} kWh (k={k_now:.4f}) → "
        f"real {realised_kwh:.1f} kWh ({delta_kwh:+.1f} kWh, {delta_pct:+.0f} %)"
    )


def _battery_overnight_drift(start_utc: datetime) -> str | None:
    """SoC % at start of overnight vs the latest snapshot. Net charge/discharge."""
    try:
        with db._lock:
            conn = db.get_connection()
            try:
                # First sample at-or-after the overnight start
                cur = conn.execute(
                    """SELECT soc_pct FROM pv_realtime_history
                       WHERE captured_at >= ? AND soc_pct IS NOT NULL
                       ORDER BY captured_at ASC LIMIT 1""",
                    (start_utc.isoformat(),),
                )
                start_row = cur.fetchone()
                # Latest snapshot
                cur = conn.execute(
                    "SELECT soc_pct FROM pv_realtime_history "
                    "WHERE soc_pct IS NOT NULL ORDER BY captured_at DESC LIMIT 1"
                )
                now_row = cur.fetchone()
            finally:
                conn.close()
    except Exception:
        return None
    if not start_row or not now_row:
        return None
    soc_start = float(start_row["soc_pct"])
    soc_now = float(now_row["soc_pct"])
    delta = soc_now - soc_start
    arrow = "→" if abs(delta) < 1 else ("↑" if delta > 0 else "↓")
    return f"- Battery SoC: {soc_start:.0f} % {arrow} {soc_now:.0f} % ({delta:+.0f} pp overnight)"


def _outdoor_forecast_vs_sensor(start_utc: datetime, end_utc: datetime) -> str | None:
    """Coldest forecast vs coldest Daikin-sensor reading across the overnight."""
    try:
        with db._lock:
            conn = db.get_connection()
            try:
                cur = conn.execute(
                    """SELECT MIN(temp_c) AS t FROM meteo_forecast_value
                       WHERE slot_time >= ? AND slot_time < ?
                         AND temp_c IS NOT NULL""",
                    (start_utc.isoformat(), end_utc.isoformat()),
                )
                fr = cur.fetchone()
                # daikin_telemetry.fetched_at is REAL epoch — see schema note
                cur = conn.execute(
                    """SELECT MIN(outdoor_temp_c) AS t FROM daikin_telemetry
                       WHERE fetched_at >= ? AND fetched_at < ?
                         AND outdoor_temp_c IS NOT NULL""",
                    (start_utc.timestamp(), end_utc.timestamp()),
                )
                dr = cur.fetchone()
            finally:
                conn.close()
    except Exception:
        return None
    forecast_min = fr["t"] if fr else None
    sensor_min = dr["t"] if dr else None
    if forecast_min is None and sensor_min is None:
        return None
    if forecast_min is None:
        return f"- Outdoor min (sensor only): {sensor_min:.1f} °C"
    if sensor_min is None:
        return f"- Outdoor min (forecast only): {forecast_min:.1f} °C"
    delta = sensor_min - forecast_min
    return (
        f"- Outdoor min: forecast {forecast_min:.1f} °C → sensor {sensor_min:.1f} °C "
        f"({delta:+.1f} °C off)"
    )


def _calibration_k_status_line() -> str | None:
    """Surface the active LWT→kW calibration value."""
    try:
        row = db.get_daikin_lwt_kw_calibration()
    except Exception:
        return None
    if row is None:
        return None
    k = float(row["k_per_degc"])
    default = 0.0333
    delta_pct = (k - default) / default * 100.0
    samples = int(row.get("samples") or 0)
    return f"- Calibration k: {k:.5f} kW/°C ({delta_pct:+.1f} % vs default, {samples} d)"


# --------------------------------------------------------------------------
# Night brief — today's actuals
# --------------------------------------------------------------------------

def build_night_payload() -> str:
    """Return the night-brief markdown body.

    Anchored on **today** (local) — sums today's ``execution_log`` rows and
    cross-references ``dispatch_decisions`` so the family sees:

    * how much we spent today,
    * how that compares to SVT shadow,
    * which planned peak-exports actually got committed and how much they
      contributed.
    """
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    today = datetime_now_local_date(tz)

    pnl = compute_daily_pnl(today)
    vwap = compute_vwap(today)
    slip = compute_slippage(today)
    arb = compute_arbitrage_efficiency(today)
    sla = compute_sla_metrics(limit=200)

    pe_today = _peak_export_outcomes_for_today(today, tz)

    # No "## Night brief" headline — same reason as the morning brief above
    # (#330): the notifier prepends "🌙 Night brief" on the Telegram path and
    # the alert-type tag is on the action_log entry already.
    lines: list[str] = [
        f"**Today ({today})** — actuals",
        "",
        _mode_status_line(),
        "",
    ]
    lines.extend(_format_pnl_block(pnl, day=today, tz=tz))
    lines.append(f"- VWAP: {vwap}p/kWh" if vwap else "- VWAP: n/a")
    if slip is not None:
        lines.append(f"- Slippage vs target: {slip}p")
    if arb is not None:
        lines.append(f"- Arbitrage efficiency (cheap quartile): {arb}%")
    lines.append(f"- SLA sample: {sla.get('sample_size', 0)} actions")
    if pe_today:
        lines.extend(["", "**Peak-export verdicts today:**", pe_today])
    # Heads-up for tomorrow's expensive windows so the family knows when
    # to expect the LP to draw the battery hardest. Independent of the
    # LP `peak` classification (which counts shave actions, not raw price).
    tomorrow_peaks = _tariff_peak_windows_summary(today + timedelta(days=1), tz)
    if tomorrow_peaks:
        lines.extend(["", f"**Heads-up for tomorrow:** {tomorrow_peaks}"])
    return "\n".join(lines)


def _mode_status_line() -> str:
    """One-line mode status so OpenClaw stops inventing Daikin advice.

    Both ``passive`` and ``active`` modes leave SPACE heating to the Daikin
    firmware's weather curve since PR #321 (climate-strip incident,
    2026-05-11). The active-mode difference is solely that HEM dispatches
    the **DHW tank target** based on the LP plan (e.g. cheap-overnight
    pre-charge, peak-window idle). LWT offset + climate_on are never
    written to Daikin in either mode now — surface that explicitly so the
    LLM rendering the brief doesn't invent tactical space-heating advice.
    """
    daikin_mode = (config.DAIKIN_CONTROL_MODE or "passive").strip().lower()
    if daikin_mode == "active":
        daikin_label = (
            "active (HEM dispatches DHW tank target per LP plan; firmware "
            "weather curve owns space heating — HEM does NOT write LWT offset)"
        )
    else:
        daikin_label = (
            "passive (telemetry-only; Onecta firmware drives autonomously on weather curve — "
            "HEM does NOT alter setpoints)"
        )
    fox_label = (
        "LP-dispatched (Scheduler V3 groups uploaded after each solve)"
        if not config.OPENCLAW_READ_ONLY
        else "READ_ONLY (no hardware writes)"
    )
    return f"**Mode:** Daikin={daikin_label}; Fox={fox_label}"


def _format_pnl_block(pnl: dict[str, Any], *, day: date, tz: ZoneInfo) -> list[str]:
    """Structured 4-pillar financial breakdown for both briefs.

    Output lines:
      - Net cost line breaking out import / standing / export
      - Energy used + exported (kWh totals)
      - One delta line per configured shadow tariff (SVT, Fixed, optional FIXED_TARIFF)
      - Optional 🔮 forecasted-export line when telemetry export is 0 but the
        LP planned committed peak_export slots for the day

    Standing charges are baked into all shadows (apples-to-apples), so the
    delta is genuine "money saved" not "energy-cost-only saved".
    """
    out: list[str] = []
    # Real-money view (preferred for user-facing £)
    net_real = pnl.get("realised_net_cost_gbp")
    imp_kwh = pnl.get("import_kwh")
    imp_real = pnl.get("import_cost_gbp")
    std = pnl.get("standing_charge_gbp", 0.0)
    exp_gbp = pnl["export_revenue_gbp"]
    exp_kwh = pnl["export_kwh"]
    # Legacy load-billed view (counterfactual: if no solar/battery)
    net_load = pnl["realised_cost_gbp"]
    imp_load = pnl["realised_import_gbp"]
    used_kwh = pnl["kwh"]

    if net_real is not None and imp_real is not None and imp_kwh is not None:
        out.append(
            f"- Net cost: **£{net_real:+.2f}** (real, metered) "
            f"— import £{imp_real:.2f} ({imp_kwh:.1f} kWh) + standing £{std:.2f} − export £{exp_gbp:.2f}"
        )
        out.append(
            f"- Energy used: {used_kwh:.1f} kWh  |  imported: {imp_kwh:.1f} kWh  |  exported: {exp_kwh:.1f} kWh"
        )
    else:
        out.append(
            f"- Net cost: £{net_load:+.2f}  (import £{imp_load:.2f} + standing £{std:.2f} − export £{exp_gbp:.2f})"
        )
        out.append(f"- Energy used: {used_kwh:.1f} kWh  |  exported: {exp_kwh:.1f} kWh")

    # Best-practice KPI context (added 2026-05-20). Each line answers a
    # distinct question:
    #   1. MTD-context  — "is today good vs my typical day this month?"
    #   2. Mean import rate — "did I import at the cheap end or the peak?"
    #   3. Forgone export — "what's the running cost of strict_savings?"
    # All three skip cleanly when the data isn't there (first of month, no
    # imports, non-strict_savings mode) so legacy briefs don't gain noise.
    # ``_mtd_summary`` is computed ONCE here and shared so we don't pay the
    # ~380 ms compute_period_pnl latency twice per brief.
    mtd = _mtd_summary(day)
    for line in (
        _mtd_context_line(day, pnl, mtd),
        _mean_agile_rate_line(day, pnl, mtd),
        _strict_savings_forgone_line(day, tz),
        _lp_scorecard_line(day),
    ):
        if line:
            out.append(line)
    # Phase A audit line: Fox CT clamps vs Octopus smart-meter daily totals
    # for divergence detection. Both sources should agree to ~5%; bigger
    # gaps suggest heartbeat coverage problems or meter calibration drift.
    audit_line = _fox_vs_meter_audit_line(day)
    if audit_line:
        out.append(audit_line)
    skill_line = _forecast_skill_line(day)
    if skill_line:
        out.append(skill_line)
    # #309: surface dispatch budget-guard drops so the user sees when the
    # Daikin quota was tight enough that low-value plan pairs were pruned.
    # Critical NEGATIVE/PEAK pairs are always preserved; only CHEAP /
    # SOLAR_PREHEAT pairs get dropped, so this is an FYI not an alarm.
    budget_line = _budget_guard_summary_line(day)
    if budget_line:
        out.append(budget_line)

    # Forecasted-export fallback: telemetry sometimes drops grid_export_kw
    # samples, leaving export_kwh=0 even on days where the LP committed
    # peak_export and the inverter genuinely discharged. Estimate from
    # `dispatch_decisions × agile_export_rates` and flag with 🔮.
    if exp_kwh == 0:
        f_kwh, f_pence, f_slots = _forecasted_export_for_day(day, tz)
        if f_slots > 0:
            out.append(
                f"- 🔮 Forecasted export (telemetry missing): "
                f"~{f_kwh:.2f} kWh ≈ £{f_pence / 100.0:+.2f} "
                f"(from {f_slots} committed peak_export slot{'s' if f_slots != 1 else ''})"
            )

    # Prefer real-money deltas when available, fall back to load-billed
    have_real = "delta_vs_svt_real_gbp" in pnl

    if "delta_vs_fixed_tariff_real_gbp" in pnl or "delta_vs_fixed_tariff_gbp" in pnl:
        label = pnl.get("fixed_tariff_label") or "fixed tariff"
        if have_real and "fixed_tariff_shadow_real_gbp" in pnl:
            shadow = pnl["fixed_tariff_shadow_real_gbp"]
            delta = pnl["delta_vs_fixed_tariff_real_gbp"]
        else:
            shadow = pnl["fixed_tariff_shadow_gbp"]
            delta = pnl["delta_vs_fixed_tariff_gbp"]
        out.append(
            f"- vs {label} (would have cost £{shadow:.2f}): "
            f"£{delta:+.2f} {'saved' if delta >= 0 else 'extra'}"
        )

    if have_real:
        out.append(
            f"- vs SVT (would have cost £{pnl['svt_shadow_real_gbp']:.2f}): "
            f"£{pnl['delta_vs_svt_real_gbp']:+.2f} "
            f"{'saved' if pnl['delta_vs_svt_real_gbp'] >= 0 else 'extra'}"
        )
        if pnl["fixed_shadow_real_gbp"] != pnl["svt_shadow_real_gbp"]:
            out.append(
                f"- vs fixed shadow (£{pnl['fixed_shadow_real_gbp']:.2f}): "
                f"£{pnl['delta_vs_fixed_real_gbp']:+.2f} "
                f"{'saved' if pnl['delta_vs_fixed_real_gbp'] >= 0 else 'extra'}"
            )
    else:
        out.append(
            f"- vs SVT (would have cost £{pnl['svt_shadow_gbp']:.2f}): "
            f"£{pnl['delta_vs_svt_gbp']:+.2f} {'saved' if pnl['delta_vs_svt_gbp'] >= 0 else 'extra'}"
        )
        if pnl["fixed_shadow_gbp"] != pnl["svt_shadow_gbp"]:
            out.append(
                f"- vs fixed shadow (£{pnl['fixed_shadow_gbp']:.2f}): "
                f"£{pnl['delta_vs_fixed_gbp']:+.2f} {'saved' if pnl['delta_vs_fixed_gbp'] >= 0 else 'extra'}"
            )
    return out


def _fox_vs_meter_audit_line(day: date) -> str | None:
    """Format a one-line side-by-side comparison of Fox CT-clamp totals vs
    Octopus smart-meter daily totals. Both sources should agree to ~5%; bigger
    gaps surface heartbeat-coverage or calibration issues early.

    Returns ``None`` when either source is missing for ``day`` (e.g. no
    Octopus daily cache yet, no Fox API rollup).
    """
    fox = db.get_fox_energy_daily_by_date(day.isoformat())
    meter = db.get_octopus_daily_meter(day.isoformat())
    if not fox or not meter:
        return None

    def _fmt_pair(fox_v: float | None, meter_v: float | None) -> str | None:
        if fox_v is None or meter_v is None:
            return None
        gap_pct = ((fox_v - meter_v) / meter_v * 100) if meter_v else 0.0
        return f"{fox_v:.2f} / {meter_v:.2f} kWh ({gap_pct:+.1f}%)"

    imp = _fmt_pair(fox.get("import_kwh"), meter.get("import_kwh"))
    exp = _fmt_pair(fox.get("export_kwh"), meter.get("export_kwh"))
    if not imp:
        return None
    parts = [f"import {imp}"]
    if exp:
        parts.append(f"export {exp}")
    return f"- Audit (Fox vs meter): {' | '.join(parts)}"


def _mtd_summary(day: date) -> dict[str, Any] | None:
    """Compute the MTD aggregate (start-of-month → day-1) ONCE per brief.

    The result is shared by ``_mtd_context_line`` and ``_mean_agile_rate_line``
    so the brief pays ~380 ms of compute_period_pnl latency once, not twice.
    Returns ``None`` on the 1st of the month or when the period aggregator
    fails — both consumer helpers handle None gracefully.
    """
    if day.day <= 1:
        return None
    try:
        from .pnl import compute_period_pnl
    except Exception:
        return None
    start_day = day.replace(day=1)
    end_day = day - timedelta(days=1)
    try:
        return compute_period_pnl(start_day, end_day, label=f"MTD to {end_day.isoformat()}")
    except Exception:
        return None


def _mtd_context_line(day: date, pnl: dict[str, Any], mtd: dict[str, Any] | None) -> str | None:
    """Today's net cost as % of MTD daily-average. Best-practice KPI: anchors
    a single £ figure against the user's typical month-so-far daily spend
    so the brief reads "good day / bad day" at a glance.

    The MTD window EXCLUDES ``day`` itself (compares vs the previous days'
    typical, not vs (today + prev days)). Caller passes the shared MTD
    summary from ``_mtd_summary(day)``; ``None`` means we're on the 1st
    of the month or the aggregator failed."""
    if mtd is None:
        return None
    n_days = int(mtd.get("n_days") or 0)
    if n_days <= 0:
        return None
    mtd_net = mtd.get("realised_net_cost_gbp")
    if mtd_net is None:
        mtd_net = mtd.get("realised_cost_gbp")
    today_net = pnl.get("realised_net_cost_gbp")
    if today_net is None:
        today_net = pnl.get("realised_cost_gbp")
    if mtd_net is None or today_net is None:
        return None
    avg_per_day = mtd_net / n_days
    if avg_per_day == 0:
        return None
    pct = 100.0 * today_net / avg_per_day
    arrow = "↓" if pct < 100 else "↑" if pct > 100 else "="
    return (
        f"- vs MTD avg daily £{avg_per_day:+.2f} (over {n_days} d): "
        f"today £{today_net:+.2f} {arrow} **{pct:.0f}%** of avg"
    )


def _mean_agile_rate_line(day: date, pnl: dict[str, Any], mtd: dict[str, Any] | None) -> str | None:
    """Import-weighted mean Agile rate for the day vs MTD weighted average.

    Import-weighted = what we ACTUALLY paid per imported kWh, not the
    24h-time-average of published rates. Tells the user "did we manage to
    import at the cheap end of today's range, or were we forced to import
    at peak?" — a leading indicator of LP + dispatch quality independent
    of weather/load variance. Caller passes the shared MTD summary.
    """
    imp_kwh = pnl.get("import_kwh")
    imp_gbp = pnl.get("import_cost_gbp")
    if imp_kwh is None or imp_gbp is None or imp_kwh <= 0:
        return None
    today_p_per_kwh = (imp_gbp * 100.0) / imp_kwh

    mtd_p_per_kwh: float | None = None
    if mtd is not None:
        m_kwh = mtd.get("import_kwh")
        m_gbp = mtd.get("import_cost_gbp")
        if m_kwh and m_gbp is not None and m_kwh > 0:
            mtd_p_per_kwh = (m_gbp * 100.0) / m_kwh

    if mtd_p_per_kwh is None:
        return f"- Mean import rate today: **{today_p_per_kwh:.1f} p/kWh** ({imp_kwh:.1f} kWh imported)"
    delta_pct = ((today_p_per_kwh - mtd_p_per_kwh) / mtd_p_per_kwh * 100.0)
    arrow = "↓" if delta_pct < 0 else "↑" if delta_pct > 0 else "="
    return (
        f"- Mean import rate today: **{today_p_per_kwh:.1f} p/kWh** "
        f"vs MTD {mtd_p_per_kwh:.1f} p/kWh ({arrow} {delta_pct:+.0f}%)"
    )


def _strict_savings_forgone_line(day: date, tz: ZoneInfo) -> str | None:
    """Counterfactual: revenue NOT realised because strict_savings (or the
    scenario filter) downgraded LP-preferred peak_export slots to standard.

    For each slot on ``day`` whose ``dispatch_decisions.dispatched_kind`` is
    not ``peak_export`` but whose snapshot ``lp_solution_snapshot.export_kwh``
    is positive, sum the would-have-earned revenue. Gives the user a daily
    running tally of holding the strict_savings policy — useful to revisit
    the trade-off when the gap is sustained over weeks."""
    if config.ENERGY_STRATEGY_MODE != "strict_savings":
        return None
    try:
        rows = db.list_strict_savings_forgone_export_for_day(day.isoformat())
    except (AttributeError, Exception):
        # Helper may not exist in older DB layers — surface nothing gracefully.
        return None
    if not rows:
        return None
    forgone_kwh = sum(float(r.get("export_kwh") or 0) for r in rows)
    forgone_p = sum(
        float(r.get("export_kwh") or 0) * float(r.get("export_price_p_kwh") or 0)
        for r in rows
    )
    if forgone_kwh <= 0:
        return None
    return (
        f"- strict_savings forgone export: ~£{forgone_p / 100.0:.2f} "
        f"({forgone_kwh:.1f} kWh over {len(rows)} slot{'s' if len(rows) != 1 else ''}) "
        f"— what *savings_first* would have earned by exporting at peak"
    )


def _lp_scorecard_line(day: date) -> str | None:
    """One-line LP optimisation grade + avoided-cost headline.

    Composite signal: did the LP outperform a naive SelfUse-only strategy
    AND did its plan execute as predicted? Hidden when the scorecard's
    grade is N/A (data too sparse) so the brief doesn't render a useless
    bullet on a fresh DB / partial-data day.
    """
    try:
        from .lp_scorecard import build_lp_scorecard
        card = build_lp_scorecard(day)
    except Exception:  # pragma: no cover — never break the brief
        return None
    grade = card.get("grade")
    if grade in (None, "N/A"):
        return None
    econ = card.get("economic_value") or {}
    dispatch = card.get("dispatch_accuracy") or {}
    avoided = econ.get("lp_avoided_cost_p")
    pcts = [
        dispatch.get(k) for k in
        ("import_accuracy_pct", "export_accuracy_pct", "charge_accuracy_pct")
        if dispatch.get(k) is not None
    ]
    if not pcts:
        return None
    avg_acc = sum(pcts) / len(pcts)
    if avoided is None:
        return f"- LP grade: **{grade}** (dispatch accuracy {avg_acc:.0f}%)"
    if avoided >= 0:
        verb = "avoided"
        sign = "+"
    else:
        verb = "overspent"
        sign = "-"
        avoided = abs(avoided)
    return (
        f"- LP grade: **{grade}** — {verb} £{sign}{avoided / 100.0:.2f} vs naive SelfUse "
        f"(dispatch accuracy {avg_acc:.0f}%)"
    )


def _forecast_skill_line(day: date) -> str | None:
    """Format a one-line forecast-vs-actual summary from forecast_skill_log.

    Reads ``forecast_skill_log`` rows for ``day`` (UTC) populated by
    ``rebuild_forecast_skill_log_for_date``. Returns mean outdoor-temp MAE
    and PV bias (forecast − actual). Skipped when no rows.
    """
    try:
        iso = day.isoformat()
        rows = db.get_forecast_skill_rows(iso, iso)
    except Exception:
        return None
    if not rows:
        return None

    temp_errs: list[float] = []
    pv_diffs: list[float] = []
    for r in rows:
        pt = r.get("predicted_temp_c")
        at = r.get("actual_temp_c")
        if pt is not None and at is not None:
            temp_errs.append(abs(float(pt) - float(at)))
        pp = r.get("predicted_pv_kwh")
        ap = r.get("actual_pv_kwh")
        if pp is not None and ap is not None:
            pv_diffs.append(float(pp) - float(ap))

    parts: list[str] = []
    if temp_errs:
        parts.append(f"outdoor MAE {sum(temp_errs)/len(temp_errs):.1f}°C")
    if pv_diffs:
        parts.append(f"PV bias {sum(pv_diffs)/len(pv_diffs):+.2f} kWh/h")
    if not parts:
        return None
    return f"- Forecast skill ({len(rows)}h): {', '.join(parts)}"


def _budget_guard_summary_line(day: date) -> str | None:
    """Format a one-line summary of any Daikin budget-guard drops on ``day``.

    Reads ``action_log`` rows tagged ``action='budget_guard_drop'`` written
    by ``write_daikin_from_lp_plan`` when the dispatch quota guard pruned
    low-value pairs. Aggregates across the day. Returns ``None`` when no
    drop events were logged.
    """
    try:
        rows = db.get_action_logs(device="daikin", trigger="lp_dispatch", limit=200)
    except Exception:
        return None
    if not rows:
        return None
    iso_prefix = day.isoformat()
    drop_rows = [
        r for r in rows
        if str(r.get("action") or "") == "budget_guard_drop"
        and str(r.get("timestamp") or "").startswith(iso_prefix)
    ]
    if not drop_rows:
        return None

    total_dropped = 0
    min_headroom = 9999
    drop_kinds: dict[str, int] = {}
    for r in drop_rows:
        params = r.get("params") or {}
        try:
            n = int(params.get("n_dropped") or 0)
        except (TypeError, ValueError):
            n = 0
        total_dropped += n
        try:
            hr = int(params.get("headroom") or 0)
            if hr < min_headroom:
                min_headroom = hr
        except (TypeError, ValueError):
            pass
        # Each "dropped" entry is "action_type@timestamp"; tally by action_type.
        for entry in params.get("dropped") or []:
            kind = str(entry).split("@", 1)[0] or "unknown"
            drop_kinds[kind] = drop_kinds.get(kind, 0) + 1
    if total_dropped == 0:
        return None
    kinds_str = ", ".join(f"{n}× {k}" for k, n in sorted(drop_kinds.items()))
    return (
        f"- ⚠️ Daikin quota: dropped {total_dropped} low-value pair(s) "
        f"({kinds_str}); critical NEGATIVE/PEAK preserved (min headroom={min_headroom})"
    )


def _forecasted_export_for_day(day: date, tz: ZoneInfo) -> tuple[float, float, int]:
    """Estimate exported kWh + pence from committed peak_export decisions.

    Used as fallback when telemetry export is 0 on a day the LP committed
    peak_export slots — most likely a missing-sample window in
    ``pv_realtime_history``. Sums ``scen_pessimistic_exp_kwh`` (the safety-
    floor amount the LP guaranteed) for every committed peak_export slot
    whose local date matches ``day``, multiplied by per-slot
    ``agile_export_rates`` (flat ``EXPORT_RATE_PENCE`` fallback).

    Returns ``(kwh, pence, slot_count)`` or ``(0, 0, 0)``.
    """
    from datetime import UTC as _UTC

    start_utc = datetime.combine(day, datetime.min.time()).replace(tzinfo=tz).astimezone(_UTC)
    end_utc = start_utc + timedelta(days=1)
    try:
        commits = db.get_committed_peak_export_in_range(
            start_utc.isoformat(), end_utc.isoformat()
        )
    except Exception:
        return 0.0, 0.0, 0
    if not commits:
        return 0.0, 0.0, 0

    flat = float(config.EXPORT_RATE_PENCE)
    rate_by_start: dict[str, float] = {}
    if (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip():
        try:
            for r in db.get_agile_export_rates_in_range(
                start_utc.isoformat(), end_utc.isoformat()
            ):
                key = str(r["valid_from"]).replace("+00:00", "Z")
                rate_by_start[key] = float(r["value_inc_vat"])
        except Exception:
            pass

    total_kwh = 0.0
    total_p = 0.0
    for r in commits:
        kwh = float(r.get("scen_pessimistic_exp_kwh") or 0.0)
        if kwh <= 0:
            continue
        slot_iso = str(r["slot_time_utc"]).replace("+00:00", "Z")
        rate = rate_by_start.get(slot_iso, flat)
        total_kwh += kwh
        total_p += kwh * rate
    return total_kwh, total_p, len(commits)


# --------------------------------------------------------------------------
# Helpers — tier windows + peak-export read-out
# --------------------------------------------------------------------------

def _tariff_peak_windows_summary(day: date, tz: ZoneInfo) -> str | None:
    """Surface raw-tariff peak windows independent of LP slot classification.

    Originally the brief only quoted the LP's ``kind_counts`` summary
    (``peak=N``). That count is **dispatch action**, not tariff signal: when
    PV+battery cover load through expensive hours, slots stay ``standard`` and
    ``peak=0`` even though Octopus has a 36p evening. The family reads
    "peak=0" as "no expensive slots tomorrow", which is wrong.

    This helper queries Octopus rates directly for ``day`` and returns a one-line
    summary of consecutive slots ≥ ``BRIEF_TARIFF_PEAK_THRESHOLD_PENCE`` (default
    25 p). Returns None when the day has no peak window or rates aren't loaded.
    """
    threshold = float(getattr(config, "BRIEF_TARIFF_PEAK_THRESHOLD_PENCE", 25.0))
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return None
    try:
        rows = db.get_agile_rates_slots_for_local_day(tariff, day, tz_name=str(tz))
    except Exception:
        return None
    if not rows:
        return None
    peaks = [r for r in rows if float(r["value_inc_vat"]) >= threshold]
    if not peaks:
        return None
    # Find the longest contiguous block of peak slots (most useful summary)
    peak_starts = sorted(
        datetime.fromisoformat(str(r["valid_from"]).replace("Z", "+00:00"))
        for r in peaks
    )
    if not peak_starts:
        return None
    blocks: list[list[datetime]] = [[peak_starts[0]]]
    for ts in peak_starts[1:]:
        prev = blocks[-1][-1]
        if (ts - prev).total_seconds() <= 30 * 60 + 1:   # contiguous half-hours
            blocks[-1].append(ts)
        else:
            blocks.append([ts])
    longest = max(blocks, key=len)
    block_start_local = longest[0].astimezone(tz)
    block_end_local = (longest[-1] + timedelta(minutes=30)).astimezone(tz)
    max_p = max(float(r["value_inc_vat"]) for r in peaks)
    return (
        f"⚠️ Tariff peak: **{block_start_local:%H:%M}–{block_end_local:%H:%M}** "
        f"(max {max_p:.1f}p, {len(peaks)} slots ≥ {threshold:.0f}p)"
    )


def _today_tier_window_summary(today: date, tz: ZoneInfo) -> str | None:
    """Reuse the family-calendar tier classification for the morning brief.

    Pulls today's local-day Octopus slots and runs ``classify_day``. Returns
    None when no rates are loaded yet (cold-start day; fall through to the
    plain strategy line)."""
    try:
        from ..google_calendar.tiers import Slot, classify_day
    except Exception:
        return None

    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return None
    try:
        rows = db.get_agile_rates_slots_for_local_day(tariff, today, tz_name=str(tz))
    except Exception:
        return None
    if not rows:
        return None

    slots = [
        Slot(
            start_utc=datetime.fromisoformat(str(r["valid_from"]).replace("Z", "+00:00")),
            end_utc=datetime.fromisoformat(str(r["valid_to"]).replace("Z", "+00:00")),
            price_p=float(r["value_inc_vat"]),
        )
        for r in rows
    ]
    windows = classify_day(slots)
    if not windows:
        return None
    parts: list[str] = []
    for w in windows:
        local_start = w.start_utc.astimezone(tz).strftime("%H:%M")
        local_end = w.end_utc.astimezone(tz).strftime("%H:%M")
        parts.append(
            f"- {w.tier.emoji} {local_start}–{local_end} {w.tier.title} "
            f"({w.price_min:.1f}–{w.price_max:.1f}p)"
        )
    return "\n".join(parts)


def _slot_local_date(slot_time_utc: str, tz: ZoneInfo) -> date | None:
    """Convert a ``slot_time_utc`` string to its **local** date.

    DST audit fix (V12): the previous prefix-match on the ISO string
    ``startswith(today_iso)`` compared a UTC timestamp against a
    local-date prefix, which inverts after 23:00 UTC on DST changeover
    days (the local date has rolled but UTC hasn't, or vice versa).
    Always go through proper TZ conversion."""
    try:
        st = datetime.fromisoformat(slot_time_utc.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return st.astimezone(tz).date()


def _peak_export_commitments_for_today(today: date, tz: ZoneInfo) -> str | None:
    """Look up the latest LP run anchored to today's plan_date and report
    every committed ``peak_export`` slot the dispatcher kept after the
    robustness filter. Returns None when there's nothing to report."""
    try:
        rid = db.find_latest_optimizer_run_id()
        if rid is None:
            return None
        rows = db.get_dispatch_decisions(rid)
    except Exception:
        return None
    commits = [
        r for r in rows
        if r["lp_kind"] == "peak_export"
        and r["committed"]
        and _slot_local_date(str(r["slot_time_utc"]), tz) == today
    ]
    if not commits:
        return None
    parts: list[str] = []
    for r in commits:
        st = datetime.fromisoformat(str(r["slot_time_utc"]).replace("Z", "+00:00"))
        local = st.astimezone(tz).strftime("%H:%M")
        nom = r.get("scen_nominal_exp_kwh") or 0.0
        parts.append(f"- {local} export {float(nom):.2f} kWh ({r['reason']})")
    return "\n".join(parts)


def _peak_export_outcomes_for_today(today: date, tz: ZoneInfo) -> str | None:
    """Summarise every ``peak_export`` decision recorded for today —
    committed AND dropped. Helps the household see what arbitrage we took
    and what we declined for safety."""
    try:
        rid = db.find_latest_optimizer_run_id()
        if rid is None:
            return None
        rows = db.get_dispatch_decisions(rid)
    except Exception:
        return None
    pe_rows = [
        r for r in rows
        if r["lp_kind"] == "peak_export"
        and _slot_local_date(str(r["slot_time_utc"]), tz) == today
    ]
    if not pe_rows:
        return None
    parts: list[str] = []
    for r in pe_rows:
        st = datetime.fromisoformat(str(r["slot_time_utc"]).replace("Z", "+00:00"))
        local = st.astimezone(tz).strftime("%H:%M")
        flag = "✅" if r["committed"] else "❌"
        nom = r.get("scen_nominal_exp_kwh") or 0.0
        pess = r.get("scen_pessimistic_exp_kwh") or 0.0
        parts.append(
            f"- {flag} {local} planned {float(nom):.2f} kWh "
            f"(pessimistic {float(pess):.2f}; reason: {r['reason']})"
        )
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Public webhooks (called by cron jobs in src.scheduler.runner)
# --------------------------------------------------------------------------

def send_morning_brief_webhook() -> None:
    notify_morning_report(build_morning_payload())


def send_night_brief_webhook() -> None:
    notify_night_brief(build_night_payload())

