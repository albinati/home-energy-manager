"""Caching layer for Fox ESS realtime and energy data.

Tracks all HTTP calls (realtime, energy, scheduler reads/writes) against the
daily quota (FOX_DAILY_BUDGET, default 1200 to stay 15% under the 1440 API
limit).  When the quota is exhausted the last cached value is returned with
``stale=True`` instead of making a new HTTP call.

The 24-h refresh counter uses api_quota.record_call() so it persists across
restarts and counts *all* Fox HTTP types, not just realtime reads.
"""
import logging
import time
from typing import Optional, Tuple

from ..config import config
from .client import FoxESSClient, FoxESSError
from .models import RealTimeData
from ..api_quota import record_call, should_block, get_quota_status

logger = logging.getLogger(__name__)

# Cached realtime snapshot and timestamps
_last_realtime: Optional[RealTimeData] = None
_last_realtime_updated_monotonic: Optional[float] = None
_last_realtime_wallclock: Optional[float] = None  # epoch seconds

# Legacy: in-memory 24h window kept for backward-compat with get_refresh_stats()
# (api_quota now persists to DB, but we keep this so callers don't break)
_refresh_timestamps: list[float] = []

# Cached daily energy summary
_last_energy_today: Optional[dict] = None
_last_energy_today_updated_monotonic: Optional[float] = None

# Cached monthly energy: (year, month) -> (data, updated_monotonic)
_energy_month_cache: dict[tuple[int, int], tuple[dict, float]] = {}
_ENERGY_MONTH_CACHE_TTL_SECONDS = 3600  # 1 hour

# Per-actor cooldown map for force-refresh
_force_refresh_timestamps: dict[str, float] = {}

# Track when we last blocked (for dashboard)
_last_blocked_at: Optional[float] = None


def _get_client() -> FoxESSClient:
    """Build FoxESSClient from config. Raises ValueError if not configured."""
    return FoxESSClient(**config.foxess_client_kwargs())


def _record_realtime_refresh() -> None:
    """Record a successful realtime fetch in both legacy window and quota tracker."""
    global _refresh_timestamps, _last_realtime_wallclock
    now_wall = time.time()
    _last_realtime_wallclock = now_wall
    cutoff = now_wall - 24 * 3600
    _refresh_timestamps = [t for t in _refresh_timestamps if t >= cutoff]
    _refresh_timestamps.append(now_wall)
    record_call("fox", "read", ok=True)


def get_refresh_stats() -> Tuple[Optional[float], int]:
    """Return (last_updated_epoch, refresh_count_in_last_24h).

    last_updated_epoch is wall-clock seconds since epoch (UTC-neutral).
    Returns (None, 0) if we have never successfully hit the Open API.
    Includes quota info in extended form; plain tuple kept for backward compat.
    """
    global _refresh_timestamps, _last_realtime_wallclock
    if _last_realtime_wallclock is None:
        return None, 0
    now_wall = time.time()
    cutoff = now_wall - 24 * 3600
    _refresh_timestamps = [t for t in _refresh_timestamps if t >= cutoff]
    return _last_realtime_wallclock, len(_refresh_timestamps)


def get_refresh_stats_extended() -> dict:
    """Return extended stats including quota for dashboard / status endpoints."""
    last_wall, count_24h = get_refresh_stats()
    qst = get_quota_status("fox")
    return {
        "last_updated_epoch": last_wall,
        "refresh_count_24h": count_24h,
        "quota_used_24h": qst["quota_used_24h"],
        "quota_remaining_24h": qst["quota_remaining_24h"],
        "daily_budget": qst["daily_budget"],
        "blocked": qst["blocked"],
        "last_blocked_at": _last_blocked_at,
        "cache_age_seconds": (
            None
            if _last_realtime_updated_monotonic is None
            else round(time.monotonic() - _last_realtime_updated_monotonic, 1)
        ),
        "stale": _last_realtime is not None and (
            _last_realtime_updated_monotonic is None
            or (time.monotonic() - _last_realtime_updated_monotonic)
            >= config.FOX_REALTIME_CACHE_TTL_SECONDS
        ),
    }


