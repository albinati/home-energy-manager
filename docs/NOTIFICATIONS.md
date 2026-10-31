# User notifications (Telegram vs OpenClaw hook)

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

## User notifications — direct Telegram (preferred) vs OpenClaw hook (fallback)

As of 2026-05-09 HEM POSTs notifications **straight to the Telegram Bot API**
when configured, bypassing OpenClaw's `/hooks/agent` LLM-shaping path. The
older flow paid for an Anthropic API call inside OpenClaw on *every* brief,
plan revision, tier-boundary ping, and appliance lifecycle event — to
re-shape Markdown HEM had already formatted. Direct Telegram removes that
tax while keeping action_log + stdout unchanged.

Transport is selected at delivery time by `src/notifier.py`:

1. **`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` set** → POST to
   `https://api.telegram.org/bot<TOKEN>/sendMessage` via
   `src/telegram_transport.py`. HTML parse mode; `**bold**` and `` `code` ``
   in the source are converted; structured `push_alert` events get bespoke
   per-event rendering (no JSON dump). Plan-proposed messages ship the
   schedule inside `<pre>` for monospace alignment. OpenClaw is **not** called.
2. **Telegram unset, `OPENCLAW_HOOKS_URL` + `OPENCLAW_HOOKS_TOKEN` set** →
   legacy hook path (LLM-shaped). Kept for rollback.
3. **Neither configured** → stdout + action_log only.

`OPENCLAW_NOTIFY_ENABLED=false` is the master mute for both transports
(stdout + action_log keep running). Per-AlertType routing (enable/disable,
severity, silent flag) still flows through the `notification_routes`
SQLite table; `target_override` / `channel_override` only apply to the
OpenClaw fallback path — the Telegram chat is global.

### `.env` keys

```
TELEGRAM_BOT_TOKEN=123456:ABC...        # from @BotFather; sensitive
TELEGRAM_CHAT_ID=7964600619             # same value as OPENCLAW_NOTIFY_TARGET
TELEGRAM_API_BASE_URL=https://api.telegram.org   # override only for testing
TELEGRAM_TIMEOUT_SECONDS=10
```

When the user's Anthropic-token budget is the concern, leaving the OpenClaw
hook path enabled is fine *as long as Telegram is also configured* — the
Telegram path short-circuits and OpenClaw is never reached for messaging.
To roll back, simply unset `TELEGRAM_BOT_TOKEN` (or `TELEGRAM_CHAT_ID`)
and restart `hem.service`. The OpenClaw hook config is untouched.

### Interactive accept/reject buttons (deferred)

Telegram supports `reply_markup.inline_keyboard` with `callback_data` for
plan-approval buttons, but receiving the callback needs either polling
`getUpdates` or a webhook ingress. Since `PLAN_AUTO_APPROVE=true` is the
default and timeouts auto-accept, the current Telegram path ships the
plan as plain text with a clear "Auto-applies in N min unless rejected"
line and instructions to reject via the `reject_plan` MCP tool. Wiring
buttons is a future follow-up.

---

