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
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..api_quota import quota_remaining, should_block
from ..config import config
from .client import DaikinClient, DaikinError
from .models import DaikinDevice

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (singleton)
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_client: DaikinClient | None = None

_devices_cache: list[DaikinDevice] | None = None
_devices_fetched_monotonic: float | None = None
_devices_fetched_wall: float | None = None   # epoch seconds
_devices_stale: bool = False                    # True after write until next refresh

# Per-actor cooldown map for force-refresh: actor_key -> last epoch seconds
_force_refresh_timestamps: dict[str, float] = {}

# #55 — one-shot cold-start 429 log: prevents the 2-minute log-spam loop when
# Daikin quota is already exhausted at boot. Reset on first successful refresh.
_cold_start_quota_logged: bool = False
# Module-level backoff for cold-start failures. Without it, every UI poll
# (cockpit_now, weather, api etc., ~3/min combined) re-tries the Daikin
# API and burns 429s while the daily quota window remains exhausted —
# this caused the 2026-05-27 22:00 UTC retry storm (#423).
_cold_start_failed_at: float | None = None
_COLD_START_BACKOFF_SECONDS: int = 600  # 10 min — bounds API attempts to ≤6/h

# Anti-burst: a hard floor on how often a REAL Daikin device read can happen,
# regardless of caller. The read-storm (#423 follow-up) was a caller hitting the
# live-fetch path ~1s apart in bursts whenever quota freed up — a sawtooth that
# kept quota pinned at the cap. This makes bursts structurally impossible: a
# read within the window returns the warm cache instead of going to the wire.
_last_refresh_monotonic: float = 0.0
_refresh_throttle_logged: bool = False  # log the offending call stack once


# ---------------------------------------------------------------------------
# Public dataclass returned to callers
# ---------------------------------------------------------------------------
@dataclass
class CachedDevices:
    devices: list[DaikinDevice]
    fetched_at_wall: float | None  # epoch seconds, or None if cold-start
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


def _do_refresh(actor: str, *, force: bool = False) -> list[DaikinDevice]:
    """Call get_devices() and update cache. Quota accounting is at the transport layer (DaikinClient._get).

    ``force=True`` (explicit user-triggered refresh) bypasses the anti-burst
    interval floor — the auto paths (LP init etc.) read on their natural ~30 min
    cache cadence, and a deliberate "refresh now" should still get fresh data.

    Defensive guard: refuse to call Daikin when the soft cap is exhausted.
    Every caller higher up SHOULD already check should_block, but this
    extra check is cheap and stops any future bypass from silently
    burning quota on 429s (#423 belt-and-braces).
    """
    global _devices_cache, _devices_fetched_monotonic, _devices_fetched_wall, _devices_stale
    global _cold_start_quota_logged, _last_refresh_monotonic, _refresh_throttle_logged

    if should_block("daikin"):
        # Raising means callers fall through to their stale-cache / empty
        # branches without ever hitting the wire. Logging level kept low —
        # the cache layer's own messaging surfaces the user-facing state.
        logger.debug("_do_refresh blocked: daikin soft cap exhausted (actor=%s)", actor)
        raise DaikinError("Daikin daily quota exhausted")

    # Anti-burst floor: at most one real device read per
    # DAIKIN_REFRESH_MIN_INTERVAL_SECONDS, regardless of caller. A tight read
    # loop thus gets the warm cache instead of hammering the wire. Set BEFORE
    # the fetch so a failing read still holds the window. First call (cache
    # present) within the window logs the offending stack once, to pinpoint the
    # looping caller without a separate instrumentation deploy.
    now_m = time.monotonic()
    min_iv = float(getattr(config, "DAIKIN_REFRESH_MIN_INTERVAL_SECONDS", 90))
    if not force and _last_refresh_monotonic and (now_m - _last_refresh_monotonic) < min_iv and _devices_cache is not None:
        if not _refresh_throttle_logged:
            _refresh_throttle_logged = True
            logger.warning(
                "Daikin _do_refresh throttled (%.1fs since last read < %.0fs floor, actor=%s) "
                "— returning warm cache. Offending caller:\n%s",
                now_m - _last_refresh_monotonic, min_iv, actor,
                "".join(traceback.format_stack(limit=10)),
            )
        else:
            logger.debug("Daikin _do_refresh throttled (actor=%s) — warm cache", actor)
        return _devices_cache
    _last_refresh_monotonic = now_m

    client = _get_or_create_client()
    devices = client.get_devices()
    _devices_cache = devices
    _devices_fetched_monotonic = time.monotonic()
    _devices_fetched_wall = time.time()
    _devices_stale = False
    _cold_start_quota_logged = False  # clear once — next 429 can log afresh
    _persist_daikin_telemetry_live(devices)
    logger.info("Daikin devices refreshed by %s (%d device(s))", actor, len(devices))
    return devices


