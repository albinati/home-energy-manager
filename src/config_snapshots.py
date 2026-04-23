"""Config snapshot and rollback (runtime only; does not rewrite .env)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import config

logger = logging.getLogger(__name__)


def _snapshot_dir() -> Path:
    d = Path(config.CONFIG_SNAPSHOT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_path(snapshot_id: str) -> Path:
    return _snapshot_dir() / f"{snapshot_id}.json"


def _gather_device_states() -> dict[str, Any]:
    states: dict[str, Any] = {}

    try:
        from .foxess.service import get_cached_realtime

        rt = get_cached_realtime()
        states["fox_work_mode"] = rt.work_mode
        states["fox_soc_percent"] = rt.soc
    except Exception as exc:
        states["fox_state_error"] = str(exc)

    try:
        from .daikin.client import DaikinClient

        client = DaikinClient()
        devices = client.get_devices()
        if devices:
            st = client.get_status(devices[0])
            states["daikin_lwt_offset"] = st.lwt_offset
            states["daikin_tank_target"] = st.tank_target
            states["daikin_mode"] = st.mode
            states["daikin_weather_regulation"] = st.weather_regulation
    except Exception as exc:
        states["daikin_state_error"] = str(exc)

    return states


def save_snapshot(trigger: str, *, include_device_states: bool = True) -> dict[str, Any]:
    now = datetime.now(UTC)
    snapshot_id = now.strftime("%Y%m%dT%H%M%SZ")

    snapshot: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "snapshot_at": now.isoformat(),
        "trigger": trigger,
        "preset": config.OPTIMIZATION_PRESET,
        "optimizer_backend": config.OPTIMIZER_BACKEND,
        "cheap_threshold_pence": config.OPTIMIZATION_CHEAP_THRESHOLD_PENCE,
        "peak_start": config.OPTIMIZATION_PEAK_START,
        "peak_end": config.OPTIMIZATION_PEAK_END,
        "lwt_boost": config.OPTIMIZATION_PREHEAT_LWT_BOOST,
        "min_soc_reserve_percent": config.MIN_SOC_RESERVE_PERCENT,
        "lwt_offset_min": config.OPTIMIZATION_LWT_OFFSET_MIN,
        "lwt_offset_max": config.OPTIMIZATION_LWT_OFFSET_MAX,
        "dhw_temp_min_normal": config.TARGET_DHW_TEMP_MIN_NORMAL_C,
        "dhw_temp_min_guests": config.TARGET_DHW_TEMP_MIN_GUESTS_C,
        "dhw_temp_max": config.TARGET_DHW_TEMP_MAX_C,
        "room_temp_min": config.TARGET_ROOM_TEMP_MIN_C,
        "room_temp_max": config.TARGET_ROOM_TEMP_MAX_C,
        "plan_consent_expiry_seconds": config.PLAN_CONSENT_EXPIRY_SECONDS,
        "tariff_code": config.OCTOPUS_TARIFF_CODE,
    }

    if include_device_states:
        snapshot["device_states"] = _gather_device_states()

    path = _snapshot_path(snapshot_id)
    try:
        path.write_text(json.dumps(snapshot, indent=2))
        logger.info("Config snapshot saved: %s (trigger=%s)", path, trigger)
    except OSError as exc:
        logger.warning("Failed to write snapshot %s: %s", path, exc)

    return snapshot


def list_snapshots() -> list[dict[str, Any]]:
    d = _snapshot_dir()
    results = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text())
            results.append(
                {
                    "snapshot_id": data.get("snapshot_id", p.stem),
                    "snapshot_at": data.get("snapshot_at"),
                    "trigger": data.get("trigger"),
                    "preset": data.get("preset"),
                }
            )
        except Exception:
            results.append({"snapshot_id": p.stem, "error": "unreadable"})
    return results


def get_latest_snapshot() -> dict[str, Any] | None:
    d = _snapshot_dir()
    files = sorted(d.glob("*.json"), reverse=True)
    for p in files:
        try:
            return json.loads(p.read_text())
        except Exception:
            continue
    return None


def restore_snapshot(snapshot_id: str) -> dict[str, Any]:
    path = _snapshot_path(snapshot_id)
    if not path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")

    snap = json.loads(path.read_text())

    config.OPTIMIZATION_PRESET = snap.get("preset", "normal")
    ob = snap.get("optimizer_backend")
    if ob is not None:
        config.OPTIMIZER_BACKEND = str(ob).strip().lower()
    else:
        # Legacy snapshots had target_price_pence; V8 uses PuLP/heuristic only.
        config.OPTIMIZER_BACKEND = "lp"
    config.OPTIMIZATION_CHEAP_THRESHOLD_PENCE = float(snap.get("cheap_threshold_pence", 12))
    config.OPTIMIZATION_PEAK_START = snap.get("peak_start", "16:00")
    config.OPTIMIZATION_PEAK_END = snap.get("peak_end", "19:00")
    config.OPTIMIZATION_PREHEAT_LWT_BOOST = float(snap.get("lwt_boost", 2.0))
    config.MIN_SOC_RESERVE_PERCENT = float(snap.get("min_soc_reserve_percent", 15))
    config.OPTIMIZATION_LWT_OFFSET_MIN = float(snap.get("lwt_offset_min", -10))
    config.OPTIMIZATION_LWT_OFFSET_MAX = float(snap.get("lwt_offset_max", 10))
    config.TARGET_DHW_TEMP_MIN_NORMAL_C = float(snap.get("dhw_temp_min_normal", 45))
    config.TARGET_DHW_TEMP_MIN_GUESTS_C = float(snap.get("dhw_temp_min_guests", 48))
    config.TARGET_DHW_TEMP_MAX_C = float(snap.get("dhw_temp_max", 65))
    config.TARGET_ROOM_TEMP_MIN_C = float(snap.get("room_temp_min", 20))
    config.TARGET_ROOM_TEMP_MAX_C = float(snap.get("room_temp_max", 23))

    logger.info("Config restored from snapshot %s", snapshot_id)
    return snap


def rollback_latest() -> dict[str, Any] | None:
    snap = get_latest_snapshot()
    if snap is None:
        return None
    return restore_snapshot(snap["snapshot_id"])
