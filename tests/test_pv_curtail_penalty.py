"""LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH — keeps the LP from curtailing solar
when pv_use is feasible.

Prod audit on 2026-04-30 found the LP curtailing 74% of one day's PV (6.34 kWh)
because pv_curt had a zero objective coefficient — grid imp at -7p ties or
beats PV's zero direct value when the chg cap binds, so the LP "happily curtails."
Adding a penalty equal to ``EXPORT_RATE_PENCE`` makes curtailment cost-equivalent
to "would have exported", restoring the correct ranking.

These tests pin the new behaviour: with the penalty enabled, the LP must prefer
pv_use over pv_curt whenever pv_use is feasible (battery has room or export is
available). With the penalty disabled (legacy), any feasible solution is allowed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch):
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    # Disable cycle penalty so PV→battery→grid arbitrage isn't penalised by the
    # tiny default; we want pv_use vs pv_curt to be the dominant signal.
    monkeypatch.setattr(app_config, "LP_CYCLE_PENALTY_PENCE_PER_KWH", 0.0)


def _series_with_pv(n: int, base: datetime, pv_per_slot: float) -> tuple[list[datetime], WeatherLpSeries]:
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    return slots, WeatherLpSeries(
        slot_starts_utc=slots,
        # Warm enough that no Daikin space heating fires (cleaner test math)
        temperature_outdoor_c=[20.0] * n,
        shortwave_radiation_wm2=[600.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=[pv_per_slot] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )


def test_penalty_eliminates_curtailment_when_export_feasible(monkeypatch):
    """Standard-price slot with PV > house load. With the penalty enabled, the LP
    must export the surplus PV instead of curtailing it."""
    monkeypatch.setattr(app_config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 15.0)

    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots, w = _series_with_pv(n, base, pv_per_slot=1.0)
    prices = [10.0] * n      # standard import
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=8.0, tank_temp_c=48.0, indoor_temp_c=21.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.status == "Optimal" and plan.ok
    total_curt = sum(plan.pv_curtail_kwh)
    total_use = sum(plan.pv_use_kwh)
    assert total_curt < 1e-3, (
        f"Penalty should drive curtailment to zero when export is feasible; "
        f"got total_curt={total_curt:.4f}, total_use={total_use:.4f}"
    )


def test_legacy_zero_penalty_allows_curtailment(monkeypatch):
    """With penalty=0 the LP has no incentive to prefer pv_use over pv_curt —
    document this so a future config change knows what the legacy behaviour was."""
    monkeypatch.setattr(app_config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 0.0)

    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots, w = _series_with_pv(n, base, pv_per_slot=1.0)
    # Negative-price slots so the LP wants max imp; PV becomes "competition"
    prices = [-7.0] * n
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=2.0, tank_temp_c=48.0, indoor_temp_c=21.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.status == "Optimal" and plan.ok
    # We don't assert curt > 0 here — the solver may tie-break either way without
    # a penalty. The point is just that the run is feasible (no regression).
    assert all(c >= -1e-6 for c in plan.pv_curtail_kwh)


def test_penalty_value_reduces_curtailment_under_chg_cap(monkeypatch):
    """The realistic prod scenario: deep negatives + PV peak + chg cap binding.
    The penalty pushes the LP to lower imp and route PV through the battery
    rather than curtailing.
    """
    monkeypatch.setattr(app_config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 15.0)

    base = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    n = 6  # 3 hours of deep negative
    # Lots of PV, well above what the battery alone can absorb at the chg cap
    slots, w = _series_with_pv(n, base, pv_per_slot=1.5)
    prices = [-7.0] * n
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=2.0, tank_temp_c=48.0, indoor_temp_c=21.0)
    plan_with = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    monkeypatch.setattr(app_config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 0.0)
    plan_without = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    curt_with = sum(plan_with.pv_curtail_kwh)
    curt_without = sum(plan_without.pv_curtail_kwh)
    assert plan_with.ok and plan_without.ok
    # Penalty must not increase curtailment; usually strictly reduces it. Allow
    # ties (edge case where there's literally nowhere for PV to go even with the
    # incentive — battery and exports both saturated).
    assert curt_with <= curt_without + 1e-6, (
        f"Penalty made curtailment worse: with={curt_with:.4f} without={curt_without:.4f}"
    )
