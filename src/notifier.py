"""Alert notifier — stdout always; optional OpenClaw webhook (webchat, Telegram, etc.)."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from . import db
from .config import config

# V2: dedicated push-webhook endpoint for energy state events
_ENERGY_WEBHOOK_URL = "http://127.0.0.1:18789/api/webhook/energy_alert"


class AlertType(str, Enum):
    MORNING_REPORT = "morning_report"
    STRATEGY_UPDATE = "strategy_update"
    RISK_ALERT = "risk_alert"
    ACTION_CONFIRMATION = "action_confirmation"
    CRITICAL_ERROR = "critical_error"
    # V2 push events
    CHEAP_WINDOW_START = "cheap_window_start"
    PEAK_WINDOW_START = "peak_window_start"
    DAILY_PNL = "daily_pnl"


def notify(message: str, urgent: bool = False) -> None:
    """Send a notification. Always prints to stdout; if ALERT_CHANNEL is set, sends to OpenClaw webhook."""
    _dispatch(AlertType.RISK_ALERT if urgent else AlertType.ACTION_CONFIRMATION, message, urgent=urgent)


def _dispatch(
    kind: AlertType,
    message: str,
    *,
    urgent: bool = False,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    prefix = "[URGENT]" if urgent else "[info]"
    ts = datetime.now().strftime("%H:%M")
    meta = f" [{kind.value}]" if kind else ""
    full_msg = f"[{ts}] {prefix} energy-manager{meta}\n{message}"
    if extra:
        full_msg += "\n" + json.dumps(extra, default=str)[:500]
    print(full_msg)

    try:
        db.log_action(
            device="system",
            action=kind.value,
            params={"message": message, "urgent": urgent, "extra": extra},
            result="success",
            trigger="notification",
        )
    except Exception:
        pass

    if config.ALERT_CHANNEL:
        _send_via_openclaw(message=full_msg)


def notify_morning_report(body: str) -> None:
    _dispatch(AlertType.MORNING_REPORT, body, urgent=False)


def notify_strategy_update(summary: str, warnings: Any = None) -> None:
    msg = summary
    if warnings:
        msg += f"\nWarnings: {warnings}"
    _dispatch(AlertType.STRATEGY_UPDATE, msg, extra={"warnings": warnings} if warnings else None)


def notify_risk(message: str, extra: Optional[dict[str, Any]] = None) -> None:
    _dispatch(AlertType.RISK_ALERT, message, urgent=True, extra=extra)


def notify_action_confirmation(message: str) -> None:
    _dispatch(AlertType.ACTION_CONFIRMATION, message, urgent=False)


def notify_critical(message: str) -> None:
    _dispatch(AlertType.CRITICAL_ERROR, message, urgent=True)


def _send_via_openclaw(message: str) -> bool:
    """Send via OpenClaw gateway (ALERT_OPENCLAW_URL). Uses ALERT_CHANNEL (e.g. webchat, telegram)."""
    try:
        payload: dict = {"message": message}
        if config.ALERT_CHANNEL:
            payload["channel"] = config.ALERT_CHANNEL
        req = urllib.request.Request(
            config.ALERT_OPENCLAW_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# V2: structured push-webhook helpers
# ---------------------------------------------------------------------------

def push_alert(event_type: str, payload: dict[str, Any]) -> bool:
    """Push a structured event to the energy alert webhook endpoint.

    event_type values:
    - ``CHEAP_WINDOW_START``  — battery charging / DHW heating active
    - ``PEAK_WINDOW_START``   — house shielded, Daikin suspended
    - ``DAILY_PNL``           — hedge-fund style D-1 financial report

    Failures are caught and logged; never raises.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    body = {"type": event_type, "ts": ts, "data": payload}
    print(f"[push_alert] {event_type}: {json.dumps(payload, default=str)[:200]}")
    try:
        req = urllib.request.Request(
            _ENERGY_WEBHOOK_URL,
            data=json.dumps(body, default=str).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as exc:
        print(f"Webhook push failed ({event_type}): {exc}. State logged locally.")
        return False


def push_cheap_window_start(soc: Optional[float] = None, fox_mode: Optional[str] = None) -> None:
    """Emit CHEAP_WINDOW_START event: battery charging and DHW heating active."""
    push_alert(
        AlertType.CHEAP_WINDOW_START.value,
        {
            "message": "Cheap window active. Forcing FoxESS charge, heating DHW.",
            "soc_percent": soc,
            "fox_mode": fox_mode,
        },
    )


def push_peak_window_start(soc: Optional[float] = None) -> None:
    """Emit PEAK_WINDOW_START event: house shielded, Daikin suspended."""
    push_alert(
        AlertType.PEAK_WINDOW_START.value,
        {
            "message": f"Peak window active. House shielded. SoC is {soc}%. Daikin heating suspended.",
            "soc_percent": soc,
        },
    )


def push_daily_pnl(metrics: dict[str, Any]) -> None:
    """Emit DAILY_PNL report: hedge-fund format with PnL, VWAP, slippage.

    *metrics* should include: date, total_kwh, total_cost_pence, total_saving_pence,
    vwap_pence, svt_vwap_pence, slippage_pence.
    """
    push_alert(AlertType.DAILY_PNL.value, metrics)
