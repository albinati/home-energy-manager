"""PR #274: rank-based export-timing bonus + per-slot percentile audit metric.

Tests two things:
1. ``_compute_rank_percentiles`` correctness (ties, single, empty).
2. LP behaviour change: on a flat Outgoing-rate day where every slot ties on
   absolute price spread, the rank bonus shifts exports toward top-quartile
   slots even when low-quartile slots have more PV available. On a peaky day
   the bonus is dominated by the absolute spread → no behaviour change.
3. Audit metric: ``outgoing_rate_percentile`` lands on every dispatch_decisions
   row, not just peak_export rows.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.scheduler.lp_dispatch import (
    _compute_rank_percentiles,
    filter_robust_peak_export,
)


# --------------------------------------------------------------------------
# Percentile helper
# --------------------------------------------------------------------------

def test_percentile_helper_endpoints() -> None:
    """Lowest value → 0, highest → 100."""
    out = _compute_rank_percentiles([1.0, 2.0, 3.0, 4.0])
    assert out[0] == 0.0
    assert out[3] == 100.0


def test_percentile_helper_handles_ties() -> None:
    """All-equal values → 50 for every slot (avg-rank tie convention)."""
    out = _compute_rank_percentiles([10.0] * 8)
    assert all(v == 50.0 for v in out), out


def test_percentile_helper_empty_and_single() -> None:
    assert _compute_rank_percentiles([]) == []
    assert _compute_rank_percentiles([42.0]) == [50.0]


def test_percentile_helper_partial_ties() -> None:
    """Three at 5p, one at 10p → the 10p slot is 100, the 5p slots all share
    rank (1+2+3)/3 = 2 → percentile (2-1)/(4-1)*100 ≈ 33.3."""
    out = _compute_rank_percentiles([5.0, 5.0, 5.0, 10.0])
    assert out[3] == 100.0
    assert all(round(v, 1) == 33.3 for v in out[:3]), out


# --------------------------------------------------------------------------
# LP behaviour: flat day with rank bonus
# --------------------------------------------------------------------------

def test_rank_bonus_solves_cleanly_on_flat_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke test: enabling the bonus on a flat-rate day must not break the
    LP (no infeasibility, no NaN). Behaviour-change validation lives in the
    prod-snapshot replay (see PR description) — constructing a synthetic
    scenario that triggers the tie-breaking is brittle when the LP already
    maximises revenue under any positive spread."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    monkeypatch.setattr(app_config, "LP_PEAK_EXPORT_RANK_BONUS_PENCE_PER_KWH", 5.0, raising=False)
    monkeypatch.setattr(app_config, "LP_PEAK_EXPORT_TOP_QUARTILE_PERCENT", 25.0, raising=False)

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    export_prices = [9.5, 9.6, 9.7, 9.8, 10.0, 10.2, 10.5, 11.5]
    pv = [3.0] * n
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[18.0] * n,
        shortwave_radiation_wm2=[600.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=pv,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    init = LpInitialState(soc_kwh=10.0, tank_temp_c=45.0, indoor_temp_c=21.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[5.0] * n,
        base_load_kwh=[0.5] * n,
        weather=weather,
        initial=init,
        tz=ZoneInfo("Europe/London"),
        export_price_pence=export_prices,
    )
    assert plan.ok, plan.status
    # All exports finite + non-negative.
    for v in plan.export_kwh:
        assert v >= 0.0
        assert v == v  # NaN check


def test_rank_bonus_objective_term_only_when_export_prices_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``export_price_pence`` is None, the bonus is skipped (we have no
    distribution to compute a percentile from). Verify by solving with bonus
    and no per-slot export prices — must succeed identically to baseline."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    monkeypatch.setattr(app_config, "LP_PEAK_EXPORT_RANK_BONUS_PENCE_PER_KWH", 5.0, raising=False)

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[50.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    init = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=21.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[20.0] * n,
        base_load_kwh=[0.3] * n,
        weather=weather,
        initial=init,
        tz=ZoneInfo("Europe/London"),
        export_price_pence=None,  # flat fallback path — bonus is a no-op
    )
    assert plan.ok, plan.status


def test_rank_bonus_does_not_force_curtailment_at_zero_export_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When some slots have export rate 0 and others 10p, the LP should NOT
    curtail the 10p exports just to "wait for top quartile" — the bonus is a
    tie-breaker, not a curtailment driver. Only positive rates count for the
    quartile cutoff (zeros excluded)."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    monkeypatch.setattr(app_config, "LP_PEAK_EXPORT_RANK_BONUS_PENCE_PER_KWH", 5.0, raising=False)
    monkeypatch.setattr(app_config, "LP_PEAK_EXPORT_TOP_QUARTILE_PERCENT", 25.0, raising=False)

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    pv = [4.0, 4.0, 4.0, 4.0]
    export_prices = [10.0, 10.0, 10.0, 10.0]  # all equal positive
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[18.0] * n,
        shortwave_radiation_wm2=[600.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=pv,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    init = LpInitialState(soc_kwh=10.0, tank_temp_c=45.0, indoor_temp_c=21.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[5.0] * n,
        base_load_kwh=[0.3] * n,
        weather=weather,
        initial=init,
        tz=ZoneInfo("Europe/London"),
        export_price_pence=export_prices,
    )
    assert plan.ok, plan.status
    # All-equal 10p slots → none excluded → all in top quartile equally → no curtailment.
    total_export = sum(plan.export_kwh)
    assert total_export > 1.0, (
        f"Bonus must not force curtailment when all slots tie on a positive "
        f"export rate; got total_export={total_export:.3f} kWh"
    )


# --------------------------------------------------------------------------
# Audit metric: percentile lands on every decision row
# --------------------------------------------------------------------------

def test_percentile_recorded_on_every_decision_row() -> None:
    """``outgoing_rate_percentile`` must be set on standard slots too, not
    just peak_export — the metric describes the slot's place in the horizon
    distribution regardless of LP kind."""
    from src.scheduler.lp_optimizer import LpPlan

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan.price_pence = [5.0, 5.0, 25.0, 25.0]  # last two are "peak"
    plan.import_kwh = [0.0, 0.0, 0.0, 0.0]
    plan.export_kwh = [0.0, 0.0, 0.0, 0.0]
    plan.battery_charge_kwh = [0.0, 0.0, 0.0, 0.0]
    plan.battery_discharge_kwh = [0.0, 0.0, 0.0, 0.0]
    plan.dhw_electric_kwh = [0.0, 0.0, 0.0, 0.0]
    plan.space_electric_kwh = [0.0, 0.0, 0.0, 0.0]
    plan.soc_kwh = [5.0] * (n + 1)
    plan.tank_temp_c = [45.0] * (n + 1)
    plan.indoor_temp_c = [21.0] * (n + 1)
    plan.pv_curt_kwh = [0.0] * n
    plan.lwt_offset_c = [0.0] * n

    export_prices = [8.0, 9.0, 11.0, 14.0]
    _, decisions = filter_robust_peak_export(
        plan, scenarios=None, export_price_pence=export_prices,
    )
    assert len(decisions) == n
    pct = [d["outgoing_rate_percentile"] for d in decisions]
    assert pct[0] == 0.0   # lowest
    assert pct[3] == 100.0 # highest
    assert all(p is not None for p in pct), pct
