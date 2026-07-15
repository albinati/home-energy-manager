"""The tank's physics, checked against numbers we can state in advance.

Two independent yardsticks, and the model has to satisfy both:

* **What the tank actually did** — 21 days of prod telemetry, fitted with nothing
  but the thermometer (UA and the cupboard's effective ambient, jointly): the tank
  coasts at 0.24 °C/h at 45 °C and has a time constant near 95 h.
* **What Daikin certified** — EN 16147 COP anchors from the databook, and the
  installer guide's resistance cliff.

If a future change breaks one of these, it has broken the model, not the test.
"""
from __future__ import annotations

import pytest

from src.dhw.model import (
    effective_cop,
    TankParams,
    coast_rate_c_per_h,
    coast_to,
    cop_dhw,
    electric_kwh_to_raise,
    heats_with_resistance,
    hours_to_coast_from,
    max_heat_c_per_slot,
    standing_loss_w,
    thermal_kwh_to_raise,
)

P = TankParams()


# ---------------------------------------------------------------------------
# The tank is a thermos — the fact the whole design rests on
# ---------------------------------------------------------------------------


def test_coast_matches_the_measured_tank():
    """Measured over 19 clean overnight episodes: 0.24 °C/h at 45 °C, τ ≈ 95 h.
    This single test validates UA, C and the ambient together — get any of the
    three wrong and it fails."""
    assert coast_rate_c_per_h(45.0, P) == pytest.approx(0.24, abs=0.02)
    assert P.tau_hours == pytest.approx(95.0, rel=0.06)


def test_standing_loss_matches_the_measured_daily_figure():
    """~1.3 kWh thermal a day at 45 °C — and the databook's own cylinder figure
    (55 W at ΔT 45 K) is the same order, which is the cross-check that the
    measured UA is a real tank and not a fitting artefact."""
    daily_kwh = standing_loss_w(45.0, P) * 24.0 / 1000.0
    assert daily_kwh == pytest.approx(1.3, abs=0.15)


def test_heating_early_and_coasting_to_the_shower_is_nearly_free():
    """The economic case, stated as physics. Heat at 03:00, shower at 20:00: 17
    hours of coasting costs about 4 °C. That is what buys the LP the freedom to
    put the energy in whenever it is cheapest — and it is why 'heat it just before
    the shower' is the wrong instinct for this tank."""
    landed = coast_to(50.0, hours=17.0, p=P)
    assert landed == pytest.approx(45.9, abs=0.7)
    assert landed >= 45.0  # still comfortable for the 20:00 showers


def test_hours_of_headroom_before_the_tank_falls_below_comfort():
    """The question the LP asks when deciding to let the tank cool."""
    hours = hours_to_coast_from(50.0, 45.0, P)
    assert hours == pytest.approx(19.6, rel=0.1)
    # Below ambient it never gets there — say so instead of returning nonsense.
    assert hours_to_coast_from(45.0, 20.0, P) is None


def test_a_colder_cupboard_costs_more_to_hold():
    """Winter does not change the insulation, it changes the gap. Every kWh counts
    in winter precisely because this number grows."""
    summer = coast_rate_c_per_h(45.0, P)
    winter = coast_rate_c_per_h(45.0, P, ambient_c=12.0)
    assert winter > summer * 1.4
    assert winter == pytest.approx(0.35, abs=0.03)


# ---------------------------------------------------------------------------
# COP — certified anchors, and the cliff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("t_out", "expected"), [(2.0, 2.17), (7.0, 2.51), (14.0, 2.76)])
def test_cop_hits_the_certified_anchors_exactly(t_out, expected):
    """EN 16147, databook EEDEN20 p.26, tested at ϑwh = 52.5 °C. Interpolating
    PIECEWISE through these points (rather than least-squares-fitting a line to
    them) is what makes them exact — and the cold anchor is the one that decides
    winter, so it is the one a fitted line must not be allowed to miss."""
    assert cop_dhw(t_out, 52.5, P) == pytest.approx(expected, abs=0.01)


def test_the_lp_used_to_believe_tank_heat_was_half_price():
    """The headline error this rewrite exists to fix. The old LP read the DHW COP
    off the SPACE-heating curve (LWT 35 °C) and landed near 4.70. At the
    household's real operating point the tank returns ~2.7."""
    real = cop_dhw(15.0, 45.0, P)
    assert 2.5 < real < 3.1
    old_lp_belief = 4.70
    assert old_lp_belief / real > 1.5


def test_a_lower_target_is_cheaper_per_degree():
    """45 °C is enough for this household — and it is also more efficient than the
    52.5 °C the certification tests at. Both facts point the same way."""
    assert cop_dhw(7.0, 45.0, P) > cop_dhw(7.0, 52.5, P)
    assert cop_dhw(7.0, 45.0, P) == pytest.approx(2.74, abs=0.03)


