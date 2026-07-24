"""PV forecast ceiling — a SAFETY RAIL whose only correct failure mode is
being too loose (#762).

Regression cover for the 2026-07-21..23 incident: the previous estimator
spread Fox DAILY totals across hours with a fixed sinusoid, which is far
flatter than a real PV curve. In prod it produced 1.32-1.43 kWh/slot for
11-13 UTC against a MEDIAN realised 1.35-1.45 — so the rail bound on ordinary
midday slots, truncating the committed forecast and censoring the error signal
the recent-bias corrector trains on.

These tests seed ``fox_energy_daily`` with the incident's own daily totals so
they exercise the OLD code path too: each one fails on the pre-#762
implementation for the right reason.
"""
from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from src.config import config

# Realised midday half-hour generation, from 90 days of prod pv_error_log.
PROD_MIDDAY_MEDIAN_KWH = 1.45
PROD_MIDDAY_MAX_KWH = 1.93  # docs/PV_TRUST_GUARDRAIL.md, 2026-05-06 13:00 UTC
PROD_MAX_REPORTED_KWH = 2.42  # largest slot the meter has ever reported


def _seed_fox_daily(conn, *, days: int = 30, solar_kwh: float = 23.0):
    """Daily totals in the range that produced the too-tight ceiling in prod."""
    for d in range(1, days + 1):
        day = (date.today() - timedelta(days=d)).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO fox_energy_daily
               (date, solar_kwh, import_kwh, export_kwh, load_kwh, fetched_at)
               VALUES (?, ?, 0, 0, 0, ?)""",
            (day, solar_kwh, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )


def test_ceiling_clears_the_median_day(monkeypatch):
    """The rail must sit above an ORDINARY midday slot.

    Fails on the pre-#762 code, which returned ~1.404 kWh/slot at 12 UTC for
    these daily totals — below the 1.45 median it was supposed to bound.
    """
    import src.db as db
    from src import weather

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_fox_daily(conn)
            conn.commit()
        finally:
            conn.close()
        ceil = weather._build_pv_hourly_ceiling()

    assert ceil[12] > PROD_MIDDAY_MEDIAN_KWH, "rail must never bind on a median day"
    assert ceil[13] > PROD_MIDDAY_MEDIAN_KWH


def test_ceiling_clears_the_best_slot_ever_measured(monkeypatch):
    """1.93 kWh was really generated; a rail below that clips reality.

    Fails on the pre-#762 code (~1.43) and on the derated
    capacity x efficiency x 0.5 bound (1.9125) that preceded it in review.
    """
    import src.db as db
    from src import weather

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_fox_daily(conn)
            conn.commit()
        finally:
            conn.close()
        ceil = weather._build_pv_hourly_ceiling()

    assert ceil[13] > PROD_MIDDAY_MAX_KWH


def test_ceiling_is_history_independent(monkeypatch):
    """The rail must not be derived from realised output.

    pv_error_log is pruned at METEO_FORECAST_HISTORY_RETENTION_DAYS (30), so a
    trailing-window estimator silently becomes "max of the last 30 days" — it
    lags the spring clear-sky ramp and collapses after a run of overcast days,
    re-creating the incident seasonally. A run of dark days must not move it.

    Fails on the pre-#762 code, whose ceiling scaled with Fox daily totals.
    """
    import src.db as db
    from src import weather

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_fox_daily(conn, solar_kwh=23.0)
            conn.commit()
        finally:
            conn.close()
        bright = weather._build_pv_hourly_ceiling()

        conn = db.get_connection()
        try:
            _seed_fox_daily(conn, solar_kwh=2.0)  # a month of overcast
            conn.commit()
        finally:
            conn.close()
        dark = weather._build_pv_hourly_ceiling()

    assert bright == dark, "a run of dark days must not lower the physical rail"


def test_ceiling_is_margined_nameplate_not_derated(monkeypatch):
    """Deliberately NOT derated: PV_SYSTEM_EFFICIENCY is an expected-yield
    derate, not a limit. The margin then clears even the largest slot the meter
    has ever reported."""
    from src import weather

    monkeypatch.setattr(config, "PV_CAPACITY_KWP", 4.5, raising=False)
    monkeypatch.setattr(config, "PV_SYSTEM_EFFICIENCY", 0.85, raising=False)
    monkeypatch.setattr(config, "PV_CEILING_MARGIN", 1.15, raising=False)

    ceil_kwh = weather.pv_slot_ceiling_kwh()
    derated = 4.5 * 0.85 * 0.5
    assert ceil_kwh > derated, "the derate is not a physical limit"
    assert ceil_kwh > PROD_MAX_REPORTED_KWH, "must clear every observed slot"

    ceil = weather._build_pv_hourly_ceiling()
    assert set(ceil) == set(range(24))
    assert all(v == ceil_kwh for v in ceil.values())


def test_ceiling_survives_empty_db(monkeypatch):
    import src.db as db
    from src import weather

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        ceil = weather._build_pv_hourly_ceiling()

    assert all(v > PROD_MAX_REPORTED_KWH for v in ceil.values())
