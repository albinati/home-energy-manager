"""Daikin bulletproof apply: payload pruning (#18) and valve settle timing."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.daikin.models import DaikinDevice, SetpointRange
from src.daikin_bulletproof import apply_scheduled_daikin_params, daikin_device_matches_params


@pytest.fixture
def operational(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.daikin_bulletproof.config.OPERATION_MODE", "operational")
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    # v10: passive mode short-circuits apply_scheduled_daikin_params. These tests
    # exercise active-mode write behaviour, so flip the flag explicitly.
    monkeypatch.setenv("DAIKIN_CONTROL_MODE", "active")
    from src.runtime_settings import clear_cache
    clear_cache()


@pytest.fixture
def no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)


def test_prune_drops_lwt_offset_when_climate_off_no_set_lwt(
    operational: None,
    no_settle: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Onecta rejects leavingWaterOffset when zone is off — do not call set_lwt_offset (#18)."""
    monkeypatch.setattr("src.daikin_bulletproof.db.log_action", MagicMock())
    dev = DaikinDevice(id="gw", name="x", is_on=False, lwt_offset=-3.0)
    dev.lwt_offset_range = SetpointRange(settable=True)
    client = MagicMock()
    apply_scheduled_daikin_params(
        dev,
        client,
        {
            "lwt_offset": -5.0,
            "climate_on": False,
            "tank_power": False,
            "tank_temp": 50.0,
        },
        trigger="test",
        skip_if_matches=False,
    )
    client.set_lwt_offset.assert_not_called()
    client.set_power.assert_called_once_with(dev, False)


def test_device_matches_ignores_lwt_when_climate_commanded_off() -> None:
    dev = DaikinDevice(id="gw", name="x", is_on=False, lwt_offset=0.0)
    assert daikin_device_matches_params(
        dev,
        {"lwt_offset": -9.0, "climate_on": False},
    )


def test_valve_settle_after_climate_off_before_dhw(
    operational: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.daikin_bulletproof.config.DAIKIN_VALVE_SETTLE_SECONDS", 10)
    monkeypatch.setattr("src.daikin_bulletproof.db.log_action", MagicMock())
    sleeps: list[float] = []

    def capture_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("src.daikin_bulletproof.time.sleep", capture_sleep)

    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=0.0)
    dev.lwt_offset_range = SetpointRange(settable=True)
    client = MagicMock()
    apply_scheduled_daikin_params(
        dev,
        client,
        {
            "climate_on": False,
            "tank_power": True,
            "tank_temp": 48.0,
        },
        trigger="test",
        skip_if_matches=False,
    )
    assert 10.0 in sleeps


# ── Phase 4.2: tank_power / tank_powerful dedup (#41) ─────────────────────────

def test_tank_power_matches_when_already_on() -> None:
    """No write needed when device already reports tank_on=True."""
    dev = DaikinDevice(id="gw", name="x", tank_on=True)
    assert daikin_device_matches_params(dev, {"tank_power": True}) is True


def test_tank_power_mismatch_when_currently_off() -> None:
    """Write needed when commanded on but live state is off."""
    dev = DaikinDevice(id="gw", name="x", tank_on=False)
    assert daikin_device_matches_params(dev, {"tank_power": True}) is False


def test_tank_power_unknown_falls_back_to_write() -> None:
    """Conservative: if device live value is unknown (None), trigger a write."""
    dev = DaikinDevice(id="gw", name="x", tank_on=None)
    assert daikin_device_matches_params(dev, {"tank_power": True}) is False


def test_tank_powerful_matches_when_already_on() -> None:
    dev = DaikinDevice(id="gw", name="x", tank_powerful=True)
    assert daikin_device_matches_params(dev, {"tank_powerful": True}) is True


def test_tank_powerful_unknown_falls_back_to_write() -> None:
    dev = DaikinDevice(id="gw", name="x", tank_powerful=None)
    assert daikin_device_matches_params(dev, {"tank_powerful": True}) is False


def test_parse_device_populates_tank_power_and_powerful() -> None:
    """_parse_device reads onOffMode and powerfulMode from DHW management point."""
    from src.daikin.client import DaikinClient

    raw = {
        "id": "gw-1",
        "embeddedId": "gw",
        "deviceModel": "Altherma",
        "managementPoints": [
            {
                "embeddedId": "domesticHotWaterTank",
                "managementPointType": "domesticHotWaterTank",
                "onOffMode": {"value": "on"},
                "powerfulMode": {"value": "off"},
                "sensoryData": {"value": {"tankTemperature": {"value": 45.0}}},
                "temperatureControl": {
                    "value": {
                        "operationModes": {
                            "heating": {
                                "setpoints": {
                                    "domesticHotWaterTemperature": {"value": 50.0}
                                }
                            }
                        }
                    }
                },
            }
        ],
    }
    client = DaikinClient()
    dev = client._parse_device("gw-1", "gw", raw["managementPoints"], raw)
    assert dev is not None
    assert dev.tank_on is True
    assert dev.tank_powerful is False


def test_parse_device_tank_fields_none_when_absent() -> None:
    """When the DHW management point omits on/off state, tank_on / tank_powerful stay None."""
    from src.daikin.client import DaikinClient

    raw = {
        "id": "gw-1",
        "embeddedId": "gw",
        "deviceModel": "Altherma",
        "managementPoints": [
            {
                "embeddedId": "domesticHotWaterTank",
                "managementPointType": "domesticHotWaterTank",
                "sensoryData": {"value": {"tankTemperature": {"value": 45.0}}},
            }
        ],
    }
    client = DaikinClient()
    dev = client._parse_device("gw-1", "gw", raw["managementPoints"], raw)
    assert dev is not None
    assert dev.tank_on is None
    assert dev.tank_powerful is None
