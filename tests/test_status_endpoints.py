"""Ops-status endpoints (/status/alerts + /status/feedback) — cockpit plan PR 2.

These power the alert strip and the self-check panel. Contract under test:
viewer-readable without a token, never leak stacktraces, sub-cache the
quota-costing upstream reads, and reflect the recently-shipped feedback
loops (DHW auto-scale #534, LWT demand gate #540, forecast provenance #542,
meter staleness #533).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src import db
from src.config import config


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    db.init_db()
    from src.api.routers import status as status_router
    status_router._cache.clear()
    yield
    status_router._cache.clear()


def _client(monkeypatch):
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    from src.api.main import app
    return TestClient(app)


def _quiet_upstreams(monkeypatch):
    """Neutralise the vendor-touching blocks so unit tests stay offline."""
    from src.api.routers import status as status_router
    monkeypatch.setattr(status_router, "_probe_sidecar_blocking", lambda: True)

    async def fake_drift():
        return {"checked_at_utc": "t", "in_sync": True, "diff_count": 0, "error": None}
    monkeypatch.setattr(status_router, "_fox_drift_block", fake_drift)
    monkeypatch.setattr(status_router, "_quota_block", lambda: {"fox": None, "daikin": None})


# ── /status/alerts ───────────────────────────────────────────────────────────

def test_alerts_viewer_readable_and_shaped(monkeypatch):
    _quiet_upstreams(monkeypatch)
    client = _client(monkeypatch)
    r = client.get("/api/v1/status/alerts")
    assert r.status_code == 200
    body = r.json()
    for k in ("now_utc", "meter", "lp", "forecast", "fox_drift", "quota"):
        assert k in body, k
    # Empty DB: no metered day yet → stale, no failures, no forecast snapshot.
    assert body["meter"]["stale"] is True
    assert body["lp"]["failures_24h"] == 0
    assert body["forecast"]["degraded"] is True


def test_alerts_meter_freshness_math(monkeypatch):
    _quiet_upstreams(monkeypatch)
    fresh = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    db.upsert_octopus_daily_meter(fresh, import_kwh=9.0, export_kwh=1.0)
    client = _client(monkeypatch)
    body = client.get("/api/v1/status/alerts").json()
    assert body["meter"]["last_day"] == fresh
    assert body["meter"]["stale"] is False


def test_alerts_lp_failures_never_leak_stacktrace(monkeypatch):
    _quiet_upstreams(monkeypatch)
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO lp_failure_log (run_at_utc, plan_date, error_class,
                                           error_msg, stacktrace)
               VALUES (?, ?, ?, ?, ?)""",
            (datetime.now(UTC).isoformat(), "2026-06-12", "Infeasible",
             "secret detail", "Traceback (most recent call last): SECRET"),
        )
        conn.commit()
    finally:
        conn.close()
    client = _client(monkeypatch)
    body = client.get("/api/v1/status/alerts").json()
    assert body["lp"]["failures_24h"] == 1
    assert body["lp"]["last_failure"]["error_class"] == "Infeasible"
    blob = client.get("/api/v1/status/alerts").text
    assert "SECRET" not in blob and "secret detail" not in blob


def test_alerts_forecast_provenance_healthy(monkeypatch):
    _quiet_upstreams(monkeypatch)
    now_iso = datetime.now(UTC).isoformat()
    # NB: save_meteo_forecast_snapshot no-ops on an empty rows list.
    db.save_meteo_forecast_snapshot(
        now_iso,
        [{"slot_time": now_iso, "temp_c": 15.0, "solar_w_m2": 100.0,
          "cloud_cover_pct": 40.0, "direct_pv_kw": 0.5}],
        source="quartz", model_name="quartz-open-site", mark_latest=True,
    )
    client = _client(monkeypatch)
    body = client.get("/api/v1/status/alerts").json()
    assert body["forecast"]["model_name"] == "quartz-open-site"
    assert body["forecast"]["degraded"] is False


def test_alerts_ttl_serves_cached(monkeypatch):
    _quiet_upstreams(monkeypatch)
    client = _client(monkeypatch)
    first = client.get("/api/v1/status/alerts").json()
    # A meter row landing AFTER the first call must not appear within the TTL.
    db.upsert_octopus_daily_meter(
        datetime.now(UTC).date().isoformat(), import_kwh=9.0, export_kwh=1.0
    )
    second = client.get("/api/v1/status/alerts").json()
    assert second == first


def test_fox_drift_subcache_blocks_repeat_live_reads(monkeypatch):
    from src.api.routers import status as status_router
    calls = {"n": 0}

    async def fake_diff():
        calls["n"] += 1
        # The REAL endpoint contract (review HIGH on #553: a fake with a
        # nonexistent "differences" key masked the wrong-key bug).
        return {"any_drift": True,
                "diffs": {"only_live": [{"g": 1}], "only_recorded": []},
                "live_error": None}

    import src.api.routers.dispatch as dispatch_router
    monkeypatch.setattr(dispatch_router, "get_foxess_schedule_diff", fake_diff)
    import asyncio
    first = asyncio.run(status_router._fox_drift_block())
    asyncio.run(status_router._fox_drift_block())
    assert calls["n"] == 1, "second call within TTL must hit the sub-cache"
    # And the real-contract keys flow through: drift visible with a count.
    assert first["in_sync"] is False
    assert first["diff_count"] == 1


# ── /status/feedback ─────────────────────────────────────────────────────────

def test_feedback_shape_and_gate_story(monkeypatch):
    _quiet_upstreams(monkeypatch)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    client = _client(monkeypatch)
    r = client.get("/api/v1/status/feedback")
    assert r.status_code == 200
    body = r.json()
    dhw = body["dhw"]
    assert dhw["mode"] == "normal"
    assert dhw["nominal_kwh"] == pytest.approx(3.0, abs=0.05)
    assert dhw["effective_budget_kwh"] == pytest.approx(
        dhw["nominal_kwh"] * dhw["autoscale_factor"], abs=0.02
    )
    gate = body["lwt_gate"]
    # Empty DB → no measured heating → gate suppresses pre-heat.
    assert gate["demand_present"] is False
    assert gate["preheat_suppressed"] is True
    assert gate["threshold_kwh"] == pytest.approx(0.5)
    assert "forecast" in body


# ── fair-compare TTL cache ───────────────────────────────────────────────────

def test_fair_compare_cached_within_ttl(monkeypatch):
    import src.api.main as api_main
    api_main._fair_compare_cache.clear()
    calls = {"n": 0}

    def fake_compute(start, end, max_tariffs=14):
        calls["n"] += 1
        return {
            "period_start": str(start), "period_end": str(end),
            "n_days": 1, "days_with_data": 1, "clamped": False,
            "catalogue_unavailable": False, "winner_product_code": "x",
            "current_product_code": "x",
            "savings_vs_current_pounds": 0.0,
            "basis": {"import_kwh": 1.0, "export_kwh": 0.0},
            "tariffs": [], "export": None,
        }

    import src.analytics.fair_compare as fc
    monkeypatch.setattr(fc, "compute_fair_comparison", fake_compute)
    client = _client(monkeypatch)
    r1 = client.get("/api/v1/tariffs/fair-compare?period=day&anchor=2026-06-10")
    r2 = client.get("/api/v1/tariffs/fair-compare?period=day&anchor=2026-06-10")
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == 1, "second hit must come from the TTL cache"
    r3 = client.get("/api/v1/tariffs/fair-compare?period=day&anchor=2026-06-09")
    assert r3.status_code == 200
    assert calls["n"] == 2, "different anchor = different key"
