"""Regression-baseline evaluator for PV forecast accuracy.

Captures MAE/RMSE/bias per hour and overall. Used to compare any
calibration improvement (cloud-aware bias, MOS regression, etc.) against
the post-#230 baseline. A real improvement reduces these metrics; a
no-op change leaves them unchanged.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


def _seed_day_with_samples(d: date, hour_kw: dict[int, float],
                            forecast_irr: dict[int, float]) -> None:
    """Seed a day's worth of measurements + forecast for given hours."""
    for h, kw in hour_kw.items():
        for minute in (0, 15, 30, 45):
            ts = datetime.combine(d, datetime.min.time()).replace(hour=h, minute=minute, tzinfo=UTC)
            db.save_pv_realtime_sample(
                captured_at=ts.isoformat().replace("+00:00", "Z"),
                solar_power_kw=kw, soc_pct=50.0, load_power_kw=0.5,
                grid_import_kw=0.0, grid_export_kw=kw,
                battery_charge_kw=0.0, battery_discharge_kw=0.0, source="seed",
            )
    rows = []
    for h, irr in forecast_irr.items():
        ts = datetime.combine(d, datetime.min.time()).replace(hour=h, tzinfo=UTC)
        rows.append({"slot_time": ts.isoformat(), "temp_c": 15.0, "solar_w_m2": irr, "cloud_cover_pct": 0.0})
    db.save_meteo_forecast(rows, d.isoformat())


def test_evaluator_returns_zero_when_no_data() -> None:
    from src.weather import evaluate_pv_forecast_accuracy
    out = evaluate_pv_forecast_accuracy(window_days=7)
    assert out["n_paired"] == 0
    assert out["overall"]["n"] == 0


def test_perfect_prediction_zero_error() -> None:
    """Forecast 800 W/m² → estimate_pv_kw ≈ 3.06 kW, calibration 1.0 → predicted 3.06.
    If actual is also 3.06 kW: MAE/RMSE/bias all ~0."""
    from src.weather import evaluate_pv_forecast_accuracy

    # No per-hour calibration table → uses flat calibration
    today = date.today()
    yesterday = today - timedelta(days=1)
    # Calibrate predicted to actual: pick irradiance such that estimate_pv_kw × flat_cal = 3.06
    # estimate_pv_kw(800) = 4.5 × 0.8 × 0.85 = 3.06
    # We want actual = 3.06 too. With flat cal computed from history... too complex.
    # Simpler: insert a calibration factor 1.0 directly so predicted == raw forecast.
    db.upsert_pv_calibration_hourly({h: 1.0 for h in range(6, 19)},
                                     {h: 50 for h in range(6, 19)}, window_days=14)
    _seed_day_with_samples(yesterday, hour_kw={12: 3.06}, forecast_irr={12: 800.0})

    out = evaluate_pv_forecast_accuracy(window_days=2)
    assert out["n_paired"] >= 1
    assert out["overall"]["mae_kw"] < 0.1  # near-zero
    assert abs(out["overall"]["bias_kw"]) < 0.1


def test_systematic_under_prediction_shows_positive_bias() -> None:
    """Forecast says 1.5 kW but reality is 3.0 kW (system under-predicts).
    bias_kw should be positive (actual > predicted)."""
    from src.weather import evaluate_pv_forecast_accuracy

    db.upsert_pv_calibration_hourly({h: 1.0 for h in range(6, 19)},
                                     {h: 50 for h in range(6, 19)}, window_days=14)
    yesterday = date.today() - timedelta(days=1)
    # estimate_pv_kw(400) = 1.53 kW. Reality is 3.0 kW.
    _seed_day_with_samples(yesterday, hour_kw={12: 3.0}, forecast_irr={12: 400.0})

    out = evaluate_pv_forecast_accuracy(window_days=2)
    assert out["overall"]["bias_kw"] > 0.5, (
        f"Expected positive bias (actual > predicted), got {out['overall']['bias_kw']}"
    )


def test_per_hour_breakdown_present() -> None:
    """The per_hour dict should have one entry per hour with measurements."""
    from src.weather import evaluate_pv_forecast_accuracy

    db.upsert_pv_calibration_hourly({h: 1.0 for h in range(6, 19)},
                                     {h: 50 for h in range(6, 19)}, window_days=14)
    yesterday = date.today() - timedelta(days=1)
    _seed_day_with_samples(yesterday,
                            hour_kw={10: 1.5, 12: 3.0, 14: 2.5},
                            forecast_irr={10: 500.0, 12: 800.0, 14: 700.0})

    out = evaluate_pv_forecast_accuracy(window_days=2)
    assert set(out["per_hour"].keys()) == {10, 12, 14}
    for h, stats in out["per_hour"].items():
        assert stats["n"] >= 1
        for k in ("mae_kw", "rmse_kw", "bias_kw", "mean_actual_kw", "mean_pred_kw"):
            assert k in stats


def test_dawn_dusk_excluded_when_both_below_threshold() -> None:
    """Both predicted AND actual < 0.05 kW → exclude (dawn/dusk noise)."""
    from src.weather import evaluate_pv_forecast_accuracy

    db.upsert_pv_calibration_hourly({h: 1.0 for h in range(0, 24)},
                                     {h: 50 for h in range(0, 24)}, window_days=14)
    yesterday = date.today() - timedelta(days=1)
    # Tiny dawn samples: predicted estimate_pv_kw(5) = 0.019 kW, actual 0.01 kW — both excluded
    _seed_day_with_samples(yesterday,
                            hour_kw={5: 0.01, 12: 2.0},
                            forecast_irr={5: 5.0, 12: 600.0})

    out = evaluate_pv_forecast_accuracy(window_days=2, min_kw=0.05)
    # Hour 5 should be excluded (both < 0.05)
    assert 5 not in out["per_hour"]
    # Hour 12 still present
    assert 12 in out["per_hour"]
