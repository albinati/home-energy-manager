"""Phase 4.5 (#44) — OpenClaw MCP-only boundary self-check.

On boot, we audit every hardware-write tool (set_daikin_*, set_inverter_*)
and emit a WARN when any lacks a ``confirmed`` parameter. That's the only
enforceable user-facing gate between OpenClaw and hardware.

This test proves the self-check fires when a write tool regresses by
forcibly stripping ``confirmed`` from one tool and asserting the warning.
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("mcp", reason="Install the `mcp` package to run MCP server tests.")

from src.mcp_server import audit_mcp_tool_surface, build_mcp


def test_audit_clean_on_current_surface() -> None:
    """All set_daikin_* tools currently have a `confirmed` parameter — audit is clean for them."""
    mcp = build_mcp()
    warnings = audit_mcp_tool_surface(mcp)
    # We allow set_inverter_mode to show up (it doesn't have `confirmed`) but must not
    # regress any `set_daikin_*` tool.
    regressions = [w for w in warnings if "set_daikin_" in w]
    assert regressions == [], f"set_daikin_* tools must retain `confirmed`: {regressions}"


def test_audit_warns_when_confirmed_stripped(caplog) -> None:
    """Removing `confirmed` from a set_daikin_* tool triggers a WARN log."""
    mcp = build_mcp()
    tm = mcp._tool_manager
    tool = tm._tools.get("set_daikin_power")
    assert tool is not None

    # Surgically strip `confirmed` from the tool schema to simulate a regression.
    original_params = tool.parameters
    doctored = {
        **original_params,
        "properties": {
            k: v for k, v in original_params.get("properties", {}).items() if k != "confirmed"
        },
    }
    tool.parameters = doctored
    try:
        with caplog.at_level(logging.WARNING, logger="src.mcp_server"):
            warnings = audit_mcp_tool_surface(mcp)
    finally:
        tool.parameters = original_params

    assert any("set_daikin_power" in w and "confirmed" in w for w in warnings)
    assert any(
        "set_daikin_power" in rec.message for rec in caplog.records if rec.levelno == logging.WARNING
    )
