"""Octopus fetch backoff / retry helpers."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import db
from src.scheduler.octopus_fetch import next_retry_seconds, should_run_retry_fetch


def test_next_retry_seconds_staircase() -> None:
    assert next_retry_seconds(1) == 600
    assert next_retry_seconds(2) == 1800
    assert next_retry_seconds(3) == 3600
    assert next_retry_seconds(99) == 3600


def test_should_run_retry_false_without_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        assert should_run_retry_fetch() is False


def test_should_run_retry_true_after_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        now = datetime.now(timezone.utc)
        db.update_octopus_fetch_state(
            consecutive_failures=2,
            failure_streak_started_at=(now - timedelta(hours=2)).isoformat(),
            last_attempt_at=(now - timedelta(seconds=1900)).isoformat(),
        )
        assert should_run_retry_fetch() is True
