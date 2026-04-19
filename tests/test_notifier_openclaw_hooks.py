"""Tests for OpenClaw Gateway hook notifier (src/notifier.py).

Covers: route resolution, POST /hooks/agent delivery, and MCP notification tools.
"""
from __future__ import annotations

import importlib.util
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    """Return a minimal config-like namespace for testing."""
    defaults = dict(
        OPENCLAW_NOTIFY_ENABLED=True,
        OPENCLAW_NOTIFY_CHANNEL="telegram",
        OPENCLAW_NOTIFY_TARGET="7964600619",
        OPENCLAW_NOTIFY_TARGET_CRITICAL="",
        OPENCLAW_NOTIFY_CHANNEL_CRITICAL="",
        OPENCLAW_NOTIFY_TARGET_REPORTS="",
        OPENCLAW_NOTIFY_CHANNEL_REPORTS="",
        OPENCLAW_HOOKS_URL="http://127.0.0.1:18789/hooks/agent",
        OPENCLAW_HOOKS_TOKEN="test-token",
        OPENCLAW_HOOKS_TIMEOUT_SECONDS=30,
        OPENCLAW_HOOKS_AGENT_ID="",
        OPENCLAW_INTERNAL_API_BASE_URL="http://127.0.0.1:8000",
        PLAN_CONSENT_EXPIRY_SECONDS=3600,
        BULLETPROOF_TIMEZONE="Europe/London",
    )
    defaults.update(overrides)
    ns = MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# _resolve_route tests
# ---------------------------------------------------------------------------

class TestResolveRoute:
    def test_returns_none_when_notify_disabled(self, monkeypatch):
        from src import notifier
        cfg = _make_config(OPENCLAW_NOTIFY_ENABLED=False)
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: None)
        assert notifier._resolve_route("risk_alert") is None

    def test_returns_none_when_row_disabled(self, monkeypatch):
        from src import notifier
        cfg = _make_config()
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 0, "severity": "critical", "target_override": None, "channel_override": None, "silent": 0
        })
        assert notifier._resolve_route("risk_alert") is None

    def test_uses_db_target_override(self, monkeypatch):
        from src import notifier
        cfg = _make_config()
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "critical",
            "target_override": "override_target", "channel_override": None, "silent": 0
        })
        result = notifier._resolve_route("risk_alert")
        assert result is not None
        assert result["target"] == "override_target"

    def test_uses_db_channel_override(self, monkeypatch):
        from src import notifier
        cfg = _make_config()
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": "discord", "silent": 0
        })
        result = notifier._resolve_route("morning_report")
        assert result is not None
        assert result["channel"] == "discord"

    def test_falls_back_to_critical_env(self, monkeypatch):
        from src import notifier
        cfg = _make_config(
            OPENCLAW_NOTIFY_TARGET_CRITICAL="critical_target",
            OPENCLAW_NOTIFY_CHANNEL_CRITICAL="telegram",
        )
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "critical",
            "target_override": None, "channel_override": None, "silent": 0
        })
        result = notifier._resolve_route("risk_alert")
        assert result is not None
        assert result["target"] == "critical_target"

    def test_falls_back_to_default_target(self, monkeypatch):
        from src import notifier
        cfg = _make_config(
            OPENCLAW_NOTIFY_TARGET_REPORTS="",
            OPENCLAW_NOTIFY_TARGET="default_target",
        )
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0
        })
        result = notifier._resolve_route("morning_report")
        assert result is not None
        assert result["target"] == "default_target"

    def test_returns_none_when_no_target_configured(self, monkeypatch):
        from src import notifier
        cfg = _make_config(
            OPENCLAW_NOTIFY_TARGET="",
            OPENCLAW_NOTIFY_TARGET_CRITICAL="",
            OPENCLAW_NOTIFY_TARGET_REPORTS="",
        )
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: None)
        assert notifier._resolve_route("risk_alert") is None

    def test_silent_flag_propagated(self, monkeypatch):
        from src import notifier
        cfg = _make_config()
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 1
        })
        result = notifier._resolve_route("strategy_update")
        assert result is not None
        assert result["silent"] is True


# ---------------------------------------------------------------------------
# Hook delivery
# ---------------------------------------------------------------------------

