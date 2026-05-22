"""When realtime SoC has slipped below the operational reserve
(``MIN_SOC_RESERVE_PERCENT``), the LP must stay feasible.

Pre-fix bug: ``soc`` LpVariable had ``lowBound=soc_min`` AND ``soc[0] ==
initial.soc_kwh`` was a hard equality. Whenever realtime SoC < reserve, that
combination was unsatisfiable → PuLP returned ``Infeasible`` → optimizer.py
fell back to the heuristic which uploaded Fox V3 ForceCharge groups with
``fdPwr=3000 W, fdSoc=95`` defaults, destructively grid-charging the battery
(see [[project_heuristic_fox_dispatch_bug]]).

Audit on 2026-05-18 found four such infeasibles in a single overnight when
prod's realtime SoC dipped to 12-15 % (= 1.04-1.55 kWh on a 10.36 kWh
battery with 15 % reserve = 1.55 kWh).

Fix: relax ``soc[0]`` lowBound to 0 (so the equality with the realtime
initial value is always satisfiable). Forward slots ``soc[1..n]`` keep the
hard reserve bound — the LP must plan a path that gets back to reserve in
slot 1, which is normally feasible (just charge from grid). The residual
case where pre-plunge or PV-sufficiency guards also block slot-0 charging
is caught by PR #338's defensive hold-previous-schedule fallback.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db as _db
from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _init_db():
    _db.init_db()


def _starts(n: int, base: datetime) -> list[datetime]:
    return [base + timedelta(minutes=30 * i) for i in range(n)]


def _weather(starts: list[datetime], outdoor_c: float = 12.0) -> WeatherLpSeries:
    """Mild outdoor + no PV. Forces the LP to lean on initial SoC + grid."""
    n = len(starts)
    return WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[outdoor_c] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )


def test_lp_feasible_when_initial_soc_below_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-fix regression. Initial SoC = 12 % (1.24 kWh on a 10.36 kWh
    battery with 15 % reserve = 1.55 kWh). Pre-fix: PuLP Infeasible. Post-fix:
    Optimal with the first slots paying the reserve slack until natural
    charging recovers SoC.
    """
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.36)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 15.0)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive")  # skip shower/legionella hard floors

    n = 12
    starts = _starts(n, datetime(2026, 5, 18, 1, 0, tzinfo=UTC))
    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=[15.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_weather(starts),
        initial=LpInitialState(soc_kwh=1.24, tank_temp_c=45.0),
        tz=starts[0].tzinfo,
    )
    assert plan.ok, f"LP should solve from below-reserve SoC, got status={plan.status!r}"
    assert plan.soc_kwh[0] == pytest.approx(1.24, abs=0.01), (
        "Initial SoC must be preserved exactly, not silently clamped"
    )
    # Battery should recover toward / above reserve within a few slots
    # (LP_SOC_RESERVE_PENALTY = 100 p/kWh dominates a 15 p Agile slot).
    last_soc = plan.soc_kwh[-1]
    soc_min = 10.36 * 0.15
    assert last_soc >= soc_min - 1e-6, (
        f"LP should recover SoC to >= reserve {soc_min:.3f}, got {last_soc:.3f}"
    )


def test_lp_feasible_at_realistic_prod_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces the 2026-05-18 00:58 UTC prod infeasible: SoC=12 % overnight,
    tank=57 °C, outdoor=7 °C, no PV. Daikin in active mode (the prod default).
    """
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.36)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 15.0)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active")
    # PR C — ENERGY_STRATEGY_MODE removed (was here).

    n = 24  # 12 h horizon
    starts = _starts(n, datetime(2026, 5, 18, 1, 0, tzinfo=UTC))
    pv_kwh = ([0.0] * 8 + [0.2, 0.4, 0.7, 1.0, 1.2, 1.3, 1.2, 1.0,
                            0.8, 0.5, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0])
    weather = WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[7.0] * n,
        shortwave_radiation_wm2=[0.0] * 8 + [50, 200, 400, 600, 700, 750, 700, 600,
                                              400, 200, 50, 0, 0, 0, 0, 0],
        cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=pv_kwh,
        cop_space=[2.5] * n,
        cop_dhw=[2.2] * n,
    )
    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=[18.0] * 6 + [14.0] * 4 + [20.0] * 4 + [30.0] * 4 + [15.0] * 6,
        base_load_kwh=[0.4] * n,
        weather=weather,
        initial=LpInitialState(soc_kwh=1.24, tank_temp_c=57.0),
        tz=starts[0].tzinfo,
    )
    assert plan.ok, (
        f"LP must remain feasible at the prod state that triggered four "
        f"infeasibles on 2026-05-18, got status={plan.status!r}"
    )


def test_lp_normal_state_still_optimal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: the relaxation doesn't degrade behavior in the normal case
    (SoC well above reserve). LP still finds Optimal and SoC never dips
    below reserve when no forcing condition requires it.
    """
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.36)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 15.0)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive")

    n = 12
    starts = _starts(n, datetime(2026, 5, 18, 12, 0, tzinfo=UTC))
    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=[15.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_weather(starts),
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
        tz=starts[0].tzinfo,
    )
    assert plan.ok
    soc_min = 10.36 * 0.15
    # All forward soc[i] (i>=1) must respect reserve — no slack needed
    for i, s in enumerate(plan.soc_kwh[1:], start=1):
        assert s >= soc_min - 1e-4, (
            f"Forward soc[{i}]={s:.3f} dipped below reserve {soc_min:.3f} "
            f"with no forcing condition — soft reserve must still bind in normal state"
        )
