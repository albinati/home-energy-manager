"""Tests for ``filter_robust_peak_export`` in src/scheduler/lp_dispatch.py.

Hand-built ``LpPlan`` instances exercise the decision tree without invoking
the MILP solver. Covers:

* No scenarios provided (trigger reason not in allow-list) → commit all.
* Pessimistic disagrees → drop, decision recorded with the right reason.
* Pessimistic agrees → commit, reason="robust".
* strict_savings → drop everything regardless of scenarios.
* Pessimistic solve failed (ok=False) → degraded-mode commit with the right reason.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.scheduler.lp_dispatch import filter_robust_peak_export
from src.scheduler.lp_optimizer import LpPlan


def _make_plan(
    *,
    n: int = 4,
    peak_export_idx: int = 2,
    export_kwh: float = 1.84,
) -> LpPlan:
    """Tiny 4-slot plan with one peak_export at the given index.

    For the slot to be classified peak_export by ``lp_plan_to_slots`` we need
    discharge>0, export>0, price > peak_threshold, and chg=0.
    """
    t0 = datetime(2026, 5, 1, 16, 0, tzinfo=UTC)
    starts = [t0 + i * timedelta(minutes=30) for i in range(n)]
    chg = [0.0] * n
    dis = [0.0] * n
    exp = [0.0] * n
    imp = [0.0] * n
    price = [10.0] * n
    dis[peak_export_idx] = 2.0
    exp[peak_export_idx] = export_kwh
    price[peak_export_idx] = 35.0  # > peak_threshold 30
    return LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=starts,
        price_pence=price,
        import_kwh=imp,
        export_kwh=exp,
        battery_charge_kwh=chg,
        battery_discharge_kwh=dis,
        pv_use_kwh=[0.0] * n,
        pv_curtail_kwh=[0.0] * n,
        dhw_electric_kwh=[0.0] * n,
        space_electric_kwh=[0.0] * n,
        soc_kwh=[5.0] * (n + 1),
        peak_threshold_pence=30.0,
    )


def test_no_scenarios_commits_all_peak_export():
    plan = _make_plan()
    slots, decisions = filter_robust_peak_export(plan, scenarios=None)
    pe = [d for d in decisions if d["lp_kind"] == "peak_export"]
    assert len(pe) == 1
    assert pe[0]["committed"] is True
    assert pe[0]["reason"] == "no_scenarios_run"
    # The slot list also keeps the peak_export kind.
    assert slots[2].kind == "peak_export"


def test_pessimistic_disagrees_drops_slot():
    plan = _make_plan(export_kwh=1.84)
    pess = _make_plan(export_kwh=0.05)  # below 0.30 floor
    nom = plan
    opt = _make_plan(export_kwh=1.84)
    slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": opt, "nominal": nom, "pessimistic": pess},
    )
    pe = [d for d in decisions if d["lp_kind"] == "peak_export"]
    assert len(pe) == 1
    assert pe[0]["committed"] is False
    assert pe[0]["reason"] == "pessimistic_disagrees"
    assert pe[0]["dispatched_kind"] == "standard"
    # Slot is downgraded
    assert slots[2].kind == "standard"
    # Per-scenario values recorded
    assert pe[0]["scen_pessimistic_exp_kwh"] == pytest.approx(0.05)
    assert pe[0]["scen_nominal_exp_kwh"] == pytest.approx(1.84)
    assert pe[0]["scen_optimistic_exp_kwh"] == pytest.approx(1.84)


def test_pessimistic_agrees_commits_robust():
    plan = _make_plan(export_kwh=1.84)
    pess = _make_plan(export_kwh=1.40)  # >= 0.30 floor
    nom = plan
    opt = _make_plan(export_kwh=2.10)
    slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": opt, "nominal": nom, "pessimistic": pess},
    )
    pe = [d for d in decisions if d["lp_kind"] == "peak_export"]
    assert len(pe) == 1
    assert pe[0]["committed"] is True
    assert pe[0]["reason"] == "robust"
    assert slots[2].kind == "peak_export"


def test_pessimistic_agrees_but_economic_margin_fails_drops_slot(monkeypatch):
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch.config.LP_PEAK_EXPORT_MIN_MARGIN_PENCE_PER_KWH",
        1.0,
        raising=False,
    )
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch.config.LP_BATTERY_WEAR_COST_PENCE_PER_KWH",
        0.0,
        raising=False,
    )
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch.config.LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH",
        0.0,
        raising=False,
    )
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch.config.BATTERY_RT_EFFICIENCY",
        1.0,
        raising=False,
    )
    plan = _make_plan(export_kwh=1.84)
    pess = _make_plan(export_kwh=1.40)
    slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": plan, "nominal": plan, "pessimistic": pess},
        export_price_pence=[10.0, 10.0, 10.5, 10.0],
    )
    pe = [d for d in decisions if d["lp_kind"] == "peak_export"]
    assert len(pe) == 1
    assert pe[0]["committed"] is False
    assert pe[0]["reason"] == "economic_margin"
    assert pe[0]["export_price_p_kwh"] == pytest.approx(10.5)
    assert pe[0]["refill_price_p_kwh"] == pytest.approx(10.0)
    assert pe[0]["economic_margin_p_kwh"] == pytest.approx(0.5)
    assert slots[2].kind == "standard"


def test_pessimistic_agrees_and_economic_margin_clears_commits_slot(monkeypatch):
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch.config.LP_PEAK_EXPORT_MIN_MARGIN_PENCE_PER_KWH",
        1.0,
        raising=False,
    )
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch.config.LP_BATTERY_WEAR_COST_PENCE_PER_KWH",
        0.5,
        raising=False,
    )
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch.config.LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH",
        0.0,
        raising=False,
    )
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch.config.BATTERY_RT_EFFICIENCY",
        1.0,
        raising=False,
    )
    plan = _make_plan(export_kwh=1.84)
    pess = _make_plan(export_kwh=1.40)
    slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": plan, "nominal": plan, "pessimistic": pess},
        export_price_pence=[10.0, 10.0, 13.0, 10.0],
    )
    pe = [d for d in decisions if d["lp_kind"] == "peak_export"]
    assert len(pe) == 1
    assert pe[0]["committed"] is True
    assert pe[0]["reason"] == "robust"
    assert pe[0]["export_price_p_kwh"] == pytest.approx(13.0)
    assert pe[0]["refill_price_p_kwh"] == pytest.approx(10.0)
    assert pe[0]["economic_margin_p_kwh"] == pytest.approx(2.0)
    assert slots[2].kind == "peak_export"


def test_strict_savings_drops_all_peak_export(monkeypatch):
    monkeypatch.setattr("src.scheduler.lp_dispatch.config.ENERGY_STRATEGY_MODE", "strict_savings", raising=False)
    plan = _make_plan(export_kwh=1.84)
    pess = _make_plan(export_kwh=1.84)  # would otherwise pass
    slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": plan, "nominal": plan, "pessimistic": pess},
    )
    pe = [d for d in decisions if d["lp_kind"] == "peak_export"]
    # In strict_savings mode, lp_plan_to_slots emits the slot as kind="peak"
    # (not peak_export — see the strict_savings branch in lp_plan_to_slots).
    # The filter therefore sees no peak_export to gate.
    assert len(pe) == 0


def test_pessimistic_solve_failure_degrades_to_commit():
    plan = _make_plan(export_kwh=1.84)
    pess_failed = LpPlan(ok=False, status="Infeasible", objective_pence=0.0)
    slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": plan, "nominal": plan, "pessimistic": pess_failed},
    )
    pe = [d for d in decisions if d["lp_kind"] == "peak_export"]
    assert len(pe) == 1
    assert pe[0]["committed"] is True
    assert pe[0]["reason"] == "pessimistic_failed"


def test_non_peak_export_slots_pass_through():
    plan = _make_plan(export_kwh=1.84)
    pess = _make_plan(export_kwh=0.05)
    slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": plan, "nominal": plan, "pessimistic": pess},
    )
    # Slots 0/1/3 are standard — should pass through with reason="not_peak_export".
    non_pe = [d for d in decisions if d["lp_kind"] != "peak_export"]
    assert len(non_pe) == 3
    assert all(d["committed"] for d in non_pe)
    assert all(d["reason"] == "not_peak_export" for d in non_pe)
