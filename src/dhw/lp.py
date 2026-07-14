"""The DHW block of the MILP: the LP times the tank itself.

This is the only module in the package that imports pulp. It builds the tank's
variables and constraints into a problem the caller owns (the battery/grid/space LP),
and hands back the two things that problem needs: the total DHW electricity per slot
(for the energy balance) and the comfort-slack penalty (for the objective).

Keeping it a separate, self-contained block is deliberate. The economic behaviour the
owner asked for — let the tank cool through the expensive hours, buy the heat when it
is cheapest, and slice it across several moments — can be proven in a tiny standalone
LP here, without the hundred other constraints of the full solver obscuring it. The
tests do exactly that.

Four ideas, each mapped to how it is modelled:

* **Slicing is free.** ``e_dhw[i]`` is continuous per slot and the tank has an ODE, so
  the LP puts a piece of the heat on a sunny afternoon and a piece on a cheap night if
  that is cheapest — no special mechanism, it falls out of the linear program. What
  needs a mechanism is the OPPOSITE: stopping the LP from planning thirty 0.05 kWh
  slivers the compressor cannot deliver and the Daikin quota cannot dispatch. Hence a
  minimum run and a slice cap.

* **The resistance cliff is a per-slot binary, and only where it can pay.** Below the
  cliff the heat pump does the work at the certified COP — no binary, no branch. A
  target above the cliff needs the immersion heater at COP 1, which is only ever worth
  it when the grid is PAYING us to import (a negative price). So the binary that
  unlocks it is created ONLY on negative-price slots — a handful a day — and everywhere
  else the tank is capped below the cliff by a plain bound. This is what makes
  ``DHW_TEMP_MAX_C=60`` stop meaning "burn resistance whenever PV is free".

* **Comfort is a soft floor at the START of the slot.** ``tank[i] + slack[i] >=
  floor[i]``, slack penalised. Soft so the solve is never infeasible (a floor that
  cannot be met surfaces as a deficit, never a dead solver — #344/#422). At the START,
  ``tank[i]`` not ``tank[i+1]``, so the LP cannot "meet" comfort by heating during the
  shower.

* **The COP is a per-slot PARAMETER, not a variable.** ``e_dhw[i] * cop[i]`` would be
  bilinear if cop depended on the variable ``tank[i]``. Evaluated at a fixed reference
  target it stays linear, and the error across the operating band [45, 50] is under
  4% — an order of magnitude below the curve's own uncertainty. The alternative
  (piecewise-COP with a segment binary per slot) costs ~96 binaries to buy 3% and is
  explicitly rejected.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulp

from .model import TankParams, cop_dhw

_J_PER_KWH = 3.6e6

#: The reference target the per-slot COP is evaluated at — mid-way between the 45 °C
#: comfort floor and the 50 °C operating ceiling. See the module docstring.
_COP_REF_TARGET_C = 47.0


@dataclass
class DhwBlock:
    """What the surrounding LP needs back from the DHW block."""

    #: Total DHW electricity per slot (heat pump + resistance), kWh — goes into the
    #: caller's energy balance exactly where the old ``e_dhw`` did.
    e_total: list[pulp.LpVariable]
    #: Heat-pump electricity ALONE, kWh. The resistance is a separate immersion
    #: element, not the compressor — so the caller's compressor cap (which DHW shares
    #: with space heating) is on this, not on e_total.
    e_hp: list[pulp.LpVariable]
    #: Tank temperature at each slot boundary (len n+1), for the plan snapshot.
    tank: list[pulp.LpVariable]
    #: The term to ADD to the caller's objective (penalised comfort slack, pence).
    comfort_penalty: pulp.LpAffineExpression
    #: Per-slot heat-pump-on binary, exposed for dispatch row-building.
    dhw_on: list[pulp.LpVariable]


@dataclass(frozen=True)
class DhwLpConfig:
    """The block's tunables. Defaults are the shipping values; the wiring layer
    passes overrides from config so nothing here reads the singleton."""

    tank_ceiling_c: float = 50.0        # hard cap in normal slots — below the cliff
    tank_max_c: float = 60.0            # reachable only on a negative-price slot
    tank_floor_c: float = 20.0          # anti-freeze; the tank never goes below this
    min_run_kwh: float = 0.25           # a heat-pump slot does at least this (~8 min)
    max_slices_per_day: int = 12        # contiguous runs are bounded → so are Daikin rows
    min_dwell_slots: int = 2            # once on, stay on ≥ this many slots
    comfort_penalty_pence_per_degc: float = 50.0
    #: Soft over-temperature allowance (see build). Small: it must not deter the LP
    #: from meeting comfort, only from voluntarily heating above the ceiling.
    ceiling_penalty_pence_per_degc: float = 2.0
    slot_hours: float = 0.5


def build_dhw_block(
    prob: pulp.LpProblem,
    *,
    slot_starts_utc,
    tank0_c: float,
    t_out_by_slot: list[float],
    ambient_by_slot: list[float],
    draw_kwh_by_slot: list[float],
    comfort_floor_by_slot: list[float | None],
    price_by_slot: list[float],
    p: TankParams,
    cfg: DhwLpConfig | None = None,
    day_index_by_slot: list[int] | None = None,
) -> DhwBlock:
    """Add the LP-owned DHW variables and constraints to ``prob``.

    Everything is passed in — no config, no db — so the block is exercised whole by a
    standalone mini-LP in the tests. ``cop`` is derived here from the model at the
    reference target, keeping the energy relation linear.
    """
    cfg = cfg or DhwLpConfig()
    n = len(t_out_by_slot)
    hp_max_kwh = p.hp_max_kw * cfg.slot_hours
    res_max_kwh = p.resistance_kw * cfg.slot_hours
    c_tank = p.c_tank_j_per_k
    dt_s = cfg.slot_hours * 3600.0

    # Per-slot heat-pump COP at the reference target — a parameter, not a variable.
    cop = [cop_dhw(t_out_by_slot[i], _COP_REF_TARGET_C, p) for i in range(n)]

    e_hp = pulp.LpVariable.dicts("dhw_e_hp", range(n), lowBound=0, upBound=hp_max_kwh)
    e_res = pulp.LpVariable.dicts("dhw_e_res", range(n), lowBound=0)
    dhw_on = pulp.LpVariable.dicts("dhw_on", range(n), cat="Binary")
    tank = pulp.LpVariable.dicts(
        "dhw_tank", range(n + 1), lowBound=cfg.tank_floor_c, upBound=cfg.tank_max_c
    )
    slack = pulp.LpVariable.dicts("dhw_comfort_slack", range(n + 1), lowBound=0)
    # Ceiling slack. The ceiling must be SOFT, not a hard bound: a tank that starts
    # ABOVE it (the firmware just ran its 60 °C legionella cycle, or the user boosted
    # it by hand) cannot shed a whole degree in one 30-min slot, so a hard cap would
    # make the first slots Infeasible. Penalising the slack instead lets an
    # over-temperature tank coast back down naturally while still forbidding the LP
    # from HEATING above the ceiling voluntarily — heating up there costs a big COP-1
    # bill AND this penalty, so it never happens unless the price is negative.
    ceil_slack = pulp.LpVariable.dicts("dhw_ceiling_slack", range(n), lowBound=0)

    prob += tank[0] == tank0_c

    for i in range(n):
        # Heat-pump electricity is gated by the on-binary, with a minimum run so the
        # LP cannot plan a sliver the compressor cannot deliver (and dispatch cannot
        # turn into a Daikin write). e_res is separate and unlocked below.
        prob += e_hp[i] <= hp_max_kwh * dhw_on[i]
        prob += e_hp[i] >= cfg.min_run_kwh * dhw_on[i]

        # The resistance is available ONLY on a slot the grid pays us to import.
        # Everywhere else the tank is capped below the cliff, so no immersion heater
        # can ever run "because PV was free".
        negative_price = price_by_slot[i] < 0
        if negative_price:
            b_res = pulp.LpVariable(f"dhw_b_res_{i}", cat="Binary")
            prob += e_res[i] <= res_max_kwh * b_res
            # Only a resistance slot may lift the tank above the heat-pump ceiling.
            prob += tank[i + 1] <= (
                cfg.tank_ceiling_c
                + (cfg.tank_max_c - cfg.tank_ceiling_c) * b_res
                + ceil_slack[i]
            )
        else:
            prob += e_res[i] == 0
            prob += tank[i + 1] <= cfg.tank_ceiling_c + ceil_slack[i]

        # Tank energy balance. Heat pump heat at the certified COP; resistance heat at
        # COP 1; standing loss against the MEASURED, per-slot ambient; the declared
        # draw removed. Linear in the variables, because cop and ambient are params.
        q_hp = e_hp[i] * cop[i] * _J_PER_KWH
        q_res = e_res[i] * 1.0 * _J_PER_KWH
        loss = p.ua_w_per_k * (tank[i] - ambient_by_slot[i]) * dt_s
        draw = draw_kwh_by_slot[i] * _J_PER_KWH
        prob += tank[i + 1] == tank[i] + (q_hp + q_res - loss - draw) / c_tank

        # Comfort: soft floor at the START of the slot, so heat must already be stored.
        floor = comfort_floor_by_slot[i]
        if floor is not None:
            prob += tank[i] + slack[i] >= floor

    # Min-dwell: once the heat pump turns on for DHW it stays on for a few slots, so we
    # get a small number of contiguous runs — each of which becomes at most one Daikin
    # row. The compressor constraint and the quota constraint are the same constraint.
    for i in range(n - 1):
        for k in range(1, cfg.min_dwell_slots):
            if i + k < n:
                # If it switched ON at i (on[i] − on[i-1] = 1), it must still be on at i+k.
                prev = dhw_on[i - 1] if i > 0 else 0
                prob += dhw_on[i + k] >= dhw_on[i] - prev

    # Slice cap per local day — bounds the number of runs, hence Daikin rows.
    if day_index_by_slot is not None:
        by_day: dict[int, list[int]] = {}
        for i, d in enumerate(day_index_by_slot):
            by_day.setdefault(d, []).append(i)
        for slots in by_day.values():
            prob += pulp.lpSum(dhw_on[i] for i in slots) <= cfg.max_slices_per_day

    e_total = [e_hp[i] + e_res[i] for i in range(n)]
    # Comfort slack is the expensive one (a cold shower). Ceiling slack is a soft
    # over-temperature allowance for the coast-down; penalise it enough that the LP
    # never HEATS above the ceiling, but far below the comfort penalty so it never
    # trades a warm shower to avoid a transient over-temperature it did not choose.
    comfort_penalty = (
        cfg.comfort_penalty_pence_per_degc * pulp.lpSum(slack[i] for i in range(n + 1))
        + cfg.ceiling_penalty_pence_per_degc * pulp.lpSum(ceil_slack[i] for i in range(n))
    )
    return DhwBlock(
        e_total=e_total,
        e_hp=[e_hp[i] for i in range(n)],
        tank=list(tank.values()),
        comfort_penalty=comfort_penalty,
        dhw_on=list(dhw_on.values()),
    )
