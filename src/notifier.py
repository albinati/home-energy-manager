"""Alert notifier — stdout always; optional OpenClaw Gateway hook delivery.

Every alert is printed to stdout unconditionally and logged to action_log.
If OPENCLAW_NOTIFY_ENABLED=true, a target is configured, and OPENCLAW_HOOKS_URL
+ OPENCLAW_HOOKS_TOKEN are set, notifications POST to the Gateway ``/hooks/agent``
endpoint so an agent can shape the message before Telegram (see
docs/openclaw-nikola-plan-prompt.md). There is no ``openclaw message send`` path.

Routing is controlled per-AlertType via the `notification_routes` SQLite table
which can be updated at runtime through the MCP tools without restarting the
service.  See src/db.py: get_notification_route / upsert_notification_route.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from enum import Enum
from typing import Any

import requests

from . import db
from .config import config

logger = logging.getLogger(__name__)


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
    # V7 plan consent
    PLAN_PROPOSED = "plan_proposed"


# OpenClaw hook payload ``name`` field (one stable label per alert key)
_HOOK_PAYLOAD_NAMES: dict[str, str] = {
    "morning_report": "EnergyMorningReport",
    "strategy_update": "EnergyStrategyUpdate",
    "risk_alert": "EnergyRisk",
    "action_confirmation": "EnergyActionConfirmation",
    "critical_error": "EnergyCritical",
    "cheap_window_start": "EnergyCheapWindow",
    "peak_window_start": "EnergyPeakWindow",
    "daily_pnl": "EnergyDailyPnl",
    "plan_proposed": "EnergyPlan",
}


def _payload_name_for_key(alert_key: str) -> str:
    return _HOOK_PAYLOAD_NAMES.get(alert_key, "EnergyNotification")


# ---------------------------------------------------------------------------
# Route resolution (SQLite → env fallback)
# ---------------------------------------------------------------------------

def _resolve_route(alert_type: str) -> dict[str, Any] | None:
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

    row: dict[str, Any] | None = None
    try:
        row = db.get_notification_route(alert_type)
    except sqlite3.Error as exc:
        logger.warning("notification route lookup failed for %s: %s", alert_type, exc)

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


def _hooks_credentials_configured() -> bool:
    return bool(config.OPENCLAW_HOOKS_URL.strip() and config.OPENCLAW_HOOKS_TOKEN.strip())


# ---------------------------------------------------------------------------
# Hook delivery
# ---------------------------------------------------------------------------

def _truncate_for_webhook(s: str, max_chars: int = 6000) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 20] + "\n… [truncated]"


def _build_hook_payload(
    route: dict[str, Any],
    agent_message: str,
    payload_name: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": agent_message,
        "name": payload_name,
        "wakeMode": "now",
        "deliver": True,
        "channel": route.get("channel") or config.OPENCLAW_NOTIFY_CHANNEL,
        "timeoutSeconds": min(300, max(30, int(config.OPENCLAW_HOOKS_TIMEOUT_SECONDS) * 4)),
    }
    to = (route.get("target") or "").strip()
    if to:
        payload["to"] = to
    aid = (config.OPENCLAW_HOOKS_AGENT_ID or "").strip()
    if aid:
        payload["agentId"] = aid
    return payload


def _send_hooks_agent_post(payload: dict[str, Any]) -> bool:
    """Synchronous POST; returns True if HTTP 2xx."""
    url = config.OPENCLAW_HOOKS_URL.strip()
    token = config.OPENCLAW_HOOKS_TOKEN.strip()
    if not url or not token:
        return False
    try:
        r = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=float(config.OPENCLAW_HOOKS_TIMEOUT_SECONDS),
        )
        return 200 <= r.status_code < 300
    except requests.RequestException as exc:
        print(f"[openclaw hooks] request failed: {exc}")
        return False


def _enqueue_hooks_delivery(
    alert_type: str,
    payload_name: str,
    agent_message: str,
    route: dict[str, Any],
) -> None:
    """Fire-and-forget POST; logs on non-2xx — no alternate transport."""

    def _run() -> None:
        pl = _build_hook_payload(route, agent_message, payload_name)
        if _send_hooks_agent_post(pl):
            return
        print(f"[openclaw hooks] delivery failed or non-2xx (alert_type={alert_type})")

    t = threading.Thread(target=_run, daemon=True, name=f"openclaw-hooks-{alert_type}")
    t.start()


def _build_generic_agent_message(
    alert_key: str,
    full_msg: str,
    *,
    urgent: bool = False,
    silent: bool = False,
    extra: dict[str, Any] | None = None,
) -> str:
    lines = [
        "Home Energy Manager notification — summarize clearly for the user in natural language.",
        f"alert_type: {alert_key}",
        f"urgent: {urgent}",
    ]
    if silent:
        lines.append(
            "delivery_preference: silent (no notification sound if the channel supports it)"
        )
    lines.append("")
    lines.append(_truncate_for_webhook(full_msg))
    if extra:
        lines.append("\nextra:\n" + json.dumps(extra, default=str)[:500])
    return "\n".join(lines)


def _build_hooks_agent_message(
    plan_id: str,
    plan_date: str,
    summary: str,
    table: str,
    *,
    api_base: str,
) -> str:
    """Compact instructions + data for OpenClaw ``/hooks/agent`` (avoid 413)."""
    bulletproof_note = (
        "IMPORTANT: In Bulletproof mode the optimizer may have ALREADY applied Fox V3 and "
        "Daikin actions at propose time. Tools confirm_plan / reject_plan are for "
        "acknowledgement and MCP gating — do NOT tell the user they must approve to "
        "'apply' hardware unless the deployment explicitly uses a consent gate."
    )
    return (
        f"{bulletproof_note}\n\n"
        f"New energy plan — summarize for the user in natural language (no raw JSON dump).\n"
        f"- plan_id: {plan_id}\n"
        f"- plan_date: {plan_date}\n"
        f"- Fetch full JSON if needed: GET {api_base}/api/v1/optimization/plan\n"
        f"- MCP: same host exposes home-energy-manager tools.\n\n"
        f"Strategy summary:\n{summary}\n\n"
        f"Schedule preview (Daikin rows, local times):\n{_truncate_for_webhook(table)}\n"
    )


# ---------------------------------------------------------------------------
# Internal dispatcher (shared by all public helpers)
# ---------------------------------------------------------------------------

def _compose_delivery_body(
    kind: AlertType,
    message: str,
    *,
    urgent: bool = False,
    extra: dict[str, Any] | None = None,
) -> str:
    prefix = "[URGENT]" if urgent else "[info]"
    ts = datetime.now().strftime("%H:%M")
    meta = f" [{kind.value}]" if kind else ""
    full_msg = f"[{ts}] {prefix} energy-manager{meta}\n{message}"
    if extra:
        full_msg += "\n" + json.dumps(extra, default=str)[:500]
    return full_msg


def _record_notification(
    kind: AlertType,
    message: str,
    *,
    urgent: bool = False,
    extra: dict[str, Any] | None = None,
    full_msg: str | None = None,
) -> str:
    """Print, log to action_log; return *full_msg* for hook delivery."""
    body = full_msg if full_msg is not None else _compose_delivery_body(
        kind, message, urgent=urgent, extra=extra
    )
    print(body)

    try:
        db.log_action(
            device="system",
            action=kind.value,
            params={"message": message, "urgent": urgent, "extra": extra},
            result="success",
            trigger="notification",
        )
    except sqlite3.Error as exc:
        logger.warning("action_log failed for notification %s: %s", kind.value, exc)

    return body


def _dispatch(
    kind: AlertType,
    message: str,
    *,
    urgent: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    full_msg = _compose_delivery_body(kind, message, urgent=urgent, extra=extra)
    _record_notification(kind, message, urgent=urgent, extra=extra, full_msg=full_msg)
    route = _resolve_route(kind.value)
    if not route:
        return
    if not _hooks_credentials_configured():
        print(
            "[openclaw hooks] OPENCLAW_HOOKS_URL and OPENCLAW_HOOKS_TOKEN are required "
            f"for user delivery (alert={kind.value}; notification was logged above)."
        )
        return
    payload_name = _payload_name_for_key(kind.value)
    agent_msg = _build_generic_agent_message(
        kind.value,
        full_msg,
        urgent=urgent,
        silent=bool(route.get("silent")),
        extra=extra,
    )
    _enqueue_hooks_delivery(kind.value, payload_name, agent_msg, route)


# ---------------------------------------------------------------------------
# Public API (identical signatures to the old notifier — callers unchanged)
# ---------------------------------------------------------------------------

def notify(message: str, urgent: bool = False) -> None:
    """Send a notification. Always prints to stdout; delivers via hook if configured."""
    _dispatch(AlertType.RISK_ALERT if urgent else AlertType.ACTION_CONFIRMATION, message, urgent=urgent)


def notify_morning_report(body: str) -> None:
    _dispatch(AlertType.MORNING_REPORT, body, urgent=False)


def notify_strategy_update(summary: str, warnings: Any = None) -> None:
    msg = summary
    if warnings:
        msg += f"\nWarnings: {warnings}"
    _dispatch(AlertType.STRATEGY_UPDATE, msg, extra={"warnings": warnings} if warnings else None)


def notify_risk(message: str, extra: dict[str, Any] | None = None) -> None:
    _dispatch(AlertType.RISK_ALERT, message, urgent=True, extra=extra)


def notify_user_override(message: str) -> None:
    """Phase 4.3: one-shot notification when a Daikin user override is detected."""
    notify_action_confirmation(f"User override detected — {message}. Schedule will re-converge at next MPC replan.")


def notify_action_confirmation(message: str) -> None:
    _dispatch(AlertType.ACTION_CONFIRMATION, message, urgent=False)


def notify_critical(message: str) -> None:
    _dispatch(AlertType.CRITICAL_ERROR, message, urgent=True)


# ---------------------------------------------------------------------------
# V2: structured push events (Gateway hooks)
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
    except sqlite3.Error as exc:
        logger.warning("action_log failed for push_alert %s: %s", event_type, exc)

    route = _resolve_route(event_type)
    if not route:
        return False
    if not _hooks_credentials_configured():
        print(
            "[openclaw hooks] OPENCLAW_HOOKS_URL and OPENCLAW_HOOKS_TOKEN are required "
            f"for user delivery (alert={event_type}; notification was logged above)."
        )
        return False
    agent_msg = _build_generic_agent_message(
        event_type,
        full_msg,
        urgent=False,
        silent=bool(route.get("silent")),
        extra=payload,
    )
    pname = _payload_name_for_key(event_type)
    _enqueue_hooks_delivery(event_type, pname, agent_msg, route)
    return True


def push_cheap_window_start(soc: float | None = None, fox_mode: str | None = None) -> None:
    """Emit CHEAP_WINDOW_START event: battery charging and DHW heating active."""
    payload: dict[str, Any] = {
        "message": "Cheap window active. Forcing FoxESS charge, heating DHW.",
        "soc_percent": soc,
    }
    if fox_mode and fox_mode != "unknown":
        payload["fox_mode"] = fox_mode
    push_alert(AlertType.CHEAP_WINDOW_START.value, payload)


def push_peak_window_start(soc: float | None = None) -> None:
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


# ---------------------------------------------------------------------------
# V7: plan consent notification
# ---------------------------------------------------------------------------


def _format_plan_actions(actions: list[dict[str, Any]], tz_name: str = "Europe/London") -> str:
    """Render action_schedule rows as a compact human-readable table."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    lines: list[str] = []
    for a in sorted(actions, key=lambda x: x.get("start_time", "")):
        atype = a.get("action_type", "")
        if atype == "restore":
            continue
        try:
            st = datetime.fromisoformat(
                str(a["start_time"]).replace("Z", "+00:00")
            ).astimezone(tz).strftime("%H:%M")
            en = datetime.fromisoformat(
                str(a["end_time"]).replace("Z", "+00:00")
            ).astimezone(tz).strftime("%H:%M")
        except (ValueError, KeyError):
            continue
        p = a.get("params") or {}
        lwt = p.get("lwt_offset", 0)
        tank = p.get("tank_temp", "")
        tank_on = p.get("tank_power", True)
        details = f"LWT{lwt:+g}"
        if tank:
            details += f" tank={tank:.0f}C"
        if not tank_on:
            details += " DHW-off"
        lines.append(f"  {st}-{en}  {atype:<12s}  {details}")
    return "\n".join(lines) if lines else "  (no actions)"