def get_cached_realtime(max_age_seconds: Optional[int] = None) -> RealTimeData:
    """Return realtime data from cache if fresh, else fetch from Fox ESS.

    Default TTL is now FOX_REALTIME_CACHE_TTL_SECONDS (default 300 s, was 30 s).
    When the quota is exhausted and a refresh would be needed, returns the last
    cached value (stale) rather than raising.

    Raises ValueError if Fox ESS is not configured.
    Raises FoxESSError on API errors (when no cache is available).
    """
    global _last_realtime, _last_realtime_updated_monotonic, _last_blocked_at
    if max_age_seconds is None:
        max_age_seconds = config.FOX_REALTIME_CACHE_TTL_SECONDS

    now = time.monotonic()
    if (
        _last_realtime is not None
        and _last_realtime_updated_monotonic is not None
        and (now - _last_realtime_updated_monotonic) < max_age_seconds
    ):
        logger.debug(
            "Fox ESS realtime: cache hit (age %.1fs)",
            now - _last_realtime_updated_monotonic,
        )
        return _last_realtime

    # Cache miss or expired — check quota before hitting the cloud
    if should_block("fox"):
        _last_blocked_at = time.time()
        if _last_realtime is not None:
            logger.warning(
                "Fox ESS daily quota exhausted; returning stale realtime cache"
            )
            return _last_realtime
        # No cache AND quota exhausted — this is a cold-start problem, try anyway
        logger.warning("Fox ESS quota exhausted and no cache; attempting cold fetch")

    logger.info("Fox ESS realtime: fetching from API (cache miss or expired)")
    client = _get_client()
    data = client.get_realtime()
    _last_realtime = data
    _last_realtime_updated_monotonic = now
    _record_realtime_refresh()
    logger.debug(
        "Fox ESS realtime: soc=%.1f solar=%.2f grid=%.2f",
        data.soc, data.solar_power, data.grid_power,
    )
    return data


def force_refresh_realtime(actor: str = "api") -> RealTimeData:
    """Explicitly refresh realtime data (e.g. user presses the Refresh button).

    Throttled by FOX_FORCE_REFRESH_MIN_INTERVAL_SECONDS per actor to prevent
    UI mash-clicking from eating into the daily quota.
    """
    global _last_realtime, _last_realtime_updated_monotonic, _last_blocked_at

    min_interval = config.FOX_FORCE_REFRESH_MIN_INTERVAL_SECONDS
    last = _force_refresh_timestamps.get(actor, 0.0)
    elapsed = time.time() - last

    if elapsed < min_interval:
        wait = min_interval - elapsed
        logger.info(
            "Fox force-refresh throttled for actor=%s (wait %.0fs)", actor, wait
        )
        if _last_realtime is not None:
            return _last_realtime
        raise FoxESSError("Fox realtime not yet available (cold start, throttled)")

    if should_block("fox"):
        _last_blocked_at = time.time()
        if _last_realtime is not None:
            logger.warning(
                "Fox ESS force-refresh blocked: daily quota exhausted (actor=%s)", actor
            )
            return _last_realtime
        raise FoxESSError("Fox ESS daily quota exhausted and no cached data available")

    _force_refresh_timestamps[actor] = time.time()
    logger.info("Fox ESS force-refresh (actor=%s)", actor)
    client = _get_client()
    data = client.get_realtime()
    _last_realtime = data
    _last_realtime_updated_monotonic = time.monotonic()
    _record_realtime_refresh()
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
    try:
        data = client.get_energy_today()
        record_call("fox", "read", ok=True)
    except Exception:
        record_call("fox", "read", ok=False)
        raise
    _last_energy_today = data
    _last_energy_today_updated_monotonic = now
    return data


def get_cached_energy_month(
    year: int, month: int, max_age_seconds: int = _ENERGY_MONTH_CACHE_TTL_SECONDS
) -> dict:
    """Return monthly energy summary from cache if fresh, else fetch and update.

    Raises ValueError if Fox ESS is not configured.
    Raises FoxESSError on API errors.
    """
    global _energy_month_cache
    now = time.monotonic()
    key = (year, month)
    if key in _energy_month_cache:
        data, updated = _energy_month_cache[key]
        if (now - updated) < max_age_seconds:
            logger.debug("Fox ESS energy month %04d-%02d: cache hit", year, month)
            return data
    client = _get_client()
    try:
        data = client.get_energy_month(year, month)
        record_call("fox", "read", ok=True)
    except Exception:
        record_call("fox", "read", ok=False)
        raise
    _energy_month_cache[key] = (data, now)
    return data
