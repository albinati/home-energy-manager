"""apply_safe_defaults: verify Fox set_min_soc tracks MIN_SOC_RESERVE_PERCENT.

Regression test for S10.3 (Epic #167). The Fox API returns 40257 when the
requested min_soc is below the inverter's configured reserve. Hardcoding 10
while the env had MIN_SOC_RESERVE_PERCENT=15 produced the failure on every
boot. Safe-defaults must mirror the configured floor.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from src import db
from src.state_machine import apply_safe_defaults


class _RecordingFox:
    """Minimal FoxESSClient stand-in that records the calls under test."""

    api_key = "test-key"

    def __init__(self) -> None:
        self.set_scheduler_flag_calls: list[bool] = []
        self.set_work_mode_calls: list[str] = []
        self.set_min_soc_calls: list[int] = []

    def set_scheduler_flag(self, flag: bool) -> None:
        self.set_scheduler_flag_calls.append(flag)

    def set_work_mode(self, mode: str) -> None:
        self.set_work_mode_calls.append(mode)

    def set_min_soc(self, value: int) -> None:
        self.set_min_soc_calls.append(int(value))


def _setup_db(monkeypatch: pytest.MonkeyPatch, td: str) -> None:
    monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
    monkeypatch.setattr("src.config.config.OPENCLAW_READ_ONLY", False)
    db.init_db()


def test_set_min_soc_uses_configured_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.config.config.MIN_SOC_RESERVE_PERCENT", 15.0)
    fox = _RecordingFox()
    with tempfile.TemporaryDirectory() as td:
        _setup_db(monkeypatch, td)
        apply_safe_defaults(fox, daikin=None, trigger="test")

    assert fox.set_min_soc_calls == [15], (
        f"expected set_min_soc(15) to mirror MIN_SOC_RESERVE_PERCENT, "
        f"got {fox.set_min_soc_calls!r}"
    )
    assert fox.set_work_mode_calls == ["Self Use"]
    assert fox.set_scheduler_flag_calls == [False]


def test_set_min_soc_clamps_to_valid_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: even a misconfigured reserve never escapes 0..100."""
    monkeypatch.setattr("src.config.config.MIN_SOC_RESERVE_PERCENT", 250.0)
    fox = _RecordingFox()
    with tempfile.TemporaryDirectory() as td:
        _setup_db(monkeypatch, td)
        apply_safe_defaults(fox, daikin=None, trigger="test")
    assert fox.set_min_soc_calls == [100]

    monkeypatch.setattr("src.config.config.MIN_SOC_RESERVE_PERCENT", -5.0)
    fox = _RecordingFox()
    with tempfile.TemporaryDirectory() as td:
        _setup_db(monkeypatch, td)
        apply_safe_defaults(fox, daikin=None, trigger="test")
    assert fox.set_min_soc_calls == [0]


def test_partial_failure_isolates_step_in_action_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one Fox endpoint rejects (e.g. 40257 on set_work_mode), the action_log
    row must identify exactly which step failed — not bundle them all into a
    single "failure" with no per-call attribution. This is the diagnostic
    foundation for tracking down vendor-API drift.
    """
    from src.foxess.client import FoxESSError

    class _PartialFox(_RecordingFox):
        def set_work_mode(self, mode: str) -> None:
            self.set_work_mode_calls.append(mode)
            raise FoxESSError("API error 40257: simulated rejection")

    monkeypatch.setattr("src.config.config.MIN_SOC_RESERVE_PERCENT", 15.0)
    fox = _PartialFox()
    with tempfile.TemporaryDirectory() as td:
        _setup_db(monkeypatch, td)
        apply_safe_defaults(fox, daikin=None, trigger="test")

        rows = list(db.get_connection().execute(
            "SELECT result, error_msg, params FROM action_log WHERE action='apply_safe_defaults'"
        ))
        assert len(rows) == 1
        result, error_msg, params = rows[0]
        assert result == "partial"  # other steps succeeded; only work_mode failed
        assert "work_mode" in error_msg
        assert "40257" in error_msg
        # The successful steps must still have happened (not blocked by the failure)
        assert fox.set_scheduler_flag_calls == [False]
        assert fox.set_min_soc_calls == [15]
