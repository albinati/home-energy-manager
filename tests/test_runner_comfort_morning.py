"""Morning comfort_check execution_log sampling (#25)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src.scheduler import runner


@pytest.fixture(autouse=True)
def clear_comfort_keys():
    runner._comfort_morning_logged.clear()
    yield
    runner._comfort_morning_logged.clear()


def test_comfort_morning_start_logs_once(monkeypatch):
    logged: list[dict] = []

    def _capture(row: dict) -> None:
        logged.append(row)

    monkeypatch.setattr(runner.db, "log_execution", _capture)
    monkeypatch.setattr(runner, "_get_forecast_temp_c", lambda _utc: None)
    monkeypatch.setattr(app_config, "LP_OCCUPIED_MORNING_START", "06:30")
    monkeypatch.setattr(app_config, "LP_OCCUPIED_MORNING_END", "08:30")

    class Dev:
        leaving_water_temperature = 40.0

    dev = Dev()
    tz = ZoneInfo("Europe/London")
    local = datetime(2026, 4, 21, 6, 31, tzinfo=tz)
    utc = local.astimezone(UTC)
    runner._maybe_log_comfort_morning_check(
        now_local=local,
        now_utc=utc,
        plan_date="2026-04-21",
        room_t=20.0,
        soc=50.0,
        fox_mode="Self Use",
        outdoor_t=10.0,
        lwt_off=0.0,
        tank_t=42.0,
        tank_tgt=48.0,
        tank_on=True,
        dev0=dev,
    )
    assert len(logged) == 1
    assert logged[0]["source"] == "comfort_check"
    assert logged[0]["slot_kind"] == "occupied_morning_start"
    assert logged[0]["daikin_room_temp"] == 20.0

    runner._maybe_log_comfort_morning_check(
        now_local=local,
        now_utc=utc,
        plan_date="2026-04-21",
        room_t=20.0,
        soc=50.0,
        fox_mode="Self Use",
        outdoor_t=10.0,
        lwt_off=0.0,
        tank_t=42.0,
        tank_tgt=48.0,
        tank_on=True,
        dev0=dev,
    )
    assert len(logged) == 1


def test_comfort_morning_outside_window_no_log(monkeypatch):
    logged: list[dict] = []

    monkeypatch.setattr(runner.db, "log_execution", lambda row: logged.append(row))
    monkeypatch.setattr(app_config, "LP_OCCUPIED_MORNING_START", "06:30")

    class Dev:
        leaving_water_temperature = 40.0

    local = datetime(2026, 4, 21, 10, 0, tzinfo=ZoneInfo("Europe/London"))
    runner._maybe_log_comfort_morning_check(
        now_local=local,
        now_utc=local.astimezone(UTC),
        plan_date="2026-04-21",
        room_t=20.0,
        soc=None,
        fox_mode=None,
        outdoor_t=None,
        lwt_off=None,
        tank_t=None,
        tank_tgt=None,
        tank_on=True,
        dev0=Dev(),
    )
    assert logged == []
