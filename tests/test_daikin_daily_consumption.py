"""DaikinClient.get_daily_consumption_from_cache — parse 'w' array (S10.12 / #178).

Daikin Onecta layout: ``consumptionData.value.electrical.<mode>.w`` is a
14-element list mapping array indices → calendar dates as:
  arr[7]   = this week's Monday
  arr[7+i] = this week's Monday + i days  (0 ≤ i ≤ 6)
  arr[i]   = last week's Monday + i days  (0 ≤ i ≤ 6)
"""
from __future__ import annotations

from datetime import date

import pytest

from src.daikin.client import DaikinClient


class _FakeDevice:
    def __init__(self, mp_payloads: list[dict]) -> None:
        self.id = "test-device"
        self.raw = {"managementPoints": mp_payloads}


def _build_client_with_devices(devices: list[_FakeDevice]) -> DaikinClient:
    """Construct a DaikinClient and stub get_devices() with the supplied list."""
    c = DaikinClient.__new__(DaikinClient)  # bypass __init__
    c.get_devices = lambda **_: devices  # type: ignore[method-assign]
    return c


def _w_array(values_by_idx: dict[int, float]) -> list[float | None]:
    arr: list[float | None] = [None] * 14
    for idx, v in values_by_idx.items():
        arr[idx] = v
    return arr


def _consumption_data(*, heating_w: list, dhw_w: list | None = None) -> dict:
    return {
        "managementPointType": "climateControl",
        "consumptionData": {
            "value": {"electrical": {"heating": {"w": heating_w}}}
        },
    }


def test_parse_today_monday_yields_today_in_idx_7() -> None:
    """When 'today' is a Monday, arr[7] maps to today's date."""
    today = date(2026, 4, 27)  # this is actually a Monday
    assert today.weekday() == 0
    heating_w = _w_array({7: 5.5})  # 5.5 kWh today
    devices = [_FakeDevice([_consumption_data(heating_w=heating_w)])]
    client = _build_client_with_devices(devices)

    out = client.get_daily_consumption_from_cache(today_utc=today)
    assert "2026-04-27" in out
    assert out["2026-04-27"]["heating_kwh"] == pytest.approx(5.5)
    assert out["2026-04-27"]["dhw_kwh"] == 0.0
    assert out["2026-04-27"]["total_kwh"] == pytest.approx(5.5)


def test_idx_0_maps_to_last_week_monday() -> None:
    today = date(2026, 4, 27)  # Monday → this week's Monday is 2026-04-27
    heating_w = _w_array({0: 12.0})  # 12 kWh on last week's Monday → 2026-04-20
    devices = [_FakeDevice([_consumption_data(heating_w=heating_w)])]
    client = _build_client_with_devices(devices)

    out = client.get_daily_consumption_from_cache(today_utc=today)
    assert "2026-04-20" in out
    assert out["2026-04-20"]["heating_kwh"] == pytest.approx(12.0)


def test_dhw_management_point_routes_to_dhw_bucket() -> None:
    today = date(2026, 4, 27)
    dhw_mp = {
        "managementPointType": "domesticHotWaterTank",
        "consumptionData": {
            "value": {"electrical": {"heating": {"w": _w_array({7: 2.5})}}}
        },
    }
    space_mp = _consumption_data(heating_w=_w_array({7: 4.0}))
    devices = [_FakeDevice([dhw_mp, space_mp])]
    client = _build_client_with_devices(devices)

    out = client.get_daily_consumption_from_cache(today_utc=today)
    today_b = out["2026-04-27"]
    assert today_b["heating_kwh"] == pytest.approx(4.0)
    assert today_b["dhw_kwh"] == pytest.approx(2.5)
    assert today_b["total_kwh"] == pytest.approx(6.5)


def test_none_values_in_array_are_skipped() -> None:
    today = date(2026, 4, 27)
    # Future days are None; only idx 0 (last Monday) and 7 (today) populated
    heating_w = _w_array({0: 8.0, 7: 3.0})
    devices = [_FakeDevice([_consumption_data(heating_w=heating_w)])]
    client = _build_client_with_devices(devices)

    out = client.get_daily_consumption_from_cache(today_utc=today)
    assert set(out.keys()) == {"2026-04-20", "2026-04-27"}
    # No noise from the None entries (e.g. 2026-04-21 should NOT be present)
    assert "2026-04-21" not in out


def test_handles_missing_consumption_data_gracefully() -> None:
    """Management points without consumptionData are silently skipped."""
    today = date(2026, 4, 27)
    no_cd_mp = {"managementPointType": "climateControl"}  # no consumptionData
    devices = [_FakeDevice([no_cd_mp])]
    client = _build_client_with_devices(devices)
    out = client.get_daily_consumption_from_cache(today_utc=today)
    assert out == {}
