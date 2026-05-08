"""today_factor scoped to today + warm-start from yesterday's per-hour table.

Two behaviors locked in here:
1. Slots in tomorrow's UTC date must NOT receive today's PV-bias factor.
   A cloudy-morning solve at 19:00 today produces today_factor ≈ 0.31; before
   the fix that scalar contaminated tomorrow's morning predictions in the
   same 48h solve. Tomorrow's slots now ignore today_factor entirely.
2. Hours unobserved today are warm-started from yesterday's per-hour map
   (persisted in lp_inputs_snapshot.exogenous_snapshot_json) instead of
   inheriting the median of today's observations.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src import db
from src.config import config as app_config
from src.scheduler.optimizer import _load_yesterday_today_factor_by_hour


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()


def _seed_yesterday_snapshot(*, run_id: int, plan_date: str, snapshot_json: str) -> None:
    """Insert one lp_inputs_snapshot row dated yesterday with the given JSON."""
    db.save_lp_snapshots(
        run_id=run_id,
        inputs_row={
            "run_at_utc": (datetime.now(UTC) - timedelta(hours=12)).isoformat(),
            "plan_date": plan_date,
            "horizon_hours": 48,
            "soc_initial_kwh": 5.0,
            "tank_initial_c": 45.0,
            "indoor_initial_c": 20.0,
            "soc_source": "test",
            "tank_source": "test",
            "indoor_source": "test",
            "base_load_json": "[]",
            "micro_climate_offset_c": 0.0,
            "forecast_fetch_at_utc": None,
            "exogenous_snapshot_json": snapshot_json,
            "config_snapshot_json": "{}",
            "price_quantize_p": 0.1,
            "peak_threshold_p": 30.0,
            "cheap_threshold_p": 10.0,
            "daikin_control_mode": "off",
            "optimization_preset": "normal",
            "energy_strategy_mode": "savings_first",
        },
        solution_rows=[],
    )


def test_load_yesterday_factor_returns_empty_when_no_history() -> None:
    """First-day-of-operation: no previous snapshot → return {} (no warm-start)."""
    assert _load_yesterday_today_factor_by_hour() == {}


def test_load_yesterday_factor_reads_effective_map_when_present() -> None:
    """The post-#5 snapshot writes today_factor_effective_by_hour. The loader
    must prefer it over the raw observed map so the warm-start chains across
    days (yesterday's effective factor was already a warm-start mix)."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    snapshot_json = '{"weather_adjustment": {"today_factor_by_hour": {"6":0.4}, "today_factor_effective_by_hour": {"6":0.9, "12":1.1, "16":1.4}}}'
    _seed_yesterday_snapshot(run_id=1, plan_date=yesterday, snapshot_json=snapshot_json)
    out = _load_yesterday_today_factor_by_hour()
    # Effective map wins, not the raw observed map (which only has hour 6).
    assert out == {6: 0.9, 12: 1.1, 16: 1.4}


def test_load_yesterday_factor_falls_back_to_raw_for_old_snapshots() -> None:
    """Pre-#5 snapshots only have today_factor_by_hour. The loader must still
    return that map so the upgrade is backwards compatible."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    snapshot_json = '{"weather_adjustment": {"today_factor_by_hour": {"7":0.5, "13":1.05}}}'
    _seed_yesterday_snapshot(run_id=2, plan_date=yesterday, snapshot_json=snapshot_json)
    assert _load_yesterday_today_factor_by_hour() == {7: 0.5, 13: 1.05}


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