def test_above_the_cliff_it_is_not_a_heat_pump_any_more():
    """Above T_HP_MAX the backup/booster resistance takes over at COP 1. A 60 °C
    target is not 'a bit more expensive' — it is a different machine. The old code
    let the LP command 60 °C freely, so PV that could have gone into a
    90%-efficient battery went into an immersion heater instead.

    Note the cliff is on the MARGINAL degrees, not on the whole lift: the heat pump
    still does everything up to 50 °C. Treating it as a switch on the COP would
    price Daikin's own 52.5 °C certification anchor at COP 1, where they measured
    2.51."""
    assert not heats_with_resistance(45.0, P)
    assert not heats_with_resistance(50.0, P)
    assert heats_with_resistance(60.0, P)

    # The EFFECTIVE cop of the lift is what collapses — and it more than halves.
    to_50 = effective_cop(45.0, 50.0, 7.0, P)
    to_60 = effective_cop(45.0, 60.0, 7.0, P)
    assert to_50 > 2.4
    assert to_60 < 1.4
    assert to_50 / to_60 > 1.8


def test_a_lift_across_the_cliff_is_priced_in_two_segments():
    """45 → 60 °C: the first 5 degrees come from the heat pump, the last 10 from
    the resistance. Pricing the whole lift at either rate is wrong in a direction
    that matters."""
    both = electric_kwh_to_raise(45.0, 60.0, 7.0, P)
    hp_part = electric_kwh_to_raise(45.0, 50.0, 7.0, P)
    res_part = thermal_kwh_to_raise(50.0, 60.0, P)  # COP 1 → thermal == electric

    assert both == pytest.approx(hp_part + res_part, abs=0.01)
    # And the resistance dominates: 10 of the 15 degrees, at 2.5x the unit cost.
    assert res_part > hp_part * 2.5


# ---------------------------------------------------------------------------
# Energy, and the reason it takes time
# ---------------------------------------------------------------------------


def test_the_daily_energy_is_the_right_order_against_the_meter():
    """An order-of-magnitude sanity check, and deliberately no stronger than that.

    Roughly 4 kWh thermal/day of draw plus ~1.3 of standing loss, at the certified
    COP, lands under 2 kWh electric. Prod's Daikin counter reports ~2.3 kWh/day.
    Same ballpark from two independent directions — which is real evidence that the
    databook COP is the right order for this house.

    It is NOT a 10% validation, and must not be sold as one: BOTH sides are coarse.
    The daily counter is Onecta's, quantised to whole kWh, and the 4 kWh draw came
    from an estimator built on that same broken instrument. Wanting a tighter
    number here is exactly the itch that produced a circular COP 'measurement' last
    time. If we ever want it properly, it takes a CT clamp on the heat pump, not a
    cleverer fit."""
    draw_thermal = 4.0
    standing_thermal = standing_loss_w(45.0, P) * 24.0 / 1000.0
    electric = (draw_thermal + standing_thermal) / cop_dhw(15.0, 45.0, P)
    assert 1.4 < electric < 2.6


def test_a_setback_recovery_costs_what_it_should():
    """37 → 45 °C, the daily warmup: 1.79 kWh thermal, ~0.65 kWh at the meter."""
    assert thermal_kwh_to_raise(37.0, 45.0, P) == pytest.approx(1.79, abs=0.02)
    assert electric_kwh_to_raise(37.0, 45.0, 15.0, P) == pytest.approx(0.59, abs=0.05)


def test_the_legionella_cycle_is_mostly_resistance_and_the_budget_covers_it():
    """The firmware drives 45 → 60 °C every Sunday, and the installer guide makes
    the 3 kW booster MANDATORY for it ("at least allow the booster heater for
    minimum 4 hours"). So most of that lift is bought at COP 1.

    The model says ~2.7 kWh electric for the lift alone; the configured budget
    (DHW_LEGIONELLA_BUDGET_KWH = 3.5) sits above it, which is right — the firmware
    also HOLDS at 60 °C for up to an hour, and holding a tank 38 K above its
    cupboard is not free."""
    cost = electric_kwh_to_raise(45.0, 60.0, 7.0, P)
    assert 2.4 < cost < 3.0
    assert cost < 3.5  # the budget covers the lift, with room for the hold

    # And it is resistance, not heat pump, that dominates the bill.
    hp_only = electric_kwh_to_raise(45.0, 50.0, 7.0, P)
    assert (cost - hp_only) > 2.0 * hp_only


def test_recovery_is_fast_so_the_binding_question_is_price_not_time():
    """A useful negative result, and it sharpens the design.

    At the 2 kW electrical cap, one 30-minute slot buys ~5-6 kWh thermal ÷ 0.22 =
    over 10 °C of tank. So a 37 → 45 setback recovery fits inside a slot or two:
    heat-up time is NOT what stops the LP from heating late.

    What that means is that the tank's freedom is almost total, and the trade-off
    the LP is really solving is PRICE against STANDING LOSS — heat cheap and hold
    (paying ~0.25 °C/h), or heat late at whatever the tariff happens to be. That is
    precisely why this belongs in the optimiser rather than in a fixed schedule,
    and why slicing the heat across several moments (some on PV, some overnight)
    can beat any single warmup."""
    gain = max_heat_c_per_slot(P, slot_hours=0.5, t_out_c=7.0, tank_c=37.0)
    assert gain > 10.0

    # The whole daily setback recovery fits in one slot of heat pump time.
    assert thermal_kwh_to_raise(37.0, 45.0, P) / (gain * P.kwh_per_degc) < 1.0


def test_the_tank_is_192_litres_not_200():
    """The EKHWSU200's usable volume is 192 L; '200' is the model name. The old
    config used 200, a 4% error in every thermal figure in the system."""
    assert P.litres == 192.0
    assert P.kwh_per_degc == pytest.approx(0.223, abs=0.002)
