"""Perf PR (2026-06-13): /energy/lifetime aggregate + /metrics TTL cache.

The cockpit footer's lifetime strip used to fire SIX /energy/monthly calls,
each re-running an uncached ~1-2.7s PnL replay — ~10s of repeated server
compute per page load, serialised on the single event loop. /metrics added
another ~1s on-loop. These tests pin the new aggregate's shape, the
active-month filter (so the displayed totals don't move), and that BOTH
endpoints now serve a TTL cache hit without recomputing.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import db
import src.api.main as main_mod
from src.api.main import app


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _foxess_configured(monkeypatch):
    # Both endpoints 503 without Fox config; force the configured path so the
    # cache logic is exercised regardless of the test env's .env.
    monkeypatch.setattr(main_mod, "_foxess_configured", lambda: True)


# ── /energy/lifetime ─────────────────────────────────────────────────────────

def test_lifetime_shape_and_active_filter(client, monkeypatch):
    """Aggregate sums only ACTIVE months (net != 0 OR export > 0) and returns
    pre-summed totals — matching the old client-side filter exactly."""
    class _Energy:
        def __init__(self, solar, export):
            self.solar_kwh = solar
            self.export_kwh = export

    class _Cost:
        def __init__(self, net):
            self.net_cost_pounds = net

    class _Insights:
        def __init__(self, solar, export, net):
            self.energy = _Energy(solar, export)
            self.cost = _Cost(net)

    # Three months: one active-by-cost, one active-by-export, one INACTIVE.
    # Seed RELATIVE to the current month using the EXACT same reference the
    # endpoint uses — datetime.now(BULLETPROOF_TIMEZONE).date() — so the test is
    # stable across month/year boundaries. A hardcoded (2026,4/5/6) reddened CI
    # every July run; and `date.today()` (system-UTC in CI) disagreed with the
    # endpoint's LOCAL-tz `today` across the UTC/BST midnight (23:56 UTC = 00:56
    # BST = next month) — both are date-relative flakes.
    from datetime import datetime as _datetime
    from zoneinfo import ZoneInfo as _ZoneInfo
    from src.config import config as _config
    _today = _datetime.now(_ZoneInfo(_config.BULLETPROOF_TIMEZONE)).date()
    _anchors = main_mod._last_n_month_anchors(_today, 3)  # oldest → current
    table = {
        _anchors[0]: _Insights(100.0, 40.0, 12.5),   # active (net != 0)
        _anchors[1]: _Insights(120.0, 50.0, 0.0),    # active (export > 0)
        _anchors[2]: _Insights(0.0, 0.0, 0.0),       # inactive — excluded
    }

    def fake_insights(year, month):
        return table.get((year, month))

    def fake_monthly_pnl(anchor):
        # +£2 each for the two active months; the inactive one is never reached.
        return {"delta_vs_fixed_tariff_real_gbp": 2.0}

    monkeypatch.setattr(main_mod, "get_monthly_insights", fake_insights)
    monkeypatch.setattr("src.analytics.pnl.compute_monthly_pnl", fake_monthly_pnl)
    main_mod._lifetime_cache.clear()

    r = client.get("/api/v1/energy/lifetime?months=3")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "months": 2,
        "solar_kwh": 220.0,
        "export_kwh": 90.0,
        "saved_vs_fixed_pounds": 4.0,
    }


def test_lifetime_serves_cache_hit_without_recompute(client, monkeypatch):
    calls = {"n": 0}

    def fake_compute(n_months, today):
        calls["n"] += 1
        return {"months": 1, "solar_kwh": 1.0, "export_kwh": 1.0, "saved_vs_fixed_pounds": 1.0}

    monkeypatch.setattr(main_mod, "_compute_lifetime", fake_compute)
    main_mod._lifetime_cache.clear()

    a = client.get("/api/v1/energy/lifetime?months=6").json()
    b = client.get("/api/v1/energy/lifetime?months=6").json()
    assert a == b
    assert calls["n"] == 1  # second request hit the cache


def test_last_n_month_anchors_walks_back_across_year_boundary():
    """Current month + previous n-1, oldest first; crosses the year edge."""
    from datetime import date as _date

    assert main_mod._last_n_month_anchors(_date(2026, 6, 13), 6) == [
        (2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5), (2026, 6),
    ]
    # February anchor reaching back into the prior year.
    assert main_mod._last_n_month_anchors(_date(2026, 2, 1), 3) == [
        (2025, 12), (2026, 1), (2026, 2),
    ]


# ── /metrics ─────────────────────────────────────────────────────────────────

def test_metrics_serves_cache_hit_without_recompute(client, monkeypatch):
    calls = {"n": 0}
    sentinel = {"battery_soc_percent": 42, "_probe": True}

    def fake_compute():
        calls["n"] += 1
        return dict(sentinel)

    monkeypatch.setattr(main_mod, "_compute_metrics", fake_compute)
    main_mod._metrics_cache = None

    a = client.get("/api/v1/metrics").json()
    b = client.get("/api/v1/metrics").json()
    assert a == b == sentinel
    assert calls["n"] == 1


def test_metrics_ttl_zero_disables_cache(client, monkeypatch):
    calls = {"n": 0}

    def fake_compute():
        calls["n"] += 1
        return {"n": calls["n"]}

    monkeypatch.setattr(main_mod, "_compute_metrics", fake_compute)
    monkeypatch.setattr(main_mod.config, "METRICS_CACHE_TTL_SECONDS", 0, raising=False)
    main_mod._metrics_cache = None

    client.get("/api/v1/metrics")
    client.get("/api/v1/metrics")
    assert calls["n"] == 2  # TTL=0 → always recompute (kill-switch)
