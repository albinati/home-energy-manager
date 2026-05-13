"""Tests for the direct Telegram Bot API transport (src/telegram_transport.py)
and its integration with src/notifier.py.

Covers:
* ``markdown_to_html`` — XSS-safe escape + bold/code promotion.
* ``send_message`` — payload shape, silent flag, error handling, truncation.
* ``notifier._dispatch`` — routes to Telegram when configured, OpenClaw skipped.
* ``notifier.push_alert`` — per-event-type rendering replaces the old JSON dump.
* ``notify_plan_proposed`` — pre-built HTML body (``<pre>`` schedule) survives.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def _make_config(**overrides):
    """Minimal config-like namespace."""
    defaults = dict(
        # OpenClaw hook fallback
        OPENCLAW_NOTIFY_ENABLED=True,
        OPENCLAW_NOTIFY_CHANNEL="telegram",
        OPENCLAW_NOTIFY_TARGET="7964600619",
        OPENCLAW_NOTIFY_TARGET_CRITICAL="",
        OPENCLAW_NOTIFY_CHANNEL_CRITICAL="",
        OPENCLAW_NOTIFY_TARGET_REPORTS="",
        OPENCLAW_NOTIFY_CHANNEL_REPORTS="",
        OPENCLAW_HOOKS_URL="http://127.0.0.1:18789/hooks/agent",
        OPENCLAW_HOOKS_TOKEN="hook-token",
        OPENCLAW_HOOKS_TIMEOUT_SECONDS=30,
        OPENCLAW_HOOKS_AGENT_ID="",
        OPENCLAW_INTERNAL_API_BASE_URL="http://127.0.0.1:8000",
        # Direct Telegram
        TELEGRAM_BOT_TOKEN="123456:abcdef",
        TELEGRAM_CHAT_ID="7964600619",
        TELEGRAM_API_BASE_URL="https://api.telegram.org",
        TELEGRAM_TIMEOUT_SECONDS=10,
        # Plan-proposed timeout used by the helper
        PLAN_APPROVAL_TIMEOUT_SECONDS=300,
        BULLETPROOF_TIMEZONE="Europe/London",
    )
    defaults.update(overrides)
    ns = MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


def _patch_config(monkeypatch, cfg):
    """Replace the config module attribute used by both modules under test."""
    from src import notifier, telegram_transport

    monkeypatch.setattr(notifier, "config", cfg)
    monkeypatch.setattr(telegram_transport, "config", cfg)


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------

class TestMarkdownToHtml:
    def test_empty_string(self):
        from src.telegram_transport import markdown_to_html
        assert markdown_to_html("") == ""

    def test_plain_text_unchanged(self):
        from src.telegram_transport import markdown_to_html
        assert markdown_to_html("hello world") == "hello world"

    def test_bold_converted(self):
        from src.telegram_transport import markdown_to_html
        assert markdown_to_html("**urgent**") == "<b>urgent</b>"

    def test_inline_code_converted(self):
        from src.telegram_transport import markdown_to_html
        # quote=False is intentional in markdown_to_html — Telegram HTML accepts
        # bare quotes in text content; escaping them adds noise.
        assert markdown_to_html("run `confirm_plan(\"x\")`") == (
            'run <code>confirm_plan("x")</code>'
        )

    def test_xss_escaped_before_tags(self):
        """Ensure raw HTML in input is escaped — no markup injection."""
        from src.telegram_transport import markdown_to_html
        out = markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_bold_after_escape(self):
        """**bold** containing HTML special chars still works (escape order)."""
        from src.telegram_transport import markdown_to_html
        out = markdown_to_html("**a<b**")
        assert out == "<b>a&lt;b</b>"

    def test_snake_case_not_italicised(self):
        """Single underscore must NOT be promoted to italic — too many false positives."""
        from src.telegram_transport import markdown_to_html
        out = markdown_to_html("see action_log_duration")
        assert "<i>" not in out
        assert "action_log_duration" in out

    def test_atx_header_h2_becomes_bold(self):
        """``## Title`` would render literally on Telegram HTML — convert to <b>."""
        from src.telegram_transport import markdown_to_html
        out = markdown_to_html("## ☀️ Morning brief")
        assert out == "<b>☀️ Morning brief</b>"
        # The raw '#' must NOT remain in the output.
        assert "#" not in out

    def test_atx_header_h3_becomes_bold(self):
        from src.telegram_transport import markdown_to_html
        out = markdown_to_html("### Subsection")
        assert out == "<b>Subsection</b>"

    def test_header_inside_multiline_body_converted(self):
        """A header in the middle of a body — multiline matching must catch it."""
        from src.telegram_transport import markdown_to_html
        text = "prelude\n## Section\nbody"
        out = markdown_to_html(text)
        assert "<b>Section</b>" in out
        assert "## Section" not in out

    def test_hashtag_in_middle_of_line_not_a_header(self):
        """``check #123`` must not be mistaken for a header."""
        from src.telegram_transport import markdown_to_html
        out = markdown_to_html("see issue #123 for context")
        assert "<b>" not in out
        assert "#123" in out


