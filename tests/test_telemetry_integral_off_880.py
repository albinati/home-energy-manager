"""#880 - the telemetry integral must not model heating/DHW kWh while the
Altherma is OFF (Onecta holiday / onOffMode=off)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import db
from src.config import config
from src.daikin import service as svc
from src.physics import get_daikin_heating_kw

DAY = date(2026, 10, 28)
ISO = DAY.isoformat()


@pytest.fixture(autouse=True)
def _init(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    db.init_db()


def _seed(**flags):
    base = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
    for i in range(7):  # 00:00 .. 03:00, 30 min apart -> buckets 0 and 1
        row = {
            "fetched_at": (base + timedelta(minutes=30 * i)).timestamp(),
            "source": "live", "tank_temp_c": 45.0, "indoor_temp_c": 21.0,
            "outdoor_temp_c": 5.0, "tank_target_c": 45.0, "lwt_actual_c": 35.0,
            "mode": "heating", "weather_regulation": 1,
        }
        row.update(flags)
        db.insert_daikin_telemetry(row)


def _sql(stmt):
    with db._lock:
        c = db.get_connection()
        try:
            c.execute(stmt)
            c.commit()
        finally:
            c.close()


def _rows():
    return {r["bucket_idx"]: r for r in db.get_daikin_consumption_2hourly_range(ISO, ISO)}


def test_normal_device_matches_model():
    _seed()
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    expect_heat = round(float(get_daikin_heating_kw(5.0)) * 0.5 * 4, 3)
    assert expect_heat > 0
    assert out[0]["kwh_heating"] == pytest.approx(expect_heat)
    assert out[0]["kwh_dhw"] > 0
    assert not out[0]["off"] and not out[1]["off"]


def test_explicit_on_flags_identical_to_null_flags():
    _seed()
    null_out = svc.compute_daikin_2hourly_telemetry(DAY)
    _sql("UPDATE daikin_telemetry SET is_on=1, tank_on=1, holiday_active=0, tank_holiday_active=0")
    assert svc.compute_daikin_2hourly_telemetry(DAY) == null_out


@pytest.mark.parametrize("flags", [
    {"holiday_active": True, "tank_holiday_active": True},
    {"is_on": False, "tank_on": False},
    {"holiday_active": True, "tank_on": False},
])
def test_off_zero_and_source(flags):
    _seed(**flags)
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out and all(b["kwh_total"] == 0 and b["off"] for b in out.values())
    res = svc.sync_daikin_2hourly_telemetry(DAY)
    assert res["written"] == len(out)
    rows = _rows()
    assert rows and all(r["source"] == "telemetry_integral_off" and r["kwh_total"] == 0
                        for r in rows.values())


def test_tank_only_holiday_zeroes_dhw_only():
    _seed(tank_holiday_active=True)
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_dhw"] == 0 and out[0]["kwh_heating"] > 0 and not out[0]["off"]
    svc.sync_daikin_2hourly_telemetry(DAY)
    assert all(r["source"] == "telemetry_integral" for r in _rows().values())


def test_climate_only_off_zeroes_heating_only():
    _seed(holiday_active=True)
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_heating"] == 0 and out[0]["kwh_dhw"] > 0 and not out[0]["off"]


def test_overwrite_rules_for_off_source():
    _seed(holiday_active=True, tank_holiday_active=True)
    # bucket 1: an existing _off row is refreshed like telemetry_integral
    db.upsert_daikin_consumption_2hourly(
        date=ISO, bucket_idx=1, kwh_total=0.0, kwh_heating=0.0, kwh_dhw=0.0,
        source="telemetry_integral_off")
    # bucket 0: Onecta counter disagrees by >= 0.5 -> Onecta stays authoritative
    db.upsert_daikin_consumption_2hourly(
        date=ISO, bucket_idx=0, kwh_total=2.0, kwh_heating=2.0, kwh_dhw=0.0,
        source="onecta_cache")
    res = svc.sync_daikin_2hourly_telemetry(DAY)
    dec = {d["bucket_idx"]: d["decision"] for d in res["buckets"]}
    assert dec[1] == "refresh_telemetry"
    assert dec[0].startswith("onecta_authoritative")
    assert _rows()[0]["source"] == "onecta_cache"
    # and a later onecta_cache upsert replaces an _off row
    db.upsert_daikin_consumption_2hourly(
        date=ISO, bucket_idx=1, kwh_total=1.0, kwh_heating=1.0, kwh_dhw=0.0,
        source="onecta_cache")
    assert _rows()[1]["source"] == "onecta_cache"


def test_historical_null_rows_keep_model():
    _seed()
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_heating"] > 0 and not out[0]["off"]


def test_live_persist_stores_flags():
    dev = SimpleNamespace(
        tank_temperature=45.0,
        temperature=SimpleNamespace(room_temperature=21.0, outdoor_temperature=5.0),
        tank_target=45.0, leaving_water_temperature=30.0, operation_mode="heating",
        weather_regulation_enabled=True, is_on=False, tank_on=None,
        holiday_active=True, tank_holiday_active=False)
    svc._persist_daikin_telemetry_live([dev])
    with db._lock:
        c = db.get_connection()
        try:
            r = c.execute(
                "SELECT is_on, tank_on, holiday_active, tank_holiday_active FROM daikin_telemetry"
            ).fetchone()
        finally:
            c.close()
    assert tuple(r) == (0, None, 1, 0)


def test_daily_integral_skips_off_samples(monkeypatch):
    monkeypatch.setattr(svc, "heating_daily_kwh", lambda *a, **k: None)
    monkeypatch.setattr(svc, "should_block", lambda _v: False)
    _seed(holiday_active=True)
    assert svc.sync_daikin_daily(DAY) is None
