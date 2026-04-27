"""db.get_half_hourly_agile_priors — median per UTC (hour, minute) from the last N days.

S10.2 (#169) helper used by the LP horizon extender to fill D+1 slots when
Octopus hasn't published tomorrow's prices yet. S10.8 (#175) refined to
half-hour granularity.
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


def test_priors_returns_median_per_half_hour() -> None:
    """Each (hour, minute) bucket returns the median of all values seeded into it."""
    tariff = "E-1R-AGILE-TEST-PRIORS"
    base = datetime.now(UTC) - timedelta(days=7)
    # Seed three values for slot 12:00 across three days → median 20p
    for day_offset, price in [(0, 10.0), (1, 20.0), (2, 30.0)]:
        t = base.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
        _seed_rate(t, price, tariff)
    # And different prices for slot 12:30 → distinct median
    for day_offset, price in [(0, 100.0), (1, 200.0), (2, 300.0)]:
        t = base.replace(hour=12, minute=30, second=0, microsecond=0) + timedelta(days=day_offset)
        _seed_rate(t, price, tariff)

    priors = db.get_half_hourly_agile_priors(tariff, window_days=14)
    assert (12, 0) in priors and (12, 30) in priors
    assert priors[(12, 0)] == pytest.approx(20.0)
    assert priors[(12, 30)] == pytest.approx(200.0)
    assert priors[(12, 0)] != priors[(12, 30)], (
        "S10.8 (#175): the two half-hours of the same hour must be separately bucketed"
    )


def test_priors_window_filters_old_data() -> None:
    """Only data within window_days is included."""
    tariff = "E-1R-AGILE-TEST-WINDOW"
    old = datetime.now(UTC) - timedelta(days=60)
    _seed_rate(old.replace(hour=10, minute=0), 999.0, tariff)
    recent = datetime.now(UTC) - timedelta(days=3)
    _seed_rate(recent.replace(hour=10, minute=0), 5.0, tariff)

    priors = db.get_half_hourly_agile_priors(tariff, window_days=14)
    assert (10, 0) in priors
    assert priors[(10, 0)] == pytest.approx(5.0)


def test_priors_empty_when_no_history() -> None:
    """No matching tariff rows → empty dict."""
    priors = db.get_half_hourly_agile_priors("NON-EXISTENT-TARIFF", window_days=28)
    assert priors == {}


def test_priors_works_for_export_table() -> None:
    """Same helper covers agile_export_rates."""
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
    priors = db.get_half_hourly_agile_priors(tariff, window_days=14, table="agile_export_rates")
    assert priors.get((14, 0)) == pytest.approx(8.5)
