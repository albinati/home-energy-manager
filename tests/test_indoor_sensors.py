"""#540 W1 — indoor temperature ingestion (table + save/get + endpoint).

Idempotent multi-room save; freshest-within-staleness getter (mean across rooms);
range query; POST writes + GET surfaces latest_fresh/newest_at.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_save_is_idempotent_and_multiroom() -> None:
    now = datetime.now(UTC)
    rows = [
        {"captured_at": _z(now), "temp_c": 20.5, "room": "living"},
        {"captured_at": _z(now), "temp_c": 19.0, "room": "bedroom"},
    ]
    assert db.save_indoor_readings(rows) == 2
    # Re-push same (captured_at, room) → no new rows.
    assert db.save_indoor_readings(rows) == 0
    # A different room at the same instant is a new row.
    assert db.save_indoor_readings([{"captured_at": _z(now), "temp_c": 18.0, "room": "office"}]) == 1


def test_latest_is_mean_across_rooms_within_staleness() -> None:
    now = datetime.now(UTC)
    db.save_indoor_readings([
        {"captured_at": _z(now), "temp_c": 20.0, "room": "living"},
        {"captured_at": _z(now), "temp_c": 22.0, "room": "bedroom"},
    ])
    latest = db.get_latest_indoor_reading(max_age_minutes=30)
    assert latest is not None
    assert latest["temp_c"] == pytest.approx(21.0)  # mean(20, 22)
    assert latest["n_rooms"] == 2
    assert set(latest["rooms"]) == {"living", "bedroom"}


def test_stale_reading_treated_as_absent() -> None:
    old = datetime.now(UTC) - timedelta(hours=2)
    db.save_indoor_readings([{"captured_at": _z(old), "temp_c": 20.0, "room": "living"}])
    assert db.get_latest_indoor_reading(max_age_minutes=30) is None  # beyond stale window


def test_latest_picks_freshest_per_room() -> None:
    now = datetime.now(UTC)
    db.save_indoor_readings([
        {"captured_at": _z(now - timedelta(minutes=20)), "temp_c": 18.0, "room": "living"},
        {"captured_at": _z(now), "temp_c": 21.0, "room": "living"},  # fresher → wins
    ])
    latest = db.get_latest_indoor_reading(max_age_minutes=30)
    assert latest["temp_c"] == pytest.approx(21.0)
    assert latest["n_rooms"] == 1


def test_range_query() -> None:
    now = datetime.now(UTC)
    db.save_indoor_readings([
        {"captured_at": _z(now - timedelta(hours=1)), "temp_c": 19.0},
        {"captured_at": _z(now), "temp_c": 20.0},
    ])
    rows = db.get_indoor_readings_range(_z(now - timedelta(hours=2)), _z(now + timedelta(minutes=1)))
    assert len(rows) == 2
    assert rows[0]["temp_c"] == pytest.approx(19.0)  # oldest first


def test_endpoint_post_and_get() -> None:
    import asyncio
    from src.api.routers import sensors as sr

    now = datetime.now(UTC)
    body = sr.IndoorReadingsBody(readings=[
        sr.IndoorReading(captured_at=_z(now), temp_c=20.5, room="living"),
        sr.IndoorReading(captured_at=_z(now), temp_c=19.5, room="bedroom"),
    ])
    post = asyncio.run(sr.post_indoor_readings(body))
    assert post == {"received": 2, "written": 2}

    got = asyncio.run(sr.get_indoor_readings(hours=24))
    assert got["n_readings"] == 2
    assert got["latest_fresh"]["temp_c"] == pytest.approx(20.0)  # mean
    assert got["newest_at"] is not None
    assert got["configured"] is True
    import json
    json.dumps(got)


def test_endpoint_empty_state() -> None:
    import asyncio
    from src.api.routers import sensors as sr

    got = asyncio.run(sr.get_indoor_readings(hours=24))
    assert got["n_readings"] == 0
    assert got["latest_fresh"] is None
    assert got["configured"] is False
