"""db.half_hourly_residual_load_profile_kwh — Daikin-subtracted load profile.

S10.13 (#179): the LP energy balance ``imp + pv + dis == base_load + exp +
chg + (e_dhw + e_space)`` adds physics-predicted Daikin on top of base_load.
Once base_load source switched to real Fox load_power_kw (#176) — which is
HOUSE TOTAL including Daikin's actual past draw — Daikin gets counted twice.
This profile subtracts the same physics estimator the LP uses for prediction
to back out the residual (non-Daikin) load.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _seed_pv(t: datetime, load_kw: float) -> None:
    db.save_pv_realtime_sample(t.isoformat().replace("+00:00", "Z"), load_power_kw=load_kw)


def _seed_meteo(t: datetime, temp_c: float) -> None:
    """Seed a meteo_forecast_history row at the hour boundary so SUBSTR matching works."""
    hour_iso = t.replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "+00:00")
    db.save_meteo_forecast_history(
        (t - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        [{"slot_time": hour_iso, "temp_c": temp_c, "solar_w_m2": 0.0}],
    )


def test_warm_outdoor_no_daikin_subtraction() -> None:
    """At outdoor > DAIKIN_WEATHER_CURVE_HIGH_C (default 18), physics yields zero
    Daikin draw → residual equals raw load (minus DHW standing-loss only)."""
    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0)
    _seed_meteo(base, temp_c=25.0)  # warm — Daikin off
    _seed_pv(base, load_kw=0.6)
    _seed_pv(base + timedelta(minutes=10), load_kw=0.6)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    # Bucket should be near load × 0.5h = 0.3 kWh (DHW standing loss tiny, < 0.05 kWh)
    found = [v for v in profile.values() if 0.25 < v < 0.31]
    assert found, f"expected at least one bucket near 0.3 kWh (no Daikin subtraction); got {sorted(set(round(v,3) for v in profile.values()))[:8]}"


def test_cold_outdoor_subtracts_daikin_space() -> None:
    """At cold outdoor (5 °C), the climate curve predicts non-trivial space heating
    draw → residual < raw load."""
    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0)
    _seed_meteo(base, temp_c=5.0)  # cold
    _seed_pv(base, load_kw=1.5)
    _seed_pv(base + timedelta(minutes=10), load_kw=1.5)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    # Raw load × 0.5h = 0.75 kWh. With Daikin space subtracted, residual must be < 0.75.
    found_residuals = [v for v in profile.values() if 0.0 <= v < 0.75]
    assert found_residuals, (
        f"expected residual buckets < 0.75 kWh after Daikin subtraction; "
        f"profile values: {sorted(set(round(v,3) for v in profile.values()))[:8]}"
    )


def test_no_outdoor_data_drops_sample_not_pollutes_residual(caplog) -> None:
    """Sample without ANY outdoor temp (history OR latest meteo_forecast) is
    dropped, not folded in with a 25 °C sentinel. Prevents the old bug where
    missing meteo silently included un-subtracted Daikin in the residual.
    """
    import logging

    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0)
    # No meteo seeded — both history AND latest fall through.
    _seed_pv(base, load_kw=0.4)
    _seed_pv(base + timedelta(minutes=10), load_kw=0.4)

    with caplog.at_level(logging.INFO, logger="src.db"):
        profile = db.half_hourly_residual_load_profile_kwh(window_days=14)

    # The (h, m) bucket for `base` must NOT carry a residual ≈ raw load × 0.5h
    # (the old 25 °C-sentinel pollution). It should have fallen back hour-aware
    # — and since no other buckets have data either, it lands on the global
    # fallback which is computed from execution_log, not from the polluted PV
    # row.
    h, m = base.astimezone(ZoneInfo(db.config.BULLETPROOF_TIMEZONE)).hour, (
        30 if base.minute >= 30 else 0
    )
    assert (h, m) in profile  # bucket exists
    # The polluted "0.2 kWh" value is what the old code would emit. Assert
    # we don't see it.
    assert not (0.18 < profile[(h, m)] < 0.22), (
        "regression: 25 °C sentinel still polluting residual at the sample's bucket"
    )

    # Coverage log line emitted with non-zero drop count.
    drops = [r for r in caplog.records if "residual_profile" in r.getMessage() and "dropped" in r.getMessage()]
    assert drops, "expected residual_profile coverage log line"
    assert "2 dropped" in drops[-1].getMessage(), drops[-1].getMessage()


def test_latest_meteo_forecast_used_when_history_missing() -> None:
    """Tier-1 fallback: when meteo_forecast_history misses but the latest
    meteo_forecast (per-slot) covers the same hour-of-day, use it. Daikin
    subtraction still happens — residual is NOT raw load."""
    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0, minute=0)
    hour_iso = base.isoformat().replace("+00:00", "Z")

    # NO history row — only latest meteo_forecast carries the temp.
    db.save_meteo_forecast(
        [{"slot_time": hour_iso, "temp_c": 5.0, "solar_w_m2": 0.0}],
        forecast_date=base.date().isoformat(),
    )
    _seed_pv(base, load_kw=1.5)
    _seed_pv(base + timedelta(minutes=10), load_kw=1.5)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    h = base.astimezone(ZoneInfo(db.config.BULLETPROOF_TIMEZONE)).hour
    bucket_val = profile.get((h, 0))
    assert bucket_val is not None
    # At 5 °C, Daikin space draw is non-trivial. Raw load × 0.5 = 0.75 kWh;
    # residual must be strictly less because Daikin was subtracted.
    assert bucket_val < 0.75, (
        f"latest meteo_forecast tier did not subtract Daikin at 5 °C: "
        f"residual={bucket_val:.4f} (≥ raw 0.75)"
    )


def test_empty_bucket_uses_hour_aware_fallback_not_global_median() -> None:
    """Bug A fix: an empty bucket inherits a same-hour or ±2h neighbour value,
    NOT the global median across all hours.

    Seed a low-load early-morning bucket (05:00) and a separate high-load
    mid-day bucket (15:00). The unseeded 04:00 bucket must inherit from the
    05:00 neighbour (low), not the 15:00 mid-day value (high).
    """
    base_day = (datetime.now(UTC) - timedelta(days=2)).replace(microsecond=0)
    tz = ZoneInfo(db.config.BULLETPROOF_TIMEZONE)
    # Build sample times anchored on local hours so bucket math is direct.
    early = base_day.astimezone(tz).replace(hour=5, minute=0, second=0).astimezone(UTC)
    midday = base_day.astimezone(tz).replace(hour=15, minute=0, second=0).astimezone(UTC)

    # Both with warm meteo so no Daikin subtraction muddies the comparison.
    _seed_meteo(early, temp_c=25.0)
    _seed_meteo(midday, temp_c=25.0)
    _seed_pv(early, load_kw=0.20)              # low early-morning load → 0.10 kWh residual
    _seed_pv(early + timedelta(minutes=10), load_kw=0.20)
    _seed_pv(midday, load_kw=2.00)             # high mid-day load → 1.00 kWh residual
    _seed_pv(midday + timedelta(minutes=10), load_kw=2.00)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    # 04:00 is empty — should inherit from 05:00 neighbour (≈ 0.10), NOT 15:00 (≈ 1.00).
    val_04 = profile[(4, 0)]
    val_05 = profile[(5, 0)]
    val_15 = profile[(15, 0)]
    assert abs(val_04 - val_05) < abs(val_04 - val_15), (
        f"empty 04:00 bucket inherited mid-day value, not neighbour: "
        f"04:00={val_04:.3f} 05:00={val_05:.3f} 15:00={val_15:.3f}"
    )
    # And it should be much closer to the early value than the mid-day value.
    assert val_04 < 0.5, f"04:00={val_04:.3f} looks like global-median pollution"


def test_other_half_of_same_hour_preferred_over_band() -> None:
    """Bug A fix tier ordering: when 05:30 is empty but 05:00 has data and
    07:00 has very different data, the 05:30 bucket should pick 05:00 (same
    hour) over 07:00 (band)."""
    base_day = (datetime.now(UTC) - timedelta(days=2)).replace(microsecond=0)
    tz = ZoneInfo(db.config.BULLETPROOF_TIMEZONE)
    same_hour = base_day.astimezone(tz).replace(hour=5, minute=0, second=0).astimezone(UTC)
    band_hour = base_day.astimezone(tz).replace(hour=7, minute=0, second=0).astimezone(UTC)

    _seed_meteo(same_hour, temp_c=25.0)
    _seed_meteo(band_hour, temp_c=25.0)
    _seed_pv(same_hour, load_kw=0.10)                     # 0.05 kWh residual
    _seed_pv(same_hour + timedelta(minutes=10), load_kw=0.10)
    _seed_pv(band_hour, load_kw=1.50)                     # 0.75 kWh residual
    _seed_pv(band_hour + timedelta(minutes=10), load_kw=1.50)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    val_05_30 = profile[(5, 30)]
    val_05_00 = profile[(5, 0)]
    val_07_00 = profile[(7, 0)]
    assert abs(val_05_30 - val_05_00) < abs(val_05_30 - val_07_00), (
        f"05:30 fallback did not prefer same-hour 05:00 over 07:00 band: "
        f"05:30={val_05_30:.3f} 05:00={val_05_00:.3f} 07:00={val_07_00:.3f}"
    )


def test_residual_never_negative() -> None:
    """Subtracted Daikin estimate must not push residual below zero."""
    base = datetime.now(UTC) - timedelta(days=2)
    base = base.replace(microsecond=0)
    # Very cold → Daikin estimate will be largeish; very small load
    _seed_meteo(base, temp_c=-10.0)
    _seed_pv(base, load_kw=0.05)  # tiny load
    _seed_pv(base + timedelta(minutes=10), load_kw=0.05)

    profile = db.half_hourly_residual_load_profile_kwh(window_days=14)
    assert all(v >= 0 for v in profile.values()), (
        f"residual went negative: {[(k,v) for k,v in profile.items() if v<0]}"
    )
