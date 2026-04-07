#!/usr/bin/env python3
"""Print a plain-text weekly energy report for the last 7 days to stdout.

Usage:
    python scripts/weekly_report.py
    python scripts/weekly_report.py --date 2026-03-07   # report for week containing that date
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is on sys.path so `src` is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.energy.monthly import get_period_insights


def _fmt(val: float | None, unit: str = "kWh", decimals: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{decimals}f} {unit}"


def _fmt_pence(val: float | None) -> str:
    if val is None:
        return "n/a"
    if abs(val) >= 100:
        return f"£{val / 100:.2f}"
    return f"{val:.1f}p"


def _bar(value: float, max_value: float, width: int = 20) -> str:
    if max_value <= 0:
        return " " * width
    filled = int(round(value / max_value * width))
    filled = max(0, min(filled, width))
    return "█" * filled + "░" * (width - filled)


def generate_report(reference_date: date) -> str:
    """Fetch weekly data and return a formatted text report."""
    pi = get_period_insights(period="week", date_str=reference_date.isoformat())
    if pi is None:
        return "ERROR: Could not fetch energy data. Check Fox ESS configuration."

    e = pi.insights.energy
    c = pi.insights.cost
    ha = pi.heating_analytics
    daily = pi.chart_data

    lines: list[str] = []
    sep = "=" * 60

    lines += [
        sep,
        f"  HOME ENERGY REPORT  ·  {pi.period_label}",
        sep,
        "",
    ]

    # ── Energy totals ──────────────────────────────────────────────
    lines += [
        "ENERGY TOTALS",
        "-" * 40,
        f"  Solar generated  : {_fmt(e.solar_kwh)}",
        f"  Home consumption : {_fmt(e.load_kwh)}",
        f"  Grid import      : {_fmt(e.import_kwh)}",
        f"  Grid export      : {_fmt(e.export_kwh)}",
        f"  Battery charged  : {_fmt(e.charge_kwh)}",
        f"  Battery discharged: {_fmt(e.discharge_kwh)}",
        "",
    ]

    # Self-sufficiency
    if e.load_kwh and e.load_kwh > 0:
        grid_share = e.import_kwh / e.load_kwh * 100
        self_suff = max(0.0, 100.0 - grid_share)
        lines += [
            "SELF-SUFFICIENCY",
            "-" * 40,
            f"  {self_suff:.1f}% of consumption met without grid import",
            f"  [{_bar(self_suff, 100, 30)}] {self_suff:.0f}%",
            "",
        ]

    # ── Cost summary ───────────────────────────────────────────────
    if c.import_cost_pence or c.export_earnings_pence or c.standing_charge_pence:
        lines += [
            "COST SUMMARY",
            "-" * 40,
            f"  Import cost      : {_fmt_pence(c.import_cost_pence)}",
            f"  Export earnings  : {_fmt_pence(c.export_earnings_pence)}",
            f"  Standing charges : {_fmt_pence(c.standing_charge_pence)}",
            f"  Net cost         : {_fmt_pence(c.net_cost_pence)}",
            "",
        ]
    else:
        lines += [
            "COST SUMMARY",
            "-" * 40,
            "  (No tariff rates configured — set MANUAL_TARIFF_IMPORT_PENCE etc. in .env)",
            "",
        ]

    # ── Heating estimate ───────────────────────────────────────────
    hi = pi.insights
    if hi.heating_estimate_kwh is not None:
        lines += [
            "HEATING (ASHP ESTIMATE)",
            "-" * 40,
            f"  Heating energy   : {_fmt(hi.heating_estimate_kwh)}",
        ]
        if hi.heating_estimate_cost_pence is not None:
            lines.append(f"  Heating cost     : {_fmt_pence(hi.heating_estimate_cost_pence)}")
        if hi.equivalent_gas_cost_pence is not None:
            lines.append(f"  Equiv. gas cost  : {_fmt_pence(hi.equivalent_gas_cost_pence)}")
        if hi.gas_comparison_ahead_pounds is not None:
            sign = "+" if hi.gas_comparison_ahead_pounds >= 0 else ""
            lines.append(f"  Saving vs gas    : {sign}£{hi.gas_comparison_ahead_pounds:.2f}")
        if ha and ha.heating_percent_of_consumption is not None:
            lines.append(f"  Heating share    : {ha.heating_percent_of_consumption:.1f}% of total load")
        lines.append("")

    # ── Weather ────────────────────────────────────────────────────
    if ha and ha.avg_outdoor_temp_c is not None:
        lines += [
            "WEATHER",
            "-" * 40,
            f"  Avg outdoor temp : {ha.avg_outdoor_temp_c:.1f}°C",
        ]
        if ha.degree_days is not None:
            lines.append(f"  Degree-days (HDD): {ha.degree_days:.1f}")
        if ha.cost_per_degree_day_pounds is not None:
            lines.append(f"  Cost/degree-day  : £{ha.cost_per_degree_day_pounds:.3f}")
        lines.append("")

    # ── Daily breakdown ────────────────────────────────────────────
    if daily:
        max_load = max((r.get("load_kwh", 0) or 0 for r in daily), default=1)
        lines += [
            "DAILY BREAKDOWN",
            "-" * 60,
            f"  {'Date':<12} {'Solar':>7} {'Import':>7} {'Export':>7} {'Load':>7}  Chart",
            f"  {'-'*11} {'-'*7} {'-'*7} {'-'*7} {'-'*7}  {'─'*20}",
        ]
        for row in daily:
            sol = row.get("solar_kwh") or 0
            imp = row.get("import_kwh") or 0
            exp = row.get("export_kwh") or 0
            load = row.get("load_kwh") or 0
            bar = _bar(load, max_load, 20)
            lines.append(
                f"  {row['date']:<12} {sol:>6.1f}k {imp:>6.1f}k {exp:>6.1f}k {load:>6.1f}k  {bar}"
            )
        lines.append("")

    lines += [sep, "  Generated by home-energy-manager  ·  London W4", sep]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly home energy report")
    parser.add_argument(
        "--date",
        default=None,
        help="Any date within the target week (YYYY-MM-DD). Defaults to last week.",
    )
    args = parser.parse_args()

    if args.date:
        ref = date.fromisoformat(args.date)
    else:
        # Default: the most recently completed week (Mon–Sun ending yesterday)
        today = date.today()
        # Go to last Monday
        days_since_monday = today.weekday()  # Mon=0
        last_monday = today - timedelta(days=days_since_monday + 7)
        ref = last_monday

    print(generate_report(ref))


if __name__ == "__main__":
    main()
