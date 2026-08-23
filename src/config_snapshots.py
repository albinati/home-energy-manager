"""Config snapshot and rollback (runtime only; does not rewrite .env)."""

from __future__ import annotations

import json
import logging
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


def _make_persister():
    """Return ``persist(attr, value)`` for :func:`restore_snapshot`.

    Runtime-tunable keys go through ``runtime_settings.set_setting`` so the
    restored value survives a restart AND is visible to every later reader —
    and any in-memory override for that key is dropped, so the restore is not
    silently shadowed by a stale one. Keys with no spec (plain attributes) keep
    the old assignment. Never raises: a snapshot restore must not half-fail on
    one bad key. See #790.
    """
    from .runtime_settings import SCHEMA, set_setting

    def persist(attr: str, value: Any) -> None:
        if attr in SCHEMA:
            try:
                set_setting(attr, value, actor="snapshot_restore")
                config.clear_override(attr)
                return
            except Exception:
                logger.warning(
                    "snapshot restore: could not persist %s=%r — falling back "
                    "to an in-process value", attr, value, exc_info=True,
                )
        try:
            setattr(config, attr, value)
        except Exception:
            logger.warning("snapshot restore: could not set %s=%r", attr, value)

    return persist


def restore_snapshot(snapshot_id: str) -> dict[str, Any]:
    path = _snapshot_path(snapshot_id)
    if not path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")

    snap = json.loads(path.read_text())

    # #790 — assigning to a runtime-tunable key writes an IN-MEMORY override
    # that shadows the DB for the life of the process, so a restore would
    # "work" until the next restart and silently swallow every later UI edit
    # to the same key. Persist through ``runtime_settings`` for anything with
    # a spec (and drop any stale in-memory override for it); fall back to
    # assignment only for keys that are plain attributes.
    _persist = _make_persister()

    _persist("OPTIMIZATION_PRESET", snap.get("preset", "normal"))
    ob = snap.get("optimizer_backend")
    if ob is not None:
        _persist("OPTIMIZER_BACKEND", str(ob).strip().lower())
    else:
        # Legacy snapshots had target_price_pence; V8 uses PuLP/heuristic only.
        _persist("OPTIMIZER_BACKEND", "lp")
    _persist("OPTIMIZATION_CHEAP_THRESHOLD_PENCE", float(snap.get("cheap_threshold_pence", 12)))
    _persist("OPTIMIZATION_PEAK_START", snap.get("peak_start", "16:00"))
    _persist("OPTIMIZATION_PEAK_END", snap.get("peak_end", "19:00"))
    _persist("OPTIMIZATION_PREHEAT_LWT_BOOST", float(snap.get("lwt_boost", 2.0)))
    _persist("MIN_SOC_RESERVE_PERCENT", float(snap.get("min_soc_reserve_percent", 15)))
    _persist("OPTIMIZATION_LWT_OFFSET_MIN", float(snap.get("lwt_offset_min", -10)))
    _persist("OPTIMIZATION_LWT_OFFSET_MAX", float(snap.get("lwt_offset_max", 10)))
    _persist("TARGET_DHW_TEMP_MIN_NORMAL_C", float(snap.get("dhw_temp_min_normal", 45)))
    _persist("TARGET_DHW_TEMP_MIN_GUESTS_C", float(snap.get("dhw_temp_min_guests", 48)))
    _persist("TARGET_DHW_TEMP_MAX_C", float(snap.get("dhw_temp_max", 65)))
    _persist("TARGET_ROOM_TEMP_MIN_C", float(snap.get("room_temp_min", 20)))
    _persist("TARGET_ROOM_TEMP_MAX_C", float(snap.get("room_temp_max", 23)))

    logger.info("Config restored from snapshot %s", snapshot_id)
    return snap


def rollback_latest() -> dict[str, Any] | None:
    snap = get_latest_snapshot()
    if snap is None:
        return None
    return restore_snapshot(snap["snapshot_id"])
