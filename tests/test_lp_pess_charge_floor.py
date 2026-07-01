"""Pessimistic-scenario charge floor (PR B, 2026-07-02 LP audit).

The LP sizes pre-peak charge for the MEDIAN load; under-charging costs ~4×
over-charging (newsvendor). The pessimistic scenario's SoC trajectory is the
right-quantile answer and is now enforced as a SOFT floor on the committed
plan. Covers: the floor raises committed SoC, softness (never Infeasible),
zero-floor bit-compat, and the orchestration helper's decision logic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.config import config
from src.scheduler import optimizer as opt_mod
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


def _mk_inputs(n=8):
    t0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    slots = [t0 + timedelta(minutes=30 * i) for i in range(n)]
    prices = [5.0] * (n // 2) + [35.0] * (n - n // 2)  # cheap morning → peak evening
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[10.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,  # winter overcast: battery is the only lever
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    return slots, prices, weather


def _solve(floor=None, *, load=0.2, soc0=2.0, n=8):
    slots, prices, weather = _mk_inputs(n)
    return solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=[load] * n,
        weather=weather,
        initial=LpInitialState(soc_kwh=soc0, tank_temp_c=52.0),
        tz=ZoneInfo("Europe/London"),
        soc_floor_kwh=floor,
    )


def test_floor_raises_committed_soc_and_costs_insurance():
    n = 8
    nominal = _solve(None, n=n)
    assert nominal.ok
    # Demand pessimistic-level charge by the end of the cheap window (slot 3).
    floor = [0.0] * n
    floor[3] = 6.0
    floored = _solve(floor, n=n)
    assert floored.ok, floored.status
    assert floored.soc_floor_applied
    assert floored.soc_kwh[4] >= 6.0 - 1e-3, floored.soc_kwh
    # Insurance is never free money: floored objective >= nominal objective.
    assert floored.objective_pence >= nominal.objective_pence - 1e-6
    assert floored.soc_floor_slack_kwh == pytest.approx(0.0, abs=1e-3)


def test_floor_is_soft_never_infeasible():
    """An unreachable floor (full battery demanded after one slot) must degrade
    to slack, not Infeasible — the LP-infeasible history is why it's soft."""
    n = 4
    floor = [float(config.BATTERY_CAPACITY_KWH)] + [0.0] * (n - 1)
    plan = _solve(floor, soc0=1.2, n=n)
    assert plan.ok, plan.status
    assert plan.soc_floor_slack_kwh > 3.0  # physically impossible → big slack


def test_zero_floor_matches_no_floor():
    n = 8
    a = _solve(None, n=n)
    b = _solve([0.0] * n, n=n)  # all below reserve → no constraints added
    assert b.ok and not b.soc_floor_slack_kwh
    assert b.objective_pence == pytest.approx(a.objective_pence, abs=1e-6)


def test_floor_length_mismatch_raises():
    with pytest.raises(ValueError):
        _solve([1.0] * 3, n=8)


# --- orchestration helper -------------------------------------------------

def _fake_plan(soc, ok=True, obj=100.0, n=None):
    n = n if n is not None else len(soc) - 1
    return SimpleNamespace(
        ok=ok, status="Optimal" if ok else "Infeasible", objective_pence=obj,
        slot_starts_utc=[datetime(2026, 1, 15, 10, 0, tzinfo=UTC) + timedelta(minutes=30 * i) for i in range(n)],
        soc_kwh=list(soc),
    )


def test_helper_reso1ves_when_pessimistic_needs_more(monkeypatch):
    nominal = _fake_plan([2.0, 2.0, 2.0, 2.0, 2.0])          # holds 2.0 flat
    pess = _fake_plan([2.0, 4.0, 6.0, 6.0, 5.0])              # wants 6.0 pre-peak
    floored = _fake_plan([2.0, 4.0, 6.0, 6.0, 5.0], obj=110.0)
    floored.soc_floor_slack_kwh = 0.0
    captured = {}

    def fake_solve(**kw):
        captured["floor"] = kw.get("soc_floor_kwh")
        return floored

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", fake_solve)
    snap = {}
    out = opt_mod._apply_pessimistic_charge_floor(
        nominal, {"pessimistic": SimpleNamespace(plan=pess)},
        solve_kwargs={}, exogenous_snapshot=snap,
    )
    assert out is floored
    tol = float(config.LP_PESS_CHARGE_FLOOR_TOLERANCE_KWH)
    # floor[i] mirrors pess.soc_kwh[i+1] − tolerance
    assert captured["floor"] == pytest.approx([4.0 - tol, 6.0 - tol, 6.0 - tol, 5.0 - tol])
    assert snap["pess_charge_floor"]["binding_slots"] >= 2
    assert snap["pess_charge_floor"]["insurance_cost_pence"] == pytest.approx(10.0)


def test_helper_skips_when_nominal_already_covers(monkeypatch):
    nominal = _fake_plan([8.0, 8.0, 8.0, 8.0, 8.0])
    pess = _fake_plan([8.0, 6.0, 6.0, 5.0, 5.0])
    called = []
    monkeypatch.setattr(
        "src.scheduler.lp_optimizer.solve_lp", lambda **kw: called.append(1)
    )
    out = opt_mod._apply_pessimistic_charge_floor(
        nominal, {"pessimistic": SimpleNamespace(plan=pess)},
        solve_kwargs={}, exogenous_snapshot={},
    )
    assert out is nominal and not called


def test_helper_keeps_nominal_when_pessimistic_failed(monkeypatch):
    nominal = _fake_plan([2.0] * 5)
    pess = _fake_plan([], ok=False)
    out = opt_mod._apply_pessimistic_charge_floor(
        nominal, {"pessimistic": SimpleNamespace(plan=pess)},
        solve_kwargs={}, exogenous_snapshot={},
    )
    assert out is nominal


def test_helper_keeps_nominal_when_resolve_fails(monkeypatch):
    nominal = _fake_plan([2.0, 2.0, 2.0, 2.0, 2.0])
    pess = _fake_plan([2.0, 6.0, 6.0, 6.0, 6.0])
    bad = _fake_plan([], ok=False)
    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", lambda **kw: bad)
    out = opt_mod._apply_pessimistic_charge_floor(
        nominal, {"pessimistic": SimpleNamespace(plan=pess)},
        solve_kwargs={}, exogenous_snapshot={},
    )
    assert out is nominal
