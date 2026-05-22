"""Explicit shower-demand model (PR B).

Formalises DHW demand as ``count × duration × flow_lpm × mixer-temp``
instead of the legacy ``DHW_DAILY_SHOWER_LITRES`` aggregate that spread a
flat number of litres over the shower window.

All settings live in :mod:`src.runtime_settings` (the SQLite-backed
hot-tunable layer). This module is pure: every function reads the current
settings and returns a derived value, with no side effects. The LP solver
consumes the per-window litres + the required-tank-temp; the dispatch
preview consumes :func:`derive_overnight_target_c`.

The "morning reserve" in normal mode is **not** a planned draw — it's a
soft floor at a single morning slot. The LP keeps the tank warm enough
for one shower in case anyone needs it, but doesn't model the litres as
consumed (the household typically doesn't shower in the morning, so
modelling consumption would force daily refills that aren't realised).
"""
from __future__ import annotations

from .config import config
from .presets import OperationPreset


# Backwards-compat escape hatch (env-only, not promoted to runtime_settings).
# If set > 0 in /srv/hem/.env, sidesteps the derivation and uses the legacy
# constant value across the whole shower window. Documented as deprecated.
_LEGACY_DAILY_LITRES_KEY = "DHW_DAILY_SHOWER_LITRES"


def _mode_enum() -> OperationPreset:
    """Coerce ``config.OPTIMIZATION_PRESET`` to the enum, defaulting to NORMAL."""
    try:
        return OperationPreset((config.OPTIMIZATION_PRESET or "normal").strip().lower())
    except (ValueError, AttributeError):
        return OperationPreset.NORMAL


def total_evening_showers(mode: OperationPreset | str | None = None) -> int:
    """Number of evening showers the LP must plan for under *mode*.

    * ``normal``: ``DHW_SHOWERS_NORMAL_EVENING`` (default 4)
    * ``guests``: normal + ``DHW_GUEST_COUNT × DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST``
    * ``vacation``: 0
    """
    m = OperationPreset(mode) if mode is not None else _mode_enum()
    if m == OperationPreset.VACATION:
        return 0
    base = int(getattr(config, "DHW_SHOWERS_NORMAL_EVENING", 4))
    if m == OperationPreset.GUESTS:
        guests = int(getattr(config, "DHW_GUEST_COUNT", 2))
        per_guest = int(getattr(config, "DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST", 1))
        return max(0, base + guests * per_guest)
    return max(0, base)


def total_morning_showers(mode: OperationPreset | str | None = None) -> int:
    """Number of morning showers the LP must plan for under *mode*.

    In normal mode this is the **reserve** count (a soft floor at the
    morning hour, no draw modelled). In guests mode the visitor extras
    are actual draws.
    """
    m = OperationPreset(mode) if mode is not None else _mode_enum()
    if m == OperationPreset.VACATION:
        return 0
    reserve = int(getattr(config, "DHW_SHOWERS_NORMAL_MORNING_RESERVE", 1))
    if m == OperationPreset.GUESTS:
        guests = int(getattr(config, "DHW_GUEST_COUNT", 2))
        per_guest = int(getattr(config, "DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST", 1))
        return max(0, reserve + guests * per_guest)
    return max(0, reserve)


def mix_litres_per_shower() -> float:
    """Mixer-out litres delivered per shower (no hot/cold split)."""
    return float(config.DHW_SHOWER_DURATION_MIN) * float(config.DHW_SHOWER_FLOW_LPM)


def hot_litres_per_shower(tank_temp_c: float) -> float:
    """Hot litres drawn from the tank for one mixer-out shower.

    Standard mixer math: ``hot = mix × (mixer − cold) / (tank − cold)``.
    Assumes the tank can still deliver at ``tank_temp_c`` throughout the
    shower; consult :func:`required_tank_temp_for_n_showers` for the
    starting temperature needed so that the *last* shower still mixes.
    """
    mixer = float(config.DHW_SHOWER_MIXER_TEMP_C)
    cold = float(config.DHW_SHOWER_COLD_INLET_TEMP_C)
    if tank_temp_c <= cold + 0.1:
        # Tank as cold as inlet — mixer math undefined; return mix litres
        # (no dilution available, full draw is "hot" by accounting fiction).
        return mix_litres_per_shower()
    return mix_litres_per_shower() * (mixer - cold) / (tank_temp_c - cold)


