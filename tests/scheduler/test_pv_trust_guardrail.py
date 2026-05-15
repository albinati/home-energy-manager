"""Tests for the PV-trust guard rail (incident 2026-05-15).

Covers:
- ``compute_pv_trust_bias`` — percentile maths, clamps, empty-DB fallbacks.
- ``evaluate_pv_sufficiency_guard`` — fires only in strict_savings; targets
  the right slot indices (today, pre-peak); the demand vs forecast inequality.
- End-to-end through ``solve_lp`` — when the guard fires, the LP solution
  obeys ``chg[i] <= pv_use[i]`` on the targeted slots.

The solve_lp E2E tests use a minimal but realistic 8-slot horizon so they
finish in well under a second.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.scheduler.pv_trust import (
    PvSufficiencyGuardDiag,
    compute_pv_trust_bias,
    evaluate_pv_sufficiency_guard,
)
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _init_db():
    _db.init_db()


# ---------------------------------------------------------------------------
# compute_pv_trust_bias
# ---------------------------------------------------------------------------

def _insert_skill_row(conn: sqlite3.Connection, date_iso: str, hour: int, pred: float, actual: float) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO forecast_skill_log
           (date_utc, hour_of_day, predicted_pv_kwh, actual_pv_kwh, built_at_utc)
           VALUES (?, ?, ?, ?, ?)""",
        (date_iso, hour, pred, actual, datetime.now(UTC).isoformat()),
    )


def _seed_skill_log(daily_ratios: list[tuple[str, float, float]]) -> None:
    """Each tuple: (date_iso, daily_pred_kwh, daily_actual_kwh). Splits the
    daily totals into one hour-12 row per day for simplicity."""
    conn = sqlite3.connect(config.DB_PATH)
    for date_iso, pred, actual in daily_ratios:
        _insert_skill_row(conn, date_iso, 12, pred, actual)
    conn.commit()
    conn.close()


def test_bias_empty_skill_log_returns_neutral():
    """No rows → factor=1.0, reason mentions insufficient samples."""
    bias = compute_pv_trust_bias(
        as_of_date_utc=datetime(2026, 5, 15, tzinfo=UTC).date(),
        min_samples=5,
    )
    assert bias.factor == 1.0
    assert "insufficient" in bias.reason
    assert bias.n_samples == 0


def test_bias_below_min_samples_returns_neutral():
    """3 days when min_samples=5 → fallback to 1.0."""
    _seed_skill_log([
        ("2026-05-10", 10.0, 8.0),  # ratio 0.8
        ("2026-05-11", 10.0, 9.0),  # 0.9
        ("2026-05-12", 10.0, 7.0),  # 0.7
    ])
    bias = compute_pv_trust_bias(
        as_of_date_utc=datetime(2026, 5, 15, tzinfo=UTC).date(),
        min_samples=5,
    )
    assert bias.factor == 1.0
    assert bias.n_samples == 3
    assert "insufficient" in bias.reason


def test_bias_p75_above_median():
    """5 days with rising ratios — P75 must exceed the median."""
    _seed_skill_log([
        ("2026-05-10", 10.0, 6.0),   # 0.6
        ("2026-05-11", 10.0, 8.0),   # 0.8
        ("2026-05-12", 10.0, 10.0),  # 1.0
        ("2026-05-13", 10.0, 12.0),  # 1.2
        ("2026-05-14", 10.0, 14.0),  # 1.4
    ])
    bias = compute_pv_trust_bias(
        as_of_date_utc=datetime(2026, 5, 15, tzinfo=UTC).date(),
        percentile=0.75,
        min_samples=5,
        min_bias=0.5,
        max_bias=2.0,
    )
    assert bias.n_samples == 5
    # P75 of [0.6, 0.8, 1.0, 1.2, 1.4] = 0.6 + 0.75 * (1.4-0.6) = 1.2 (linear interp on 4 gaps)
    assert 1.15 < bias.factor < 1.25, f"expected ~1.2 got {bias.factor}"
    # Median would be 1.0 — confirm P75 strictly above.
    assert bias.factor > 1.0


