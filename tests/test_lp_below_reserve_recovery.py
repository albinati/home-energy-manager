"""#789 — a solve that STARTS below the SoC reserve must still return a plan.

Prod, 2026-08-22 20:22-23:05 UTC: 9 consecutive ``Infeasible`` solves at 8-9 %
SoC, each falling back to the held schedule for ~3 h. Measured over the
preceding 60 days: 0 Infeasible in 1732 solves starting at/above the reserve,
10 in 26 starting below it.

Mechanism: ``soc[0]`` was relaxed for the hard ``soc[0] == measured`` equality
(#338/#339) but ``soc[1..n]`` kept the reserve as a HARD lower bound — so the
LP had to lift the battery back over the reserve inside the first 30-min slot.
Any constraint imposing ``chg[i] <= pv_use[i]`` on slot 0 (the plunge-prep rule
and the PV-sufficiency guard both do) makes that impossible at night, and the
whole solve dies.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

CAPACITY_KWH = 10.36
RESERVE_PCT = 10.0
RESERVE_KWH = CAPACITY_KWH * RESERVE_PCT / 100.0  # 1.036


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 20)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(app_config, "BATTERY_CAPACITY_KWH", CAPACITY_KWH, raising=False)
    monkeypatch.setattr(app_config, "MIN_SOC_RESERVE_PERCENT", RESERVE_PCT, raising=False)
    monkeypatch.setattr(app_config, "LP_PLUNGE_PREP_HOURS", 12)


def _starts(n: int) -> list[datetime]:
    # 20:00 UTC — the slot the prod incident started on. Night: no PV anywhere
    # in the near horizon, so ``chg <= pv_use`` is a hard zero.
    base = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def _solve(prices: list[float], soc: float, *, pv: float = 0.0):
    starts = _starts(len(prices))
    n = len(prices)
    weather = WeatherLpSeries(
        slot_starts_utc=starts,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[pv] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    return solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=[0.3] * n,
        weather=weather,
        initial=LpInitialState(
            soc_kwh=soc, tank_temp_c=46.0, soc_source="test", tank_source="test",
        ),
        tz=ZoneInfo("UTC"),
        export_price_pence=[10.0] * n,
    )


# The plunge-prep rule pins ``chg[i] <= pv_use[i]`` on every positive-priced
# slot within 12 h of a negative one — including slot 0, at night.
PLUNGE_PRICES = [25.0] * 10 + [-5.0] * 2 + [25.0] * 12


def test_below_reserve_night_start_still_solves() -> None:
    """The exact prod shape: 9 % SoC, dark, grid→battery blocked on slot 0."""
    plan = _solve(PLUNGE_PRICES, soc=0.9324)
    assert plan.ok, plan.status
    assert plan.soc_reserve_recovery_applied is True


def test_at_reserve_is_unaffected() -> None:
    """Control: starting exactly AT the reserve solved fine before this PR and
    must not engage the relaxation."""
    plan = _solve(PLUNGE_PRICES, soc=RESERVE_KWH)
    assert plan.ok, plan.status
    assert plan.soc_reserve_recovery_applied is False
    assert plan.soc_reserve_recovery_slack_kwh == 0.0


def test_deeply_below_reserve_still_solves() -> None:
    """Near-empty battery — the worst case the fallback used to hit hardest."""
    plan = _solve(PLUNGE_PRICES, soc=0.2)
    assert plan.ok, plan.status
    assert plan.soc_reserve_recovery_applied is True


def test_recovery_slack_sizes_the_shortfall() -> None:
    """The slack is the diagnostic: with charging blocked at night the plan
    genuinely sits under the reserve for a while, and that shows up as
    non-zero slack over a non-zero number of slots — not as a silent pass."""
    plan = _solve(PLUNGE_PRICES, soc=0.2)
    assert plan.ok, plan.status
    assert plan.soc_reserve_recovery_slack_kwh > 0.0
    assert plan.soc_reserve_recovery_slots > 0


def test_reserve_still_behaves_hard_when_reachable() -> None:
    """The relaxation must not become a licence to run under the reserve.
    With grid charging available (no negative window → no plunge-prep block),
    the steep penalty drives the LP back over the floor immediately and it
    stays there for the whole horizon."""
    prices = [25.0] * 24
    plan = _solve(prices, soc=0.9324)
    assert plan.ok, plan.status
    assert plan.soc_reserve_recovery_applied is True
    # Recovered inside slot 0 → every PLANNED SoC is at or above the reserve.
    # soc_kwh[0] is the measurement, not a decision, so it stays at 0.9324.
    assert plan.soc_reserve_recovery_slack_kwh == pytest.approx(0.0, abs=1e-6)
    assert plan.soc_kwh[0] == pytest.approx(0.9324, abs=1e-9)
    assert min(plan.soc_kwh[1:]) >= RESERVE_KWH - 1e-6


def test_penalty_zero_disables_the_cost_but_not_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill switch: penalty 0 keeps the solve feasible (never Infeasible) —
    the relaxation itself is unconditional by design."""
    monkeypatch.setattr(
        app_config, "LP_SOC_RESERVE_RECOVERY_SLACK_PENALTY_PENCE", 0.0, raising=False,
    )
    plan = _solve(PLUNGE_PRICES, soc=0.9324)
    assert plan.ok, plan.status


# ── the wire: the audit block that reaches lp_inputs_snapshot ────────────────

def test_snapshot_audit_absent_when_above_reserve() -> None:
    """No key at all on a healthy solve — the key's PRESENCE is the query."""
    from src.scheduler.optimizer import _soc_reserve_recovery_snapshot

    plan = _solve(PLUNGE_PRICES, soc=RESERVE_KWH)
    initial = LpInitialState(soc_kwh=RESERVE_KWH, tank_temp_c=46.0)
    assert _soc_reserve_recovery_snapshot(plan, initial) is None


def test_snapshot_audit_records_the_below_reserve_start() -> None:
    """A below-reserve solve now SUCCEEDS, so the operational fact has to be
    recorded somewhere queryable — it used to live in lp_failure_log."""
    from src.scheduler.optimizer import _soc_reserve_recovery_snapshot

    plan = _solve(PLUNGE_PRICES, soc=0.2)
    initial = LpInitialState(soc_kwh=0.2, tank_temp_c=46.0)
    audit = _soc_reserve_recovery_snapshot(plan, initial)
    assert audit is not None
    assert audit["initial_soc_kwh"] == pytest.approx(0.2)
    assert audit["reserve_kwh"] == pytest.approx(RESERVE_KWH)
    assert audit["min_soc_reserve_percent"] == RESERVE_PCT
    assert audit["slack_kwh"] > 0.0
    assert audit["slack_slots"] > 0
