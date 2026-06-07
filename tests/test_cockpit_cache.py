"""Phase 1 cockpit performance: in-process TTL caches + Cache-Control headers.

The cockpit was slow because /weather + /pv/today hit Open-Meteo and /energy/period
(day/week) hit Fox ESS on every request with no server-side cache. These tests
lock in the caching + headers (no Redis — single-container in-process TTL).
"""
from __future__ import annotations

import src.weather as weather
from src.config import config


def test_fetch_forecast_cached_dedupes_within_ttl(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(config, "WEATHER_FORECAST_CACHE_TTL_SECONDS", 900, raising=False)

    def _fake(**_k):
        calls["n"] += 1
        return [object()]  # non-empty

    monkeypatch.setattr(weather, "fetch_forecast", _fake)
    weather._forecast_cache.clear()
    a = weather.fetch_forecast_cached(hours=48)
    b = weather.fetch_forecast_cached(hours=48)
    c = weather.fetch_forecast_cached(hours=48)
    assert calls["n"] == 1, "three cached calls must hit Open-Meteo once"
    assert a is b is c


def test_fetch_forecast_cached_serves_stale_on_failed_refresh(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_FORECAST_CACHE_TTL_SECONDS", 0, raising=False)
    # TTL=0 disables caching → always passes through.
    seq = [[object()], []]  # first good, then empty (failed fetch)

    def _fake(**_k):
        return seq.pop(0) if seq else []

    monkeypatch.setattr(weather, "fetch_forecast", _fake)
    weather._forecast_cache.clear()
    assert weather.fetch_forecast_cached(hours=48)  # passthrough, good
    # Re-enable cache; prime it, then make the next fetch fail → stale served.
    monkeypatch.setattr(config, "WEATHER_FORECAST_CACHE_TTL_SECONDS", 1, raising=False)
    good = [object()]
    state = {"fail": False}
    monkeypatch.setattr(weather, "fetch_forecast", lambda **_k: ([] if state["fail"] else good))
    weather._forecast_cache.clear()
    primed = weather.fetch_forecast_cached(hours=48)
    assert primed == good
    state["fail"] = True
    import time
    time.sleep(1.05)  # expire the 1s TTL
    served = weather.fetch_forecast_cached(hours=48)
    assert served == good, "a failed refresh must serve the last good value, not []"


def test_period_insights_cache_dedupes_day(monkeypatch):
    import src.api.main as m
    monkeypatch.setattr(config, "ENERGY_PERIOD_CACHE_TTL_SECONDS", 1200, raising=False)
    calls = {"n": 0}
    monkeypatch.setattr(m, "get_period_insights",
                        lambda period, **k: (calls.__setitem__("n", calls["n"] + 1) or {"period": period}))
    m._period_insights_cache.clear()
    m._get_period_insights_cached("day", date_str="2026-06-07")
    m._get_period_insights_cached("day", date_str="2026-06-07")
    assert calls["n"] == 1  # cached
    # A different date is a different key → recomputed.
    m._get_period_insights_cached("day", date_str="2026-06-06")
    assert calls["n"] == 2


def test_period_insights_cache_passthrough_for_month(monkeypatch):
    import src.api.main as m
    monkeypatch.setattr(config, "ENERGY_PERIOD_CACHE_TTL_SECONDS", 1200, raising=False)
    calls = {"n": 0}
    monkeypatch.setattr(m, "get_period_insights",
                        lambda period, **k: (calls.__setitem__("n", calls["n"] + 1) or {"period": period}))
    m._period_insights_cache.clear()
    # month self-caches downstream; the day/week wrapper must NOT cache it.
    m._get_period_insights_cached("month", month_str="2026-06")
    m._get_period_insights_cached("month", month_str="2026-06")
    assert calls["n"] == 2


def test_cache_control_header_on_weather(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.main import app
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    monkeypatch.setattr(weather, "fetch_forecast", lambda **_k: [])  # no external call
    weather._forecast_cache.clear()
    client = TestClient(app)
    r = client.get("/api/v1/weather")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "private, max-age=900"
