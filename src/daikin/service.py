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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..api_quota import quota_remaining, record_call, should_block
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


def _do_refresh(actor: str) -> list[DaikinDevice]:
    """Call get_devices() and update cache. Quota accounting is at the transport layer (DaikinClient._get)."""
    global _devices_cache, _devices_fetched_monotonic, _devices_fetched_wall, _devices_stale
    global _cold_start_quota_logged
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
                # #55 — log the cold-start failure exactly once per boot instead
                # of every 2 minutes (the old heartbeat-loop behavior).
                global _cold_start_quota_logged
                if not _cold_start_quota_logged:
                    logger.warning(
                        "Daikin cold-start fetch failed: %s — service will use "
                        "the physics estimator until the quota window rolls over",
                        e,
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
            client = _get_or_create_client()
            daily = client.get_heating_daily_kwh(date_obj.year, date_obj.month)
            record_call("daikin", "read", ok=True)
            if daily:
                idx = date_obj.day - 1
                if 0 <= idx < len(daily):
                    val = float(daily[idx] or 0.0)
                    if val > 0:
                        kwh = val
                        source = "onecta"
    except Exception as exc:
        logger.debug("sync_daikin_daily Onecta path failed for %s: %s", iso, exc)
        record_call("daikin", "read", ok=False)

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


