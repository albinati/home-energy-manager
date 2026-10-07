"""Battery policy for autumn/winter on a banded tariff (#803 / #806).

* ``LP_BATTERY_EXPORT_ENABLED=false`` — the battery never discharges to the
  grid in ANY preset (no peak_export, no pre-negative drain, no ForceDischarge
  group); incidental PV surplus still exports.
* ``LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH`` — soft "no grid import in the peak
  band" term; feasible by construction.
* Heartbeat peak-import guard — one alert per peak window + replan.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config as app_config
from src.scheduler.lp_dispatch import build_fox_groups_from_lp, lp_plan_to_slots
from src.scheduler.lp_optimizer import LpInitialState, LpPlan, solve_lp
from src.weather import WeatherLpSeries

COSY_CHEAP, COSY_DAY, COSY_PEAK = 12.4868, 25.4461, 38.174
TZ = ZoneInfo("Europe/London")
DAY = date(2026, 10, 14)


def _series(starts: list[datetime], *, pv: float, t_out: float = 9.0) -> WeatherLpSeries:
    n = len(starts)
    return WeatherLpSeries(
        slot_starts_utc=starts, temperature_outdoor_c=[t_out] * n, shortwave_radiation_wm2=[100.0] * n,
        cloud_cover_pct=[60.0] * n, pv_kwh_per_slot=[pv] * n, cop_space=[3.0] * n, cop_dhw=[2.6] * n,
    )


def _cosy_day(d: date) -> tuple[list[datetime], list[float]]:
    starts, prices = [], []
    for h in range(24):
        for m in (0, 30):
            starts.append(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).astimezone(UTC))
            prices.append(COSY_CHEAP if (4 <= h < 7 or 13 <= h < 16 or h >= 22) else COSY_PEAK if 16 <= h < 19 else COSY_DAY)
    return starts, prices


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_STRUCTURE", "auto", raising=False)
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 20)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    monkeypatch.setattr(app_config, "LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH", 0.0, raising=False)
    monkeypatch.setattr(app_config, "LP_BATTERY_EXPORT_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(app_config, "MIN_SOC_RESERVE_PERCENT", 15.0, raising=False)
    monkeypatch.setattr(app_config, "BATTERY_CAPACITY_KWH", 10.0, raising=False)
    monkeypatch.setattr(app_config, "LP_PESS_CHARGE_FLOOR_ENABLED", False, raising=False)
    db.init_db()


def _solve(starts, prices, *, soc, pv, load=0.2, export_price=None):
    return solve_lp(
        slot_starts_utc=starts, price_pence=prices, base_load_kwh=[load] * len(starts),
        weather=_series(starts, pv=pv), initial=LpInitialState(soc_kwh=soc, tank_temp_c=50.0), tz=TZ,
        export_price_pence=[export_price] * len(starts) if export_price is not None else None,
    )


# ── LP: battery→grid export policy ───────────────────────────────────────────


def test_export_disabled_caps_exp_at_pv_in_vacation(monkeypatch):
    """Vacation used to allow ``exp <= pv_use + dis``. With the policy off the
    battery stays in the house even at a 30p export price."""
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    base = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    starts = [base + timedelta(minutes=30 * i) for i in range(8)]
    prices = [5.0] * 8
    on = _solve(starts, prices, soc=9.0, pv=0.0, export_price=30.0)
    assert on.ok and sum(on.export_kwh) > 0.5  # arbitrage happens when allowed
    monkeypatch.setattr(app_config, "LP_BATTERY_EXPORT_ENABLED", False, raising=False)
    off = _solve(starts, prices, soc=9.0, pv=0.0, export_price=30.0)
    assert off.ok and sum(off.export_kwh) == pytest.approx(0.0, abs=1e-6)


def test_export_disabled_blocks_pre_negative_drain(monkeypatch):
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-H")  # dynamic, negatives
    base = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    starts = [base + timedelta(minutes=30 * i) for i in range(12)]
    prices = [10.0] * 8 + [-5.0] * 4
    on = _solve(starts, prices, soc=9.0, pv=0.0, export_price=15.0)
    assert on.ok and on.pre_negative_export_slots and sum(on.export_kwh) > 0.5
    monkeypatch.setattr(app_config, "LP_BATTERY_EXPORT_ENABLED", False, raising=False)
    off = _solve(starts, prices, soc=9.0, pv=0.0, export_price=15.0)
    assert off.ok and off.pre_negative_export_slots == [] and sum(off.export_kwh) == pytest.approx(0.0, abs=1e-6)


def test_pv_surplus_still_exports_with_policy_off(monkeypatch):
    monkeypatch.setattr(app_config, "LP_BATTERY_EXPORT_ENABLED", False, raising=False)
    base = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)
    starts = [base + timedelta(minutes=30 * i) for i in range(6)]
    plan = _solve(starts, [20.0] * 6, soc=9.9, pv=1.5, load=0.1, export_price=4.1)
    assert plan.ok and sum(plan.export_kwh) > 1.0
    for e, p in zip(plan.export_kwh, plan.pv_use_kwh):
        assert e <= p + 1e-6  # never more than PV


# ── LP: peak-import penalty ──────────────────────────────────────────────────


def test_cosy_peak_slots_import_zero_when_battery_sufficient(monkeypatch):
    monkeypatch.setattr(app_config, "LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH", 100.0, raising=False)
    starts, prices = _cosy_day(DAY)
    plan = _solve(starts, prices, soc=8.0, pv=0.1, load=0.4)
    assert plan.ok and plan.peak_import_penalty_applied
    assert plan.peak_import_kwh == pytest.approx(0.0, abs=1e-6)
    peak_idx = [i for i, p in enumerate(prices) if p == COSY_PEAK]
    assert all(plan.import_kwh[i] < 1e-6 for i in peak_idx)
    # and the battery covers the peak: it still holds charge entering 16:00
    assert plan.soc_kwh[peak_idx[0]] >= 1.5 + 2.4 / (0.92 ** 0.5)  # reserve + the peak's load


def test_penalty_keeps_feasible_when_battery_cannot_cover_peak(monkeypatch):
    """Load beyond what the battery can hold through the peak: the soft term
    lets the LP import at 38p instead of going Infeasible."""
    monkeypatch.setattr(app_config, "LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH", 100.0, raising=False)
    starts, prices = _cosy_day(DAY)
    plan = _solve(starts, prices, soc=1.5, pv=0.0, load=2.0)
    assert plan.ok, plan.status
    assert plan.peak_import_kwh > 0.5


def test_dynamic_tariff_unaffected_by_default(monkeypatch):
    import random

    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-H")
    rnd = random.Random(11)
    base = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
    starts = [base + timedelta(minutes=30 * i) for i in range(24)]
    prices = [round(rnd.uniform(5, 40), 2) for _ in range(24)]
    a = _solve(starts, prices, soc=4.0, pv=0.3)
    monkeypatch.setattr(app_config, "LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH", 100.0, raising=False)
    b = _solve(starts, prices, soc=4.0, pv=0.3)
    assert not b.peak_import_penalty_applied
    assert b.objective_pence == pytest.approx(a.objective_pence, abs=1e-6)


# ── dispatch: the wire ───────────────────────────────────────────────────────


def _synthetic_plan_with_peak_export() -> LpPlan:
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    n = 6
    starts = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0, slot_starts_utc=starts,
                  price_pence=[30.0] * n, peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    for i in range(n):
        drain = i in (2, 3)
        plan.import_kwh.append(0.0)
        plan.export_kwh.append(1.0 if drain else 0.0)
        plan.battery_charge_kwh.append(0.0)
        plan.battery_discharge_kwh.append(1.2 if drain else 0.2)
        plan.pv_use_kwh.append(0.0)
        plan.pv_curtail_kwh.append(0.0)
        plan.dhw_electric_kwh.append(0.0)
        plan.space_electric_kwh.append(0.0)
        plan.lwt_offset_c.append(0.0)
        plan.temp_outdoor_c.append(9.0)
    plan.soc_kwh.extend([8.0 - 0.5 * i for i in range(n + 1)])
    plan.tank_temp_c.extend([48.0] * (n + 1))
    return plan


def test_no_forcedischarge_group_when_export_disabled(monkeypatch):
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    plan = _synthetic_plan_with_peak_export()
    assert any(s.kind == "peak_export" for s in lp_plan_to_slots(plan))  # the labeller sees a drain
    groups_on, _ = build_fox_groups_from_lp(plan)
    assert any(g.work_mode == "ForceDischarge" for g in groups_on)
    monkeypatch.setattr(app_config, "LP_BATTERY_EXPORT_ENABLED", False, raising=False)
    logged: list[dict] = []
    monkeypatch.setattr(db, "log_action", lambda **kw: logged.append(kw))
    groups_off, _ = build_fox_groups_from_lp(plan)
    assert all(g.work_mode != "ForceDischarge" for g in groups_off)
    assert len([x for x in logged if x["action"] == "export_slot_suppressed"]) == 2  # one audit row per slot


def test_cosy_peak_window_is_selfuse(monkeypatch):
    monkeypatch.setattr(app_config, "LP_BATTERY_EXPORT_ENABLED", False, raising=False)
    monkeypatch.setattr(app_config, "LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH", 100.0, raising=False)
    d = datetime.now(TZ).date() + timedelta(days=1)
    starts, prices = _cosy_day(d)
    plan = _solve(starts, prices, soc=8.0, pv=0.1, load=0.4)
    assert plan.ok
    slots = lp_plan_to_slots(plan)
    for s, p in zip(slots, prices):
        if p == COSY_PEAK:
            assert s.kind in ("standard", "peak", "tank_idle_overnight")
    assert plan.peak_import_kwh == pytest.approx(0.0, abs=1e-6)
    groups, _ = build_fox_groups_from_lp(plan)
    assert all(g.work_mode != "ForceDischarge" for g in groups)
    # every minute of 16:00-18:59 local is covered by a SelfUse group at the
    # reserve floor, or by no group at all (= firmware SelfUse)
    for h in (16, 17, 18):
        for m in (0, 30):
            t = h * 60 + m
            covering = [g for g in groups if g.start_hour * 60 + g.start_minute <= t <= g.end_hour * 60 + g.end_minute]
            for g in covering:
                assert g.work_mode == "SelfUse", f"{h:02d}:{m:02d} covered by {g.work_mode}"


# ── heartbeat: peak-import guard ─────────────────────────────────────────────


@pytest.fixture
def guard_env(monkeypatch):
    from src.scheduler import runner

    runner._peak_guard_ticks = 0
    runner._peak_guard_window_key = None
    runner._peak_guard_alerted_key = None
    monkeypatch.setattr(app_config, "PEAK_IMPORT_GUARD_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "PEAK_IMPORT_GUARD_KW", 0.3, raising=False)
    monkeypatch.setattr(app_config, "PEAK_IMPORT_GUARD_TICKS", 2, raising=False)
    monkeypatch.setattr(app_config, "PEAK_IMPORT_GUARD_ACTION", "replan", raising=False)
    db.save_daily_target({"date": DAY.isoformat(), "cheap_threshold": 18.97, "peak_threshold": 31.81})
    alerts: list[tuple[str, dict]] = []
    replans: list[dict] = []
    logged: list[dict] = []
    monkeypatch.setattr(runner, "notify_risk", lambda msg, extra=None: alerts.append((msg, extra or {})))
    monkeypatch.setattr(runner, "bulletproof_mpc_job", lambda **kw: replans.append(kw) or True)
    monkeypatch.setattr(runner.db, "log_action", lambda **kw: logged.append(kw))
    monkeypatch.setattr(runner, "_lp_planned_import_kwh_at", lambda slot_start: 0.0)
    return runner, alerts, replans, logged


def _tick(runner, *, price, grid_kw, minute=0):
    now_local = datetime(DAY.year, DAY.month, DAY.day, 16, minute, tzinfo=TZ)
    return runner._peak_import_guard_tick(now_local=now_local, plan_date=DAY.isoformat(),
                                          price=price, grid_kw=grid_kw, soc=40.0)


def test_peak_guard_fires_after_n_ticks(guard_env):
    runner, alerts, replans, logged = guard_env
    assert _tick(runner, price=COSY_PEAK, grid_kw=0.8)["fired"] is False
    out = _tick(runner, price=COSY_PEAK, grid_kw=0.9, minute=5)
    assert out["fired"] is True and out["ticks"] == 2
    assert len(alerts) == 1 and alerts[0][1]["warning_key"].startswith("peak_import_2026-10-14_1600")
    assert len(replans) == 1 and replans[0]["trigger_reason"] == "peak_import" and replans[0]["bypass_cooldown"] is True
    assert logged[0]["action"] == "peak_import_guard" and logged[0]["params"]["replanned"] is True
    assert "re-planned" in alerts[0][0]


def test_peak_guard_notifies_once_per_window(guard_env):
    runner, alerts, replans, _ = guard_env
    for m in range(0, 30, 5):
        _tick(runner, price=COSY_PEAK, grid_kw=1.0, minute=m)
    assert len(alerts) == 1 and len(replans) == 1


def test_peak_guard_resets_below_threshold_and_outside_peak(guard_env):
    runner, alerts, _, _ = guard_env
    _tick(runner, price=COSY_PEAK, grid_kw=1.0)
    _tick(runner, price=COSY_PEAK, grid_kw=0.0, minute=5)      # import stopped → counter resets
    assert _tick(runner, price=COSY_PEAK, grid_kw=1.0, minute=10)["ticks"] == 1
    assert alerts == []
    out = _tick(runner, price=COSY_DAY, grid_kw=2.0, minute=15)  # day band: inert
    assert out["in_peak"] is False and alerts == []


def test_peak_guard_kill_switch(guard_env, monkeypatch):
    runner, alerts, replans, _ = guard_env
    monkeypatch.setattr(app_config, "PEAK_IMPORT_GUARD_ENABLED", False, raising=False)
    for m in range(0, 20, 5):
        _tick(runner, price=COSY_PEAK, grid_kw=2.0, minute=m)
    assert alerts == [] and replans == []


def test_peak_guard_action_none_alerts_without_replan(guard_env, monkeypatch):
    runner, alerts, replans, _ = guard_env
    monkeypatch.setattr(app_config, "PEAK_IMPORT_GUARD_ACTION", "none", raising=False)
    _tick(runner, price=COSY_PEAK, grid_kw=1.0)
    _tick(runner, price=COSY_PEAK, grid_kw=1.0, minute=5)
    assert len(alerts) == 1 and replans == []


# ── review follow-ups ────────────────────────────────────────────────────────


def test_peak_guard_inert_on_dynamic_tariff(guard_env, monkeypatch):
    """F1: on Agile the guard never pages (26-35p evenings with 0.5 kW import
    are normal)."""
    runner, alerts, replans, _ = guard_env
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-H")
    for m in range(0, 30, 5):
        out = _tick(runner, price=34.0, grid_kw=2.0, minute=m)
    assert out["in_peak"] is False and alerts == [] and replans == []


def test_peak_guard_none_price_keeps_window_state(guard_env):
    runner, alerts, _, _ = guard_env
    _tick(runner, price=COSY_PEAK, grid_kw=1.0)
    _tick(runner, price=COSY_PEAK, grid_kw=1.0, minute=5)
    assert len(alerts) == 1
    _tick(runner, price=None, grid_kw=1.0, minute=10)       # transient rates miss
    _tick(runner, price=COSY_PEAK, grid_kw=1.0, minute=15)
    _tick(runner, price=COSY_PEAK, grid_kw=1.0, minute=20)
    assert len(alerts) == 1  # same window, no second alert


def test_peak_guard_quiet_when_import_was_planned(guard_env, monkeypatch):
    """F4: the committed plan already buys this slot → not a plan failure."""
    runner, alerts, replans, _ = guard_env
    monkeypatch.setattr(runner, "_lp_planned_import_kwh_at", lambda slot_start: 0.6)
    _tick(runner, price=COSY_PEAK, grid_kw=1.0)
    out = _tick(runner, price=COSY_PEAK, grid_kw=1.0, minute=5)
    assert out.get("planned") is True and alerts == [] and replans == []


def test_peak_guard_records_swallowed_replan(guard_env, monkeypatch):
    """F2: a replan the MPC job declined is recorded honestly."""
    runner, alerts, _, logged = guard_env
    monkeypatch.setattr(runner, "bulletproof_mpc_job", lambda **kw: False)
    _tick(runner, price=COSY_PEAK, grid_kw=1.0)
    _tick(runner, price=COSY_PEAK, grid_kw=1.0, minute=5)
    assert logged[0]["params"]["replanned"] is False and logged[0]["result"] == "failure"
    assert "NOT run" in alerts[0][0]


def test_export_suppression_is_a_dispatch_decision(monkeypatch):
    """F3: the downgrade is decided inside filter_robust_peak_export so the
    audit trail matches the uploaded groups."""
    from src.scheduler.lp_dispatch import filter_robust_peak_export

    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    monkeypatch.setattr(app_config, "LP_BATTERY_EXPORT_ENABLED", False, raising=False)
    plan = _synthetic_plan_with_peak_export()
    slots, decisions = filter_robust_peak_export(plan, None)
    assert all(s.kind != "peak_export" for s in slots)
    sup = [d for d in decisions if d["reason"] == "export_disabled"]
    assert len(sup) == 2 and all(d["committed"] is False and d["dispatched_kind"] == "standard" for d in sup)


def test_objective_reported_net_of_peak_penalty(monkeypatch):
    """F5: the 100p policy penalty must not leak into the reported economics."""
    starts, prices = _cosy_day(DAY)
    base = _solve(starts, prices, soc=1.5, pv=0.0, load=2.0)
    monkeypatch.setattr(app_config, "LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH", 100.0, raising=False)
    pen = _solve(starts, prices, soc=1.5, pv=0.0, load=2.0)
    assert pen.peak_import_kwh > 0.5 and pen.peak_import_penalty_pence == pytest.approx(100.0 * pen.peak_import_kwh)
    # net objective is an economic cost of the same order as the unpenalised solve
    assert pen.objective_pence < base.objective_pence + 150.0
