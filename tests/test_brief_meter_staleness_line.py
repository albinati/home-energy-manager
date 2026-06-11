"""``_meter_staleness_line`` (#533) — the brief must ALARM when the Octopus
metered feed stalls, instead of degrading silently to Fox-only PnL.

Background: between 2026-05-09 and 2026-05-30 the smart meter stopped
publishing half-hourly data; the per-day audit line just said "not yet
published" every day, which reads as routine lag. Nobody noticed for a
month. This line trips only on multi-day staleness.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.analytics import daily_brief
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    from src import db as _db
    _db.init_db()
    yield


def test_silent_when_meter_fresh(monkeypatch):
    from src import db
    db.upsert_octopus_daily_meter("2026-06-09", import_kwh=9.0, export_kwh=1.0)
    assert daily_brief._meter_staleness_line(date(2026, 6, 11)) is None


def test_warns_when_meter_stale_beyond_threshold(monkeypatch):
    from src import db
    db.upsert_octopus_daily_meter("2026-05-25", import_kwh=9.0, export_kwh=1.0)
    line = daily_brief._meter_staleness_line(date(2026, 6, 11))
    assert line is not None
    assert "2026-05-25" in line
    assert "17 days" in line


def test_warns_when_no_meter_rows_at_all():
    line = daily_brief._meter_staleness_line(date(2026, 6, 11))
    assert line is not None
    assert "Fox-only" in line


def test_disabled_by_zero_threshold(monkeypatch):
    monkeypatch.setattr(app_config, "CONSUMPTION_METER_STALE_DAYS", 0, raising=False)
    assert daily_brief._meter_staleness_line(date(2026, 6, 11)) is None