class TestIsConfigured:
    def test_true_when_both_set(self, monkeypatch):
        from src import telegram_transport
        monkeypatch.setattr(telegram_transport, "config", _make_config())
        assert telegram_transport.is_configured() is True

    def test_false_when_token_missing(self, monkeypatch):
        from src import telegram_transport
        monkeypatch.setattr(
            telegram_transport, "config",
            _make_config(TELEGRAM_BOT_TOKEN=""),
        )
        assert telegram_transport.is_configured() is False

    def test_false_when_chat_id_missing(self, monkeypatch):
        from src import telegram_transport
        monkeypatch.setattr(
            telegram_transport, "config",
            _make_config(TELEGRAM_CHAT_ID=""),
        )
        assert telegram_transport.is_configured() is False


class TestSendMessage:
    def test_returns_false_when_not_configured(self, monkeypatch):
        from src import telegram_transport
        monkeypatch.setattr(
            telegram_transport, "config",
            _make_config(TELEGRAM_BOT_TOKEN=""),
        )
        assert telegram_transport.send_message("hi") is False

    def test_posts_correct_url_and_payload(self, monkeypatch):
        from src import telegram_transport

        captured: dict[str, Any] = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            r = MagicMock()
            r.status_code = 200
            return r

        monkeypatch.setattr(telegram_transport, "config", _make_config())
        monkeypatch.setattr(telegram_transport.requests, "post", fake_post)

        ok = telegram_transport.send_message("**hello** world")
        assert ok is True
        assert captured["url"] == "https://api.telegram.org/bot123456:abcdef/sendMessage"
        body = captured["json"]
        assert body["chat_id"] == "7964600619"
        assert body["parse_mode"] == "HTML"
        assert body["disable_web_page_preview"] is True
        assert "disable_notification" not in body
        assert body["text"] == "<b>hello</b> world"
        assert captured["timeout"] == 10.0

    def test_silent_sets_disable_notification(self, monkeypatch):
        from src import telegram_transport

        captured: dict[str, Any] = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            r = MagicMock()
            r.status_code = 200
            return r

        monkeypatch.setattr(telegram_transport, "config", _make_config())
        monkeypatch.setattr(telegram_transport.requests, "post", fake_post)

        telegram_transport.send_message("hi", silent=True)
        assert captured["json"]["disable_notification"] is True

    def test_convert_markdown_false_keeps_raw_html(self, monkeypatch):
        """Pre-built HTML (e.g. plan-proposed <pre>) must survive."""
        from src import telegram_transport

        captured: dict[str, Any] = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            r = MagicMock()
            r.status_code = 200
            return r

        monkeypatch.setattr(telegram_transport, "config", _make_config())
        monkeypatch.setattr(telegram_transport.requests, "post", fake_post)

        telegram_transport.send_message(
            "<pre>10:00  charge</pre>", convert_markdown=False
        )
        assert captured["json"]["text"] == "<pre>10:00  charge</pre>"
        assert captured["json"]["parse_mode"] == "HTML"

    def test_returns_false_on_http_error(self, monkeypatch):
        from src import telegram_transport

        def fake_post(*a, **kw):
            r = MagicMock()
            r.status_code = 500
            r.text = "internal error"
            return r

        monkeypatch.setattr(telegram_transport, "config", _make_config())
        monkeypatch.setattr(telegram_transport.requests, "post", fake_post)

        assert telegram_transport.send_message("x") is False

    def test_returns_false_on_request_exception(self, monkeypatch):
        from src import telegram_transport
        import requests as rq

        def fake_post(*a, **kw):
            raise rq.ConnectionError("boom")

        monkeypatch.setattr(telegram_transport, "config", _make_config())
        monkeypatch.setattr(telegram_transport.requests, "post", fake_post)

        assert telegram_transport.send_message("x") is False

    def test_truncates_over_limit(self, monkeypatch):
        from src import telegram_transport

        captured: dict[str, Any] = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            r = MagicMock()
            r.status_code = 200
            return r

        monkeypatch.setattr(telegram_transport, "config", _make_config())
        monkeypatch.setattr(telegram_transport.requests, "post", fake_post)

        long_text = "a" * 10_000
        telegram_transport.send_message(long_text)
        text = captured["json"]["text"]
        assert len(text) <= 4096
        assert text.endswith("<i>truncated</i>")


