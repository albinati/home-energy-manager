"""Sensor-data lifecycle (#540): tiered hot/warm/cold storage.

Pins the contract: raw is gzip-archived (partitioned by captured month) BEFORE
it's pruned, a permanent 15-min rollup is kept, and a wide ML CSV is built —
and the whole thing is a no-op when disabled.
"""
from __future__ import annotations

import csv
import gzip
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from src.config import config as app_config


@pytest.fixture()
def fresh_db(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(app_config, "DB_PATH", os.path.join(d, "e.db"), raising=False)
    monkeypatch.setattr(app_config, "DATA_ARCHIVE_DIR", os.path.join(d, "archive"), raising=False)
    monkeypatch.setattr(app_config, "DATA_ARCHIVE_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "INDOOR_SENSOR_RAW_RETENTION_DAYS", 90, raising=False)
    monkeypatch.setattr(app_config, "DEVICE_LOG_RETENTION_DAYS", 30, raising=False)
    from src import db
    db.init_db()
    return db, d


def _seed_indoor(db, now, *, recent=40, old=20):
    rows = [{"captured_at": (now - timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "room": "sala", "temp_c": 20.0 + (i % 5) * 0.1} for i in range(recent)]
    rows += [{"captured_at": (now - timedelta(days=100, minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "room": "sala", "temp_c": 15.0} for i in range(old)]
    db.save_indoor_readings(rows)


def test_archive_before_prune_and_rollup(fresh_db):
    db, d = fresh_db
    now = datetime.now(UTC)
    _seed_indoor(db, now)
    from src.analytics.data_archival import archive_root, run_sensor_data_lifecycle
    res = run_sensor_data_lifecycle()

    # old rows archived AND pruned; recent rows kept hot
    assert res["room_temperature_history"] == {"archived": 20, "pruned": 20}
    c = sqlite3.connect(app_config.DB_PATH)
    assert c.execute("SELECT COUNT(*) FROM room_temperature_history").fetchone()[0] == 40

    # the archive exists, partitioned by the rows' CAPTURED month (~100d ago),
    # and holds exactly the 20 pruned rows — nothing lost.
    gzs = list((archive_root() / "room_temperature_history").glob("*.jsonl.gz"))
    assert len(gzs) >= 1
    total = 0
    for p in gzs:
        with gzip.open(p, "rt") as fh:
            total += sum(1 for _ in fh)
    assert total == 20

    # warm rollup populated
    assert res["rollup_15min"] >= 1
    rr = db.get_indoor_rollup_15min(
        (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    assert len(rr) >= 1 and all("mean_c" in r for r in rr)


def test_wide_table_aligns_features(fresh_db):
    db, d = fresh_db
    now = datetime.now(UTC)
    _seed_indoor(db, now, old=0)
    for i in range(6):
        db.log_execution({"timestamp": (now - timedelta(minutes=30 * i)).isoformat(),
                          "daikin_outdoor_temp": 6.0 + i, "daikin_lwt": 35.0,
                          "daikin_tank_temp": 45.0, "soc_percent": 50 + i,
                          "status": "ok", "action_taken": "none"})
    from src.analytics.data_archival import archive_root, build_ml_wide_archive
    build_ml_wide_archive()
    p = sorted((archive_root() / "ml_wide").glob("*.csv.gz"))[-1]
    with gzip.open(p, "rt") as fh:
        rows = list(csv.DictReader(fh))
    # at least one slot carries BOTH a bucketed indoor mean and a joined outdoor
    joined = [r for r in rows if r["indoor_c"] and r["outdoor_c"]]
    assert joined, "no slot aligned indoor + execution_log features"
    r = joined[-1]
    assert 15.0 <= float(r["indoor_c"]) <= 25.0
    assert float(r["outdoor_c"]) >= 6.0
    assert r["ts"].endswith("Z")


def test_disabled_is_noop(fresh_db, monkeypatch):
    db, d = fresh_db
    monkeypatch.setattr(app_config, "DATA_ARCHIVE_ENABLED", False, raising=False)
    _seed_indoor(db, datetime.now(UTC))
    from src.analytics.data_archival import run_sensor_data_lifecycle
    assert run_sensor_data_lifecycle() == {"enabled": False}
    # nothing pruned when disabled
    c = sqlite3.connect(app_config.DB_PATH)
    assert c.execute("SELECT COUNT(*) FROM room_temperature_history").fetchone()[0] == 60
