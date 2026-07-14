"""The Daikin rollup must not burn quota it doesn't need.

Daikin Onecta allows 200 requests/day, and the dispatch write-budget guard prunes
scheduled actions when headroom runs out (#309) — so a recurring job overspending
is not cosmetic.

Both `get_*_consumption_from_cache` methods promised "Zero extra Daikin API quota
— read-only over an already-cached payload", while each called `self.get_devices()`
internally: an unconditional `/gateway-devices` wire call. The rollup job calls
both → 2 reads per run, against a job advertising zero.

Two things are locked here:
  1. the REAL client makes ZERO wire calls when handed a devices payload, and
  2. the JOB reads through the service cache (30-min TTL) rather than the client,
     so a warm cache costs zero reads — and it shares ONE payload with both parsers.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 1. The REAL client — the seam that actually matters
# ---------------------------------------------------------------------------

def test_real_client_makes_zero_wire_calls_when_given_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handed a payload, the parsers must never touch the wire.

    This is the assertion that holds the fix down. A signature check alone would
    still pass if the body called `self.get_devices()` unconditionally.
    """
    from src.daikin.client import DaikinClient

    client = DaikinClient.__new__(DaikinClient)   # no network/auth in __init__

    wire_calls: list[str] = []

    def _boom(path: str = "", *a: Any, **kw: Any) -> Any:
        wire_calls.append(path or "get_devices")
        raise AssertionError(f"unexpected Daikin wire call: {path or 'get_devices'}")

    monkeypatch.setattr(client, "_get", _boom, raising=False)
    monkeypatch.setattr(client, "get_devices", _boom, raising=False)

    day = dt.date(2026, 5, 14)
    assert client.get_daily_consumption_from_cache(today_utc=day, devices=[]) == {}
    assert client.get_2hourly_consumption_from_cache(today_local=day, devices=[]) == {}
    assert wire_calls == [], f"parsers hit the wire despite being handed devices: {wire_calls}"


def test_from_cache_parsers_expose_the_devices_seam() -> None:
    import inspect

    from src.daikin.client import DaikinClient

    for name in ("get_daily_consumption_from_cache", "get_2hourly_consumption_from_cache"):
        sig = inspect.signature(getattr(DaikinClient, name))
        assert "devices" in sig.parameters, (
            f"{name} must take `devices=` so callers can avoid a redundant "
            f"/gateway-devices read (200/day cap)"
        )


# ---------------------------------------------------------------------------
# 2. The JOB — reads through the service cache, shares one payload
# ---------------------------------------------------------------------------

class _Device:
    """Minimal stand-in; the parsers iterate it and find no consumption data."""
    management_points: list[Any] = []
    raw: dict[str, Any] = {}


class _CountingClient:
    def __init__(self) -> None:
        self.get_devices_calls = 0

    def get_devices(self) -> list[Any]:
        self.get_devices_calls += 1
        return [_Device()]

    def get_daily_consumption_from_cache(self, today_utc: Any = None, devices: Any = None) -> dict:
        if devices is None:
            self.get_devices()          # the OLD behaviour we guard against
        return {}

    def get_2hourly_consumption_from_cache(self, today_local: Any = None, devices: Any = None) -> dict:
        if devices is None:
            self.get_devices()
        return {}


def _cached_devices(**kw: Any) -> Any:
    from src.daikin.service import CachedDevices
    return CachedDevices(
        devices=[_Device()], fetched_at_wall=0.0, age_seconds=12.0,
        stale=False, source="cache",
    )


def test_rollup_reads_through_the_service_cache_not_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WARM service cache must cost the job ZERO /gateway-devices reads.

    `get_daikin_client()` is documented "for write operations only. For reads, use
    daikin_service" — the service layer carries the 30-min TTL cache and the
    anti-burst floor. Going to the client directly bypasses both, and throws the
    payload away instead of seeding the cache for the next caller.
    """
    from src.daikin import service as daikin_service
    from src.scheduler import runner

    client = _CountingClient()
    monkeypatch.setattr("src.api.main.get_daikin_client", lambda: client, raising=True)

    fetches: list[str] = []

    def _spy(**kw: Any) -> Any:
        fetches.append(str(kw.get("actor", "?")))
        return _cached_devices()

    monkeypatch.setattr(daikin_service, "get_cached_devices", _spy, raising=True)

    runner.bulletproof_daikin_consumption_rollup_job()

    assert client.get_devices_calls == 0, (
        f"job burned {client.get_devices_calls} direct client read(s); it must go "
        f"through daikin_service.get_cached_devices (warm cache = zero quota)"
    )
    assert len(fetches) == 1, (
        f"job must fetch the device payload ONCE and share it with both parsers, "
        f"got {len(fetches)} fetches"
    )


def test_rollup_survives_a_stale_cache_under_quota_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When quota is exhausted the service degrades to a STALE payload rather than
    raising. The consumption arrays are historical data, so a 40-min-stale payload
    rolls up perfectly well — the job must proceed, not abort with zero rows on
    the one day quota pressure actually bites."""
    from src.daikin import service as daikin_service
    from src.daikin.service import CachedDevices
    from src.scheduler import runner

    client = _CountingClient()
    monkeypatch.setattr("src.api.main.get_daikin_client", lambda: client, raising=True)
    monkeypatch.setattr(
        daikin_service, "get_cached_devices",
        lambda **kw: CachedDevices(
            devices=[_Device()], fetched_at_wall=0.0, age_seconds=2400.0,
            stale=True, source="cache_stale",
        ),
        raising=True,
    )

    runner.bulletproof_daikin_consumption_rollup_job()   # must not raise
    assert client.get_devices_calls == 0
