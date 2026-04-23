"""v10.2 E1.S1 — Fox read-through daily cache.

The cache is SQLite-first. ``ensure_fox_month_cached(year, month)`` should:
  - Hit Fox cloud at most once per (year, month) when nothing is cached.
  - Hit Fox zero times when every day in the month is already cached.
  - Skip future dates entirely (no cloud call for them).
  - Skip today by default unless ``force=True`` (today is volatile).
  - Re-fetch only the missing days when the month is partially cached.

Tests use a fake FoxESSClient — never touches the real cloud.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from src import db
from src.foxess import service as fox_svc


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


class _FakeFoxClient:
    """Returns a deterministic month: every day = (day_idx, 1, 2, 3) kWh."""

    def __init__(self):
        self.call_count = 0
        self.last_args: tuple[int, int] | None = None

    def get_energy_month_daily_breakdown(self, year: int, month: int):
        self.call_count += 1
        self.last_args = (year, month)
        from calendar import monthrange
        _, ndays = monthrange(year, month)
        days = [
            {
                "date": f"{year:04d}-{month:02d}-{d:02d}",
                "solar_kwh": float(d),
                "load_kwh": 1.0,
                "import_kwh": 2.0,
                "export_kwh": 0.5,
                "charge_kwh": 1.0,
                "discharge_kwh": 1.0,
            }
            for d in range(1, ndays + 1)
        ]
        return ({"sum_solar": sum(x["solar_kwh"] for x in days)}, days)


def test_first_call_fetches_then_caches_full_month(monkeypatch):
    fake = _FakeFoxClient()
    monkeypatch.setattr(fox_svc, "_get_client", lambda: fake)

    rows = fox_svc.ensure_fox_month_cached(2024, 11)  # past month, fully fetchable
    assert fake.call_count == 1, f"first call must hit Fox once; got {fake.call_count}"
    assert len(rows) == 30, f"November has 30 days; got {len(rows)}"


def test_second_call_zero_cloud_hits_when_complete(monkeypatch):
    fake = _FakeFoxClient()
    monkeypatch.setattr(fox_svc, "_get_client", lambda: fake)

    fox_svc.ensure_fox_month_cached(2024, 11)
    fake.call_count = 0  # reset
    rows = fox_svc.ensure_fox_month_cached(2024, 11)
    assert fake.call_count == 0, "fully-cached month must NOT hit Fox"
    assert len(rows) == 30


def test_partial_month_refetches_only_when_missing(monkeypatch):
    """Insert a few rows manually; ensure_fox_month_cached fills the gaps."""
    fake = _FakeFoxClient()
    monkeypatch.setattr(fox_svc, "_get_client", lambda: fake)

    db.upsert_fox_energy_daily([
        {"date": "2024-11-05", "solar_kwh": 5.0, "load_kwh": 1.0,
         "import_kwh": 2.0, "export_kwh": 0.5, "charge_kwh": 1.0, "discharge_kwh": 1.0},
        {"date": "2024-11-10", "solar_kwh": 10.0, "load_kwh": 1.0,
         "import_kwh": 2.0, "export_kwh": 0.5, "charge_kwh": 1.0, "discharge_kwh": 1.0},
    ])
    fake.call_count = 0

    rows = fox_svc.ensure_fox_month_cached(2024, 11)
    # The missing days trigger one cloud call; that call returns the whole
    # month. ensure_fox_month_cached only upserts rows it needs.
    assert fake.call_count == 1, f"partial month should fire one cloud call; got {fake.call_count}"
    assert len(rows) == 30


def test_skips_future_dates(monkeypatch):
    fake = _FakeFoxClient()
    monkeypatch.setattr(fox_svc, "_get_client", lambda: fake)

    future = date.today() + timedelta(days=400)
    rows = fox_svc.ensure_fox_month_cached(future.year, future.month)
    assert fake.call_count == 0, "future month must not hit Fox"
    assert rows == []


def test_force_refresh_re_fetches(monkeypatch):
    fake = _FakeFoxClient()
    monkeypatch.setattr(fox_svc, "_get_client", lambda: fake)

    fox_svc.ensure_fox_month_cached(2024, 11)
    fake.call_count = 0
    fox_svc.ensure_fox_month_cached(2024, 11, force=True)
    assert fake.call_count == 1, "force=True must re-fetch even when fully cached"


def test_get_fox_daily_cached_round_trip(monkeypatch):
    fake = _FakeFoxClient()
    monkeypatch.setattr(fox_svc, "_get_client", lambda: fake)

    row = fox_svc.get_fox_daily_cached(date(2024, 11, 15))
    assert row is not None
    assert row["date"] == "2024-11-15"
    assert row["solar_kwh"] == 15.0
    # Second call hits cache, no cloud
    fake.call_count = 0
    row2 = fox_svc.get_fox_daily_cached(date(2024, 11, 15))
    assert fake.call_count == 0
    assert row2["date"] == "2024-11-15"
