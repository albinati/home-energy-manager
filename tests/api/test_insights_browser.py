"""v10.2 E2 — Insights browser endpoints (agile/day, patterns, timeseries).

Covers shape + validation + DST handling. Pure SQLite reads — no cloud.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src import db


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def seed_agile(monkeypatch):
    """Seed 48 half-hour rates for 2025-07-15 + 46 for 2025-03-30 (DST spring)."""
    monkeypatch.setenv("OCTOPUS_TARIFF_CODE", "E-1R-AGILE-TEST")
    from src.config import config
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-TEST")

    rows: list[dict] = []
    # Normal day (UK, no DST)
    base = datetime(2025, 7, 15, 0, 0, tzinfo=UTC)
    for i in range(48):
        s = base + timedelta(minutes=30 * i)
        e = s + timedelta(minutes=30)
        # Synthetic price curve: cheap at night (<5p), peak 16-19h (>30p),
        # rest mid-band ~15p
        h = s.replace(tzinfo=UTC).hour
        if h < 6:
            p = 2.0 + 0.1 * i
        elif 16 <= h < 19:
            p = 35.0 + 0.5 * i
        else:
            p = 14.0 + 0.05 * i
        rows.append({
            "valid_from": s.isoformat().replace("+00:00", "Z"),
            "valid_to": e.isoformat().replace("+00:00", "Z"),
            "value_inc_vat": p,
            "value_exc_vat": p / 1.05,
        })
    # Spring-forward day in UK: 30 March 2025 has 46 slots in local time
    base = datetime(2025, 3, 30, 0, 0, tzinfo=UTC)
    for i in range(48):
        s = base + timedelta(minutes=30 * i)
        e = s + timedelta(minutes=30)
        rows.append({
            "valid_from": s.isoformat().replace("+00:00", "Z"),
            "valid_to": e.isoformat().replace("+00:00", "Z"),
            "value_inc_vat": 12.0,
            "value_exc_vat": 12.0 / 1.05,
        })

    db.save_agile_rates(rows, "E-1R-AGILE-TEST")
    yield "E-1R-AGILE-TEST"


@pytest.fixture
def seed_execution_log():
    """Seed a few execution_log rows on 2025-07-14 for execution endpoint + patterns tests."""
    base = datetime(2025, 7, 14, 0, 0, tzinfo=UTC)
    with db._lock:
        conn = db.get_connection()
        try:
            for i in range(48):
                ts = base + timedelta(minutes=30 * i)
                conn.execute(
                    """INSERT INTO execution_log
                       (timestamp, consumption_kwh, agile_price_pence, svt_shadow_price_pence,
                        soc_percent, fox_mode, daikin_lwt, daikin_outdoor_temp, slot_kind)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ts.isoformat().replace("+00:00", "Z"),
                        0.4 + 0.01 * i,
                        15.0 + 0.1 * i,
                        24.5,
                        50, "Self Use", 35.0, 12.0, "standard",
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    yield


class TestAgileDay:
    def test_returns_classified_slots(self, client, seed_agile):
        r = client.get("/api/v1/agile/day?date=2025-07-15")
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == "2025-07-15"
        assert body["tariff_code"] == "E-1R-AGILE-TEST"
        # At least 46 (DST-safe), at most 50
        assert 46 <= len(body["slots"]) <= 50
        kinds = {s["kind"] for s in body["slots"]}
        # Synthetic data has clear cheap+peak windows
        assert "cheap" in kinds
        assert "peak" in kinds

    def test_dst_spring_forward(self, client, seed_agile):
        # 30 March 2025: UK clocks jump 01:00 → 02:00 BST → 46 local slots
        r = client.get("/api/v1/agile/day?date=2025-03-30")
        assert r.status_code == 200
        body = r.json()
        # Allow 46 (true DST) or 48 if tz config differs in env
        assert len(body["slots"]) in (46, 48)

    def test_bad_date_400(self, client, seed_agile):
        r = client.get("/api/v1/agile/day?date=not-a-date")
        assert r.status_code == 400

    def test_no_tariff_returns_empty(self, client, monkeypatch):
        monkeypatch.setenv("OCTOPUS_TARIFF_CODE", "")
        from src.config import config
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "")
        r = client.get("/api/v1/agile/day?date=2025-07-15")
        assert r.status_code == 200
        assert r.json()["slots"] == []


