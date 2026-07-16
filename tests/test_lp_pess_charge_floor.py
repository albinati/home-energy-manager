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


def test_helper_exempts_pre_negative_and_negative_slots(monkeypatch):
    """The pre-negative drain WANTS low SoC; flooring it would block the
    profitable export→refill choreography. Negative-price slots likewise."""
    nominal = _fake_plan([2.0, 2.0, 2.0, 2.0, 2.0])
    nominal.pre_negative_export_slots = [0, 1]
    nominal.price_pence = [10.0, 10.0, -5.0, -5.0]  # slots 2-3 negative
    pess = _fake_plan([2.0, 6.0, 6.0, 6.0, 6.0])
    called = []
    monkeypatch.setattr(
        "src.scheduler.lp_optimizer.solve_lp", lambda **kw: called.append(1)
    )
    out = opt_mod._apply_pessimistic_charge_floor(
        nominal, {"pessimistic": SimpleNamespace(plan=pess)},
        solve_kwargs={}, exogenous_snapshot={},
    )
    # every slot exempt (0,1 = pre-neg drain; 2,3 = negative price) → no re-solve
    assert out is nominal and not called


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


# --- integration: optimizer gate on event-driven triggers (#668) -----------
#
# The unit tests above exercise trigger_runs_scenarios and the floor helper in
# isolation. This drives the REAL ``_run_optimizer_lp`` gate (the
# ``trigger_runs_scenarios(...)`` block in optimizer.py) end-to-end with
# trigger_reason="soc_drift": scenarios must run, the pessimistic charge floor
# must be applied (exogenous_snapshot["pess_charge_floor"] populated + a
# floored re-solve issued), and the same pipeline under trigger_reason="manual"
# must stay nominal-only. ``solve_lp`` is stubbed (as in
# test_lp_appliance_real_solver.py) so CBC never runs — the subject here is
# the gating/orchestration, not the solver.

_GATE_TARIFF = "E-1R-AGILE-TEST-PESS-FLOOR-GATE"


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _seed_cheap_to_peak_day(start):
    from src import db

    rows = []
    vf = start
    for _ in range(48):
        price = 35.0 if 16 <= vf.hour < 19 else 10.0  # all positive: no floor exemptions
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, _GATE_TARIFF)


def _stub_plan(slot_starts_utc, price_pence, *, soc, obj):
    from src.scheduler.lp_optimizer import LpPlan

    n = len(slot_starts_utc)
    plan = LpPlan(ok=True, status="Optimal", objective_pence=obj)
    plan.slot_starts_utc = list(slot_starts_utc)
    plan.price_pence = list(price_pence)
    plan.temp_outdoor_c = [12.0] * n
    plan.import_kwh = [0.0] * n
    plan.export_kwh = [0.0] * n
    plan.battery_charge_kwh = [0.0] * n
    plan.battery_discharge_kwh = [0.0] * n
    plan.pv_use_kwh = [0.0] * n
    plan.pv_curtail_kwh = [0.0] * n
    plan.dhw_electric_kwh = [0.0] * n
    plan.space_electric_kwh = [0.0] * n
    plan.lwt_offset_c = [0.0] * n
    plan.tank_temp_c = [45.0] * (n + 1)
    plan.soc_kwh = [soc] * (n + 1)
    plan.soc_floor_slack_kwh = 0.0
    return plan


def test_soc_drift_trigger_runs_scenarios_and_charge_floor_but_manual_does_not(
    monkeypatch,
):
    from src import db
    from src.scheduler import optimizer

    db.init_db()
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", _GATE_TARIFF)
    monkeypatch.setattr(config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    monkeypatch.setattr(config, "LP_PESS_CHARGE_FLOOR_ENABLED", True)

    now = datetime(2026, 5, 20, 13, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_cheap_to_peak_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_cheap_to_peak_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))

    # Nominal solve holds SoC flat at 2.0; a floor re-solve (soc_floor_kwh
    # passed) is recorded and returns the pessimistic-level trajectory.
    floor_resolves = []

    def _fake_nominal_solve(*, slot_starts_utc, price_pence, soc_floor_kwh=None, **kw):
        if soc_floor_kwh is not None:
            floor_resolves.append(max(soc_floor_kwh))
            return _stub_plan(slot_starts_utc, price_pence, soc=6.0, obj=110.0)
        return _stub_plan(slot_starts_utc, price_pence, soc=2.0, obj=100.0)

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _fake_nominal_solve)
    # scenarios.py binds solve_lp at import time — patch its module attribute
    # so the 2 side solves (optimistic/pessimistic) want SoC 6.0 > nominal 2.0.
    monkeypatch.setattr(
        "src.scheduler.scenarios.solve_lp",
        lambda *, slot_starts_utc, price_pence, **kw: _stub_plan(
            slot_starts_utc, price_pence, soc=6.0, obj=105.0
        ),
    )

    # Spy on the real floor helper to observe the snapshot it populates.
    real_floor = optimizer._apply_pessimistic_charge_floor
    floor_seen = {}

    def _spy(plan, scenarios_dict, *, solve_kwargs, exogenous_snapshot):
        out = real_floor(
            plan, scenarios_dict,
            solve_kwargs=solve_kwargs, exogenous_snapshot=exogenous_snapshot,
        )
        floor_seen["scenarios"] = set(scenarios_dict)
        floor_seen["snapshot"] = exogenous_snapshot
        return out

    monkeypatch.setattr(optimizer, "_apply_pessimistic_charge_floor", _spy)

    result = optimizer.run_optimizer(fox=None, daikin=None, trigger_reason="soc_drift")
    assert result["ok"] is True, result
    assert result["scenarios_run"] is True
    assert {"nominal", "optimistic", "pessimistic"} <= floor_seen["scenarios"]
    pcf = floor_seen["snapshot"]["pess_charge_floor"]
    assert pcf["binding_slots"] >= 1
    assert pcf["insurance_cost_pence"] == pytest.approx(10.0)
    tol = float(config.LP_PESS_CHARGE_FLOOR_TOLERANCE_KWH)
    assert floor_resolves == [pytest.approx(6.0 - tol)]

    # Contrast: `manual` stays nominal-only — no scenarios, no floor.
    floor_seen.clear()
    floor_resolves.clear()
    result_manual = optimizer.run_optimizer(
        fox=None, daikin=None, trigger_reason="manual"
    )
    assert result_manual["ok"] is True, result_manual
    assert result_manual["scenarios_run"] is False
    assert not floor_seen and not floor_resolves


