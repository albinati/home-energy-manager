"""The DHW block, proven in a standalone mini-LP.

The point of a separate block is that the behaviour the owner asked for can be shown in
isolation, without the hundred other constraints of the full solver. Each test here is
a tiny complete LP: a tank, a price curve, a comfort floor, and nothing else. If the
block does the right thing here, it does the right thing in the solver — and when it
does the wrong thing, the failure is legible.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pulp
import pytest

from src.dhw.lp import DhwLpConfig, build_dhw_block
from src.dhw.model import TankParams

P = TankParams()


def _solve(
    *,
    prices,
    floors,
    tank0=45.0,
    draw=None,
    t_out=7.0,
    ambient=22.0,
    cfg=None,
    import_price_weight=True,
):
    """Build a self-contained LP: buy grid electricity to run the DHW block, minimise
    cost + comfort penalty. Returns the solved block and its variables' values."""
    n = len(prices)
    cfg = cfg or DhwLpConfig()
    starts = [datetime(2026, 7, 8, 0, 0, tzinfo=UTC) + i * timedelta(minutes=30)
              for i in range(n)]
    prob = pulp.LpProblem("dhw_test", pulp.LpMinimize)

    block = build_dhw_block(
        prob,
        slot_starts_utc=starts,
        tank0_c=tank0,
        t_out_by_slot=[t_out] * n,
        ambient_by_slot=[ambient] * n,
        draw_kwh_by_slot=draw or [0.0] * n,
        comfort_floor_by_slot=floors,
        price_by_slot=prices,
        p=P,
        cfg=cfg,
        day_index_by_slot=[0] * n,
    )
    # Import to cover the DHW electricity, priced at the tariff.
    imp = pulp.LpVariable.dicts("imp", range(n), lowBound=0)
    for i in range(n):
        prob += imp[i] == block.e_total[i]
    energy_cost = pulp.lpSum(imp[i] * prices[i] for i in range(n))
    prob += energy_cost + block.comfort_penalty

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    return {
        "status": pulp.LpStatus[prob.status],
        "tank": [pulp.value(t) for t in block.tank],
        "e_total": [pulp.value(e) for e in block.e_total],
        "on": [pulp.value(o) for o in block.dhw_on],
        "comfort_penalty": pulp.value(block.comfort_penalty),
    }


# ---------------------------------------------------------------------------
# The behaviour the owner asked for
# ---------------------------------------------------------------------------


def test_it_does_not_heat_through_the_expensive_window():
    """The owner's own hypothesis: let the tank cool while the tariff is high, and buy
    the heat when it is cheap. A cliff in the price, a comfort floor at the end — the
    heating must land in the cheap slots, not the dear ones."""
    # 10 slots. Slots 3-5 are brutally expensive; comfort needed at slot 9.
    prices = [8.0, 8.0, 8.0, 90.0, 90.0, 90.0, 8.0, 8.0, 8.0, 8.0]
    floors = [None] * 9 + [45.0]
    r = _solve(prices=prices, floors=floors, tank0=45.0)
    assert r["status"] == "Optimal"

    peak_kwh = sum(r["e_total"][3:6])
    cheap_kwh = sum(r["e_total"][i] for i in range(10) if i not in (3, 4, 5))
    assert peak_kwh < 0.01, f"heated at 90p/kWh: {peak_kwh:.3f}"
    assert cheap_kwh > 0
    # Comfort met without buying it with slack.
    assert r["comfort_penalty"] == pytest.approx(0.0, abs=1e-6)
    assert r["tank"][9] >= 45.0 - 1e-3


def test_it_lets_the_tank_coast_instead_of_holding_it_hot():
    """Because the tank is a thermos, holding it at 45 all day wastes standing-loss
    energy the LP is charged for. With comfort needed only in the evening, the LP
    should let it drift down and heat once, late — not maintain it."""
    n = 20
    prices = [10.0] * n
    floors = [None] * n
    floors[18] = 45.0
    r = _solve(prices=prices, floors=floors, tank0=45.0)
    assert r["status"] == "Optimal"
    # The tank is allowed to fall below 45 during the day...
    assert min(r["tank"][1:17]) < 45.0
    # ...and total heating is modest — roughly one recovery plus the day's losses,
    # not a full day of holding.
    assert sum(r["e_total"]) < 1.5


