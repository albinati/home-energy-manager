"""Deadband-aware warmup escalation (#735, follow-up to #732).

The firmware only reheats when tank ≤ target − differential (~6-7 °C measured),
so a warm-tank day turns the commanded warmup into a silent no-op. Measured
2026-07-17: commanded 47, tank 42 — nothing happened, showers at ~40.5 °C
against the family's declared 45. The declared dial is the spec: force the
lift ONLY when the coast projection misses the next shower window's floor.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.config import config
from src.state_machine import _warmup_deadband_force_reason


@pytest.fixture(autouse=True)
def _fixed_env(monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "normal")
    # Pin the measured physics so projections are deterministic.
    monkeypatch.setattr(config, "DHW_REHEAT_DIFFERENTIAL_FALLBACK_C", 6.0, raising=False)
    yield


def _dev(tank: float, target: float | None = None):
    return SimpleNamespace(tank_temperature=tank, tank_target=target)


# 2026-07-17 12:05 UTC = 13:05 BST — the real incident's warmup fire time.
_FIRE = datetime(2026, 7, 17, 12, 5, tzinfo=UTC)


def test_incident_case_lifts_the_heat_pump_not_powerful():
    """Tank 42, target 47 (inside the deadband) and ~7 h coast to the 20:00
    window → projected < 45. 47 is below the 50 °C cliff, so the heat pump can
    do the lift: command the cliff, NOT Powerful (#737)."""
    r = _warmup_deadband_force_reason(_dev(42.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["window"] == "evening_showers"
    assert r["projected_c"] < r["floor_c"]
    assert r["mechanism"] == "hp_target_lift"
    # Command the cliff — stable across the heat-up; clears the deadband from
    # tank 42 (Δ = 50 − 42 = 8 > differential) and stays sub-resistance.
    assert r["lift_target_c"] == int(r["cliff_c"])
    assert r["lift_target_c"] - r["tank_c"] > r["differential_c"]


def test_powerful_is_the_fallback_only_when_tank_cannot_clear_sub_cliff():
    """Tank 44: cliff − tank (6) ≤ differential — no sub-cliff target can
    trigger the heat pump, so resistance (Powerful) is the only way to add
    heat, and the missed floor demands it."""
    r = _warmup_deadband_force_reason(_dev(44.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["mechanism"] == "powerful"
    assert r["lift_target_c"] is None


def test_mechanism_stays_hp_mid_lift_when_tank_warms_into_the_powerful_band():
    """Review #737 finding 3: once the device is lifted to the cliff, a tick
    where the tank has warmed into the would-be-Powerful band (44) must NOT
    flip to resistance — the heat pump is already doing the lift. ``already_
    lifted`` (device target at the cliff) keeps it on the HP."""
    r = _warmup_deadband_force_reason(
        _dev(44.0, target=50.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["already_lifted"] is True
    assert r["mechanism"] == "hp_target_lift"
    assert r["lift_target_c"] == 50


def test_not_yet_lifted_tank_in_powerful_band_still_falls_back():
    """The same tank 44 but device NOT yet lifted (target 47) is the genuine
    fallback corner — resistance, since the HP can't be triggered sub-cliff."""
    r = _warmup_deadband_force_reason(
        _dev(44.0, target=47.0), {"tank_temp": 47, "tank_power": True}, _FIRE)
    assert r is not None
    assert r["already_lifted"] is False
    assert r["mechanism"] == "powerful"


def test_firmware_will_heat_unaided_no_force():
    """Δ9 is beyond the deadband — the plain command heats; no Powerful."""
    assert _warmup_deadband_force_reason(
        _dev(38.0), {"tank_temp": 47, "tank_power": True}, _FIRE) is None


def test_coast_clearing_the_floor_keeps_the_free_skip():
    """Tank 46 an hour before the window: inside the deadband, but the short
    coast stays above the 45 floor — the firmware skip is deliberate and
    cheaper. (At 13:00 the same 46 °C would NOT clear: τ=95 h drops it to
    ~44.3 by 20:00, which is exactly why the V3 schedule heats to 47.)"""
    late_fire = datetime(2026, 7, 17, 18, 5, tzinfo=UTC)  # 19:05 BST
    assert _warmup_deadband_force_reason(
        _dev(46.0), {"tank_temp": 47, "tank_power": True}, late_fire) is None


def test_already_at_target_no_force():
    assert _warmup_deadband_force_reason(
        _dev(47.5), {"tank_temp": 47, "tank_power": True}, _FIRE) is None


def test_missing_telemetry_fails_open_to_plain_command():
    assert _warmup_deadband_force_reason(
        _dev(None), {"tank_temp": 47, "tank_power": True}, _FIRE) is None
    assert _warmup_deadband_force_reason(
        SimpleNamespace(), {"tank_power": True}, _FIRE) is None


def test_vacation_preset_never_forces(monkeypatch):
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "vacation")
    assert _warmup_deadband_force_reason(
        _dev(42.0), {"tank_temp": 47, "tank_power": True}, _FIRE) is None


def test_early_fire_judges_every_window_not_just_the_soonest():
    """Review case: a cheap-night warmup fires at 04:05 BST. The soonest window
    is the morning reserve (floor 40) which a warm tank clears — but the
    EVENING 45 floor, 16 h out, does not survive the coast. Must force."""
    early = datetime(2026, 7, 17, 3, 5, tzinfo=UTC)
    r = _warmup_deadband_force_reason(_dev(44.5), {"tank_temp": 47, "tank_power": True}, early)
    assert r is not None
    assert r["window"] == "evening_showers"


def test_mid_window_fire_is_judged_against_the_current_floor():
    """Review case: a backstop row firing INSIDE the shower window used to be
    scored against tomorrow. The floor is owed NOW (hours = 0)."""
    mid = datetime(2026, 7, 17, 19, 30, tzinfo=UTC)  # 20:30 BST, inside 20-21h
    r = _warmup_deadband_force_reason(_dev(43.0), {"tank_temp": 45, "tank_power": True}, mid)
    assert r is not None
    assert r["window"] == "evening_showers"
    assert r["hours_to_window"] == 0.0
    assert r["projected_c"] == pytest.approx(43.0)
