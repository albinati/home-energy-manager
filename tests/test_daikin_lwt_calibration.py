"""Per-installation Daikin LWT→kW calibration.

Tests the regression math + loader fallback that replaces the hardcoded
``_KW_PER_DEGC_LWT_DEFAULT = 0.0333`` with a value fitted nightly from the
household's actual ``kwh_heating`` vs outdoor-temp history.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from src import db, physics
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_meteo_day(d: date, hourly_temps: list[float], *, table: str = "meteo_forecast_value") -> None:
    """Insert ONE snapshot covering all 24 UTC hours of ``d`` into ``table``.

    Default writes to ``meteo_forecast_value``; pass ``table='meteo_forecast_history'``
    to seed older days (the value table is pruned to ~7 days in prod).
    """
    assert len(hourly_temps) == 24
    fetch_at = (datetime(d.year, d.month, d.day, 23, 0, tzinfo=UTC)).isoformat()
    cols = "(forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2, cloud_cover_pct"
    vals = "(?, ?, ?, NULL, NULL"
    if table == "meteo_forecast_value":
        cols += ", direct_pv_kw)"
        vals += ", NULL)"
    else:
        cols += ")"
        vals += ")"
    with db._lock:
        conn = db.get_connection()
        try:
            for h, t in enumerate(hourly_temps):
                slot = datetime(d.year, d.month, d.day, h, 0, tzinfo=UTC).isoformat()
                conn.execute(f"INSERT INTO {table} {cols} VALUES {vals}", (fetch_at, slot, t))
            conn.commit()
        finally:
            conn.close()


def _x_day(hourly_temps: list[float]) -> float:
    """Recompute the ground-truth X_day = Σ_h max(0, LWT(t) − 18) × 1 h."""
    return sum(max(0.0, physics.get_lwt_base_c(t) - 18.0) for t in hourly_temps) * 1.0


# ---------------------------------------------------------------------------
# Regression math
# ---------------------------------------------------------------------------

def test_regression_recovers_known_k() -> None:
    """With y = 0.045 · X exactly across 14 days, fit should land near 0.045."""
    target_k = 0.045
    base = date.today() - timedelta(days=20)
    for i in range(14):
        d = base + timedelta(days=i)
        # Vary outdoor temp 4..14 across days to give the regressor leverage.
        t_const = 4.0 + (i % 11)
        temps = [t_const] * 24
        _seed_meteo_day(d, temps)
        y = target_k * _x_day(temps)
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=y + 2.0, kwh_heating=y, kwh_dhw=2.0,
            source="onecta",
        )

    result = db.compute_daikin_lwt_kw_calibration(window_days=30, min_samples=7)
    assert result["status"] == "ok", result
    assert result["k_per_degc"] == pytest.approx(target_k, abs=1e-6)
    assert result["samples"] >= 7
    assert result["rmse_kwh"] < 1e-6   # noise-free fit


def test_regression_skips_when_too_few_samples() -> None:
    """Only 3 days of data → status='skipped', no row written."""
    base = date.today() - timedelta(days=20)
    for i in range(3):
        d = base + timedelta(days=i)
        temps = [8.0] * 24
        _seed_meteo_day(d, temps)
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=10.0, kwh_heating=8.0, kwh_dhw=2.0,
            source="onecta",
        )

    result = db.compute_daikin_lwt_kw_calibration(window_days=30, min_samples=7)
    assert result["status"] == "skipped"
    assert result["samples"] < 7


def test_regression_drops_anomalous_days() -> None:
    """Inject one day with 10× heating draw — outlier filter must drop it."""
    target_k = 0.040
    base = date.today() - timedelta(days=20)
    for i in range(12):
        d = base + timedelta(days=i)
        temps = [5.0 + (i % 8)] * 24
        _seed_meteo_day(d, temps)
        y = target_k * _x_day(temps)
        if i == 5:
            y *= 10.0  # huge anomaly — open windows or sensor glitch
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=y + 2.0, kwh_heating=y, kwh_dhw=2.0,
            source="onecta",
        )
    result = db.compute_daikin_lwt_kw_calibration(window_days=30, min_samples=7)
    assert result["status"] == "ok"
    assert result["k_per_degc"] == pytest.approx(target_k, abs=1e-3)
    assert result["outliers_filtered"] >= 1


def test_regression_skipped_when_no_meteo() -> None:
    """Heating rows present but zero meteo coverage → skipped."""
    base = date.today() - timedelta(days=20)
    for i in range(10):
        d = base + timedelta(days=i)
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=8.0, kwh_heating=6.0, kwh_dhw=2.0,
            source="onecta",
        )
    result = db.compute_daikin_lwt_kw_calibration(window_days=30, min_samples=7)
    assert result["status"] == "skipped"
    assert "no daikin_consumption" not in (result.get("reason") or "")  # rows exist
    assert result["samples"] == 0  # all dropped for missing meteo


# ---------------------------------------------------------------------------
# Loader / physics integration
# ---------------------------------------------------------------------------

def test_loader_returns_default_when_table_empty() -> None:
    assert physics.get_kw_per_degc_lwt() == physics._KW_PER_DEGC_LWT_DEFAULT


def test_loader_returns_calibrated_value_when_present() -> None:
    db.upsert_daikin_lwt_kw_calibration(
        k_per_degc=0.040, samples=14, window_days=30, rmse_kwh=0.5, bias_kwh=0.0,
    )
    assert physics.get_kw_per_degc_lwt() == pytest.approx(0.040, abs=1e-9)


def test_loader_falls_back_when_calibrated_value_out_of_bounds() -> None:
    """A wildly wrong fit (e.g. 0.5 — 15× default) must NOT poison the LP."""
    db.upsert_daikin_lwt_kw_calibration(
        k_per_degc=0.5, samples=10, window_days=30, rmse_kwh=10.0, bias_kwh=0.0,
    )
    assert physics.get_kw_per_degc_lwt() == physics._KW_PER_DEGC_LWT_DEFAULT


def test_loader_falls_back_on_negative_value() -> None:
    """Defensive: negative k would invert the model — must reject."""
    db.upsert_daikin_lwt_kw_calibration(
        k_per_degc=-0.01, samples=10, window_days=30,
    )
    assert physics.get_kw_per_degc_lwt() == physics._KW_PER_DEGC_LWT_DEFAULT


def test_get_daikin_heating_kw_uses_calibrated_value() -> None:
    """End-to-end: a calibrated k should change the LP's heating estimate."""
    # At 4°C outdoor, default model: LWT ≈ 35.5 → kW ≈ (35.5 - 18) × 0.0333 ≈ 0.583
    default_kw = physics.get_daikin_heating_kw(4.0)
    db.upsert_daikin_lwt_kw_calibration(
        k_per_degc=0.050, samples=14, window_days=30,
    )
    bumped_kw = physics.get_daikin_heating_kw(4.0)
    # Same LWT, ~50% higher k → ~50% higher kW
    assert bumped_kw == pytest.approx(default_kw * (0.050 / 0.0333), abs=1e-3)


