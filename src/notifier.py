"""Alert notifier — stdout always; optional OpenClaw webhook (webchat, Telegram, etc.)."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from . import db
from .config import config


class AlertType(str, Enum):
    MORNING_REPORT = "morning_report"
    STRATEGY_UPDATE = "strategy_update"
    RISK_ALERT = "risk_alert"
    ACTION_CONFIRMATION = "action_confirmation"
    CRITICAL_ERROR = "critical_error"


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