# ---------------------------------------------------------------------------
# notifier._dispatch — Telegram preferred when configured
# ---------------------------------------------------------------------------

class TestDispatchPrefersTelegram:
    def _route_active(self, monkeypatch):
        from src import notifier
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)

    def test_routes_to_telegram_when_configured(self, monkeypatch):
        from src import notifier, telegram_transport

        cfg = _make_config()
        _patch_config(monkeypatch, cfg)
        self._route_active(monkeypatch)

        telegram_calls: list[dict[str, Any]] = []
        hook_calls: list[Any] = []

        def fake_send(text, *, silent=False, convert_markdown=True, parse_mode="HTML"):
            telegram_calls.append({"text": text, "silent": silent})
            return True

        def fake_hook_post(*a, **kw):
            hook_calls.append(1)
            r = MagicMock(); r.status_code = 200
            return r

        # Pin transport.is_configured to True to bypass MagicMock-attr quirks.
        monkeypatch.setattr(telegram_transport, "is_configured", lambda: True)
        monkeypatch.setattr(telegram_transport, "send_message", fake_send)
        monkeypatch.setattr(notifier.requests, "post", fake_hook_post)

        notifier._dispatch(notifier.AlertType.RISK_ALERT, "battery low", urgent=True)

        assert len(telegram_calls) == 1
        assert hook_calls == []  # OpenClaw fully skipped
        text = telegram_calls[0]["text"]
        assert "Risk alert" in text
        assert "battery low" in text
        # Debug-prefix line from OpenClaw flow must not leak through
        assert "energy-manager" not in text

    def test_extra_dict_not_dumped_as_json_into_body(self, monkeypatch):
        """The ``extra`` payload is for the hook + action_log, not for humans.

        Before #330 the dispatcher appended ``json.dumps(extra)`` in an inline
        code block, producing ``{"warnings": [...]}`` blobs right after a
        human-readable summary that already contained the same info — ugly
        and confusing. The Telegram body must omit it.
        """
        from src import notifier, telegram_transport

        cfg = _make_config()
        _patch_config(monkeypatch, cfg)
        self._route_active(monkeypatch)

        captured: list[str] = []

        def fake_send(text, *, silent=False, convert_markdown=True, parse_mode="HTML"):
            captured.append(text)
            return True

        monkeypatch.setattr(telegram_transport, "is_configured", lambda: True)
        monkeypatch.setattr(telegram_transport, "send_message", fake_send)

        notifier._dispatch(
            notifier.AlertType.STRATEGY_UPDATE,
            "summary line",
            extra={"warnings": ["a", "b", "c"]},
        )

        assert len(captured) == 1
        text = captured[0]
        assert "summary line" in text
        assert '{"warnings"' not in text
        assert "json" not in text.lower()

    def test_leading_markdown_header_in_body_stripped(self, monkeypatch):
        """A body that still leads with ``## ...`` must not stack on top of the
        dispatcher's own bold headline (#330)."""
        from src import notifier, telegram_transport

        cfg = _make_config()
        _patch_config(monkeypatch, cfg)
        self._route_active(monkeypatch)

        captured: list[str] = []

        def fake_send(text, *, silent=False, convert_markdown=True, parse_mode="HTML"):
            captured.append(text)
            return True

        monkeypatch.setattr(telegram_transport, "is_configured", lambda: True)
        monkeypatch.setattr(telegram_transport, "send_message", fake_send)

        notifier._dispatch(
            notifier.AlertType.MORNING_REPORT,
            "## ☀️ Morning brief\n**Today (2026-05-13)**\nstrategy line",
        )

        assert len(captured) == 1
        text = captured[0]
        # Only the dispatcher's headline survives; the inner duplicate is gone.
        assert text.count("Morning brief") == 1
        assert "## ☀️" not in text
        assert "Today (2026-05-13)" in text

    def test_falls_back_to_openclaw_when_telegram_unset(self, monkeypatch):
        from src import notifier, telegram_transport

        cfg = _make_config(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
        _patch_config(monkeypatch, cfg)
        self._route_active(monkeypatch)

        hook_calls: list[Any] = []

        def fake_hook_post(url, json=None, headers=None, timeout=None):
            hook_calls.append({"url": url, "json": json})
            r = MagicMock(); r.status_code = 200
            return r

        class ImmediateThread:
            def __init__(self, target, daemon=True, name=""):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(notifier.threading, "Thread", ImmediateThread)
        # is_configured driven by the empty config above — no monkeypatch needed.
        monkeypatch.setattr(notifier.requests, "post", fake_hook_post)

        notifier._dispatch(notifier.AlertType.MORNING_REPORT, "morning")

        assert len(hook_calls) == 1
        assert "/hooks/agent" in hook_calls[0]["url"]


# ---------------------------------------------------------------------------
# push_alert — clean per-event rendering replaces JSON dump
# ---------------------------------------------------------------------------

class TestPushAlertTelegramRendering:
    def _setup(self, monkeypatch):
        from src import notifier, telegram_transport

        captured: list[dict[str, Any]] = []

        def fake_post(url, json=None, timeout=None):
            captured.append({"url": url, "json": json})
            r = MagicMock(); r.status_code = 200
            return r

        cfg = _make_config()
        _patch_config(monkeypatch, cfg)
        monkeypatch.setattr(telegram_transport.requests, "post", fake_post)
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })
        return captured

    def test_cheap_window_human_readable(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.push_cheap_window_start(soc=85.0, fox_mode="Self Use")
        assert len(captured) == 1
        text = captured[0]["json"]["text"]
        assert "Cheap window" in text
        assert "Battery charging" in text
        assert "SoC 85" in text
        assert "Fox: Self Use" in text
        # The pre-Telegram path used to ship raw JSON — make sure it doesn't anymore.
        assert '{"' not in text

    def test_peak_window_human_readable(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.push_peak_window_start(soc=42.0)
        text = captured[0]["json"]["text"]
        assert "Peak window" in text
        assert "Daikin suspended" in text
        assert "SoC 42" in text

    def test_negative_window_includes_title_and_body(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.push_negative_window_start(soc=70.0, fox_mode="Self Use", price_pence=-3.5)
        text = captured[0]["json"]["text"]
        assert "PAID to use" in text
        assert "Octopus is paying us" in text  # body content
        assert "SoC 70" in text
        assert "-3.5p/kWh" in text


# ---------------------------------------------------------------------------
# notify_strategy_update — warnings as bullets, no Python repr / no JSON dump
# ---------------------------------------------------------------------------

class TestStrategyUpdateFormatting:
    def _setup(self, monkeypatch):
        from src import notifier, telegram_transport

        captured: list[str] = []

        def fake_send(text, *, silent=False, convert_markdown=True, parse_mode="HTML"):
            captured.append(text)
            return True

        _patch_config(monkeypatch, _make_config())
        monkeypatch.setattr(telegram_transport, "is_configured", lambda: True)
        monkeypatch.setattr(telegram_transport, "send_message", fake_send)
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })
        return captured

    def test_warnings_list_rendered_as_bullets(self, monkeypatch):
        """``['a', 'b']`` was being str()'d into the message, producing
        Python repr ``Warnings: ['a', 'b']``. After #330 the list becomes
        a bullet list and the JSON dump is gone."""
        from src import notifier
        captured = self._setup(monkeypatch)

        notifier.notify_strategy_update(
            "Daikin write-budget guard active",
            warnings=[
                "tank_idle_overnight@2026-05-09T21:00:00Z",
                "pre_heat@2026-05-10T12:00:00Z",
            ],
        )

        assert len(captured) == 1
        text = captured[0]
        assert "Daikin write-budget guard active" in text
        assert "Warnings (2)" in text
        assert "• tank_idle_overnight@2026-05-09T21:00:00Z" in text
        assert "• pre_heat@2026-05-10T12:00:00Z" in text
        # No Python-repr brackets, no JSON dump duplicate.
        assert "['tank_idle_overnight" not in text
        assert '{"warnings"' not in text

    def test_no_warnings_keeps_message_clean(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.notify_strategy_update("plain summary")
        assert len(captured) == 1
        text = captured[0]
        assert "plain summary" in text
        assert "Warnings" not in text


# ---------------------------------------------------------------------------
# Appliance lifecycle — dynamic Telegram header (no duplicated emoji/name/verb)
# ---------------------------------------------------------------------------

class TestApplianceTelegramHeader:
    """The static ``_TELEGRAM_HEADERS`` map gives every alert a generic
    headline (``🧺 Appliance armed``). For appliance lifecycle events, the
    body used to repeat the name + verb on the next line — producing the
    stacked ``🧺 Appliance armed / 🧺 Washing machine armed for …`` look the
    user flagged. Each helper now injects a dynamic
    ``telegram_header_override`` so the header carries the specific name
    (``🧺 Washing machine armed``) and the body holds only schedule details.
    """

    def _setup(self, monkeypatch):
        from src import notifier, telegram_transport

        captured: list[str] = []

        def fake_send(text, *, silent=False, convert_markdown=True, parse_mode="HTML"):
            captured.append(text)
            return True

        _patch_config(monkeypatch, _make_config())
        monkeypatch.setattr(telegram_transport, "is_configured", lambda: True)
        monkeypatch.setattr(telegram_transport, "send_message", fake_send)
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })
        return captured

    def test_armed_header_carries_appliance_name(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.notify_appliance_armed(
            appliance_name="Washing machine",
            planned_start_local="Wed 15:00",
            planned_end_local="16:17",
            deadline_local="07:00",
            duration_minutes=77,
            avg_price_pence=2.3,
            replan=False,
        )
        assert len(captured) == 1
        text = captured[0]
        # The bold dispatcher header carries the appliance name + verb.
        assert "**🧺 Washing machine armed**" in text
        # Body should NOT repeat the appliance name + emoji + verb again.
        body_only = text.split("\n", 1)[1]
        assert "Washing machine" not in body_only
        assert "🧺" not in body_only
        assert "armed" not in body_only
        # Schedule details still in the body.
        assert "Wed 15:00" in text
        assert "16:17" in text
        assert "2.3p/kWh" in text
        # No JSON tail from #330 — make sure that fix still holds on this path.
        assert '{"appliance"' not in text

    def test_armed_replan_header_uses_re_armed(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.notify_appliance_armed(
            appliance_name="Dishwasher",
            planned_start_local="Wed 02:00",
            planned_end_local="03:30",
            deadline_local="07:00",
            duration_minutes=90,
            avg_price_pence=2.1,
            replan=True,
        )
        text = captured[0]
        assert "**🧺 Dishwasher re-armed**" in text
        assert "armed" in text  # substring of re-armed; that's fine

    def test_starting_header_and_clean_body(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.notify_appliance_starting(
            appliance_name="Dryer",
            planned_start_local="15:00",
            deadline_local="17:00",
            avg_price_pence=4.2,
            duration_minutes=60,
        )
        text = captured[0]
        assert "**🧺 Dryer starting**" in text
        body_only = text.split("\n", 1)[1]
        assert "Dryer" not in body_only
        assert "starting" not in body_only
        assert "15:00" in body_only
        assert "60 min" in body_only

    def test_finished_header_and_clean_body(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.notify_appliance_finished(
            appliance_name="Washing machine",
            started_local="15:00",
            ended_local="16:17",
            duration_minutes=77,
            estimated_kwh=0.85,
            estimated_cost_p=1.9,
            kwh_is_measured=True,
        )
        text = captured[0]
        assert "**✅ Washing machine cycle complete**" in text
        body_only = text.split("\n", 1)[1]
        assert "Washing machine" not in body_only
        assert "cycle complete" not in body_only
        assert "0.85 kWh" in body_only

    def test_cancelled_header_and_clean_body(self, monkeypatch):
        from src import notifier
        captured = self._setup(monkeypatch)
        notifier.notify_appliance_cancelled(
            appliance_name="Washing machine",
            reason="replanned",
            planned_start_local="Wed 15:00",
        )
        text = captured[0]
        assert "**🚫 Washing machine cancelled**" in text
        body_only = text.split("\n", 1)[1]
        assert "Washing machine" not in body_only
        assert "cancelled" not in body_only
        assert "replanned" in body_only
        assert "Wed 15:00" in body_only


# ---------------------------------------------------------------------------
# notify_plan_proposed — HTML schedule block survives
# ---------------------------------------------------------------------------

class TestNotifyPlanProposedTelegram:
    def test_emits_html_with_pre_block(self, monkeypatch):
        from src import notifier, telegram_transport

        captured: list[dict[str, Any]] = []

        def fake_post(url, json=None, timeout=None):
            captured.append({"json": json})
            r = MagicMock(); r.status_code = 200
            return r

        _patch_config(monkeypatch, _make_config())
        monkeypatch.setattr(telegram_transport.requests, "post", fake_post)
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })

        notifier.notify_plan_proposed(
            "lp-2026-06-01",
            "2026-06-01",
            "PuLP ok",
            [{
                "start_time": "2026-06-01T12:00:00Z",
                "end_time": "2026-06-01T12:30:00Z",
                "action_type": "pre_heat",
                "params": {"lwt_offset": 2, "tank_temp": 55},
            }],
        )

        assert len(captured) == 1
        body = captured[0]["json"]
        text = body["text"]
        assert body["parse_mode"] == "HTML"
        assert "<pre>" in text
        assert "</pre>" in text
        assert "lp-2026-06-01" in text
        assert "2026-06-01" in text
        assert "Auto-applies" in text
        assert "reject_plan" in text