def notify_plan_proposed(
    plan_id: str,
    plan_date: str,
    summary: str,
    actions: list[dict[str, Any]],
) -> None:
    """Send a PLAN_PROPOSED notification with the full schedule and approval instructions."""
    tz_name = getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    table = _format_plan_actions(actions, tz_name)
    msg = (
        f"New energy plan for {plan_date} — ID: {plan_id}\n"
        f"\n{table}\n"
        f"\n{summary}\n"
        f"\nTo activate: confirm_plan(\"{plan_id}\")\n"
        f"To reject:   reject_plan(\"{plan_id}\")\n"
        f"(Auto-activates in {config.PLAN_CONSENT_EXPIRY_SECONDS // 60} min if no response)"
    )
    full_msg = _compose_delivery_body(AlertType.PLAN_PROPOSED, msg, urgent=True, extra=None)
    _record_notification(AlertType.PLAN_PROPOSED, msg, urgent=True, extra=None, full_msg=full_msg)

    route = _resolve_route(AlertType.PLAN_PROPOSED.value)
    if not route:
        return
    if not _hooks_credentials_configured():
        print(
            "[openclaw hooks] OPENCLAW_HOOKS_URL and OPENCLAW_HOOKS_TOKEN are required "
            "for user delivery (plan_proposed; notification was logged above)."
        )
        return
    api_base = getattr(config, "OPENCLAW_INTERNAL_API_BASE_URL", "http://127.0.0.1:8000")
    agent_msg = _build_hooks_agent_message(
        plan_id,
        plan_date,
        summary,
        table,
        api_base=api_base,
    )
    _enqueue_hooks_delivery(
        AlertType.PLAN_PROPOSED.value,
        _payload_name_for_key(AlertType.PLAN_PROPOSED.value),
        agent_msg,
        route,
    )
