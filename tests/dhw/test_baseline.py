"""The fixed-schedule simulator — the honest baseline of the economic gate.

Its job is to reproduce what the incumbent actually does, INCLUDING its two known
flaws (the 13:00 warmup at whatever price 13:00 happens to be, and the evening reheat
during the showers). A baseline that hides the incumbent's flaws would understate the
LP's value exactly where the value lives; a baseline built from the dhw_policy
forecast overstates it (~2.4× the real energy). The simulation is the middle path:
same physics, same draw, same legionella budget as the LP arm.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.dhw.baseline import legionella_budget_by_slot, simulate_fixed_schedule
from src.dhw.model import TankParams

P = TankParams()
TZ = ZoneInfo("UTC")


def _starts(n, first_hour=0, day=8):
    base = datetime(2026, 7, day, first_hour, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def test_the_thermostat_reheats_at_the_warmup_boundary():
    """Setback overnight, warmup at 13:00: the simulator must fire the reheat right
    at the boundary — the incumbent's fixed clock, reproduced."""
    starts = _starts(48)  # full day from midnight
    e, tank = simulate_fixed_schedule(
        starts, TZ, tank0_c=37.0, p=P,
        t_out_by_slot=[15.0] * 48, draw_kwh_by_slot=[0.0] * 48,
    )
    by_hour = {starts[i].hour + starts[i].minute / 60: e[i] for i in range(48)}
    assert by_hour[13.0] > 0.3          # the warmup fires at 13:00
    assert sum(e[:26]) < 0.15           # and not before (setback holds)
    # After the warmup the tank sits at target.
    assert tank[28] == pytest.approx(45.0, abs=1.0)


def test_the_evening_draw_forces_a_reheat_during_the_showers():
    """The incumbent's flaw the owner named: the tank is HELD at 45 until 22:00, so
    the 20:00 draw pulls it below target and the firmware reheats DURING the showers
    — at whatever the evening price is. The simulator must reproduce this, because it
    is precisely the cost the LP-owned regime exists to avoid."""
    starts = _starts(28, first_hour=10)  # 10:00 → 24:00
    draw = [0.0] * 28
    for i, st in enumerate(starts):
        if st.hour == 20:
            draw[i] = 1.7  # 3.4 kWh across the two shower slots
    e, tank = simulate_fixed_schedule(
        starts, TZ, tank0_c=44.0, p=P,
        t_out_by_slot=[15.0] * 28, draw_kwh_by_slot=draw,
    )
    shower_and_after = [e[i] for i, st in enumerate(starts) if 20 <= st.hour < 22]
    assert sum(shower_and_after) > 0.5, "the firmware must reheat during/after the draw"


def test_energy_is_conserved():
    """Over a long horizon at steady state, electricity in ≈ (draw + standing loss)
    ÷ COP. The simulator must not create or destroy heat."""
    n = 96  # two days
    starts = _starts(n)
    draw = [0.0] * n
    for i, st in enumerate(starts):
        if st.hour == 20:
            draw[i] = 1.7
    e, tank = simulate_fixed_schedule(
        starts, TZ, tank0_c=45.0, p=P,
        t_out_by_slot=[15.0] * n, draw_kwh_by_slot=draw,
    )
    from src.dhw.model import cop_dhw

    total_draw = sum(draw)
    # Steady state: tank ends near where it started, so electric×COP ≈ draw + losses.
    thermal_in = sum(e) * cop_dhw(15.0, 45.0, P)
    losses_approx = 1.3 * 2  # ~1.3 kWh/day standing
    assert thermal_in == pytest.approx(
        total_draw + losses_approx + (tank[-1] - tank[0]) * P.kwh_per_degc, abs=1.2
    )


def test_legionella_budget_lands_on_sunday_and_in_the_simulation():
    # 2026-07-12 is a Sunday.
    starts = _starts(48, day=12)
    leg = legionella_budget_by_slot(starts, budget_kwh=3.5)
    assert sum(leg) == pytest.approx(3.5)
    hit = [starts[i] for i in range(48) if leg[i] > 0]
    assert hit and all(st.weekday() == 6 and st.hour in (11, 12) for st in hit)

    e, tank = simulate_fixed_schedule(
        starts, TZ, tank0_c=45.0, p=P,
        t_out_by_slot=[15.0] * 48, draw_kwh_by_slot=[0.0] * 48,
        legionella_kwh_by_slot=leg,
    )
    # The cycle's electricity is carried, and the tank rises toward 60 but never past.
    assert sum(e) >= 3.4
    assert max(tank) > 50.0
    assert max(tank) <= 60.0 + 1e-6

    # A Tuesday gets none.
    tue = _starts(48, day=14)
    assert sum(legionella_budget_by_slot(tue, budget_kwh=3.5)) == 0.0
