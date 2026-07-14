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


def _forecast(now: datetime, rain_at_h: int | None = None, *, include_past: bool = False) -> list[_F]:
    """Hourly forecast.

    PRODUCTION SHAPE by default: future-only. The upstream fetch
    (``weather._fetch...``) drops every hour with ``dt < now``, so the freshest
    hour the endpoint ever sees is the next top-of-hour. ``rain_at_h`` is an
    index into this list (0 = the reported/current hour).
    """
    start = -1 if include_past else 1
    base = now.replace(minute=0, second=0, microsecond=0)
    out = []
    for n, i in enumerate(range(start, 95)):
        f = _F(time_utc=base + timedelta(hours=i))
        if rain_at_h is not None and n == rain_at_h:
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


def test_reports_the_nearest_hour_production_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production forecast is future-only; the nearest hour is fc[0]."""
    now = datetime.now(UTC)
    fc = _forecast(now)                       # future-only, as prod
    fc[0].temperature_c = 27.5
    fc[1].temperature_c = 99.9                # an hour ahead — must NOT be picked
    out = _call(monkeypatch, fc)
    assert out["temp_c"] == pytest.approx(27.5), "must report the nearest hour, not one ahead"


def test_prefers_the_current_hour_if_the_source_ever_includes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: if the source ever DOES include the started hour, use it."""
    now = datetime.now(UTC)
    fc = _forecast(now, include_past=True)    # fc[0]=prev hour, fc[1]=current started hour
    # Mark whichever hour is the last one <= now (the "current" hour).
    current = [f for f in fc if f.time_utc <= now][-1]
    current.temperature_c = 27.5
    out = _call(monkeypatch, fc)
    assert out["temp_c"] == pytest.approx(27.5)


def test_rain_in_h_counts_hours_to_the_next_rain(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    out = _call(monkeypatch, _forecast(now, rain_at_h=6))
    assert out["rain_in_h"] == 6


def test_rain_in_h_is_null_when_the_horizon_is_dry(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _call(monkeypatch, _forecast(datetime.now(UTC)))
    assert out["rain_in_h"] is None


def test_rain_in_h_ignores_current_rain(monkeypatch: pytest.MonkeyPatch) -> None:
    """It answers "when does it NEXT rain", so a wet *reported* hour is not future rain."""
    now = datetime.now(UTC)
    fc = _forecast(now)          # future-only
    fc[0].weather_code = 61      # the reported hour is wet...
    fc[0].precipitation_mm = 2.0
    # ...and nothing after it rains.
    out = _call(monkeypatch, fc)
    assert out["precipitation_mm"] == pytest.approx(2.0)
    assert out["rain_in_h"] is None, "current rain must not be reported as future rain"


def test_rain_next_hour_is_1_not_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rain in the very next hour reads as 1 h, never 0 (which would mean 'now')."""
    now = datetime.now(UTC)
    out = _call(monkeypatch, _forecast(now, rain_at_h=1))   # index 1 = one hour after reported
    assert out["rain_in_h"] == 1
    assert out["precipitation_mm"] == pytest.approx(0.0), "reported (dry) hour, not the rain hour"


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


def test_route_is_gated_in_the_REAL_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end against the assembled app, not a hand-built middleware — the
    thing that actually ships. /weather/now needs a token; the 96 h /weather does
    not; the ingest token opens /weather/now and nothing admin.
    """
    monkeypatch.setenv("HEM_UI_AUTH_REQUIRED", "true")
    monkeypatch.setenv("HEM_ADMIN_TOKEN", "ADMINTOK")
    monkeypatch.setenv("HEM_SENSOR_INGEST_TOKEN", "INGESTTOK")
    from src.config import config
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", True, raising=False)
    monkeypatch.setattr(config, "HEM_ADMIN_TOKEN", "ADMINTOK", raising=False)
    monkeypatch.setattr(config, "HEM_SENSOR_INGEST_TOKEN", "INGESTTOK", raising=False)

    # keep the handler cheap + deterministic
    from datetime import UTC as _U, datetime as _d, timedelta as _t
    monkeypatch.setattr(
        "src.weather.fetch_weather_panel_forecast_cached",
        lambda hours=96: _forecast(_d.now(_U)), raising=True,
    )

    from starlette.testclient import TestClient
    from src.api.main import app
    cl = TestClient(app)

    assert cl.get("/api/v1/weather/now").status_code == 401
    assert cl.get("/api/v1/weather/now", headers={"Authorization": "Bearer INGESTTOK"}).status_code == 200
    assert cl.get("/api/v1/weather/now", headers={"Authorization": "Bearer ADMINTOK"}).status_code == 200
    # the 96 h route stays viewer-open
    assert cl.get("/api/v1/weather").status_code == 200
    # the ingest token buys no admin read, and cannot POST the read route
    assert cl.get("/api/v1/settings", headers={"Authorization": "Bearer INGESTTOK"}).status_code == 401
    assert cl.post("/api/v1/weather/now", headers={"Authorization": "Bearer INGESTTOK"}).status_code in (401, 405)
