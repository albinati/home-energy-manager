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
