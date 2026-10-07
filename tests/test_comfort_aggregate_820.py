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


def test_w3_comfort_knobs_are_runtime_settings(monkeypatch):
    from src import runtime_settings
    for k in ("LP_W3_NIGHT_FLOOR_C", "LP_W3_PEAK_COAST_DELTA_C", "INDOOR_COMFORT_AGGREGATE"):
        monkeypatch.delenv(k, raising=False)
        config._overrides.pop(k, None)
    runtime_settings.clear_cache()
    for key, lo, hi in (("LP_W3_NIGHT_FLOOR_C", 14.0, 22.0), ("LP_W3_PEAK_COAST_DELTA_C", 0.0, 4.0)):
        spec = _SPECS[key]
        assert spec.type_name == "float" and spec.min_value == lo and spec.max_value == hi
    assert _SPECS["INDOOR_COMFORT_AGGREGATE"].type_name == "str"
    # Defaults unchanged vs the old .env-only values.
    assert config.LP_W3_NIGHT_FLOOR_C == pytest.approx(17.5)
    assert config.LP_W3_PEAK_COAST_DELTA_C == pytest.approx(1.0)
    assert config.INDOOR_COMFORT_AGGREGATE == "mean"


def _solve(monkeypatch, floor: float):
    from tests.test_lwt_source_lp import _solve_w3
    monkeypatch.setitem(config._overrides, "LP_W3_NIGHT_FLOOR_C", floor)
    plan, _ = _solve_w3(monkeypatch, source="tier")
    return plan


def test_runtime_override_reaches_the_w3_solve(monkeypatch):
    """A real W3 solve: the night floor in force shapes the trajectory and is
    recorded on the plan."""
    lo = _solve(monkeypatch, 16.0)
    hi = _solve(monkeypatch, 22.0)
    assert lo.w3_night_floor_c == pytest.approx(16.0)
    assert hi.w3_night_floor_c == pytest.approx(22.0)
    assert (min(lo.indoor_temp_c) != pytest.approx(min(hi.indoor_temp_c))
            or sum(lo.comfort_slack_c) != pytest.approx(sum(hi.comfort_slack_c))
            or sum(lo.space_electric_kwh) != pytest.approx(sum(hi.space_electric_kwh)))


def test_plausibility_gate_uses_the_plans_recorded_floor(monkeypatch):
    from src.scheduler.lp_dispatch import w3_trajectory_plausible
    plan = _solve(monkeypatch, 17.5)
    assert plan.w3_night_floor_c == pytest.approx(17.5)
    assert w3_trajectory_plausible(plan)[0]
    # live config raised afterwards must not retroactively condemn the plan
    monkeypatch.setitem(config._overrides, "LP_W3_NIGHT_FLOOR_C", 20.0)
    monkeypatch.setattr(config, "LP_W3_IMPLAUSIBLE_BELOW_FLOOR_C", 0.1, raising=False)
    plan.w3_night_floor_c = 17.5
    plan.indoor_temp_c = [18.0] * len(plan.indoor_temp_c)
    plan.comfort_slack_c = [0.0] * len(plan.comfort_slack_c)
    assert w3_trajectory_plausible(plan)[0]  # 18 >= 17.5-0.1 though live floor is 20
    plan.w3_night_floor_c = None  # older plan → live config (20) applies
    assert not w3_trajectory_plausible(plan)[0]


@pytest.mark.parametrize(("val", "code"), [
    ("rom:corredor", 400), ("room:", 400), ("median", 400),
    ("room:Corredor", 200), ("min", 200),
])
def test_put_validates_comfort_aggregate(monkeypatch, val, code):
    from fastapi.testclient import TestClient

    from src.api.main import app
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    monkeypatch.setenv("REQUIRE_SIMULATION_ID", "false")
    r = TestClient(app).put("/api/v1/settings/INDOOR_COMFORT_AGGREGATE", json={"value": val})
    assert r.status_code == code, r.text
    from src import runtime_settings
    config._overrides.pop("INDOOR_COMFORT_AGGREGATE", None)
    runtime_settings.clear_cache()
