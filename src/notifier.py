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
    # V12 — twice-daily digest split + tier-aware policy
    NIGHT_BRIEF = "night_brief"
    PLAN_REVISION = "plan_revision"
    NEGATIVE_WINDOW_START = "negative_window_start"
    # PR #234 — appliance lifecycle (laundry start/finish)
    APPLIANCE_STARTING = "appliance_starting"
    APPLIANCE_FINISHED = "appliance_finished"


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
    "night_brief": "EnergyNightBrief",
    "plan_revision": "EnergyPlanRevision",
    "negative_window_start": "EnergyNegativeWindow",
    "appliance_starting": "EnergyApplianceStart",
    "appliance_finished": "EnergyApplianceFinish",
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
    *,
    extra: dict[str, Any] | None = None,
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
    if extra:
        payload.update(extra)
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
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget POST; logs on non-2xx — no alternate transport."""

    def _run() -> None:
        pl = _build_hook_payload(route, agent_message, payload_name, extra=extra)
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


def notify_night_brief(body: str) -> None:
    """Companion to ``notify_morning_report`` — fires once per evening with
    today's actuals (realised cost, savings vs SVT, peak-export verdicts).

    Routed via the same OpenClaw delivery path as the morning report; routes
    can be configured per-AlertType in the ``notification_routes`` table.
    """
    _dispatch(AlertType.NIGHT_BRIEF, body, urgent=False)


def notify_appliance_starting(
    *,
    appliance_name: str,
    planned_start_local: str,
    deadline_local: str,
    avg_price_pence: float,
    duration_minutes: int,
    brief_md: str | None = None,
) -> None:
    """🧺 Cycle is starting now (LP-armed cron just fired ``setMachineState run``).

    Inlines a 4-line forward-looking brief so the family sees today + tomorrow
    tariff windows and the running PnL alongside the start confirmation.
    """
    body_lines = [
        f"🧺 **{appliance_name}** starting now",
        f"Window: {planned_start_local} → end ≤ {deadline_local}",
        f"Cycle: {duration_minutes} min · avg {avg_price_pence:.1f}p/kWh",
    ]
    if brief_md:
        body_lines.extend(["", brief_md])
    body = "\n".join(body_lines)
    extra = {
        "appliance": appliance_name,
        "planned_start_local": planned_start_local,
        "deadline_local": deadline_local,
        "avg_price_pence": round(float(avg_price_pence), 2),
        "duration_minutes": int(duration_minutes),
    }
    _dispatch(AlertType.APPLIANCE_STARTING, body, urgent=False, extra=extra)


def notify_appliance_finished(
    *,
    appliance_name: str,
    started_local: str,
    ended_local: str,
    duration_minutes: int,
    avg_price_pence: float | None = None,
    estimated_kwh: float | None = None,
    estimated_cost_p: float | None = None,
    brief_md: str | None = None,
    kwh_is_measured: bool = False,
) -> None:
    """✅ Cycle has finished (poll detected state transition out of ``run``).

    Includes a concise outcome line + same 4-line brief so the family closes
    the loop on cost and sees what's coming next.

    ``kwh_is_measured`` distinguishes a real measurement (Samsung
    ``powerConsumptionReport`` delta — PR #235) from a static
    ``typical_kw × duration`` fallback. When measured, the message renders
    without the ``≈`` ambiguity prefix and the structured extra carries
    ``actual_kwh`` / ``actual_cost_pence`` instead of the ``estimated_*`` keys.
    """
    if estimated_kwh is not None and estimated_cost_p is not None:
        if kwh_is_measured:
            cost_line = f"{estimated_kwh:.2f} kWh · {estimated_cost_p:.1f}p"
        else:
            cost_line = f"≈ {estimated_kwh:.2f} kWh ≈ {estimated_cost_p:.1f}p"
    elif estimated_kwh is not None:
        prefix = "" if kwh_is_measured else "≈ "
        cost_line = f"{prefix}{estimated_kwh:.2f} kWh"
    elif avg_price_pence is not None:
        cost_line = f"avg {avg_price_pence:.1f}p/kWh"
    else:
        cost_line = ""
    body_lines = [
        f"✅ **{appliance_name}** cycle complete",
        f"{started_local} → {ended_local} ({duration_minutes} min)" + (f" · {cost_line}" if cost_line else ""),
    ]
    if brief_md:
        body_lines.extend(["", brief_md])
    body = "\n".join(body_lines)
    extra = {
        "appliance": appliance_name,
        "started_local": started_local,
        "ended_local": ended_local,
        "duration_minutes": int(duration_minutes),
        "kwh_is_measured": bool(kwh_is_measured),
    }
    if estimated_kwh is not None:
        key = "actual_kwh" if kwh_is_measured else "estimated_kwh"
        extra[key] = round(float(estimated_kwh), 3)
    if estimated_cost_p is not None:
        key = "actual_cost_pence" if kwh_is_measured else "estimated_cost_pence"
        extra[key] = round(float(estimated_cost_p), 2)
    _dispatch(AlertType.APPLIANCE_FINISHED, body, urgent=False, extra=extra)


def notify_plan_revision(body: str, *, trigger_reason: str | None = None) -> None:
    """Fires when an in-day MPC re-solve materially changed the plan.

    ``trigger_reason`` is what surfaced the revision (e.g. ``forecast_revision``,
    ``soc_drift``, ``tier_boundary``); recorded in the payload so OpenClaw can
    explain *why* a revision happened. Non-urgent — these are FYI-grade pings,
    not alerts.
    """
    extra = {"trigger_reason": trigger_reason} if trigger_reason else None
    _dispatch(AlertType.PLAN_REVISION, body, urgent=False, extra=extra)


def push_negative_window_start(
    *,
    soc: float | None = None,
    fox_mode: str | None = None,
    price_pence: float | None = None,
) -> None:
    """🔵 PAID-to-use window has just started. Always fires (rare, actionable).

    Mirrors the family-calendar tier copy so the same word ("PAID") reaches
    Telegram and the calendar — household members can run laundry / dishwasher /
    EV charge during the window.
    """
    payload: dict[str, Any] = {
        "title": "🔵 PAID to use — negative-price window started",
        # Body suggests boosting the DHW tank manually via the Daikin app —
        # because we deliberately keep DAIKIN_CONTROL_MODE=passive (we only
        # observe / forecast Daikin via the heating-curve function, not
        # control it). Negative-price slots are the one moment a manual
        # tank boost is high-value — the user gets paid to reheat.
        "body": (
            "Octopus is paying us to consume right now. Good moment to "
            "boost the hot-water tank from the Daikin app and run heavy "
            "appliances (laundry, dishwasher, EV charge)."
        ),
    }
    if soc is not None:
        payload["soc_pct"] = soc
    if fox_mode is not None:
        payload["fox_mode"] = fox_mode
    if price_pence is not None:
        payload["price_pence"] = price_pence
    push_alert(AlertType.NEGATIVE_WINDOW_START.value, payload)


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
    """Send a PLAN_PROPOSED notification with the full schedule and approval instructions.

    Payload advertises ``autoAcceptOnTimeout: true`` and ``approvalTimeoutSeconds``
    so interactive channels (Telegram/Discord) can surface accept/reject buttons
    that default to *accept* on timeout — the plan goes live unless the user
    rejects it in the grace window. OpenClaw implements the UI and the timeout.
    """
    tz_name = getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    table = _format_plan_actions(actions, tz_name)
    approval_timeout_s = int(getattr(config, "PLAN_APPROVAL_TIMEOUT_SECONDS", 300))
    msg = (
        f"New energy plan for {plan_date} — ID: {plan_id}\n"
        f"\n{table}\n"
        f"\n{summary}\n"
        f"\nTo activate: confirm_plan(\"{plan_id}\")\n"
        f"To reject:   reject_plan(\"{plan_id}\")\n"
        f"(Auto-activates in {approval_timeout_s // 60} min if no response)"
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
    extra = {
        "approvalTimeoutSeconds": approval_timeout_s,
        "autoAcceptOnTimeout": True,
        "planId": plan_id,
        "planDate": plan_date,
        "confirmTool": "confirm_plan",
        "rejectTool": "reject_plan",
    }
    _enqueue_hooks_delivery(
        AlertType.PLAN_PROPOSED.value,
        _payload_name_for_key(AlertType.PLAN_PROPOSED.value),
        agent_msg,
        route,
        extra=extra,
    )
