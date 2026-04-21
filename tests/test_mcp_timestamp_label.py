"""Tests for ``src.mcp_server._augment_actions_with_local_time`` (#47).

The scheduler's raw UTC timestamps in MCP responses are what misled the
OpenClaw agent into filing #47 (it saw ``"2026-04-21T15:00:00Z"`` and
concluded the peak window was firing an hour early, missing the UTC→BST
conversion entirely). The augmenter adds a human-readable local sibling
alongside the canonical UTC so no agent can repeat that mistake.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="Install the `mcp` package to run MCP tests.")

from src.mcp_server import _augment_actions_with_local_time


def test_augment_bst_row_has_local_sibling() -> None:
    """BST: UTC 15:00Z row surfaces as ``16:00:00 BST`` in the local sibling.

    Canonical UTC field is UNCHANGED — consumers that rely on it still work.
    """
    rows = [
        {
            "start_time": "2026-06-15T15:00:00Z",
            "end_time": "2026-06-15T15:30:00Z",
            "device": "daikin",
            "action_type": "shutdown",
        }
    ]
    out = _augment_actions_with_local_time(rows, "Europe/London")

    assert out[0]["start_time"] == "2026-06-15T15:00:00Z"
    assert out[0]["end_time"] == "2026-06-15T15:30:00Z"
    assert out[0]["start_time_local"] == "2026-06-15T16:00:00 BST"
    assert out[0]["end_time_local"] == "2026-06-15T16:30:00 BST"


def test_augment_gmt_row_has_local_sibling() -> None:
    """GMT (winter): UTC 15:00Z row surfaces as ``15:00:00 GMT`` — same digits but
    the ``GMT`` suffix removes any ambiguity."""
    rows = [
        {
            "start_time": "2026-12-15T15:00:00Z",
            "end_time": "2026-12-15T15:30:00Z",
        }
    ]
    out = _augment_actions_with_local_time(rows, "Europe/London")
    assert out[0]["start_time_local"] == "2026-12-15T15:00:00 GMT"
    assert out[0]["end_time_local"] == "2026-12-15T15:30:00 GMT"


def test_augment_skips_malformed_without_crashing() -> None:
    """Garbage input must not crash the augmenter. Rows with unparseable
    timestamps keep their canonical fields but get no `_local` sibling."""
    rows = [
        {"start_time": "not-a-date", "end_time": None},
        {"start_time": "2026-06-15T15:00:00Z"},  # missing end_time
    ]
    out = _augment_actions_with_local_time(rows, "Europe/London")

    assert "start_time_local" not in out[0]
    assert "end_time_local" not in out[0]
    assert out[1]["start_time_local"] == "2026-06-15T16:00:00 BST"
    assert "end_time_local" not in out[1]


def test_augment_is_non_destructive() -> None:
    """The input list must not be mutated — pure function."""
    rows = [{"start_time": "2026-06-15T15:00:00Z", "end_time": "2026-06-15T15:30:00Z"}]
    snapshot = [dict(r) for r in rows]
    _augment_actions_with_local_time(rows, "Europe/London")
    assert rows == snapshot, "input rows were mutated — augmenter must return a copy"
