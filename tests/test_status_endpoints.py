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


def _insert_coherence(ts_iso: str, params: dict) -> None:
    """action_log always stamps NOW(), so insert directly when the test needs
    a controlled timestamp (mirrors the lp_failure_log test above)."""
    import json as _json
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO action_log (timestamp, device, action, params, result, trigger)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ts_iso, "scheduler", "plan_dispatch_coherence", _json.dumps(params), "success", "cron"),
        )
        conn.commit()
    finally:
        conn.close()


def test_alerts_coherence_quiet_when_no_audit(monkeypatch):
    _quiet_upstreams(monkeypatch)
    client = _client(monkeypatch)
    body = client.get("/api/v1/status/alerts").json()
    assert "coherence" in body
    assert body["coherence"]["severe_count"] == 0
    assert body["coherence"]["severe"] == []
    assert body["coherence"]["result"] is None


def test_alerts_coherence_surfaces_latest_severe(monkeypatch):
    _quiet_upstreams(monkeypatch)
    now = datetime.now(UTC)
    # An older clean audit and a newer severe one — the block reflects the
    # newest event only.
    _insert_coherence(
        (now - timedelta(hours=6)).isoformat(),
        {"total_slots": 48, "matched": 48, "mismatched": 0,
         "severe_count": 0, "severe": [], "result": "ok"},
    )
    _insert_coherence(
        (now - timedelta(minutes=30)).isoformat(),
        {"total_slots": 48, "matched": 46, "mismatched": 2,
         "severe_count": 2, "result": "severe_divergence",
         "severe": [
             {"slot_time_utc": "2026-07-10T18:00:00Z", "planned": "hold", "actual": "selfuse"},
             {"slot_time_utc": "2026-07-10T18:30:00Z", "planned": "hold", "actual": "absent"},
         ]},
    )
    client = _client(monkeypatch)
    coh = client.get("/api/v1/status/alerts").json()["coherence"]
    assert coh["severe_count"] == 2
    assert coh["result"] == "severe_divergence"
    assert len(coh["severe"]) == 2
    assert coh["severe"][0]["planned"] == "hold"


def test_alerts_coherence_ignores_events_older_than_24h(monkeypatch):
    _quiet_upstreams(monkeypatch)
    _insert_coherence(
        (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
        {"severe_count": 3, "result": "severe_divergence", "severe": []},
    )
    client = _client(monkeypatch)
    coh = client.get("/api/v1/status/alerts").json()["coherence"]
    assert coh["severe_count"] == 0


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
    # Observational DHW warmup shadow is present (null until a shadow row exists).
    assert "dhw_warmup_shadow" in body
    assert body["dhw_warmup_shadow"] is None


def test_feedback_surfaces_warmup_shadow(monkeypatch):
    """A persisted observational would-pick row is surfaced on /status/feedback,
    with today's row preferred and D+1 as the fallback."""
    _quiet_upstreams(monkeypatch)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    from zoneinfo import ZoneInfo

    from src import dhw_policy
    today = datetime.now(ZoneInfo("Europe/London")).date()
    tomorrow = today + timedelta(days=1)
    # Only a D+1 row exists → the endpoint falls back to it.
    dhw_policy._persist_warmup_shadow(tomorrow, static_hour=13, chosen_hour=14, delta_pence=0.37)
    client = _client(monkeypatch)
    ws = client.get("/api/v1/status/feedback").json()["dhw_warmup_shadow"]
    assert ws is not None
    assert ws["static_hour"] == 13
    assert ws["would_pick_hour"] == 14
    assert ws["delta_pence"] == pytest.approx(0.37)
    assert ws["enabled"] is False
    # Today's row now lands → it takes precedence over the D+1 fallback.
    from src.api.routers import status as status_router
    status_router._cache.clear()
    dhw_policy._persist_warmup_shadow(today, static_hour=13, chosen_hour=12, delta_pence=1.1)
    ws2 = client.get("/api/v1/status/feedback").json()["dhw_warmup_shadow"]
    assert ws2["would_pick_hour"] == 12


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