def test_bias_p50_matches_median():
    """P50 with the same data must equal the median."""
    _seed_skill_log([
        ("2026-05-10", 10.0, 6.0),
        ("2026-05-11", 10.0, 8.0),
        ("2026-05-12", 10.0, 10.0),
        ("2026-05-13", 10.0, 12.0),
        ("2026-05-14", 10.0, 14.0),
    ])
    bias = compute_pv_trust_bias(
        as_of_date_utc=datetime(2026, 5, 15, tzinfo=UTC).date(),
        percentile=0.5,
        min_samples=5,
        min_bias=0.5,
        max_bias=2.0,
    )
    assert abs(bias.factor - 1.0) < 0.01


def test_bias_clamps_to_max():
    """A wild upper tail must clamp to max_bias."""
    _seed_skill_log([
        ("2026-05-10", 1.0, 3.0),   # 3.0
        ("2026-05-11", 1.0, 4.0),   # 4.0
        ("2026-05-12", 1.0, 5.0),   # 5.0
        ("2026-05-13", 1.0, 6.0),   # 6.0
        ("2026-05-14", 1.0, 7.0),   # 7.0
    ])
    bias = compute_pv_trust_bias(
        as_of_date_utc=datetime(2026, 5, 15, tzinfo=UTC).date(),
        percentile=0.75,
        min_samples=5,
        min_bias=0.5,
        max_bias=1.5,
    )
    # raw P75 = ~6.0; clamped to 1.5
    assert bias.factor == 1.5
    assert bias.raw_ratio is not None and bias.raw_ratio > 5.0


def test_bias_clamps_to_min_and_drops_low_predict_days():
    """Predicted-PV < 1 kWh days are filtered (winter / data gaps)."""
    _seed_skill_log([
        ("2026-05-09", 0.5, 0.0),    # FILTERED: pred < 1 kWh
        ("2026-05-10", 10.0, 5.0),   # 0.5
        ("2026-05-11", 10.0, 4.0),   # 0.4
        ("2026-05-12", 10.0, 3.0),   # 0.3
        ("2026-05-13", 10.0, 2.0),   # 0.2
        ("2026-05-14", 10.0, 1.0),   # 0.1
    ])
    bias = compute_pv_trust_bias(
        as_of_date_utc=datetime(2026, 5, 15, tzinfo=UTC).date(),
        percentile=0.5,
        min_samples=4,
        min_bias=0.7,
        max_bias=1.5,
    )
    # Five remaining ratios, P50 = 0.3 → clamped to min 0.7.
    assert bias.n_samples == 5
    assert bias.factor == 0.7


# ---------------------------------------------------------------------------
# evaluate_pv_sufficiency_guard
# ---------------------------------------------------------------------------

def _slot_starts(n: int, base_utc: datetime) -> list[datetime]:
    return [base_utc + timedelta(minutes=30 * i) for i in range(n)]


def test_guard_skipped_when_disabled():
    starts = _slot_starts(4, datetime(2026, 5, 15, 8, 0, tzinfo=UTC))
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[2.0] * 4,
        base_load_kwh=[0.5] * 4,
        price_line=[15.0] * 4,
        peak_threshold_p=25.0,
        initial_soc_kwh=2.0,
        soc_max_kwh=10.0,
        strict_savings=True,
        enabled=False,
    )
    assert not diag.applied
    assert diag.reason == "disabled"


def test_guard_skipped_when_not_strict_savings():
    starts = _slot_starts(4, datetime(2026, 5, 15, 8, 0, tzinfo=UTC))
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[10.0] * 4,
        base_load_kwh=[0.1] * 4,
        price_line=[10.0] * 4,
        peak_threshold_p=25.0,
        initial_soc_kwh=0.0,
        soc_max_kwh=5.0,
        strict_savings=False,
        enabled=True,
    )
    assert not diag.applied
    assert diag.reason == "not_strict_savings"


