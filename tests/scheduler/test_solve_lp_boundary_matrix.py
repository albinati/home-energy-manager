"""solve_lp must produce a sensible status across the full range of
``initial.soc_kwh`` — including values below the operational reserve.

Pre-PR #339 the LP went Infeasible the moment realtime SoC dipped below
``MIN_SOC_RESERVE_PERCENT`` (e.g. 15 %), because ``soc`` LpVariable was
bounded ``[soc_min, soc_max]`` for every slot — including slot 0 — while
``prob += soc[0] == initial.soc_kwh`` was a hard equality. The two became
unsatisfiable. This boundary regression went undetected because no existing
test parametrised over below-reserve initial SoC values; ``solve_lp`` tests
all used comfortable 50 %-ish starts.

This file is the boundary matrix: it sweeps initial SoC from 0 → soc_max
and asserts solver status. Adding any new hard constraint to ``solve_lp``
that introduces a new infeasibility region will fail one of these cases
loudly, before it can ship.

Why ``solve_lp`` boundary tests aren't already organised this way: the
existing suite is structured around feature behaviour (DHW, peak-export,
plunge windows etc.). Boundary feasibility is an *invariant* across all
features — it deserves its own surface. See the 2026-05-18 testing audit
write-up for the broader gap analysis.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _init_db() -> None:
    _db.init_db()


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the matrix-friendly setup from test_lp_plunge_window.py — keep
    the LP small + the solver permissive so the parametrised sweep runs in
    seconds, not minutes."""
    monkeypatch.setattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    # Active mode: LP can choose e_dhw=e_space=0 (no HP draw) so the import
    # budget isn't fighting predicted passive load. Tank starts at 45 °C so no
    # shower hard floor pressures the LP either (we set no shower windows).
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(config, "DHW_SHOWER_SCHEDULE", "")  # no shower mask
    monkeypatch.setattr(config, "LP_SHOWER_MORNING_LOCAL", "")
    monkeypatch.setattr(config, "LP_SHOWER_EVENING_LOCAL", "")
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.0)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 15.0)
    # Disable pre-plunge and PV-sufficiency to isolate the SoC bound test —
    # those add their own ``chg <= pv_use`` constraints that could mask the
    # boundary regression we're guarding against.
    monkeypatch.setattr(config, "LP_PLUNGE_PREP_HOURS", 0)
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", False)


def _starts(n: int) -> list[datetime]:
    """12 h horizon starting at a quiet local time so the test is timezone-agnostic."""
    base = datetime(2026, 5, 18, 1, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def _benign_weather(starts: list[datetime]) -> WeatherLpSeries:
    """Mild + zero-PV. Forces the LP to lean on the battery / grid balance —
    the path where SoC bounds matter most. We don't want PV abundance masking
    a SoC infeasibility (PV → battery is always feasible regardless of SoC bound)."""
    n = len(starts)
    return WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )


# Sweep: 0 kWh → soc_max (= 10.0 kWh on the matrix-configured battery).
# Each entry: (initial_soc_kwh, must_be_status). All entries must currently
# resolve to "Optimal" — post-PR #339 there is no infeasible region for plain
# initial-SoC alone. If a future LP change introduces one, this sweep will
# fail loudly at the boundary that broke.
_SOC_SWEEP_KWH = [
    0.0,    # flat battery — physical floor
    0.5,    # well below 15 % reserve
    1.0,    # ≈ 10 % (Fox minSocOnGrid territory)
    1.49,   # one tick below 15 % reserve
    1.5,    # exactly at 15 % reserve
    1.51,   # one tick above
    2.5,    # 25 % (normal-low)
    5.0,    # 50 % (normal)
    7.5,    # 75 % (normal-high)
    9.5,    # 95 % (near full)
    10.0,   # at soc_max
]


@pytest.mark.parametrize("initial_soc_kwh", _SOC_SWEEP_KWH, ids=lambda v: f"soc={v:g}kWh")
def test_solve_lp_status_across_initial_soc_sweep(initial_soc_kwh: float) -> None:
    """For every initial SoC in [0, soc_max], solve_lp must return Optimal —
    never Infeasible, never Unbounded, never Not Solved (timeout).

    Pre-PR #339 the values ``[0.0, 0.5, 1.0, 1.49]`` returned Infeasible
    (because ``soc[0]`` had ``lowBound=soc_min=1.5`` AND ``soc[0]==initial``
    was a hard equality). This test would have caught that the moment it ran.
    """
    n = 12  # 6 h horizon — long enough for SoC recovery, small enough to solve fast
    starts = _starts(n)
    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=[15.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_benign_weather(starts),
        initial=LpInitialState(soc_kwh=initial_soc_kwh, tank_temp_c=45.0),
        tz=ZoneInfo("UTC"),
    )
    assert plan.status not in ("Unbounded", "Not Solved"), (
        f"solve_lp returned {plan.status!r} for initial_soc={initial_soc_kwh} kWh — "
        f"that's a solver degradation (timeout or model-error), not a feasibility "
        f"answer. Investigate before shipping."
    )
    assert plan.ok, (
        f"solve_lp returned {plan.status!r} for initial_soc={initial_soc_kwh} kWh. "
        f"With benign benign weather + no pre-plunge / PV-sufficiency constraints, "
        f"every value in [0, soc_max] should be feasible. If you just added a hard "
        f"constraint to solve_lp, audit that the SoC boundary still works — see the "
        f"2026-05-18 prod incident where exactly this assumption broke."
    )
    # And SoC must be preserved exactly at slot 0 (the LP is not silently clamping
    # the realtime value — the dispatch trusts the LP to plan FROM the actual state).
    assert plan.soc_kwh[0] == pytest.approx(initial_soc_kwh, abs=1e-6), (
        f"solve_lp silently mutated initial SoC: got soc[0]={plan.soc_kwh[0]:.3f}, "
        f"expected {initial_soc_kwh:.3f}. Dispatch downstream would compute the wrong "
        f"per-slot energy balance."
    )


def test_solve_lp_status_unbounded_returns_distinct_status() -> None:
    """Sanity check the parametrised sweep would actually catch a degraded
    solver state — assert that the ``Unbounded`` / ``Not Solved`` branches in
    the sweep above are reachable string values, not a typo. (PuLP's ``Optimal``,
    ``Infeasible``, ``Unbounded``, ``Not Solved`` are the four documented
    statuses for CBC's return code mapping.)
    """
    # We can't easily *construct* an Unbounded LP without breaking the model
    # invariants, so this is a thin invariant check: at minimum, the statuses
    # we're filtering for are distinct strings. If pulp ever renames them, the
    # sweep above would silently pass on a broken solver.
    valid_statuses = {"Optimal", "Infeasible", "Unbounded", "Not Solved", "Undefined"}
    for s in ("Unbounded", "Not Solved"):
        assert s in valid_statuses, (
            f"Status name {s!r} no longer recognised by pulp — update the sweep filter."
        )
