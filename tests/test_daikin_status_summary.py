"""Daikin status semantics for LLM consumers (OpenClaw etc.).

Regression: 2026-05-03 — OpenClaw reported "Daikin is off" because it read
the legacy `is_on` field which only reflects the climate zone. With outdoor
~19 °C the climate zone is idle (`is_on=true, room_temp=null`) while DHW is
actively maintaining the tank — the unit is *not* off. These tests pin the
enriched response shape so the misread cannot recur:

  1. _daikin_state_summary builds an unambiguous one-liner from the typed
     fields (no field is silently dropped).
  2. _device_status_dict surfaces climate_on / dhw_on / control_mode /
     state_summary so an agent never has to guess.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.daikin.models import DaikinDevice, DaikinStatus
from src.mcp_server import _daikin_state_summary, _device_status_dict


def _status(**overrides) -> DaikinStatus:
    base = dict(
        device_name="dev",
        is_on=True,
        mode="heating",
        room_temp=None,
        target_temp=None,
        outdoor_temp=19.0,
        lwt=22.0,
        lwt_offset=-1.0,
        tank_temp=41.0,
        tank_target=40.0,
        weather_regulation=True,
        climate_on=True,
        dhw_on=True,
    )
    base.update(overrides)
    return DaikinStatus(**base)


# ---------------------------------------------------------------------------
# state_summary covers every cell of the truth table the LLM might face
# ---------------------------------------------------------------------------

def test_summary_dhw_maintaining_climate_weather_regulated() -> None:
    s = _status()  # exact prod snapshot: tank near target, climate idle, WR on
    out = _daikin_state_summary(s, "passive")
    assert "DHW maintaining" in out
    assert "climate weather-regulated" in out
    assert "hem-control=passive" in out
    assert "off" not in out.lower(), f"must not contain misleading 'off': {out!r}"


def test_summary_dhw_actively_heating() -> None:
    out = _daikin_state_summary(_status(tank_temp=35.0, tank_target=45.0), "passive")
    assert "DHW heating (tank 35→45°C)" in out


def test_summary_dhw_zone_explicitly_off() -> None:
    out = _daikin_state_summary(_status(dhw_on=False), "passive")
    assert "DHW zone OFF" in out


def test_summary_climate_zone_explicitly_off() -> None:
    out = _daikin_state_summary(
        _status(climate_on=False, weather_regulation=False, room_temp=None, target_temp=None),
        "passive",
    )
    assert "climate OFF" in out


def test_summary_climate_room_setpoint_satisfied() -> None:
    out = _daikin_state_summary(
        _status(weather_regulation=False, room_temp=21.5, target_temp=21.0),
        "active",
    )
    assert "climate satisfied" in out
    assert "21.5/21.0" in out
    assert "hem-control=active" in out


def test_summary_climate_actively_heating() -> None:
    out = _daikin_state_summary(
        _status(weather_regulation=False, room_temp=18.0, target_temp=21.0),
        "passive",
    )
    assert "climate heating" in out
    assert "18.0/21.0" in out


def test_summary_unknowns_do_not_say_off() -> None:
    """The pre-fix bug: null fields read as 'off'. Now they read as 'unknown' / 'idle'."""
    s = _status(
        tank_temp=None, tank_target=None,
        room_temp=None, target_temp=None,
        weather_regulation=False,
        climate_on=None, dhw_on=None,
    )
    out = _daikin_state_summary(s, "passive")
    assert "DHW state unknown" in out
    assert "climate idle" in out
    assert "OFF" not in out, f"unknown != off: {out!r}"


# ---------------------------------------------------------------------------
# _device_status_dict shape — what OpenClaw actually receives
# ---------------------------------------------------------------------------

def test_device_status_dict_includes_unambiguous_fields() -> None:
    """Pin the keys an LLM consumer can rely on."""
    s = _status()
    client = MagicMock()
    client.get_status.return_value = s
    dev = DaikinDevice(id="gw1", name="x", model="Altherma")

    out = _device_status_dict(client, dev)
    # Legacy field stays for back-compat
    assert "is_on" in out
    # New unambiguous fields
    for key in ("climate_on", "dhw_on", "control_mode", "state_summary"):
        assert key in out, f"missing {key!r} in {sorted(out)}"
    # state_summary is a non-empty string
    assert isinstance(out["state_summary"], str) and out["state_summary"].strip()


def test_device_status_dict_state_summary_matches_helper() -> None:
    """The dict's state_summary is byte-identical to the helper output."""
    from src.config import config
    s = _status()
    client = MagicMock()
    client.get_status.return_value = s
    dev = DaikinDevice(id="gw1", name="x", model="Altherma")

    out = _device_status_dict(client, dev)
    assert out["state_summary"] == _daikin_state_summary(s, config.DAIKIN_CONTROL_MODE)