# --- #728: LP_PESS_CHARGE_FLOOR_SCOPE=peak_entry --------------------------------

@pytest.fixture(autouse=True)
def _pin_tz(monkeypatch):
    # The peak-entry classifier groups slots by config.BULLETPROOF_TIMEZONE —
    # pin it so a host env override can't shift the local-day grouping and
    # move the expected entry indices. January dates → London == UTC.
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    yield


def _day_plan(soc_flat=2.0, *, peak=range(34, 42), base_p=15.0, peak_p=40.0):
    """48-slot day from local midnight (January → Europe/London == UTC) with a
    4 h severe/expensive evening window (40p vs 15p median → ≥1.5× + >30p)."""
    n = 48
    plan = SimpleNamespace(
        ok=True, status="Optimal", objective_pence=100.0,
        slot_starts_utc=[
            datetime(2026, 1, 15, 0, 0, tzinfo=UTC) + timedelta(minutes=30 * i)
            for i in range(n)
        ],
        soc_kwh=[soc_flat] * (n + 1),
        price_pence=[peak_p if i in peak else base_p for i in range(n)],
    )
    return plan


def test_peak_entry_indices_find_the_evening_window():
    plan = _day_plan()
    idx = opt_mod._peak_entry_floor_indices(plan.slot_starts_utc, plan.price_pence)
    assert idx == [34]


def test_peak_entry_scope_floors_only_the_entry_boundary(monkeypatch):
    monkeypatch.setattr(config, "LP_PESS_CHARGE_FLOOR_SCOPE", "peak_entry")
    nominal = _day_plan(2.0)
    pess = _day_plan(2.0)
    pess.soc_kwh = [6.0] * 49                        # pessimistic wants 6.0 held
    floored = _day_plan(6.0)
    floored.objective_pence = 108.0
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
    floor = captured["floor"]
    # ONLY slot 33 — the boundary entering the 17:00 window — carries a floor:
    # soc[34] (peak-entry SoC) is end-of-slot-33 in constraint indexing.
    assert floor[33] == pytest.approx(6.0 - tol)
    assert all(f == 0.0 for i, f in enumerate(floor) if i != 33)
    assert snap["pess_charge_floor"]["scope"] == "peak_entry"
    assert snap["pess_charge_floor"]["entry_slots"] == [34]
    assert snap["pess_charge_floor"]["binding_slots"] == 1


def test_peak_entry_scope_no_resolve_when_nominal_covers_entry(monkeypatch):
    """Nominal already ≥ pess at the peak boundary → no re-solve even though
    the pessimistic TRAJECTORY is higher elsewhere (the whole point of #728:
    mid-morning path differences alone must not force a grid charge)."""
    monkeypatch.setattr(config, "LP_PESS_CHARGE_FLOOR_SCOPE", "peak_entry")
    nominal = _day_plan(2.0)
    nominal.soc_kwh = [2.0] * 34 + [6.5] * 15        # fills by the entry, late
    pess = _day_plan(2.0)
    pess.soc_kwh = [6.0] * 49                        # higher path ALL day
    called = []
    monkeypatch.setattr(
        "src.scheduler.lp_optimizer.solve_lp", lambda **kw: called.append(1)
    )
    out = opt_mod._apply_pessimistic_charge_floor(
        nominal, {"pessimistic": SimpleNamespace(plan=pess)},
        solve_kwargs={}, exogenous_snapshot={},
    )
    assert out is nominal and not called


def test_trajectory_scope_stays_default_and_stamped(monkeypatch):
    nominal = _fake_plan([2.0, 2.0, 2.0, 2.0, 2.0])
    pess = _fake_plan([2.0, 4.0, 6.0, 6.0, 5.0])
    floored = _fake_plan([2.0, 4.0, 6.0, 6.0, 5.0], obj=110.0)
    floored.soc_floor_slack_kwh = 0.0
    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", lambda **kw: floored)
    snap = {}
    opt_mod._apply_pessimistic_charge_floor(
        nominal, {"pessimistic": SimpleNamespace(plan=pess)},
        solve_kwargs={}, exogenous_snapshot=snap,
    )
    assert snap["pess_charge_floor"]["scope"] == "trajectory"
    assert snap["pess_charge_floor"]["entry_slots"] == []


def test_peak_entry_merges_contiguous_expensive_and_severe_windows():
    """An expensive window butted against a severe_peak one is the SAME peak
    for charging purposes — one entry, not one per tier transition."""
    plan = _day_plan()
    # 15:00-17:00 expensive (28p > 25p abs floor), 17:00-21:00 severe (40p)
    for i in range(30, 34):
        plan.price_pence[i] = 28.0
    idx = opt_mod._peak_entry_floor_indices(plan.slot_starts_utc, plan.price_pence)
    assert idx == [30]
