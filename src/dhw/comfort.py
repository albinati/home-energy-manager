"""SHOWER comfort: what the household needs from the hot-water tank, and when.

## This is not house comfort, and the two must never be conflated

There are two comfort problems in this system and they take their answers from
different places. Mixing them is how you get a heat pump that heats the house to
serve a shower, or a tank held at 50 °C because a hallway sensor read cold.

**Shower comfort (this module).** DECLARED by the household. There is no sensor in
the shower and there never will be; the only instrument that matters is whether the
family felt the water was warm enough. So the numbers here are settings the owner
turns, and the system's job is to hit them — not to infer them. They are expected to
be TUNED over time ("45 was fine all winter, try 44"), which is why they are runtime
settings and not constants in the source.

**House comfort (elsewhere, later).** Space heating has real instruments — the indoor
sensors (#540) publish room temperatures, and a thermal model can reason about what
the house needs and when. That comfort target is also adjustable, but unlike this one
it takes SENSOR INPUT. It belongs to the LWT/radiator side and is deliberately out of
scope here.

The failure this separation prevents is not hypothetical. An earlier attempt derived
shower demand from an estimator and concluded this household showered in the MORNING —
it was structurally blind to evening draws, because the firmware reheats *during* a
shower and the Onecta counter truncates that reheat to zero. Three people shower
between 20:00 and 21:00. Had it reached the tank, the children would have had cold
showers every day, and the solve would have reported success.

Hence the rule: **a calibration bug may cause the LP to FAIL a floor — visibly, as
penalised slack and as a dispatch backstop — but may never MOVE one.**

## Ground truth (owner, 2026-07-14)

* Showers are **20:00–21:00**, three people (two children and his wife).
* **45 °C is enough for four-plus people.** Not 48, not 60.
* He *sometimes* showers in the morning — one shower, not every day.
* The tank does not need heating just before the showers: it holds heat well
  (τ ≈ 95 h), so the energy can be bought hours earlier and coasted in.

That last point is a LICENCE, not a requirement. The floors below say what must be
true at shower time and say nothing about how the LP gets there. Deciding *when* to
buy the heat — and whether to buy it in one go or slice it across a sunny afternoon
and a cheap night — is the optimiser's entire job.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .draw import ShowerSpec, required_tank_temp_for
from .model import TankParams


@dataclass(frozen=True)
class ShowerComfortWindow:
    """The tank must be at least ``floor_c`` throughout ``[start_hour, end_hour)``
    local, measured at the START of each slot so the heat is already stored."""

    start_hour: float
    end_hour: float
    floor_c: float
    label: str


# ---------------------------------------------------------------------------
# The declared schedule. Every number here is a DIAL, not a discovery.
# ---------------------------------------------------------------------------

#: Defaults, from the owner. All four are runtime-tunable (see ``shower_windows``):
#: the family adjusts them by how the shower felt, which is the only sensor that
#: exists for this.
DEFAULT_EVENING_START_HOUR = 20.0
DEFAULT_EVENING_END_HOUR = 21.0
#: 45 °C covers four-plus people — lived experience, and the mixer arithmetic agrees
#: (three 5-minute showers need ~43 °C at the start of the run). Turn this DOWN if
#: the family never notices; that is the cheapest saving in the system.
DEFAULT_EVENING_FLOOR_C = 45.0

DEFAULT_MORNING_START_HOUR = 7.0
DEFAULT_MORNING_END_HOUR = 9.0
#: Deliberately modest: this is ONE occasional shower, not the family's. Holding the
#: tank hot all night for a shower that may not happen is exactly the standing-loss
#: waste this rewrite exists to stop. 40 °C still delivers 38 °C at the mixer.
DEFAULT_MORNING_FLOOR_C = 40.0

#: Guests: an unknown house-full, and no lived-experience number to lean on. This is
#: the ONE place the mixer arithmetic is allowed to set a floor.
GUESTS_EXTRA_SHOWERS = 3


def _setting(key: str, default: float) -> float:
    """Read a runtime-tunable comfort dial, falling back to the declared default.

    Deliberately tolerant: comfort must survive a missing or malformed setting by
    using the household's stated number, never by failing or by guessing lower.
    """
    try:
        from .. import runtime_settings as rts

        val = rts.get_setting(key)
    except Exception:  # noqa: BLE001 — a settings outage may not cool the showers
        return default
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def shower_windows(
    *,
    preset: str,
    p: TankParams | None = None,
    spec: ShowerSpec | None = None,
    guest_count: int = 2,
) -> tuple[ShowerComfortWindow, ...]:
    """The household's shower comfort windows under the active preset."""
    preset = (preset or "normal").strip().lower()
    if preset == "vacation":
        return ()  # nobody home; the firmware still runs its own legionella cycle

    p = p or TankParams()
    spec = spec or ShowerSpec()

    if preset == "guests":
        n = 3 + max(0, guest_count) + GUESTS_EXTRA_SHOWERS
        derived = required_tank_temp_for(n, p, spec)
        # A comfort requirement must NEVER be a reason to pay COP 1. If the house
        # genuinely needs more hot water than the heat pump can store below the
        # resistance cliff, that is a conversation about the cylinder — not something
        # to fix by silently burning a 3 kW immersion heater.
        return (
            ShowerComfortWindow(19.0, 22.0, min(derived, p.t_hp_max_c), "evening_guests"),
            ShowerComfortWindow(7.0, 10.0, min(derived, 45.0), "morning_guests"),
        )

    return (
        ShowerComfortWindow(
            _setting("DHW_SHOWER_EVENING_START_HOUR", DEFAULT_EVENING_START_HOUR),
            _setting("DHW_SHOWER_EVENING_END_HOUR", DEFAULT_EVENING_END_HOUR),
            _setting("DHW_SHOWER_COMFORT_C", DEFAULT_EVENING_FLOOR_C),
            "evening_showers",
        ),
        ShowerComfortWindow(
            _setting("DHW_MORNING_RESERVE_START_HOUR", DEFAULT_MORNING_START_HOUR),
            _setting("DHW_MORNING_RESERVE_END_HOUR", DEFAULT_MORNING_END_HOUR),
            _setting("DHW_MORNING_RESERVE_C", DEFAULT_MORNING_FLOOR_C),
            "morning_reserve",
        ),
    )


