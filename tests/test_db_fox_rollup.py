"""compute_fox_energy_daily_from_realtime — local rollup of pv_realtime_history.

S10.10 (#177): replaces the broken Fox Cloud per-day API rollup with local
trapezoidal integration over telemetry the heartbeat captures every ~3 min.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _seed_sample(t: datetime, **fields: float) -> None:
    db.save_pv_realtime_sample(t.isoformat().replace("+00:00", "Z"), **fields)


def test_rollup_basic_trapezoidal_integration() -> None:
    """Two samples 1 hour apart at constant 1 kW solar → 1 kWh integrated."""
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    _seed_sample(base, solar_power_kw=1.0, load_power_kw=0.5)
    _seed_sample(base + timedelta(hours=1), solar_power_kw=1.0, load_power_kw=0.5)
    # ...need a sample on day boundary to NOT contribute, so cap behaviour is OK
    rows = db.compute_fox_energy_daily_from_realtime(
        start_date="2026-04-26", end_date="2026-04-26", max_gap_seconds=7200,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2026-04-26"
    # 1 hour at 1 kW → 1 kWh; load 0.5 kW for 1h → 0.5 kWh
    assert r["solar_kwh"] == pytest.approx(1.0, abs=0.01)
    assert r["load_kwh"] == pytest.approx(0.5, abs=0.01)


def test_rollup_caps_long_gap_at_max_gap_seconds() -> None:
    """A multi-hour heartbeat outage must NOT extrapolate constant power
    across the gap — the cap limits each interval."""
    base = datetime(2026, 4, 26, 0, 0, tzinfo=UTC)
    _seed_sample(base, solar_power_kw=2.0)
    # 6 hours later, still 2 kW solar — but cap at 1800 s = 30 min (default)
    _seed_sample(base + timedelta(hours=6), solar_power_kw=2.0)

    rows = db.compute_fox_energy_daily_from_realtime(
        start_date="2026-04-26", end_date="2026-04-26",
    )
    # Without cap: 2 kW × 6 h = 12 kWh.
    # With default cap 30 min: 2 kW × 0.5 h = 1.0 kWh
    assert len(rows) == 1
    assert rows[0]["solar_kwh"] == pytest.approx(1.0, abs=0.01), (
        f"expected gap-capped integration to limit total to ~1.0 kWh; got {rows[0]['solar_kwh']}"
    )


def test_rollup_no_samples_returns_empty() -> None:
    rows = db.compute_fox_energy_daily_from_realtime(
        start_date="2026-01-01", end_date="2026-01-02",
    )
    assert rows == []


def test_rollup_per_day_separation() -> None:
    """Samples on two different UTC days must produce two separate rows."""
    base = datetime(2026, 4, 26, 23, 0, tzinfo=UTC)
    _seed_sample(base, solar_power_kw=1.0)
    _seed_sample(base + timedelta(minutes=30), solar_power_kw=1.0)  # still 26th
    _seed_sample(base + timedelta(hours=2), solar_power_kw=1.0)     # 27th 01:00
    _seed_sample(base + timedelta(hours=2, minutes=30), solar_power_kw=1.0)  # 27th 01:30

    rows = db.compute_fox_energy_daily_from_realtime(
        start_date="2026-04-26", end_date="2026-04-27", max_gap_seconds=7200,
    )
    by_date = {r["date"]: r for r in rows}
    assert "2026-04-26" in by_date
    assert "2026-04-27" in by_date
