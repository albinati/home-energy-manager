"""Tests for src/api_quota.py — persistent quota tracking and enforcement."""
import time

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own SQLite file so quota counts don't bleed across tests."""
    db_file = str(tmp_path / "test_quota.db")
    monkeypatch.setenv("DB_PATH", db_file)
    # Force config to reload DB_PATH
    import importlib

    import src.config as cfg_mod
    importlib.reload(cfg_mod)
    import src.api_quota as aq
    importlib.reload(aq)
    aq.ensure_table()
    yield aq
    importlib.reload(cfg_mod)
    importlib.reload(aq)


def test_record_and_count(isolated_db):
    aq = isolated_db
    assert aq.count_calls_24h("fox") == 0
    aq.record_call("fox", "read", ok=True)
    aq.record_call("fox", "read", ok=True)
    aq.record_call("daikin", "read", ok=True)
    assert aq.count_calls_24h("fox") == 2
    assert aq.count_calls_24h("daikin") == 1


def test_quota_remaining_and_block(isolated_db):
    aq = isolated_db
    # Default budgets: daikin=180, fox=1200
    assert aq.quota_remaining("daikin") == 180
    assert not aq.should_block("daikin")

    # Fill quota
    for _ in range(180):
        aq.record_call("daikin", "read", ok=True)
    assert aq.quota_remaining("daikin") == 0
    assert aq.should_block("daikin")


def test_get_quota_status_structure(isolated_db):
    aq = isolated_db
    aq.record_call("fox", "write", ok=True)
    status = aq.get_quota_status("fox")
    assert "quota_used_24h" in status
    assert "quota_remaining_24h" in status
    assert "daily_budget" in status
    assert "blocked" in status
    assert status["quota_used_24h"] == 1
    assert not status["blocked"]


def test_old_entries_not_counted(isolated_db):
    """Calls older than 24h must not count towards today's quota."""
    import sqlite3
    from pathlib import Path
    aq = isolated_db
    from src.config import config
    db_path = Path(config.DB_PATH).expanduser().resolve()

    # Insert a call 25 hours ago
    old_ts = time.time() - 25 * 3600
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO api_call_log (vendor, kind, ts_utc, ok) VALUES (?, ?, ?, ?)",
        ("fox", "read", old_ts, 1),
    )
    conn.commit()
    conn.close()

    assert aq.count_calls_24h("fox") == 0
