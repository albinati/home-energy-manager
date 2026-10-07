"""#820 — comfort policy: which room is THE house (INDOOR_COMFORT_AGGREGATE),
and the W3 night floor / peak coast delta as runtime settings."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.config import config
from src.runtime_settings import SCHEMA as _SPECS


@pytest.fixture(autouse=True)
def _db():
    db.init_db()


def _seed(rooms: dict[str, float], *, age_min: float = 1.0) -> None:
    at = (datetime.now(UTC) - timedelta(minutes=age_min)).isoformat()
    db.save_indoor_readings([{"captured_at": at, "room": r, "temp_c": t} for r, t in rooms.items()])


def test_default_aggregate_is_the_mean_and_exposes_the_rooms(monkeypatch):
    monkeypatch.setattr(config, "INDOOR_COMFORT_AGGREGATE", "mean")
    _seed({"corredor": 21.0, "cozinha": 18.0})
    s = db.get_latest_indoor_reading(max_age_minutes=30)
    assert s["temp_c"] == pytest.approx(19.5)
    assert s["aggregate"] == "mean"
    assert s["rooms_c"] == {"corredor": 21.0, "cozinha": 18.0}
    assert s["spread_c"] == pytest.approx(3.0) and s["mean_c"] == pytest.approx(19.5)


@pytest.mark.parametrize(("mode", "expected", "applied"), [
    ("min", 18.0, "min"),
    ("max", 21.0, "max"),
    ("room:corredor", 21.0, "room:corredor"),
    ("ROOM:Cozinha", 18.0, "room:cozinha"),
    ("room:sotao", 19.5, "mean"),     # named room absent → honest mean
    ("bogus", 19.5, "mean"),          # unknown mode → mean
])
def test_aggregate_modes(monkeypatch, mode, expected, applied):
    monkeypatch.setattr(config, "INDOOR_COMFORT_AGGREGATE", mode)
    _seed({"corredor": 21.0, "cozinha": 18.0})
    s = db.get_latest_indoor_reading(max_age_minutes=30)
    assert s["temp_c"] == pytest.approx(expected)
    assert s["aggregate"] == applied


def test_named_room_that_went_stale_falls_back_to_the_fresh_mean(monkeypatch):
    monkeypatch.setattr(config, "INDOOR_COMFORT_AGGREGATE", "room:cozinha")
    _seed({"corredor": 21.0})
    _seed({"cozinha": 18.0}, age_min=90)  # beyond the 30-min window
    s = db.get_latest_indoor_reading(max_age_minutes=30)
    assert s["rooms"] == ["corredor"] and s["temp_c"] == pytest.approx(21.0)
    assert s["aggregate"] == "mean"


def test_indoor_summary_carries_the_comfort_temperature(monkeypatch):
    monkeypatch.setattr(config, "INDOOR_COMFORT_AGGREGATE", "min")
    at = datetime.now(UTC).isoformat()
    rows = [
        {"captured_at": at, "room": "corredor", "temp_c": 21.0, "device_id": "a"},
        {"captured_at": at, "room": "cozinha", "temp_c": 18.0, "device_id": "b"},
    ]
    db.save_indoor_readings(rows)
    db.save_device_reading_log(rows)  # the cockpit summary reads the per-device log
    summ = db.get_indoor_summary(stale_minutes=30)
    assert summ["mean_c"] == pytest.approx(19.5)
    assert summ["comfort_c"] == pytest.approx(18.0) and summ["comfort_aggregate"] == "min"


def test_w3_comfort_knobs_are_runtime_settings():
    for key, lo, hi in (("LP_W3_NIGHT_FLOOR_C", 14.0, 22.0), ("LP_W3_PEAK_COAST_DELTA_C", 0.0, 4.0)):
        spec = _SPECS[key]
        assert spec.type_name == "float" and spec.min_value == lo and spec.max_value == hi
    assert _SPECS["INDOOR_COMFORT_AGGREGATE"].type_name == "str"
    # Defaults unchanged vs the old .env-only values.
    assert config.LP_W3_NIGHT_FLOOR_C == pytest.approx(17.5)
    assert config.LP_W3_PEAK_COAST_DELTA_C == pytest.approx(1.0)
    assert config.INDOOR_COMFORT_AGGREGATE == "mean"


def test_runtime_override_reaches_the_w3_floor(monkeypatch):
    """The LP's W3 floor reads the runtime value, not a frozen env default."""
    from src.scheduler import lp_optimizer

    monkeypatch.setitem(config._overrides, "LP_W3_NIGHT_FLOOR_C", 16.0)
    monkeypatch.setitem(config._overrides, "LP_W3_PEAK_COAST_DELTA_C", 2.0)
    assert float(getattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5)) == 16.0
    assert float(getattr(config, "LP_W3_PEAK_COAST_DELTA_C", 1.0)) == 2.0
    assert hasattr(lp_optimizer, "solve_lp")
