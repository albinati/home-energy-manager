"""Comfort is declared, not learned — and these tests pin the reason.

The failure this module exists to prevent is specific and it already happened once:
an estimator concluded this household showered in the MORNING (it was structurally
blind to evening draws, because the firmware reheats during a shower and the Onecta
counter truncates that reheat to zero), and the LP was about to be fed that. Three
people shower between 20:00 and 21:00. Had it shipped, the children would have had
cold showers every day, and the system would have reported success.

So: the floors come from the household. A calibration bug may cause the LP to FAIL a
floor — visibly, as slack — but may never MOVE one.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.dhw.comfort import (
    backstop_floor_c,
    comfort_floor_c,
    comfort_floors_for_slots,
)
from src.dhw.draw import ShowerSpec, draw_kwh_thermal, required_tank_temp_for
from src.dhw.model import TankParams

P = TankParams()
SPEC = ShowerSpec()
LONDON = ZoneInfo("Europe/London")


# ---------------------------------------------------------------------------
# The household's actual schedule
# ---------------------------------------------------------------------------


def test_the_floor_is_at_the_showers_and_the_showers_are_in_the_evening():
    """20:00-21:00, three people, 45 °C. The owner's words, encoded."""
    assert comfort_floor_c(20.5, preset="normal") == 45.0
    assert comfort_floor_c(20.0, preset="normal") == 45.0

    # ...and NOT in the middle of the day, which is where the fixed schedule used
    # to force a 45 °C tank whether anyone wanted hot water or not.
    assert comfort_floor_c(14.0, preset="normal") is None
    assert comfort_floor_c(17.0, preset="normal") is None


def test_the_morning_reserve_is_modest_on_purpose():
    """One occasional shower — his, not the family's. Holding the tank hot all night
    to serve a shower that may not happen is exactly the standing-loss waste this
    rewrite exists to stop. 40 °C still delivers 38 °C at the mixer."""
    assert comfort_floor_c(8.0, preset="normal") == 40.0
    assert comfort_floor_c(8.0, preset="normal") < comfort_floor_c(20.5, preset="normal")


def test_the_mixer_arithmetic_agrees_with_the_owner():
    """The owner says 45 °C covers four-plus people. That is lived experience and it
    outranks any model — but it is worth knowing the model does not contradict him."""
    for n in (3, 4):
        assert required_tank_temp_for(n, P, SPEC) <= 45.0
    # Three 5-minute showers is a real but modest draw.
    assert draw_kwh_thermal(3, SPEC) == pytest.approx(3.4, abs=0.2)


def test_vacation_has_no_floors():
    """Nobody home. The firmware still runs its own legionella cycle."""
    assert comfort_floor_c(20.5, preset="vacation") is None
    assert comfort_floor_c(8.0, preset="vacation") is None
    assert backstop_floor_c("vacation") is None


def test_guests_raise_the_floor_but_never_into_the_resistance_heater():
    """Guests are the ONE place we fall back to the mixer arithmetic — there is no
    lived-experience number for an unknown house-full.

    But a comfort requirement must never be a reason to pay COP 1. If the house
    genuinely needs more hot water than the heat pump can store below 50 °C, that is
    a conversation about the cylinder — not something to fix by silently burning a
    3 kW immersion heater."""
    floor = comfort_floor_c(20.5, preset="guests", guest_count=6)
    assert floor is not None
    assert floor > 45.0                 # guests really do need a hotter tank
    assert floor <= P.t_hp_max_c        # ...but never past the cliff


# ---------------------------------------------------------------------------
# How it reaches the LP
# ---------------------------------------------------------------------------


def test_floors_land_on_the_right_slots_in_local_time():
    """The LP's horizon is UTC; the household lives in local time. In BST the 20:00
    shower is a 19:00 UTC slot — get this wrong and the floor lands an hour off, in
    the direction that gives everyone a cold shower."""
    starts = [
        datetime(2026, 7, 8, 17, 0, tzinfo=UTC) + i * timedelta(minutes=30)
        for i in range(8)  # 17:00-21:00 UTC = 18:00-22:00 BST
    ]
    floors = comfort_floors_for_slots(starts, LONDON, preset="normal")

    hot = [st for st, f in zip(starts, floors, strict=True) if f == 45.0]
    assert hot, "the evening floor must land somewhere"
    for st in hot:
        assert st.astimezone(LONDON).hour == 20

    # The same wall-clock shower is a DIFFERENT UTC slot in winter: with no BST
    # offset, 20:00 local is 20:00 UTC. The 19:00 UTC slot that carried the floor in
    # July carries nothing in January — and that is the whole reason this is
    # computed from local time rather than hard-coded against the horizon.
    winter = [
        datetime(2026, 1, 8, 19, 0, tzinfo=UTC) + i * timedelta(minutes=30)
        for i in range(6)  # 19:00-21:30 UTC == 19:00-21:30 GMT
    ]
    wf = comfort_floors_for_slots(winter, LONDON, preset="normal")
    assert wf[0] is None   # 19:00 GMT — before the showers
    assert wf[2] == 45.0   # 20:00 GMT — the showers
    assert wf[4] is None   # 21:00 GMT — done


