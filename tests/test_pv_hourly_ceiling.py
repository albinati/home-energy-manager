"""PV forecast ceiling (_build_pv_hourly_ceiling) — a SAFETY RAIL whose only
correct failure mode is being too loose.

Regression cover for the 2026-07-21..23 incident: the previous estimator
distributed Fox DAILY totals across hours with a fixed sinusoid, which is far
flatter than a real PV curve. In prod it produced 1.32-1.43 kWh/slot for
11-13 UTC against a MEDIAN realised 1.35-1.45 — so the rail bound on ordinary
midday slots, truncating the committed forecast and censoring the error signal
the recent-bias corrector trains on.
"""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.config import config


def _seed_actual(conn, *, hour: int, days_ago: int, actual: float, minute: int = 0):
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    conn.execute(
        """INSERT OR REPLACE INTO pv_error_log
           (slot_time_utc, run_id, forecast_kwh, actual_kwh, error_kwh, built_at_utc)
           VALUES (?, 1, NULL, ?, NULL, ?)""",
        (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), actual,
         datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )


def _physical_cap() -> float:
    return float(config.PV_CAPACITY_KWP) * float(config.PV_SYSTEM_EFFICIENCY) * 0.5


def test_ceiling_clears_typical_generation(monkeypatch):
    """The rail must sit ABOVE ordinary days — the incident was it sitting below
    the median."""
    import src.db as db
    from src import weather

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            # 20 days of a realistic midday spread around ~1.45 kWh/slot.
            for d in range(1, 21):
                _seed_actual(conn, hour=12, days_ago=d, actual=1.30 + 0.02 * d)
            conn.commit()
        finally:
            conn.close()
        ceil = weather._build_pv_hourly_ceiling()

    median = 1.30 + 0.02 * 10
    assert ceil[12] > median, "ceiling must never bind on a median day"
    assert ceil[12] >= 1.70, "p99 x margin should clear the observed spread"
    assert ceil[12] <= _physical_cap(), "never above the physical bound"


def test_ceiling_falls_back_to_physical_bound_when_sparse(monkeypatch):
    """Too little evidence → the loose physical bound, never a tight guess."""
    import src.db as db
    from src import weather

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            for d in range(1, 3):  # below PV_CEILING_MIN_SAMPLES
                _seed_actual(conn, hour=12, days_ago=d, actual=0.4)
            conn.commit()
        finally:
            conn.close()
        ceil = weather._build_pv_hourly_ceiling()

    assert ceil[12] == _physical_cap()


def test_ceiling_empty_history_is_physical_bound(monkeypatch):
    import src.db as db
    from src import weather

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        ceil = weather._build_pv_hourly_ceiling()

    assert set(ceil) == set(range(24))
    assert all(v == _physical_cap() for v in ceil.values())


def test_ceiling_keeps_dawn_dusk_shape(monkeypatch):
    """A low-sun hour keeps a tight-but-honest cap, so the rail still catches an
    absurd 1.9 kWh/slot forecast at 07:00."""
    import src.db as db
    from src import weather

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            for d in range(1, 21):
                _seed_actual(conn, hour=7, days_ago=d, actual=0.30)
                _seed_actual(conn, hour=13, days_ago=d, actual=1.50)
            conn.commit()
        finally:
            conn.close()
        ceil = weather._build_pv_hourly_ceiling()

    assert ceil[7] < ceil[13], "the rail keeps the diurnal shape"
    assert ceil[7] < _physical_cap()
    assert ceil[7] > 0.30, "but still clears the observed maximum"