def _persist_daikin_telemetry_live(devices: list[DaikinDevice]) -> None:
    """Write a ``source='live'`` row to daikin_telemetry (#55) — seed for the
    physics estimator when the quota subsequently runs out. Best-effort; a
    failure here must never break the refresh path."""
    if not devices:
        return
    d0 = devices[0]
    try:
        from .. import db
        db.insert_daikin_telemetry({
            "fetched_at": time.time(),
            "source": "live",
            "tank_temp_c": d0.tank_temperature,
            "indoor_temp_c": getattr(d0.temperature, "room_temperature", None),
            "outdoor_temp_c": getattr(d0.temperature, "outdoor_temperature", None),
            "tank_target_c": d0.tank_target,
            "lwt_actual_c": d0.leaving_water_temperature,
            "mode": d0.operation_mode,
            "weather_regulation": 1 if d0.weather_regulation_enabled else 0,
        })
    except Exception as e:
        logger.debug("daikin_telemetry live persistence failed: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_cached_devices(
    *,
    allow_refresh: bool = False,
    max_age_seconds: int | None = None,
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
        # Cold-start: no cache at all.
        if _devices_cache is None:
            global _cold_start_quota_logged, _cold_start_failed_at

            now = time.time()
            backoff_active = (
                _cold_start_failed_at is not None
                and (now - _cold_start_failed_at) < _COLD_START_BACKOFF_SECONDS
            )

            # Belt-and-braces — if the local soft-cap says blocked, don't even
            # try the API. Daikin's hard daily limit is 200/day; once we're
            # over, every additional 429 contributes to nothing but the
            # bookkeeping count.
            quota_blocked = should_block("daikin")

            if backoff_active or quota_blocked:
                if not _cold_start_quota_logged:
                    if quota_blocked:
                        logger.warning(
                            "Daikin cold-start skipped — soft cap exhausted "
                            "(actor=%s); using physics estimator until reset",
                            actor,
                        )
                    else:
                        logger.warning(
                            "Daikin cold-start in backoff (actor=%s, %.0fs since "
                            "last failure) — skipping API call",
                            actor, now - (_cold_start_failed_at or now),
                        )
                    _cold_start_quota_logged = True
                else:
                    logger.debug("Daikin cold-start skipped (suppressed, actor=%s)", actor)
                # Record the skip so subsequent callers re-hit the backoff
                # check, not the API.
                if not backoff_active:
                    _cold_start_failed_at = now
                return CachedDevices(
                    devices=[],
                    fetched_at_wall=None,
                    age_seconds=float("inf"),
                    stale=True,
                    source="cold_start_backoff",
                )

            logger.info("Daikin service: cold-start fetch (actor=%s)", actor)
            try:
                devices = _do_refresh(actor)
                _cold_start_failed_at = None  # success clears the backoff
                return CachedDevices(
                    devices=devices,
                    fetched_at_wall=_devices_fetched_wall,
                    age_seconds=0.0,
                    stale=False,
                    source="cold_start",
                )
            except Exception as e:
                # On failure, set the backoff so we don't retry on the next UI
                # poll (~20 s). Log once until the next successful refresh.
                _cold_start_failed_at = now
                if not _cold_start_quota_logged:
                    logger.warning(
                        "Daikin cold-start fetch failed: %s — service will use "
                        "the physics estimator; next attempt in %ds",
                        e, _COLD_START_BACKOFF_SECONDS,
                    )
                    _cold_start_quota_logged = True
                else:
                    logger.debug("Daikin cold-start fetch failed (suppressed): %s", e)
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
    min_interval_seconds: int | None = None,
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
            devices = _do_refresh(actor, force=True)
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
    force_iv = int(config.DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS)
    with _lock:
        age = _cache_age_seconds()
        qst = _qs("daikin")
        # Remaining cooldown for the UI's manual "force refresh" (actor="api"),
        # so the button can lock + count down in lock-step with the server.
        last_api_force = _force_refresh_timestamps.get("api", 0.0)
        elapsed = time.time() - last_api_force if last_api_force else force_iv
        force_available_in = max(0.0, force_iv - elapsed)
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
        # Surfaced so the UI shows the heating lock/active state even when
        # device telemetry is cold (quota blocked → /daikin/status empty).
        "control_mode": config.DAIKIN_CONTROL_MODE,
        # Manual force-refresh cooldown (UI lock).
        "force_refresh_min_interval_seconds": force_iv,
        "force_refresh_available_in_seconds": round(force_available_in, 1),
        **qst,
    }


