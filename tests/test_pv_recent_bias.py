"""Adaptive PV recent-bias corrector (#486) — closed loop on the committed
forecast's own error (pv_error_log), damped + clamped + recency-weighted."""
from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from src.config import config


def _seed(conn, *, hour: int, days_ago: float, forecast: float, actual: float,
          ceiling: float | None = 99.0):
    """Seed a slot. ``ceiling`` is the rail STAMPED on the row (#762): None
    reproduces a pre-migration row, which must be excluded from training."""
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    conn.execute(
        """INSERT OR REPLACE INTO pv_error_log
           (slot_time_utc, run_id, forecast_kwh, actual_kwh, error_kwh, built_at_utc,
            ceiling_kwh)
           VALUES (?, 1, ?, ?, ?, ?, ?)""",
        (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), forecast, actual, actual - forecast,
         datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), ceiling),
    )


def _seed_minute(conn, *, hour: int, minute: int, days_ago: float, forecast: float,
                 actual: float, ceiling: float | None = 99.0):
    """Same as :func:`_seed` but for the second half-hour slot of an hour."""
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    conn.execute(
        """INSERT OR REPLACE INTO pv_error_log
           (slot_time_utc, run_id, forecast_kwh, actual_kwh, error_kwh, built_at_utc,
            ceiling_kwh)
           VALUES (?, 1, ?, ?, ?, ?, ?)""",
        (ts.strftime("%Y-%m-%dT%H:%M:%SZ"), forecast, actual, actual - forecast,
         datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), ceiling),
    )