def test_guard_fires_when_pv_sufficient():
    """Battery empty, 4 today-slots all pre-peak, PV ≫ headroom + load → fires."""
    starts = _slot_starts(4, datetime(2026, 5, 15, 8, 0, tzinfo=UTC))
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[2.0] * 4,           # Σ = 8 kWh today
        base_load_kwh=[0.5] * 4,      # Σ = 2 kWh
        price_line=[15.0] * 4,        # all below peak_threshold
        peak_threshold_p=25.0,
        initial_soc_kwh=2.0,
        soc_max_kwh=8.0,              # headroom 6 → demand = 6 + 2 = 8
        strict_savings=True,
        enabled=True,
        margin=1.0,
    )
    # forecast 8 × 1.0 ≥ 8 → fires, all 4 slots blocked
    assert diag.applied
    assert diag.reason == "sufficient_pv"
    assert diag.pre_peak_slot_indices == [0, 1, 2, 3]


def test_guard_skipped_when_pv_insufficient():
    """Forecast PV below demand → guard does not fire."""
    starts = _slot_starts(4, datetime(2026, 5, 15, 8, 0, tzinfo=UTC))
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[0.5] * 4,           # Σ = 2 kWh forecast PV
        base_load_kwh=[0.5] * 4,      # Σ = 2 kWh load
        price_line=[15.0] * 4,
        peak_threshold_p=25.0,
        initial_soc_kwh=2.0,
        soc_max_kwh=8.0,              # demand = 6 + 2 = 8 > 2 → does NOT fire
        strict_savings=True,
        enabled=True,
        margin=1.0,
    )
    assert not diag.applied
    assert diag.reason == "insufficient_pv"


def test_guard_excludes_peak_and_post_peak_slots():
    """Once a peak slot appears, every slot from there onwards is excluded."""
    starts = _slot_starts(6, datetime(2026, 5, 15, 8, 0, tzinfo=UTC))
    # Make slot 3 the peak.
    prices = [15.0, 15.0, 15.0, 30.0, 30.0, 30.0]
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[2.0] * 6,
        base_load_kwh=[0.5] * 6,
        price_line=prices,
        peak_threshold_p=25.0,
        initial_soc_kwh=0.0,
        soc_max_kwh=4.0,
        strict_savings=True,
        enabled=True,
    )
    # forecast = 12, demand = 4 + 3 = 7 → fires
    assert diag.applied
    assert diag.first_peak_slot_idx == 3
    assert diag.pre_peak_slot_indices == [0, 1, 2]


def test_guard_excludes_tomorrows_slots():
    """Today = 2026-05-15. Slot 5 onwards is tomorrow → only first 5 considered."""
    # Build slots from 22:00 UTC today (5 slots cover 22:00→00:00, then 3 tomorrow).
    starts = _slot_starts(8, datetime(2026, 5, 15, 22, 0, tzinfo=UTC))
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[1.0] * 8,           # only the first 4 (today, 22:00-23:30) count = 4
        base_load_kwh=[0.5] * 8,      # today load Σ = 2 (4 slots)
        price_line=[15.0] * 8,
        peak_threshold_p=25.0,
        initial_soc_kwh=8.0,
        soc_max_kwh=10.0,             # headroom 2 → demand = 2 + 2 = 4 ≤ 4
        strict_savings=True,
        enabled=True,
    )
    assert diag.applied
    # only today's 4 slots targeted
    assert diag.pre_peak_slot_indices == [0, 1, 2, 3]


def test_guard_margin_lower_demands_more_pv():
    """margin=0.5 means PV needs to be 2× demand → does NOT fire with 1× PV."""
    starts = _slot_starts(4, datetime(2026, 5, 15, 8, 0, tzinfo=UTC))
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[2.0] * 4,           # Σ = 8
        base_load_kwh=[0.5] * 4,
        price_line=[15.0] * 4,
        peak_threshold_p=25.0,
        initial_soc_kwh=2.0,
        soc_max_kwh=8.0,              # demand = 6 + 2 = 8 → 8 × 0.5 = 4 < 8 → no
        strict_savings=True,
        enabled=True,
        margin=0.5,
    )
    assert not diag.applied


# ---------------------------------------------------------------------------
# End-to-end through solve_lp
# ---------------------------------------------------------------------------

