"""Alert notifier — stdout always; optional OpenClaw CLI delivery.

Every alert is printed to stdout unconditionally and logged to action_log.
If OPENCLAW_NOTIFY_ENABLED=true and a target is configured, it is also sent
via `openclaw message send` (subprocess — no HTTP API keys required).

Routing is controlled per-AlertType via the `notification_routes` SQLite table
which can be updated at runtime through the MCP tools without restarting the
service.  See src/db.py: get_notification_route / upsert_notification_route.
"""
from __future__ import annotations

import json
import subprocess
import threading
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
    # V2 push events
    CHEAP_WINDOW_START = "cheap_window_start"
    PEAK_WINDOW_START = "peak_window_start"
    DAILY_PNL = "daily_pnl"


# ---------------------------------------------------------------------------
# Route resolution (SQLite → env fallback)
# ---------------------------------------------------------------------------

def _resolve_route(alert_type: str) -> Optional[dict[str, Any]]:
    """Return {channel, target, silent} for *alert_type* or None if disabled/unconfigured.

    Resolution order:
    1. notification_routes row: if enabled=0, return None (muted).
    2. target_override / channel_override from the row, if set.
    3. Severity-specific env vars (OPENCLAW_NOTIFY_TARGET_CRITICAL, etc.).
    4. Default env vars (OPENCLAW_NOTIFY_TARGET, OPENCLAW_NOTIFY_CHANNEL).
    5. If no target, return None (silently skip delivery).
    """
    if not config.OPENCLAW_NOTIFY_ENABLED:
        return None

    row: Optional[dict[str, Any]] = None
    try:
        row = db.get_notification_route(alert_type)
    except Exception:
        pass

    if row is not None and not row.get("enabled", 1):
        return None

    severity: str = (row or {}).get("severity") or "reports"

    # Resolve target
    target: str = (
        (row or {}).get("target_override") or ""
        or (
            config.OPENCLAW_NOTIFY_TARGET_CRITICAL
            if severity == "critical"
            else config.OPENCLAW_NOTIFY_TARGET_REPORTS
        )
        or config.OPENCLAW_NOTIFY_TARGET
    ).strip()

    # Resolve channel
    channel: str = (
        (row or {}).get("channel_override") or ""
        or (
            config.OPENCLAW_NOTIFY_CHANNEL_CRITICAL
            if severity == "critical"
            else config.OPENCLAW_NOTIFY_CHANNEL_REPORTS
        )
        or config.OPENCLAW_NOTIFY_CHANNEL
    ).strip()

    if not target:
        return None

    silent: bool = bool((row or {}).get("silent", 0))
    return {"channel": channel, "target": target, "silent": silent}


# ---------------------------------------------------------------------------
# Subprocess sender
# ---------------------------------------------------------------------------

def _send_via_openclaw_cli(alert_type: str, message: str) -> bool:
    """Enqueue *message* for delivery via `openclaw message send` in a daemon thread.

    Returns True immediately (fire-and-forget) so the caller is never blocked by
    the ~40 s Telegram round-trip.  The background thread logs failures to stdout.
    """
    route = _resolve_route(alert_type)
    if not route:
        return False

    cmd = [
        config.OPENCLAW_CLI_PATH, "message", "send",
        "--channel", route["channel"],
        "--target", route["target"],
        "--message", message,
    ]
    if route["silent"]:
        cmd.append("--silent")

    def _run() -> None:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.OPENCLAW_CLI_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                stderr_snippet = (result.stderr or "").strip()[:200]
                print(f"[openclaw send failed] {alert_type}: {stderr_snippet}")
        except subprocess.TimeoutExpired:
            print(f"[openclaw timeout] {alert_type} (>{config.OPENCLAW_CLI_TIMEOUT_SECONDS}s)")
        except FileNotFoundError:
            print(f"[openclaw cli missing] {config.OPENCLAW_CLI_PATH}")
        except Exception as exc:
            print(f"[openclaw error] {alert_type}: {exc}")

    t = threading.Thread(target=_run, daemon=True, name=f"openclaw-{alert_type}")
    t.start()
    return True


# ---------------------------------------------------------------------------
# Internal dispatcher (shared by all public helpers)
# ---------------------------------------------------------------------------

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

    _send_via_openclaw_cli(kind.value, full_msg)


# ---------------------------------------------------------------------------
# Public API (identical signatures to the old notifier — callers unchanged)
# ---------------------------------------------------------------------------

def notify(message: str, urgent: bool = False) -> None:
    """Send a notification. Always prints to stdout; delivers via OpenClaw CLI if configured."""
    _dispatch(AlertType.RISK_ALERT if urgent else AlertType.ACTION_CONFIRMATION, message, urgent=urgent)


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


# ---------------------------------------------------------------------------
# V2: structured push-webhook helpers (now delivered via CLI instead of HTTP)
# ---------------------------------------------------------------------------

def push_alert(event_type: str, payload: dict[str, Any]) -> bool:
    """Push a structured event notification.

    event_type values:
    - ``CHEAP_WINDOW_START``  — battery charging / DHW heating active
    - ``PEAK_WINDOW_START``   — house shielded, Daikin suspended
    - ``DAILY_PNL``           — hedge-fund style D-1 financial report

    Failures are caught and logged; never raises.
    """
    ts = datetime.now().strftime("%H:%M")
    payload_snippet = json.dumps(payload, default=str)[:500]
    full_msg = f"[{ts}] [info] energy-manager [{event_type}]\n{payload_snippet}"
    print(f"[push_alert] {event_type}: {payload_snippet[:200]}")

    try:
        db.log_action(
            device="system",
            action=event_type,
            params=payload,
            result="success",
            trigger="notification",
        )
    except Exception:
        pass

    return _send_via_openclaw_cli(event_type, full_msg)


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
    """Emit DAILY_PNL report: hedge-fund format with PnL, VWAP, slippage."""
    push_alert(AlertType.DAILY_PNL.value, metrics)
