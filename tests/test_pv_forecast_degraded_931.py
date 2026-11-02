"""#931 - an empty / implausible PV forecast must never be planned as PV = 0 silently.

Fallback chain: last good persisted fetch (< PV_FORECAST_LASTGOOD_MAX_AGE_HOURS)
-> Open-Meteo physics -> none. Every fallback logs WARNING, is recorded in
``exogenous_snapshot["pv_source"]`` and sends ONE ``notify_risk`` per UTC day.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from src import db, weather
from src.config import config
from src.weather import ForecastFetchResult, HourlyForecast, compute_heating_demand_factor


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()
    monkeypatch.setattr(config, "WEATHER_LAT", "51.4927", raising=False)
    monkeypatch.setattr(config, "WEATHER_LON", "-0.2628", raising=False)
    monkeypatch.setattr(config, "FORECAST_SOURCE", "quartz", raising=False)
    monkeypatch.setattr(config, "QUARTZ_PROVIDER", "open", raising=False)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    config.PV_FORECAST_LASTGOOD_ENABLED = True
    config.PV_FORECAST_LASTGOOD_MAX_AGE_HOURS = 6.0
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, extra=None: sent.append((msg, extra or {})))
    return sent


@pytest.fixture
def sent(_env):
    return _env


def _hf(t: datetime, rad: float, kw: float = 0.0, direct: bool = False) -> HourlyForecast:
    return HourlyForecast(
        time_utc=t, temperature_c=10.0, cloud_cover_pct=30.0,
        shortwave_radiation_wm2=rad, estimated_pv_kw=kw,
        heating_demand_factor=compute_heating_demand_factor(10.0), pv_direct=direct,
    )


def _now_floor() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def _seed_snapshot(age_h: float, source: str = "quartz", kw: float = 2.0, n: int = 10) -> str:
    fetch_at = (datetime.now(UTC) - timedelta(hours=age_h)).isoformat()
    t0 = _now_floor()
    rows = [
        {"slot_time": (t0 + timedelta(minutes=30 * i)).isoformat(), "temp_c": 9.0,
         "solar_w_m2": 400.0, "cloud_cover_pct": 20.0,
         "direct_pv_kw": kw if source == "quartz" else None}
        for i in range(n)
    ]
    db.save_meteo_forecast_snapshot(fetch_at, rows, source=source, mark_latest=True)
    return fetch_at


def _patch_providers(monkeypatch, *, base, quartz=None):
    monkeypatch.setattr(weather, "_fetch_open_meteo_forecast", lambda **k: list(base))
    monkeypatch.setattr(
        weather, "_fetch_quartz_open_forecast",
        lambda **k: quartz if quartz is not None else ForecastFetchResult(forecast=[], source="quartz-open"),
    )


# --- fallback chain ---------------------------------------------------------
def test_quartz_empty_reuses_last_good_and_alerts(monkeypatch, sent):
    fetch_at = _seed_snapshot(age_h=1.0)
    _patch_providers(monkeypatch, base=[])
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "lastgood" and res.fallback_reason == "quartz_empty"
    assert res.source == "quartz-lastgood" and res.persist is False
    assert res.reused_fetch_at_utc == fetch_at
    assert res.forecast and all(f.pv_direct for f in res.forecast)
    assert max(f.estimated_pv_kw for f in res.forecast) == pytest.approx(2.0)
    info = res.pv_source_info()
    assert info["status"] == "lastgood" and info["reused_age_min"] == pytest.approx(60.0, abs=2.0)
    assert len(sent) == 1 and "degradada" in sent[0][0]


def test_stale_last_good_falls_to_open_meteo(monkeypatch, sent):
    _seed_snapshot(age_h=7.0)  # older than the 6 h limit
    base = [_hf(_now_floor() + timedelta(hours=i), 300.0, 1.0) for i in range(4)]
    _patch_providers(monkeypatch, base=base)
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "open_meteo_fallback" and res.source == "open-meteo"
    assert res.persist is True and len(res.forecast) == 4
    assert len(sent) == 1


def test_open_meteo_snapshots_are_not_used_as_quartz_last_good(monkeypatch, sent):
    _seed_snapshot(age_h=1.0, source="open-meteo")
    _patch_providers(monkeypatch, base=[])
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "none" and res.forecast == []
    assert len(sent) == 1 and "SEM previsao" in sent[0][0]


def test_everything_empty_is_flagged_not_silent(monkeypatch, sent):
    _patch_providers(monkeypatch, base=[])
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "none" and res.pv_source_info()["n_rows"] == 0
    assert len(sent) == 1


def test_last_good_tail_filled_from_open_meteo(monkeypatch, sent):
    _seed_snapshot(age_h=1.0, n=4)  # covers only ~2 h
    far = _now_floor() + timedelta(hours=20)
    base = [_hf(far, 250.0, 0.9), _hf(far + timedelta(hours=1), 250.0, 0.9)]
    _patch_providers(monkeypatch, base=base)
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "lastgood"
    assert [f.pv_direct for f in res.forecast][-2:] == [False, False]
    assert res.forecast[-1].time_utc == far + timedelta(hours=1)


def test_alert_fires_once_per_day(monkeypatch, sent):
    _seed_snapshot(age_h=1.0)
    _patch_providers(monkeypatch, base=[])
    weather.fetch_forecast_snapshot(hours=48)
    weather.fetch_forecast_snapshot(hours=48)
    weather.fetch_forecast_snapshot(hours=48)
    assert len(sent) == 1
    # next UTC day -> one more
    db.set_kv("pv_forecast_degraded_alert_day", (datetime.now(UTC) - timedelta(days=1)).date().isoformat())
    weather.fetch_forecast_snapshot(hours=48)
    assert len(sent) == 2


def test_healthy_quartz_has_no_alert_and_persists(monkeypatch, sent):
    t0 = _now_floor()
    base = [_hf(t0 + timedelta(hours=i), 300.0) for i in range(8)]
    q = [_hf(t0 + timedelta(minutes=30 * i), 300.0, 1.5, True) for i in range(16)]
    _patch_providers(monkeypatch, base=base, quartz=ForecastFetchResult(forecast=q, source="quartz"))
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "ok" and res.source == "quartz" and res.persist is True
    assert sent == []


def test_quartz_zero_in_daylight_is_treated_as_empty(monkeypatch, sent):
    _seed_snapshot(age_h=1.0)
    t0 = _now_floor()
    base = [_hf(t0 + timedelta(hours=i), 400.0) for i in range(8)]       # real daylight
    q = [_hf(t0 + timedelta(minutes=30 * i), 400.0, 0.0, True) for i in range(16)]  # all-zero
    _patch_providers(monkeypatch, base=base, quartz=ForecastFetchResult(forecast=q, source="quartz"))
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "lastgood" and res.fallback_reason == "quartz_zero_in_daylight"


def test_legitimately_dark_day_is_not_flagged(monkeypatch, sent):
    """Small PV is not an outage: Open-Meteo irradiance is low too."""
    t0 = _now_floor()
    base = [_hf(t0 + timedelta(hours=i), 30.0) for i in range(8)]
    q = [_hf(t0 + timedelta(minutes=30 * i), 30.0, 0.0, True) for i in range(16)]
    _patch_providers(monkeypatch, base=base, quartz=ForecastFetchResult(forecast=q, source="quartz"))
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "ok" and sent == []


def test_kill_switch_restores_legacy_behaviour(monkeypatch, sent):
    config.PV_FORECAST_LASTGOOD_ENABLED = False
    _seed_snapshot(age_h=1.0)
    base = [_hf(_now_floor() + timedelta(hours=i), 300.0, 1.0) for i in range(4)]
    _patch_providers(monkeypatch, base=base)
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.source == "open-meteo" and res.status == "ok" and sent == []


def test_open_meteo_only_source_uses_last_good(monkeypatch, sent):
    monkeypatch.setattr(config, "FORECAST_SOURCE", "open_meteo", raising=False)
    _seed_snapshot(age_h=2.0, source="open-meteo")
    monkeypatch.setattr(weather, "_fetch_open_meteo_forecast", lambda **k: [])
    res = weather.fetch_forecast_snapshot(hours=48)
    assert res.status == "lastgood" and res.fallback_reason == "open_meteo_empty"
    assert len(sent) == 1


# --- wire: what solve_lp actually receives ----------------------------------
def _wire_seed(age_h: float | None):
    """Last-good rows covering the test horizon (2026-05-20 18:30 + 48 h)."""
    if age_h is None:
        return
    fetch_at = (datetime.now(UTC) - timedelta(hours=age_h)).isoformat()
    t = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    rows = []
    for i in range(110):
        st = t + timedelta(minutes=30 * i)
        day = 7 <= st.hour <= 16
        rows.append({"slot_time": st.isoformat(), "temp_c": 11.0,
                     "solar_w_m2": 500.0 if day else 0.0, "cloud_cover_pct": 20.0,
                     "direct_pv_kw": 2.5 if day else 0.0})
    db.save_meteo_forecast_snapshot(fetch_at, rows, source="quartz", mark_latest=True)


def _run_wire(monkeypatch, age_h):
    from test_initial_state_roll_forward_929 import _setup_wire
    from test_lp_appliance_real_solver import _stub_optimal_lp_plan

    from src.scheduler import optimizer

    _setup_wire(monkeypatch)
    _patch_providers(monkeypatch, base=[])
    _wire_seed(age_h)
    seen: dict = {}

    def _capture(*, weather, **kwargs):
        seen["pv"] = list(weather.pv_kwh_per_slot)
        return _stub_optimal_lp_plan(**kwargs)

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _capture)
    optimizer.run_optimizer(fox=None, daikin=None)
    return seen


def _last_exogenous() -> dict:
    conn = db.get_connection()
    try:
        r = conn.execute(
            "SELECT exogenous_snapshot_json, forecast_fetch_at_utc FROM lp_inputs_snapshot "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return json.loads(r[0]), r[1]


def test_wire_solve_lp_gets_last_good_pv_when_source_empty(monkeypatch, sent):
    seen = _run_wire(monkeypatch, age_h=1.0)
    assert max(seen["pv"]) > 0.0, "solve_lp planned on PV = 0 although a good forecast existed"
    exo, fetch_at = _last_exogenous()
    assert exo["pv_source"]["status"] == "lastgood"
    assert exo["pv_source"]["fallback_reason"] == "quartz_empty"
    assert exo["pv_source"]["source"] == "quartz-lastgood"
    assert exo["pv_source"]["reused_fetch_at_utc"] == fetch_at  # replay resolves the real fetch
    assert len(sent) == 1
    # not re-persisted as a fresh fetch
    conn = db.get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) FROM meteo_forecast_snapshot").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_wire_no_source_at_all_is_recorded_and_alerted(monkeypatch, sent):
    seen = _run_wire(monkeypatch, age_h=None)
    assert max(seen["pv"]) == 0.0
    exo, _ = _last_exogenous()
    assert exo["pv_source"]["status"] == "none"
    assert len(sent) == 1


# --- /pv/today: forward-only live fetch must not blank the day ---------------
def test_pv_today_uses_stored_forecast_after_sunset(monkeypatch):
    import asyncio

    from src.api.routers import pv as pv_router

    monkeypatch.setattr(weather, "fetch_forecast", lambda hours=48: [])  # live fetch empty
    d = datetime.now(UTC).date()
    day0 = datetime(d.year, d.month, d.day, tzinfo=UTC)
    rows = []
    for i in range(48):
        st = day0 + timedelta(minutes=30 * i)
        up = 9 <= st.hour < 15
        rows.append({"slot_time": st.isoformat(), "temp_c": 10.0,
                     "solar_w_m2": 400.0 if up else 0.0, "cloud_cover_pct": 20.0,
                     "direct_pv_kw": 2.0 if up else 0.0})
    db.save_meteo_forecast_snapshot((datetime.now(UTC) - timedelta(hours=1)).isoformat(), rows, source="quartz")
    resp = asyncio.run(pv_router.get_pv_today(date=d.isoformat()))
    assert resp["forecast_kwh_day_total"] > 0.0
    assert any(s["pv_forecast_kwh"] > 0.0 for s in resp["slots"])
