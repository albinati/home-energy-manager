"""The DHW tank's physics. Pure functions, no DB, no config, no solver.

This module is deliberately the bottom of the dependency graph: it takes a frozen
:class:`TankParams` and answers questions about the tank. Nothing here reads
``config`` or ``db``, so every number below is testable against physics we can
state in advance — and a bad calibration can never reach in and change the model,
only the parameters handed to it.

WHY A REWRITE (2026-07-14). The previous DHW stack rested on three assumptions
that measurement and the manufacturer's own databook both refuted:

* **The LP believed tank heat cost half of what it does.** It read the DHW COP off
  the SPACE-heating curve (LWT 35 °C) minus a fudge, landing at ~4.70. DHW needs
  LWT ~50-55 °C. Daikin's certified figure (EN 16147) is **2.51 at 7 °C**.
* **Above ~50 °C the tank stops being a heat pump.** The installer guide defines a
  ``T_HP_MAX`` above which the backup/booster resistance takes over — **COP ≈ 1**.
  A 60 °C target is not "a bit more expensive"; it is a different machine. The old
  code let the LP command 60 °C freely (``DHW_TEMP_MAX_C``, the PV-abundance
  target), so PV that could have gone into a 90%-efficient battery went into a
  100%-efficient-at-COP-1 immersion heater instead.
* **The standing-loss ambient was the living-room setpoint (21 °C).** The tank sits
  in a cupboard whose *effective* ambient measures 22.4 °C while the house is at
  29-31 °C. Assuming the house temperature biases the loss badly in both seasons.

WHAT WE DO NOT MODEL, AND WHY (read this before "improving" it):

* **We do not learn the COP from telemetry.** It is not measurable here:
  ``daikin_consumption_2hourly.kwh_dhw`` is half a counter quantised to WHOLE kWh
  (a 0.6 kWh reheat reads 0.0) and half a figure our own code SYNTHESISES by
  dividing the tank's temperature rise by an assumed COP. Fitting a COP against
  that returns the assumption. It has already happened once. The certified EN 16147
  numbers are third-party test data and the daily cross-check closes to ~10%
  (2.1 kWh/day predicted vs ~2.3 measured), which is better than any fit we could
  do with the instruments on this house.
* **The tank temperature is not an energy state.** The sensor sits ABOVE the coil,
  so it reads the TOP of the cylinder. During a draw the thermocline rises from the
  bottom and the sensor holds its reading until the front reaches it, then falls
  off a cliff. Treat ``tank_c`` as "how hot is the water we would deliver next",
  not "how much energy is stored".
"""
from __future__ import annotations

from dataclasses import dataclass

# EN 16147 certified DHW COP for the Altherma 3 H HT (databook EEDEN20 p.26),
# tested at a reference hot-water temperature of 52.5 °C. These are the anchors;
# everything else is interpolation between them.
#
# Interpolate PIECEWISE through the points rather than fitting a line to them: a
# least-squares line misses every anchor (2.27 vs 2.17 at the cold end), and the
# cold end is the one that decides winter. The three tuples cost nothing.
_EN16147_COP: tuple[tuple[float, float], ...] = (
    (2.0, 2.17),   # "colder" climate
    (7.0, 2.51),   # "average" climate
    (14.0, 2.76),  # "warmer" climate
)
_COP_TEST_TARGET_C = 52.5

# COP falls as the tank target rises (the condenser has to reach a higher
# temperature). Daikin publishes no DHW-vs-target table, so this comes from the
# EN 14511 pair at the same outdoor temperature: A7/W45 → COP 3.42, A7/W55 → 3.01.
# That is −12% over 10 K.
_COP_PER_K_OF_TARGET = -0.012

# Sanity rails. A DHW COP outside this band is a modelling error, not a heat pump.
_COP_MIN, _COP_MAX = 1.5, 3.2

_J_PER_KWH = 3.6e6