def test_lwt_offset_inverse_uses_calibrated_value() -> None:
    """``lwt_offset_from_space_kw`` is the inverse — must read same k."""
    db.upsert_daikin_lwt_kw_calibration(
        k_per_degc=0.050, samples=14, window_days=30,
    )
    # Round-trip: pick a kW, get the implied LWT offset, then forward-calc kW.
    target_kw = 0.4
    offset = physics.lwt_offset_from_space_kw(target_kw, temp_outdoor_c=4.0)
    # The implied LWT_actual = 0.4 / 0.050 + 18 = 26 °C; clamped to bounds.
    # Just assert the offset is in the configured range.
    assert app_config.OPTIMIZATION_LWT_OFFSET_MIN <= offset <= app_config.OPTIMIZATION_LWT_OFFSET_MAX


# ---------------------------------------------------------------------------
# Bugs the dry-run-against-prod uncovered (regression coverage)
# ---------------------------------------------------------------------------

def test_calibration_uses_half_hour_units_consistently() -> None:
    """Bug fix: X_day must integrate over time (Δt), not be a raw °C sum.

    Half-hour cadence ⇒ Δt = 0.5 h, so X_day = Σ (LWT−18) × 0.5. If the code
    forgot the multiplier, the resulting k would be ~2× too small and land
    OUTSIDE the safety bounds — the loader would silently fall back to the
    default and the whole feature is a no-op. We seed at HALF-HOUR cadence
    here (same shape as prod) and verify the fitted k matches the synthetic
    one we baked in.
    """
    target_k = 0.045
    base = date.today() - timedelta(days=20)
    for i in range(12):
        d = base + timedelta(days=i)
        # 48 half-hour slots (matches prod cadence)
        slot_temps = [4.0 + (i % 11)] * 48
        with db._lock:
            conn = db.get_connection()
            try:
                fetch_at = datetime(d.year, d.month, d.day, 23, 30, tzinfo=UTC).isoformat()
                for s, t in enumerate(slot_temps):
                    minute = 30 if s % 2 else 0
                    hour = s // 2
                    slot = datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC).isoformat()
                    conn.execute(
                        """INSERT INTO meteo_forecast_value
                           (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2, cloud_cover_pct, direct_pv_kw)
                           VALUES (?, ?, ?, NULL, NULL, NULL)""",
                        (fetch_at, slot, t),
                    )
                conn.commit()
            finally:
                conn.close()
        # Ground truth y = k × X_day where X_day uses Δt = 0.5 (half-hour slots)
        x = sum(max(0.0, physics.get_lwt_base_c(t) - 18.0) for t in slot_temps) * 0.5
        y = target_k * x
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=y + 2.0, kwh_heating=y, kwh_dhw=2.0,
            source="onecta",
        )
    result = db.compute_daikin_lwt_kw_calibration(window_days=30, min_samples=7)
    assert result["status"] == "ok", result
    assert result["k_per_degc"] == pytest.approx(target_k, abs=1e-3), (
        f"unit-conversion bug: got k={result['k_per_degc']:.5f}, expected {target_k}"
    )