def test_it_slices_the_heat_when_one_slot_cannot_cover_the_load():
    """Slicing is free — it falls out of the LP the moment a single slot cannot do the
    job. A draw larger than one slot of heat-pump capacity forces the heat across
    several slots; the LP takes the cheapest ones.

    (The complementary, non-obvious fact this exposes: with only ~5 °C of headroom
    below the resistance cliff, the tank banks barely one shower's worth ahead — so
    "heat it all on cheap afternoon PV and coast to the evening" is capped by the tank,
    not the tariff. Most of the heat has to land near the draw. That is physics worth
    knowing before promising the moon on slicing.)"""
    # A sustained draw with the tank held at comfort. The tank banks only ~5 °C of
    # headroom below the cliff, so a prolonged draw cannot be pre-stored in one go —
    # the LP has to top it up across several slots. It takes the cheap ones.
    n = 8
    prices = [5.0, 50.0, 5.0, 50.0, 5.0, 50.0, 5.0, 50.0]
    draw = [0.0, 0.0] + [0.5] * 6      # continuous draw from slot 2 on
    floors = [None, None] + [45.0] * 6  # comfort held throughout the draw
    cfg = DhwLpConfig(min_run_kwh=0.1, min_dwell_slots=1)
    r = _solve(prices=prices, floors=floors, tank0=45.0, draw=draw, cfg=cfg)
    assert r["status"] == "Optimal"
    assert r["comfort_penalty"] == pytest.approx(0.0, abs=1e-6)

    on_slots = [i for i, o in enumerate(r["on"]) if o and o > 0.5]
    assert len(on_slots) >= 2, "a sustained draw must be topped up across slots"
    # And the tops-up land in cheap slots, never the 50p ones.
    assert all(prices[i] < 40.0 for i in on_slots)


# ---------------------------------------------------------------------------
# The resistance cliff
# ---------------------------------------------------------------------------


def test_the_tank_cannot_be_pushed_past_the_cliff_on_a_normal_slot():
    """No amount of cheap electricity may command the tank above 50 °C when the price
    is positive — above the cliff is the immersion heater at COP 1, and that is never
    the best home for a positive-priced kWh (the battery beats it)."""
    prices = [1.0] * 8  # absurdly cheap, but positive
    floors = [None] * 8
    r = _solve(prices=prices, floors=floors, tank0=45.0)
    assert r["status"] == "Optimal"
    assert max(r["tank"]) <= 50.0 + 1e-3


def test_a_negative_price_unlocks_the_resistance():
    """When the grid PAYS us to import, the immersion heater earns its keep: filling
    the tank to 60 °C soaks up paid energy the battery may not have room for. Only
    then is the cliff allowed to be crossed."""
    prices = [-15.0] * 8  # paid to import
    floors = [None] * 8
    r = _solve(prices=prices, floors=floors, tank0=45.0)
    assert r["status"] == "Optimal"
    assert max(r["tank"]) > 50.0  # it went past the cliff, because it paid to


# ---------------------------------------------------------------------------
# Never infeasible; comfort as slack
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tank0", [20.0, 30.0, 37.0, 45.0, 50.0])
def test_never_infeasible_even_with_a_cold_tank_at_the_shower(tank0):
    """A comfort floor the tank cannot possibly reach in time must surface as slack,
    never as a dead solver (#344/#422). Floor at slot 1 with a cold start is the
    unreachable case."""
    prices = [10.0] * 6
    floors = [None, 45.0, 45.0, None, None, None]
    r = _solve(prices=prices, floors=floors, tank0=tank0)
    assert r["status"] == "Optimal"


def test_an_impossible_floor_shows_up_as_a_penalty_not_a_crash():
    prices = [10.0] * 4
    floors = [None, 60.0, None, None]  # 60 at slot 1 from a 30 °C start: impossible
    r = _solve(prices=prices, floors=floors, tank0=30.0)
    assert r["status"] == "Optimal"
    assert r["comfort_penalty"] > 0.0  # the deficit is priced, and visible


# ---------------------------------------------------------------------------
# Quota: few runs
# ---------------------------------------------------------------------------


def test_the_slice_cap_bounds_the_number_of_runs():
    """Runs become Daikin rows, and the quota is 200/day. A tight cap must hold even
    when heating every slot would otherwise be free-ish."""
    n = 24
    prices = [5.0] * n
    draw = [0.15] * n  # constant small draw tempts the LP to nibble every slot
    floors = [None] * n
    floors[-1] = 44.0
    cfg = DhwLpConfig(max_slices_per_day=6, min_run_kwh=0.1, min_dwell_slots=1)
    r = _solve(prices=prices, floors=floors, draw=draw, cfg=cfg)
    assert r["status"] == "Optimal"
    assert sum(1 for o in r["on"] if o and o > 0.5) <= 6
