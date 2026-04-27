"""db.get_hourly_agile_priors — median per UTC hour-of-day from the last N days.

S10.2 (#169) helper used by the LP horizon extender to fill D+1 slots when
Octopus hasn't published tomorrow's prices yet.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_rate(t: datetime, price: float, tariff: str) -> None:
    db.save_agile_rates(
        [{"valid_from": _iso(t), "valid_to": _iso(t + timedelta(minutes=30)), "value_inc_vat": price}],
        tariff,
    )


def test_priors_returns_median_per_hour() -> None:
    """Each hour returns the median of all values seeded into that hour over the window."""
    tariff = "E-1R-AGILE-TEST-PRIORS"
    base = datetime.now(UTC) - timedelta(days=7)
    # Seed three values for hour 12 across three different days:
    #   day 1: 10p, day 2: 20p, day 3: 30p → median 20p
    for day_offset, price in [(0, 10.0), (1, 20.0), (2, 30.0)]:
        t = base.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
        _seed_rate(t, price, tariff)

    priors = db.get_hourly_agile_priors(tariff, window_days=14)
    assert 12 in priors, f"hour 12 missing from priors: {priors}"
    assert priors[12] == pytest.approx(20.0)


def test_priors_window_filters_old_data() -> None:
    """Only data within window_days is included."""
    tariff = "E-1R-AGILE-TEST-WINDOW"
    # Old data (60 days ago) — should be excluded by window_days=14
    old = datetime.now(UTC) - timedelta(days=60)
    _seed_rate(old.replace(hour=10, minute=0), 999.0, tariff)
    # Recent data (3 days ago) — included
    recent = datetime.now(UTC) - timedelta(days=3)
    _seed_rate(recent.replace(hour=10, minute=0), 5.0, tariff)

    priors = db.get_hourly_agile_priors(tariff, window_days=14)
    assert 10 in priors
    # Window-filtered: only recent value (5.0) — old 999.0 should not appear
    assert priors[10] == pytest.approx(5.0)


def test_priors_empty_when_no_history() -> None:
    """No matching tariff rows → empty dict (caller falls back)."""
    priors = db.get_hourly_agile_priors("NON-EXISTENT-TARIFF", window_days=28)
    assert priors == {}


def test_priors_works_for_export_table() -> None:
    """Same helper covers agile_export_rates by passing table='agile_export_rates'."""
    tariff = "E-1R-AGILE-OUTGOING-TEST"
    t = datetime.now(UTC) - timedelta(days=2)
    db.save_agile_export_rates(
        [
            {
                "valid_from": _iso(t.replace(hour=14, minute=0)),
                "valid_to": _iso(t.replace(hour=14, minute=30)),
                "value_inc_vat": 8.5,
            }
        ],
        tariff,
    )
    priors = db.get_hourly_agile_priors(tariff, window_days=14, table="agile_export_rates")
    assert priors.get(14) == pytest.approx(8.5)