class TestHooksDelivery:
    def _immediate_thread(self, monkeypatch, notifier_mod):
        class ImmediateThread:
            def __init__(self, target, daemon=True, name=""):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(notifier_mod.threading, "Thread", ImmediateThread)

    def test_dispatch_posts_hook(self, monkeypatch):
        from src import notifier

        posted: list[dict[str, Any]] = []

        def fake_post(url, json=None, headers=None, timeout=None):
            posted.append({"url": url, "json": json})
            r = MagicMock()
            r.status_code = 200
            return r

        self._immediate_thread(monkeypatch, notifier)
        monkeypatch.setattr(notifier.requests, "post", fake_post)
        monkeypatch.setattr(notifier, "config", _make_config())
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "critical",
            "target_override": None, "channel_override": None, "silent": 0,
        })
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)

        notifier._dispatch(notifier.AlertType.RISK_ALERT, "low battery", urgent=True)

        assert len(posted) == 1
        assert posted[0]["url"] == "http://127.0.0.1:18789/hooks/agent"
        assert posted[0]["json"]["name"] == "EnergyRisk"
        assert "low battery" in posted[0]["json"]["message"]

    def test_dispatch_skips_when_no_hooks_url(self, monkeypatch):
        from src import notifier

        posted: list[Any] = []

        def fake_post(*a, **k):
            posted.append(1)
            return MagicMock(status_code=200)

        self._immediate_thread(monkeypatch, notifier)
        monkeypatch.setattr(notifier.requests, "post", fake_post)
        cfg = _make_config(OPENCLAW_HOOKS_URL="", OPENCLAW_HOOKS_TOKEN="")
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)

        notifier._dispatch(notifier.AlertType.MORNING_REPORT, "brief", urgent=False)
        assert posted == []

    def test_dispatch_logs_on_http_error(self, monkeypatch, capsys):
        from src import notifier

        def fake_post(*a, **k):
            r = MagicMock()
            r.status_code = 500
            return r

        self._immediate_thread(monkeypatch, notifier)
        monkeypatch.setattr(notifier.requests, "post", fake_post)
        monkeypatch.setattr(notifier, "config", _make_config())
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)

        notifier._dispatch(notifier.AlertType.STRATEGY_UPDATE, "x", urgent=False)
        err = capsys.readouterr().out
        assert "[openclaw hooks] delivery failed" in err


class TestDispatchLogsToActionLog:
    def test_logs_to_action_log(self, monkeypatch):
        from src import notifier

        logged = []
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: logged.append(kw))
        monkeypatch.setattr(notifier, "config", _make_config(OPENCLAW_HOOKS_URL=""))
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "critical",
            "target_override": None, "channel_override": None, "silent": 0,
        })

        notifier._dispatch(notifier.AlertType.RISK_ALERT, "test message", urgent=True)
        assert len(logged) == 1
        assert logged[0]["action"] == "risk_alert"


class TestNotifyPlanProposed:
    def test_posts_energy_plan(self, monkeypatch):
        from src import notifier

        posted: list[dict[str, Any]] = []

        def fake_post(url, json=None, headers=None, timeout=None):
            posted.append({"json": json})
            r = MagicMock()
            r.status_code = 200
            return r

        class ImmediateThread:
            def __init__(self, target, daemon=True, name=""):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(notifier.threading, "Thread", ImmediateThread)
        monkeypatch.setattr(notifier.requests, "post", fake_post)
        monkeypatch.setattr(notifier, "config", _make_config())
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })

        notifier.notify_plan_proposed(
            "lp-2026-06-01",
            "2026-06-01",
            "PuLP ok",
            [{"start_time": "2026-06-01T12:00:00Z", "end_time": "2026-06-01T12:30:00Z",
              "action_type": "pre_heat", "params": {"lwt_offset": 2, "tank_temp": 55}}],
        )

        assert len(posted) == 1
        assert posted[0]["json"]["name"] == "EnergyPlan"
        assert "PuLP ok" in posted[0]["json"]["message"]


class TestPushAlert:
    def test_push_cheap_window_posts_hook(self, monkeypatch):
        from src import notifier

        posted: list[Any] = []

        def fake_post(url, json=None, **kwargs):
            posted.append({"url": url, "json": json})
            r = MagicMock()
            r.status_code = 200
            return r

        class ImmediateThread:
            def __init__(self, target, daemon=True, name=""):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(notifier.threading, "Thread", ImmediateThread)
        monkeypatch.setattr(notifier.requests, "post", fake_post)
        monkeypatch.setattr(notifier, "config", _make_config())
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)
        monkeypatch.setattr(notifier.db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "reports",
            "target_override": None, "channel_override": None, "silent": 0,
        })

        notifier.push_cheap_window_start(soc=85.0, fox_mode="Self Use")
        assert len(posted) == 1
        body = posted[0]["json"]
        assert body["name"] == "EnergyCheapWindow"


# ---------------------------------------------------------------------------
# MCP tool: set_notification_route validates alert_type
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional mcp package not installed",
)
class TestMcpSetNotificationRoute:
    def test_rejects_invalid_alert_type(self):
        """set_notification_route must reject unknown alert_type values."""
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        tool_fn = None
        for t in mcp._tool_manager._tools.values():
            if t.name == "set_notification_route":
                tool_fn = t.fn
                break

        if tool_fn is None:
            pytest.skip("Could not locate set_notification_route tool function")

        result = tool_fn(alert_type="does_not_exist")
        assert result["ok"] is False
        assert "Invalid alert_type" in result["error"]

    def test_accepts_valid_alert_type(self, monkeypatch):
        """set_notification_route succeeds for a known alert_type."""
        from src import db as src_db
        from src.mcp_server import build_mcp

        monkeypatch.setattr(src_db, "upsert_notification_route", lambda *a, **kw: None)
        monkeypatch.setattr(src_db, "get_notification_route", lambda _: {
            "enabled": 1, "severity": "critical", "target_override": None,
            "channel_override": None, "silent": 0
        })

        mcp = build_mcp()
        tool_fn = None
        for t in mcp._tool_manager._tools.values():
            if t.name == "set_notification_route":
                tool_fn = t.fn
                break

        if tool_fn is None:
            pytest.skip("Could not locate set_notification_route tool function")

        result = tool_fn(alert_type="risk_alert", enabled=False)
        assert result["ok"] is True