@dataclass(frozen=True)
class TankParams:
    """Everything the physics needs. Frozen: the model cannot be reconfigured
    mid-solve, and a test can state the whole world in one literal.

    The defaults are the DATABOOK tank (EKHWSU200) plus the two constants we can
    honestly measure from the thermometer alone (``ua_w_per_k``, ``ambient_c``).
    ``src.dhw.params`` swaps in learned values when they pass their gates.
    """

    #: 192 L of storage, NOT 200 — the EKHWSU200's nameplate is the model name,
    #: its usable volume is 192 (technical datasheet). The old config said 200,
    #: a 4% error in every thermal calculation.
    litres: float = 192.0
    cp_j_per_kg_k: float = 4186.0

    #: MEASURED (joint fit of UA and ambient over 19 overnight coast episodes).
    #: The catalogue declares 1.22 W/K for the bare cylinder; the real system loses
    #: twice that through pipework, dead legs and the coil. Use what the tank does,
    #: not what the cylinder was tested as.
    ua_w_per_k: float = 2.44
    #: MEASURED, and an EFFECTIVE value: it is whatever ambient makes the observed
    #: decay come out right, so it also absorbs unmodelled losses. It is NOT the
    #: cupboard's thermometer reading, and should not be presented as one.
    ambient_c: float = 22.4

    #: Above this the heat pump hands over to resistance (installer guide, §9). The
    #: true threshold varies with outdoor temperature and Daikin does not publish
    #: it; 50 °C is the value in their own worked example and is the conservative
    #: read. Everything above it costs COP 1.
    t_hp_max_c: float = 50.0

    #: Electrical caps (kW). The heat pump's DHW *thermal* capacity is ~8 kW at
    #: 7 °C, so the binding constraint in practice is the electrical draw, not the
    #: coil.
    hp_max_kw: float = 2.0
    resistance_kw: float = 3.0

    #: Where these numbers came from — carried into the plan snapshot so an audit
    #: can tell a measured tank from a databook one.
    source: str = "databook"

    @property
    def c_tank_j_per_k(self) -> float:
        """Heat capacity of the stored water (J/K)."""
        return self.litres * self.cp_j_per_kg_k

    @property
    def kwh_per_degc(self) -> float:
        """Thermal kWh to move the whole tank by 1 K."""
        return self.c_tank_j_per_k / _J_PER_KWH

    @property
    def tau_hours(self) -> float:
        """Time constant of the coast: C/UA. Measures ~92 h — the tank is very
        nearly a thermos, and that is the entire economic case for letting the LP
        time it. Heat at 03:00, coast to 20:00, lose about 4 °C."""
        return self.c_tank_j_per_k / (self.ua_w_per_k * 3600.0)


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


def _interp(points: tuple[tuple[float, float], ...], x: float) -> float:
    """Piecewise-linear through ``points`` (sorted by x), extrapolating the end
    segments' slopes beyond the range."""
    if x <= points[0][0]:
        (x0, y0), (x1, y1) = points[0], points[1]
    elif x >= points[-1][0]:
        (x0, y0), (x1, y1) = points[-2], points[-1]
    else:
        for i in range(len(points) - 1):
            if points[i][0] <= x <= points[i + 1][0]:
                (x0, y0), (x1, y1) = points[i], points[i + 1]
                break
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def heats_with_resistance(target_c: float, p: TankParams) -> bool:
    """Would a target of ``target_c`` be delivered by the resistance heater?

    This is a cliff, not a slope. Below ``t_hp_max_c`` the heat pump does the work
    at COP 2-3; above it the 3 kW immersion (or the 6 kW backup) does it at COP 1.
    The LP must never treat the last few degrees as merely "a bit dearer" — they
    cost 2.5× the ones below."""
    return target_c > p.t_hp_max_c


def cop_dhw(t_out_c: float, target_c: float, p: TankParams | None = None) -> float:
    """The HEAT PUMP's COP for heating the tank to ``target_c``.

    Certified EN 16147 curve in the outdoor temperature, corrected for the target
    (the test runs at 52.5 °C; this household runs 45, where the heat pump does
    better). The old code's ~4.70 came from the SPACE-heating curve; at the real
    operating point this returns ~2.7 in summer and ~2.3 in winter — tank heat
    costs roughly **twice** what the LP used to believe.

    This is the heat pump ALONE, and it never returns 1.0. The resistance cliff is
    NOT a switch on this function: the heat pump works all the way up to
    ``t_hp_max_c`` and only the degrees ABOVE it are bought from the immersion
    heater. Modelling the cliff as "target > 50 ⇒ the whole lift is COP 1" is both
    wrong and self-contradictory — it would price the certification's own 52.5 °C
    anchor at COP 1 when Daikin measured 2.51 there. Ask
    :func:`electric_kwh_to_raise` what a lift costs; it splits the segments.
    """
    p = p or TankParams()
    base = _interp(_EN16147_COP, t_out_c)
    corrected = base * (1.0 + _COP_PER_K_OF_TARGET * (target_c - _COP_TEST_TARGET_C))
    return max(_COP_MIN, min(_COP_MAX, corrected))


