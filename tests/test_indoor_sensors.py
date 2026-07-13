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
    assert post["received"] == 2
    assert post["written"] == 2   # temp rows → room_temperature_history
    assert post["logged"] == 2    # raw rows → device_reading_log

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


# ── #540 W1c — full per-device logging ──────────────────────────────────────

def test_device_log_captures_all_fields_and_extras() -> None:
    now = datetime.now(UTC)
    r = {
        "captured_at": _z(now), "temp_c": 31.0, "humidity_pct": 63.3,
        "pressure_hpa": 1020.7, "room": "sala", "source": "hem-temp-sensor",
        "device_id": "hem-temp-sensor", "mac": "70:4B:CA:26:EA:B4",
        "temperature_bmp280_c": 31.2,   # an EXTRA field, not a named column
    }
    assert db.save_device_reading_log([r]) == 1
    rows = db.get_device_reading_log()
    assert len(rows) == 1
    row = rows[0]
    assert row["mac"] == "70:4B:CA:26:EA:B4"
    assert row["device_key"] == "70:4B:CA:26:EA:B4"   # MAC preferred
    assert row["humidity_pct"] == pytest.approx(63.3)
    assert row["pressure_hpa"] == pytest.approx(1020.7)
    # The extra field survives losslessly in the raw payload.
    assert row["payload"]["temperature_bmp280_c"] == pytest.approx(31.2)


def test_device_log_idempotent_on_device_and_time() -> None:
    now = datetime.now(UTC)
    r = {"captured_at": _z(now), "temp_c": 20.0, "mac": "AA:BB", "room": "sala"}
    assert db.save_device_reading_log([r]) == 1
    assert db.save_device_reading_log([r]) == 0   # retry → no dup
    # Same instant, DIFFERENT device → distinct row.
    assert db.save_device_reading_log([{**r, "mac": "CC:DD"}]) == 1


def test_device_log_null_captured_at_still_dedups() -> None:
    """A reading with an unparseable/missing timestamp is still logged, and two
    such readings from the same device in one batch collapse (dedup_key falls
    back to the server clock — NOT a NULL that SQLite treats as always-distinct)."""
    r = {"captured_at": "not-a-timestamp", "temp_c": 20.0, "mac": "AA:BB", "room": "sala"}
    # Same batch, same device, both timestamp-less → one row (deduped).
    assert db.save_device_reading_log([r, dict(r)]) == 1
    rows = db.get_device_reading_log()
    assert len(rows) == 1
    assert rows[0]["captured_at"] is None       # honest: device gave no time
    assert rows[0]["payload"]["temp_c"] == pytest.approx(20.0)


def test_device_log_rejects_non_finite_temp() -> None:
    """NaN/Inf must never reach the log — they serialize as the invalid-JSON
    token `NaN` and would poison every reader of the feed."""
    now = datetime.now(UTC)
    assert db.save_device_reading_log([
        {"captured_at": _z(now), "temp_c": float("nan"), "humidity_pct": 55.0,
         "mac": "AA:BB", "room": "sala"},
    ]) == 1
    rows = db.get_device_reading_log()
    assert rows[0]["temp_c"] is None                     # coerced out of the column
    assert rows[0]["payload"]["temp_c"] is None          # and out of the raw blob
    assert rows[0]["payload"]["humidity_pct"] == pytest.approx(55.0)  # sibling intact
    import json
    json.dumps(rows)   # the whole response is valid JSON (would raise on NaN)


def test_device_log_batch_latest_is_freshest_capture() -> None:
    """When a device pushes several readings in ONE batch (same received_at),
    the 'latest' overview must be the freshest captured_at, not an arbitrary tie."""
    now = datetime.now(UTC)
    db.save_device_reading_log([
        {"captured_at": _z(now - timedelta(minutes=5)), "temp_c": 18.0, "mac": "AA:BB", "room": "sala"},
        {"captured_at": _z(now), "temp_c": 21.0, "mac": "AA:BB", "room": "sala"},  # freshest
    ])
    devs = db.list_sensor_devices()
    assert devs[0]["latest"]["temp_c"] == pytest.approx(21.0)