def required_tank_temp_for_n_showers(
    n: int,
    *,
    mixer_temp_c: float | None = None,
    flow_lpm: float | None = None,
    duration_min: float | None = None,
    tank_litres: float | None = None,
    cold_temp_c: float | None = None,
    usable_fraction: float | None = None,
    safety_margin_c: float = 2.0,
) -> float:
    """Lowest tank °C at the start of a draw of *n* consecutive showers
    that still delivers at ``mixer_temp_c`` for the final shower.

    Stratification simplification: we assume the upper ``usable_fraction``
    of the tank is at the storage temperature; the lower portion mixes
    with cold inlet during the draw. This is an empirical approximation
    matching observed Daikin Altherma behaviour (≈0.7 of nominal volume).

    The mathematics is: total mix litres = ``n × duration × flow``. Hot
    litres required at the storage temperature to satisfy the mix:
    ``hot_required = mix × (mixer − cold) / (storage − cold)``. The tank
    can deliver at most ``usable_fraction × tank_litres`` of hot litres
    before stratification gives way. Solve for the storage temperature
    that makes the two equal, plus ``safety_margin_c``.

    Bails to a 1 °C-above-mixer floor when n=0 (used by the morning
    reserve path when reserve=0 means "any tank state is fine").
    """
    if n <= 0:
        return float(config.DHW_SHOWER_MIXER_TEMP_C) + 1.0
    mixer = float(mixer_temp_c if mixer_temp_c is not None else config.DHW_SHOWER_MIXER_TEMP_C)
    cold = float(cold_temp_c if cold_temp_c is not None else config.DHW_SHOWER_COLD_INLET_TEMP_C)
    flow = float(flow_lpm if flow_lpm is not None else config.DHW_SHOWER_FLOW_LPM)
    dur = float(duration_min if duration_min is not None else config.DHW_SHOWER_DURATION_MIN)
    tank_l = float(tank_litres if tank_litres is not None else config.DHW_TANK_LITRES)
    usable = float(usable_fraction if usable_fraction is not None else config.DHW_TANK_USABLE_FRACTION)

    if mixer <= cold + 0.1:
        return mixer + safety_margin_c

    mix_required = n * dur * flow
    hot_capacity = usable * tank_l
    if hot_capacity <= 0.0:
        return float(config.DHW_TEMP_MAX_C)

    # hot_required / mix = (mixer − cold) / (storage − cold)
    # If hot_required <= hot_capacity, the tank delivers at storage temp
    # and we just need storage above mixer. Otherwise solve for the
    # storage that, given the dilution from the inaccessible portion,
    # still yields enough hot.
    if mix_required <= hot_capacity:
        return mixer + safety_margin_c

    storage = cold + mix_required * (mixer - cold) / hot_capacity
    return max(mixer + safety_margin_c, storage + safety_margin_c)


def derive_overnight_target_c(mode: OperationPreset | str | None = None) -> float:
    """Tank target the dispatch preview should hold overnight.

    Derived from the morning-reserve demand under *mode*. Clamped into
    [``DHW_TEMP_NORMAL_C - 5``, ``DHW_TEMP_NORMAL_C + 5``] so a
    pathological flow/duration setting can't push the tank into useless
    extremes.

    Vacation mode: returns the anti-freeze floor (``DHW_TEMP_MIN_FLOOR_C``,
    default 30 °C) — though the dispatch will set ``tank_power=False`` and
    skip writes in vacation mode entirely, this is the value the soft
    floor falls back to if the dispatch path ever queries it.
    """
    m = OperationPreset(mode) if mode is not None else _mode_enum()
    if m == OperationPreset.VACATION:
        return float(getattr(config, "DHW_TEMP_MIN_FLOOR_C", 30.0))
    reserve = total_morning_showers(m)
    if reserve <= 0:
        return float(config.DHW_TEMP_NORMAL_C)
    derived = required_tank_temp_for_n_showers(reserve)
    floor = float(config.DHW_TEMP_NORMAL_C) - 5.0
    ceil = float(config.DHW_TEMP_NORMAL_C) + 5.0
    return max(floor, min(ceil, derived))


def required_tank_temp_for_window(
    window: str, mode: OperationPreset | str | None = None
) -> float:
    """Tank temperature required at the start of *window* ('evening' or
    'morning') under *mode*. Reuses :func:`required_tank_temp_for_n_showers`
    with the mode-appropriate shower count."""
    m = OperationPreset(mode) if mode is not None else _mode_enum()
    if window == "evening":
        n = total_evening_showers(m)
    elif window == "morning":
        n = total_morning_showers(m)
    else:
        raise ValueError(f"window must be 'evening' or 'morning', got {window!r}")
    return required_tank_temp_for_n_showers(n)


def daily_shower_litres_drawn(mode: OperationPreset | str | None = None) -> float:
    """Total mix-out litres planned per day under *mode*.

    The legacy ``DHW_DAILY_SHOWER_LITRES`` env var overrides this when
    set > 0 (escape hatch for operators who want the old aggregate
    behaviour back). Otherwise: evening × mix_per_shower + morning
    extras (in guests mode only — morning reserve in normal mode is a
    floor, not a draw).
    """
    legacy = float(getattr(config, _LEGACY_DAILY_LITRES_KEY, 0.0) or 0.0)
    if legacy > 0.0:
        return legacy
    m = OperationPreset(mode) if mode is not None else _mode_enum()
    if m == OperationPreset.VACATION:
        return 0.0
    mix = mix_litres_per_shower()
    evening = total_evening_showers(m) * mix
    # Morning draw only in guests mode — normal mode's morning is a reserve.
    morning = 0.0
    if m == OperationPreset.GUESTS:
        guests = int(getattr(config, "DHW_GUEST_COUNT", 2))
        per_guest = int(getattr(config, "DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST", 1))
        morning = guests * per_guest * mix
    return evening + morning


def kwh_electric_to_reheat(
    from_c: float,
    to_c: float,
    cop: float,
    tank_litres: float | None = None,
) -> float:
    """How much electrical kWh to lift the tank from *from_c* to *to_c*
    at a given DHW COP. Informational helper for the brief and audit
    paths; the LP derives the same physics from the in-solve thermal
    balance.

    Example: 200 L tank, 38 → 47.5 °C, COP 3.0 → ~0.74 kWh electric.
    """
    if to_c <= from_c or cop <= 0.0:
        return 0.0
    tank_l = float(tank_litres if tank_litres is not None else config.DHW_TANK_LITRES)
    cp = float(getattr(config, "DHW_WATER_CP", 4186.0))
    joules = (to_c - from_c) * tank_l * cp
    return joules / (cop * 3.6e6)
