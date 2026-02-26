"""Caching layer for Fox ESS realtime and energy data to avoid excessive API calls.

Also tracks when the last successful cloud refresh happened and how many
refreshes have occurred in the last 24 hours so we can stay within the
Fox ESS Open API limits (1440 calls per inverter per day).
"""
import logging
import time
from typing import Optional, Tuple

from ..config import config
from .client import FoxESSClient, FoxESSError
from .models import RealTimeData

logger = logging.getLogger(__name__)

# Cached realtime snapshot and timestamps
_last_realtime: Optional[RealTimeData] = None
_last_realtime_updated_monotonic: Optional[float] = None
_last_realtime_wallclock: Optional[float] = None  # epoch seconds

# Cached daily energy summary
_last_energy_today: Optional[dict] = None
_last_energy_today_updated_monotonic: Optional[float] = None

# Sliding window of realtime refresh timestamps (wall-clock, epoch seconds)
_refresh_timestamps: list[float] = []


def _get_client() -> FoxESSClient:
    """Build FoxESSClient from config. Raises ValueError if not configured."""
    return FoxESSClient(**config.foxess_client_kwargs())


def _record_realtime_refresh() -> None:
    """Record a successful realtime fetch and maintain a 24h sliding window."""
    global _refresh_timestamps, _last_realtime_wallclock
    now_wall = time.time()
    _last_realtime_wallclock = now_wall
    cutoff = now_wall - 24 * 3600
    # Drop entries older than 24h
    _refresh_timestamps = [t for t in _refresh_timestamps if t >= cutoff]
    _refresh_timestamps.append(now_wall)


def get_refresh_stats() -> Tuple[Optional[float], int]:
    """Return (last_updated_epoch, refresh_count_in_last_24h).

    last_updated_epoch is wall-clock seconds since epoch (UTC-neutral).
    Returns (None, 0) if we have never successfully hit the Open API.
    """
    global _refresh_timestamps, _last_realtime_wallclock
    if _last_realtime_wallclock is None:
        return None, 0

    now_wall = time.time()
    cutoff = now_wall - 24 * 3600
    _refresh_timestamps = [t for t in _refresh_timestamps if t >= cutoff]
    return _last_realtime_wallclock, len(_refresh_timestamps)


def get_cached_realtime(max_age_seconds: int = 30) -> RealTimeData:
    """Return realtime data from cache if fresh, else fetch from Fox ESS.

    - Uses a short-lived in-memory cache keyed only by time.
    - On a real cloud call, updates the 24h refresh counter and last-updated time.

    Raises ValueError if Fox ESS is not configured.
    Raises FoxESSError on API errors (for API endpoints to map to 502).
    """
    global _last_realtime, _last_realtime_updated_monotonic
    now = time.monotonic()
    if (
        _last_realtime is not None
        and _last_realtime_updated_monotonic is not None
        and (now - _last_realtime_updated_monotonic) < max_age_seconds
    ):
        logger.debug("Fox ESS realtime: cache hit (age %.1fs)", now - _last_realtime_updated_monotonic)
        return _last_realtime

    logger.info("Fox ESS realtime: fetching from API (cache miss or expired)")
    client = _get_client()
    data = client.get_realtime()
    _last_realtime = data
    _last_realtime_updated_monotonic = now
    _record_realtime_refresh()
    logger.debug("Fox ESS realtime: soc=%.1f solar=%.2f grid=%.2f", data.soc, data.solar_power, data.grid_power)
    return data


def get_cached_energy_today(max_age_seconds: int = 300) -> dict:
    """Return today's energy summary from cache if fresh, else fetch and update.

    Raises ValueError if Fox ESS is not configured.
    Raises FoxESSError on API errors.
    """
    global _last_energy_today, _last_energy_today_updated_monotonic
    now = time.monotonic()
    if (
        _last_energy_today is not None
        and _last_energy_today_updated_monotonic is not None
        and (now - _last_energy_today_updated_monotonic) < max_age_seconds
    ):
        return _last_energy_today

    client = _get_client()
    data = client.get_energy_today()
    _last_energy_today = data
    _last_energy_today_updated_monotonic = now
    return data
