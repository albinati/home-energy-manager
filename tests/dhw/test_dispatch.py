"""The LP tank trajectory → a few Daikin rows, proven purely.

The compression has to satisfy the quota (few rows) and comfort (the backstop) at the
same time. Each test builds a small plan and checks the rows that come out.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.dhw.dispatch import (
    DEFAULT_RUNGS,
    apply_comfort_backstop,
    tank_rows_from_plan,
)

LONDON = ZoneInfo("Europe/London")


def _starts(n, first_hour=0):
    base = datetime(2026, 7, 8, first_hour, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


# ---------------------------------------------------------------------------
# Few rows
# ---------------------------------------------------------------------------


def test_a_flat_trajectory_is_one_row():
    """A tank held at one temperature all horizon must not become one row per slot."""
    n = 12
    starts = _starts(n)
    tank = [45.0] * (n + 1)
    rows = tank_rows_from_plan(starts, tank, [0.0] * n, [10.0] * n)
    assert len(rows) == 1
    assert rows[0].tank_temp_c == 45
    assert rows[0].start_utc == starts[0]
    assert rows[0].end_utc == starts[-1] + timedelta(minutes=30)


def test_a_warmup_then_setback_is_two_rows():
    """The daily shape: heat to comfort, then let it fall back. Two rows, and the
    action types reflect the direction."""
    n = 12
    starts = _starts(n)
    # Coasting at 37 for 6 slots, then warmed to 45 for 6.
    tank = [37.0] * 6 + [45.0] * 7
    ekwh = [0.0] * 6 + [1.0] + [0.0] * 5  # heat added at the transition
    rows = tank_rows_from_plan(starts, tank, ekwh, [10.0] * n)
    assert len(rows) == 2
    assert [r.tank_temp_c for r in rows] == [37, 45]
    assert rows[0].action_type == "tank_setback"
    assert rows[1].action_type == "tank_warmup"


def test_a_one_slot_blip_is_absorbed_not_given_its_own_row():
    """A single-slot excursion below the min dwell must not earn a Daikin write — it
    merges into the cooler neighbour (fail-cheap)."""
    n = 10
    starts = _starts(n)
    tank = [45.0] * 4 + [48.0] + [45.0] * 6  # one slot pops to 48
    ekwh = [0.0] * 10
    rows = tank_rows_from_plan(starts, tank, ekwh, [10.0] * n, min_dwell_slots=2)
    assert len(rows) == 1
    assert rows[0].tank_temp_c == 45


def test_coasting_rounds_down_and_heating_rounds_up():
    """Direction of rounding is load-bearing. A coasting tank must be told a setpoint
    at or below where it is (so the Daikin sits idle and it drifts); a heating tank
    must be told one at or above the plan (so the shower is never cooler than promised)."""
    n = 4
    starts = _starts(n)
    # Coasting through 46.4 → rounds DOWN to 45.
    coast = tank_rows_from_plan(starts, [46.4] * (n + 1), [0.0] * n, [10.0] * n)
    assert coast[0].tank_temp_c == 45
    # Heating towards 45.6 → rounds UP to 48.
    heat = tank_rows_from_plan(starts, [45.6] * (n + 1), [1.0] * n, [10.0] * n)
    assert heat[0].tank_temp_c == 48


def test_a_negative_price_slot_is_a_boost_row():
    """Paid to import → a boost row, powerful, and never absorbed."""
    n = 6
    starts = _starts(n)
    tank = [45.0, 45.0, 60.0, 60.0, 45.0, 45.0, 45.0]
    ekwh = [0.0, 0.0, 3.0, 0.0, 0.0, 0.0]
    prices = [10.0, 10.0, -15.0, -15.0, 10.0, 10.0]
    rows = tank_rows_from_plan(starts, tank, ekwh, prices)
    boost = [r for r in rows if r.action_type == "tank_negative_boost"]
    assert len(boost) == 1
    assert boost[0].tank_powerful is True
    assert boost[0].tank_temp_c == 60


def test_rows_carry_the_lp_owned_marker():
    rows = tank_rows_from_plan(_starts(4), [45.0] * 5, [0.0] * 4, [10.0] * 4)
    assert rows[0].to_params()["lp_owned"] is True
    assert rows[0].to_params()["tank_power"] is True


# ---------------------------------------------------------------------------
# The comfort backstop
# ---------------------------------------------------------------------------


def test_backstop_is_a_noop_when_the_plan_already_delivers_comfort():
    """If the LP already planned a hot tank over the window, the backstop must not add
    a redundant row — the firmware would just see a target it already meets."""
    starts = _starts(8, first_hour=18)  # 18:00-22:00 BST covers the 20:00 window
    # A single 45 °C row spanning the whole horizon.
    tank = [45.0] * 9
    rows = tank_rows_from_plan(starts, tank, [0.0] * 8, [10.0] * 8)
    out = apply_comfort_backstop(
        rows, starts, LONDON, backstop_c=45.0,
        window_start_hour=20.0, window_end_hour=21.0,
    )
    assert out == rows  # unchanged


def test_backstop_repairs_an_optimistic_plan_that_left_the_tank_cold():
    """The failure the backstop exists for: an optimistic calibration bug lets the LP
    believe the tank is still hot at 20:00 when it isn't. The soft floor in the solver
    cannot catch that (the solve looks perfect). The backstop lays a 45 °C target over
    the window from a CONSTANT — nothing learned — so the firmware repairs it."""
    starts = _starts(8, first_hour=18)
    # The LP let the tank coast down to 38 across the evening — no row hits 45.
    tank = [42.0 - i for i in range(9)]
    rows = tank_rows_from_plan(starts, tank, [0.0] * 8, [10.0] * 8)
    assert max(r.tank_temp_c for r in rows) < 45

    out = apply_comfort_backstop(
        rows, starts, LONDON, backstop_c=45.0,
        window_start_hour=20.0, window_end_hour=21.0,
    )
    # A 45 °C row now covers the 20:00-21:00 window.
    window_rows = [
        r for r in out
        if r.tank_temp_c >= 45 and r.start_utc.astimezone(LONDON).hour == 20
    ]
    assert window_rows, "the backstop must lay a comfort row over the window"


def test_backstop_reads_only_the_constant_it_is_given():
    """Belt-and-braces on the promise: the backstop temperature is exactly the
    argument, never derived from the plan or a fit."""
    starts = _starts(4, first_hour=19)  # 20:00 BST inside
    rows = tank_rows_from_plan(starts, [30.0] * 5, [0.0] * 4, [10.0] * 4)
    out = apply_comfort_backstop(
        rows, starts, LONDON, backstop_c=43.0,
        window_start_hour=20.0, window_end_hour=21.0,
    )
    assert any(r.tank_temp_c == 43 for r in out)