def test_device_log_oversized_payload_truncated() -> None:
    now = datetime.now(UTC)
    big = "x" * 9000   # blows past the 4 KB per-reading ceiling
    db.save_device_reading_log([
        {"captured_at": _z(now), "temp_c": 20.0, "mac": "AA:BB", "room": "sala", "junk": big},
    ])
    row = db.get_device_reading_log()[0]
    assert row["payload"].get("_truncated") is True
    assert "junk" not in row["payload"]                 # extra dropped
    assert row["payload"]["temp_c"] == pytest.approx(20.0)  # known field kept
    assert row["temp_c"] == pytest.approx(20.0)          # typed column unaffected


def test_out_of_band_temp_logged_but_not_in_thermal_history() -> None:
    """An 85 °C sensor fault must be logged per-device but must NOT poison the
    LP's room_temperature_history."""
    import asyncio
    from src.api.routers import sensors as sr

    now = datetime.now(UTC)
    body = sr.IndoorReadingsBody(readings=[
        sr.IndoorReading(captured_at=_z(now), temp_c=85.0, room="sala", mac="AA:BB"),
    ])
    post = asyncio.run(sr.post_indoor_readings(body))
    assert post["logged"] == 1     # in the raw device log
    assert post["written"] == 0    # NOT in thermal history (out of band)
    assert db.get_latest_indoor_reading(max_age_minutes=30) is None


def test_devices_overview_and_device_log_endpoints() -> None:
    import asyncio
    from src.api.routers import sensors as sr

    now = datetime.now(UTC)
    body = sr.IndoorReadingsBody(readings=[
        sr.IndoorReading(captured_at=_z(now), temp_c=21.0, humidity_pct=55.0,
                         room="sala", mac="AA:BB", device_id="node-sala"),
    ])
    asyncio.run(sr.post_indoor_readings(body))

    devs = asyncio.run(sr.get_sensor_devices())
    assert devs["n_devices"] == 1
    d = devs["devices"][0]
    assert d["device_key"] == "AA:BB"
    assert d["n_readings"] == 1
    assert d["latest"]["humidity_pct"] == pytest.approx(55.0)

    log = asyncio.run(sr.get_sensor_device_log(device="AA:BB", hours=24))
    assert log["n_rows"] == 1
    assert log["rows"][0]["payload"]["device_id"] == "node-sala"
    import json
    json.dumps(devs); json.dumps(log)   # JSON-serialisable for the API


def test_renamed_device_shows_its_CURRENT_room_not_the_alphabetical_max() -> None:
    """A renamed sensor must display the room it reports NOW.

    Regression: the overview took ``MAX(room)`` per device, i.e. the
    alphabetically-largest label the device EVER sent. A sensor renamed
    ``sala`` → ``corredor`` kept showing "sala" forever ('s' > 'c'), and one
    renamed ``cozinha`` → ``quarto_roof`` → ``cozinha`` showed "quarto_roof".
    Same trap for ``device_id`` ('hem-temp-sensor' > 'hem-sensor-01').
    """
    now = datetime.now(UTC)
    mac = "70:4B:CA:26:EA:B4"
    # Oldest label sorts ABOVE the newest one on both room and device_id, so a
    # MAX() aggregate would pick the retired names.
    db.save_device_reading_log([
        {"captured_at": _z(now - timedelta(hours=2)), "temp_c": 20.0, "mac": mac,
         "room": "sala", "device_id": "hem-temp-sensor"},
        {"captured_at": _z(now - timedelta(hours=1)), "temp_c": 21.0, "mac": mac,
         "room": "corredor_quartos", "device_id": "hem-sensor-01"},
        {"captured_at": _z(now), "temp_c": 22.0, "mac": mac,
         "room": "corredor", "device_id": "hem-sensor-01"},
    ])

    devs = db.list_sensor_devices()
    assert len(devs) == 1                      # one physical device, not three
    d = devs[0]
    assert d["device_key"] == mac
    assert d["n_readings"] == 3                # the count stays cumulative
    assert d["room"] == "corredor"             # NOT "sala"
    assert d["device_id"] == "hem-sensor-01"   # NOT "hem-temp-sensor"
    assert d["latest"]["temp_c"] == pytest.approx(22.0)
