"""Site-level open Quartz provider (#542) — sidecar/open.quartz.solar client.

The provider must be a drop-in for the legacy hosted client: same
HourlyForecast shape (pv_direct=True, half-hour slot starts, weather context
interpolated from Open-Meteo), so the calibration chain and snapshots stay
provider-agnostic.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src import db
from src.config import config as app_config


class _Resp:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()
    monkeypatch.setattr(app_config, "WEATHER_LAT", "51.4927", raising=False)
    monkeypatch.setattr(app_config, "WEATHER_LON", "-0.2628", raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_OPEN_SEND_LIVE", False, raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_OPEN_PLANES", "", raising=False)


def _base_weather(start: datetime, n: int = 6):
    from src.weather import HourlyForecast, compute_heating_demand_factor
    return [
        HourlyForecast(
            time_utc=start + timedelta(hours=i),
            temperature_c=12.0 + i,
            cloud_cover_pct=40.0,
            shortwave_radiation_wm2=300.0,
            estimated_pv_kw=1.0,
            heating_demand_factor=compute_heating_demand_factor(12.0 + i),
        )
        for i in range(n)
    ]


def _open_payload(start: datetime, quarter_kw: list[float]) -> dict[str, Any]:
    """Build an open-schema response: naive-UTC 15-min timestamps → kW."""
    return {
        "timestamp": start.replace(tzinfo=None).isoformat(),
        "predictions": {
            "power_kw": {
                (start + timedelta(minutes=15 * i)).replace(tzinfo=None).isoformat(): kw
                for i, kw in enumerate(quarter_kw)
            }
        },
    }


def test_open_provider_buckets_quarters_into_half_hours(monkeypatch):
    from src import weather

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    calls: list[dict] = []

    def fake_urlopen(req, timeout=0):  # noqa: ANN001
        calls.append(json.loads(req.data.decode()))
        return _Resp(_open_payload(start, [1.0, 2.0, 3.0, 5.0]))

    monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)
    res = weather._fetch_quartz_open_forecast(
        hours=48, base_weather=_base_weather(start), lat="51.4927", lon="-0.2628"
    )
    assert res.forecast, "expected merged forecast rows"
    assert res.model_name == "quartz-open-site"
    by_time = {f.time_utc: f for f in res.forecast if f.pv_direct}
    # :00+:15 → mean 1.5 kW at slot :00; :30+:45 → mean 4.0 kW at slot :30
    assert by_time[start].estimated_pv_kw == pytest.approx(1.5)
    assert by_time[start + timedelta(minutes=30)].estimated_pv_kw == pytest.approx(4.0)
    # Weather context interpolated from base, not placeholder
    assert by_time[start].temperature_c == pytest.approx(12.0)
    # Single aggregate plane by default
    assert len(calls) == 1
    assert calls[0]["site"]["capacity_kwp"] == pytest.approx(
        float(app_config.PV_CAPACITY_KWP)
    )


def test_open_provider_sums_planes(monkeypatch):
    from src import weather

    monkeypatch.setattr(
        app_config, "QUARTZ_OPEN_PLANES",
        '[{"tilt": 35, "orientation": 225, "capacity_kwp": 2.25},'
        ' {"tilt": 10, "orientation": 180, "capacity_kwp": 2.25}]',
        raising=False,
    )
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    calls: list[dict] = []

    def fake_urlopen(req, timeout=0):  # noqa: ANN001
        body = json.loads(req.data.decode())
        calls.append(body)
        kw = 1.0 if body["site"]["orientation"] == 225 else 0.5
        return _Resp(_open_payload(start, [kw, kw]))

    monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)
    res = weather._fetch_quartz_open_forecast(
        hours=48, base_weather=_base_weather(start), lat="51.4927", lon="-0.2628"
    )
    assert len(calls) == 2
    by_time = {f.time_utc: f for f in res.forecast if f.pv_direct}
    assert by_time[start].estimated_pv_kw == pytest.approx(1.5)  # 1.0 + 0.5


def test_open_provider_failure_falls_back_to_open_meteo(monkeypatch):
    from src import weather

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    def fake_urlopen(req, timeout=0):  # noqa: ANN001
        raise weather.urllib.error.URLError("sidecar down")

    monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)
    res = weather._fetch_quartz_open_forecast(
        hours=48, base_weather=_base_weather(start), lat="51.4927", lon="-0.2628"
    )
    assert res.forecast == []  # caller (fetch_forecast_snapshot) falls back to OM


def test_fetch_snapshot_provider_switch(monkeypatch):
    """FORECAST_SOURCE=quartz + QUARTZ_PROVIDER=open routes to the open client
    and falls back to Open-Meteo rows when it returns empty."""
    from src import weather

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    monkeypatch.setattr(app_config, "FORECAST_SOURCE", "quartz", raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_PROVIDER", "open", raising=False)
    monkeypatch.setattr(
        weather, "_fetch_open_meteo_forecast", lambda **_kw: _base_weather(start)
    )
    seen = {}

    def fake_open_fetch(**kwargs):
        seen.update(kwargs)
        return weather.ForecastFetchResult(forecast=[], source="quartz-open")

    monkeypatch.setattr(weather, "_fetch_quartz_open_forecast", fake_open_fetch)
    res = weather.fetch_forecast_snapshot(hours=12)
    assert "base_weather" in seen, "open provider was not invoked"
    assert res.source == "open-meteo"  # graceful fallback
    assert len(res.forecast) == 6


def test_bad_planes_json_falls_back_to_aggregate(monkeypatch):
    from src import weather

    monkeypatch.setattr(app_config, "QUARTZ_OPEN_PLANES", "not json", raising=False)
    planes = weather._quartz_open_planes()
    assert len(planes) == 1
    assert planes[0]["orientation"] == pytest.approx(200.0)


def test_planes_sum_despite_per_request_microsecond_offsets(monkeypatch):
    """The live API stamps timestamps with per-REQUEST microsecond offsets
    ('09:45:00.533720', different on every call). Plane keys must be floored
    to the 15-min grid so they collide and SUM — otherwise a 2-plane site
    forecasts at half its real output (caught during #544 self-review)."""
    from src import weather

    monkeypatch.setattr(
        app_config, "QUARTZ_OPEN_PLANES",
        '[{"tilt": 35, "orientation": 225, "capacity_kwp": 2.25},'
        ' {"tilt": 10, "orientation": 180, "capacity_kwp": 2.25}]',
        raising=False,
    )
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    call_n = {"n": 0}

    def fake_urlopen(req, timeout=0):  # noqa: ANN001
        call_n["n"] += 1
        jitter = timedelta(microseconds=123456 * call_n["n"])  # differs per call
        jittered = start + jitter
        return _Resp(_open_payload(jittered, [1.0, 1.0, 1.0, 1.0]))

    monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)
    res = weather._fetch_quartz_open_forecast(
        hours=48, base_weather=_base_weather(start), lat="51.4927", lon="-0.2628"
    )
    by_time = {f.time_utc: f for f in res.forecast if f.pv_direct}
    # Each plane contributes 1.0 kW per quarter → the SUM is 2.0 kW, not the
    # 1.0 kW a non-colliding average would produce.
    assert by_time[start].estimated_pv_kw == pytest.approx(2.0)
