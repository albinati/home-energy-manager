"""DaikinClient.get_daily_consumption_from_cache — management-point routing.

Symmetry guard for the weekly ('w') array path, mirroring the routing test that
already exists for the 2-hourly ('d') path. The 2026-06-15 telemetry audit
found Daikin CLEAN of the Fox generation-vs-PVEnergyTotal class precisely
because consumption is routed by management-point TYPE (domesticHotWaterTank →
DHW bucket; everything else → space-heating bucket), not by the electrical mode
field. These tests pin that contract so a future refactor can't silently swap
the buckets (which would mis-attribute heat-pump energy in the brief / LP
without changing any total).

Daikin Onecta 'w' layout: 14 elements — indices 0–6 = last week (Mon→Sun),
7–13 = this week (Mon→Sun); arr[7] = this Monday.
"""
from __future__ import annotations

from datetime import date

from src.daikin.client import DaikinClient


class _FakeDevice:
    def __init__(self, mp_payloads: list[dict]) -> None:
        self.id = "test-device"
        self.raw = {"managementPoints": mp_payloads}


def _build_client(devices: list[_FakeDevice]) -> DaikinClient:
    c = DaikinClient.__new__(DaikinClient)
    c.get_devices = lambda **_: devices  # type: ignore[method-assign]
    return c


def _w_array(values_by_idx: dict[int, float]) -> list[float | None]:
    arr: list[float | None] = [None] * 14
    for idx, v in values_by_idx.items():
        arr[idx] = v
    return arr


def _mp(*, w: list, mp_type: str, mode: str = "heating") -> dict:
    return {
        "managementPointType": mp_type,
        "consumptionData": {"value": {"electrical": {mode: {"w": w}}}},
    }


def test_dhw_point_routes_to_dhw_bucket_heating_point_to_heating() -> None:
    today = date(2026, 5, 6)  # a Wednesday → this Monday = 2026-05-04 (arr[7])
    dhw = _mp(w=_w_array({7: 0.4}), mp_type="domesticHotWaterTank")
    space = _mp(w=_w_array({7: 0.6}), mp_type="climateControl")
    out = _build_client([_FakeDevice([dhw, space])]).get_daily_consumption_from_cache(today_utc=today)

    monday = out["2026-05-04"]
    assert monday["dhw_kwh"] == 0.4
    assert monday["heating_kwh"] == 0.6
    assert monday["total_kwh"] == 1.0


def test_heatpump_point_counts_as_heating_not_dhw() -> None:
    today = date(2026, 5, 6)
    hp = _mp(w=_w_array({7: 1.1}), mp_type="heatPump")
    out = _build_client([_FakeDevice([hp])]).get_daily_consumption_from_cache(today_utc=today)
    assert out["2026-05-04"]["heating_kwh"] == 1.1
    assert out["2026-05-04"]["dhw_kwh"] == 0.0


def test_cooling_mode_sums_into_same_bucket_as_heating() -> None:
    # Both electrical 'heating' and 'cooling' modes are electrical draw for the
    # same point — they must accumulate together, not split or cancel.
    today = date(2026, 5, 6)
    mp = {
        "managementPointType": "climateControl",
        "consumptionData": {"value": {"electrical": {
            "heating": {"w": _w_array({7: 0.5})},
            "cooling": {"w": _w_array({7: 0.2})},
        }}},
    }
    out = _build_client([_FakeDevice([mp])]).get_daily_consumption_from_cache(today_utc=today)
    assert out["2026-05-04"]["heating_kwh"] == 0.7


def test_last_week_indices_map_to_prior_week() -> None:
    today = date(2026, 5, 6)  # this Monday = 2026-05-04; last Monday = 2026-04-27
    mp = _mp(w=_w_array({0: 0.9}), mp_type="climateControl")  # arr[0] = last Monday
    out = _build_client([_FakeDevice([mp])]).get_daily_consumption_from_cache(today_utc=today)
    assert out["2026-04-27"]["heating_kwh"] == 0.9
