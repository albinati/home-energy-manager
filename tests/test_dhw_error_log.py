"""dhw_error_log (PR C, 2026-07-02 LP audit): committed LP DHW forecast vs
realised Daikin DHW energy per LOCAL 2-hour bucket.

Uses a real temp DB (init_db) — the stitcher + rebuild are exercised
end-to-end against lp_inputs_snapshot / lp_solution_snapshot /
daikin_consumption_2hourly fixtures.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from src import db
from src.config import config


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(db, "_db_path", lambda: path)
    db.init_db()
    return path


def _seed(path, day: date):
    """Two solves covering the local day: an early one and a later revision for
    the afternoon. Actuals in daikin_consumption_2hourly for three buckets."""
    conn = sqlite3.connect(path)
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    runs = [
        (1, (day_start - timedelta(hours=8)).isoformat()),   # committed day-ahead
        (2, (day_start + timedelta(hours=6)).isoformat()),   # intra-day revision
    ]
    for run_id, run_at in runs:
        conn.execute(
            "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc, plan_date, horizon_hours, lp_status)"
            " VALUES (?, ?, ?, 48, 'Optimal')",
            (run_id, run_at, day.isoformat()),
        )
    # run 1: 0.4 kWh at 07:00 + 0.4 at 13:00; run 2 revises 13:00 to 0.8
    slots = [
        (1, 14, day_start + timedelta(hours=7), 0.4),
        (1, 26, day_start + timedelta(hours=13), 0.4),
        (2, 2, day_start + timedelta(hours=13), 0.8),
    ]
    for run_id, idx, st, kwh in slots:
        conn.execute(
            "INSERT INTO lp_solution_snapshot (run_id, slot_index, slot_time_utc, price_p, dhw_kwh)"
            " VALUES (?, ?, ?, 10.0, ?)",
            (run_id, idx, st.isoformat(), kwh),
        )
    for bucket, kwh in ((3, 0.5), (6, 0.7), (9, 0.1)):
        conn.execute(
            "INSERT INTO daikin_consumption_2hourly (date, bucket_idx, kwh_dhw, source, fetched_at)"
            " VALUES (?, ?, ?, 'test', '2026-01-01T00:00:00Z')",
            (day.isoformat(), bucket, kwh),
        )
    conn.commit()
    conn.close()


def test_stitcher_buckets_and_prefers_latest_eligible(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    day = date(2026, 1, 20)  # winter → UTC == local, buckets align
    _seed(tmp_db, day)
    fc = db.committed_dhw_forecast_by_bucket(day)
    # 07:00 → bucket 3 from run 1; 13:00 → bucket 6, run 2 (later, still
    # eligible: run_at 06:00 <= slot 13:00) wins over run 1
    assert fc[3] == pytest.approx(0.4)
    assert fc[6] == pytest.approx(0.8)


def test_rebuild_joins_forecast_and_actual(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    day = date(2026, 1, 20)
    _seed(tmp_db, day)
    n = db.rebuild_dhw_error_log_for_date(day)
    assert n == 3  # union of buckets {3, 6} (forecast) and {3, 6, 9} (actual)
    conn = sqlite3.connect(tmp_db)
    rows = {
        b: (f, a, e)
        for b, f, a, e in conn.execute(
            "SELECT bucket_idx, forecast_kwh, actual_kwh, error_kwh FROM dhw_error_log WHERE day=?",
            (day.isoformat(),),
        )
    }
    assert rows[3][0] == pytest.approx(0.4) and rows[3][1] == pytest.approx(0.5)
    assert rows[3][2] == pytest.approx(0.1)          # actual − forecast
    assert rows[6][2] == pytest.approx(0.7 - 0.8)
    assert rows[9][0] is None and rows[9][1] == pytest.approx(0.1)

    # idempotent upsert
    assert db.rebuild_dhw_error_log_for_date(day) == 3
    n_rows = conn.execute("SELECT COUNT(*) FROM dhw_error_log").fetchone()[0]
    assert n_rows == 3


def test_rebuild_empty_day_is_noop(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    assert db.rebuild_dhw_error_log_for_date(date(2026, 2, 2)) == 0
