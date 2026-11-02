"""#931 - an empty / implausible PV forecast must never be planned as PV = 0 silently.

Fallback chain: last good persisted fetch (< PV_FORECAST_LASTGOOD_MAX_AGE_HOURS,
itself plausible) -> Open-Meteo physics -> none. Beyond the coverage of a reused
fetch PV is ZERO (never held flat). Every fallback is WARNING-logged on the
optimizer path and written to ``exogenous_snapshot["pv_source"]``; only status
``none`` pushes (own route, once per UTC day).

ONE clock is frozen per test (``weather._now_utc``, and the optimizer's for the
wire tests) so the last-good age and the solve agree.
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
from src.weather import ForecastFetchResult, HourlyForecast, compute_heating_demand_factor, estimate_pv_kw

NOW = datetime(2026, 11, 2, 14, 20, tzinfo=UTC)   # the prod incident: 14:20 London (GMT) boot solve

# Open-Meteo-like shortwave W/m2 by UTC hour for a typical early-Nov day.
_RAD = {7: 5, 8: 60, 9: 150, 10: 240, 11: 300, 12: 320, 13: 280, 14: 190, 15: 80, 16: 10}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()
    monkeypatch.setattr(config, "WEATHER_LAT", "51.4927", raising=False)
    monkeypatch.setattr(config, "WEATHER_LON", "-0.2628", raising=False)
    monkeypatch.setattr(config, "FORECAST_SOURCE", "quartz", raising=False)
    monkeypatch.setattr(config, "QUARTZ_PROVIDER", "open", raising=False)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(weather, "_now_utc", lambda: NOW)
    config.PV_FORECAST_LASTGOOD_ENABLED = True
    config.PV_FORECAST_LASTGOOD_MAX_AGE_HOURS = 6.0
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.notifier.notify_pv_forecast_degraded",
        lambda msg, extra=None: sent.append((msg, extra or {})),
    )
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


def _base_profile(now: datetime, hours: int = 48) -> list[HourlyForecast]:
    """Hourly Open-Meteo-style rows from the next full hour (it drops the past)."""
    t = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    out = []
    for _ in range(hours):
        rad = float(_RAD.get(t.hour, 0))
        out.append(_hf(t, rad, estimate_pv_kw(rad)))
        t += timedelta(hours=1)
    return out


def _quartz_rows(now: datetime, scale: float, hours: int = 48) -> list[HourlyForecast]:
    t = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    out = []
    for _ in range(hours * 2):
        rad = float(_RAD.get(t.hour, 0))
        out.append(_hf(t, rad, scale * estimate_pv_kw(rad), True))
        t += timedelta(minutes=30)
    return out


def _seed_snapshot(
    age_h: float, source: str = "quartz", kw: float = 2.0, n: int = 10,
    start: datetime | None = None, now: datetime = NOW,
) -> str:
    fetch_at = (now - timedelta(hours=age_h)).isoformat()
    t0 = start or now.replace(minute=0, second=0, microsecond=0)
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


# --- detector: whole-horizon, elevation-gated --------------------------------
def test_detector_flags_all_zero_quartz_on_the_2_nov_afternoon_profile():
    """The old 6 h / >=2 hourly rows >= 150 W/m2 gate could not fire here: only the
    15:00 row (80 W/m2) is left today. The horizon-wide check sees tomorrow."""
    base = _base_profile(NOW)
    assert sum(1 for b in base if b.time_utc <= NOW + timedelta(hours=6) and b.shortwave_radiation_wm2 >= 150) == 0
    zeros = _quartz_rows(NOW, 0.0)
    assert weather._source_pv_implausible(zeros, base, now=NOW) is True


def test_detector_flags_all_zero_quartz_overnight_for_tomorrow():
    now = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
    assert weather._source_pv_implausible(_quartz_rows(now, 0.0), _base_profile(now), now=now) is True


def test_detector_accepts_healthy_and_small_but_real_quartz():
    base = _base_profile(NOW)
    assert weather._source_pv_implausible(_quartz_rows(NOW, 0.6), base, now=NOW) is False
    # a poor day: 15 % of physics is "small PV", not an outage
    assert weather._source_pv_implausible(_quartz_rows(NOW, 0.15), base, now=NOW) is False


def test_detector_ignores_a_dark_day_and_night():
    """Physics itself predicts ~nothing: zero Quartz is legitimate."""
    t = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    dark = [_hf(t + timedelta(hours=i), 8.0, estimate_pv_kw(8.0)) for i in range(48)]
    assert weather._source_pv_implausible(_quartz_rows(NOW, 0.0), dark, now=NOW) is False


def test_detector_needs_a_physics_baseline():
    assert weather._source_pv_implausible(_quartz_rows(NOW, 0.0), [], now=NOW) is False


# --- fallback chain -----------------------------------------------------------
def test_quartz_empty_reuses_last_good_without_push(monkeypatch, sent):
    fetch_at = _seed_snapshot(age_h=1.0)
    _patch_providers(monkeypatch, base=[])
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "lastgood" and res.fallback_reason == "quartz_empty"
    assert res.source == "quartz-lastgood" and res.persist is False
    assert res.reused_fetch_at_utc == fetch_at
    assert max(f.estimated_pv_kw for f in res.forecast) == pytest.approx(2.0)
    info = res.pv_source_info()
    assert info["status"] == "lastgood" and info["reused_age_min"] == pytest.approx(60.0, abs=0.1)
    assert sent == []          # pull-only: lastgood never pushes


def test_all_zero_quartz_in_daylight_falls_to_last_good(monkeypatch, sent):
    _seed_snapshot(age_h=1.0, start=NOW - timedelta(hours=1), n=40)
    _patch_providers(
        monkeypatch, base=_base_profile(NOW),
        quartz=ForecastFetchResult(forecast=_quartz_rows(NOW, 0.0), source="quartz"),
    )
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "lastgood" and res.fallback_reason == "quartz_implausible_vs_physics"


def test_stale_last_good_falls_to_open_meteo_without_push(monkeypatch, sent):
    _seed_snapshot(age_h=7.0)  # older than the 6 h limit
    base = _base_profile(NOW, 4)
    _patch_providers(monkeypatch, base=base)
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "open_meteo_fallback" and res.source == "open-meteo"
    assert res.persist is True and len(res.forecast) == 4
    assert sent == []


def test_implausible_last_good_candidate_is_skipped(monkeypatch, sent):
    t0 = NOW - timedelta(hours=1)
    older = _seed_snapshot(age_h=3.0, kw=2.0, start=t0, n=80)
    _seed_snapshot(age_h=1.0, kw=0.0, start=t0, n=80)     # newest, but ~0 in daylight
    _patch_providers(monkeypatch, base=[])
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "lastgood" and res.reused_fetch_at_utc == older


def test_open_meteo_snapshots_are_not_used_as_quartz_last_good(monkeypatch, sent):
    _seed_snapshot(age_h=1.0, source="open-meteo")
    _patch_providers(monkeypatch, base=[])
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "none" and res.forecast == []


def test_none_pushes_once_per_utc_day_on_the_optimizer_path_only(monkeypatch, sent):
    _patch_providers(monkeypatch, base=[])
    weather.fetch_forecast_snapshot(hours=48)              # appliance/sim/MCP/heartbeat style
    assert sent == []
    weather.fetch_forecast_snapshot(hours=48, report=True)
    weather.fetch_forecast_snapshot(hours=48, report=True)
    assert len(sent) == 1 and "Sem previsao solar" in sent[0][0]
    assert db.get_kv("pv_forecast_none_alert_day") == "2026-11-02"
    monkeypatch.setattr(weather, "_now_utc", lambda: NOW + timedelta(days=1))
    weather.fetch_forecast_snapshot(hours=48, report=True)
    assert len(sent) == 2


def test_failed_dispatch_does_not_burn_the_daily_key(monkeypatch, sent):
    def _boom(msg, extra=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("src.notifier.notify_pv_forecast_degraded", _boom)
    _patch_providers(monkeypatch, base=[])
    weather.fetch_forecast_snapshot(hours=48, report=True)   # swallowed
    assert db.get_kv("pv_forecast_none_alert_day") is None


def test_healthy_quartz_has_no_alert_and_persists(monkeypatch, sent):
    _patch_providers(
        monkeypatch, base=_base_profile(NOW),
        quartz=ForecastFetchResult(forecast=_quartz_rows(NOW, 0.6), source="quartz"),
    )
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "ok" and res.source == "quartz" and res.persist is True
    assert sent == []


def test_kill_switch_skips_last_good_and_never_reports_ok(monkeypatch, sent):
    config.PV_FORECAST_LASTGOOD_ENABLED = False
    _seed_snapshot(age_h=1.0)
    base = _base_profile(NOW, 4)
    _patch_providers(monkeypatch, base=base)
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.source == "open-meteo" and res.status == "open_meteo_fallback"
    _patch_providers(monkeypatch, base=[])
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "none"                       # empty is never "ok"


def test_open_meteo_only_source_uses_last_good(monkeypatch, sent):
    monkeypatch.setattr(config, "FORECAST_SOURCE", "open_meteo", raising=False)
    fetch_at = _seed_snapshot(age_h=2.0, source="open-meteo")
    monkeypatch.setattr(weather, "_fetch_open_meteo_forecast", lambda **k: [])
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.fallback_reason == "open_meteo_empty"
    assert res.status == "lastgood" and res.reused_fetch_at_utc == fetch_at
    assert sent == []


# --- horizon end: ZERO beyond coverage, never held flat -------------------------
def test_beyond_last_good_coverage_pv_is_zero_not_flat(monkeypatch, sent):
    now = datetime(2026, 11, 3, 10, 5, tzinfo=UTC)
    monkeypatch.setattr(weather, "_now_utc", lambda: now)
    # fetch at 10:05 covering 10:00 .. 10:00 next day; the LAST row is a daylight one
    start = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    _seed_snapshot(age_h=0.1, kw=2.0, start=start, n=49, now=now)
    _patch_providers(monkeypatch, base=[])
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "lastgood"
    f = res.forecast
    end_cov = start + timedelta(hours=24)
    for dt_h in (1, 6, 12, 20):
        v = weather._interp_hourly_scalar(f, end_cov + timedelta(hours=dt_h), "estimated_pv_kw", -1.0)
        assert v == 0.0, f"+{dt_h}h beyond coverage held {v} kW (flat hold)"
    assert weather._interp_hourly_scalar(f, end_cov - timedelta(minutes=30), "estimated_pv_kw", -1.0) == pytest.approx(2.0)
    assert f[-1].time_utc == now + timedelta(hours=48)


def test_last_good_tail_filled_from_open_meteo_then_zero(monkeypatch, sent):
    _seed_snapshot(age_h=1.0, n=4)  # covers only ~2 h
    far = NOW.replace(minute=0) + timedelta(hours=20)
    base = [_hf(far, 250.0, 0.9), _hf(far + timedelta(hours=1), 250.0, 0.9)]
    _patch_providers(monkeypatch, base=base)
    res = weather.fetch_forecast_snapshot(hours=48, report=True)
    assert res.status == "lastgood"
    assert [f.pv_direct for f in res.forecast if f.time_utc in (far, far + timedelta(hours=1))] == [False, False]
    assert weather._interp_hourly_scalar(res.forecast, far + timedelta(hours=10), "estimated_pv_kw", -1.0) == 0.0


# --- /pv/today and the range query ---------------------------------------------
def test_range_query_is_latest_per_slot_and_day_bounded():
    d0 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)

    def _snap(fetch_at, slots, kw):
        db.save_meteo_forecast_snapshot(
            fetch_at.isoformat(),
            [{"slot_time": s.isoformat(), "temp_c": 9.0, "solar_w_m2": 1.0,
              "cloud_cover_pct": 1.0, "direct_pv_kw": kw} for s in slots],
            source="quartz",
        )

    day = [d0 + timedelta(minutes=30 * i) for i in range(48)]
    nxt = [d0 + timedelta(days=1, minutes=30 * i) for i in range(48)]
    _snap(NOW - timedelta(hours=5), day + nxt, 1.0)
    _snap(NOW - timedelta(hours=1), day[28:] + nxt, 2.0)      # revises later slots only
    rows = db.get_meteo_forecast_for_slot_range("2026-11-02", "2026-11-03")
    assert len(rows) == 48 and all(r["slot_time"].startswith("2026-11-02") for r in rows)
    by = {r["slot_time"]: r["direct_pv_kw"] for r in rows}
    assert by[day[0].isoformat()] == 1.0 and by[day[40].isoformat()] == 2.0
    two = db.get_meteo_forecast_for_slot_range("2026-11-02", "2026-11-04")
    assert len(two) == 96


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


# --- wire: what solve_lp actually receives ----------------------------------------
def _seed_wire_rows(now: datetime, *, age_h: float, until: datetime | None = None):
    """Last-good rows from 2026-05-20 18:00 (daylight direct kW 2.5 for 6<=h<=18 UTC)."""
    fetch_at = (now - timedelta(hours=age_h)).isoformat()
    t = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    end = until or (t + timedelta(hours=55))
    rows = []
    while t <= end:
        day = 6 <= t.hour <= 18
        rows.append({"slot_time": t.isoformat(), "temp_c": 11.0,
                     "solar_w_m2": 500.0 if day else 0.0, "cloud_cover_pct": 20.0,
                     "direct_pv_kw": 2.5 if day else 0.0})
        t += timedelta(minutes=30)
    db.save_meteo_forecast_snapshot(fetch_at, rows, source="quartz", mark_latest=True)
    return fetch_at


def _run_wire(monkeypatch, *, seed, quartz_rows=None, pushed=None):
    """Run the real optimizer with ``solve_lp`` stubbed; returns (nominal_pv, all_pv, slots)."""
    from test_initial_state_roll_forward_929 import _setup_wire
    from test_lp_appliance_real_solver import _stub_optimal_lp_plan

    from src.scheduler import optimizer

    now = _setup_wire(monkeypatch)
    monkeypatch.setattr(weather, "_now_utc", lambda: now)       # ONE clock
    monkeypatch.setattr(
        "src.notifier.notify_pv_forecast_degraded",
        lambda m, extra=None: pushed.append(m) if pushed is not None else None,
    )
    if quartz_rows is not None:
        _patch_providers(
            monkeypatch, base=[],
            quartz=ForecastFetchResult(forecast=quartz_rows, source="quartz"),
        )
    else:
        _patch_providers(monkeypatch, base=[])
    fetch_at = seed(now) if seed else None
    seen: dict = {"all": []}

    def _capture(*, weather, **kwargs):
        pv = list(weather.pv_kwh_per_slot)
        seen["all"].append(pv)
        seen.setdefault("nominal", pv)           # the FIRST call is the nominal solve
        seen.setdefault("slots", list(weather.slot_starts_utc))
        return _stub_optimal_lp_plan(**kwargs)

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _capture)
    optimizer.run_optimizer(fox=None, daikin=None)
    return seen, fetch_at, now


def _last_exogenous() -> tuple[dict, str | None]:
    conn = db.get_connection()
    try:
        r = conn.execute(
            "SELECT exogenous_snapshot_json, forecast_fetch_at_utc FROM lp_inputs_snapshot "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return json.loads(r[0]), r[1]


def test_wire_last_good_pv_aligned_calibrated_once_and_same_in_every_solve(monkeypatch):
    seen, fetch_at, now = _run_wire(monkeypatch, seed=lambda n: _seed_wire_rows(n, age_h=1.0))
    nominal, slots = seen["nominal"], seen["slots"]
    assert max(nominal) > 0.0, "solve_lp planned on PV = 0 although a good forecast existed"
    # slot alignment against the seeded rows: PV only while the seeded kW is > 0
    for st, v in zip(slots, nominal):
        centre = st + timedelta(minutes=15)
        daylight = 6 <= centre.hour <= 18
        if not daylight and centre.hour not in (5, 19):
            assert v == 0.0, f"{st} planned {v} kWh on a zero-kW slot"
    assert any(v > 0 for st, v in zip(slots, nominal) if st.hour == 11)
    # every solve (scenarios / charge floor) is built from the same PV series
    assert len(seen["all"]) >= 1
    for pv in seen["all"]:
        assert len(pv) == len(nominal)
        if sum(nominal) > 0:
            assert 0.4 <= sum(pv) / sum(nominal) <= 1.6
    # calibration applied exactly once: identical to the FRESH-fetch path on the same rows
    rows = db.get_last_good_forecast_snapshots(max_age_hours=6.0, source="quartz", now_utc=now)[0][1]
    fresh_rows = weather._rows_to_hourly(rows)
    seen2, _, _ = _run_wire(monkeypatch, seed=None, quartz_rows=fresh_rows)
    n = min(len(nominal), len(seen2["nominal"]))
    assert seen2["nominal"][:n] == pytest.approx(nominal[:n], rel=1e-9, abs=1e-9)
    # audit trail
    exo, fx = _last_exogenous()
    assert fx is not None
    conn = db.get_connection()
    try:
        first = conn.execute(
            "SELECT exogenous_snapshot_json, forecast_fetch_at_utc FROM lp_inputs_snapshot ORDER BY run_id ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    exo1 = json.loads(first[0])
    assert exo1["pv_source"]["status"] == "lastgood"
    assert exo1["pv_source"]["fallback_reason"] == "quartz_empty"
    assert exo1["pv_source"]["reused_fetch_at_utc"] == first[1] == fetch_at
    assert exo1["pv_source"]["pv_kwh_per_slot"] == [round(v, 4) for v in nominal]


def test_wire_both_empty_beyond_last_good_coverage_plans_zero_pv(monkeypatch):
    """Fetch covers only to 2026-05-21 10:00 and its last row is daylight: the old flat
    hold would plan 2.5 kW through the night and the LP would skip grid charging."""
    until = datetime(2026, 5, 21, 10, 0, tzinfo=UTC)
    seen, _, _ = _run_wire(monkeypatch, seed=lambda n: _seed_wire_rows(n, age_h=0.2, until=until))
    nominal, slots = seen["nominal"], seen["slots"]
    inside = [v for st, v in zip(slots, nominal) if datetime(2026, 5, 21, 8, 0, tzinfo=UTC) <= st < until]
    assert max(inside) > 0.0
    beyond = [v for st, v in zip(slots, nominal) if st >= until + timedelta(minutes=30)]
    assert beyond and all(v == 0.0 for v in beyond), "PV held flat beyond the last-good coverage"


def test_wire_no_source_at_all_is_recorded_and_pushes_once(monkeypatch):
    pushed: list = []
    seen, _, _ = _run_wire(monkeypatch, seed=None, pushed=pushed)
    assert max(seen["nominal"]) == 0.0
    exo, _ = _last_exogenous()
    assert exo["pv_source"]["status"] == "none"
    assert len(pushed) == 1
    from src.scheduler import optimizer

    optimizer.run_optimizer(fox=None, daikin=None)
    assert len(pushed) == 1                                   # once per UTC day


def test_status_block_shows_the_degraded_source(monkeypatch):
    _run_wire(monkeypatch, seed=lambda n: _seed_wire_rows(n, age_h=1.0))
    ps = db.get_latest_pv_source()
    assert ps["status"] == "lastgood" and "pv_kwh_per_slot" not in ps
    from src.api.routers.status import _forecast_block

    blk = _forecast_block(True)
    assert blk["degraded"] is True and blk["pv_source"]["status"] == "lastgood"
