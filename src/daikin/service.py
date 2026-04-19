"""Daikin service layer — singleton client, device cache, and quota-aware refresh.

Key design:
- One module-level DaikinClient instance, created lazily on first use.
- Device list is cached for DAIKIN_DEVICES_CACHE_TTL_SECONDS (default 30 min).
- ``get_cached_devices(allow_refresh=False)`` — always reads cache on warm hits.
  Only refreshes when allow_refresh=True AND cache is stale AND quota allows.
- ``force_refresh_devices(actor)`` — explicit user-triggered refresh, throttled by
  DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS per actor.
- Write wrappers check quota and call ``invalidate_after_write()`` so the next
  allowed refresh slot will fetch fresh state.
- On quota exhaustion returns last cached value with stale=True (never hard-fails).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import config
from .client import DaikinClient, DaikinError
from .models import DaikinDevice
from ..api_quota import record_call, should_block, quota_remaining, get_quota_status

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (singleton)
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_client: Optional[DaikinClient] = None

_devices_cache: Optional[List[DaikinDevice]] = None
_devices_fetched_monotonic: Optional[float] = None
_devices_fetched_wall: Optional[float] = None   # epoch seconds
_devices_stale: bool = False                    # True after write until next refresh

# Per-actor cooldown map for force-refresh: actor_key -> last epoch seconds
_force_refresh_timestamps: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Public dataclass returned to callers
# ---------------------------------------------------------------------------
@dataclass
class CachedDevices:
    devices: List[DaikinDevice]
    fetched_at_wall: Optional[float]  # epoch seconds, or None if cold-start
    age_seconds: float
    stale: bool
    source: str  # "fresh" | "cache" | "cache_stale" | "cold_start"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_or_create_client() -> DaikinClient:
    global _client
    if _client is None:
        _client = DaikinClient()
    return _client


def _cache_age_seconds() -> float:
    if _devices_fetched_monotonic is None:
        return float("inf")
    return time.monotonic() - _devices_fetched_monotonic


def _cache_is_warm() -> bool:
    return (
        _devices_cache is not None
        and _cache_age_seconds() < config.DAIKIN_DEVICES_CACHE_TTL_SECONDS
    )


def _do_refresh(actor: str) -> List[DaikinDevice]:
    """Actually call get_devices(), update cache, and record quota usage."""
    global _devices_cache, _devices_fetched_monotonic, _devices_fetched_wall, _devices_stale
    client = _get_or_create_client()
    try:
        devices = client.get_devices()
        ok = True
    except Exception:
        ok = False
        record_call("daikin", "read", ok=False)
        raise

    record_call("daikin", "read", ok=True)
    _devices_cache = devices
    _devices_fetched_monotonic = time.monotonic()
    _devices_fetched_wall = time.time()
    _devices_stale = False
    logger.info("Daikin devices refreshed by %s (%d device(s))", actor, len(devices))
    return devices


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_cached_devices(
    *,
    allow_refresh: bool = False,
    max_age_seconds: Optional[int] = None,
    actor: str = "unknown",
) -> CachedDevices:
    """Return Daikin devices, preferring the cache.

    Parameters
    ----------
    allow_refresh:
        When False (default), always returns whatever is in cache.
        When True, may trigger a real API call if cache is stale and quota allows.
    max_age_seconds:
        Override for the cache TTL for this call.
        Defaults to config.DAIKIN_DEVICES_CACHE_TTL_SECONDS.
    actor:
        Label for log messages (e.g. "heartbeat", "mpc", "api").
    """
    global _devices_cache, _devices_stale

    if max_age_seconds is None:
        max_age_seconds = config.DAIKIN_DEVICES_CACHE_TTL_SECONDS

    with _lock:
        # Cold-start: no cache at all — do exactly one initial fetch regardless of quota.
        if _devices_cache is None:
            logger.info("Daikin service: cold-start fetch (actor=%s)", actor)
            try:
                devices = _do_refresh(actor)
                return CachedDevices(
                    devices=devices,
                    fetched_at_wall=_devices_fetched_wall,
                    age_seconds=0.0,
                    stale=False,
                    source="cold_start",
                )
            except Exception as e:
                logger.warning("Daikin cold-start fetch failed: %s", e)
                return CachedDevices(
                    devices=[],
                    fetched_at_wall=None,
                    age_seconds=float("inf"),
                    stale=True,
                    source="cold_start",
                )

        age = _cache_age_seconds()
        is_warm = age < max_age_seconds

        # Cache hit — no refresh needed regardless of allow_refresh
        if is_warm and not _devices_stale:
            logger.debug("Daikin cache hit (age=%.0fs, actor=%s)", age, actor)
            return CachedDevices(
                devices=_devices_cache,
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=age,
                stale=False,
                source="cache",
            )

        # Cache is stale but allow_refresh=False — return stale data
        if not allow_refresh:
            return CachedDevices(
                devices=_devices_cache,
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=age,
                stale=True,
                source="cache_stale",
            )

        # allow_refresh=True — check quota first
        if should_block("daikin"):
            remaining = quota_remaining("daikin")
            logger.warning(
                "Daikin quota exhausted (remaining=%d); returning stale cache (actor=%s)",
                remaining,
                actor,
            )
            return CachedDevices(
                devices=_devices_cache,
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=age,
                stale=True,
                source="cache_stale",
            )

        # Quota OK — do the refresh
        try:
            devices = _do_refresh(actor)
            return CachedDevices(
                devices=devices,
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=0.0,
                stale=False,
                source="fresh",
            )
        except Exception as e:
            logger.warning("Daikin refresh failed (actor=%s): %s — returning stale cache", actor, e)
            return CachedDevices(
                devices=_devices_cache,
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=age,
                stale=True,
                source="cache_stale",
            )


def force_refresh_devices(
    actor: str,
    min_interval_seconds: Optional[int] = None,
) -> CachedDevices:
    """Explicitly refresh device data (e.g. from a user-facing "Refresh" button).

    Throttled by DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS per actor to prevent
    UI mash-clicking from blowing through the daily quota.
    """
    if min_interval_seconds is None:
        min_interval_seconds = config.DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS

    with _lock:
        last = _force_refresh_timestamps.get(actor, 0.0)
        elapsed = time.time() - last
        if elapsed < min_interval_seconds:
            wait = min_interval_seconds - elapsed
            logger.info(
                "Daikin force-refresh throttled for actor=%s (wait %.0fs)", actor, wait
            )
            # Return cache with staleness info but do not hit the API
            age = _cache_age_seconds()
            return CachedDevices(
                devices=_devices_cache or [],
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=age,
                stale=True,
                source="cache_stale",
            )

        if should_block("daikin"):
            logger.warning("Daikin force-refresh blocked: daily quota exhausted (actor=%s)", actor)
            age = _cache_age_seconds()
            return CachedDevices(
                devices=_devices_cache or [],
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=age,
                stale=True,
                source="cache_stale",
            )

        _force_refresh_timestamps[actor] = time.time()
        try:
            devices = _do_refresh(actor)
            return CachedDevices(
                devices=devices,
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=0.0,
                stale=False,
                source="fresh",
            )
        except Exception as e:
            logger.warning("Daikin force-refresh failed (actor=%s): %s", actor, e)
            age = _cache_age_seconds()
            return CachedDevices(
                devices=_devices_cache or [],
                fetched_at_wall=_devices_fetched_wall,
                age_seconds=age,
                stale=True,
                source="cache_stale",
            )


def invalidate_after_write() -> None:
    """Mark cache as stale so the next allowed refresh slot fetches fresh state."""
    global _devices_stale
    with _lock:
        _devices_stale = True
    logger.debug("Daikin device cache invalidated after write")


def get_quota_status_daikin() -> dict:
    """Return cache and quota info for dashboard / status endpoints."""
    from ..api_quota import get_quota_status as _qs
    with _lock:
        age = _cache_age_seconds()
        qst = _qs("daikin")
    return {
        "cache_age_seconds": None if age == float("inf") else round(age, 1),
        "cache_warm": _cache_is_warm(),
        "stale": _devices_stale,
        "last_refresh_at_utc": (
            None
            if _devices_fetched_wall is None
            else __import__("datetime").datetime.fromtimestamp(
                _devices_fetched_wall,
                tz=__import__("datetime").timezone.utc,
            ).isoformat()
        ),
        **qst,
    }


# ---------------------------------------------------------------------------
# Write wrappers (quota-aware, invalidate cache)
# ---------------------------------------------------------------------------

def _require_client_and_devices(actor: str):
    """Return (client, first_device). Raises if not available."""
    result = get_cached_devices(allow_refresh=True, actor=actor)
    if not result.devices:
        raise DaikinError("No Daikin devices available")
    return _get_or_create_client(), result.devices


def set_power(on: bool, actor: str = "api") -> None:
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set power")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_power(dev, on)
    record_call("daikin", "write", ok=True)
    invalidate_after_write()


def set_temperature(temperature: float, mode: str = "heating", actor: str = "api") -> None:
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set temperature")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_temperature(dev, temperature, mode)
    record_call("daikin", "write", ok=True)
    invalidate_after_write()


def set_lwt_offset(offset: float, mode: str = "heating", actor: str = "api") -> None:
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set LWT offset")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_lwt_offset(dev, offset, mode)
    record_call("daikin", "write", ok=True)
    invalidate_after_write()


def set_operation_mode(mode_str: str, actor: str = "api") -> None:
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set mode")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_operation_mode(dev, mode_str)
    record_call("daikin", "write", ok=True)
    invalidate_after_write()


def set_tank_temperature(temperature: float, actor: str = "api") -> None:
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set tank temperature")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_tank_temperature(dev, temperature)
    record_call("daikin", "write", ok=True)
    invalidate_after_write()


def set_tank_power(on: bool, actor: str = "api") -> None:
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set tank power")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_tank_power(dev, on)
    record_call("daikin", "write", ok=True)
    invalidate_after_write()


def set_tank_powerful(on: bool, actor: str = "api") -> None:
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set tank powerful mode")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_tank_powerful(dev, on)
    record_call("daikin", "write", ok=True)
    invalidate_after_write()


def set_weather_regulation(enabled: bool, actor: str = "api") -> None:
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set weather regulation")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_weather_regulation(dev, enabled)
    record_call("daikin", "write", ok=True)
    invalidate_after_write()
