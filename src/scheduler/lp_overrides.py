"""v10.2 — shared LP override patch/restore for MCP and HTTP /workbench.

Both ``mcp_server._run_simulate_plan_body`` and the new
``api/routers/workbench.py`` need the same pattern: validate a dict of
override keys, monkey-patch the ``config`` singleton, run the LP, restore
prior values. This module is the single owner of the whitelist + validators
+ apply/restore so both callers behave identically.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from ..config import config


@dataclass(frozen=True)
class OverrideSpec:
    """One whitelisted LP override.

    ``config_attr`` is the attribute on ``src.config.config`` that gets
    patched. ``promotable`` means there's a matching ``runtime_settings.SCHEMA``
    entry, so promote-to-prod can persist it via ``set_setting``.
    """
    key: str
    config_attr: str
    type_name: str               # "float" | "int" | "str"
    min_value: float | None = None
    max_value: float | None = None
    enum: tuple[str, ...] | None = None
    description: str = ""
    group: str = "comfort"       # comfort | battery | hardware | penalty | solver | schedule | mode
    promotable: bool = False     # True if matching SCHEMA key in runtime_settings


# Single source of truth for the workbench knobs. Each ``key`` is the wire-name
# (matches runtime_settings.SCHEMA where promotable=True) and each ``config_attr``
# is the live config attribute the LP reads at solve time.
WHITELIST: dict[str, OverrideSpec] = {
    # --- Comfort
    "DHW_TEMP_NORMAL_C": OverrideSpec(
        key="DHW_TEMP_NORMAL_C",
        config_attr="DHW_TEMP_NORMAL_C",
        type_name="float", min_value=40.0, max_value=65.0,
        description="Tank target — restore / safe-default (°C).",
        group="comfort", promotable=True,
    ),
    "DHW_TEMP_COMFORT_C": OverrideSpec(
        key="DHW_TEMP_COMFORT_C",
        config_attr="DHW_TEMP_COMFORT_C",
        type_name="float", min_value=40.0, max_value=65.0,
        description="Tank target when negative-price plunge fills headroom (°C).",
        group="comfort", promotable=True,
    ),
    "INDOOR_SETPOINT_C": OverrideSpec(
        key="INDOOR_SETPOINT_C",
        config_attr="INDOOR_SETPOINT_C",
        type_name="float", min_value=16.0, max_value=26.0,
        description="Indoor comfort setpoint (°C).",
        group="comfort", promotable=True,
    ),
    "TARGET_DHW_TEMP_MIN_GUESTS_C": OverrideSpec(
        key="TARGET_DHW_TEMP_MIN_GUESTS_C",
        config_attr="TARGET_DHW_TEMP_MIN_GUESTS_C",
        type_name="float", min_value=40.0, max_value=65.0,
        description="Guest-mode LP floor (°C) — multiple showers expected.",
        group="comfort",
    ),
    "TARGET_DHW_TEMP_MIN_NORMAL_C": OverrideSpec(
        key="TARGET_DHW_TEMP_MIN_NORMAL_C",
        config_attr="TARGET_DHW_TEMP_MIN_NORMAL_C",
        type_name="float", min_value=40.0, max_value=60.0,
        description="Normal-mode DHW comfort floor (°C).",
        group="comfort",
    ),
    "TARGET_DHW_TEMP_MAX_C": OverrideSpec(
        key="TARGET_DHW_TEMP_MAX_C",
        config_attr="TARGET_DHW_TEMP_MAX_C",
        type_name="float", min_value=50.0, max_value=70.0,
        description="DHW max safe temp (°C).",
        group="comfort",
    ),
    "TARGET_ROOM_TEMP_MIN_C": OverrideSpec(
        key="TARGET_ROOM_TEMP_MIN_C",
        config_attr="TARGET_ROOM_TEMP_MIN_C",
        type_name="float", min_value=15.0, max_value=22.0,
        description="Room comfort floor (°C).",
        group="comfort",
    ),
    "TARGET_ROOM_TEMP_MAX_C": OverrideSpec(
        key="TARGET_ROOM_TEMP_MAX_C",
        config_attr="TARGET_ROOM_TEMP_MAX_C",
        type_name="float", min_value=18.0, max_value=26.0,
        description="Room max temp (°C).",
        group="comfort",
    ),
    # --- Battery
    "MIN_SOC_RESERVE_PERCENT": OverrideSpec(
        key="MIN_SOC_RESERVE_PERCENT",
        config_attr="MIN_SOC_RESERVE_PERCENT",
        type_name="float", min_value=5.0, max_value=50.0,
        description="Minimum SOC the LP is allowed to discharge to (%).",
        group="battery",
    ),
    "BATTERY_RT_EFFICIENCY": OverrideSpec(
        key="BATTERY_RT_EFFICIENCY",
        config_attr="BATTERY_RT_EFFICIENCY",
        type_name="float", min_value=0.7, max_value=0.99,
        description="Round-trip efficiency (0.92 nominal).",
        group="battery",
    ),
    "MAX_INVERTER_KW": OverrideSpec(
        key="MAX_INVERTER_KW",
        config_attr="MAX_INVERTER_KW",
        type_name="float", min_value=1.0, max_value=15.0,
        description="Inverter cap used in LP charge/discharge bounds (kW).",
        group="hardware",
    ),
    # --- Hardware
    "DAIKIN_MAX_HP_KW": OverrideSpec(
        key="DAIKIN_MAX_HP_KW",
        config_attr="DAIKIN_MAX_HP_KW",
        type_name="float", min_value=0.5, max_value=10.0,
        description="Heat pump nameplate cap (kW) used in LP HP power bounds.",
        group="hardware",
    ),
    "HEAT_PUMP_COP_ESTIMATE": OverrideSpec(
        key="HEAT_PUMP_COP_ESTIMATE",
        config_attr="HEAT_PUMP_COP_ESTIMATE",
        type_name="float", min_value=1.5, max_value=6.0,
        description="HP COP estimate used in LP heat → kWh conversion.",
        group="hardware",
    ),
    "EXPORT_RATE_PENCE": OverrideSpec(
        key="EXPORT_RATE_PENCE",
        config_attr="EXPORT_RATE_PENCE",
        type_name="float", min_value=0.0, max_value=40.0,
        description="Default export tariff if no Outgoing Agile (p/kWh).",
        group="hardware",
    ),
    # --- Penalties
    "OPTIMIZATION_PEAK_THRESHOLD_PENCE": OverrideSpec(
        key="OPTIMIZATION_PEAK_THRESHOLD_PENCE",
        config_attr="OPTIMIZATION_PEAK_THRESHOLD_PENCE",
        type_name="float", min_value=10.0, max_value=80.0,
        description="Slot price above this → 'peak' classification (p/kWh).",
        group="penalty",
    ),
    "LP_CYCLE_PENALTY_PENCE_PER_KWH": OverrideSpec(
        key="LP_CYCLE_PENALTY_PENCE_PER_KWH",
        config_attr="LP_CYCLE_PENALTY_PENCE_PER_KWH",
        type_name="float", min_value=0.0, max_value=2.0,
        description="Battery cycle wear penalty (p/kWh throughput).",
        group="penalty",
    ),
    "LP_INVERTER_STRESS_COST_PENCE": OverrideSpec(
        key="LP_INVERTER_STRESS_COST_PENCE",
        config_attr="LP_INVERTER_STRESS_COST_PENCE",
        type_name="float", min_value=0.0, max_value=1.0,
        description="Inverter stress penalty at nominal power (p/kWh). 0 = disabled.",
        group="penalty",
    ),
    "LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA": OverrideSpec(
        key="LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA",
        config_attr="LP_BATTERY_TV_PENALTY_PENCE_PER_KWH_DELTA",
        type_name="float", min_value=0.0, max_value=2.0,
        description="Battery TV penalty (smooths slot-to-slot jumps).",
        group="penalty",
    ),
    "LP_HP_POWER_TV_PENALTY_PENCE_PER_KWH_DELTA": OverrideSpec(
        key="LP_HP_POWER_TV_PENALTY_PENCE_PER_KWH_DELTA",
        config_attr="LP_HP_POWER_TV_PENALTY_PENCE_PER_KWH_DELTA",
        type_name="float", min_value=0.0, max_value=2.0,
        description="HP power TV penalty (smooths heat-pump cycling).",
        group="penalty",
    ),
    "LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA": OverrideSpec(
        key="LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA",
        config_attr="LP_IMPORT_TV_PENALTY_PENCE_PER_KWH_DELTA",
        type_name="float", min_value=0.0, max_value=2.0,
        description="Import TV penalty (smooths grid-import jumps).",
        group="penalty",
    ),
    "LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT": OverrideSpec(
        key="LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT",
        config_attr="LP_COMFORT_SLACK_PENCE_PER_DEGC_SLOT",
        type_name="float", min_value=10.0, max_value=10000.0,
        description="Penalty for breaching comfort floor (p / °C / slot).",
        group="penalty",
    ),
    # --- Solver
    "LP_HORIZON_HOURS": OverrideSpec(
        key="LP_HORIZON_HOURS",
        config_attr="LP_HORIZON_HOURS",
        type_name="int", min_value=4, max_value=48,
        description="LP planning horizon (h). Rolling now → now+H.",
        group="solver",
    ),
    "LP_HIGHS_TIME_LIMIT_SECONDS": OverrideSpec(
        key="LP_HIGHS_TIME_LIMIT_SECONDS",
        config_attr="LP_HIGHS_TIME_LIMIT_SECONDS",
        type_name="int", min_value=5, max_value=600,
        description="Solver wall-clock cap (s). Raise on infeasible.",
        group="solver",
    ),
    "LP_HP_MIN_ON_SLOTS": OverrideSpec(
        key="LP_HP_MIN_ON_SLOTS",
        config_attr="LP_HP_MIN_ON_SLOTS",
        type_name="int", min_value=1, max_value=8,
        description="Min consecutive slots HP must run once started.",
        group="solver",
    ),
    "LP_INVERTER_STRESS_SEGMENTS": OverrideSpec(
        key="LP_INVERTER_STRESS_SEGMENTS",
        config_attr="LP_INVERTER_STRESS_SEGMENTS",
        type_name="int", min_value=2, max_value=20,
        description="Piecewise segments for inverter stress (more = slower).",
        group="solver",
    ),
    # --- Mode flags
    "OPTIMIZATION_PRESET": OverrideSpec(
        key="OPTIMIZATION_PRESET",
        config_attr="OPTIMIZATION_PRESET",
        type_name="str",
        enum=("normal", "guests", "travel", "away"),
        description="Occupancy preset — affects DHW floor and peak-export gates.",
        group="mode", promotable=True,
    ),
    "ENERGY_STRATEGY_MODE": OverrideSpec(
        key="ENERGY_STRATEGY_MODE",
        config_attr="ENERGY_STRATEGY_MODE",
        type_name="str",
        enum=("savings_first", "strict_savings"),
        description="Allow peak-export discharge or never.",
        group="mode", promotable=True,
    ),
}


class OverrideValidationError(ValueError):
    """Raised by :func:`validate_overrides` for bad keys/values."""


def validate_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """Coerce + range/enum-check every override. Returns canonical dict.

    Raises :class:`OverrideValidationError` on the first bad key or value.
    """
    if not isinstance(overrides, dict):
        raise OverrideValidationError("overrides must be a JSON object")
    out: dict[str, Any] = {}
    for key, raw in overrides.items():
        spec = WHITELIST.get(key)
        if spec is None:
            raise OverrideValidationError(
                f"unknown override key: {key!r}. Use GET /api/v1/workbench/schema for the whitelist."
            )
        try:
            if spec.type_name == "float":
                v = float(raw)
            elif spec.type_name == "int":
                v = int(raw)
            elif spec.type_name == "str":
                v = str(raw).strip().lower()
            else:
                raise OverrideValidationError(f"{key}: unsupported type {spec.type_name!r}")
        except (TypeError, ValueError) as exc:
            raise OverrideValidationError(f"{key}: cannot coerce {raw!r} to {spec.type_name}") from exc

        if spec.enum is not None and v not in spec.enum:
            raise OverrideValidationError(f"{key}: {v!r} not in {list(spec.enum)}")
        if spec.min_value is not None and isinstance(v, (int, float)) and v < spec.min_value:
            raise OverrideValidationError(f"{key}: {v} < min {spec.min_value}")
        if spec.max_value is not None and isinstance(v, (int, float)) and v > spec.max_value:
            raise OverrideValidationError(f"{key}: {v} > max {spec.max_value}")
        out[key] = v
    return out


@contextmanager
def patched_config(overrides: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Context manager: monkey-patch ``config`` for the duration of ``with``.

    Yields a mapping of ``{key: prior_value}`` so callers can audit what was
    actually changed. On exit (or exception) every prior value is restored.
    """
    prior: dict[str, Any] = {}
    try:
        for key, value in overrides.items():
            spec = WHITELIST.get(key)
            if spec is None or not hasattr(config, spec.config_attr):
                continue
            prior[spec.config_attr] = getattr(config, spec.config_attr)
            setattr(config, spec.config_attr, value)
        yield prior
    finally:
        for attr, val in prior.items():
            setattr(config, attr, val)


def schema_for_response() -> list[dict[str, Any]]:
    """Return the whitelist as a JSON-friendly list, one entry per key.

    Front-end uses this to build the editor — current value + default + min/max
    + group + description + promotable flag are all in here.
    """
    out: list[dict[str, Any]] = []
    for spec in WHITELIST.values():
        try:
            current = getattr(config, spec.config_attr, None)
        except Exception:
            current = None
        out.append({
            "key": spec.key,
            "config_attr": spec.config_attr,
            "type": spec.type_name,
            "min": spec.min_value,
            "max": spec.max_value,
            "enum": list(spec.enum) if spec.enum else None,
            "description": spec.description,
            "group": spec.group,
            "promotable": spec.promotable,
            "current": current,
        })
    return out


def promotable_keys() -> set[str]:
    return {spec.key for spec in WHITELIST.values() if spec.promotable}


__all__ = [
    "WHITELIST",
    "OverrideSpec",
    "OverrideValidationError",
    "validate_overrides",
    "patched_config",
    "schema_for_response",
    "promotable_keys",
]
