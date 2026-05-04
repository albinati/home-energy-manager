"""Retention policy for append-only history tables.

ADR-004 flagged daikin_telemetry as unbounded-growth; the Phase 0 snapshot
tables share the same concern. prune_history_tables() is idempotent, runs
at startup (API lifespan hook) and daily at 03:15 UTC, and tolerates
individual table failures so one broken policy doesn't block the rest.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from src import db


@pytest.fixture(autouse=True)
def _db_ready():
    db.init_db()


def _seed_daikin_telemetry(row_count_pairs: list[tuple[float, str]]) -> None:
    """row_count_pairs: list of (fetched_at_epoch, source)."""
    conn = db.get_connection()
    try:
        for fetched_at, source in row_count_pairs:
            conn.execute(
                """INSERT INTO daikin_telemetry
                   (fetched_at, source, tank_temp_c, indoor_temp_c, outdoor_temp_c)
                   VALUES (?, ?, ?, ?, ?)""",
                (fetched_at, source, 46.0, 20.5, 12.0),
            )
        conn.commit()
    finally:
        conn.close()


def _count(table: str) -> int:
    conn = db.get_connection()
    try:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# prune_old_rows: core behavior
# ---------------------------------------------------------------------------

def test_prune_old_rows_deletes_only_rows_older_than_cutoff():
    now_epoch = time.time()
    _seed_daikin_telemetry([
        (now_epoch - 40 * 86400, "live"),   # older than 30 d → delete
        (now_epoch - 15 * 86400, "live"),   # keep
        (now_epoch - 1 * 86400, "live"),    # keep
    ])
    assert _count("daikin_telemetry") == 3
    deleted = db.prune_old_rows(
        "daikin_telemetry", "fetched_at", max_age_days=30, epoch_seconds=True,
    )
    assert deleted == 1
    assert _count("daikin_telemetry") == 2


def test_prune_old_rows_zero_days_is_noop():
    now_epoch = time.time()
    _seed_daikin_telemetry([(now_epoch - 1000 * 86400, "live")])
    # max_age_days=0 must delete nothing (safety — can't accidentally wipe).
    deleted = db.prune_old_rows(
        "daikin_telemetry", "fetched_at", max_age_days=0, epoch_seconds=True,
    )
    assert deleted == 0
    assert _count("daikin_telemetry") == 1


def test_prune_old_rows_iso_timestamps():
    # meteo_forecast_history stores ISO strings; cutoff comparison is lexical.
    conn = db.get_connection()
    try:
        old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        fresh = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO meteo_forecast_history (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2) VALUES (?, ?, ?, ?)",
            (old, "2026-03-15T00:00:00+00:00", 10.0, 200.0),
        )
        conn.execute(
            "INSERT INTO meteo_forecast_history (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2) VALUES (?, ?, ?, ?)",
            (fresh, "2026-04-23T00:00:00+00:00", 12.0, 250.0),
        )
        conn.commit()
    finally:
        conn.close()

    deleted = db.prune_old_rows(
        "meteo_forecast_history", "forecast_fetch_at_utc", max_age_days=30,
    )
    assert deleted == 1
    assert _count("meteo_forecast_history") == 1


# ---------------------------------------------------------------------------
# prune_history_tables: sweep
# ---------------------------------------------------------------------------

def test_prune_history_tables_returns_per_table_counts():
    # Seed a couple of old rows across tables to verify the aggregator hits each.
    now_epoch = time.time()
    _seed_daikin_telemetry([(now_epoch - 100 * 86400, "live")])
    db.log_config_change("X", "old", op="set", actor="test")
    # Stamp the config_audit row to 2 years ago so CONFIG_AUDIT_RETENTION_DAYS=365 catches it.
    conn = db.get_connection()
    try:
        old_ts = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        conn.execute(
            "UPDATE config_audit SET changed_at_utc = ? WHERE key = 'X' AND value = 'old'",
            (old_ts,),
        )
        conn.commit()
    finally:
        conn.close()

    results = db.prune_history_tables()
    assert set(results.keys()) == {
        "daikin_telemetry",
        "meteo_forecast_history",
        "forecast_skill_log",
        "lp_solution_snapshot",
        "lp_inputs_snapshot",
        "config_audit",
        # Dispatch + scenario tables ride on LP_SNAPSHOT_RETENTION_DAYS — the
        # rows would have nothing useful to point at after the snapshot is
        # pruned, so they're swept on the same horizon.
        "dispatch_decisions",
        "scenario_solve_log",
        # Per-day-keyed warning acks (issue #200) — without TTL the table
        # grows linearly across days of stuck warnings.
        "acknowledged_warnings",
    }
    assert results["daikin_telemetry"] >= 1
    assert results["config_audit"] >= 1


def test_prune_history_tables_sweeps_acknowledged_warnings():
    """Issue #200 — per-day-keyed acks accumulate; the prune sweep must
    drop rows older than ACKNOWLEDGED_WARNINGS_RETENTION_DAYS (default 30 d)."""
    conn = db.get_connection()
    try:
        old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
        recent_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        conn.execute(
            "INSERT INTO acknowledged_warnings (warning_key, acknowledged_at) VALUES (?, ?)",
            ("fox_scheduler_disabled_2026-03-15", old_ts),
        )
        conn.execute(
            "INSERT INTO acknowledged_warnings (warning_key, acknowledged_at) VALUES (?, ?)",
            ("fox_scheduler_disabled_2026-04-24", recent_ts),
        )
        conn.commit()
    finally:
        conn.close()

    results = db.prune_history_tables()
    assert results["acknowledged_warnings"] == 1
    assert _count("acknowledged_warnings") == 1


def test_prune_history_tables_tolerates_one_bad_policy(monkeypatch):
    # Force one specific table to raise and verify others still run.
    orig = db.prune_old_rows
    def flaky(table, *args, **kwargs):
        if table == "daikin_telemetry":
            raise RuntimeError("simulated failure")
        return orig(table, *args, **kwargs)
    monkeypatch.setattr(db, "prune_old_rows", flaky)

    results = db.prune_history_tables()
    assert results["daikin_telemetry"] == -1  # failure sentinel
    assert results["meteo_forecast_history"] >= 0  # normal completion
