"""Today-aware PV calibration adjuster (OCF-style).

Live diagnostic 2026-05-02 16:20 BST showed the per-hour calibration table
was over-correcting by ~2-3×: hour 14 UTC table factor 0.166 multiplied a
2.59 kW raw forecast → 0.43 kW, but reality at 14:38 was 0.98 kW. The
14-day rolling table is dominated by the day-mix in the window and can't
adapt to today's specific conditions.

Fix: compute today's morning observed/forecast ratio, apply as a global
multiplier on top of the per-hour table. Inspired by OCF Quartz Solar
Forecast adjuster pattern.
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


def _seed_realtime(hour_utc: int, kw: float, n_samples: int = 12) -> None:
    """Seed n_samples 5-min PV samples at the given hour UTC for today."""
    today = datetime.now(UTC).date()
    for i in range(n_samples):
        ts = datetime.combine(today, datetime.min.time()).replace(
            hour=hour_utc, minute=i * 5, tzinfo=UTC,
        )
        db.save_pv_realtime_sample(
            captured_at=ts.isoformat().replace("+00:00", "Z"),
            solar_power_kw=kw,
            soc_pct=50.0, load_power_kw=0.5,
            grid_import_kw=0.0, grid_export_kw=kw,
            battery_charge_kw=0.0, battery_discharge_kw=0.0,
            source="seed",
        )


def _seed_forecast(hour_utc: int, irradiance_wm2: float) -> None:
    """Seed a meteo_forecast row at the given hour UTC for today."""
    today = datetime.now(UTC).date()
    ts = datetime.combine(today, datetime.min.time()).replace(
        hour=hour_utc, tzinfo=UTC,
    )
    db.save_meteo_forecast(
        [{
            "slot_time": ts.isoformat(),
            "temp_c": 15.0,
            "solar_w_m2": irradiance_wm2,
            # Pin cloud cover so ``forecast_pv_kw_from_row`` does not apply
            # the default 50% attenuation the new transform uses when
            # cloud_cover_pct is absent — these tests reason about the raw
            # irradiance → kW conversion.
            "cloud_cover_pct": 0.0,
        }],
        today.isoformat(),
    )


def test_no_data_returns_neutral_factor() -> None:
    """No pv_realtime_history yet today → factor 1.0 (no adjustment)."""
    from src.weather import compute_today_pv_correction_factor
    f, diag = compute_today_pv_correction_factor()
    assert f == 1.0
    assert "no pv_realtime_history" in diag["reason"]


def test_no_forecast_returns_neutral() -> None:
    """Has actuals but no forecast → factor 1.0 (can't compute ratio)."""
    from src.weather import compute_today_pv_correction_factor
    _seed_realtime(hour_utc=10, kw=2.0)
    f, diag = compute_today_pv_correction_factor()
    assert f == 1.0
    assert "no meteo_forecast" in diag["reason"] or "insufficient" in diag.get("reason", "").lower() or "only" in diag.get("reason", "").lower()


def test_insufficient_daylight_hours_returns_neutral() -> None:
    """Need ≥ min_hours of paired daylight data to trust the ratio."""
    from src.weather import compute_today_pv_correction_factor
    # Only 1 hour with both actual + forecast → not enough
    _seed_realtime(hour_utc=10, kw=2.0)
    _seed_forecast(hour_utc=10, irradiance_wm2=800.0)
    f, diag = compute_today_pv_correction_factor()
    assert f == 1.0
    assert "only 1 daylight" in diag["reason"]


def test_two_hours_cloudy_morning_scales_down() -> None:
    """Cloudy morning: actual ~50% of forecast → factor ≈ 0.5."""
    from src.weather import compute_today_pv_correction_factor
    # Forecast says 800 W/m² → ~3.06 kW peak. Actual measured 1.5 kW (~50%).
    for h in (10, 11):
        _seed_realtime(hour_utc=h, kw=1.5)
        _seed_forecast(hour_utc=h, irradiance_wm2=800.0)
    f, diag = compute_today_pv_correction_factor()
    assert 0.4 <= f <= 0.6, f"Expected ~0.5, got {f}; diag={diag}"
    assert diag["n_hours"] == 2


def test_sunny_morning_scales_up() -> None:
    """Sunny morning: actual ~150% of forecast → factor ≈ 1.5."""
    from src.weather import compute_today_pv_correction_factor
    # Forecast says 400 W/m² → ~1.53 kW. Actual measured 2.3 kW (~150%).
    for h in (10, 11):
        _seed_realtime(hour_utc=h, kw=2.3)
        _seed_forecast(hour_utc=h, irradiance_wm2=400.0)
    f, diag = compute_today_pv_correction_factor()
    assert 1.4 <= f <= 1.6, f"Expected ~1.5, got {f}; diag={diag}"


def test_safety_clamp_blocks_extreme_factors() -> None:
    """Even if observations say 10× the forecast, clamp to safe range (default 0.30 - 2.0)."""
    from src.weather import compute_today_pv_correction_factor
    for h in (10, 11):
        _seed_realtime(hour_utc=h, kw=10.0)  # absurdly high
        _seed_forecast(hour_utc=h, irradiance_wm2=200.0)  # forecast: 0.77 kW
    f, diag = compute_today_pv_correction_factor()
    assert f == 2.0, f"Should clamp to 2.0, got {f}"
    assert diag["clamped"] is True


def test_dawn_dusk_hours_excluded() -> None:
    """Tiny actual + tiny forecast (< 0.05 kWh) should be excluded as noise."""
    from src.weather import compute_today_pv_correction_factor
    # Two daylight hours with real signal
    _seed_realtime(hour_utc=10, kw=2.0)
    _seed_forecast(hour_utc=10, irradiance_wm2=600.0)
    _seed_realtime(hour_utc=11, kw=2.0)
    _seed_forecast(hour_utc=11, irradiance_wm2=600.0)
    # Plus one dusk hour with negligible signal
    _seed_realtime(hour_utc=20, kw=0.01)
    _seed_forecast(hour_utc=20, irradiance_wm2=2.0)
    f, diag = compute_today_pv_correction_factor()
    assert diag["n_hours"] == 2, f"Dusk hour should be excluded: {diag}"


# ---------------------------------------------------------------------------
# Per-hour today-aware adjuster — addresses asymmetric morning/afternoon bias
# the scalar adjuster averages away.
# ---------------------------------------------------------------------------


def test_per_hour_returns_observed_ratios_and_imputes_unobserved():
    """Two observed hours produce per-hour factors; the rest get the median."""
    from src.weather import compute_today_pv_correction_factor_by_hour

    # Morning ratio = 1.5 (sunny morning beats forecast); midday spot-on.
    _seed_realtime(hour_utc=9, kw=2.0)   # forecast → 1.33; ratio = 1.5
    _seed_forecast(hour_utc=9, irradiance_wm2=350.0)
    _seed_realtime(hour_utc=11, kw=2.0)
    _seed_forecast(hour_utc=11, irradiance_wm2=525.0)  # forecast → 2.0; ratio = 1.0

    by_hour, diag = compute_today_pv_correction_factor_by_hour()
    assert by_hour, f"expected non-empty map; diag={diag}"
    assert diag["n_observed"] == 2
    # Observed hours get their own ratios.
    assert 9 in diag["ratios_per_hour"]
    assert 11 in diag["ratios_per_hour"]
    # Unobserved hours get the median (between 1.0 and 1.5 → median 1.5
    # because there are only 2 values; sorted_obs[1] = 1.5). ``median_ratio``
    # in diag is rounded to 4 places; ``by_hour`` values carry full precision.
    median = diag["median_ratio"]
    assert by_hour[3] == pytest.approx(median, abs=1e-3)
    assert by_hour[14] == pytest.approx(median, abs=1e-3)
    # Observed hour ratios are NOT the median (asymmetry preserved).
    assert by_hour[9] != by_hour[11], f"observed hours should keep their own factor: {by_hour}"


def test_per_hour_zero_forecast_clamps_to_upper_bound():
    """A 'forecast said nothing, reality produced PV' hour clamps to the
    upper safety bound rather than returning NaN/inf."""
    from src.weather import compute_today_pv_correction_factor_by_hour

    # Two observed hours so we cross min_hours.
    _seed_realtime(hour_utc=9, kw=1.0)
    _seed_forecast(hour_utc=9, irradiance_wm2=300.0)
    _seed_realtime(hour_utc=10, kw=1.0)
    _seed_forecast(hour_utc=10, irradiance_wm2=0.5)  # near-zero forecast

    by_hour, diag = compute_today_pv_correction_factor_by_hour()
    assert by_hour
    # Hour 10's forecast was effectively zero → clamped to upper bound 2.0.
    assert by_hour[10] == 2.0
    assert 10 in diag["clamped_hours"]


def test_per_hour_returns_empty_when_only_one_hour_observed():
    """Falls back to scalar adjuster when fewer than min_hours observed."""
    from src.weather import compute_today_pv_correction_factor_by_hour

    _seed_realtime(hour_utc=9, kw=1.0)
    _seed_forecast(hour_utc=9, irradiance_wm2=300.0)

    by_hour, diag = compute_today_pv_correction_factor_by_hour()
    assert by_hour == {}, "single observation should not produce a per-hour map"
    assert "min_hours_required" in diag


# ---------------------------------------------------------------------------
# PR-J: median computation fixes
# Bug 1: clamped values were anchoring the median at clamp bounds.
# Bug 2: ``sorted[len//2]`` is upper of two middles for even-length lists.
# ---------------------------------------------------------------------------


def test_per_hour_median_excludes_clamped_observations():
    """Two valid + two clamped observations → median computed only from valid pair.

    Repro of the 2026-05-06 prod state: observations at hours 5/6/7/9 produced
    ratios [2.0(clamp), 0.67, 1.10, 0.30(clamp)]. The old code returned 1.10
    (sorted[2] of all 4); after this fix it should return (0.67+1.10)/2 = 0.885
    by excluding the two clamped values.
    """
    from src.weather import compute_today_pv_correction_factor_by_hour

    # Hour 5: dawn, tiny forecast → ratio explodes → clamped to 2.0 (upper).
    _seed_realtime(hour_utc=5, kw=0.10)
    _seed_forecast(hour_utc=5, irradiance_wm2=2.0)
    # Hour 6: ratio ~0.67 (within clamp range).
    _seed_realtime(hour_utc=6, kw=1.0)
    _seed_forecast(hour_utc=6, irradiance_wm2=440.0)  # cal ~1.495 kW; ratio ~0.67
    # Hour 7: ratio ~1.10.
    _seed_realtime(hour_utc=7, kw=2.0)
    _seed_forecast(hour_utc=7, irradiance_wm2=475.0)  # cal ~1.815 kW; ratio ~1.10
    # Hour 9: heavy cloud morning, ratio at lower clamp.
    _seed_realtime(hour_utc=9, kw=0.10)
    _seed_forecast(hour_utc=9, irradiance_wm2=350.0)  # cal ~1.34 kW; ratio ~0.075 → clamp 0.30

    by_hour, diag = compute_today_pv_correction_factor_by_hour()
    assert by_hour, f"expected non-empty map; diag={diag}"
    # Two of four observations were clamped (hours 5 and 9).
    assert set(diag["clamped_hours"]) == {5, 9}, f"clamped_hours: {diag['clamped_hours']}"
    # Median should reflect ONLY the unclamped {6, 7} → mean(0.67, 1.10) ≈ 0.885.
    assert diag["median_source"] == "unclamped_only"
    assert 0.80 <= diag["median_ratio"] <= 0.95, f"expected ~0.89; got {diag['median_ratio']}"


def test_per_hour_median_falls_back_when_too_few_unclamped():
    """If excluding clamped leaves <min_hours observations, fall back to all-observed.

    Otherwise we'd return an empty map when most of the day's data was at clamp
    boundaries — losing the signal entirely.
    """
    from src.weather import compute_today_pv_correction_factor_by_hour

    # Two clamped observations, no unclamped → unclamped subset has 0 entries.
    _seed_realtime(hour_utc=5, kw=0.10)
    _seed_forecast(hour_utc=5, irradiance_wm2=2.0)  # tiny forecast → clamp upper
    _seed_realtime(hour_utc=18, kw=0.10)
    _seed_forecast(hour_utc=18, irradiance_wm2=2.0)  # tiny forecast → clamp upper

    by_hour, diag = compute_today_pv_correction_factor_by_hour()
    assert by_hour, f"expected non-empty map (fallback to all-observed); diag={diag}"
    assert diag["median_source"] == "all_observed", f"diag: {diag}"


def test_scalar_uses_statistical_median_for_even_length():
    """Scalar adjuster: 4 observations [0.4, 0.6, 1.4, 1.6] → median = 1.0,
    not ``sorted[2] = 1.4``."""
    from src.weather import compute_today_pv_correction_factor

    # Calibrated forecast for hours 6/7/8/9 with ratios 0.4 / 0.6 / 1.4 / 1.6
    # against measured kW.
    seeds = [
        (6,  0.4, 1.0),  # actual / forecast = 0.4 → ratio 0.4 (clamped if outside range)
        (7,  0.6, 1.0),
        (8,  1.4, 1.0),
        (9,  1.6, 1.0),
    ]
    # We want to produce ratios [0.4, 0.6, 1.4, 1.6] so the calibrated forecast
    # for each hour must be ~1.0 kW. estimate_pv_kw(rad) × cloud_attenuation
    # × calibration ≈ 1.0 kW. Easiest: seed irradiance + a flat hourly cal of 1.0.
    db.upsert_pv_calibration_hourly(
        {h: 1.0 for h in range(6, 19)},
        {h: 50 for h in range(6, 19)},
        window_days=14,
    )
    for h, actual_kw, _ in seeds:
        _seed_realtime(hour_utc=h, kw=actual_kw)
    # Need irradiance such that estimate_pv_kw(att·rad) ≈ 1.0 → 4.5 × (rad/1000) × 0.85 = 1.0
    # → rad ≈ 261. With cloud=0% (passed via _seed_forecast helper), att=1.0.
    for h, _, _ in seeds:
        _seed_forecast(hour_utc=h, irradiance_wm2=261.0)

    factor, diag = compute_today_pv_correction_factor()
    # True median of [0.4, 0.6, 1.4, 1.6] = (0.6 + 1.4) / 2 = 1.0
    # Old (buggy) code returned sorted[2] = 1.4
    assert diag["median_ratio"] == pytest.approx(1.0, abs=0.05), (
        f"expected statistical median 1.0; got {diag['median_ratio']}"
    )
