"""Direct Telegram Bot API transport — bypasses OpenClaw LLM-shaped delivery.

When ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` are set, ``src/notifier.py``
prefers this transport over ``POST /hooks/agent``. The previous flow paid for a
Claude API call inside OpenClaw on *every* notification — to re-shape Markdown
HEM had already formatted. Sending straight to ``api.telegram.org`` removes
that tax while keeping action_log and stdout unchanged.

We only target the small Telegram HTML subset (``<b>``, ``<i>``, ``<code>``,
``<pre>``, ``<a href>``) — MarkdownV2's escape rules are punishing for
free-form text and we never use anything more exotic than bold + code.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any

import requests

from .config import config

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_CHARS = 4096

# Bold: ``**text**``. Inline code: ``` `text` ```. Italic with single underscore is
# deliberately NOT translated — snake_case identifiers all over the codebase
# would render as malformed entities. The OpenClaw LLM didn't use italic either.
_BOLD_RE = re.compile(r"\*\*([^*\n][^*]*?)\*\*")
_CODE_RE = re.compile(r"`([^`\n]+?)`")
# Markdown ATX headers (``## Title``, ``### Sub``). Telegram HTML has no header
# tags; without this conversion the leading ``#`` characters show up literally.
_HEADER_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def is_configured() -> bool:
    """True when both ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are set."""
    return bool(
        getattr(config, "TELEGRAM_BOT_TOKEN", "").strip()
        and getattr(config, "TELEGRAM_CHAT_ID", "").strip()
    )


def markdown_to_html(text: str) -> str:
    """Convert HEM's ad-hoc Markdown to Telegram's HTML subset.

    Order matters: HTML special chars are escaped first, then ``**...**`` /
    `` `...` `` are promoted to ``<b>`` / ``<code>`` tags. ``*`` and `` ` ``
    are not HTML-special so they survive escaping intact, which means
    user-supplied content can never inject markup.
    """
    if not text:
        return ""
    escaped = html.escape(text, quote=False)
    escaped = _HEADER_RE.sub(r"<b>\1</b>", escaped)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    return escaped


def _truncate(text: str, *, limit: int = _TELEGRAM_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n… <i>truncated</i>"
    return text[: limit - len(suffix)] + suffix


def send_message(
    text: str,
    *,
    silent: bool = False,
    convert_markdown: bool = True,
    parse_mode: str = "HTML",
    chat_id_override: str | None = None,
) -> bool:
    """POST to Bot API ``sendMessage``. Returns True on HTTP 2xx.

    Three flags, intentionally orthogonal:

    * ``convert_markdown`` — when True (default), HEM-style Markdown
      (``**bold**``, `` `code` ``) is HTML-escaped and promoted to Telegram
      HTML tags via :func:`markdown_to_html`. Callers that ship pre-built
      HTML (e.g. ``notify_plan_proposed`` with its ``<pre>`` schedule block)
      pass ``False`` to keep their tags intact.
    * ``parse_mode`` — Telegram parse mode header. Defaults to ``"HTML"``;
      set to ``""`` to send literal text with no formatting.
    * ``chat_id_override`` — when set (non-empty), POST to that chat ID
      instead of ``config.TELEGRAM_CHAT_ID``. Used by the appliance fanout
      path in ``notifier`` to deliver the same body to a secondary chat
      (e.g. a household member). The bot token still comes from config —
      both chats must have already started a conversation with the bot.

    Failures are logged and swallowed — a Telegram outage must never abort
    the LP solver, scheduler tick, or appliance dispatcher. stdout +
    action_log entries upstream of this call are unaffected.
    """
    if not is_configured():
        return False
    token = config.TELEGRAM_BOT_TOKEN.strip()
    chat_id = (chat_id_override or config.TELEGRAM_CHAT_ID).strip()
    if not chat_id:
        return False
    base = (
        getattr(config, "TELEGRAM_API_BASE_URL", "https://api.telegram.org") or ""
    ).rstrip("/") or "https://api.telegram.org"
    url = f"{base}/bot{token}/sendMessage"

    body = markdown_to_html(text) if convert_markdown else text
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": _truncate(body),
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if silent:
        payload["disable_notification"] = True

    timeout = float(getattr(config, "TELEGRAM_TIMEOUT_SECONDS", 10))
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        if 200 <= r.status_code < 300:
            return True
        logger.warning(
            "telegram sendMessage non-2xx (status=%s body=%s)",
            r.status_code,
            (r.text or "")[:200],
        )
        return False
    except requests.RequestException as exc:
        logger.warning("telegram sendMessage failed: %s", exc)
        return False
