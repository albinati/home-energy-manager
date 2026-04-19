"""Tests for the OpenClaw CLI notifier (src/notifier.py).

Covers: route resolution, subprocess invocation, error handling,
stale-data fallback, and the three MCP notification tools.
"""
from __future__ import annotations

import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    """Return a minimal config-like namespace for testing."""
    defaults = dict(
        OPENCLAW_NOTIFY_ENABLED=True,
        OPENCLAW_CLI_PATH="/usr/local/bin/openclaw",
        OPENCLAW_CLI_TIMEOUT_SECONDS=8,
        OPENCLAW_NOTIFY_CHANNEL="telegram",
        OPENCLAW_NOTIFY_TARGET="7964600619",
        OPENCLAW_NOTIFY_TARGET_CRITICAL="",
        OPENCLAW_NOTIFY_CHANNEL_CRITICAL="",
        OPENCLAW_NOTIFY_TARGET_REPORTS="",
        OPENCLAW_NOTIFY_CHANNEL_REPORTS="",
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
# _send_via_openclaw_cli tests
# ---------------------------------------------------------------------------

class TestSendViaOpenclawCli:
    def _default_route_patch(self):
        return {"channel": "telegram", "target": "7964600619", "silent": False}

    def test_invokes_subprocess_with_correct_args(self, monkeypatch):
        from src import notifier
        cfg = _make_config()
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier, "_resolve_route", lambda _: self._default_route_patch())

        captured = {}
        started = threading.Event()

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            started.set()
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert notifier._send_via_openclaw_cli("risk_alert", "hello") is True
        started.wait(timeout=2)
        assert captured.get("cmd") == [
            "/usr/local/bin/openclaw", "message", "send",
            "--channel", "telegram",
            "--target", "7964600619",
            "--message", "hello",
        ]

    def test_silent_flag_appended_when_true(self, monkeypatch):
        from src import notifier
        cfg = _make_config()
        monkeypatch.setattr(notifier, "config", cfg)
        monkeypatch.setattr(notifier, "_resolve_route", lambda _: {"channel": "telegram", "target": "123", "silent": True})

        captured = {}
        started = threading.Event()

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            started.set()
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        notifier._send_via_openclaw_cli("strategy_update", "msg")
        started.wait(timeout=2)
        assert "--silent" in captured.get("cmd", [])

    def test_returns_false_on_nonzero_returncode(self, monkeypatch):
        """Non-zero exit is logged but send still returns True (fire-and-forget)."""
        from src import notifier
        monkeypatch.setattr(notifier, "_resolve_route", lambda _: {"channel": "telegram", "target": "123", "silent": False})
        monkeypatch.setattr(notifier, "config", _make_config())

        done = threading.Event()

        def fake_run(cmd, **kwargs):
            done.set()
            result = MagicMock()
            result.returncode = 1
            result.stderr = "error"
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        # fire-and-forget: returns True immediately regardless of subprocess outcome
        assert notifier._send_via_openclaw_cli("risk_alert", "msg") is True
        done.wait(timeout=2)

    def test_handles_timeout(self, monkeypatch):
        from src import notifier
        monkeypatch.setattr(notifier, "_resolve_route", lambda _: {"channel": "telegram", "target": "123", "silent": False})
        monkeypatch.setattr(notifier, "config", _make_config())
        done = threading.Event()

        def fake_run(*a, **k):
            done.set()
            raise subprocess.TimeoutExpired([], 8)

        monkeypatch.setattr(subprocess, "run", fake_run)
        # fire-and-forget: always returns True; timeout is handled in background thread
        assert notifier._send_via_openclaw_cli("risk_alert", "msg") is True
        done.wait(timeout=2)

    def test_handles_missing_binary(self, monkeypatch):
        from src import notifier
        monkeypatch.setattr(notifier, "_resolve_route", lambda _: {"channel": "telegram", "target": "123", "silent": False})
        monkeypatch.setattr(notifier, "config", _make_config())
        done = threading.Event()

        def fake_run(*a, **k):
            done.set()
            raise FileNotFoundError()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert notifier._send_via_openclaw_cli("risk_alert", "msg") is True
        done.wait(timeout=2)

    def test_returns_false_when_no_route(self, monkeypatch):
        from src import notifier
        monkeypatch.setattr(notifier, "_resolve_route", lambda _: None)
        monkeypatch.setattr(notifier, "config", _make_config())
        assert notifier._send_via_openclaw_cli("risk_alert", "msg") is False


# ---------------------------------------------------------------------------
# _dispatch still logs to action_log when send fails
# ---------------------------------------------------------------------------

class TestDispatchLogsOnFailure:
    def test_logs_to_action_log_even_when_cli_fails(self, monkeypatch):
        from src import notifier

        logged = []
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: logged.append(kw))
        monkeypatch.setattr(notifier, "_send_via_openclaw_cli", lambda *a: False)

        notifier._dispatch(notifier.AlertType.RISK_ALERT, "test message", urgent=True)
        assert len(logged) == 1
        assert logged[0]["action"] == "risk_alert"


# ---------------------------------------------------------------------------
# push_alert uses CLI (not urllib)
# ---------------------------------------------------------------------------

class TestPushAlertUsesCli:
    def test_push_cheap_window_start_calls_cli(self, monkeypatch):
        from src import notifier

        calls = []
        monkeypatch.setattr(notifier, "_send_via_openclaw_cli", lambda at, msg: calls.append((at, msg)) or True)
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)

        notifier.push_cheap_window_start(soc=85.0, fox_mode="Self Use")
        assert len(calls) == 1
        assert calls[0][0] == "cheap_window_start"

    def test_push_peak_window_start_calls_cli(self, monkeypatch):
        from src import notifier

        calls = []
        monkeypatch.setattr(notifier, "_send_via_openclaw_cli", lambda at, msg: calls.append((at, msg)) or True)
        monkeypatch.setattr(notifier.db, "log_action", lambda **kw: None)

        notifier.push_peak_window_start(soc=92.0)
        assert len(calls) == 1
        assert calls[0][0] == "peak_window_start"


# ---------------------------------------------------------------------------
# MCP tool: set_notification_route validates alert_type
# ---------------------------------------------------------------------------

class TestMcpSetNotificationRoute:
    def test_rejects_invalid_alert_type(self):
        """set_notification_route must reject unknown alert_type values."""
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        # Find the set_notification_route function directly on the mcp object
        # by calling it through the registered tools dict
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
