"""Forecast tab — half-hour slots now interpolate between hourly weather rows.

The bug: Open-Meteo returns hourly data, meteo_forecast stores it at HH:00
timestamps, and /api/v1/optimization/inputs used to do exact-ISO lookup —
so HH:30 slots showed "—" for temp + solar. Users thought the LP solved
against sparse inputs. Actually the LP interpolates internally via
weather._interp_hourly_scalar, so the bug was purely display.

Fix: mirror the LP's linear interpolation in the endpoint so the Forecast
tab shows the same continuous series the solver consumes.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src import db
from src.api.main import app


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()
    # Clean any leftover meteo rows so our seed is deterministic.
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM meteo_forecast")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def _seed_three_hours(start: datetime) -> None:
    """Seed temp+solar at start, start+1h, start+2h — spanning the first 3 hours
    of the rolling horizon. Half-hour slots between them should interpolate."""
    rows = [
        {"slot_time": start.isoformat(),                          "temp_c": 10.0, "solar_w_m2": 100.0},
        {"slot_time": (start + timedelta(hours=1)).isoformat(),   "temp_c": 12.0, "solar_w_m2": 200.0},
        {"slot_time": (start + timedelta(hours=2)).isoformat(),   "temp_c": 14.0, "solar_w_m2": 300.0},
    ]
    db.save_meteo_forecast(rows, start.date().isoformat())


def test_half_hour_slot_interpolates_linearly(client):
    # Anchor the horizon so our seed rows land inside it. The endpoint's
    # day_start is the hour floor of "now", so we must seed at that hour.
    now = datetime.now(UTC)
    anchor = now.replace(minute=0, second=0, microsecond=0)
    _seed_three_hours(anchor)

    r = client.get(f"/api/v1/optimization/inputs?horizon_hours=4")
    assert r.status_code == 200
    slots = r.json()["slots"]
    by_time = {s["t_utc"].rstrip("Z"): s for s in slots}

    anchor_key = anchor.isoformat().replace("+00:00", "")
    # The HH:00 slot has the raw value, the HH:30 slot must be the linear
    # midpoint between this hour and the next hour's raw values.
    hh00 = by_time.get(anchor_key) or {}
    hh30_key = anchor.replace(minute=30).isoformat().replace("+00:00", "")
    hh30 = by_time.get(hh30_key) or {}

    # Raw anchors came through.
    assert hh00.get("temp_c") == pytest.approx(10.0)
    assert hh00.get("solar_w_m2") == pytest.approx(100.0)
    # Interpolated midpoint of 10↔12 and 100↔200.
    assert hh30.get("temp_c") is not None, f"HH:30 missing temp_c: {hh30}"
    assert hh30["temp_c"] == pytest.approx(11.0, abs=0.01)
    assert hh30["solar_w_m2"] == pytest.approx(150.0, abs=0.01)


def test_slots_beyond_last_seed_carry_last_forward(client):
    anchor = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    _seed_three_hours(anchor)

    r = client.get("/api/v1/optimization/inputs?horizon_hours=6")
    slots = r.json()["slots"]
    # The last seeded row was anchor+2h; slots beyond that must carry that
    # value forward rather than flip to None (the LP's interp does the same).
    tail = [s for s in slots if s["t_utc"] > (anchor + timedelta(hours=2)).isoformat().replace("+00:00", "Z")]
    assert tail, "need at least one post-seed slot"
    for s in tail:
        assert s["temp_c"] == pytest.approx(14.0), f"slot {s['t_utc']} should carry 14.0 forward, got {s['temp_c']}"


def test_no_seed_rows_returns_nulls_gracefully(client):
    # Empty meteo_forecast table — endpoint must still 200 with None fields.
    r = client.get("/api/v1/optimization/inputs")
    assert r.status_code == 200
    slots = r.json()["slots"]
    assert slots  # rolling horizon always emits slots
    # At least the temp field is None when no weather data exists.
    assert all(s["temp_c"] is None for s in slots)
