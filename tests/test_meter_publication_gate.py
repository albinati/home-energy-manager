"""#795 — a day counts as published by COVERAGE, not by how much energy flowed.

Prod, 2026-08-19..21: the backfill fetched a complete 49-slot day each time and
threw all three away, because total import was 0.257-0.354 kWh and the gate was
``import_total >= 0.5``. Fox independently agreed (0.18-0.30 kWh). The cockpit
read "meter stale, 5 days" and the PnL fell back to CT-clamp estimates for
three real days.

The premise behind the old floor — "a real household never uses less than
~0.5 kWh/day" — fails precisely when the system is working well: a 4.5 kWp
array plus a 10.36 kWh battery with nobody home imports almost nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from src import config as _config
    monkeypatch.setattr(_config.config, "DB_PATH", db_path, raising=False)
    from src import db as _db
    _db.init_db()
    yield


@dataclass
class _FakeSlot:
    interval_start: datetime
    interval_end: datetime
    consumption_kwh: float


def _day_of_slots(day: date, n: int, kwh_each: float) -> list[_FakeSlot]:
    """``n`` consecutive half-hour slots starting at 00:00 UTC on ``day``."""
    base = datetime(day.year, day.month, day.day, 0, 0, tzinfo=UTC)
    return [
        _FakeSlot(base + timedelta(minutes=30 * i),
                  base + timedelta(minutes=30 * (i + 1)), kwh_each)
        for i in range(n)
    ]


def _run_backfill(monkeypatch, target: date, slots: list[_FakeSlot]):
    from src.scheduler import consumption_backfill

    monkeypatch.setattr(consumption_backfill, "_octopus_credentials_ready", lambda: True)
    fake = MagicMock()
    fake.get_mpan_roles.return_value = MagicMock(
        import_mpan="2000000000000", import_serial="ABC123",
        export_mpan=None, export_serial=None,
    )
    fake.fetch_consumption.return_value = slots
    monkeypatch.setitem(__import__("sys").modules, "src.energy.octopus_client", fake)
    return consumption_backfill.backfill_for_date(target)


# ── the backfill gate ────────────────────────────────────────────────────────

def test_complete_low_import_day_is_published(monkeypatch):
    """The exact prod case: a full day that used almost nothing."""
    from src import db

    target = date(2026, 8, 20)
    # 48 slots totalling 0.257 kWh — the real 2026-08-20 figure.
    _run_backfill(monkeypatch, target, _day_of_slots(target, 48, 0.257 / 48))

    row = db.get_octopus_daily_meter(target.isoformat())
    assert row is not None, "a complete day must be cached however little it imported"
    assert row["import_kwh"] == pytest.approx(0.257, abs=1e-6)
    assert row["slots_fetched"] == 48
    assert row["slots_expected"] == 48


def test_partial_day_is_not_published(monkeypatch):
    """2026-08-22 came back with 3 of 48 slots — Octopus had not published."""
    from src import db

    target = date(2026, 8, 22)
    _run_backfill(monkeypatch, target, _day_of_slots(target, 3, 0.5))

    assert db.get_octopus_daily_meter(target.isoformat()) is None


def test_complete_all_zeros_day_is_not_published(monkeypatch):
    """The pre-propagation case the original floor was written for: full
    coverage, but every slot reads zero."""
    from src import db

    target = date(2026, 8, 20)
    _run_backfill(monkeypatch, target, _day_of_slots(target, 48, 0.0))

    assert db.get_octopus_daily_meter(target.isoformat()) is None


def test_empty_fetch_is_not_published(monkeypatch):
    from src import db

    target = date(2026, 8, 20)
    _run_backfill(monkeypatch, target, [])

    assert db.get_octopus_daily_meter(target.isoformat()) is None


def test_high_import_day_still_published(monkeypatch):
    """Control — the ordinary case must be unaffected."""
    from src import db

    target = date(2026, 8, 18)
    _run_backfill(monkeypatch, target, _day_of_slots(target, 48, 14.0 / 48))

    row = db.get_octopus_daily_meter(target.isoformat())
    assert row is not None
    assert row["import_kwh"] == pytest.approx(14.0, abs=1e-6)


def test_expected_slot_count_follows_the_dst_window():
    """A 23 h spring-forward day must be judged against 46 slots, not 48, or a
    complete DST day reads as 96 % coverage and a short one sneaks past."""
    from zoneinfo import ZoneInfo

    from src.scheduler.consumption_backfill import (
        _expected_slot_count,
        _local_day_window_utc,
    )

    tz = ZoneInfo("Europe/London")
    assert _expected_slot_count(*_local_day_window_utc(date(2026, 3, 29), tz)) == 46
    assert _expected_slot_count(*_local_day_window_utc(date(2026, 10, 25), tz)) == 50
    assert _expected_slot_count(*_local_day_window_utc(date(2026, 8, 20), tz)) == 48


# ── the freshness cursor ─────────────────────────────────────────────────────

def test_freshness_counts_a_stamped_low_import_day():
    """Writing the row is only half the fix — the staleness cursor carried the
    same 0.5 kWh filter, so a low-import day would still have read as stale."""
    from src import db

    db.upsert_octopus_daily_meter(
        "2026-08-18", import_kwh=14.0, export_kwh=0.0,
        slots_fetched=48, slots_expected=48,
    )
    db.upsert_octopus_daily_meter(
        "2026-08-20", import_kwh=0.257, export_kwh=5.0,
        slots_fetched=48, slots_expected=48,
    )
    assert db.get_octopus_meter_last_day() == "2026-08-20"


def test_freshness_ignores_a_stamped_partial_day():
    from src import db

    db.upsert_octopus_daily_meter(
        "2026-08-18", import_kwh=14.0, export_kwh=0.0,
        slots_fetched=48, slots_expected=48,
    )
    db.upsert_octopus_daily_meter(
        "2026-08-22", import_kwh=0.011, export_kwh=0.0,
        slots_fetched=3, slots_expected=48,
    )
    assert db.get_octopus_meter_last_day() == "2026-08-18"


def test_freshness_still_excludes_legacy_unstamped_garbage():
    """Pre-migration rows carry no coverage stamp and keep the old energy
    reading, so the known 2026-05-09..20 garbage stays excluded — the
    staleness alarm must not be muted by rows written before the fix."""
    from src import db

    db.upsert_octopus_daily_meter("2026-05-25", import_kwh=9.0, export_kwh=1.0)
    db.upsert_octopus_daily_meter("2026-06-09", import_kwh=None, export_kwh=None)
    db.upsert_octopus_daily_meter("2026-06-10", import_kwh=0.03, export_kwh=0.0)

    assert db.get_octopus_meter_last_day() == "2026-05-25"
