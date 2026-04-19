"""API quota tracker for Daikin and Fox ESS cloud calls.

Persists a sliding 24-hour call log to SQLite and exposes:
  - record_call(vendor, kind, ok) — log one HTTP call
  - count_calls_24h(vendor) — count calls in last 24 h
  - quota_remaining(vendor) — soft-cap headroom
  - should_block(vendor) — True when quota exhausted

Vendors: "daikin" | "fox"
Kinds:   "read"   | "write"
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Optional

from .config import config

logger = logging.getLogger(__name__)

_lock = threading.RLock()


def _get_db_path() -> str:
    return config.DB_PATH


def _conn() -> sqlite3.Connection:
    from pathlib import Path
    path = Path(_get_db_path()).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table() -> None:
    """Create api_call_log table if it does not exist (idempotent)."""
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS api_call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    ts_utc REAL NOT NULL,
                    ok INTEGER NOT NULL DEFAULT 1
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_call_log_vendor_ts ON api_call_log(vendor, ts_utc)"
            )
            conn.commit()
        finally:
            conn.close()


def record_call(vendor: str, kind: str = "read", ok: bool = True) -> None:
    """Log one HTTP call and prune entries older than 48 h."""
    now = time.time()
    cutoff_48h = now - 48 * 3600
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO api_call_log (vendor, kind, ts_utc, ok) VALUES (?, ?, ?, ?)",
                (vendor, kind, now, 1 if ok else 0),
            )
            conn.execute(
                "DELETE FROM api_call_log WHERE ts_utc < ?",
                (cutoff_48h,),
            )
            conn.commit()
        except sqlite3.OperationalError:
            # Table may not exist yet on first call before init_db; create it.
            conn.close()
            ensure_table()
            conn = _conn()
            try:
                conn.execute(
                    "INSERT INTO api_call_log (vendor, kind, ts_utc, ok) VALUES (?, ?, ?, ?)",
                    (vendor, kind, now, 1 if ok else 0),
                )
                conn.commit()
            finally:
                conn.close()
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass


def count_calls_24h(vendor: str) -> int:
    """Return number of calls for *vendor* in the last 24 hours."""
    cutoff = time.time() - 24 * 3600
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM api_call_log WHERE vendor = ? AND ts_utc >= ?",
                (vendor, cutoff),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()


def _budget(vendor: str) -> int:
    if vendor == "daikin":
        return int(getattr(config, "DAIKIN_DAILY_BUDGET", 180))
    if vendor == "fox":
        return int(getattr(config, "FOX_DAILY_BUDGET", 1200))
    return 9999


def quota_remaining(vendor: str) -> int:
    """Return how many more calls we can safely make today."""
    used = count_calls_24h(vendor)
    return max(0, _budget(vendor) - used)


def should_block(vendor: str) -> bool:
    """Return True when the daily budget for *vendor* is exhausted."""
    return quota_remaining(vendor) <= 0


def get_quota_status(vendor: Optional[str] = None) -> dict:
    """Return a dict suitable for status endpoints."""
    vendors = [vendor] if vendor else ["daikin", "fox"]
    out: dict = {}
    for v in vendors:
        used = count_calls_24h(v)
        budget = _budget(v)
        out[v] = {
            "quota_used_24h": used,
            "quota_remaining_24h": max(0, budget - used),
            "daily_budget": budget,
            "blocked": used >= budget,
        }
    return out if not vendor else out[vendor]
