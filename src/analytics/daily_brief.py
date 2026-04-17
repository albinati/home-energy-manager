"""08:00 hedge-fund-style brief: yesterday PnL + today strategy."""
from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from ..config import config
from .. import db
from ..notifier import notify_morning_report
from .pnl import (
    compute_arbitrage_efficiency,
    compute_daily_pnl,
    compute_slippage,
    compute_vwap,
)
from .sla import compute_sla_metrics


def build_daily_brief_text() -> str:
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    today = datetime_now_local_date(tz)
    yesterday = today - timedelta(days=1)
    pnl = compute_daily_pnl(yesterday)
    vwap = compute_vwap(yesterday)
    slip = compute_slippage(yesterday)
    arb = compute_arbitrage_efficiency(yesterday)
    sla = compute_sla_metrics(limit=200)
    tgt = db.get_daily_target(today)
    strategy = (tgt or {}).get("strategy_summary") or "No strategy row for today yet."
    lines = [
        "## Daily brief",
        f"**Yesterday ({yesterday})**",
        f"- Realised cost: £{pnl['realised_cost_gbp']:.2f}",
        f"- vs SVT shadow: £{pnl['delta_vs_svt_gbp']:+.2f}",
        f"- vs fixed shadow: £{pnl['delta_vs_fixed_gbp']:+.2f}",
        f"- VWAP: {vwap}p/kWh" if vwap else "- VWAP: n/a",
        f"- Slippage vs target: {slip}p" if slip is not None else "",
        f"- Arbitrage efficiency (cheap quartile): {arb}%" if arb is not None else "",
        f"- SLA sample: {sla.get('sample_size', 0)} actions",
        "",
        f"**Today**",
        strategy,
    ]
    return "\n".join(x for x in lines if x is not None)


def datetime_now_local_date(tz: ZoneInfo) -> date:
    from datetime import datetime

    return datetime.now(tz).date()


def send_daily_brief_webhook() -> None:
    notify_morning_report(build_daily_brief_text())
