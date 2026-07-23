"""W2 thermal learner (#540): synthetic-τ recovery, decay-episode
decontamination, quality gates, bounded readers, and the estimator wiring.

The headline pre-sensor deliverable: the fitters are PURE, so these tests
drive them with synthetic exponential decays of KNOWN τ and assert recovery —
proving the learner works before a single real sensor reading exists.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.analytics import thermal_learning as tl
from src.config import config

TZ = ZoneInfo("UTC")


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(db, "_db_path", lambda: path)
    db.init_db()
    return path


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 600.0, raising=False)
    monkeypatch.setattr(config, "BUILDING_THERMAL_MASS_KWH_PER_K", 12.0, raising=False)
    monkeypatch.setattr(config, "THERMAL_LEARNED_VALUES_ENABLED", True, raising=False)
    yield


# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------


def _decay_night(
    night_of: date,
    *,
    tau_h: float = 20.0,
    t0: float = 21.0,
    t_out: float = 8.0,
    noise: float = 0.0,
    cadence_min: int = 10,
    start_hour: int = 22,
    end_hour: int = 7,
    room: str = "living",
) -> list[dict]:
    """Sensor readings for one night (start_hour on night_of → end_hour next
    day) following T(t) = t_out + (t0 − t_out)·e^(−t/τ) + deterministic noise."""
    start = datetime(night_of.year, night_of.month, night_of.day, start_hour, tzinfo=UTC)
    end = start + timedelta(hours=(24 - start_hour) + end_hour)
    rows = []
    i = 0
    ts = start
    while ts <= end:
        t_h = (ts - start).total_seconds() / 3600.0
        temp = t_out + (t0 - t_out) * math.exp(-t_h / tau_h)
        if noise:
            temp += noise * math.sin(i * 2.399)  # deterministic pseudo-noise
        rows.append({
            "captured_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "room": room,
            "temp_c": round(temp, 3),
        })
        ts += timedelta(minutes=cadence_min)
        i += 1
    return rows


def _outdoor_flat(start_day: date, days: int, temp: float = 8.0):
    out = []
    ts = datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)
    for _ in range(days * 24):
        out.append((ts, temp))
        ts += timedelta(hours=1)
    return out


def _select(readings, consumption=(), offsets=(), outdoor=None, **kw):
    if outdoor is None:
        first = datetime.fromisoformat(readings[0]["captured_at"].replace("Z", "+00:00"))
        outdoor = _outdoor_flat(first.date() - timedelta(days=1), 40)
    defaults = dict(
        tz=TZ, night_start_hour_local=21, night_end_hour_local=8,
        min_episode_hours=4.0, min_points=8, settle_hours=2.0,
        min_delta_t_c=5.0, heating_contam_kwh=0.1, dhw_contam_kwh=0.8,
    )
    defaults.update(kw)
    return tl.select_decay_episodes(
        list(readings), list(consumption), list(offsets), outdoor, **defaults
    )


# ---------------------------------------------------------------------------
# 1. Synthetic-τ recovery (the headline)
# ---------------------------------------------------------------------------


def test_synthetic_tau_recovery_with_noise():
    readings = []
    for i in range(3):
        readings += _decay_night(date(2026, 11, 2 + i), tau_h=20.0, noise=0.1)
    eps = _select(readings)
    assert len(eps) >= 3
    fit = tl.fit_tau(eps, min_episodes=3)
    assert fit["status"] == "ok"
    assert fit["tau_hours"] == pytest.approx(20.0, rel=0.10)
    assert fit["r2_median"] >= 0.8


def test_fit_single_episode_exact():
    eps = _select(_decay_night(date(2026, 11, 2), tau_h=15.0))
    assert len(eps) == 1
    tau, r2 = tl.fit_tau_for_episode(eps[0])
    assert tau == pytest.approx(15.0, rel=0.05)
    assert r2 > 0.99


# ---------------------------------------------------------------------------
# 2. Contamination / decontamination
# ---------------------------------------------------------------------------


def _bucket(day: date, idx: int, heating: float = 0.0, dhw: float = 0.0) -> dict:
    return {"date": day.isoformat(), "bucket_idx": idx,
            "kwh_heating": heating, "kwh_dhw": dhw}


def test_heating_bucket_rejects_episode():
    night = date(2026, 11, 2)
    readings = _decay_night(night)
    # heating active 00:00-02:00 (bucket 0 of the NEXT day) — mid-episode
    consumption = [_bucket(night + timedelta(days=1), 0, heating=0.5)]
    assert _select(readings, consumption) == []


def test_offset_window_rejects_episode():
    night = date(2026, 11, 2)
    readings = _decay_night(night)
    ws = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
    offsets = [(ws.isoformat(), (ws + timedelta(hours=1)).isoformat())]
    assert _select(readings, offsets=offsets) == []


def test_settle_margin_trims_after_heating():
    """Heating in bucket 10 (20:00-22:00) + 2 h settle blocks until 24:00 —
    the episode may only start at 00:00; the fit still succeeds on the
    remaining stretch."""
    night = date(2026, 11, 2)
    readings = _decay_night(night, start_hour=22, end_hour=7)
    consumption = [_bucket(night, 10, heating=1.0)]  # 20:00-22:00 local
    eps = _select(readings, consumption)
    assert len(eps) == 1
    # blocked until: 22:00 bucket end + 2h settle = 00:00 next day
    assert eps[0].start_utc >= datetime(2026, 11, 3, 0, 0, tzinfo=UTC)


def test_settle_margin_rejects_too_short_remainders():
    """Heating 00-02 blocks (with settle) until 04:00: the 22:00-00:00 head
    (2 h) and the 04:00-07:00 tail (3 h) are both < min 4 h → no episodes."""
    night = date(2026, 11, 2)
    readings = _decay_night(night, start_hour=22, end_hour=7)
    consumption = [_bucket(night + timedelta(days=1), 0, heating=1.0)]  # 00-02
    assert _select(readings, consumption) == []


def test_pre_dawn_warmup_keeps_the_clean_head(review_ref="M3"):
    """Winter pattern: HEM's cheap-slot warmup runs 04:00-06:00. The clean
    22:00-04:00 decay BEFORE it must survive as its own episode — the first
    cut rejected the whole night and would have starved τ all winter."""
    night = date(2026, 11, 2)
    readings = _decay_night(night, start_hour=22, end_hour=7)
    consumption = [_bucket(night + timedelta(days=1), 2, heating=1.5)]  # 04-06
    eps = _select(readings, consumption)
    assert len(eps) == 1
    assert eps[0].end_utc <= datetime(2026, 11, 3, 4, 0, tzinfo=UTC)
    fit = tl.fit_tau_for_episode(eps[0])
    assert fit is not None
    assert fit[0] == pytest.approx(20.0, rel=0.05)


def test_room_dropout_or_join_splits_episode(review_ref="H2"):
    """A sensor dying (or joining) mid-night shifts the house mean and fakes
    a decay/rise the gates can't see — the composition change must split the
    episode so no fit spans it."""
    night = date(2026, 11, 2)
    a = _decay_night(night, t0=22.0, room="living")
    b = _decay_night(night, t0=20.0, room="bedroom")
    # bedroom sensor dies at 02:00
    cutoff = datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    b = [r for r in b if datetime.fromisoformat(r["captured_at"].replace("Z", "+00:00")) < cutoff]
    eps = _select(a + b)
    for ep in eps:
        assert not (ep.start_utc < cutoff < ep.end_utc)  # nothing spans the change
    # surviving episodes still recover the true τ
    for ep in eps:
        fit = tl.fit_tau_for_episode(ep)
        if fit is not None:
            assert fit[0] == pytest.approx(20.0, rel=0.08)


def test_mixed_cadence_rooms_do_not_shatter_episodes():
    """Round-2 regression: two healthy rooms on different report cadences
    (10 vs 15 min) must NOT flicker the bin-level composition into splitting
    a clean night to nothing — the 30-min bin absorbs cadence jitter."""
    night = date(2026, 11, 2)
    a = _decay_night(night, t0=22.0, room="living", cadence_min=10)
    b = _decay_night(night, t0=20.0, room="bedroom", cadence_min=15)
    eps = _select(a + b)
    assert len(eps) == 1
    assert (eps[0].end_utc - eps[0].start_utc) >= timedelta(hours=8)


def test_varying_outdoor_per_point_fit(review_ref="M2"):
    """A falling night (12→2 °C) biased the mean-T_out fit ~+10%; the
    per-point fit must stay within ±8% of the true τ. Simulated with the real
    ODE (Euler, 5-min steps), not the constant-ambient closed form."""
    tau_true = 20.0
    start = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
    end = datetime(2026, 11, 3, 7, 0, tzinfo=UTC)
    total_h = (end - start).total_seconds() / 3600.0

    def t_out_at(t_h: float) -> float:
        return 12.0 - 10.0 * (t_h / total_h)  # linear 12 → 2 °C

    readings = []
    outdoor = []
    temp = 21.0
    step_min = 5
    for minutes in range(0, int(total_h * 60) + 1, step_min):
        t_h = minutes / 60.0
        ts = start + timedelta(minutes=minutes)
        if minutes % 10 == 0:  # sensor reports every 10 min
            readings.append({
                "captured_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "room": "living", "temp_c": round(temp, 3),
            })
        outdoor.append((ts, t_out_at(t_h)))
        temp += -(temp - t_out_at(t_h)) / tau_true * (step_min / 60.0)
    eps = _select(readings, outdoor=outdoor)
    assert len(eps) == 1
    fit = tl.fit_tau_for_episode(eps[0])
    assert fit is not None
    assert fit[0] == pytest.approx(tau_true, rel=0.08)


def test_dhw_boost_rejects_but_normal_dhw_kept():
    night = date(2026, 11, 2)
    readings = _decay_night(night)
    # normal maintenance DHW (0.1 kWh) mid-episode → kept
    keep = [_bucket(night + timedelta(days=1), 1, dhw=0.1)]
    assert len(_select(readings, keep)) == 1
    # heavy boost (1.5 kWh) → rejected
    reject = [_bucket(night + timedelta(days=1), 1, dhw=1.5)]
    assert _select(readings, reject) == []


def test_warm_night_and_rising_temp_rejected():
    night = date(2026, 11, 2)
    # ΔT = 21 − 18 = 3 °C < 5 → unidentifiable
    warm = _decay_night(night, t_out=18.0)
    warm_outdoor = _outdoor_flat(night - timedelta(days=1), 40, temp=18.0)
    assert _select(warm, outdoor=warm_outdoor) == []
    # rising temperature (gains / heater someone forgot to log) → rejected
    rising = _decay_night(night, tau_h=20.0)
    for i, r in enumerate(rising):
        r["temp_c"] = 21.0 + 0.05 * i  # monotone rise above the start temp
    assert _select(rising) == []


def test_gap_splits_segment():
    night = date(2026, 11, 2)
    readings = [
        r for r in _decay_night(night)
        if not ("T00:3" in r["captured_at"] or "T01:" in r["captured_at"])
    ]  # ~1.5h hole around 00:30-02:00
    eps = _select(readings)
    # split into two segments; both may or may not pass length gates, but no
    # single episode may span the gap
    for ep in eps:
        assert (ep.end_utc - ep.start_utc) < timedelta(hours=8)


def test_multi_room_mean():
    night = date(2026, 11, 2)
    a = _decay_night(night, t0=22.0, room="living")
    b = _decay_night(night, t0=20.0, room="bedroom")
    eps = _select(a + b)
    assert len(eps) == 1
    # 30-min bin mean of the first half hour of decay, not the instant t0
    assert eps[0].t_in_start_c == pytest.approx(21.0, abs=0.15)


# ---------------------------------------------------------------------------
# 3. Gates / no-op on the empty prod-shaped DB
# ---------------------------------------------------------------------------


def test_refresh_on_empty_db_is_quiet_skip(tmp_db):
    result = tl.refresh_building_thermal_calibration()
    assert result["status"] == "skipped"
    assert "no indoor sensor data" in result["reason"]
    assert db.get_building_thermal_calibration() is None  # no upsert


def test_fit_tau_below_min_episodes_skips():
    eps = _select(_decay_night(date(2026, 11, 2)))
    fit = tl.fit_tau(eps, min_episodes=5)
    assert fit["status"] == "skipped"


def test_ua_fit_gates():
    # too few days
    rows = [(30.0, 20.0, 10.0)] * 5
    assert tl.fit_ua_hdd(rows, min_days=20)["status"] == "skipped"
    # synthetic load = 7.2 + 5.0·HDD → UA = 5.0 × 3 / 24 × 1000 = 625 W/K
    rows = []
    for i in range(30):
        hdd = 2.0 + i * 0.4
        rows.append((7.2 + 5.0 * hdd, 20.0, 20.0 - hdd))
    fit = tl.fit_ua_hdd(rows, assumed_cop=3.0, min_days=20)
    assert fit["status"] == "ok"
    assert fit["ua_w_per_k"] == pytest.approx(625.0, rel=0.02)
    assert fit["r2"] > 0.99


# ---------------------------------------------------------------------------
# 4. Readers: bounds, fallbacks, kill switch
# ---------------------------------------------------------------------------


def _seed_calibration(tau=18.0, ua=None, c=None):
    db.upsert_building_thermal_calibration({
        "tau_hours": tau, "tau_r2_median": 0.9, "tau_episodes": 6,
        "tau_window_days": 21, "tau_computed_at": "2026-11-05T05:30:00Z",
        "ua_w_per_k": ua, "c_kwh_per_k": c,
        "c_source": "tau_x_env_ua" if c is not None else None,
    })


def test_readers_fallback_and_learned(tmp_db, monkeypatch):
    # empty table → env
    assert tl.get_building_tau_hours() == pytest.approx(20.0)  # 12 kWh/K / 600 W/K
    assert tl.get_building_ua_w_per_k() == pytest.approx(600.0)
    assert tl.get_building_thermal_mass_kwh_per_k() == pytest.approx(12.0)
    # good row → learned
    _seed_calibration(tau=18.0, ua=650.0, c=11.7)
    assert tl.get_building_tau_hours() == pytest.approx(18.0)
    assert tl.get_building_ua_w_per_k() == pytest.approx(650.0)
    assert tl.get_building_thermal_mass_kwh_per_k() == pytest.approx(11.7)
    # kill switch → env
    monkeypatch.setattr(config, "THERMAL_LEARNED_VALUES_ENABLED", False, raising=False)
    assert tl.get_building_tau_hours() == pytest.approx(20.0)


def test_readers_reject_out_of_bounds(tmp_db):
    _seed_calibration(tau=300.0, ua=5000.0, c=200.0)  # all absurd
    assert tl.get_building_tau_hours() == pytest.approx(20.0)
    assert tl.get_building_ua_w_per_k() == pytest.approx(600.0)
    assert tl.get_building_thermal_mass_kwh_per_k() == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# 5. Estimator integration
# ---------------------------------------------------------------------------


def test_estimator_uses_learned_constants(tmp_db):
    from src.daikin.estimator import estimate_state

    last_live = {
        "fetched_at": datetime(2026, 11, 5, 0, 0, tzinfo=UTC).timestamp(),
        "tank_temp_c": 45.0,
        "indoor_temp_c": 21.0,
        "outdoor_temp_c": 5.0,
    }
    now = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)  # 6h walk
    base = estimate_state(last_live, now)
    # A leakier house (higher UA, lower C → much smaller τ) must decay further
    _seed_calibration(tau=8.0, ua=1200.0, c=9.6)
    leaky = estimate_state(last_live, now)
    assert leaky.indoor_temp_c < base.indoor_temp_c


def test_meteo_temps_range_freshest_per_slot(tmp_db):
    """H1 companion: the range query returns the freshest value per slot
    across BOTH meteo tables, over the whole range in one call."""
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO meteo_forecast_history (slot_time, forecast_fetch_at_utc, temp_c)"
        " VALUES ('2026-11-02T10:00:00Z', '2026-11-01T00:00:00Z', 5.0)"
    )
    conn.execute(
        "INSERT INTO meteo_forecast_value (slot_time, forecast_fetch_at_utc, temp_c)"
        " VALUES ('2026-11-02T10:00:00Z', '2026-11-02T00:00:00Z', 7.0)"  # fresher
    )
    conn.execute(
        "INSERT INTO meteo_forecast_value (slot_time, forecast_fetch_at_utc, temp_c)"
        " VALUES ('2026-11-03T10:00:00Z', '2026-11-02T00:00:00Z', 9.0)"
    )
    conn.commit()
    conn.close()
    rows = db.get_meteo_temps_range("2026-11-02", "2026-11-03")
    assert dict(rows) == {"2026-11-02T10:00:00Z": 7.0, "2026-11-03T10:00:00Z": 9.0}


# ---------------------------------------------------------------------------
# 6. End-to-end refresh with synthetic prod-shaped data
# ---------------------------------------------------------------------------


def test_refresh_end_to_end_learns_tau(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "THERMAL_TAU_MIN_EPISODES", 3, raising=False)
    # Freeze windows to cover our synthetic nights (recent relative to now)
    today = datetime.now(UTC).date()
    readings = []
    for i in range(4):
        readings += _decay_night(today - timedelta(days=i + 2), tau_h=22.0, noise=0.05)
    db.save_indoor_readings(readings)
    # outdoor series via meteo_forecast_value rows
    conn = sqlite3.connect(tmp_db)
    d0 = today - timedelta(days=7)
    ts = datetime(d0.year, d0.month, d0.day, tzinfo=UTC)
    while ts < datetime.now(UTC):
        conn.execute(
            "INSERT OR REPLACE INTO meteo_forecast_value"
            " (slot_time, forecast_fetch_at_utc, temp_c) VALUES (?, ?, 8.0)",
            (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-01-01T00:00:00Z"),
        )
        ts += timedelta(hours=1)
    conn.commit()
    conn.close()

    result = tl.refresh_building_thermal_calibration()
    assert result["status"] == "ok"
    row = db.get_building_thermal_calibration()
    assert row is not None
    assert row["tau_hours"] == pytest.approx(22.0, rel=0.12)
    # UA stays unlearned (no winter HDD data) → C from env UA, source flagged
    assert row["ua_w_per_k"] is None
    assert row["c_source"] == "tau_x_env_ua"
    assert row["c_kwh_per_k"] == pytest.approx(22.0 * 600.0 / 1000.0, rel=0.12)
    # second refresh with no new data keeps the row coherent (merge semantics)
    result2 = tl.refresh_building_thermal_calibration()
    assert result2["status"] == "ok"


# ---------------------------------------------------------------------------
# 7. #760 — phantom onecta_cache kwh_heating guard (the #749 family)
# ---------------------------------------------------------------------------


def _phantom_row(day: date, idx: int, heating: float, source: str = "onecta_cache",
                 dhw: float = 0.0) -> dict:
    return {"date": day.isoformat(), "bucket_idx": idx, "kwh_heating": heating,
            "kwh_dhw": dhw, "source": source}


def _patch_curve_kw(monkeypatch, kw: float):
    """Fix the weather-curve model at a constant kW draw."""
    import src.physics as physics
    monkeypatch.setattr(physics, "get_daikin_heating_kw",
                        lambda t, lwt_offset_delta=0.0: kw)


def test_phantom_summer_bucket_zeroed(monkeypatch):
    _patch_curve_kw(monkeypatch, 0.0)  # mild night: curve says compressor off
    day = date(2026, 7, 22)
    rows = [_phantom_row(day, 0, 1.0, dhw=0.4)]
    outdoor = _outdoor_flat(day - timedelta(days=1), 3, temp=19.0)
    cleaned, n = tl.sanitize_phantom_heating(rows, outdoor, TZ)
    assert n == 1
    assert cleaned[0]["kwh_heating"] == 0.0
    assert cleaned[0]["kwh_dhw"] == 0.4  # DHW untouched
    assert rows[0]["kwh_heating"] == 1.0  # input not mutated


def test_winter_real_integer_bucket_kept(monkeypatch):
    _patch_curve_kw(monkeypatch, 0.8)  # cold night: 1.6 kWh plausible per bucket
    day = date(2026, 1, 10)
    rows = [_phantom_row(day, 2, 2.0)]
    cleaned, n = tl.sanitize_phantom_heating(
        rows, _outdoor_flat(day - timedelta(days=1), 3, temp=2.0), TZ)
    assert n == 0
    assert cleaned[0]["kwh_heating"] == 2.0


def test_non_candidates_untouched(monkeypatch):
    _patch_curve_kw(monkeypatch, 0.0)
    day = date(2026, 7, 22)
    rows = [
        _phantom_row(day, 1, 1.0, source="telemetry_integral"),  # not onecta
        _phantom_row(day, 2, 0.7),  # non-integer → real decimal, not a quantum
        _phantom_row(day, 3, 0.0),  # zero heating
    ]
    cleaned, n = tl.sanitize_phantom_heating(
        rows, _outdoor_flat(day - timedelta(days=1), 3, temp=19.0), TZ)
    assert n == 0
    assert [r["kwh_heating"] for r in cleaned] == [1.0, 0.7, 0.0]


def test_no_outdoor_coverage_keeps_row(monkeypatch):
    _patch_curve_kw(monkeypatch, 0.0)
    day = date(2026, 7, 22)
    rows = [_phantom_row(day, 0, 1.0)]
    cleaned, n = tl.sanitize_phantom_heating(rows, [], TZ)
    assert n == 0
    assert cleaned[0]["kwh_heating"] == 1.0


def test_phantom_guard_restores_episode_end_to_end(monkeypatch):
    """The prod starvation shape: a clean decay night + one phantom 1.0 bucket
    mid-episode. Raw rows kill the episode; sanitized rows restore it."""
    _patch_curve_kw(monkeypatch, 0.0)
    night = date(2026, 7, 21)
    readings = _decay_night(night, tau_h=50.0, t0=28.0, t_out=18.0)
    outdoor = _outdoor_flat(night - timedelta(days=1), 3, temp=18.0)
    phantom = [_phantom_row(night + timedelta(days=1), 0, 1.0)]  # 00-02h mid-decay
    assert _select(readings, phantom, outdoor=outdoor) == []
    cleaned, n = tl.sanitize_phantom_heating(phantom, outdoor, TZ)
    assert n == 1
    eps = _select(readings, cleaned, outdoor=outdoor)
    assert len(eps) == 1
    tau, r2 = tl.fit_tau_for_episode(eps[0])
    assert tau == pytest.approx(50.0, rel=0.05)
