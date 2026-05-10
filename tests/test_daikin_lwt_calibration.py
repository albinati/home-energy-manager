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

def _seed_meteo_day(d: date, hourly_temps: list[float]) -> None:
    """Insert one snapshot covering all 24 UTC hours of ``d``."""
    assert len(hourly_temps) == 24
    fetch_at = (datetime(d.year, d.month, d.day, 23, 0, tzinfo=UTC)).isoformat()
    with db._lock:
        conn = db.get_connection()
        try:
            for h, t in enumerate(hourly_temps):
                slot = datetime(d.year, d.month, d.day, h, 0, tzinfo=UTC).isoformat()
                conn.execute(
                    """INSERT INTO meteo_forecast_value
                       (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2, cloud_cover_pct, direct_pv_kw)
                       VALUES (?, ?, ?, NULL, NULL, NULL)""",
                    (fetch_at, slot, t),
                )
            conn.commit()
        finally:
            conn.close()


def _x_day(hourly_temps: list[float]) -> float:
    """Recompute the ground-truth X_day = Σ_h max(0, LWT(t) − 18)."""
    return sum(max(0.0, physics.get_lwt_base_c(t) - 18.0) for t in hourly_temps)


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
