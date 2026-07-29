"""Decide whether the tank needs an overnight top-up, and where to buy it.

The question this answers is deliberately narrow, and it is asked at the one
moment it can be answered honestly: **after the evening showers**.

Before them, "will the morning be warm enough?" requires modelling a draw, and
the two available models disagree by 3x — the declared three showers imply a
15.3 °C fall on this cylinder, while the measured setback-to-morning drop over
28 nights has a median of 5 and a p90 of 10. Guessing high buys resistance heat
nine nights in ten; guessing low is the 2026-07-28 cold shower. Waiting until
~22:00 removes the guess entirely: the draw has happened, and the tank
thermometer has already told us the answer.

From there the physics is a plain coast — no further draws until morning — so
the projection is the same ODE the rest of the module uses, and the only real
decision left is economic: *which* half-hour to buy the heat in.

The household's constraint is "avoid overnight heating as much as possible",
not "never". Where a top-up is genuinely unavoidable this puts it in the
cheapest half-hour before the shower rather than leaving the firmware to pick
the moment — which today means it fires wherever the tank happens to cross the
reheat deadband, quite possibly inside the evening peak.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .model import TankParams, coast_to

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TopUpPlan:
    """One scheduled half-hour of heat, and the arithmetic that justified it."""

    at_utc: datetime
    end_utc: datetime
    target_c: int
    price_p: float
    projected_without_c: float
    projected_with_c: float
    floor_c: float
    hours_to_morning: float
    # True when no reachable target clears the floor and this is simply the best
    # the heat pump can do. The caller should say so rather than imply comfort
    # is guaranteed.
    best_effort: bool


def required_start_temp_c(
    floor_c: float, hours: float, p: TankParams, *, max_target_c: float
) -> float:
    """The tank temperature that coasts down to exactly ``floor_c`` after
    ``hours``, i.e. :func:`coast_to` inverted.

    ``coast_to`` is ``A + (T0 − A)·exp(−h/τ)``, so ``T0 = A + (floor − A)·exp(h/τ)``.
    Clamped at ``max_target_c`` by the caller's feasibility test rather than here,
    so the caller can tell "needs 48" apart from "needs 62, impossible".
    """
    tau_h = p.tau_hours  # the same τ `coast_to` uses — never recompute it here
    if tau_h <= 0 or hours <= 0:
        return floor_c
    amb = p.ambient_c
    # A floor at or below ambient is reached by coasting alone — no heat needed.
    if floor_c <= amb:
        return floor_c
    return amb + (floor_c - amb) * math.exp(hours / tau_h)


def plan_morning_topup(
    *,
    tank_c: float,
    now_utc: datetime,
    morning_entry_utc: datetime,
    floor_c: float,
    margin_c: float,
    p: TankParams,
    rates: list[dict[str, Any]],
    max_target_c: float,
    differential_c: float,
    min_shortfall_c: float,
) -> TopUpPlan | None:
    """The cheapest half-hour to buy the morning's comfort, or None if the tank
    will coast there on its own — or if buying would cost more than it is worth.

    ``rates`` are Agile import rows (``valid_from`` / ``valid_to`` / ``value_inc_vat``)
    covering the window; only slots strictly between now and the morning entry
    are eligible. Later slots need a lower commanded target — less coasting left
    — so feasibility is easiest late and cheapness can be anywhere. That trade
    is the whole reason this searches rather than just picking the last slot.

    THE DEADBAND IS A HARD PHYSICAL FLOOR ON THE PURCHASE. The firmware only
    starts heating when the commanded target exceeds the tank by more than its
    reheat differential (~6 °C measured), and then it heats all the way to that
    target. So a small top-up is not something this hardware can do: asking for
    39 °C from a 38 °C tank is a no-op, and the smallest purchase that does
    anything is ``tank + differential``.

    That makes the top-up inherently coarse, and it is why ``min_shortfall_c``
    exists. Buying a ~6 °C overshoot to close a 0.5 °C gap is bad value, so a
    shortfall smaller than that threshold is left alone. Without it this fires
    on roughly a third of nights — against a household rule of "avoid overnight
    heating as much as possible", that is the wrong default.

    (The first version of this ignored the deadband and commanded the bare
    minimum. On the common firing band that command was inside the deadband, so
    the firmware never started and the only thing that actuated was the
    deadband guard's escalation — to the 50 °C cliff, i.e. the exact opposite of
    the "minimum target" the docstring claimed.)
    """
    if morning_entry_utc <= now_utc:
        return None
    hours_total = (morning_entry_utc - now_utc).total_seconds() / 3600.0

    projected = coast_to(tank_c, hours_total, p)
    shortfall = floor_c - projected
    if shortfall <= 0:
        return None  # the tank gets there by itself; buy nothing
    if shortfall < max(0.0, min_shortfall_c):
        return None  # too small to be worth a deadband-sized overshoot

    goal = floor_c + max(0.0, margin_c)
    # The smallest command that actually starts the heat pump. `+ 0.5` mirrors
    # the guard `_warmup_deadband_force_reason` uses for the same physics.
    min_effective = tank_c + differential_c + 0.5

    best: TopUpPlan | None = None
    best_feasible_price: float | None = None
    fallback: TopUpPlan | None = None

    for r in rates:
        try:
            start = _parse(r.get("valid_from"))
            end = _parse(r.get("valid_to"))
            price = float(r.get("value_inc_vat"))
        except (TypeError, ValueError):
            continue
        if start is None or end is None:
            continue
        if start <= now_utc or start >= morning_entry_utc:
            continue

        hours_after = (morning_entry_utc - start).total_seconds() / 3600.0
        need = required_start_temp_c(goal, hours_after, p, max_target_c=max_target_c)
        # Whichever is larger: what the floor needs, or what the firmware needs
        # to move at all.
        want = max(need, min_effective)
        feasible = want <= max_target_c
        target = int(math.ceil(min(want, max_target_c)))
        plan = TopUpPlan(
            at_utc=start,
            end_utc=end,
            target_c=target,
            price_p=price,
            projected_without_c=round(projected, 1),
            projected_with_c=round(coast_to(float(target), hours_after, p), 1),
            floor_c=floor_c,
            hours_to_morning=round(hours_after, 2),
            best_effort=not feasible,
        )
        if feasible:
            if best_feasible_price is None or price < best_feasible_price:
                best_feasible_price = price
                best = plan
        elif best is None:
            # Nothing reachable yet — keep the cheapest unreachable one so the
            # tank still gets the most the heat pump can give, cheaply. A
            # feasible slot found later always wins over this.
            if fallback is None or price < fallback.price_p:
                fallback = plan

    return best or fallback


def _parse(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt


def next_morning_entry_utc(now_local: datetime, start_hour_local: float) -> datetime:
    """UTC instant of the next morning shower-window entry after ``now_local``.

    ``now_local`` must be tz-aware. The arithmetic is done on the LOCAL wall
    clock — the household showers at 07:00 local, not at a fixed UTC hour — and
    converted at the end, so the answer stays right across a DST change.
    """
    from datetime import UTC

    entry = now_local.replace(
        hour=int(start_hour_local) % 24,
        minute=int(round((start_hour_local % 1) * 60)),
        second=0,
        microsecond=0,
    )
    if entry <= now_local:
        entry += timedelta(days=1)
    return entry.astimezone(UTC)
