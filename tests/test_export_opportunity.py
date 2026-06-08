"""Export opportunity log — the running tally of money left on the table by
being on flat SEG export instead of Outgoing Agile.

Covers the db upsert/get round-trip, the opportunity = agile − seg arithmetic,
and the /api/v1/export/opportunity endpoint's aggregation + live-today field.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from src import db
from src.config import config


def _client(monkeypatch):
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    from src.api.main import app
    return TestClient(app)


def test_upsert_is_idempotent_and_computes_opportunity():
    db.init_db()
    d = date(2026, 6, 1)
    db.upsert_export_opportunity(d, 5.0, 20.5, 60.0)
    db.upsert_export_opportunity(d, 5.0, 20.5, 70.0)  # re-record (overwrite)
    rows = db.get_export_opportunity(date(2026, 5, 1), date(2026, 6, 30))
    assert len(rows) == 1                     # idempotent on day
    assert rows[0]["opportunity_pence"] == 49.5   # 70.0 − 20.5


def test_export_opportunity_endpoint_aggregates(monkeypatch):
    db.init_db()
    y = date.today() - timedelta(days=1)
    db.upsert_export_opportunity(y - timedelta(days=1), 5.0, 20.5, 60.0)  # opp 39.5p
    db.upsert_export_opportunity(y - timedelta(days=2), 3.0, 12.3, 30.0)  # opp 17.7p
    # Live "today" accrual — mock the per-day revenue split.
    monkeypatch.setattr(
        "src.analytics.pnl.export_revenues_for_day",
        lambda d: {"agile_pence": 10.0, "seg_flat_pence": 4.0, "export_kwh": 1.0, "agile_avg_p": 10.0},
    )
    client = _client(monkeypatch)
    body = client.get("/api/v1/export/opportunity?days=30").json()

    assert body["n_days"] == 2
    assert round(body["opportunity_gbp"], 2) == round((39.5 + 17.7) / 100, 2)  # £0.57
    assert round(body["seg_gbp"], 2) == round((20.5 + 12.3) / 100, 2)
    assert round(body["agile_gbp"], 2) == round((60.0 + 30.0) / 100, 2)
    # Annualized = period opp / n_days × 365.
    assert body["annualized_gbp"] == round((57.2 / 100) / 2 * 365, 2)
    # Live today: agile 10 − seg 4 = 6p = £0.06.
    assert round(body["today"]["opportunity_gbp"], 2) == 0.06
    assert len(body["daily"]) == 2


def test_endpoint_lazy_backfills_when_empty(monkeypatch):
    """An empty log triggers a one-shot backfill so the figure is there at once."""
    db.init_db()
    monkeypatch.setattr(
        "src.analytics.pnl.export_revenues_for_day",
        lambda d: {"agile_pence": 8.0, "seg_flat_pence": 4.1, "export_kwh": 1.0, "agile_avg_p": 8.0},
    )
    client = _client(monkeypatch)
    body = client.get("/api/v1/export/opportunity?days=5").json()
    # Backfill ran for the 5-day window → rows exist, each opp = 8.0 − 4.1 = 3.9p.
    assert body["n_days"] >= 1
    assert all(round(r["opportunity_gbp"], 3) == round(3.9 / 100, 3) for r in body["daily"])
