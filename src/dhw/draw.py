"""How much hot water the household takes, and how hot the tank must be to give it.

Mixer arithmetic. This is the one part of the legacy DHW stack that was right, and
it survives the rewrite intact — a shower at 38 °C drawn from a tank at 45 °C needs
a knowable number of litres, and that number does not depend on any of the beliefs
the rewrite is throwing out.

**The draw is DECLARED, not learned, and that is deliberate.** An earlier attempt
tried to measure it from the tank's energy balance and produced a profile that put
the household's hot-water use in the MORNING — when in fact three people shower
between 20:00 and 21:00. The estimator was not merely noisy; it was structurally
blind. In the evening the firmware holds the tank at target and reheats *during* the
shower, so the temperature barely moves, and the Onecta counter truncates that
sub-1-kWh reheat to zero. In the morning the tank is in setback and nobody reheats
it, so the same draw shows up as a clean fall. It measured *draws that happened to be
visible*, and called that the demand.

So the household's demand comes from the household. See :mod:`src.dhw.comfort`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import TankParams

_J_PER_KWH = 3.6e6


@dataclass(frozen=True)
class ShowerSpec:
    """One shower, as this household actually takes them.

    Defaults are the empirically calibrated ones (PR G, 2026-05): a UK low-flow
    head at 7 L/min, and a mixer temperature of 38 °C — the tap setting, not the
    tank setting. The two are routinely confused, and confusing them is what makes
    people think a tank needs to be at 60 °C.
    """

    flow_lpm: float = 7.0
    duration_min: float = 5.0
    mixer_c: float = 38.0
    cold_inlet_c: float = 10.0

    @property
    def mix_litres(self) -> float:
        """Litres out of the shower head, at ``mixer_c``."""
        return self.flow_lpm * self.duration_min


def hot_litres_for(n_showers: int, tank_c: float, spec: ShowerSpec) -> float:
    """Litres drawn FROM THE TANK to deliver ``n_showers`` at the mixer.

    A hotter tank delivers the same shower from fewer of its own litres — which is
    the whole reason a tank can be pre-heated and then coast: it is storing showers,
    not litres.
    """
    if tank_c <= spec.cold_inlet_c + 0.1:
        return n_showers * spec.mix_litres  # no dilution possible
    dilution = (spec.mixer_c - spec.cold_inlet_c) / (tank_c - spec.cold_inlet_c)
    return n_showers * spec.mix_litres * dilution


def draw_kwh_thermal(n_showers: int, spec: ShowerSpec) -> float:
    """Heat that leaves the tank for ``n_showers`` (kWh thermal).

    Independent of the tank temperature: the energy in a shower is set by the water
    that comes out of the head and the mains it was diluted from, not by how hot the
    cylinder happened to be. A hotter tank simply gives up fewer litres for it.
    """
    return (
        n_showers * spec.mix_litres
        * 1.0  # kg/L
        * (spec.mixer_c - spec.cold_inlet_c)
        * 4186.0
    ) / _J_PER_KWH


def required_tank_temp_for(
    n_showers: int,
    p: TankParams,
    spec: ShowerSpec,
    *,
    usable_fraction: float = 0.85,
    safety_margin_c: float = 2.0,
) -> float:
    """Coldest the tank may be at the START of a run of ``n_showers`` and still
    deliver a warm last shower.

    The tank cannot give up all of its litres at temperature: cold mains enters at
    the bottom while hot leaves the top, and the thermocline climbs. Only the upper
    ``usable_fraction`` comes out at storage temperature — an empirical figure
    (0.85 for this Altherma cylinder), and one the hardware itself argues for: the
    tank thermistor sits ABOVE the coil, so what it reads is exactly this usable
    upper layer.

    Solve ``hot_litres_needed(T) == usable_fraction × litres`` for T.
    """
    if n_showers <= 0:
        return spec.mixer_c + 1.0
    if spec.mixer_c <= spec.cold_inlet_c + 0.1:
        return spec.mixer_c + safety_margin_c

    usable_litres = usable_fraction * p.litres
    mix = n_showers * spec.mix_litres
    delta = spec.mixer_c - spec.cold_inlet_c

    # hot = mix × (mixer − cold) / (T − cold) ≤ usable  ⇒  T ≥ cold + mix·Δ/usable
    required = spec.cold_inlet_c + (mix * delta) / usable_litres
    return required + safety_margin_c