class TestExecutionByDate:
    def test_arbitrary_date(self, client, seed_execution_log):
        r = client.get("/api/v1/execution/today?date=2025-07-14")
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == "2025-07-14"
        assert len(body["slots"]) == 48
        # Totals sane
        t = body["totals"]
        assert t["load_kwh"] > 0
        assert t["cost_realised_p"] > 0

    def test_bad_date_400(self, client):
        r = client.get("/api/v1/execution/today?date=2025-13-99")
        assert r.status_code == 400

    def test_no_date_means_today(self, client):
        r = client.get("/api/v1/execution/today")
        assert r.status_code == 200
        # Today returns whatever today is — just shape check
        assert "slots" in r.json()
        assert "totals" in r.json()


class TestPatterns:
    def test_hourly_endpoint_shape(self, client, seed_execution_log):
        r = client.get("/api/v1/patterns/hourly?start=2025-07-14&end=2025-07-14")
        assert r.status_code == 200
        body = r.json()
        assert body["start"] == "2025-07-14"
        # 24 hour buckets
        assert len(body["profile"]) == 24
        # Has samples in every bucket since we seeded 48 rows across 24 hours
        assert body["total_samples"] == 48

    def test_dow_endpoint_shape(self, client):
        r = client.get("/api/v1/patterns/dow?start=2025-07-01&end=2025-07-31")
        assert r.status_code == 200
        body = r.json()
        assert set(body["profile"].keys()) == {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}

    def test_price_distribution_endpoint(self, client, seed_agile):
        r = client.get("/api/v1/patterns/price-distribution?start=2025-07-15&end=2025-07-15")
        assert r.status_code == 200
        body = r.json()
        # Total is 48 (or 46/50 at DST) for one day
        assert body["total_slots"] >= 46
        # Sum of pcts ~= 100
        kinds = body["kinds"]
        s = sum(k["pct"] for k in kinds.values())
        assert 99.5 <= s <= 100.5

    def test_pv_calibration_partial_response(self, client):
        r = client.get("/api/v1/patterns/pv-calibration?start=2025-07-01&end=2025-07-07")
        assert r.status_code == 200
        body = r.json()
        assert body["forecast_unavailable"] is True
        assert len(body["series"]) == 7

    def test_range_validation(self, client):
        r = client.get("/api/v1/patterns/hourly?start=bad&end=worse")
        assert r.status_code == 400
        r = client.get("/api/v1/patterns/hourly?start=2025-07-15&end=2025-07-01")
        assert r.status_code == 400


class TestTimeseries:
    def test_load_kwh_hour(self, client, seed_execution_log):
        r = client.get("/api/v1/timeseries?metric=load_kwh&start=2025-07-14&end=2025-07-14&granularity=hour")
        assert r.status_code == 200
        body = r.json()
        assert body["metric"] == "load_kwh"
        assert body["granularity"] == "hour"
        # 24 hour buckets in seeded day
        assert body["count"] == 24
        # Each point has t and v
        assert all("t" in p and "v" in p for p in body["points"])

    def test_load_kwh_slot_returns_per_tick(self, client, seed_execution_log):
        r = client.get("/api/v1/timeseries?metric=load_kwh&start=2025-07-14&end=2025-07-14&granularity=slot")
        assert r.status_code == 200
        # All 48 slots
        assert r.json()["count"] == 48

    def test_import_p_mean_aggregation(self, client, seed_execution_log):
        r = client.get("/api/v1/timeseries?metric=import_p&start=2025-07-14&end=2025-07-14&granularity=day")
        assert r.status_code == 200
        body = r.json()
        # Day-level mean of seeded prices (15..19.7) should be ~17.35
        assert body["count"] == 1
        v = body["points"][0]["v"]
        assert 16.5 <= v <= 18.5

    def test_unknown_metric_400(self, client):
        r = client.get("/api/v1/timeseries?metric=temperature_c&start=2025-07-14&end=2025-07-14")
        assert r.status_code == 400

    def test_solar_kwh_requires_day_granularity(self, client):
        r = client.get("/api/v1/timeseries?metric=solar_kwh&start=2025-07-14&end=2025-07-14&granularity=hour")
        assert r.status_code == 400

    def test_bad_granularity(self, client):
        r = client.get("/api/v1/timeseries?metric=load_kwh&start=2025-07-14&end=2025-07-14&granularity=year")
        assert r.status_code == 400
