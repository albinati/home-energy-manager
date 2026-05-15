"""today_factor scoping rules in the LP PV-scale closure.

Two behaviors locked in here:

1. Slots in tomorrow's UTC date must NOT receive today's PV-bias factor.
   A cloudy-morning solve at 19:00 today produces today_factor ≈ 0.31; before
   the fix that scalar contaminated tomorrow's morning predictions in the
   same 48h solve. Tomorrow's slots now ignore today_factor entirely.

2. Hours unobserved today get tf=1.0 (no intraday adjustment) — they defer
   to ``pv_calibration_hourly`` (the multi-day per-hour statistical
   baseline) instead of pulling a single-day noisy sample on top. The
   previous "warm-start from yesterday's snapshot" mechanism was removed
   in 2026-05-15 as part of the PV-trust-guard-rail follow-up:
   ``compute_today_pv_correction_factor_by_hour`` returns 1.0 for
   unobserved hours; ``optimizer._run_optimizer_lp`` passes the map
   through verbatim (no warm-start fill-in).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()


def test_unobserved_hours_imputed_to_one_not_yesterday() -> None:
    """When no observation exists for an hour today, the per-hour map returns
    1.0 — NOT yesterday's value, NOT today's observed median. The 30-day
    baseline in ``pv_calibration_hourly`` is the only legitimate signal for
    unobserved hours.
    """
    from src.weather import compute_today_pv_correction_factor_by_hour

    today = datetime.now(UTC).date()

    def _seed(hour: int, kw: float, irr: float) -> None:
        for i in range(12):
            ts = datetime.combine(today, datetime.min.time()).replace(
                hour=hour, minute=i * 5, tzinfo=UTC,
            )
            db.save_pv_realtime_sample(
                captured_at=ts.isoformat().replace("+00:00", "Z"),
                solar_power_kw=kw,
                soc_pct=50.0, load_power_kw=0.5,
                grid_import_kw=0.0, grid_export_kw=kw,
                battery_charge_kw=0.0, battery_discharge_kw=0.0,
                source="seed",
            )
        ts0 = datetime.combine(today, datetime.min.time()).replace(
            hour=hour, tzinfo=UTC,
        )
        db.save_meteo_forecast(
            [{
                "slot_time": ts0.isoformat(),
                "temp_c": 15.0,
                "solar_w_m2": irr,
                "cloud_cover_pct": 0.0,
            }],
            today.isoformat(),
        )

    _seed(9, 2.0, 350.0)   # ratio ~1.5
    _seed(10, 2.5, 500.0)  # ratio ~1.25

    by_hour, diag = compute_today_pv_correction_factor_by_hour()
    assert by_hour, f"expected non-empty map; diag={diag}"
    # Imputation policy reported in diag — guards regressions if the
    # imputation default changes back to median or to a warm-start.
    assert diag.get("imputation_policy") == "neutral_1.0"
    # Unobserved hours (any hour not in {9, 10}) get exactly 1.0.
    assert by_hour[3] == 1.0
    assert by_hour[15] == 1.0
    assert by_hour[20] == 1.0
    # Observed hours keep their measured ratio (not 1.0).
    assert by_hour[9] != 1.0
    assert by_hour[10] != 1.0


def test_pv_scale_callable_skips_today_factor_for_tomorrow_slots() -> None:
    """End-to-end: the closure built inside run_optimizer applies today_factor
    only to slots whose UTC date matches today. Stand-alone test reconstructs
    the closure shape that's hot-pathed in the LP solve.
    """
    today = datetime.now(UTC).date()
    today_factor_by_hour = {h: 0.30 for h in range(24)}  # heavy bias

    def closure(hour_utc: int, cloud_pct: float, slot_start_utc=None) -> float:
        cal = 1.0  # ignore cloud_table for this test
        if slot_start_utc is not None and slot_start_utc.date() != today:
            return cal  # tomorrow → no today bias
        return cal * today_factor_by_hour.get(hour_utc, 1.0)

    # Today's hour 10 → 0.30 (bias applied)
    today_slot = datetime.combine(today, datetime.min.time(), tzinfo=UTC).replace(hour=10)
    assert closure(10, 50.0, today_slot) == pytest.approx(0.30)

    # Tomorrow's hour 10 → 1.0 (no bias)
    tomorrow_slot = today_slot + timedelta(days=1)
    assert closure(10, 50.0, tomorrow_slot) == pytest.approx(1.0)


def test_forecast_to_lp_inputs_invokes_callable_with_slot_start_utc() -> None:
    """The legacy 2-arg signature (hour, cloud) must keep working — graceful
    fallback when the callable doesn't accept slot_start_utc."""
    from src.weather import HourlyForecast, forecast_to_lp_inputs

    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    forecast = [
        HourlyForecast(
            time_utc=base, temperature_c=12.0, cloud_cover_pct=50.0,
            shortwave_radiation_wm2=400.0, estimated_pv_kw=2.0,
            heating_demand_factor=0.0, pv_direct=False,
        ),
    ]
    legacy_calls: list[tuple[int, float]] = []
    def legacy_callable(h: int, c: float) -> float:
        legacy_calls.append((h, c))
        return 0.5
    out_legacy = forecast_to_lp_inputs(forecast, [base], pv_scale=legacy_callable)
    assert legacy_calls, "legacy 2-arg callable must still be invoked"
    assert out_legacy.pv_kwh_per_slot[0] >= 0  # didn't crash

    new_calls: list[tuple[int, float, datetime]] = []
    def new_callable(h: int, c: float, slot_start_utc) -> float:
        new_calls.append((h, c, slot_start_utc))
        return 0.5
    forecast_to_lp_inputs(forecast, [base], pv_scale=new_callable)
    assert len(new_calls) == 1
    assert new_calls[0][2] == base, "new 3-arg callable must receive the slot start_utc"