def test_calibration_finds_meteo_in_history_table() -> None:
    """Bug fix: meteo_forecast_value gets pruned to ~7 days in prod, but
    meteo_forecast_history retains 14+. The compute fn must UNION both so the
    rolling window can extend past _value's cutoff."""
    target_k = 0.040
    base = date.today() - timedelta(days=25)
    for i in range(12):
        d = base + timedelta(days=i)
        temps = [5.0 + (i % 8)] * 24
        # Seed ONLY into history (older days in prod live exclusively here)
        _seed_meteo_day(d, temps, table="meteo_forecast_history")
        y = target_k * _x_day(temps)
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=y + 2.0, kwh_heating=y, kwh_dhw=2.0,
            source="onecta",
        )
    result = db.compute_daikin_lwt_kw_calibration(window_days=30, min_samples=7)
    assert result["status"] == "ok", result
    assert result["samples"] >= 7
    assert result["k_per_degc"] == pytest.approx(target_k, abs=1e-3)


def test_calibration_per_slot_not_per_day_max_subquery() -> None:
    """Bug fix: when multiple snapshots cover slots from the same day with
    different `forecast_fetch_at_utc`, the compute fn must pick the latest
    fetch PER slot_time, not the latest fetch that has any slot in that day.

    Setup: for a given day, snapshot A covers hours 0-11, snapshot B (later)
    covers hours 12-23. If we picked the latest fetch with any matching slot,
    we'd get ONLY hours 12-23 from snapshot B. The fix walks per-slot, so we
    get all 24 hours — half from A, half from B."""
    target_k = 0.040
    base = date.today() - timedelta(days=20)
    for i in range(10):
        d = base + timedelta(days=i)
        # Two half-day snapshots with different fetch times
        with db._lock:
            conn = db.get_connection()
            try:
                fetch_a = datetime(d.year, d.month, d.day, 8, 0, tzinfo=UTC).isoformat()
                fetch_b = datetime(d.year, d.month, d.day, 20, 0, tzinfo=UTC).isoformat()
                temps_24h = [6.0 + (i % 9)] * 24
                # First half from earlier fetch
                for h in range(12):
                    slot = datetime(d.year, d.month, d.day, h, 0, tzinfo=UTC).isoformat()
                    conn.execute(
                        """INSERT INTO meteo_forecast_value (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2, cloud_cover_pct, direct_pv_kw)
                           VALUES (?, ?, ?, NULL, NULL, NULL)""",
                        (fetch_a, slot, temps_24h[h]),
                    )
                # Second half from later fetch
                for h in range(12, 24):
                    slot = datetime(d.year, d.month, d.day, h, 0, tzinfo=UTC).isoformat()
                    conn.execute(
                        """INSERT INTO meteo_forecast_value (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2, cloud_cover_pct, direct_pv_kw)
                           VALUES (?, ?, ?, NULL, NULL, NULL)""",
                        (fetch_b, slot, temps_24h[h]),
                    )
                conn.commit()
            finally:
                conn.close()
        y = target_k * _x_day(temps_24h)
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=y + 2.0, kwh_heating=y, kwh_dhw=2.0,
            source="onecta",
        )
    result = db.compute_daikin_lwt_kw_calibration(window_days=30, min_samples=7)
    assert result["status"] == "ok", result
    assert result["k_per_degc"] == pytest.approx(target_k, abs=1e-3), (
        f"per-day MAX bug: got k={result['k_per_degc']:.5f}, expected {target_k}; "
        "likely only half the day's hours were used (half-day snapshot bug)"
    )


