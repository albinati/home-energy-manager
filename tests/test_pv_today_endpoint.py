"""GET /api/v1/pv/today — planned-vs-realised PV roll-up + accuracy."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.api.routers import pv as pv_router


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "DB_PATH", str(db_path))
    db.init_db()
    # No network / forecast provider in tests — degrade planned line to zero
    # so the test isolates the realised roll-up + accuracy maths.
    import src.weather as weather
    monkeypatch.setattr(weather, "fetch_forecast", lambda hours=48: [])
    yield db_path


def _seed_constant_solar(day, hour_utc: int, kw: float, *, minutes=(0, 15, 30, 45, 60)):
    """Insert constant-kW solar samples across one hour of ``day`` (UTC)."""
    for m in minutes:
        ts = datetime(day.year, day.month, day.day, hour_utc, 0, tzinfo=UTC) + timedelta(minutes=m)
        db.save_pv_realtime_sample(
            ts.isoformat().replace("+00:00", "Z"),
            solar_power_kw=kw,
            source="test",
        )


def test_solar_rollup_integrates_to_kwh():
    day = (datetime.now(UTC) - timedelta(days=1)).date()
    _seed_constant_solar(day, 10, 3.0)  # 3 kW held for one hour → ~3 kWh
    rollup = db.half_hourly_solar_kwh_for_day(day)
    total = sum(rollup.values())
    assert 2.5 <= total <= 3.5, f"expected ~3 kWh integrated, got {total}"


def test_pv_today_shape_and_accuracy():
    day = (datetime.now(UTC) - timedelta(days=1)).date()  # fully elapsed
    _seed_constant_solar(day, 10, 3.0)

    resp = asyncio.run(pv_router.get_pv_today(date=day.isoformat()))

    assert resp["date"] == day.isoformat()
    assert len(resp["slots"]) == 48
    # Every slot carries the full overlay key set; forecast is 0 (no provider
    # in test); price/load/kind are null (no rates/profile/runs seeded).
    # tank_target_c / dhw_load_kwh come from the dhw_policy heating-plan forecast.
    expected_keys = {"slot_utc", "pv_forecast_kwh", "pv_actual_kwh", "import_price_p",
                     "base_load_kwh", "kind", "tank_target_c", "dhw_load_kwh"}
    assert all(set(s) == expected_keys for s in resp["slots"])
    assert all(s["pv_forecast_kwh"] == 0.0 for s in resp["slots"])

    realised = [s["pv_actual_kwh"] for s in resp["slots"] if s["pv_actual_kwh"] is not None]
    assert realised, "expected at least one slot with realised PV"
    assert 2.5 <= sum(realised) <= 3.5

    acc = resp["accuracy"]
    assert acc is not None
    assert acc["slots_compared"] >= 1
    assert acc["forecast_kwh"] == 0.0
    assert 2.5 <= acc["actual_kwh"] <= 3.5
    # Forecast is zero, so bias (actual − forecast) == realised total, and MAE
    # is the mean realised kWh per compared slot.
    assert acc["bias_kwh"] == pytest.approx(acc["actual_kwh"], abs=1e-6)


def test_pv_today_future_day_has_no_realised():
    day = (datetime.now(UTC) + timedelta(days=1)).date()  # all slots in the future
    resp = asyncio.run(pv_router.get_pv_today(date=day.isoformat()))
    assert len(resp["slots"]) == 48
    assert all(s["pv_actual_kwh"] is None for s in resp["slots"])
    assert resp["accuracy"] is None


def test_pv_today_rejects_bad_date():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(pv_router.get_pv_today(date="not-a-date"))