def _cfg(monkeypatch, **kw):
    defaults = dict(WINDOW_DAYS=14, HALFLIFE_DAYS=5.0, DAMPING=0.5, MIN=0.4, MAX=2.5,
                    MIN_KWH=0.05, MIN_DAYS=1)
    defaults.update(kw)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_WINDOW_DAYS", defaults["WINDOW_DAYS"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_HALFLIFE_DAYS", defaults["HALFLIFE_DAYS"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_DAMPING", defaults["DAMPING"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_MIN", defaults["MIN"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_MAX", defaults["MAX"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_MIN_KWH", defaults["MIN_KWH"], raising=False)
    monkeypatch.setattr(config, "PV_RECENT_BIAS_MIN_DAYS", defaults["MIN_DAYS"], raising=False)


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
    """A forecast sitting at its stamped ceiling is a censored observation: the
    true forecast was higher, so actual/forecast over-states the correction
    needed. Training on it is what made this loop ratchet (the 2026-07-21..23
    incident)."""
    import src.db as db
    from src import weather

    _cfg(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            # Clipped slots that LOOK 2x under-forecast — must be ignored.
            for d in range(1, 5):
                _seed(conn, hour=12, days_ago=d, forecast=1.0, actual=2.0, ceiling=1.0)
            # An uncensored hour is still learned normally.
            for d in range(1, 5):
                _seed(conn, hour=10, days_ago=d, forecast=1.0, actual=1.5, ceiling=99.0)
            conn.commit()
        finally:
            conn.close()
        factors, raw, _s, diag = weather.compute_pv_recent_bias_by_hour()

    assert 12 not in factors, "clipped slots must not train the corrector"
    assert round(raw[10], 2) == 1.5, "uncensored hours still learn"
    assert diag["censored_slots_excluded"] == 4


def test_unstamped_legacy_rows_excluded(monkeypatch):
    """Pre-#762 rows carry no ceiling, so we cannot tell whether they were
    clipped — and they date from the era when the rail bound on 41 % of midday
    slots. They must be dropped, not trusted.

    Without this the poisoned backlog keeps training the corrector for a full
    PV_RECENT_BIAS_WINDOW_DAYS after deploy, while the diagnostic reports zero
    exclusions and looks healthy.
    """
    import src.db as db
    from src import weather

    _cfg(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            for d in range(1, 5):
                _seed(conn, hour=12, days_ago=d, forecast=1.0, actual=2.0, ceiling=None)
            conn.commit()
        finally:
            conn.close()
        factors, _raw, _s, diag = weather.compute_pv_recent_bias_by_hour()

    assert 12 not in factors
    assert diag["unstamped_slots_excluded"] == 4


def test_shipped_defaults_bound_the_corrector():
    """The suite mostly runs with relaxed knobs to exercise the maths, so pin
    what PRODUCTION actually ships. A regression reverting these to the
    pre-#762 values would otherwise pass 100 % green."""
    assert config.PV_RECENT_BIAS_MIN == 0.6
    assert config.PV_RECENT_BIAS_MAX == 1.4, "2.5 let the ratchet reach 2.5x"
    assert config.PV_RECENT_BIAS_MIN_DAYS == 3, "one day is weather, not bias"


def test_backfilling_a_pre_rail_day_does_not_stamp_it_clean(monkeypatch):
    """Rebuilding an OLD day must leave it unstamped.

    Slots before the flat rail were committed under the old sinusoid ceiling
    (clipping at 1.32-1.43). Stamping today's 2.59 rail onto them would make a
    censored forecast compare clean and re-enter training — reproducing #762.
    A back-rebuild is the obvious response to seeing `unstamped_slots_excluded`
    in the logs, so it has to be safe by construction.
    """
    import src.db as db

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()

        old_day = db.PV_FLAT_RAIL_SINCE - timedelta(days=1)
        new_day = db.PV_FLAT_RAIL_SINCE
        monkeypatch.setattr(db, "committed_pv_forecast_by_slot",
                            lambda d: {f"{d.isoformat()}T12:00:00Z": 1.40})
        monkeypatch.setattr(db, "half_hourly_solar_kwh_for_day",
                            lambda d: {f"{d.isoformat()}T12:00:00Z": 0.90})

        db.rebuild_pv_error_log_for_date(old_day)
        db.rebuild_pv_error_log_for_date(new_day)

        conn = db.get_connection()
        try:
            stamps = dict(conn.execute(
                "SELECT substr(slot_time_utc,1,10), ceiling_kwh FROM pv_error_log"))
        finally:
            conn.close()

    assert stamps[old_day.isoformat()] is None, "pre-rail day must stay unstamped"
    assert stamps[new_day.isoformat()] is not None, "post-rail day must be stamped"


def test_cutover_excludes_the_mixed_deploy_day():
    """The cutover must be deploy-day PLUS ONE.

    The rollout landed midday on 2026-07-24, so that day is MIXED: its morning
    slots were committed under the old rail, and two (11:00, 11:30 UTC) sit
    pinned at the old 1.322 value. Setting the cutover to the deploy date would
    stamp those censored slots clean and feed them back into the corrector.
    """
    import src.db as db

    assert db.PV_FLAT_RAIL_SINCE > date(2026, 7, 24), (
        "2026-07-24 is a mixed day — planned under the old rail until midday"
    )


def test_single_day_cannot_set_a_factor(monkeypatch):
    """One day's weather is not bias.

    A half-hour slot yields 2 samples/hour/day, so a bare `n >= 2` gate let a
    SINGLE day drive the correction. Measured against the real prod DB: with
    only 2026-07-23 (96 % cloud) stamped, the corrector produced factors
    slammed against BOTH clamps (0.6 and 1.4) and would have applied them to
    the next day's plan.
    """
    import src.db as db
    from src import weather

    _cfg(monkeypatch, MIN_DAYS=3)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            # Both half-hour slots of hour 12, but all on ONE day.
            _seed(conn, hour=12, days_ago=1, forecast=1.0, actual=0.4)
            _seed_minute(conn, hour=12, minute=30, days_ago=1, forecast=1.0, actual=0.4)
            conn.commit()
        finally:
            conn.close()
        factors, _raw, _s, diag = weather.compute_pv_recent_bias_by_hour()

    assert 12 not in factors, "one overcast day must not set a correction"
    assert diag["hours_below_min_days"] == 1

    # Spread the same evidence across 3 days → now it counts.
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            for d in (1, 2, 3):
                _seed(conn, hour=12, days_ago=d, forecast=1.0, actual=0.4)
            conn.commit()
        finally:
            conn.close()
        factors, _raw, _s, diag = weather.compute_pv_recent_bias_by_hour()

    assert 12 in factors
    assert diag["hours_below_min_days"] == 0


def test_refresh_clears_stale_factors_when_no_usable_samples(monkeypatch):
    """No usable evidence must mean NO correction, not "keep yesterday's".

    Returning early left the last-persisted factors in force indefinitely — an
    absorbing state where a bad correction outlives the data that produced it.
    """
    import src.db as db
    from src import weather

    _cfg(monkeypatch)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        # An inflated factor from the incident era is already persisted...
        db.upsert_pv_recent_bias({12: 2.5}, {12: 1.1}, {12: 9},
                                 datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        assert db.get_pv_recent_bias().get(12) == 2.5
        # ...and the only available rows are untrustworthy legacy ones.
        conn = db.get_connection()
        try:
            for d in range(1, 5):
                _seed(conn, hour=12, days_ago=d, forecast=1.0, actual=2.0, ceiling=None)
            conn.commit()
        finally:
            conn.close()

        n = weather.refresh_pv_recent_bias()
        remaining = db.get_pv_recent_bias()

    assert n == 0
    assert remaining == {}, "stale inflated factors must be cleared, not preserved"


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
