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
    warmup_hour_local: float = 13.0,
    setback_hour_local: float = 22.0,
    target_c: float = 45.0,
    setback_c: float = 37.0,
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

        # Thermostat latch.
        if tank[i] < target - hysteresis_c:
            heating = True
        if tank[i] >= target:
            heating = False

        cop = cop_dhw(t_out_by_slot[i], target, p)
        cap_thermal = p.hp_max_kw * slot_hours * cop  # kWh thermal per slot

        loss = (
            p.ua_w_per_k * (tank[i] - p.ambient_c) * slot_hours * 3600.0 / _J_PER_KWH
        )
        draw = draw_kwh_by_slot[i]

        thermal_in = 0.0
        if heating:
            needed = max(0.0, (target - tank[i]) * p.kwh_per_degc + loss + draw)
            thermal_in = min(needed, cap_thermal)
        e[i] = thermal_in / cop if cop > 0 else 0.0

        # Firmware legionella cycle: resistance electricity, thermal 1:1.
        if legionella_kwh_by_slot is not None and legionella_kwh_by_slot[i] > 0:
            leg = legionella_kwh_by_slot[i]
            e[i] += leg
            thermal_in += leg

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
