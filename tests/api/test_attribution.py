"""Phase 5 — GET /api/v1/attribution/day shares computation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src import db
from src.api.main import app


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


@pytest.fixture
def client():
    return TestClient(app)


def _upsert_day(date_str: str, *, solar: float, load: float, imp: float,
                exp: float, chg: float, dis: float) -> None:
    db.upsert_fox_energy_daily([{
        "date": date_str,
        "solar_kwh": solar, "load_kwh": load,
        "import_kwh": imp, "export_kwh": exp,
        "charge_kwh": chg, "discharge_kwh": dis,
        "fetched_at": datetime.now(UTC).isoformat(),
    }])


def test_attribution_returns_unavailable_when_no_row(client):
    r = client.get("/api/v1/attribution/day?date=1999-01-01")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["shares"] is None


def test_attribution_shares_sum_to_100(client):
    _upsert_day("2026-04-20",
                solar=20.0, load=15.0, imp=2.0, exp=5.0, chg=6.0, dis=3.0)
    r = client.get("/api/v1/attribution/day?date=2026-04-20")
    body = r.json()
    assert body["available"] is True
    s = body["shares"]
    total = s["self_use_pct"] + s["battery_pct"] + s["export_pct"]
    assert abs(total - 100.0) < 0.2, f"shares sum to {total}"


def test_attribution_defaults_to_yesterday(client):
    yday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    _upsert_day(yday, solar=10.0, load=8.0, imp=1.0, exp=3.0, chg=2.0, dis=1.0)
    r = client.get("/api/v1/attribution/day")
    assert r.status_code == 200
    assert r.json()["date"] == yday
    assert r.json()["available"] is True
