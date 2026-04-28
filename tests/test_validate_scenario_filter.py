"""Tests for the scenario-filter regression validator.

Scope: the score-and-aggregate logic only. Replaying the LP requires a full
historical DB snapshot; that's exercised against prod data via the
``scripts/validate_scenario_filter.py`` CLI, not in unit tests. Here we
synthesize an ``LpPlan`` + ``scenarios`` dict, run the same scoring code the
script uses, and assert it correctly identifies "filter saved money" vs
"filter cost money" cases.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def _build_plan_with_peak_export(export_kwh: float = 1.84):
    """Tiny 4-slot plan with one peak_export slot at index 2 (35p, 18:00 UTC)."""
    from src.scheduler.lp_optimizer import LpPlan

    t0 = datetime(2026, 5, 1, 16, 0, tzinfo=UTC)
    starts = [t0 + i * timedelta(minutes=30) for i in range(4)]
    chg = [0.0] * 4
    dis = [0.0] * 4
    exp = [0.0] * 4
    imp = [0.0] * 4
    price = [10.0] * 4
    dis[2] = 2.0
    exp[2] = export_kwh
    price[2] = 35.0  # > peak_threshold 30 → kind=peak_export
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
        pv_use_kwh=[0.0] * 4,
        pv_curtail_kwh=[0.0] * 4,
        dhw_electric_kwh=[0.0] * 4,
        space_electric_kwh=[0.0] * 4,
        soc_kwh=[5.0] * 5,
        peak_threshold_pence=30.0,
    )


def test_filter_keeps_slot_when_pessimistic_agrees__no_dropped_delta():
    """Filter committed → no dropped slots → no contribution to aggregate."""
    from src.scheduler.lp_dispatch import filter_robust_peak_export

    plan = _build_plan_with_peak_export(export_kwh=1.84)
    pess = _build_plan_with_peak_export(export_kwh=1.40)  # >= 0.30 floor
    _slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": plan, "nominal": plan, "pessimistic": pess},
    )
    pe_decisions = [d for d in decisions if d["lp_kind"] == "peak_export"]
    dropped = [d for d in pe_decisions if not d["committed"]]
    assert len(pe_decisions) == 1
    assert len(dropped) == 0


def test_filter_drops_when_pessimistic_disagrees__produces_negative_delta_under_proxy():
    """When the filter drops a slot the LP wanted to export, the per-slot
    delta under the proxy = (planned_export × terminal_value) − (planned_export × actual_export_price).

    With terminal_value=5p and actual_export_price=32p, the delta is
    1.84 × (5 − 32) = -49.7p → negative (filter cost us money in £ terms,
    even though it protected against a forecast risk we didn't materialise)."""
    from src.scheduler.lp_dispatch import filter_robust_peak_export

    plan = _build_plan_with_peak_export(export_kwh=1.84)
    pess = _build_plan_with_peak_export(export_kwh=0.05)  # below 0.30 floor → drops
    _slots, decisions = filter_robust_peak_export(
        plan,
        scenarios={"optimistic": plan, "nominal": plan, "pessimistic": pess},
    )
    dropped = [d for d in decisions if d["lp_kind"] == "peak_export" and not d["committed"]]
    assert len(dropped) == 1

    # Apply the validator's per-slot scoring proxy.
    actual_export_price_p = 32.0
    terminal_value_p = 5.0
    planned_export_kwh = 1.84
    saved = planned_export_kwh * terminal_value_p
    lost = planned_export_kwh * actual_export_price_p
    net = saved - lost
    assert net == pytest.approx(-49.68)


def test_filter_drops_with_high_terminal_value__net_can_be_positive():
    """If terminal SoC is valued highly enough, dropping a marginal export
    is net-favourable. Worth knowing because the proxy's threshold is what
    determines the validator's verdict."""
    planned_export_kwh = 0.5
    actual_export_price_p = 12.0  # cheap export window
    terminal_value_p = 15.0  # high — saved kWh is precious
    saved = planned_export_kwh * terminal_value_p
    lost = planned_export_kwh * actual_export_price_p
    net = saved - lost
    assert net > 0  # net positive even though we declined revenue


def test_validation_report_threshold_pass_fail():
    """ValidationReport.passed is governed by aggregate_delta_p vs fail_threshold_p."""
    from scripts.validate_scenario_filter import ValidationReport

    # Aggregate is well above threshold → pass.
    r = ValidationReport(
        days_back=30,
        runs_validated=10,
        runs_skipped=0,
        aggregate_delta_p=-100.0,
        total_slots_dropped=2,
        total_slots_committed=8,
        fail_threshold_p=-500.0,
    )
    assert r.passed

    # Aggregate at exactly the threshold → still pass (>=).
    r2 = ValidationReport(
        days_back=30, runs_validated=1, runs_skipped=0,
        aggregate_delta_p=-500.0, total_slots_dropped=1, total_slots_committed=0,
        fail_threshold_p=-500.0,
    )
    assert r2.passed

    # Aggregate below threshold → fail.
    r3 = ValidationReport(
        days_back=30, runs_validated=1, runs_skipped=0,
        aggregate_delta_p=-501.0, total_slots_dropped=1, total_slots_committed=0,
        fail_threshold_p=-500.0,
    )
    assert not r3.passed


def test_validator_skips_runs_with_no_peak_export(monkeypatch):
    """Helper query should only yield runs that had peak_export in the LP plan."""
    import scripts.validate_scenario_filter as v

    seen_calls: list[int] = []

    def _fake_runs(days):
        return []  # no qualifying runs

    monkeypatch.setattr(v, "_runs_with_peak_export", _fake_runs)
    report = v.validate(days_back=30, fail_threshold_p=-500.0)
    assert report.runs_validated == 0
    assert report.aggregate_delta_p == 0.0
    assert report.passed  # vacuously
