"""GET /api/v1/pv/clearness — daily PV vs the rolling clear-sky envelope.

Sky vs system: the envelope is the array's own best trailing-21d day (Fox
meter), so a haze day reads as low clearness under a stable ceiling while a
system fault would drag the ceiling itself down within a window.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.api.routers.pv import get_pv_clearness


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "DB_PATH", str(db_path))
    db.init_db()
    yield db_path


def _seed(day: str, solar: float, load: float = 15.0) -> None:
    conn = sqlite3.connect(str(db.config.DB_PATH))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO fox_energy_daily "
            "(date, solar_kwh, load_kwh, import_kwh, export_kwh, charge_kwh, discharge_kwh, fetched_at) "
            "VALUES (?, ?, ?, 2.0, 1.0, 3.0, 3.0, 0)",
            (day, solar, load),
        )
        conn.commit()
    finally:
        conn.close()


def test_clearness_is_solar_over_trailing_max():
    today = datetime.now(UTC).date()  # the endpoint iterates UTC dates (Fox rollup buckets UTC)
    # A clear 24-kWh day inside the window, then a hazy 16-kWh day. Day-2, not
    # day-1: yesterday is legitimately PARTIAL until the ~02:30 UTC rollup.
    _seed((today - timedelta(days=5)).isoformat(), 24.0)
    _seed((today - timedelta(days=2)).isoformat(), 16.0)
    out = asyncio.run(get_pv_clearness(days=10))
    by = {d["date"]: d for d in out["days"]}
    hazy = by[(today - timedelta(days=2)).isoformat()]
    assert hazy["envelope_kwh"] == 24.0
    assert hazy["clearness"] == pytest.approx(16 / 24, abs=0.01)
    clear = by[(today - timedelta(days=5)).isoformat()]
    assert clear["clearness"] == pytest.approx(1.0)


def test_unfinalised_days_are_partial_with_no_verdict():
    today = datetime.now(UTC).date()
    _seed((today - timedelta(days=3)).isoformat(), 20.0)
    # Accumulating rows (today; yesterday before the ~02:30 UTC rollup) are
    # tiny stubs — they must not drag the envelope down NOR get a confident
    # "Sky 0%" verdict (review finding: that read wrong every night).
    _seed(today.isoformat(), 1.2)
    out = asyncio.run(get_pv_clearness(days=5))
    trow = out["days"][-1]
    assert trow["date"] == today.isoformat()
    assert trow["partial"] is True
    assert trow["clearness"] is None
    assert trow["envelope_kwh"] == 20.0
    # Yesterday's flag depends on whether its rollup ran (03:00 UTC boundary) —
    # whichever way, a partial day never carries a clearness verdict.
    yrow = out["days"][-2]
    if yrow["partial"]:
        assert yrow["clearness"] is None


def test_missing_days_yield_nulls_not_errors():
    out = asyncio.run(get_pv_clearness(days=7))
    assert len(out["days"]) == 7
    assert all(d["solar_kwh"] is None and d["clearness"] is None for d in out["days"][:-1])
