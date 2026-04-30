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
    """Log one HTTP call and prune entries older than 48 h.

    Side effect: when this is a Daikin write recorded under
    ``DAIKIN_CONTROL_MODE=active``, ensure the active-mode soak start timestamp
    exists in ``runtime_settings`` so the budget cap kicks in. When the mode is
    passive, clear the marker so a future active flip starts a fresh soak window.
    """
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

    if vendor == "daikin" and kind == "write":
        try:
            from . import db
            mode = str(getattr(config, "DAIKIN_CONTROL_MODE", "passive")).lower()
            if mode == "active":
                if not db.get_runtime_setting("daikin_active_mode_started_at"):
                    db.set_runtime_setting("daikin_active_mode_started_at", str(now))
                    logger.info("Daikin active-mode soak window started at ts=%.0f", now)
            else:
                # Mode flipped back to passive — clear the marker so a future active
                # flip starts a fresh soak window.
                if db.get_runtime_setting("daikin_active_mode_started_at"):
                    db.delete_runtime_setting("daikin_active_mode_started_at")
        except Exception:
            pass  # Non-fatal — bookkeeping must never break the write path.


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
        full = int(getattr(config, "DAIKIN_DAILY_BUDGET", 180))
        soak = _daikin_active_mode_soak_budget(full)
        return soak if soak is not None else full
    if vendor == "fox":
        return int(getattr(config, "FOX_DAILY_BUDGET", 1200))
    return 9999


def _daikin_active_mode_soak_budget(full_budget: int) -> int | None:
    """Return the reduced daily budget to apply during the active-mode soak window,
    or None when soak is inapplicable.

    Soak only applies in active mode. The first time an active-mode write is recorded,
    ``runtime_settings.daikin_active_mode_started_at`` is set; after
    ``DAIKIN_ACTIVE_SOAK_DAYS`` days the function falls through to the full budget.
    Flipping back to passive clears the marker (handled in ``record_call``).
    """
    if str(getattr(config, "DAIKIN_CONTROL_MODE", "passive")).lower() != "active":
        return None
    soak_budget = int(getattr(config, "DAIKIN_ACTIVE_SOAK_DAILY_BUDGET", 0))
    soak_days = int(getattr(config, "DAIKIN_ACTIVE_SOAK_DAYS", 0))
    if soak_budget <= 0 or soak_days <= 0:
        return None
    try:
        from . import db
        started = db.get_runtime_setting("daikin_active_mode_started_at")
    except Exception:
        return None
    if not started:
        # No write recorded yet → no soak in flight. Caller gets the full budget;
        # the cap activates at the first ``record_call("daikin", "write", ...)``
        # which plants the marker. This avoids spurious caps during fixture set-up
        # and tests that toggle ``DAIKIN_CONTROL_MODE`` without exercising writes.
        return None
    try:
        elapsed_s = time.time() - float(started)
    except (TypeError, ValueError):
        return min(soak_budget, full_budget)
    if elapsed_s >= soak_days * 86400:
        return None  # Soak window expired — use full budget
    return min(soak_budget, full_budget)


def quota_remaining(vendor: str) -> int:
    """Return how many more calls we can safely make today."""
    used = count_calls_24h(vendor)
    return max(0, _budget(vendor) - used)


def should_block(vendor: str) -> bool:
    """Return True when the daily budget for *vendor* is exhausted, or — for daikin —
    when the consecutive-failure circuit breaker is open."""
    if quota_remaining(vendor) <= 0:
        return True
    if vendor == "daikin" and daikin_circuit_open():
        return True
    return False


# ---------------------------------------------------------------------------
# Daikin write circuit breaker
# ---------------------------------------------------------------------------

def daikin_circuit_open() -> bool:
    """Return True when Daikin writes should pause due to consecutive failures.

    Walks back through ``api_call_log`` rows (vendor='daikin', kind='write') in
    descending time order and counts consecutive ``ok=0`` entries. If the count
    reaches ``DAIKIN_CIRCUIT_BREAKER_FAILS`` AND the oldest failure in that streak
    is within ``DAIKIN_CIRCUIT_BREAKER_WINDOW_MINUTES`` minutes, the breaker is
    open until the most recent failure is older than
    ``DAIKIN_CIRCUIT_BREAKER_COOLDOWN_MINUTES`` minutes. A successful write between
    failures resets the streak (we stop counting at the first ``ok=1`` we encounter).
    """
    fails_threshold = int(getattr(config, "DAIKIN_CIRCUIT_BREAKER_FAILS", 0))
    if fails_threshold <= 0:
        return False
    window_s = max(60, int(getattr(config, "DAIKIN_CIRCUIT_BREAKER_WINDOW_MINUTES", 15)) * 60)
    cooldown_s = max(60, int(getattr(config, "DAIKIN_CIRCUIT_BREAKER_COOLDOWN_MINUTES", 30)) * 60)
    now = time.time()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT ts_utc, ok FROM api_call_log "
                "WHERE vendor='daikin' AND kind='write' "
                "ORDER BY ts_utc DESC LIMIT ?",
                (fails_threshold,),
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()
    if len(rows) < fails_threshold:
        return False
    if any(int(r[1]) == 1 for r in rows):
        return False  # A success interrupts the failure streak
    most_recent_fail = float(rows[0][0])
    oldest_in_streak = float(rows[-1][0])
    if (most_recent_fail - oldest_in_streak) > window_s:
        return False  # Streak spans too long — not a tight burst
    if (now - most_recent_fail) >= cooldown_s:
        return False  # Cooldown elapsed — breaker auto-closes
    return True


def get_quota_status(vendor: str | None = None) -> dict:
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
