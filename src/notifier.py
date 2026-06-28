"""Alert notifier — stdout always; Telegram-direct or OpenClaw-hook delivery.

Every alert is printed to stdout unconditionally and logged to action_log.

Two delivery transports are supported, in priority order:

1. **Direct Telegram Bot API** — when ``TELEGRAM_BOT_TOKEN`` +
   ``TELEGRAM_CHAT_ID`` are set, ``src/telegram_transport.py`` POSTs
   straight to ``api.telegram.org``. No LLM in the loop.
2. **OpenClaw Gateway hook** — fallback path when Telegram is not
   configured. POSTs to ``OPENCLAW_HOOKS_URL`` (e.g. ``/hooks/agent``)
   so an agent can shape the message before Telegram. This costs an
   Anthropic API call per notification.

``OPENCLAW_NOTIFY_ENABLED=false`` mutes both paths (stdout + action_log
keep running). Per-AlertType routing — enable/disable, severity, silent
flag — still flows through the ``notification_routes`` SQLite table; see
``src/db.py: get_notification_route / upsert_notification_route``.
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
import threading
from datetime import datetime
from enum import Enum
from typing import Any

import requests

from . import db, telegram_transport
from .config import config

logger = logging.getLogger(__name__)


class AlertType(str, Enum):
    MORNING_REPORT = "morning_report"
    RISK_ALERT = "risk_alert"
    ACTION_CONFIRMATION = "action_confirmation"
    CRITICAL_ERROR = "critical_error"
    # V2 push events
    CHEAP_WINDOW_START = "cheap_window_start"
    PEAK_WINDOW_START = "peak_window_start"
    # V7 plan consent — fires only on the nightly plan_push (in-day MPC re-solves
    # auto-apply silently; users pull the new plan via get_plan_timeline / UI).
    PLAN_PROPOSED = "plan_proposed"
    # V12 — twice-daily digest split + tier-aware policy
    NIGHT_BRIEF = "night_brief"
    NEGATIVE_WINDOW_START = "negative_window_start"
    # PR #234 — appliance lifecycle (laundry start/finish)
    APPLIANCE_STARTING = "appliance_starting"
    APPLIANCE_FINISHED = "appliance_finished"
    APPLIANCE_ARMED = "appliance_armed"
    APPLIANCE_CANCELLED = "appliance_cancelled"
    # 2026-06-07 — proactive "load the machine for an upcoming cheap/negative
    # window" nudge (the user must physically load + Smart-Control; HEM can only
    # prompt). Negative-only by default; debounced once per appliance per window.
    APPLIANCE_WINDOW_NUDGE = "appliance_window_nudge"
    # 2026-05-21 — LP solver failure (Infeasible / CBC crash). Default route:
    # ``severity=critical`` so it bypasses the morning-brief mute; rate-limited
    # by hash so a recurring failure across MPC re-solves only pages once.
    LP_FAILURE = "lp_failure"


# OpenClaw hook payload ``name`` field (one stable label per alert key)
_HOOK_PAYLOAD_NAMES: dict[str, str] = {
    "morning_report": "EnergyMorningReport",
    "risk_alert": "EnergyRisk",
    "action_confirmation": "EnergyActionConfirmation",
    "critical_error": "EnergyCritical",
    "cheap_window_start": "EnergyCheapWindow",
    "peak_window_start": "EnergyPeakWindow",
    "plan_proposed": "EnergyPlan",
    "night_brief": "EnergyNightBrief",
    "negative_window_start": "EnergyNegativeWindow",
    "appliance_starting": "EnergyApplianceStart",
    "appliance_finished": "EnergyApplianceFinish",
    "appliance_armed": "EnergyApplianceArmed",
    "appliance_cancelled": "EnergyApplianceCancelled",
    "appliance_window_nudge": "EnergyApplianceNudge",
    "lp_failure": "EnergyLPFailure",
}


def _payload_name_for_key(alert_key: str) -> str:
    return _HOOK_PAYLOAD_NAMES.get(alert_key, "EnergyNotification")


# Per-alert headline shown at the top of the Telegram message. Used only on the
# direct-Telegram path; the OpenClaw hook path uses ``_HOOK_PAYLOAD_NAMES``.
_TELEGRAM_HEADERS: dict[str, str] = {
    "morning_report": "🌅 Morning brief",
    "night_brief": "🌙 Night brief",
    "risk_alert": "⚠️ Risk alert",
    "action_confirmation": "✅ Action",
    "critical_error": "🚨 Critical error",
    "cheap_window_start": "💚 Cheap window",
    "peak_window_start": "🔴 Peak window",
    "negative_window_start": "🔵 PAID-to-use window",
    "plan_proposed": "📋 New energy plan",
    "appliance_starting": "🧺 Appliance starting",
    "appliance_finished": "✅ Appliance finished",
    "appliance_armed": "🧺 Appliance armed",
    "appliance_cancelled": "🚫 Appliance cancelled",
    "appliance_window_nudge": "🧺⚡ Carregue a máquina",
    "lp_failure": "🚨 LP solver failure",
}


def _telegram_header_for(alert_key: str) -> str:
    return _TELEGRAM_HEADERS.get(alert_key, "Home Energy Manager")


# ---------------------------------------------------------------------------
# Appliance fanout — additional Telegram chats for lifecycle events
# ---------------------------------------------------------------------------

_APPLIANCE_FANOUT_ALERT_KEYS: frozenset[str] = frozenset({
    AlertType.APPLIANCE_ARMED.value,
    AlertType.APPLIANCE_STARTING.value,
    AlertType.APPLIANCE_FINISHED.value,
    AlertType.APPLIANCE_CANCELLED.value,
    AlertType.APPLIANCE_WINDOW_NUDGE.value,
})


def _appliance_fanout_chat_ids() -> list[str]:
    """Return extra Telegram chat IDs to fan appliance notifications out to.

    Reads CSV from ``config.TELEGRAM_APPLIANCE_FANOUT_CHAT_IDS``. Empty
    strings are filtered; the primary ``TELEGRAM_CHAT_ID`` is removed so
    listing it here doesn't cause a duplicate send. Order is preserved
    so the user can predict delivery order from the CSV.
    """
    raw = (getattr(config, "TELEGRAM_APPLIANCE_FANOUT_CHAT_IDS", "") or "").strip()
    if not raw:
        return []
    primary = (getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
    seen: set[str] = {primary} if primary else set()
    out: list[str] = []
    for chunk in raw.split(","):
        cid = chunk.strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


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


# ---------------------------------------------------------------------------
# Direct Telegram body builders (used when telegram_transport.is_configured())
# ---------------------------------------------------------------------------

def _build_telegram_dispatch_body(
    alert_key: str,
    message: str,
    *,
    urgent: bool,
    extra: dict[str, Any] | None,
    header_override: str | None = None,
) -> str:
    """Compose a Telegram message for ``_dispatch`` flows.

    Strips the ``[HH:MM] [info] energy-manager [kind]`` debug prefix that the
    OpenClaw path used (the LLM ignored it anyway). The structured ``extra``
    dict is **not** rendered on the Telegram path — callers already include
    the relevant facts in *message*; ``extra`` exists only for the OpenClaw
    hook + action_log payloads (#330).

    ``header_override`` lets a caller swap the static ``_TELEGRAM_HEADERS[...]``
    headline for one that carries dynamic context — e.g. the appliance name
    (``🧺 Washing machine armed``) instead of the generic class
    (``🧺 Appliance armed``). Set to a non-empty string to override; ``None``
    (default) keeps the static lookup.
    """
    if header_override:
        head = header_override
    else:
        head = _telegram_header_for(alert_key)
    if urgent and not head.startswith(("🚨", "⚠️")):
        head = f"🚨 {head}"
    # Drop a body's own leading ``## ...`` headline if it duplicates the
    # dispatcher header (the briefs used to embed their own `## ☀️ Morning
    # brief` line; #330 strips it from the source, but defensive cleanup here
    # keeps any straggler — or third-party caller — from producing two
    # stacked headers.
    stripped = message.lstrip("\n")
    if stripped.startswith("##"):
        first_nl = stripped.find("\n")
        if first_nl == -1:
            stripped = ""
        else:
            stripped = stripped[first_nl + 1 :]
    parts = [f"**{head}**", stripped]
    return "\n".join(p for p in parts if p)


def _build_telegram_push_alert_body(event_type: str, payload: dict[str, Any]) -> str:
    """Render a structured push event into a short, valuable Telegram message.

    Until 2026-05-09 ``push_alert`` shipped raw JSON to OpenClaw and relied on
    a Claude call there to humanize it. With the LLM out of the loop, we have
    to format here — one or two lines per event with the key facts.
    """
    head = _telegram_header_for(event_type)
    if event_type == AlertType.NEGATIVE_WINDOW_START.value:
        title = payload.get("title") or head
        body_text = payload.get("body") or ""
        bits: list[str] = []
        soc = payload.get("soc_pct")
        if soc is not None:
            bits.append(f"SoC {float(soc):.0f}%")
        mode = payload.get("fox_mode")
        if mode and mode != "unknown":
            bits.append(f"Fox: {mode}")
        price = payload.get("price_pence")
        if price is not None:
            bits.append(f"{float(price):.1f}p/kWh")
        tail = ("\n" + " · ".join(bits)) if bits else ""
        return f"**{title}**\n{body_text}{tail}"
    if event_type == AlertType.CHEAP_WINDOW_START.value:
        soc = payload.get("soc_percent")
        mode = payload.get("fox_mode")
        lines = [f"**{head}**", "Battery charging, DHW heating."]
        bits = []
        if soc is not None:
            bits.append(f"SoC {float(soc):.0f}%")
        if mode and mode != "unknown":
            bits.append(f"Fox: {mode}")
        if bits:
            lines.append(" · ".join(bits))
        return "\n".join(lines)
    if event_type == AlertType.PEAK_WINDOW_START.value:
        soc = payload.get("soc_percent")
        lines = [f"**{head}**", "House on battery, Daikin suspended."]
        if soc is not None:
            lines.append(f"SoC {float(soc):.0f}%")
        return "\n".join(lines)
    # Generic fallback — surface payload["message"] if present.
    lines = [f"**{head}**"]
    msg = payload.get("message") if isinstance(payload, dict) else None
    if msg:
        lines.append(str(msg))
    return "\n".join(lines)


def _build_telegram_plan_proposed_body(
    plan_id: str,
    plan_date: str,
    summary: str,
    table: str,
    *,
    approval_timeout_s: int,
    auto_applied: bool,
) -> str:
    """Plan-proposed body. Returned as raw HTML — pass ``parse_html=False`` to
    ``send_message`` so the schedule ``<pre>`` block survives intact.

    ``auto_applied=True`` (the common case under PLAN_AUTO_APPROVE) drops the
    "Auto-applies in N min unless rejected" footer — it confused readers when
    the plan was already live. Auto-applied messages instead point at
    ``get_plan_timeline()`` for review.
    """
    head = _telegram_header_for(AlertType.PLAN_PROPOSED.value)
    plan_id_safe = html.escape(plan_id)
    plan_date_safe = html.escape(plan_date)
    summary_html = telegram_transport.markdown_to_html(summary)
    parts = [
        f"<b>{head} — {plan_date_safe}</b>",
        f"Plan ID: <code>{plan_id_safe}</code>",
    ]
    if (table or "").strip():
        parts.append("")
        parts.append("<pre>" + html.escape(table) + "</pre>")
    if summary_html:
        parts.append("")
        parts.append(summary_html)
    parts.append("")
    if auto_applied:
        parts.append("Already applied. Review via <code>get_plan_timeline()</code>.")
    else:
        minutes = max(1, int(approval_timeout_s) // 60)
        parts.append(f"Auto-applies in {minutes} min unless rejected.")
        parts.append(f'Reject via MCP: <code>reject_plan("{plan_id_safe}")</code>')
    return "\n".join(parts)


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
    telegram_header_override: str | None = None,
) -> None:
    full_msg = _compose_delivery_body(kind, message, urgent=urgent, extra=extra)
    _record_notification(kind, message, urgent=urgent, extra=extra, full_msg=full_msg)
    route = _resolve_route(kind.value)
    if not route:
        return
    silent = bool(route.get("silent"))

    # Direct Telegram is preferred when configured — bypasses OpenClaw LLM.
    if telegram_transport.is_configured():
        body = _build_telegram_dispatch_body(
            kind.value, message,
            urgent=urgent, extra=extra,
            header_override=telegram_header_override,
        )
        telegram_transport.send_message(body, silent=silent)
        # Appliance lifecycle events fan out to optional secondary chats so
        # household members get the same message (washing-machine armed /
        # starting / finished / cancelled). Failures are swallowed by the
        # transport — one chat being unreachable must not block the others.
        if kind.value in _APPLIANCE_FANOUT_ALERT_KEYS:
            for cid in _appliance_fanout_chat_ids():
                telegram_transport.send_message(body, silent=silent, chat_id_override=cid)
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
        silent=silent,
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
) -> None:
    """🧺 Cycle is starting now (LP-armed cron just fired ``setMachineState run``).

    Lean message: window + cycle facts only. The appliance name lives in the
    Telegram header (``telegram_header_override`` below) to avoid duplicating
    ``🧺 **Name**`` on a second body line. Tariff/PnL context lives in the
    morning brief — duplicating it here was just longer pings, not richer
    information.
    """
    body = "\n".join([
        f"{planned_start_local} → end ≤ {deadline_local}",
        f"{duration_minutes} min · avg {avg_price_pence:.1f}p/kWh",
    ])
    extra = {
        "appliance": appliance_name,
        "planned_start_local": planned_start_local,
        "deadline_local": deadline_local,
        "avg_price_pence": round(float(avg_price_pence), 2),
        "duration_minutes": int(duration_minutes),
    }
    _dispatch(
        AlertType.APPLIANCE_STARTING, body, urgent=False, extra=extra,
        telegram_header_override=f"🧺 {appliance_name} starting",
    )


def notify_appliance_finished(
    *,
    appliance_name: str,
    started_local: str,
    ended_local: str,
    duration_minutes: int,
    avg_price_pence: float | None = None,
    estimated_kwh: float | None = None,
    estimated_cost_p: float | None = None,
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
    # Appliance name lives in the Telegram header (telegram_header_override
    # below) to avoid duplicating it on a second body line.
    body = f"{started_local} → {ended_local} ({duration_minutes} min)" + (
        f" · {cost_line}" if cost_line else ""
    )
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
    _dispatch(
        AlertType.APPLIANCE_FINISHED, body, urgent=False, extra=extra,
        telegram_header_override=f"✅ {appliance_name} cycle complete",
    )


def notify_appliance_armed(
    *,
    appliance_name: str,
    planned_start_local: str,
    planned_end_local: str,
    deadline_local: str,
    duration_minutes: int,
    avg_price_pence: float,
    replan: bool = False,
) -> None:
    """🧺 LP picked a window for this appliance and armed the cron.

    Fires when ``appliance_dispatch.reconcile()`` writes a new ``appliance_jobs``
    row. The ``replan=True`` window-shift ping is muted by default (gated by
    ``APPLIANCE_NOTIFY_REPLAN`` at the call site) under the pull-based
    notification policy — the user wants only the first-arm confirmation and
    the finished summary; window revisions are pulled via the UI/MCP.
    """
    verb = "re-armed" if replan else "armed"
    # One-line confirmation (short by request, 2026-06-28): window · duration ·
    # price · deadline. Appliance name + verb live in the Telegram header.
    body = (
        f"{planned_start_local}→{planned_end_local} · "
        f"{duration_minutes} min · {avg_price_pence:.1f}p/kWh · by {deadline_local}"
    )
    extra = {
        "appliance": appliance_name,
        "planned_start_local": planned_start_local,
        "planned_end_local": planned_end_local,
        "deadline_local": deadline_local,
        "duration_minutes": int(duration_minutes),
        "avg_price_pence": round(float(avg_price_pence), 2),
        "replan": bool(replan),
    }
    _dispatch(
        AlertType.APPLIANCE_ARMED, body, urgent=False, extra=extra,
        telegram_header_override=f"🧺 {appliance_name} {verb}",
    )


def notify_appliance_window_nudge(
    *,
    appliance_name: str,
    recommended_start_local: str,
    recommended_end_local: str,
    deadline_local: str,
    duration_minutes: int,
    avg_price_pence: float,
    est_kwh: float,
    est_cost_pence: float,
    is_negative: bool,
) -> None:
    """🧺⚡ A cheap/negative window is coming — prompt the user to LOAD the
    machine + enable Smart Control so the dispatcher can run it at the best slot.

    HEM can't load the machine (the physical Smart-Control button is the consent
    gate), so this is the only lever. pt-BR body; debounced once per window by
    the caller (``appliance_dispatch.nudge_appliance_windows``).
    """
    if is_negative or est_cost_pence < 0:
        headline = "Janela paga (negativa) chegando!"
        # est_cost_pence is signed; negative = you're paid to run.
        money = f"você RECEBE ~{abs(est_cost_pence):.0f}p"
    else:
        headline = "Janela barata chegando!"
        money = f"custo ~{est_cost_pence:.0f}p"
    body = "\n".join([
        headline,
        f"Carregue a {appliance_name} e ative o Smart Control até {deadline_local}.",
        f"Recomendado: {recommended_start_local}–{recommended_end_local} "
        f"({duration_minutes} min) · média {avg_price_pence:.1f}p/kWh",
        f"Estimado: {est_kwh:.1f} kWh → {money}",
    ])
    extra = {
        "appliance": appliance_name,
        "recommended_start_local": recommended_start_local,
        "recommended_end_local": recommended_end_local,
        "deadline_local": deadline_local,
        "duration_minutes": int(duration_minutes),
        "avg_price_pence": round(float(avg_price_pence), 2),
        "est_kwh": round(float(est_kwh), 2),
        "est_cost_pence": round(float(est_cost_pence), 1),
        "is_negative": bool(is_negative),
    }
    _dispatch(
        AlertType.APPLIANCE_WINDOW_NUDGE, body, urgent=False, extra=extra,
        telegram_header_override=f"🧺⚡ Carregue a {appliance_name}",
    )


def notify_appliance_cancelled(
    *,
    appliance_name: str,
    reason: str,
    planned_start_local: str | None = None,
) -> None:
    """🚫 Job was cancelled before it ran (cron dropped, status='cancelled').

    Reasons surfaced today by ``appliance_dispatch._cancel``:
    ``remote_mode_dropped`` — user disabled Smart Control on the appliance
    ``replanned`` — LP picked a different window; new ARMED hook follows
    ``deadline_passed`` — deadline expired before a feasible window opened
    """
    body_lines = [f"Reason: {reason}"]
    if planned_start_local:
        body_lines.append(f"Was scheduled for {planned_start_local}")
    body = "\n".join(body_lines)
    extra: dict[str, Any] = {
        "appliance": appliance_name,
        "reason": reason,
    }
    if planned_start_local:
        extra["planned_start_local"] = planned_start_local
    _dispatch(
        AlertType.APPLIANCE_CANCELLED, body, urgent=False, extra=extra,
        telegram_header_override=f"🚫 {appliance_name} cancelled",
    )


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


def notify_risk(message: str, extra: dict[str, Any] | None = None) -> None:
    _dispatch(AlertType.RISK_ALERT, message, urgent=True, extra=extra)


def notify_user_override(message: str) -> None:
    """Phase 4.3: one-shot notification when a Daikin user override is detected."""
    notify_action_confirmation(f"User override detected — {message}. Schedule will re-converge at next MPC replan.")


def notify_action_confirmation(message: str) -> None:
    _dispatch(AlertType.ACTION_CONFIRMATION, message, urgent=False)


def notify_critical(message: str) -> None:
    _dispatch(AlertType.CRITICAL_ERROR, message, urgent=True)


def notify_lp_failure(
    *,
    run_at_utc: str,
    plan_date: str,
    error_class: str,
    error_msg: str,
    lp_inputs_run_id: int | None = None,
) -> None:
    """🚨 Fired when the LP solver returns Infeasible, hits CBC timeout, or
    raises. The defensive "hold previous schedule" path keeps hardware safe,
    but the user should still know about the failure so it can be investigated.

    Rate-limited by message hash via the existing per-AlertType notification
    debounce — a recurring infeasible across many MPC re-solves only pages
    once per debounce window. Investigation surface is the new MCP tool
    :func:`get_recent_lp_failures` and the ``lp_failure_log`` DB table.
    """
    body_lines = [
        f"At: {run_at_utc}",
        f"Plan date: {plan_date}",
        f"Reason: `{error_class}`",
    ]
    if error_msg:
        # Keep one line — full stacktrace lives in lp_failure_log.
        snippet = error_msg.split("\n", 1)[0][:200]
        body_lines.append(f"Detail: {snippet}")
    if lp_inputs_run_id is not None:
        body_lines.append(
            f"Replay: `get_lp_solution(run_id={lp_inputs_run_id})` + "
            f"`get_recent_lp_failures()` for full context."
        )
    else:
        body_lines.append("Investigate: `get_recent_lp_failures()`.")
    body = "\n".join(body_lines)
    extra = {
        "run_at_utc": run_at_utc,
        "plan_date": plan_date,
        "error_class": error_class,
        "lp_inputs_run_id": lp_inputs_run_id,
    }
    _dispatch(AlertType.LP_FAILURE, body, urgent=True, extra=extra)


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
    silent = bool(route.get("silent"))

    if telegram_transport.is_configured():
        body = _build_telegram_push_alert_body(event_type, payload)
        return telegram_transport.send_message(body, silent=silent)

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
        silent=silent,
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
    *,
    auto_applied: bool = False,
) -> None:
    """Send a PLAN_PROPOSED notification with the full schedule.

    ``auto_applied=True`` (default under PLAN_AUTO_APPROVE) means the plan is
    already live — the message tells the user where to review it and skips the
    approve/reject footer. ``auto_applied=False`` is the legacy consent flow.

    Payload advertises ``autoAcceptOnTimeout`` + ``approvalTimeoutSeconds`` for
    the OpenClaw fallback path that still implements interactive accept/reject
    buttons; on the direct-Telegram path those keys are inert.
    """
    tz_name = getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    table = _format_plan_actions(actions, tz_name)
    approval_timeout_s = int(getattr(config, "PLAN_APPROVAL_TIMEOUT_SECONDS", 300))
    if auto_applied:
        msg = (
            f"New energy plan for {plan_date} — ID: {plan_id} (auto-applied)\n"
            f"\n{table}\n"
            f"\n{summary}\n"
            f"\nReview: get_plan_timeline()"
        )
    else:
        msg = (
            f"New energy plan for {plan_date} — ID: {plan_id}\n"
            f"\n{table}\n"
            f"\n{summary}\n"
            f"\nTo activate: confirm_plan(\"{plan_id}\")\n"
            f"To reject:   reject_plan(\"{plan_id}\")\n"
            f"(Auto-activates in {approval_timeout_s // 60} min if no response)"
        )
    full_msg = _compose_delivery_body(AlertType.PLAN_PROPOSED, msg, urgent=not auto_applied, extra=None)
    _record_notification(AlertType.PLAN_PROPOSED, msg, urgent=not auto_applied, extra=None, full_msg=full_msg)

    route = _resolve_route(AlertType.PLAN_PROPOSED.value)
    if not route:
        return
    silent = bool(route.get("silent"))

    if telegram_transport.is_configured():
        body = _build_telegram_plan_proposed_body(
            plan_id, plan_date, summary, table,
            approval_timeout_s=approval_timeout_s,
            auto_applied=auto_applied,
        )
        # Body is already HTML (with <pre> for the schedule); keep parse_mode=HTML
        # but skip the markdown→HTML pre-pass so our tags survive intact.
        telegram_transport.send_message(body, silent=silent, convert_markdown=False)
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
