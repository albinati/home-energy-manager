"""GET /api/v1/forecast/daily — per-LOCAL-day committed forecast vs actual sums
(load_error_log + pv_error_log) for the aggregated-chart overlay (#624)."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from src import db
from src.api.routers import pv as pv_router


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "DB_PATH", str(db_path))
    db.init_db()
    yield db_path


def _insert(table: str, slot_utc: str, forecast: float | None, actual: float | None) -> None:
    conn = sqlite3.connect(str(db.config.DB_PATH))
    try:
        if table == "load_error_log":
            conn.execute(
                "INSERT INTO load_error_log (slot_time_utc, forecast_kwh, forecast_base_kwh, actual_kwh, error_kwh, built_at_utc)"
                " VALUES (?, ?, ?, ?, 0, '2026-07-01T04:22:00Z')",
                (slot_utc, forecast, forecast, actual),
            )
        else:
            conn.execute(
                "INSERT INTO pv_error_log (slot_time_utc, run_id, forecast_kwh, actual_kwh, error_kwh, built_at_utc)"
                " VALUES (?, 1, ?, ?, 0, '2026-07-01T04:20:00Z')",
                (slot_utc, forecast, actual),
            )
        conn.commit()
    finally:
        conn.close()


def test_daily_sums_group_by_local_day():
    # Two load slots + one pv slot on 25 Jun (UTC am, unambiguous local day).
    _insert("load_error_log", "2026-06-25T10:00:00+00:00", 0.4, 0.5)
    _insert("load_error_log", "2026-06-25T10:30:00+00:00", 0.6, 0.7)
    _insert("pv_error_log", "2026-06-25T11:00:00+00:00", 1.5, 1.2)

    resp = asyncio.run(pv_router.get_forecast_daily("2026-06-25", "2026-06-25"))
    assert len(resp["days"]) == 1
    d = resp["days"][0]
    assert d["date"] == "2026-06-25"
    assert d["load_forecast_kwh"] == pytest.approx(1.0)
    assert d["load_actual_kwh"] == pytest.approx(1.2)
    assert d["load_n_slots"] == 2
    assert d["pv_forecast_kwh"] == pytest.approx(1.5)
    assert d["pv_actual_kwh"] == pytest.approx(1.2)
    assert d["pv_n_slots"] == 1


def test_bst_midnight_slot_buckets_to_next_local_day():
    # 23:30 UTC on the 25th = 00:30 local (BST) on the 26th — must bucket to
    # the 26th, matching the period navigator's local days.
    _insert("load_error_log", "2026-06-25T23:30:00+00:00", 0.3, 0.3)
    resp = asyncio.run(pv_router.get_forecast_daily("2026-06-25", "2026-06-26"))
    days = {d["date"]: d for d in resp["days"]}
    assert "2026-06-26" in days and "2026-06-25" not in days
    assert days["2026-06-26"]["load_forecast_kwh"] == pytest.approx(0.3)


def test_day_missing_from_one_log_has_nulls_for_that_side():
    _insert("pv_error_log", "2026-06-25T11:00:00+00:00", 2.0, 1.8)
    resp = asyncio.run(pv_router.get_forecast_daily("2026-06-25", "2026-06-25"))
    d = resp["days"][0]
    assert d["load_forecast_kwh"] is None
    assert d["pv_forecast_kwh"] == pytest.approx(2.0)


def test_invalid_range_rejected():
    with pytest.raises(HTTPException):
        asyncio.run(pv_router.get_forecast_daily("not-a-date", "2026-06-25"))
    with pytest.raises(HTTPException):
        asyncio.run(pv_router.get_forecast_daily("2026-06-25", "2026-06-24"))  # end < start
    with pytest.raises(HTTPException):
        asyncio.run(pv_router.get_forecast_daily("2020-01-01", "2026-06-25"))  # > 400 days


def test_dst_fall_back_day_buckets_all_50_slots_to_the_day():
    # 2026-10-25 = clocks-back day in Europe/London (25 local hours, 50 slots).
    # The 00:30Z slot (01:30 BST) and the 23:30Z slot (23:30 GMT) both belong
    # to the 25th; the 24T23:30Z slot (00:30 BST on the 25th) does too.
    _insert("load_error_log", "2026-10-24T23:30:00Z", 0.1, 0.1)
    _insert("load_error_log", "2026-10-25T00:30:00Z", 0.2, 0.2)
    _insert("load_error_log", "2026-10-25T23:30:00Z", 0.4, 0.4)
    resp = asyncio.run(pv_router.get_forecast_daily("2026-10-25", "2026-10-25"))
    assert len(resp["days"]) == 1
    d = resp["days"][0]
    assert d["date"] == "2026-10-25"
    assert d["load_forecast_kwh"] == pytest.approx(0.7)
    assert d["load_n_slots"] == 3


def test_datetime_string_rejected_not_silently_shifted():
    with pytest.raises(HTTPException):
        asyncio.run(pv_router.get_forecast_daily("2026-06-25T18:00:00", "2026-06-26"))