# ---------------------------------------------------------------------------
# LP-initial-state wrapper (cache → live → physics estimator). #55
# ---------------------------------------------------------------------------


def get_lp_state_cached_or_estimated(
    *,
    actor: str = "lp_init",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Return tank/indoor state preferring live, falling back to the physics
    estimator when Daikin quota is exhausted (#55).

    Flow:
      1. Fresh row in ``daikin_telemetry`` (within
         ``DAIKIN_TELEMETRY_MAX_STALENESS_SECONDS``) → return as ``source='live'``.
      2. Quota has headroom → live fetch via ``get_cached_devices`` (which also
         writes a fresh ``source='live'`` row via ``_do_refresh``), return.
      3. Quota exhausted → walk the physics estimator forward from the last
         ``source='live'`` row, persist as ``source='estimate'``, return.

    Never raises. When nothing at all is available, returns ``source='degraded'``
    with ``None`` temps so the LP falls back to config defaults without crashing.
    The return shape is deliberately narrower than ``CachedDevices`` — this
    wrapper is LP-focused, not a general device-state accessor.
    """
    from .. import db
    from .estimator import estimate_state

    if now_utc is None:
        now_utc = datetime.now(UTC)

    max_staleness = int(config.DAIKIN_TELEMETRY_MAX_STALENESS_SECONDS)

    latest_live = db.get_latest_daikin_telemetry(source="live")
    if latest_live is not None:
        age = now_utc.timestamp() - float(latest_live["fetched_at"])
        if 0 <= age <= max_staleness:
            logger.debug(
                "Daikin LP state: live cache hit (age=%.0fs, actor=%s)", age, actor
            )
            return {
                "tank_temp_c": latest_live.get("tank_temp_c"),
                "indoor_temp_c": latest_live.get("indoor_temp_c"),
                "outdoor_temp_c": latest_live.get("outdoor_temp_c"),
                "source": "live",
                "age_seconds": round(age, 1),
            }

    if not should_block("daikin"):
        try:
            result = get_cached_devices(allow_refresh=True, actor=actor)
            if result.devices:
                d0 = result.devices[0]
                return {
                    "tank_temp_c": d0.tank_temperature,
                    "indoor_temp_c": getattr(d0.temperature, "room_temperature", None),
                    "outdoor_temp_c": getattr(d0.temperature, "outdoor_temperature", None),
                    "source": "live",
                    "age_seconds": round(result.age_seconds, 1),
                }
        except Exception as e:
            logger.warning("Daikin live fetch failed in LP wrapper: %s", e)

    if latest_live is None:
        logger.warning(
            "Daikin LP state: no seed row and quota exhausted — "
            "LP will fall back to config defaults"
        )
        return {
            "tank_temp_c": None,
            "indoor_temp_c": None,
            "outdoor_temp_c": None,
            "source": "degraded",
            "age_seconds": None,
        }

    try:
        try:
            meteo_rows = db.get_meteo_forecast(now_utc.date().isoformat())
        except Exception:
            meteo_rows = None
        est = estimate_state(latest_live, now_utc, meteo_rows=meteo_rows)
        db.insert_daikin_telemetry({
            "fetched_at": now_utc.timestamp(),
            "source": "estimate",
            "tank_temp_c": est.tank_temp_c,
            "indoor_temp_c": est.indoor_temp_c,
            "outdoor_temp_c": est.outdoor_temp_c,
        })
        logger.info(
            "Daikin LP state: estimator fallback "
            "(seed age=%.0fs, tank≈%.1f°C, indoor≈%.1f°C, actor=%s)",
            est.seed_age_seconds,
            est.tank_temp_c,
            est.indoor_temp_c,
            actor,
        )
        return {
            "tank_temp_c": est.tank_temp_c,
            "indoor_temp_c": est.indoor_temp_c,
            "outdoor_temp_c": est.outdoor_temp_c,
            "source": "estimate",
            "age_seconds": round(est.seed_age_seconds, 1),
        }
    except Exception as e:
        logger.warning("Daikin estimator failed (%s) — returning stale live row", e)
        return {
            "tank_temp_c": latest_live.get("tank_temp_c"),
            "indoor_temp_c": latest_live.get("indoor_temp_c"),
            "outdoor_temp_c": latest_live.get("outdoor_temp_c"),
            "source": "stale",
            "age_seconds": round(
                now_utc.timestamp() - float(latest_live["fetched_at"]), 1
            ),
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


def _passive_guard(action: str) -> None:
    """Raise DaikinError when DAIKIN_CONTROL_MODE=passive — blocks every writer.

    See plan: passive mode means zero outbound writes from this service.
    Telemetry (read paths) is unaffected. Flip DAIKIN_CONTROL_MODE=active to allow.
    """
    if config.DAIKIN_CONTROL_MODE == "passive":
        raise DaikinError(f"DAIKIN_CONTROL_MODE=passive — cannot {action}")


def set_power(on: bool, actor: str = "api") -> None:
    _passive_guard("set power")
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set power")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_power(dev, on)
    invalidate_after_write()


def set_temperature(temperature: float, mode: str = "heating", actor: str = "api") -> None:
    _passive_guard("set temperature")
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set temperature")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_temperature(dev, temperature, mode)
    invalidate_after_write()


def set_lwt_offset(offset: float, mode: str = "heating", actor: str = "api") -> None:
    _passive_guard("set LWT offset")
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set LWT offset")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_lwt_offset(dev, offset, mode)
    invalidate_after_write()


def set_operation_mode(mode_str: str, actor: str = "api") -> None:
    _passive_guard("set mode")
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set mode")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_operation_mode(dev, mode_str)
    invalidate_after_write()


def set_tank_temperature(temperature: float, actor: str = "api") -> None:
    _passive_guard("set tank temperature")
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set tank temperature")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_tank_temperature(dev, temperature)
    invalidate_after_write()


def set_tank_power(on: bool, actor: str = "api") -> None:
    _passive_guard("set tank power")
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set tank power")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_tank_power(dev, on)
    invalidate_after_write()


def set_tank_powerful(on: bool, actor: str = "api") -> None:
    _passive_guard("set tank powerful mode")
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set tank powerful mode")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_tank_powerful(dev, on)
    invalidate_after_write()


def set_weather_regulation(enabled: bool, actor: str = "api") -> None:
    _passive_guard("set weather regulation")
    if should_block("daikin"):
        raise DaikinError("Daikin daily quota exhausted — cannot set weather regulation")
    client, devices = _require_client_and_devices(actor)
    for dev in devices:
        client.set_weather_regulation(dev, enabled)
    invalidate_after_write()


# ---------------------------------------------------------------------------
# v10.2 — daikin_consumption_daily sync (deferred Epic #70 minimal first cut)
# ---------------------------------------------------------------------------

def heating_consumption_kwh(year: int, month: int, *, actor: str = "energy_insights") -> float | None:
    """Monthly heating kWh, read from the CACHED device payload (30-min TTL) —
    the consumption figures live inside the gateway-devices response, so this
    never needs its own wire read when the cache is warm. This is the fix for
    the energy-insights read-burst: ``/energy/period`` etc. previously spun up a
    fresh client and hit ``get_devices()`` on every call."""
    result = get_cached_devices(allow_refresh=True, actor=actor)
    if not result.devices:
        return None
    return _get_or_create_client().get_heating_consumption_kwh(year, month, devices=result.devices)


def heating_daily_kwh(year: int, month: int, *, actor: str = "energy_insights") -> list[float] | None:
    """Per-day heating kWh for the month, from the cached device payload. See
    :func:`heating_consumption_kwh`."""
    result = get_cached_devices(allow_refresh=True, actor=actor)
    if not result.devices:
        return None
    return _get_or_create_client().get_heating_daily_kwh(year, month, devices=result.devices)


def sync_daikin_daily(date_obj) -> dict | None:
    """Populate ``daikin_consumption_daily`` for the given date.

    Strategy (in order):
      1. **Onecta** ``get_heating_daily_kwh(year, month)`` returns 28-31 daily
         values; pick the date's value if present and > 0.
      2. **Telemetry integral**: integrate ``daikin_telemetry`` rows for the
         date through ``physics.get_daikin_heating_kw(outdoor_c)`` over each
         ~2-min sample's interval. Honest fallback when Onecta unhelpful.

    Records ``source`` so the UI can show "from cloud" vs "estimated".
    Returns the upserted row dict, or None if both paths fail.
    Quota-aware: Onecta path counted via ``api_quota.record_call``.
    """
    from datetime import date as _date

    from .. import db as _db

    if not isinstance(date_obj, _date):
        raise ValueError("date_obj must be a datetime.date")
    iso = date_obj.isoformat()

    # Path 1: Onecta daily breakdown
    kwh = None
    source = "unknown"
    try:
        if not should_block("daikin"):
            # Cached read — the transport records its own quota on a real fetch,
            # so no manual record_call here (that double-counted), and a warm
            # cache means zero extra reads.
            daily = heating_daily_kwh(date_obj.year, date_obj.month, actor="daikin_daily_sync")
            if daily:
                idx = date_obj.day - 1
                if 0 <= idx < len(daily):
                    val = float(daily[idx] or 0.0)
                    if val > 0:
                        kwh = val
                        source = "onecta"
    except Exception as exc:
        logger.debug("sync_daikin_daily Onecta path failed for %s: %s", iso, exc)

    # Path 2: telemetry integral fallback. daikin_telemetry.fetched_at is
    # stored as epoch seconds (float), so bound the range in epoch.
    if kwh is None:
        try:
            from datetime import datetime as _dt, time as _time
            from ..physics import get_daikin_heating_kw

            day_start_epoch = _dt.combine(date_obj, _time(0, 0)).replace(
                tzinfo=UTC
            ).timestamp()
            day_end_epoch = day_start_epoch + 86400

            with _db._lock:
                conn = _db.get_connection()
                try:
                    cur = conn.execute(
                        "SELECT fetched_at, outdoor_temp_c FROM daikin_telemetry "
                        "WHERE fetched_at >= ? AND fetched_at < ? "
                        "ORDER BY fetched_at ASC",
                        (day_start_epoch, day_end_epoch),
                    )
                    rows = [dict(r) for r in cur.fetchall()]
                finally:
                    conn.close()
            if rows:
                # Trapezoidal-ish integral: weight each tick's load_kw by half
                # the gap to its neighbours. Cheap and good enough for an
                # historic estimate.
                total_kwh = 0.0
                for i, r in enumerate(rows):
                    out = r.get("outdoor_temp_c")
                    if out is None:
                        continue
                    load_kw = float(get_daikin_heating_kw(out))
                    # Time slice in hours: half the gap to prev + half to next
                    if i == 0 and len(rows) > 1:
                        slice_h = max(0.0, (float(rows[1]["fetched_at"]) - float(rows[0]["fetched_at"])) / 3600.0 / 2.0)
                    elif i == len(rows) - 1:
                        slice_h = max(0.0, (float(rows[-1]["fetched_at"]) - float(rows[-2]["fetched_at"])) / 3600.0 / 2.0)
                    else:
                        slice_h = max(0.0, (float(rows[i + 1]["fetched_at"]) - float(rows[i - 1]["fetched_at"])) / 3600.0 / 2.0)
                    total_kwh += load_kw * slice_h
                if total_kwh > 0:
                    kwh = round(total_kwh, 3)
                    source = "telemetry_integral"
        except Exception as exc:
            logger.debug("sync_daikin_daily telemetry-integral path failed for %s: %s", iso, exc)

    if kwh is None:
        return None

    _db.upsert_daikin_consumption_daily(
        date=iso, kwh_total=kwh, kwh_heating=kwh, kwh_dhw=None, cop_daily=None, source=source,
    )
    return _db.get_daikin_consumption_daily_by_date(iso)


# ---------------------------------------------------------------------------
# 2-hourly telemetry-integral fallback for daikin_consumption_2hourly (#425).
#
# Why: Onecta's public ``consumptionData.value.electrical.<mode>.d`` array
# returns INTEGER kWh per 2-hour bucket. That truncates everything below 1
# kWh to zero — a typical 0.6 kWh DHW reheat cycle shows up as "0", which
# makes the home Energy-chart Daikin breakdown look flat in summer. The
# mobile app sees sub-integer numbers because it talks to a private endpoint
# we don't have access to. Telemetry integration is our legitimate path to
# the same precision.
#
# Inputs (all already in SQLite, zero Daikin API quota):
#   * ``daikin_telemetry`` rows (~every 30 min): outdoor temp, tank temp,
#     LWT, mode. ``fetched_at`` is epoch seconds.
#   * ``DAIKIN_COP_CURVE`` + ``COP_DHW_PENALTY`` from config.
#   * ``DHW_TANK_LITRES`` for the thermal-mass calc.
#   * ``physics.get_daikin_heating_kw(outdoor)`` for the space-heating draw.
#
# Algorithm: between each pair of consecutive telemetry samples,
#   space_kwh = get_daikin_heating_kw(midpoint_outdoor) × dt
#     (already returns 0 above the weather-curve cutoff)
#   dhw_kwh  = max(0, tank_temp_delta) × m_water × c_water / cop_dhw
#     (only positive ramps count — a cooling tank is loss, not consumption)
# Both kWh fall into the local-time 2h bucket containing the midpoint.
# ---------------------------------------------------------------------------

# Thermal mass calc constants — c_water in kWh/(kg·K). Tank water mass ≈
# DHW_TANK_LITRES kg (1 L ≈ 1 kg for water).
_C_WATER_KWH_PER_KG_K = 0.001163  # = 4.186 kJ/(kg·K) / 3600

# Tiny tank delta filter — within sensor noise / 0.1 °C resolution, ignore.
_TANK_DELTA_NOISE_C = 0.15

# Standing-loss model — a well-insulated 200 L cylinder loses roughly 1.5 kWh
# of thermal energy per day at 45 °C tank vs 21 °C room (consistent with the
# 1.5–2 kWh manufacturer EN 12897 figures for class C cylinders). Per-K, per-
# litre, per-hour: 1.5 / (200 × (45-21) × 24) ≈ 1.3e-5 kWh.
_TANK_STANDING_LOSS_KWH_PER_K_L_H = 1.3e-5
# Room temperature reference for the standing-loss ΔT. Telemetry has
# indoor_temp_c when available — we fall back to this value when not.
_TANK_ROOM_TEMP_REF_C = 21.0


def compute_daikin_2hourly_telemetry(
    date_obj,
    local_tz: str | None = None,
) -> dict[int, dict[str, float]]:
    """Integrate ``daikin_telemetry`` rows into 12 × 2h buckets for *date_obj*.

    Returns ``{bucket_idx: {"kwh_heating", "kwh_dhw", "kwh_total"}}`` keyed by
    the local 2-hour bucket (0 = local 00:00–02:00, 11 = 22:00–24:00, same
    convention as the Onecta cache writer). Buckets with no telemetry are
    omitted.

    Pure read; nothing is written to the DB here — see
    :func:`sync_daikin_2hourly_telemetry` for the upsert side.
    """
    from datetime import date as _date, datetime as _dt, time as _time, timedelta as _td
    from zoneinfo import ZoneInfo

    from .. import db as _db
    from ..config import cop_at_temperature
    from ..physics import get_daikin_heating_kw

    if not isinstance(date_obj, _date):
        raise ValueError("date_obj must be a datetime.date")

    tz_name = local_tz or getattr(config, "BULLETPROOF_TIMEZONE", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    # Local midnight of the requested date, expressed in epoch seconds — this
    # is the same anchor the Onecta cache writer uses (12 buckets starting at
    # local midnight).
    day_start_local = _dt.combine(date_obj, _time(0, 0)).replace(tzinfo=tz)
    day_start_epoch = day_start_local.timestamp()
    day_end_epoch = day_start_epoch + 24 * 3600

    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """SELECT fetched_at, outdoor_temp_c, tank_temp_c, lwt_actual_c, mode
                   FROM daikin_telemetry
                   WHERE fetched_at >= ? AND fetched_at < ?
                   ORDER BY fetched_at ASC""",
                (day_start_epoch, day_end_epoch),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    if len(rows) < 2:
        return {}

    tank_litres = float(getattr(config, "DHW_TANK_LITRES", 200))
    cop_curve = config.DAIKIN_COP_CURVE
    dhw_penalty = float(getattr(config, "COP_DHW_PENALTY", 0.5))

    buckets: dict[int, dict[str, float]] = {}

    def _bucket_of(epoch_s: float) -> int:
        local_dt = _dt.fromtimestamp(epoch_s, tz=tz)
        # Same local-date check — drop samples that round into a different
        # day when local TZ wraps. This can happen with one stray row at the
        # boundary; clamp to nearest bucket within [0, 11].
        if local_dt.date() != date_obj:
            return -1
        return min(11, max(0, local_dt.hour // 2))

    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        dt_h = (float(b["fetched_at"]) - float(a["fetched_at"])) / 3600.0
        # Skip suspicious gaps — telemetry usually fires ~30 min. A 2h+ gap
        # likely means the service was down; don't extrapolate over a void.
        if dt_h <= 0 or dt_h > 2.0:
            continue

        # Midpoint outdoor temp — falls back if either sample is None
        outs = [v for v in (a.get("outdoor_temp_c"), b.get("outdoor_temp_c")) if v is not None]
        mid_outdoor = sum(outs) / len(outs) if outs else None

        # Space heating: physics function already returns 0 above the
        # weather-curve cutoff, so it naturally handles "summer day,
        # space heating off". When mode is known to be off, force 0 to
        # avoid charging stand-by losses to the heat pump.
        space_kwh = 0.0
        if mid_outdoor is not None:
            mode_a = (a.get("mode") or "").lower()
            mode_b = (b.get("mode") or "").lower()
            mode_off = (mode_a in ("off", "standby")) and (mode_b in ("off", "standby"))
            if not mode_off:
                space_kw = float(get_daikin_heating_kw(mid_outdoor))
                space_kwh = space_kw * dt_h

        # DHW: positive tank-temp ramp + standing-loss replacement (the heat
        # pump fights losses even when the observed tank temp is flat).
        # Without the loss term we undercount by ~50-70 % vs Onecta because
        # most "DHW heating" replaces continuous loss to the room, not the
        # comparatively-rare reheat events.
        dhw_kwh = 0.0
        ta, tb = a.get("tank_temp_c"), b.get("tank_temp_c")
        if ta is not None and tb is not None:
            mid_tank = (float(ta) + float(tb)) / 2.0
            room_temp = a.get("indoor_temp_c") or b.get("indoor_temp_c") or _TANK_ROOM_TEMP_REF_C
            dt_room = max(0.0, mid_tank - float(room_temp))
            # Standing loss component — always present (heat leaks 24/7).
            loss_thermal_kwh = (
                _TANK_STANDING_LOSS_KWH_PER_K_L_H * dt_room * tank_litres * dt_h
            )
            # Ramp component — explicit ΔT × m × c when tank climbs.
            delta_c = float(tb) - float(ta)
            ramp_thermal_kwh = (
                tank_litres * _C_WATER_KWH_PER_KG_K * delta_c
                if delta_c > _TANK_DELTA_NOISE_C else 0.0
            )
            thermal_kwh = ramp_thermal_kwh + loss_thermal_kwh
            if thermal_kwh > 0:
                # DHW COP = space COP × penalty (0.5 default — DHW supply
                # LWT is higher than space heat, so COP is worse).
                space_cop = cop_at_temperature(cop_curve, mid_outdoor) if mid_outdoor is not None else 3.0
                dhw_cop = max(1.5, space_cop * dhw_penalty)
                dhw_kwh = thermal_kwh / dhw_cop

        mid_epoch = (float(a["fetched_at"]) + float(b["fetched_at"])) / 2.0
        bi = _bucket_of(mid_epoch)
        if bi < 0:
            continue
        slot = buckets.setdefault(bi, {"kwh_heating": 0.0, "kwh_dhw": 0.0})
        slot["kwh_heating"] += space_kwh
        slot["kwh_dhw"] += dhw_kwh

    for slot in buckets.values():
        slot["kwh_heating"] = round(slot["kwh_heating"], 3)
        slot["kwh_dhw"] = round(slot["kwh_dhw"], 3)
        slot["kwh_total"] = round(slot["kwh_heating"] + slot["kwh_dhw"], 3)

    return buckets


def sync_daikin_2hourly_telemetry(date_obj) -> dict:
    """Compute telemetry-integral 2h buckets for *date_obj* and upsert into
    ``daikin_consumption_2hourly`` with ``source='telemetry_integral'``.

    Reconciliation with existing Onecta rows:
      * If no row exists for ``(date, bucket_idx)`` → write telemetry value.
      * If existing row is ``source='onecta_cache'`` and its integer kwh_total
        equals 0 while telemetry shows > 0.2 kWh → write telemetry value
        (Onecta missed sub-integer activity, which is the whole point).
      * If existing row is ``onecta_cache`` and the integer differs from
        telemetry by less than 0.5 kWh → write telemetry value (refines
        the rounded integer to a real decimal).
      * If existing row is ``onecta_cache`` and disagrees by ≥ 0.5 kWh →
        leave Onecta in place (it's measured at the inverter; trust it).
      * If existing row is already ``telemetry_integral`` → overwrite
        unconditionally (re-running the integral with more telemetry).

    Returns ``{"written": N, "skipped": M, "buckets": [...]}``.
    """
    from datetime import date as _date

    from .. import db as _db

    if not isinstance(date_obj, _date):
        raise ValueError("date_obj must be a datetime.date")

    buckets = compute_daikin_2hourly_telemetry(date_obj)
    if not buckets:
        return {"written": 0, "skipped": 0, "buckets": []}

    iso = date_obj.isoformat()
    written = 0
    skipped = 0
    detail: list[dict] = []

    with _db._lock:
        conn = _db.get_connection()
        try:
            existing_rows = conn.execute(
                """SELECT bucket_idx, kwh_total, source
                   FROM daikin_consumption_2hourly WHERE date = ?""",
                (iso,),
            ).fetchall()
            existing = {int(r[0]): {"kwh_total": float(r[1] or 0), "source": r[2]}
                        for r in existing_rows}
        finally:
            conn.close()

    for bi, slot in buckets.items():
        tele_total = slot["kwh_total"]
        ex = existing.get(bi)
        write = False
        reason = ""
        if ex is None:
            write = True
            reason = "no_existing_row"
        elif ex["source"] == "telemetry_integral":
            write = True
            reason = "refresh_telemetry"
        elif ex["source"] == "onecta_cache":
            onecta_int = ex["kwh_total"]
            if onecta_int == 0 and tele_total > 0.2:
                write = True
                reason = "onecta_zero_telemetry_positive"
            elif abs(onecta_int - tele_total) < 0.5:
                write = True
                reason = "refine_integer_to_decimal"
            else:
                reason = f"onecta_authoritative (int={onecta_int}, tele={tele_total})"
        else:
            write = True
            reason = "unknown_source_overwrite"

        if write:
            _db.upsert_daikin_consumption_2hourly(
                date=iso, bucket_idx=bi,
                kwh_total=tele_total,
                kwh_heating=slot["kwh_heating"],
                kwh_dhw=slot["kwh_dhw"],
                source="telemetry_integral",
            )
            written += 1
        else:
            skipped += 1
        detail.append({"bucket_idx": bi, **slot, "decision": reason})

    return {"written": written, "skipped": skipped, "buckets": detail}