def comfort_floor_c(
    hour_local: float,
    *,
    preset: str,
    p: TankParams | None = None,
    spec: ShowerSpec | None = None,
    guest_count: int = 2,
) -> float | None:
    """The tank floor at a given local hour, or None when the tank is free to coast."""
    floors = [
        w.floor_c
        for w in shower_windows(preset=preset, p=p, spec=spec, guest_count=guest_count)
        if w.start_hour <= hour_local < w.end_hour
    ]
    return max(floors) if floors else None


def comfort_floors_for_slots(
    slot_starts_utc: list[datetime],
    tz: ZoneInfo,
    *,
    preset: str,
    p: TankParams | None = None,
    spec: ShowerSpec | None = None,
    guest_count: int = 2,
) -> list[float | None]:
    """Shower-comfort floors, aligned to the LP's horizon — one per window, at ENTRY.

    The floor is placed on the slot the household ENTERS the shower window on, not on
    every slot within it. This is the correction that makes the declared comfort and
    the modelled draw agree. The tank must be hot ENOUGH when the first person steps
    in; after that the showers draw it down, and — because the cylinder is stratified
    (the sensor reads the hot top, ``model.py``) — the second and third showers still
    run warm off that stored heat. A tank at the entry floor holds several showers'
    worth (the mixer arithmetic in :func:`~src.dhw.draw.required_tank_temp_for` puts
    three well inside 45 °C).

    Flooring EVERY slot of the window instead would fight the modelled draw: the
    single-node ODE drops the average temperature ~7 °C per shower slot, so a
    per-slot floor would force the heat pump to run DURING the showers to hold it up —
    exactly the "top it up while they're in there" behaviour the owner rejected, and
    exactly what a stratified tank does not actually need. Entry-only is what keeps
    the promise.

    Applied at the START of the entry slot (``tank[i]``), so the heat is already
    stored — the LP cannot satisfy it by heating into the window.
    """
    windows = shower_windows(preset=preset, p=p, spec=spec, guest_count=guest_count)

    def _floor_at(hour: float) -> float | None:
        fs = [w.floor_c for w in windows if w.start_hour <= hour < w.end_hour]
        return max(fs) if fs else None

    out: list[float | None] = []
    prev_floor: float | None = None
    for st in slot_starts_utc:
        local = st.astimezone(tz)
        hour = local.hour + local.minute / 60.0
        floor = _floor_at(hour)
        # Emit the floor only on ENTRY — the transition from outside a window (or a
        # lower one) into it. A horizon that starts mid-window still floors its first
        # slot (prev_floor is None), which is what a 20:30 re-plan needs.
        if floor is not None and (prev_floor is None or floor > prev_floor):
            out.append(floor)
        else:
            out.append(None)
        prev_floor = floor
    return out


