"""Tests for the morning + night brief split (V12)."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """Each test gets a fresh DB so daily_target / dispatch_decisions reads
    don't cross-contaminate."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from src import config as _config
    monkeypatch.setattr(_config.config, "DB_PATH", db_path, raising=False)
    from src import db as _db
    _db.init_db()
    yield


def test_morning_payload_renders_without_target_row():
    """Cold-start day with no daily_targets row should still produce a payload
    (the fallback string path) instead of raising.

    Post-#330 the body no longer carries its own "## ☀️ Morning brief" title
    line — the notifier prepends the headline on the Telegram path. The body
    starts at ``**Today (...)**`` straight away.
    """
    from src.analytics.daily_brief import build_morning_payload

    body = build_morning_payload()
    assert "Today" in body
    assert "Mode:" in body
    # Body MUST NOT carry the old "## Morning brief" header — that was the
    # source of the stacked-title bug on Telegram.
    assert "Morning brief" not in body


def test_night_payload_renders_with_zero_data():
    """Empty execution_log → night brief still renders cleanly via the
    2026-05-21 redesign. Mode line + PnL line always present; the
    today-vs-forecast / battery / Daikin sections degrade to empty silently."""
    from src.analytics.daily_brief import build_night_payload

    body = build_night_payload()
    assert "actuals" in body
    # Body must not contain the old "## Night brief" header (#330).
    assert "Night brief" not in body
    # 2026-05-21 redesign: PnL is now a single line ("PnL today:") not a
    # multi-line block ("Net cost:" / "Energy used:" / ...).
    assert "**PnL today:**" in body
    # Mode chip always renders
    assert "**Mode:**" in body


def test_morning_and_night_are_independent_calls():
    """Both payload builders are pure functions; calling one doesn't mutate
    state needed by the other."""
    from src.analytics.daily_brief import build_morning_payload, build_night_payload

    m1 = build_morning_payload()
    n1 = build_night_payload()
    m2 = build_morning_payload()
    n2 = build_night_payload()
    assert m1 == m2
    assert n1 == n2


def test_morning_payload_includes_tomorrow_peaks_when_rates_present(monkeypatch):
    """2026-05-21 redesign moved today's tariff-tier breakdown out of the
    morning brief (it's now forward-looking only). Tomorrow's peak windows
    stay — they're the actionable signal for 'when will the LP work hardest'.
    Today's full tier breakdown lives in the daily calendar + audit MCP tools."""
    from datetime import UTC, date, datetime, timedelta
    from zoneinfo import ZoneInfo
    from src import db
    from src.analytics import daily_brief as db_mod

    monkeypatch.setattr(db_mod.config, "OCTOPUS_TARIFF_CODE", "TEST", raising=False)
    monkeypatch.setattr(db_mod.config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)

    # Use the same timezone as build_morning_payload so the assertion holds
    # across midnight UTC (CI runs anywhere — UTC, BST, etc.). Without this,
    # the test silently flakes when UTC and London disagree on "today".
    today = datetime.now(ZoneInfo("Europe/London")).date()
    tomorrow = today + timedelta(days=1)
    rows = []
    # Seed TOMORROW's rates with a clear peak window so the heads-up renders.
    for i in range(48):
        # Two-tier day: 24 cheap (8p), 24 expensive peak (32p).
        price = 8.0 if i < 24 else 32.0
        st = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, tzinfo=UTC) + timedelta(minutes=30 * i)
        rows.append({
            "valid_from": st.isoformat().replace("+00:00", "Z"),
            "valid_to": (st + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": price,
            "tariff_code": "TEST",
            "fetched_at": "now",
        })
    db.save_agile_rates([{
        "valid_from": r["valid_from"],
        "valid_to": r["valid_to"],
        "value_inc_vat": r["value_inc_vat"],
    } for r in rows], "TEST")

    body = db_mod.build_morning_payload()
    assert f"**Tomorrow ({tomorrow}):**" in body
    # And the peak window is mentioned (tomorrow has 32p prices)
    assert "peak" in body.lower()