def effective_cop(from_c: float, to_c: float, t_out_c: float,
                  p: TankParams | None = None) -> float:
    """Heat delivered ÷ electricity bought for a whole lift — the number that
    actually decides whether a plan is a good idea.

    For a lift that stays under the cliff this is just :func:`cop_dhw`. For one
    that crosses it, it collapses towards 1 as the resistance takes over: this is
    the function that tells the LP that pushing the tank to 60 °C is not "a bit
    dearer" but a different machine.
    """
    p = p or TankParams()
    e = electric_kwh_to_raise(from_c, to_c, t_out_c, p)
    if e <= 0:
        return cop_dhw(t_out_c, to_c, p)
    return thermal_kwh_to_raise(from_c, to_c, p) / e


# ---------------------------------------------------------------------------
# Losses and coasting
# ---------------------------------------------------------------------------


def standing_loss_w(tank_c: float, p: TankParams, *, ambient_c: float | None = None) -> float:
    """Heat leaking to the cupboard (W). Negative below ambient (the tank would be
    gaining), which the callers clamp — a tank that cold is a different problem."""
    amb = p.ambient_c if ambient_c is None else ambient_c
    return p.ua_w_per_k * (tank_c - amb)


def coast_rate_c_per_h(tank_c: float, p: TankParams, *, ambient_c: float | None = None) -> float:
    """How fast the tank cools with nothing running (°C/h).

    ~0.25 °C/h at 45 °C in summer. Winter is worse only because the cupboard is
    colder — the tank's insulation does not change with the season, the gap does.
    """
    return standing_loss_w(tank_c, p, ambient_c=ambient_c) * 3600.0 / p.c_tank_j_per_k


def coast_to(tank_c: float, hours: float, p: TankParams,
             *, ambient_c: float | None = None) -> float:
    """Exact Newtonian coast: where the tank lands after ``hours`` unheated.

    Exact rather than ``T − rate·h`` because over the horizons that matter (heat at
    03:00, shower at 20:00 — 17 hours) the linear approximation drifts.
    """
    import math

    amb = p.ambient_c if ambient_c is None else ambient_c
    return amb + (tank_c - amb) * math.exp(-hours / p.tau_hours)


def hours_to_coast_from(tank_c: float, to_c: float, p: TankParams,
                        *, ambient_c: float | None = None) -> float | None:
    """How long the tank can be left alone before it falls to ``to_c``.

    The question the LP is really asking when it decides to let the tank cool. None
    when it never gets there (``to_c`` at or below ambient)."""
    import math

    amb = p.ambient_c if ambient_c is None else ambient_c
    if to_c <= amb or tank_c <= to_c:
        return None
    return p.tau_hours * math.log((tank_c - amb) / (to_c - amb))


# ---------------------------------------------------------------------------
# Heating
# ---------------------------------------------------------------------------


def thermal_kwh_to_raise(from_c: float, to_c: float, p: TankParams) -> float:
    """Heat needed to lift the whole tank from one temperature to another (kWh).

    Ignores standing loss during the lift — at ~2 kW in and ~55 W out, the loss is
    under 3% of the energy and the LP models it separately anyway.
    """
    return max(0.0, (to_c - from_c)) * p.kwh_per_degc


def electric_kwh_to_raise(from_c: float, to_c: float, t_out_c: float,
                          p: TankParams) -> float:
    """What that lift costs at the meter.

    Note the discontinuity: crossing ``t_hp_max_c`` does not make the next degree
    slightly dearer, it makes it ~2.5× dearer. The segments are priced separately
    so a lift that straddles the cliff is costed honestly.
    """
    if to_c <= from_c:
        return 0.0
    cliff = p.t_hp_max_c
    hp_to = min(to_c, max(from_c, cliff))
    res_from = max(from_c, cliff)

    kwh = 0.0
    if hp_to > from_c:
        thermal = thermal_kwh_to_raise(from_c, hp_to, p)
        kwh += thermal / cop_dhw(t_out_c, hp_to, p)
    if to_c > res_from:
        # Resistance: COP 1, so thermal kWh == electric kWh.
        kwh += thermal_kwh_to_raise(res_from, to_c, p)
    return kwh


def max_heat_c_per_slot(p: TankParams, *, slot_hours: float, t_out_c: float,
                        tank_c: float) -> float:
    """Ceiling on how many °C the tank can gain in one slot, from the electrical
    cap. This is what stops the LP from planning a lift that the compressor cannot
    physically deliver in time — the reason it must start heating hours before the
    shower rather than minutes."""
    e_max = p.hp_max_kw * slot_hours
    thermal = e_max * cop_dhw(t_out_c, min(tank_c + 5.0, p.t_hp_max_c), p)
    return thermal / p.kwh_per_degc