def declared_draw_kwh_for_slots(
    slot_starts_utc: list[datetime],
    tz: ZoneInfo,
    *,
    preset: str,
    spec: ShowerSpec | None = None,
    n_evening: int = 3,
    n_morning: int = 1,
    guest_count: int = 2,
) -> list[float]:
    """Declared hot-water draw per slot (kWh thermal), spread across the comfort
    windows. DECLARED, not measured — the tank sensor cannot see an evening draw
    (the firmware reheats through it), so the demand comes from the household's own
    account of who showers when, priced by the mixer arithmetic in :mod:`src.dhw.draw`.

    Three evening showers and one occasional morning one, by default; guests add to
    the count. The energy is split evenly across the slots of each window — the LP
    does not need it slot-accurate within the hour, only in the right window with the
    right total.
    """
    from .draw import draw_kwh_thermal

    spec = spec or ShowerSpec()
    preset = (preset or "normal").strip().lower()
    if preset == "vacation":
        return [0.0] * len(slot_starts_utc)

    ev_n, mo_n = n_evening, n_morning
    if preset == "guests":
        ev_n = 3 + max(0, guest_count) + GUESTS_EXTRA_SHOWERS
        mo_n = 2

    windows = shower_windows(preset=preset, guest_count=guest_count)
    # Map each window label to its shower count and its member slots.
    ev_total = draw_kwh_thermal(ev_n, spec)
    mo_total = draw_kwh_thermal(mo_n, spec)

    out = [0.0] * len(slot_starts_utc)
    for w, total in ((_evening_window(windows), ev_total), (_morning_window(windows), mo_total)):
        if w is None or total <= 0:
            continue
        members = [
            i for i, st in enumerate(slot_starts_utc)
            if _in_window(st.astimezone(tz), w)
        ]
        if not members:
            continue
        per = total / len(members)
        for i in members:
            out[i] = per
    return out


def _evening_window(windows: tuple[ShowerComfortWindow, ...]) -> ShowerComfortWindow | None:
    return next((w for w in windows if w.label.startswith("evening")), None)


def _morning_window(windows: tuple[ShowerComfortWindow, ...]) -> ShowerComfortWindow | None:
    return next((w for w in windows if w.label.startswith("morning")), None)


def _in_window(local: datetime, w: ShowerComfortWindow) -> bool:
    h = local.hour + local.minute / 60.0
    return w.start_hour <= h < w.end_hour


def backstop_floor_c(preset: str) -> float | None:
    """The floor DISPATCH enforces regardless of what the LP planned.

    A soft floor in the solver protects against an LP that is *pessimistic* about the
    tank. It does nothing against one that is *optimistic*: if a future calibration
    bug claims the tank coasts at 0.05 °C/h, the LP will heat in the morning, believe
    the tank is still hot at 20:00, and be wrong — with no slack, no infeasibility,
    and three cold showers. The solve would look perfect.

    So dispatch writes a tank target over the shower window unconditionally, from the
    DECLARED comfort temperature, reading nothing learned. If the tank really did
    coast well, the firmware does nothing (it is already above target) and the row
    costs nothing. If the model was wrong, the firmware repairs it.

    It is also the regime's health alarm: a backstop that actually fires means the
    physics is wrong, and two days running means the LP should be switched off.
    """
    if (preset or "normal").strip().lower() == "vacation":
        return None
    return _setting("DHW_SHOWER_COMFORT_C", DEFAULT_EVENING_FLOOR_C)
