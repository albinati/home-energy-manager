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

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..notifier import notify_morning_report, notify_night_brief
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

    lines: list[str] = [
        "## ☀️ Morning brief",
        f"**Today ({today})**",
        strategy,
        "",
    ]
    if tier_summary:
        lines.extend(["**Tariff windows today:**", tier_summary, ""])
    if pe_summary:
        lines.extend(["**Peak-export plan:**", pe_summary, ""])

    lines.extend([
        f"**Yesterday ({yesterday}):** realised £{pnl['realised_cost_gbp']:.2f}, "
        f"vs SVT shadow £{pnl['delta_vs_svt_gbp']:+.2f}.",
    ])
    return "\n".join(lines)


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

    lines: list[str] = [
        "## 🌙 Night brief",
        f"**Today ({today})** — actuals",
        f"- Realised cost: £{pnl['realised_cost_gbp']:.2f}",
        f"- vs SVT shadow: £{pnl['delta_vs_svt_gbp']:+.2f}",
        f"- vs fixed shadow: £{pnl['delta_vs_fixed_gbp']:+.2f}",
        f"- VWAP: {vwap}p/kWh" if vwap else "- VWAP: n/a",
    ]
    if slip is not None:
        lines.append(f"- Slippage vs target: {slip}p")
    if arb is not None:
        lines.append(f"- Arbitrage efficiency (cheap quartile): {arb}%")
    lines.append(f"- SLA sample: {sla.get('sample_size', 0)} actions")
    if pe_today:
        lines.extend(["", "**Peak-export verdicts today:**", pe_today])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Helpers — tier windows + peak-export read-out
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Backwards-compatible aliases — pre-V12 callers
# --------------------------------------------------------------------------

# Kept so any external integration still calling the old names doesn't break;
# new code should use the explicit morning / night helpers.

build_daily_brief_text = build_morning_payload
send_daily_brief_webhook = send_morning_brief_webhook
