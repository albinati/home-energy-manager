"""Sensor-data lifecycle (#540): tiered hot / warm / cold storage.

The room-sensor tables are append-only and would grow unbounded on the
storage-constrained box. Rather than just delete old rows, we tier them so
nothing valuable is lost (future ML corpus):

  * HOT  — full-resolution raw in SQLite for a recent window (LP + W2 read it).
  * WARM — a permanent, tiny 15-min rollup table (long-term UI trends).
  * COLD — the full-resolution raw + a wide ML-ready join, written to monthly
           gzip files under the state volume BEFORE the raw is pruned. Nothing
           is deleted without a compressed copy landing first.

`run_sensor_data_lifecycle()` is called once per day by the same scheduler tick
that prunes the other history tables. Every step is best-effort and swallows
its own errors so a lifecycle hiccup never breaks the solve loop.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import db
from ..config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Archive location
# ---------------------------------------------------------------------------
def archive_root() -> Path:
    """Directory the monthly gzip archives live in (created on demand).

    Defaults to ``<dir of DB_PATH>/archive`` so archives sit on the persistent
    state volume and survive image swaps.
    """
    configured = (config.DATA_ARCHIVE_DIR or "").strip()
    if configured:
        root = Path(configured)
    else:
        root = Path(config.DB_PATH).resolve().parent / "archive"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _month_key(ts_iso: str | None) -> str | None:
    """`YYYY-MM` partition key from an ISO timestamp, or None if unparseable."""
    if not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        return f"{dt.year:04d}-{dt.month:02d}"
    except (ValueError, TypeError):
        return None


def _append_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> int:
    """Append rows to a gzip JSONL file (one JSON object per line).

    gzip members concatenate cleanly, so append mode produces a file every
    gzip reader (``gzip.open``, pandas ``read_json(lines=True)``) reads whole.
    JSONL (not CSV) so a device's varying extra payload survives without a
    fixed header, and appends never need to rewrite the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str, separators=(",", ":")) + "\n")
    return len(rows)


# ---------------------------------------------------------------------------
# COLD — archive raw rows to monthly gzip, partitioned by captured month,
#        then prune them from SQLite.
# ---------------------------------------------------------------------------
def archive_and_prune_raw(table: str, ts_col: str, retention_days: int) -> dict[str, int]:
    """Archive rows older than ``retention_days`` to ``archive/<table>/<YYYY-MM>.jsonl.gz``
    (partitioned by the *row's* month), then DELETE them. Returns counts."""
    if retention_days <= 0:
        return {"archived": 0, "pruned": 0}
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.fetch_rows_older_than(table, ts_col, cutoff)
    if not rows:
        return {"archived": 0, "pruned": 0}

    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = _month_key(r.get(ts_col)) or "undated"
        by_month[key].append(r)

    root = archive_root() / table
    archived = 0
    for month, rs in by_month.items():
        archived += _append_jsonl_gz(root / f"{month}.jsonl.gz", rs)

    pruned = db.delete_rows_older_than(table, ts_col, cutoff)
    logger.info("archival: %s → %d rows archived, %d pruned (cutoff %s)", table, archived, pruned, cutoff)
    return {"archived": archived, "pruned": pruned}


# ---------------------------------------------------------------------------
# Orchestrator — called daily from the prune tick.
# ---------------------------------------------------------------------------
def run_sensor_data_lifecycle() -> dict[str, Any]:
    """WARM rollup → COLD wide-ML build → COLD raw-archive+prune, in that order.

    Order matters: roll up + build the ML join from the raw BEFORE archiving it
    out, so the warm/wide tiers are complete for the window about to be pruned.
    """
    out: dict[str, Any] = {}
    if not config.DATA_ARCHIVE_ENABLED:
        logger.debug("archival: disabled (DATA_ARCHIVE_ENABLED=false)")
        return {"enabled": False}

    # WARM — refresh the permanent 15-min indoor rollup.
    try:
        out["rollup_15min"] = db.refresh_indoor_rollup_15min()
    except Exception as e:
        logger.warning("archival: 15-min rollup refresh failed: %s", e)
        out["rollup_15min"] = -1

    # COLD — wide ML-ready monthly join (current + previous month, idempotent).
    try:
        out["ml_wide"] = build_ml_wide_archive()
    except Exception as e:
        logger.warning("archival: ml-wide build failed: %s", e)
        out["ml_wide"] = -1

    # COLD — archive full-res raw then prune. device_reading_log keys on
    # received_at (always set; captured_at may be NULL).
    try:
        out["room_temperature_history"] = archive_and_prune_raw(
            "room_temperature_history", "captured_at",
            int(config.INDOOR_SENSOR_RAW_RETENTION_DAYS),
        )
    except Exception as e:
        logger.warning("archival: room_temperature_history failed: %s", e)
    try:
        out["device_reading_log"] = archive_and_prune_raw(
            "device_reading_log", "received_at",
            int(config.DEVICE_LOG_RETENTION_DAYS),
        )
    except Exception as e:
        logger.warning("archival: device_reading_log failed: %s", e)

    return out


# build_ml_wide_archive is defined in data_archival_wide once the source map
# is confirmed; imported lazily so this module loads even before it exists.
def build_ml_wide_archive() -> dict[str, Any]:  # pragma: no cover - thin shim
    from .data_archival_wide import build_ml_wide_archive as _impl
    return _impl(archive_root)
