"""``GET /api/v1/weather/now`` — the ESP32's ~120-byte current-conditions read.

Two contracts are locked here:

1. **Shape** — a tiny flat payload the sensor can parse without blowing its heap,
   including ``rain_in_h``, which needs the WHOLE 96 h forecast to compute and is
   therefore precisely the thing the device cannot afford to fetch itself.
2. **Auth** — this route is NOT part of the token-free viewer surface. The sensor
   is on the house LAN and reaches HEM through the PUBLIC Tailscale funnel, so an
   unauthenticated weather route is a route the whole internet can read. It takes
   the scoped ingest token (the same one it already carries to POST readings) or
   an admin token; everyone else gets 401.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest


@dataclass
class _F:
    time_utc: datetime
    temperature_c: float | None = 20.0
    estimated_pv_kw: float | None = 1.0
    cloud_cover_pct: float | None = 10.0
    shortwave_radiation_wm2: float | None = 400.0
    precipitation_mm: float | None = 0.0
    weather_code: int | None = 1


def _forecast(now: datetime, rain_at_h: int | None = None) -> list[_F]:
    """96 h hourly forecast starting one hour BEFORE now (so `now` has a current hour)."""
    out = []
    for i in range(-1, 95):
        f = _F(time_utc=(now + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0))
        if rain_at_h is not None and i == rain_at_h:
            f.weather_code = 61          # WMO: rain
            f.precipitation_mm = 1.4
        out.append(f)
    return out


def _call(monkeypatch: pytest.MonkeyPatch, fc: list[_F]) -> dict:
    from src.api import main as api_main
    monkeypatch.setattr(
        "src.weather.fetch_weather_panel_forecast_cached",
        lambda hours=96: fc, raising=True,
    )
    return asyncio.run(api_main.api_v1_weather_now())


def test_payload_is_small_and_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    now = datetime.now(UTC)
    out = _call(monkeypatch, _forecast(now))

    assert set(out) == {"temp_c", "weather_code", "precipitation_mm", "rain_in_h", "pv_now_kw"}
    assert all(not isinstance(v, (dict, list)) for v in out.values()), "must stay flat for the ESP32"
    # The whole point: the 96 h /weather payload is ~15 KB. This must not be.
    assert len(json.dumps(out)) < 200, f"payload too big for an ESP32 heap: {out}"


def test_picks_the_current_hour_not_a_future_one(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    fc = _forecast(now)
    # Mark the hour that has already started; a naive `fc[0]` would pick the hour
    # BEFORE it, and a naive "next" would pick a future hour.
    for f in fc:
        if f.time_utc <= now:
            current = f
    current.temperature_c = 27.5

    out = _call(monkeypatch, fc)
    assert out["temp_c"] == pytest.approx(27.5)


def test_rain_in_h_counts_hours_to_the_next_rain(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    out = _call(monkeypatch, _forecast(now, rain_at_h=6))
    assert out["rain_in_h"] is not None
    assert 5 <= out["rain_in_h"] <= 7, out["rain_in_h"]


def test_rain_in_h_is_null_when_the_horizon_is_dry(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _call(monkeypatch, _forecast(datetime.now(UTC)))
    assert out["rain_in_h"] is None


def test_rain_in_h_ignores_current_rain(monkeypatch: pytest.MonkeyPatch) -> None:
    """It answers "when does it NEXT rain", so a wet *current* hour is not 0 h."""
    now = datetime.now(UTC)
    fc = _forecast(now)
    for f in fc:
        if f.time_utc <= now:
            cur = f
    cur.weather_code = 61
    cur.precipitation_mm = 2.0

    out = _call(monkeypatch, fc)
    assert out["precipitation_mm"] == pytest.approx(2.0)
    assert out["rain_in_h"] is None, "current rain must not be reported as future rain"


# ---------------------------------------------------------------------------
# Auth — the security half. This route must NOT be viewer-open.
# ---------------------------------------------------------------------------

def _role_auth():
    from src.api.middleware import ApiV1RoleAuth
    return ApiV1RoleAuth(
        app=None, admin_tokens=["ADMIN"], enabled=lambda: True,
        ingest_tokens=["INGEST"],
    )


def test_weather_now_is_not_viewer_open() -> None:
    """The whole point. /api/v1/weather (96 h) IS viewer-open and therefore public
    on the funnel; /weather/now must not inherit that."""
    m = _role_auth()
    assert m._needs_admin("GET", "/api/v1/weather/now"), (
        "/weather/now must be gated — a token-free route here is readable by the "
        "whole internet through the public Tailscale funnel"
    )
    # ...whereas the 96 h /weather IS viewer-open (existing, deliberate).
    assert not m._needs_admin("GET", "/api/v1/weather")


def test_ingest_token_may_GET_weather_now_and_nothing_else() -> None:
    m = _role_auth()
    # the two keys on the keyring
    assert m._ingest_allowed("GET", "/api/v1/weather/now")
    assert m._ingest_allowed("POST", "/api/v1/sensors/indoor")

    # ...and nothing else. Verbs must not cross between the two lists.
    assert not m._ingest_allowed("POST", "/api/v1/weather/now")
    assert not m._ingest_allowed("GET", "/api/v1/sensors/indoor")
    # no admin reads
    assert not m._ingest_allowed("GET", "/api/v1/settings")
    assert not m._ingest_allowed("GET", "/api/v1/action-log")
    # no path games
    assert not m._ingest_allowed("GET", "/api/v1/weather/nowX")
    assert not m._ingest_allowed("GET", "/api/v1/weather/now/foo")
    assert not m._ingest_allowed("GET", "/api/v1/weather")
