"""v10.2 E1.S3 — daikin_consumption_daily cache.

Two-path sync_daikin_daily:
  1. Onecta path: client.get_heating_daily_kwh returns the value → source='onecta'.
  2. Telemetry-integral fallback: integrate daikin_telemetry rows over the day.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src import db
from src.daikin import service as daikin_svc


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


def _stub_heating_daily(monkeypatch, daily: list | None):
    """Stub the cached service read used by sync_daikin_daily (post read-burst
    fix it goes through ``heating_daily_kwh`` instead of a raw client)."""
    calls: list[tuple[int, int]] = []

    def _fake(year: int, month: int, *, actor: str = "energy_insights"):
        calls.append((year, month))
        return daily

    monkeypatch.setattr(daikin_svc, "heating_daily_kwh", _fake)
    return calls


def test_onecta_path_records_source(monkeypatch):
    daily = [0.0] * 30
    daily[14] = 12.5  # day 15 (idx 14)
    _stub_heating_daily(monkeypatch, daily)
    monkeypatch.setattr(daikin_svc, "should_block", lambda _v: False)

    row = daikin_svc.sync_daikin_daily(date(2024, 11, 15))
    assert row is not None
    assert row["kwh_total"] == 12.5
    assert row["source"] == "onecta"
    assert row["date"] == "2024-11-15"


def test_onecta_zero_falls_back_to_telemetry_integral(monkeypatch):
    """Onecta returns the day but value is 0 → fall back to telemetry."""
    _stub_heating_daily(monkeypatch, [0.0] * 30)
    monkeypatch.setattr(daikin_svc, "should_block", lambda _v: False)

    # Seed daikin_telemetry: 4 ticks across the day with cold outdoor temps.
    # fetched_at is stored as epoch seconds (float) per src/db.py:2115.
    base = datetime(2024, 11, 15, 6, 0, tzinfo=UTC)
    for i in range(4):
        ts = base + timedelta(hours=i * 4)
        db.insert_daikin_telemetry({
            "fetched_at": ts.timestamp(),
            "tank_temp_c": 45.0,
            "indoor_temp_c": 21.0,
            "outdoor_temp_c": 5.0,
            "tank_target_c": 45.0,
            "lwt_actual_c": 35.0,
            "mode": "heating",
            "weather_regulation": 1,
            "source": "live",
        })

    row = daikin_svc.sync_daikin_daily(date(2024, 11, 15))
    assert row is not None
    assert row["source"] == "telemetry_integral"
    assert row["kwh_total"] > 0


def test_no_data_returns_none(monkeypatch):
    _stub_heating_daily(monkeypatch, None)
    monkeypatch.setattr(daikin_svc, "should_block", lambda _v: False)

    row = daikin_svc.sync_daikin_daily(date(2024, 11, 15))
    assert row is None


def test_quota_blocked_skips_onecta_path(monkeypatch):
    """When Daikin quota is exhausted, Onecta path is skipped entirely."""
    calls = _stub_heating_daily(monkeypatch, [10.0] * 30)
    monkeypatch.setattr(daikin_svc, "should_block", lambda _v: True)  # blocked!

    row = daikin_svc.sync_daikin_daily(date(2024, 11, 15))
    # No telemetry seeded → returns None; importantly Onecta was never called.
    assert row is None
    assert calls == [], "must not read Daikin when quota blocked"
