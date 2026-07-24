"""Adaptive PV recent-bias corrector (#486) — closed loop on the committed
forecast's own error (pv_error_log), damped + clamped + recency-weighted."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.config import config


def _seed(conn, *, hour: int, days_ago: float, forecast: float, actual: float):
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    conn.execute(
        """INSERT OR REPLACE INTO pv_error_log
           (slot_time_utc, run_id, forecast_kwh, actual_kwh, error_kwh, built_at_utc)
           VALUES (?, 1, ?, ?, ?, ?)""",
        (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), forecast, actual, actual - forecast,
         datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )


def _no_ceiling(monkeypatch):
    """Pin the ceiling above every seeded value so the censored-slot filter is
    a no-op — these tests exercise the bias maths, not the rail."""
    from src import weather

    monkeypatch.setattr(
        weather, "_build_pv_hourly_ceiling", lambda *a, **k: {h: 99.0 for h in range(24)}
    )


def _cfg(monkeypatch, **kw):
    _no_ceiling(monkeypatch)
    defaults = dict(WINDOW_DAYS=14, HALFLIFE_DAYS=5.0, DAMPING=0.5, MIN=0.4, MAX=2.5, MIN_KWH=0.05)
    defaults.update(kw)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_WINDOW_DAYS", defaults["WINDOW_DAYS"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_HALFLIFE_DAYS", defaults["HALFLIFE_DAYS"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_DAMPING", defaults["DAMPING"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_MIN", defaults["MIN"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_MAX", defaults["MAX"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_MIN_KWH", defaults["MIN_KWH"], raising=False)


def test_warm_start_full_correction_first_pass(monkeypatch):
    # No prior factor → jump straight to the measured ratio (we already have the
    # history; don't crawl from 1.0). Damping only kicks in once a factor exists.
    import src.db as db
    from src import weather

    _cfg(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            # Hour 9: forecast 2× too low (ratio 2.0) over several recent days.
            for d in range(1, 5):
                _seed(conn, hour=9, days_ago=d, forecast=1.0, actual=2.0)
            # Hour 14: forecast 2× too high (ratio 0.5).
            for d in range(1, 5):
                _seed(conn, hour=14, days_ago=d, forecast=2.0, actual=1.0)
            conn.commit()
        finally:
            conn.close()
        factors, raw, samples, diag = weather.compute_pv_recent_bias_by_hour()

    assert round(raw[9], 2) == 2.0
    assert factors[9] == 2.0  # warm start = full measured correction
    assert round(raw[14], 2) == 0.5
    assert factors[14] == 0.5
    assert samples[9] == 4


def test_clamp_caps_extreme(monkeypatch):
    import src.db as db
    from src import weather

    _cfg(monkeypatch, DAMPING=1.0, MAX=1.6)  # full correction, but clamp bites
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            for d in range(1, 5):
                _seed(conn, hour=9, days_ago=d, forecast=0.5, actual=3.0)  # ratio 6
            conn.commit()
        finally:
            conn.close()
        factors, raw, _s, _d = weather.compute_pv_recent_bias_by_hour()
    assert raw[9] == 6.0
    assert factors[9] == 1.6  # clamped to MAX


def test_recency_weighting_favours_recent(monkeypatch):
    import src.db as db
    from src import weather

    _cfg(monkeypatch, HALFLIFE_DAYS=2.0, DAMPING=1.0)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            # Recent days say ratio ~1.0; one very old day says ratio 3.0.
            for d in (1, 2, 3):
                _seed(conn, hour=10, days_ago=d, forecast=1.0, actual=1.0)
            _seed(conn, hour=10, days_ago=13, forecast=1.0, actual=3.0)
            conn.commit()
        finally:
            conn.close()
        _f, raw, _s, _d = weather.compute_pv_recent_bias_by_hour()
    # The stale 3.0 is heavily down-weighted → weighted ratio stays near 1.
    assert raw[10] < 1.3


def test_min_samples_excluded(monkeypatch):
    import src.db as db
    from src import weather

    _cfg(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed(conn, hour=11, days_ago=1, forecast=1.0, actual=2.0)  # only 1 sample
            conn.commit()
        finally:
            conn.close()
        factors, _r, _s, _d = weather.compute_pv_recent_bias_by_hour()
    assert 11 not in factors


def test_accumulates_toward_full_correction(monkeypatch):
    # With a previous factor of 1.5 and the corrected forecast still 1.33× low,
    # the factor ACCUMULATES upward (toward full correction), not back to ~1.5.
    # NB accumulation is only sound while the error signal is UNCENSORED —
    # see test_censored_slots_excluded_from_training for the ratchet it caused
    # once the ceiling started clipping the forecast it trains on.
    import src.db as db
    from src import weather

    _cfg(monkeypatch, DAMPING=0.5, MAX=2.5)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        # seed previous factor
        db.upsert_pv_recent_bias({9: 1.5}, {9: 2.0}, {9: 4},
                                 datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        conn = db.get_connection()
        try:
            for d in range(1, 5):
                _seed(conn, hour=9, days_ago=d, forecast=1.5, actual=2.0)  # residual ratio 1.33
            conn.commit()
        finally:
            conn.close()
        factors, raw, _s, _d = weather.compute_pv_recent_bias_by_hour()
    assert round(raw[9], 2) == 1.33
    # old 1.5 × (1 + 0.5*(1.333-1)) = 1.5 × 1.1665 ≈ 1.75 — grew past 1.5.
    assert 1.7 < factors[9] < 1.8


def test_censored_slots_excluded_from_training(monkeypatch):
    """A forecast sitting at its ceiling is a censored observation: the true
    forecast was higher, so actual/forecast over-states the correction needed.
    Training on it is what made this loop ratchet (2026-07-21..23 incident)."""
    import src.db as db
    from src import weather

    _cfg(monkeypatch)
    # Ceiling at 1.0 for hour 12 — the seeded clipped slots sit exactly there.
    monkeypatch.setattr(
        weather, "_build_pv_hourly_ceiling",
        lambda *a, **k: {h: (1.0 if h == 12 else 99.0) for h in range(24)},
    )
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            # Clipped slots that LOOK 2x under-forecast — must be ignored.
            for d in range(1, 5):
                _seed(conn, hour=12, days_ago=d, forecast=1.0, actual=2.0)
            # An uncensored hour is still learned normally.
            for d in range(1, 5):
                _seed(conn, hour=10, days_ago=d, forecast=1.0, actual=1.5)
            conn.commit()
        finally:
            conn.close()
        factors, raw, _s, diag = weather.compute_pv_recent_bias_by_hour()

    assert 12 not in factors, "clipped slots must not train the corrector"
    assert round(raw[10], 2) == 1.5, "uncensored hours still learn"
    assert diag["censored_slots_excluded"] == 4


def test_apply_path_scales_pv_when_enabled(monkeypatch):
    import src.db as db
    from src import weather
    from src.weather import HourlyForecast, forecast_to_lp_inputs

    # Pin the flat calibration to 1.0 so base PV is positive (empty test DB
    # would otherwise make compute_pv_calibration_factor return ~0).
    monkeypatch.setattr(weather, "compute_pv_calibration_factor", lambda *a, **k: 1.0)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        # Bias factor 2.0 at UTC hour 9.
        db.upsert_pv_recent_bias({9: 2.0}, {9: 2.0}, {9: 4},
                                 datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

        base = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)
        fc = [
            HourlyForecast(time_utc=base + timedelta(hours=k), temperature_c=12.0,
                           cloud_cover_pct=10.0, shortwave_radiation_wm2=300.0,
                           estimated_pv_kw=0.0, heating_demand_factor=0.0)
            for k in range(4)
        ]
        slots = [datetime(2026, 6, 10, 9, 0, tzinfo=UTC)]  # UTC hour 9

        monkeypatch.setattr(config, "PV_RECENT_BIAS_ENABLED", False, raising=False)
        off = forecast_to_lp_inputs(fc, slots, pv_scale=1.0).pv_kwh_per_slot[0]
        monkeypatch.setattr(config, "PV_RECENT_BIAS_ENABLED", True, raising=False)
        on = forecast_to_lp_inputs(fc, slots, pv_scale=1.0).pv_kwh_per_slot[0]

    assert off > 0.0, "need positive base PV to test scaling"
    # Enabled ≈ 2× disabled (modulo the physical ceiling, which is well above).
    assert abs(on - 2.0 * off) < 1e-6


def test_refresh_persists_and_get(monkeypatch):
    import src.db as db
    from src import weather

    _cfg(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            for d in range(1, 5):
                _seed(conn, hour=9, days_ago=d, forecast=1.0, actual=2.0)
            conn.commit()
        finally:
            conn.close()
        n = weather.refresh_pv_recent_bias()
        got = db.get_pv_recent_bias()
    assert n >= 1
    assert got.get(9) == 2.0  # warm start (no prior) = full measured ratio
