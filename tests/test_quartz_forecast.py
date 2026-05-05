from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src import db
from src.config import config as app_config


class _Resp:
    def __init__(self, payload: dict[str, Any] | list[dict[str, Any]]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


def test_quartz_fetch_merges_direct_pv_with_open_meteo_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import weather

    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    open_meteo_payload = {
        "hourly": {
            "time": [(base + timedelta(hours=i)).isoformat().replace("+00:00", "") for i in range(4)],
            "temperature_2m": [11.0, 12.0, 13.0, 14.0],
            "cloud_cover": [80.0, 70.0, 60.0, 50.0],
            "shortwave_radiation_instant": [100.0, 200.0, 300.0, 400.0],
        }
    }
    quartz_target = base + timedelta(minutes=30)
    quartz_payload = [
        {
            "targetTime": quartz_target.isoformat().replace("+00:00", "Z"),
            "expectedPowerGenerationMegawatts": 10.0,
            "expectedPowerGenerationNormalized": 0.5,
        }
    ]

    def fake_urlopen(req, timeout=0):  # noqa: ANN001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.open-meteo.com" in url:
            return _Resp(open_meteo_payload)
        if "oauth/token" in url:
            return _Resp({"access_token": "token"})
        if "/v0/solar/GB/" in url:
            return _Resp(quartz_payload)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(app_config, "FORECAST_SOURCE", "quartz", raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_USERNAME", "user", raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_PASSWORD", "pass", raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_GSP_ID", "42", raising=False)
    monkeypatch.setattr(weather, "_QUARTZ_TOKEN", None)
    monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)

    result = weather.fetch_forecast_snapshot(hours=4)

    direct = [f for f in result.forecast if f.pv_direct]
    assert result.source == "quartz"
    assert len(direct) == 1
    assert direct[0].time_utc == base
    assert direct[0].estimated_pv_kw == pytest.approx(2.25)
    assert direct[0].temperature_c == pytest.approx(11.0)
    assert direct[0].cloud_cover_pct == pytest.approx(80.0)
    assert result.raw_payload_json


def test_direct_pv_uses_site_calibration_scale() -> None:
    from src.weather import HourlyForecast, forecast_to_lp_inputs

    db.upsert_pv_calibration_hourly({12: 0.5}, {12: 8}, window_days=30)

    slot = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    forecast = [
        HourlyForecast(
            time_utc=slot,
            temperature_c=12.0,
            cloud_cover_pct=100.0,
            shortwave_radiation_wm2=0.0,
            estimated_pv_kw=2.0,
            heating_demand_factor=0.0,
            pv_direct=True,
        )
    ]

    series = forecast_to_lp_inputs(forecast, [slot], pv_scale=1.0)

    assert series.pv_kwh_per_slot == pytest.approx([0.5])


def test_quartz_national_metadata_capacity_can_downscale_site_kw(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import weather

    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    open_meteo_payload = {
        "hourly": {
            "time": [base.isoformat().replace("+00:00", "")],
            "temperature_2m": [11.0],
            "cloud_cover": [50.0],
            "shortwave_radiation_instant": [100.0],
        }
    }
    quartz_payload = {
        "location": {"label": "national", "installedCapacityMw": 1000.0},
        "model": {"name": "blend_adjust", "version": "1.3.0"},
        "forecastValues": [
            {
                "targetTime": (base + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                "expectedPowerGenerationMegawatts": 500.0,
            }
        ],
    }

    def fake_urlopen(req, timeout=0):  # noqa: ANN001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.open-meteo.com" in url:
            return _Resp(open_meteo_payload)
        if "oauth/token" in url:
            return _Resp({"access_token": "token"})
        if "/v0/solar/GB/" in url:
            return _Resp(quartz_payload)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(app_config, "FORECAST_SOURCE", "quartz", raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_USERNAME", "user", raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_PASSWORD", "pass", raising=False)
    monkeypatch.setattr(app_config, "QUARTZ_GSP_ID", "", raising=False)
    monkeypatch.setattr(weather, "_QUARTZ_TOKEN", None)
    monkeypatch.setattr(weather.urllib.request, "urlopen", fake_urlopen)

    result = weather.fetch_forecast_snapshot(hours=2)

    direct = [f for f in result.forecast if f.pv_direct]
    assert result.model_name == "blend_adjust"
    assert direct[0].estimated_pv_kw == pytest.approx(2.25)


def test_direct_pv_round_trips_through_canonical_snapshot() -> None:
    fetched_at = "2026-05-05T10:00:00+00:00"
    db.save_meteo_forecast_snapshot(
        fetched_at,
        [
            {
                "slot_time": "2026-05-05T12:00:00+00:00",
                "temp_c": 12.0,
                "solar_w_m2": 0.0,
                "cloud_cover_pct": 90.0,
                "direct_pv_kw": 1.7,
            }
        ],
        source="quartz",
        model_name="quartz-gsp",
        raw_payload_json='{"ok":true}',
    )

    rows = db.get_meteo_forecast_at(fetched_at)

    assert rows[0]["direct_pv_kw"] == pytest.approx(1.7)
