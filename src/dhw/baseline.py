"""Simulate what the FIXED schedule actually does — the honest baseline.

The economic gate compares "LP times the tank" against "the fixed schedule owns it".
Comparing against the fixed schedule's FORECAST is not honest: that forecast plans
~2.9 kWh/day of DHW electricity while the household's counter shows ~1.2 — the tank
only ever absorbs what its thermostat asks, not what a forecast budgeted. A plan-vs-plan
delta therefore credits the LP for "saving" phantom energy the incumbent never spends.

So the baseline arm is built here instead: a slot-by-slot SIMULATION of the firmware's
thermostat under the SAME physics, the SAME declared draw and the SAME prices as the
LP arm. Target 45 °C from warmup to setback, 37 °C overnight, reheat whenever the tank
falls below target minus hysteresis — including the incumbent's two known flaws, which
this simulation reproduces *by construction*:

* the 13:00 warmup fires at whatever the 13:00 price happens to be, and
* the evening draw pulls the held tank below target, so the firmware reheats DURING
  the showers at evening prices.

Both arms then deliver the same heat to the same household, and the cost difference is
purely WHEN each regime bought it — which is the only thing the gate is supposed to be
measuring.

Pure function: physics in, electricity out. No DB, no config, no clock.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .model import TankParams, cop_dhw

_J_PER_KWH = 3.6e6


def simulate_fixed_schedule(
    slot_starts_utc: list[datetime],
    tz: ZoneInfo,
    *,
    tank0_c: float,
    p: TankParams,
    t_out_by_slot: list[float],
    draw_kwh_by_slot: list[float],
    legionella_kwh_by_slot: list[float] | None = None,
    price_pence_by_slot: list[float] | None = None,
    warmup_hour_local: float = 13.0,
    setback_hour_local: float = 22.0,
    target_c: float = 45.0,
    setback_c: float = 37.0,
    negative_boost_target_c: float = 60.0,
    hysteresis_c: float = 1.0,
    slot_hours: float = 0.5,
) -> tuple[list[float], list[float]]:
    """The fixed schedule's electricity per slot + tank trajectory (len n+1).

    Thermostat semantics, matching the Altherma's reheat behaviour: heating LATCHES ON
    when the tank drops below ``target − hysteresis`` and stays on until the target is
    reached — so a draw mid-window triggers a reheat even if the tank is only a couple
    of degrees short. Heat delivery is capped by the heat pump's electrical limit at
    the certified COP for that slot's outdoor temperature.

    The legionella budget (when given) is added as resistance electricity (COP 1) on
    its slots — the same treatment the LP arm gives it, so the Sunday cycle cancels
    out of the comparison instead of polluting it.
    """
    n = len(slot_starts_utc)
    tank = [0.0] * (n + 1)
    e = [0.0] * n
    tank[0] = tank0_c
    heating = False

    for i in range(n):
        local = slot_starts_utc[i].astimezone(tz)
        hour = local.hour + local.minute / 60.0
        if warmup_hour_local <= hour < setback_hour_local:
            target = target_c
        else:
            target = setback_c

        # Negative-price boost — the REAL fixed schedule does this (dhw_policy writes
        # tank_negative_boost rows commanding ~60 °C whenever the grid pays to import).
        # A baseline that omits it is WORSE than the incumbent on negative days, which
        # would credit the LP-owned arm with an edge the incumbent already captures.
        boosting = (
            price_pence_by_slot is not None
            and i < len(price_pence_by_slot)
            and price_pence_by_slot[i] < 0
        )
        if boosting:
            target = max(target, negative_boost_target_c)

        # Thermostat latch.
        if tank[i] < target - hysteresis_c:
            heating = True
        if tank[i] >= target:
            heating = False

        loss = (
            p.ua_w_per_k * (tank[i] - p.ambient_c) * slot_hours * 3600.0 / _J_PER_KWH
        )
        draw = draw_kwh_by_slot[i]

        thermal_in = 0.0
        e_slot = 0.0
        if heating:
            needed = max(0.0, (target - tank[i]) * p.kwh_per_degc + loss + draw)
            # Below the resistance cliff the heat pump does the work at the certified
            # COP; the degrees above it come from the immersion heater at COP 1 —
            # exactly how the machine splits a boost lift (see dhw.model).
            hp_target = min(target, p.t_hp_max_c)
            cop = cop_dhw(t_out_by_slot[i], hp_target, p)
            hp_needed = max(0.0, min(needed, (hp_target - tank[i]) * p.kwh_per_degc + loss + draw))
            hp_thermal = min(hp_needed, p.hp_max_kw * slot_hours * cop)
            res_thermal = 0.0
            if target > p.t_hp_max_c and tank[i] >= p.t_hp_max_c - hysteresis_c:
                res_thermal = min(needed - hp_thermal if needed > hp_thermal else 0.0,
                                  p.resistance_kw * slot_hours)
            thermal_in = hp_thermal + res_thermal
            e_slot = (hp_thermal / cop if cop > 0 else 0.0) + res_thermal  # res at COP 1

        # Firmware legionella cycle: resistance electricity, thermal 1:1.
        if legionella_kwh_by_slot is not None and legionella_kwh_by_slot[i] > 0:
            leg = legionella_kwh_by_slot[i]
            e_slot += leg
            thermal_in += leg

        # SOLVABILITY CAP. This vector is destined for the LP's PINNED arm, whose
        # e_dhw variable is bounded by the compressor cap (`e_dhw ≤ hp_max × hp_on`)
        # — a pinned value above it makes the whole solve Infeasible (measured: every
        # boost day and one Sunday dropped out of the backtest). The real machine can
        # exceed the cap by stacking the 3 kW booster on the compressor, but the
        # baseline's job is a fair, SOLVABLE incumbent: cap the slot and let the
        # boost/cycle spread across more of its window instead. Thermal scales with
        # the cap so the tank trajectory stays consistent with the energy pinned.
        cap = p.hp_max_kw * slot_hours
        if e_slot > cap and e_slot > 0:
            thermal_in *= cap / e_slot
            e_slot = cap
        e[i] = e_slot

        tank[i + 1] = tank[i] + (thermal_in - loss - draw) / p.kwh_per_degc
        # The firmware never lets the cylinder exceed its own max.
        tank[i + 1] = min(tank[i + 1], 60.0)

    return e, tank


def legionella_budget_by_slot(
    slot_starts_utc: list[datetime],
    *,
    dow: int = 6,
    start_hour_utc: int = 11,
    start_minute_utc: int = 0,
    duration_minutes: int = 120,
    budget_kwh: float = 3.5,
) -> list[float]:
    """Spread the firmware's Sunday-cycle electricity across its stand-off window.

    UTC-anchored — the firmware clock is UTC (see CLAUDE.md). Shared by the LP block's
    wiring and the baseline simulator so both arms budget the identical cycle and it
    cancels out of the comparison.
    """
    start_min = start_hour_utc * 60 + start_minute_utc
    members = [
        i for i, st in enumerate(slot_starts_utc)
        if st.weekday() == dow
        and start_min <= (st.hour * 60 + st.minute) < start_min + duration_minutes
    ]
    out = [0.0] * len(slot_starts_utc)
    if members and budget_kwh > 0:
        per = budget_kwh / len(members)
        for i in members:
            out[i] = per
    return out
