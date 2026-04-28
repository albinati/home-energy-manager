"""Tests for scenario-LP perturbations and the trigger allow-list.

Pure unit tests — no LP solve invoked here (those would be slow and require
a tariff/rates fixture). The full integration is covered indirectly via
``test_lp_dispatch_robust_filter.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.config import config
from src.scheduler import scenarios
from src.weather import WeatherLpSeries


def _weather_fixture(n: int = 4) -> WeatherLpSeries:
    return WeatherLpSeries(
        slot_starts_utc=[
            datetime(2026, 5, 1, 12, 0, tzinfo=UTC).replace(minute=30 * (i % 2))
            for i in range(n)
        ],
        temperature_outdoor_c=[10.0] * n,
        shortwave_radiation_wm2=[200.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=[0.5] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )


def test_perturb_weather_pessimistic_shifts_temp_and_recomputes_cop():
    w = _weather_fixture()
    p = scenarios.perturb_weather(w, temp_delta_c=-1.5)
    assert p is not w  # new instance
    assert all(t == 8.5 for t in p.temperature_outdoor_c)
    # COP should be recomputed (LiFePO4 / heat pump sees colder air → lower COP).
    # Exact values depend on DAIKIN_COP_CURVE; we only check the shape.
    assert len(p.cop_space) == len(w.cop_space)
    assert len(p.cop_dhw) == len(w.cop_dhw)
    # PV irradiance unchanged — temperature decoupled from irradiance.
    assert p.pv_kwh_per_slot == w.pv_kwh_per_slot


def test_perturb_weather_zero_delta_returns_input():
    w = _weather_fixture()
    p = scenarios.perturb_weather(w, temp_delta_c=0.0)
    assert p is w  # identity short-circuit


def test_perturb_base_load_factor_one_returns_copy():
    bl = [0.4, 0.5, 0.3]
    out = scenarios.perturb_base_load(bl, factor=1.0)
    assert out == bl
    assert out is not bl  # copy semantics


def test_perturb_base_load_pessimistic_factor():
    bl = [0.4, 0.5, 0.3]
    out = scenarios.perturb_base_load(bl, factor=1.15)
    assert out == pytest.approx([0.46, 0.575, 0.345])


def test_perturb_base_load_negative_clamped():
    bl = [0.4, -0.1, 0.3]  # negatives shouldn't happen in real data, defensive
    out = scenarios.perturb_base_load(bl, factor=1.5)
    assert out[1] == 0.0


def test_perturbation_for_scenarios_match_config_defaults():
    p_pess = scenarios._perturbation_for("pessimistic")
    assert p_pess.temp_delta_c == config.LP_SCENARIO_PESSIMISTIC_TEMP_DELTA_C
    assert p_pess.load_factor == config.LP_SCENARIO_PESSIMISTIC_LOAD_FACTOR

    p_opt = scenarios._perturbation_for("optimistic")
    assert p_opt.temp_delta_c == config.LP_SCENARIO_OPTIMISTIC_TEMP_DELTA_C
    assert p_opt.load_factor == config.LP_SCENARIO_OPTIMISTIC_LOAD_FACTOR

    p_nom = scenarios._perturbation_for("nominal")
    assert p_nom.temp_delta_c == 0.0
    assert p_nom.load_factor == 1.0


def test_perturbation_for_unknown_raises():
    with pytest.raises(ValueError):
        scenarios._perturbation_for("paranoid")  # type: ignore[arg-type]


def test_trigger_runs_scenarios_default_includes_octopus_fetch():
    # Default LP_SCENARIOS_ON_TRIGGER_REASONS = "cron,plan_push,octopus_fetch"
    assert scenarios.trigger_runs_scenarios("cron")
    assert scenarios.trigger_runs_scenarios("plan_push")
    assert scenarios.trigger_runs_scenarios("octopus_fetch")
    assert not scenarios.trigger_runs_scenarios("soc_drift")
    assert not scenarios.trigger_runs_scenarios("forecast_revision")
    assert not scenarios.trigger_runs_scenarios("dynamic_replan")
    assert not scenarios.trigger_runs_scenarios("manual")


def test_trigger_runs_scenarios_handles_whitespace_and_case(monkeypatch):
    monkeypatch.setattr(
        scenarios.config,
        "LP_SCENARIOS_ON_TRIGGER_REASONS",
        "Cron,  PLAN_PUSH ,Octopus_Fetch",
        raising=False,
    )
    assert scenarios.trigger_runs_scenarios("cron")
    assert scenarios.trigger_runs_scenarios("plan_push")
    assert scenarios.trigger_runs_scenarios("octopus_fetch")


def test_trigger_runs_scenarios_empty_disables(monkeypatch):
    monkeypatch.setattr(
        scenarios.config, "LP_SCENARIOS_ON_TRIGGER_REASONS", "", raising=False
    )
    assert not scenarios.trigger_runs_scenarios("cron")
    assert not scenarios.trigger_runs_scenarios("plan_push")


# -----------------------------------------------------------------------
# Parallel solve_scenarios — verifies wall-clock parallelism via mocked solve.
# -----------------------------------------------------------------------
def test_solve_scenarios_runs_three_in_parallel(monkeypatch):
    """Each fake solve sleeps 0.3 s. Sequential = ≥ 0.9 s. Parallel ≤ ~0.5 s.

    Uses time.monotonic() before/after to assert the executor actually
    overlaps. We don't measure exact speedup (CI variability) — just
    "did all three run inside one slowest-solve duration plus margin".
    """
    import time as _time
    from src.scheduler.lp_optimizer import LpPlan

    sleep_s = 0.3
    call_times: list[float] = []

    def _fake_solve_lp(**kwargs):
        call_times.append(_time.monotonic())
        _time.sleep(sleep_s)
        return LpPlan(ok=True, status="Optimal", objective_pence=0.0)

    monkeypatch.setattr(scenarios, "solve_lp", _fake_solve_lp)

    t0 = _time.monotonic()
    out = scenarios.solve_scenarios(
        slot_starts_utc=[],
        price_pence=[],
        base_load_kwh=[],
        weather=_weather_fixture(),
        initial=None,
        tz=None,
    )
    elapsed = _time.monotonic() - t0

    # All three scenarios returned.
    assert set(out.keys()) == {"optimistic", "nominal", "pessimistic"}
    # Result wraps the plan.
    assert all(r.plan.ok for r in out.values())

    # Three calls were dispatched.
    assert len(call_times) == 3
    # The three calls all started within ~0.1 s of each other → parallel,
    # not serial. (Sequential would have ≥ 0.3 s gaps between starts.)
    span = max(call_times) - min(call_times)
    assert span < 0.15, f"calls were serial-like (span={span:.3f}s)"

    # Total wall-clock < 2 × single-solve. Generous bound to avoid flakes.
    assert elapsed < 2 * sleep_s, f"wall-clock {elapsed:.3f}s suggests no parallelism"


def test_solve_scenarios_individual_failure_does_not_break_batch(monkeypatch):
    """When pessimistic raises but optimistic + nominal succeed, the batch
    still returns three results; the failed one carries plan.ok=False
    plus an ``error`` string for the audit log."""
    from src.scheduler.lp_optimizer import LpPlan

    def _solve(**kwargs):
        # Detect pessimistic via the perturbed temperature in weather input.
        w = kwargs["weather"]
        if w.temperature_outdoor_c and w.temperature_outdoor_c[0] < 9.0:
            raise RuntimeError("pessimistic LP infeasible")
        return LpPlan(ok=True, status="Optimal", objective_pence=0.0)

    monkeypatch.setattr(scenarios, "solve_lp", _solve)

    out = scenarios.solve_scenarios(
        slot_starts_utc=[],
        price_pence=[],
        base_load_kwh=[],
        weather=_weather_fixture(),
        initial=None,
        tz=None,
    )
    assert out["optimistic"].plan.ok
    assert out["nominal"].plan.ok
    assert not out["pessimistic"].plan.ok
    assert out["pessimistic"].error == "pessimistic LP infeasible"
    assert out["pessimistic"].duration_ms >= 0


def test_solve_scenarios_with_nominal_short_circuits():
    """When the nominal plan is supplied, only the two side scenarios are
    re-solved. Verify by counting how many times solve_lp gets called."""
    import unittest.mock
    from src.scheduler.lp_optimizer import LpPlan

    nominal = LpPlan(ok=True, status="Optimal", objective_pence=42.0)
    with unittest.mock.patch.object(scenarios, "solve_lp") as mock:
        mock.return_value = LpPlan(ok=True, status="Optimal", objective_pence=0.0)
        out = scenarios.solve_scenarios_with_nominal(
            nominal=nominal,
            slot_starts_utc=[],
            price_pence=[],
            base_load_kwh=[],
            weather=_weather_fixture(),
            initial=None,
            tz=None,
        )
    # Only optimistic + pessimistic re-solved; nominal reused.
    assert mock.call_count == 2
    assert out["nominal"].plan is nominal
    assert out["nominal"].duration_ms == 0  # not re-timed
    assert out["optimistic"].plan.ok
    assert out["pessimistic"].plan.ok
