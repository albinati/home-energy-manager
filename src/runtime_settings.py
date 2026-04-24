"""Runtime-tunable settings layer (#52).

Callers read settings via ``get_setting(key)`` which returns the coerced value
(float/int/str/list) from a 30-sec TTL + version-counter cache. Cache misses
hit SQLite (``runtime_settings`` table); absent rows fall back to the
env-derived default declared in :data:`SCHEMA`. Writes happen only via
``set_setting`` — validation, persistence, and cache invalidation are atomic.

Design choices:
  * **Schema-driven**: every tunable has an entry in :data:`SCHEMA` — no silent
    extension. A PUT for an unknown key returns 400.
  * **Env defaults via lambda**: the ``env_default`` callable is re-evaluated
    only on first read (or after a ``delete_setting``), so env changes after
    process start do **not** retroactively shift the default. Matches the
    "zero-risk rollback" behavior in the issue — delete the row, env reasserts.
  * **TTL + version**: single-process hot paths short-circuit on version match
    (O(1) dict lookup). The 30-sec TTL is a belt-and-braces floor so
    out-of-band writes (a human typing ``UPDATE runtime_settings ...``) get
    picked up without a restart.
  * **Cron hot-reload side effect**: settings whose change requires
    APScheduler job re-registration are tagged ``cron_reload=True``; the PUT
    handler calls ``scheduler.runner.reregister_cron_jobs(reason)`` after
    persistence. All other keys take effect on the next cache-miss read.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import db

logger = logging.getLogger(__name__)


class SettingValidationError(ValueError):
    """Raised by :func:`set_setting` when the new value fails schema validation."""


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type_name: str  # "float" | "int" | "str" | "list[int]"
    env_default: Callable[[], Any]
    min_value: float | None = None
    max_value: float | None = None
    enum: tuple[str, ...] | None = None
    cron_reload: bool = False
    description: str = ""


def _float_env(name: str, default: str) -> Callable[[], float]:
    return lambda: float(os.getenv(name, default))


def _int_env(name: str, default: str) -> Callable[[], int]:
    return lambda: int(os.getenv(name, default))


def _str_env(name: str, default: str) -> Callable[[], str]:
    return lambda: (os.getenv(name) or default).strip().lower()


def _int_list_env(name: str, default: str) -> Callable[[], list[int]]:
    def _load() -> list[int]:
        raw = (os.getenv(name) or default).strip()
        if not raw:
            return []
        return sorted({int(p.strip()) for p in raw.split(",") if p.strip()})
    return _load


SCHEMA: dict[str, SettingSpec] = {
    # DHW comfort knobs — user-tunable per season / presence.
    "DHW_TEMP_COMFORT_C": SettingSpec(
        key="DHW_TEMP_COMFORT_C",
        type_name="float",
        env_default=_float_env("DHW_TEMP_COMFORT_C", "48"),
        min_value=40.0,
        max_value=65.0,
        description="Tank target when negative-price plunge fills headroom (°C).",
    ),
    "DHW_TEMP_NORMAL_C": SettingSpec(
        key="DHW_TEMP_NORMAL_C",
        type_name="float",
        env_default=_float_env("DHW_TEMP_NORMAL_C", "50"),
        min_value=40.0,
        max_value=65.0,
        description="Restore / safe-default tank target (°C).",
    ),
    "INDOOR_SETPOINT_C": SettingSpec(
        key="INDOOR_SETPOINT_C",
        type_name="float",
        env_default=_float_env("INDOOR_SETPOINT_C", "21"),
        min_value=16.0,
        max_value=26.0,
        description="Indoor comfort setpoint (°C).",
    ),
    # Strategy switches.
    "OPTIMIZATION_PRESET": SettingSpec(
        key="OPTIMIZATION_PRESET",
        type_name="str",
        env_default=_str_env("OPTIMIZATION_PRESET", "normal"),
        enum=("normal", "guests", "travel", "away"),
        description="Occupancy preset — affects DHW floor and peak-export gates.",
    ),
    "ENERGY_STRATEGY_MODE": SettingSpec(
        key="ENERGY_STRATEGY_MODE",
        type_name="str",
        env_default=_str_env("ENERGY_STRATEGY_MODE", "savings_first"),
        enum=("savings_first", "strict_savings"),
        description="savings_first allows peak-export discharge; strict_savings never does.",
    ),
    "DAIKIN_CONTROL_MODE": SettingSpec(
        key="DAIKIN_CONTROL_MODE",
        type_name="str",
        env_default=_str_env("DAIKIN_CONTROL_MODE", "passive"),
        enum=("passive", "active"),
        description=(
            "passive = service never writes to Daikin (firmware autonomous; "
            "treated as fixed thermal load by LP). active = legacy v9 control."
        ),
    ),
    "REQUIRE_SIMULATION_ID": SettingSpec(
        key="REQUIRE_SIMULATION_ID",
        type_name="str",  # "true" / "false" — kept as str so PUT payloads stay simple
        env_default=_str_env("REQUIRE_SIMULATION_ID", "false"),
        enum=("true", "false"),
        description=(
            "v10.1 cockpit: when 'true', every state-changing API route requires a "
            "valid X-Simulation-Id header from a paired /simulate call. Default 'false' "
            "so legacy dashboard + scripts keep working until the new cockpit ships."
        ),
    ),
    # Schedule cadence — require cron hot-reload when changed.
    "LP_PLAN_PUSH_HOUR": SettingSpec(
        key="LP_PLAN_PUSH_HOUR",
        type_name="int",
        env_default=_int_env("LP_PLAN_PUSH_HOUR", "0"),
        min_value=0,
        max_value=23,
        cron_reload=True,
        description="UTC hour for the nightly plan-push cron.",
    ),
    "LP_PLAN_PUSH_MINUTE": SettingSpec(
        key="LP_PLAN_PUSH_MINUTE",
        type_name="int",
        env_default=_int_env("LP_PLAN_PUSH_MINUTE", "5"),
        min_value=0,
        max_value=59,
        cron_reload=True,
        description="UTC minute for the nightly plan-push cron.",
    ),
    "LP_MPC_HOURS": SettingSpec(
        key="LP_MPC_HOURS",
        type_name="list[int]",
        env_default=_int_list_env("LP_MPC_HOURS", "6,12,21"),
        cron_reload=True,
        description="Local hours at which the MPC re-solves the LP (e.g. [6,12,21]).",
    ),
}


# Version counter: bumped on every set_setting() so get() can short-circuit
# without a DB round-trip when the cache entry is current. A TTL-based fallback
# catches out-of-band writes (e.g. manual UPDATE from sqlite3 shell).
_lock = threading.RLock()
_version: int = 0
_cache: dict[str, tuple[Any, int, float]] = {}  # key -> (value, version, monotonic_at)
_TTL_SECONDS: float = 30.0


def _coerce(spec: SettingSpec, raw: str) -> Any:
    if spec.type_name == "float":
        return float(raw)
    if spec.type_name == "int":
        return int(raw)
    if spec.type_name == "str":
        return raw.strip().lower()
    if spec.type_name == "list[int]":
        return sorted({int(p.strip()) for p in raw.split(",") if p.strip()})
    raise SettingValidationError(f"unknown type {spec.type_name!r}")


def _validate(spec: SettingSpec, value: Any) -> Any:
    """Coerce and range/enum-check. Returns the canonical in-memory value.

    Raises :class:`SettingValidationError` with a human-readable message that
    becomes the 400 response body.
    """
    try:
        if spec.type_name == "float":
            v = float(value)
        elif spec.type_name == "int":
            v = int(value)
        elif spec.type_name == "str":
            v = str(value).strip().lower()
        elif spec.type_name == "list[int]":
            if isinstance(value, str):
                v = sorted({int(p.strip()) for p in value.split(",") if p.strip()})
            else:
                v = sorted({int(p) for p in value})
        else:
            raise SettingValidationError(f"unknown type {spec.type_name!r}")
    except (TypeError, ValueError) as e:
        raise SettingValidationError(
            f"{spec.key}: cannot coerce {value!r} to {spec.type_name}: {e}"
        ) from e

    if spec.enum is not None and v not in spec.enum:
        raise SettingValidationError(
            f"{spec.key}: {v!r} not in {list(spec.enum)}"
        )
    if spec.min_value is not None and isinstance(v, (int, float)):
        if v < spec.min_value:
            raise SettingValidationError(
                f"{spec.key}: {v} < min {spec.min_value}"
            )
    if spec.max_value is not None and isinstance(v, (int, float)):
        if v > spec.max_value:
            raise SettingValidationError(
                f"{spec.key}: {v} > max {spec.max_value}"
            )
    return v


def _serialize(spec: SettingSpec, value: Any) -> str:
    if spec.type_name == "list[int]":
        return ",".join(str(int(x)) for x in value)
    return str(value)


def get_setting(key: str) -> Any:
    """Return the current value for *key*.

    Reads hit an in-memory cache that is valid for the current ``_version`` and
    up to ``_TTL_SECONDS``. On miss: read the DB; if absent, call the spec's
    ``env_default`` once and cache it.
    """
    spec = SCHEMA.get(key)
    if spec is None:
        raise KeyError(f"unknown runtime setting: {key!r}")
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None:
            value, version, cached_at = entry
            if version == _version and (now - cached_at) < _TTL_SECONDS:
                return value

        raw = db.get_runtime_setting(key)
        if raw is None:
            try:
                value = spec.env_default()
            except Exception as e:
                logger.warning(
                    "runtime_setting %s: env_default failed (%s); using 0", key, e
                )
                value = 0
        else:
            try:
                value = _coerce(spec, raw)
            except Exception as e:
                logger.warning(
                    "runtime_setting %s: stored value %r failed to coerce (%s); "
                    "falling back to env default",
                    key,
                    raw,
                    e,
                )
                value = spec.env_default()
        _cache[key] = (value, _version, now)
        return value


def set_setting(key: str, value: Any, *, actor: str = "api") -> Any:
    """Validate + persist + invalidate cache. Returns the canonical value stored.

    Side effect: when the spec has ``cron_reload=True`` the caller (API/MCP
    handler) must invoke ``scheduler.runner.reregister_cron_jobs`` after this
    function returns. We do **not** import the scheduler here to avoid a
    circular dependency (scheduler reads config which will soon read this).
    """
    spec = SCHEMA.get(key)
    if spec is None:
        raise SettingValidationError(f"unknown runtime setting: {key!r}")

    canonical = _validate(spec, value)
    serialized = _serialize(spec, canonical)
    db.set_runtime_setting(key, serialized)
    # V11: append-only audit trail so a past LP run can be explained even
    # after a knob is changed. Non-fatal — never block the setting write.
    try:
        db.log_config_change(key, serialized, op="set", actor=actor)
    except Exception as e:
        logger.debug("config_audit insert failed (non-fatal): %s", e)
    global _version
    with _lock:
        _version += 1
        _cache.pop(key, None)
    logger.info(
        "runtime_setting updated: %s=%r (actor=%s, cron_reload=%s)",
        key,
        canonical,
        actor,
        spec.cron_reload,
    )
    return canonical


def delete_setting(key: str, *, actor: str = "api") -> bool:
    """Drop the override row so the next read returns the env default.

    Returns True when a row was removed.
    """
    spec = SCHEMA.get(key)
    if spec is None:
        raise SettingValidationError(f"unknown runtime setting: {key!r}")
    removed = db.delete_runtime_setting(key)
    if removed:
        try:
            db.log_config_change(key, None, op="delete", actor=actor)
        except Exception as e:
            logger.debug("config_audit delete insert failed (non-fatal): %s", e)
    global _version
    with _lock:
        _version += 1
        _cache.pop(key, None)
    logger.info(
        "runtime_setting cleared: %s (actor=%s, removed=%s)", key, actor, removed
    )
    return removed


def list_settings() -> list[dict[str, Any]]:
    """Return current state of every known key, with default and updated_at."""
    rows = {r["key"]: r for r in db.list_runtime_settings()}
    out: list[dict[str, Any]] = []
    for key, spec in SCHEMA.items():
        row = rows.get(key)
        try:
            current = get_setting(key)
        except Exception:
            current = None
        try:
            default = spec.env_default()
        except Exception:
            default = None
        out.append({
            "key": key,
            "value": current,
            "default": default,
            "updated_at": row["updated_at"] if row else None,
            "overridden": row is not None,
            "type": spec.type_name,
            "min": spec.min_value,
            "max": spec.max_value,
            "enum": list(spec.enum) if spec.enum else None,
            "cron_reload": spec.cron_reload,
            "description": spec.description,
        })
    return out


def clear_cache() -> None:
    """Invalidate the entire cache (used by tests and by the cron-reload path)."""
    global _version
    with _lock:
        _version += 1
        _cache.clear()
