"""DaikinClient.get_2hourly_consumption_from_cache — parse 'd' array (#238).

Daikin Onecta layout: ``consumptionData.value.electrical.<mode>.d`` is a
24-element list mapping array indices → 2-hour buckets as:
  arr[0..11]  = yesterday's 12 × 2-hour buckets (00:00–02:00 ... 22:00–24:00)
  arr[12..23] = today's 12 × 2-hour buckets (same layout)

Confirmed empirically — the Onecta app's "DAY" view labels the resolution as
"2-hourly average" and renders 12 bars across a 24-hour x-axis.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src import db
from src.config import config as app_config
from src.daikin.client import DaikinClient


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


class _FakeDevice:
    def __init__(self, mp_payloads: list[dict]) -> None:
        self.id = "test-device"
        self.raw = {"managementPoints": mp_payloads}


def _build_client_with_devices(devices: list[_FakeDevice]) -> DaikinClient:
    c = DaikinClient.__new__(DaikinClient)
    c.get_devices = lambda **_: devices  # type: ignore[method-assign]
    return c


def _d_array(values_by_idx: dict[int, float]) -> list[float | None]:
    arr: list[float | None] = [None] * 24
    for idx, v in values_by_idx.items():
        arr[idx] = v
    return arr


def _consumption_data(*, heating_d: list, mp_type: str = "climateControl") -> dict:
    return {
        "managementPointType": mp_type,
        "consumptionData": {"value": {"electrical": {"heating": {"d": heating_d}}}},
    }


def test_idx_0_to_11_maps_to_yesterday() -> None:
    today = date(2026, 5, 2)
    # idx 0 = yesterday 00:00–02:00, idx 11 = yesterday 22:00–24:00
    heating_d = _d_array({0: 0.5, 11: 1.2})
    devices = [_FakeDevice([_consumption_data(heating_d=heating_d)])]
    client = _build_client_with_devices(devices)

    out = client.get_2hourly_consumption_from_cache(today_local=today)
    assert "2026-05-01" in out  # yesterday
    yest = out["2026-05-01"]
    assert yest[0]["heating_kwh"] == pytest.approx(0.5)
    assert yest[0]["total_kwh"] == pytest.approx(0.5)
    assert yest[11]["heating_kwh"] == pytest.approx(1.2)


def test_idx_12_to_23_maps_to_today() -> None:
    today = date(2026, 5, 2)
    # idx 12 = today 00:00–02:00, idx 23 = today 22:00–24:00
    heating_d = _d_array({12: 0.3, 14: 0.8})  # 00–02 and 04–06
    devices = [_FakeDevice([_consumption_data(heating_d=heating_d)])]
    client = _build_client_with_devices(devices)

    out = client.get_2hourly_consumption_from_cache(today_local=today)
    assert "2026-05-02" in out
    today_b = out["2026-05-02"]
    assert today_b[0]["heating_kwh"] == pytest.approx(0.3)
    assert today_b[2]["heating_kwh"] == pytest.approx(0.8)


def test_dhw_management_point_routes_to_dhw_bucket() -> None:
    today = date(2026, 5, 2)
    dhw_mp = _consumption_data(heating_d=_d_array({12: 0.4}), mp_type="domesticHotWaterTank")
    space_mp = _consumption_data(heating_d=_d_array({12: 0.6}))
    devices = [_FakeDevice([dhw_mp, space_mp])]
    client = _build_client_with_devices(devices)

    out = client.get_2hourly_consumption_from_cache(today_local=today)
    today_bucket0 = out["2026-05-02"][0]
    assert today_bucket0["heating_kwh"] == pytest.approx(0.6)
    assert today_bucket0["dhw_kwh"] == pytest.approx(0.4)
    assert today_bucket0["total_kwh"] == pytest.approx(1.0)


def test_none_values_skipped_so_only_populated_buckets_returned() -> None:
    today = date(2026, 5, 2)
    heating_d = _d_array({0: 0.5, 12: 0.3})  # one yesterday bucket, one today bucket; rest None
    devices = [_FakeDevice([_consumption_data(heating_d=heating_d)])]
    client = _build_client_with_devices(devices)

    out = client.get_2hourly_consumption_from_cache(today_local=today)
    # Only one bucket per day populated
    assert set(out["2026-05-01"].keys()) == {0}
    assert set(out["2026-05-02"].keys()) == {0}


def test_wrong_array_length_silently_skipped() -> None:
    """Defensive — only the documented 24-element shape is parsed.
    A 12- or 48-element array should not crash, just yield nothing."""
    today = date(2026, 5, 2)
    weird = [0.5] * 12  # wrong shape
    devices = [_FakeDevice([_consumption_data(heating_d=weird)])]
    client = _build_client_with_devices(devices)
    out = client.get_2hourly_consumption_from_cache(today_local=today)
    assert out == {}


def test_db_upsert_round_trip() -> None:
    db.upsert_daikin_consumption_2hourly(
        date="2026-05-01", bucket_idx=3, kwh_total=0.55,
        kwh_heating=0.55, kwh_dhw=0.0, source="onecta_cache",
    )
    rows = db.get_daikin_consumption_2hourly_range("2026-05-01", "2026-05-01")
    assert len(rows) == 1
    r = rows[0]
    assert r["bucket_idx"] == 3
    assert r["kwh_total"] == pytest.approx(0.55)
    assert r["source"] == "onecta_cache"


def test_db_upsert_idempotent_overwrites() -> None:
    """Re-polling the same (date, bucket_idx) overwrites — the correct semantics
    when Onecta later revises an earlier-reported value."""
    db.upsert_daikin_consumption_2hourly(
        date="2026-05-01", bucket_idx=5, kwh_total=0.10, source="onecta_cache",
    )
    db.upsert_daikin_consumption_2hourly(
        date="2026-05-01", bucket_idx=5, kwh_total=0.42, source="onecta_cache",
    )
    rows = db.get_daikin_consumption_2hourly_range("2026-05-01", "2026-05-01")
    assert len(rows) == 1
    assert rows[0]["kwh_total"] == pytest.approx(0.42)


def test_db_rejects_out_of_range_bucket() -> None:
    with pytest.raises(ValueError):
        db.upsert_daikin_consumption_2hourly(
            date="2026-05-01", bucket_idx=12, kwh_total=0.0,
        )
    with pytest.raises(ValueError):
        db.upsert_daikin_consumption_2hourly(
            date="2026-05-01", bucket_idx=-1, kwh_total=0.0,
        )