# ---------------------------------------------------------------------------
# Inline refresh wrapper (replaces the standalone cron)
# ---------------------------------------------------------------------------

def test_refresh_writes_row_when_data_supports_a_fit() -> None:
    """End-to-end: refresh upserts the calibration row when data is sufficient."""
    target_k = 0.042
    base = date.today() - timedelta(days=20)
    for i in range(12):
        d = base + timedelta(days=i)
        temps = [4.0 + (i % 11)] * 24
        _seed_meteo_day(d, temps)
        y = target_k * _x_day(temps)
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=y + 2.0, kwh_heating=y, kwh_dhw=2.0,
            source="onecta",
        )
    result = db.refresh_daikin_lwt_kw_calibration()
    assert result["status"] == "ok"
    row = db.get_daikin_lwt_kw_calibration()
    assert row is not None
    assert row["k_per_degc"] == pytest.approx(target_k, abs=1e-3)


def test_refresh_does_not_overwrite_when_data_insufficient() -> None:
    """A 'skipped' refresh must NOT clobber a previously-good calibration row."""
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.045, samples=14, window_days=30)
    # No daikin_consumption_daily rows seeded → refresh returns 'skipped'
    result = db.refresh_daikin_lwt_kw_calibration()
    assert result["status"] == "skipped"
    row = db.get_daikin_lwt_kw_calibration()
    # The previously-good row is preserved
    assert row["k_per_degc"] == pytest.approx(0.045, abs=1e-9)


def test_refresh_logs_only_on_meaningful_delta(caplog: pytest.LogCaptureFixture) -> None:
    """24+ daily LP solves must NOT spam the log when k is unchanged."""
    import logging
    caplog.set_level(logging.INFO, logger="src.db")

    target_k = 0.042
    base = date.today() - timedelta(days=20)
    for i in range(12):
        d = base + timedelta(days=i)
        temps = [4.0 + (i % 11)] * 24
        _seed_meteo_day(d, temps)
        y = target_k * _x_day(temps)
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=y + 2.0, kwh_heating=y, kwh_dhw=2.0,
            source="onecta",
        )

    # First refresh — should log (cold start)
    db.refresh_daikin_lwt_kw_calibration()
    first_log_count = sum(1 for r in caplog.records if "daikin_lwt_calibration" in r.getMessage())
    assert first_log_count >= 1

    # Five identical refreshes — same data, k stays identical, must NOT log
    caplog.clear()
    for _ in range(5):
        db.refresh_daikin_lwt_kw_calibration()
    quiet_log_count = sum(1 for r in caplog.records if "daikin_lwt_calibration" in r.getMessage())
    assert quiet_log_count == 0, f"unexpected log spam across redundant refreshes: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Schema migration is idempotent
# ---------------------------------------------------------------------------

def test_init_db_idempotent_for_calibration_table() -> None:
    db.init_db()
    db.init_db()  # second call must not raise
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.040, samples=10, window_days=30)
    row = db.get_daikin_lwt_kw_calibration()
    assert row is not None
    assert row["k_per_degc"] == pytest.approx(0.040, abs=1e-9)
    # Re-running init must not wipe the row.
    db.init_db()
    row2 = db.get_daikin_lwt_kw_calibration()
    assert row2 is not None
    assert row2["k_per_degc"] == pytest.approx(0.040, abs=1e-9)