def _minimal_weather(n: int) -> WeatherLpSeries:
    """Generic flat-weather LP inputs."""
    base = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)
    return WeatherLpSeries(
        slot_starts_utc=[base + timedelta(minutes=30 * i) for i in range(n)],
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[200.0] * n,
        cloud_cover_pct=[50.0] * n,
        pv_kwh_per_slot=[2.0] * n,    # 4 kW × 0.5 h = 2 kWh/slot
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )


def test_e2e_guard_blocks_grid_charging_under_strict_savings(monkeypatch):
    """With strict_savings + abundant PV forecast, solve_lp must NOT grid-charge
    in any pre-peak slot — every chg[i] ≤ pv_use[i] on those slots."""
    monkeypatch.setattr(config, "ENERGY_STRATEGY_MODE", "strict_savings")
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", True)
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_MARGIN", 1.0)
    # Disable peak_export so the LP only has cheap grid → battery vs PV.
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive")

    weather = _minimal_weather(n=8)
    # Cheap morning (10p), peak evening (40p) so the LP is tempted to charge cheap.
    prices = [10.0, 10.0, 10.0, 10.0, 40.0, 40.0, 40.0, 40.0]
    base_load = [0.3] * 8

    plan = solve_lp(
        slot_starts_utc=weather.slot_starts_utc,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=LpInitialState(soc_kwh=2.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=[5.0] * 8,
    )
    assert plan.ok, f"LP failed: {plan.status}"
    assert plan.pv_sufficiency_guard is not None
    assert plan.pv_sufficiency_guard.applied, (
        f"guard reason={plan.pv_sufficiency_guard.reason} "
        f"diag={plan.pv_sufficiency_guard.to_snapshot_dict()}"
    )
    # On every pre-peak slot in the guard's index list, chg must be ≤ pv_use.
    for i in plan.pv_sufficiency_guard.pre_peak_slot_indices:
        assert plan.battery_charge_kwh[i] <= plan.pv_use_kwh[i] + 1e-6, (
            f"slot {i}: grid-charge leaked. chg={plan.battery_charge_kwh[i]} "
            f"pv_use={plan.pv_use_kwh[i]}"
        )


def test_e2e_guard_inactive_under_savings_first(monkeypatch):
    """In savings_first mode, the guard is inert — the LP can grid-charge freely."""
    monkeypatch.setattr(config, "ENERGY_STRATEGY_MODE", "savings_first")
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", True)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive")

    weather = _minimal_weather(n=8)
    prices = [10.0, 10.0, 10.0, 10.0, 40.0, 40.0, 40.0, 40.0]
    base_load = [0.3] * 8

    plan = solve_lp(
        slot_starts_utc=weather.slot_starts_utc,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=LpInitialState(soc_kwh=2.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=[5.0] * 8,
    )
    assert plan.ok
    assert plan.pv_sufficiency_guard is not None
    # not_strict_savings → applied=False; LP may have grid-charged freely.
    assert not plan.pv_sufficiency_guard.applied
    assert plan.pv_sufficiency_guard.reason == "not_strict_savings"


def test_e2e_guard_skipped_when_pv_low(monkeypatch):
    """Forecast PV well below demand → guard inert even in strict_savings."""
    monkeypatch.setattr(config, "ENERGY_STRATEGY_MODE", "strict_savings")
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", True)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive")

    base = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)
    n = 8
    weather = WeatherLpSeries(
        slot_starts_utc=[base + timedelta(minutes=30 * i) for i in range(n)],
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[50.0] * n,
        cloud_cover_pct=[90.0] * n,
        pv_kwh_per_slot=[0.05] * n,   # tiny PV — guard must not fire
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    prices = [10.0, 10.0, 10.0, 10.0, 40.0, 40.0, 40.0, 40.0]
    base_load = [0.3] * n

    plan = solve_lp(
        slot_starts_utc=weather.slot_starts_utc,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=LpInitialState(soc_kwh=2.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=[5.0] * n,
    )
    assert plan.ok
    assert plan.pv_sufficiency_guard is not None
    assert not plan.pv_sufficiency_guard.applied
    assert plan.pv_sufficiency_guard.reason == "insufficient_pv"