def test_shower_comfort_is_a_dial_the_family_turns(monkeypatch):
    """Shower comfort has no instrument and never will — the only sensor is whether
    the family felt the water was warm enough. So it is DECLARED, and it is meant to
    be tuned by hand as they learn what they actually need. Turning the evening floor
    down is the cheapest saving in the system, and the LP simply hits whatever number
    it is given.

    This is the opposite of HOUSE comfort, which has real instruments (the indoor
    sensors) and will take their input. The two must never be conflated — that is how
    you get a tank held at 50 °C because a hallway sensor read cold."""
    from src.dhw import comfort as c

    settings = {"DHW_SHOWER_COMFORT_C": 43.0, "DHW_SHOWER_EVENING_START_HOUR": 19.0}
    monkeypatch.setattr(c, "_setting", lambda k, d: settings.get(k, d))

    assert c.comfort_floor_c(19.5, preset="normal") == 43.0  # window moved AND cooled
    assert c.backstop_floor_c("normal") == 43.0              # the backstop follows it
    assert c.comfort_floor_c(18.0, preset="normal") is None


def test_a_broken_settings_store_never_cools_the_showers(monkeypatch):
    """Comfort must survive a settings outage by using the household's stated number
    — never by failing, and never by quietly guessing lower."""
    from src.dhw import comfort as c

    def _boom(key):
        raise RuntimeError("settings table is gone")

    monkeypatch.setattr("src.runtime_settings.get_setting", _boom)
    assert c.comfort_floor_c(20.5, preset="normal") == c.DEFAULT_EVENING_FLOOR_C


def test_the_floor_is_at_window_ENTRY_only_not_every_slot():
    """The correction that makes comfort and the modelled draw agree. The tank must be
    hot when the household ENTERS the shower window; after that the showers draw it
    down and — the cylinder being stratified — still run warm off the stored heat.

    Flooring every slot instead would force the heat pump to run DURING the showers to
    hold the average temperature up (the single-node ODE drops it ~7 °C per shower
    slot) — the 'top it up while they're in there' behaviour the owner rejected. So
    the floor lands on the ENTRY slot and nowhere else inside the window."""
    # 18:00-22:00 BST (17:00-21:00 UTC) — the 20:00 window is two slots (20:00, 20:30).
    starts = [
        datetime(2026, 7, 8, 17, 0, tzinfo=UTC) + i * timedelta(minutes=30)
        for i in range(8)
    ]
    floors = comfort_floors_for_slots(starts, LONDON, preset="normal")
    hot_slots = [(st.astimezone(LONDON).strftime("%H:%M"), f)
                 for st, f in zip(starts, floors, strict=True) if f is not None]
    assert hot_slots == [("20:00", 45.0)], hot_slots  # ONLY the entry slot


def test_a_mid_window_replan_still_floors_its_first_slot():
    """A re-plan at 20:30 starts the horizon inside the window. There is no 'entry'
    transition to see, so the first slot must carry the floor anyway — otherwise a
    re-plan mid-shower would drop comfort entirely."""
    starts = [
        datetime(2026, 7, 8, 19, 30, tzinfo=UTC) + i * timedelta(minutes=30)  # 20:30 BST
        for i in range(3)
    ]
    floors = comfort_floors_for_slots(starts, LONDON, preset="normal")
    assert floors[0] == 45.0  # 20:30 local, mid-window, still floored


def test_the_backstop_reads_nothing_learned():
    """The floor that survives a calibration bug.

    A soft floor in the solver protects against a PESSIMISTIC LP. It does nothing
    against an optimistic one: if a future fit claims the tank coasts at 0.05 °C/h,
    the LP heats in the morning, believes the tank is still hot at 20:00, and is
    wrong — with no slack, no infeasibility, and three cold showers. So dispatch
    writes a 45 °C target over the shower window unconditionally.

    Cheap when the model is right (the tank is already above target, the firmware
    does nothing), and self-repairing when it isn't."""
    assert backstop_floor_c("normal") == 45.0
    assert backstop_floor_c("guests") == 45.0
