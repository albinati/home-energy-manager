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
    (the fallback string path) instead of raising."""
    from src.analytics.daily_brief import build_morning_payload

    body = build_morning_payload()
    assert "Morning brief" in body
    assert "Today" in body


def test_night_payload_renders_with_zero_data():
    """Empty execution_log + no peak-export decisions → realistic zero summary."""
    from src.analytics.daily_brief import build_night_payload

    body = build_night_payload()
    assert "Night brief" in body
    assert "actuals" in body
    # The brief now uses the structured "Net cost" line (was "Realised cost") —
    # see the daily-brief expansion for #207's follow-up. Either is acceptable.
    assert "Net cost:" in body


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


def test_legacy_aliases_preserved():
    """Old callers using build_daily_brief_text / send_daily_brief_webhook
    must keep working — they delegate to morning."""
    import src.analytics.daily_brief as db_mod

    assert db_mod.build_daily_brief_text() == db_mod.build_morning_payload()
    # send_daily_brief_webhook now points at send_morning_brief_webhook —
    # symbol-identity check.
    assert db_mod.send_daily_brief_webhook is db_mod.send_morning_brief_webhook


def test_morning_payload_includes_tier_summary_when_rates_present(monkeypatch):
    """When agile_rates is populated, the morning brief shows the tier-window
    summary (reusing the same classify_day call as the family calendar)."""
    from datetime import UTC, date, datetime, timedelta
    from src import db
    from src.analytics import daily_brief as db_mod

    monkeypatch.setattr(db_mod.config, "OCTOPUS_TARIFF_CODE", "TEST", raising=False)
    monkeypatch.setattr(db_mod.config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)

    today = date.today()
    rows = []
    for i in range(48):
        # Two-tier day: 24 cheap (8p), 24 expensive (32p).
        price = 8.0 if i < 24 else 32.0
        st = datetime(today.year, today.month, today.day, 0, 0, tzinfo=UTC) + timedelta(minutes=30 * i)
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
    assert "Tariff windows today:" in body
