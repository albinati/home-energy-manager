"""Tests for the PV-sufficiency guard rail + daily PV calibration refresh job
(incident 2026-05-15).

Covers:
- ``evaluate_pv_sufficiency_guard`` — fires only in strict_savings; targets
  the right slot indices (today, pre-peak); the demand vs forecast inequality.
- End-to-end through ``solve_lp`` — when the guard fires, the LP solution
  obeys ``chg[i] <= pv_use[i]`` on the targeted slots.
- ``bulletproof_pv_calibration_refresh_job`` — smoke test against an empty
  history (the underlying compute functions handle the no-data case
  gracefully so the cron stays safe on a fresh container).

The solve_lp E2E tests use a minimal but realistic 8-slot horizon so they
finish in well under a second.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.scheduler.pv_trust import evaluate_pv_sufficiency_guard
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _init_db(monkeypatch):
    _db.init_db()
    # PR K2: e2e LP test exercises legacy free-DHW path; opt out of pinning.
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False, raising=False)


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


def test_guard_always_on_when_enabled():
    """PR C — guard is mode-agnostic now; fires whenever PV is sufficient.
    Previously gated by ``strict_savings`` (removed in PR C)."""
    starts = _slot_starts(4, datetime(2026, 5, 15, 8, 0, tzinfo=UTC))
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[10.0] * 4,         # huge PV
        base_load_kwh=[0.1] * 4,
        price_line=[10.0] * 4,
        peak_threshold_p=25.0,
        initial_soc_kwh=0.0,
        soc_max_kwh=5.0,
        strict_savings=False,        # legacy arg, ignored now
        enabled=True,
    )
    # Forecast 40 kWh >> demand 5.4 kWh → fires
    assert diag.applied
    assert diag.reason == "sufficient_pv"


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


def test_guard_exempts_negative_price_slots():
    """2026-07-02 window audit: the guard's premise inverts when the grid PAYS
    for import — negative slots must stay grid-chargeable even on a
    PV-sufficient day (the guard blocked grid→battery across a 15-slot
    negative window, forcing chg==pv_use + curtailment)."""
    starts = _slot_starts(6, datetime(2026, 7, 2, 8, 0, tzinfo=UTC))
    prices = [15.0, -1.9, -4.4, 15.0, 30.0, 30.0]
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
    assert diag.applied
    # slots 1-2 (negative) are exempt; slots 4-5 (peak+) excluded as before
    assert diag.pre_peak_slot_indices == [0, 3]


def test_guard_excludes_tomorrows_slots():
    """Today = 2026-05-15. Slot 4+ is tomorrow → only first 4 considered."""
    starts = _slot_starts(8, datetime(2026, 5, 15, 22, 0, tzinfo=UTC))
    diag = evaluate_pv_sufficiency_guard(
        slot_starts_utc=starts,
        pv_avail=[1.0] * 8,
        base_load_kwh=[0.5] * 8,
        price_line=[15.0] * 8,
        peak_threshold_p=25.0,
        initial_soc_kwh=8.0,
        soc_max_kwh=10.0,
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
        pv_avail=[2.0] * 4,
        base_load_kwh=[0.5] * 4,
        price_line=[15.0] * 4,
        peak_threshold_p=25.0,
        initial_soc_kwh=2.0,
        soc_max_kwh=8.0,
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
        pv_kwh_per_slot=[2.0] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )


def test_e2e_guard_blocks_grid_charging_when_pv_sufficient(monkeypatch):
    """PR C — guard is mode-agnostic. With abundant PV forecast, solve_lp
    must NOT grid-charge in any pre-peak slot regardless of mode (previously
    gated by ``strict_savings``, removed in PR C)."""
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", True)
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_MARGIN", 1.0)
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
    assert plan.ok, f"LP failed: {plan.status}"
    assert plan.pv_sufficiency_guard is not None
    assert plan.pv_sufficiency_guard.applied, (
        f"guard reason={plan.pv_sufficiency_guard.reason} "
        f"diag={plan.pv_sufficiency_guard.to_snapshot_dict()}"
    )
    for i in plan.pv_sufficiency_guard.pre_peak_slot_indices:
        assert plan.battery_charge_kwh[i] <= plan.pv_use_kwh[i] + 1e-6, (
            f"slot {i}: grid-charge leaked. chg={plan.battery_charge_kwh[i]} "
            f"pv_use={plan.pv_use_kwh[i]}"
        )


# PR C — `test_e2e_guard_inactive_under_savings_first` removed.
# strict_savings/savings_first is gone; the guard is always-on when enabled.


def test_e2e_guard_allows_grid_charge_in_negative_slots(monkeypatch):
    """The 2026-07-02 production failure in miniature: PV-sufficient day +
    negative-price window + empty battery. The LP must grid-charge in the
    negative slots (paid import) instead of being pinned to chg==pv_use."""
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", True)
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_MARGIN", 1.0)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive")

    weather = _minimal_weather(n=8)
    prices = [10.0, -4.4, -4.4, -3.0, 40.0, 40.0, 40.0, 40.0]
    base_load = [0.3] * 8

    plan = solve_lp(
        slot_starts_utc=weather.slot_starts_utc,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=LpInitialState(soc_kwh=1.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=[0.6] * 8,
    )
    assert plan.ok, f"LP failed: {plan.status}"
    assert plan.pv_sufficiency_guard is not None and plan.pv_sufficiency_guard.applied
    neg = [1, 2, 3]
    for i in neg:
        assert i not in plan.pv_sufficiency_guard.pre_peak_slot_indices
    grid_charge_neg = sum(
        max(0.0, plan.battery_charge_kwh[i] - plan.pv_use_kwh[i]) for i in neg
    )
    assert grid_charge_neg > 0.5, (
        f"LP should grid-charge in the paid window; "
        f"chg={[round(plan.battery_charge_kwh[i], 2) for i in neg]} "
        f"pv_use={[round(plan.pv_use_kwh[i], 2) for i in neg]}"
    )


def test_e2e_guard_skipped_when_pv_low(monkeypatch):
    """Forecast PV well below demand → guard inert regardless of mode."""
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", True)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive")

    base = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)
    n = 8
    weather = WeatherLpSeries(
        slot_starts_utc=[base + timedelta(minutes=30 * i) for i in range(n)],
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[50.0] * n,
        cloud_cover_pct=[90.0] * n,
        pv_kwh_per_slot=[0.05] * n,
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


# ---------------------------------------------------------------------------
# bulletproof_pv_calibration_refresh_job — smoke test
# ---------------------------------------------------------------------------

def test_calibration_refresh_job_safe_on_empty_history():
    """The cron must not raise when there's no history yet (fresh container).

    The underlying compute functions return ``status='skipped'`` when no
    pv_realtime_history rows exist. The cron logs and continues; the LP keeps
    using whatever (possibly empty) calibration tables were there before.
    """
    from src.scheduler.runner import bulletproof_pv_calibration_refresh_job
    # Should be a no-op + no exception on an empty DB.
    bulletproof_pv_calibration_refresh_job()
