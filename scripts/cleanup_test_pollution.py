#!/usr/bin/env python3
"""Cleanup script for #46 — remove test-pollution rows from the production DB.

Targets three fingerprints unique to test fixtures:

1. Rows with ``date`` in the hardcoded-test set ``{2030-06-01, 2026-06-01}``
   (from ``tests/test_state_machine_frost.py`` and
   ``tests/test_replan_preserves_daikin_actions.py``).
2. ``action_schedule`` rows with ``status='active'`` AND ``executed_at IS NULL``.
   The real ``mark_action`` code path always sets ``executed_at`` when
   transitioning status; NULL is only possible when a test fixture
   ``INSERT``'d the row directly with ``status='active'`` (matches
   ``tests/test_user_override.py::_seed_active_row``).
3. ``action_log`` rows with ``trigger='test'``.

Safe to re-run. Prints the count of rows that would be deleted, and deletes
only when ``--apply`` is passed. Safe to run while the service is up — uses
the same SQLite connection discipline as the service itself.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DEFAULT = ROOT / "data" / "energy_state.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_DEFAULT)
    ap.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found at {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    # Count each fingerprint
    hardcoded_dates = conn.execute(
        "SELECT COUNT(*) FROM action_schedule WHERE date IN ('2030-06-01','2026-06-01')"
    ).fetchone()[0]
    malformed_active = conn.execute(
        "SELECT COUNT(*) FROM action_schedule "
        "WHERE device='daikin' AND status='active' AND executed_at IS NULL"
    ).fetchone()[0]
    test_log = conn.execute(
        "SELECT COUNT(*) FROM action_log WHERE trigger='test'"
    ).fetchone()[0]

    total = hardcoded_dates + malformed_active
    print(f"action_schedule: {hardcoded_dates} hardcoded-test-date rows")
    print(f"action_schedule: {malformed_active} malformed-active rows (impossible via mark_action)")
    print(f"action_log:      {test_log} trigger='test' entries")
    print(f"TOTAL: {total} action_schedule + {test_log} action_log rows")

    if not args.apply:
        print("\nDry run. Re-run with --apply to delete.")
        return 0

    with conn:
        conn.execute("DELETE FROM action_schedule WHERE date IN ('2030-06-01','2026-06-01')")
        conn.execute(
            "DELETE FROM action_schedule "
            "WHERE device='daikin' AND status='active' AND executed_at IS NULL"
        )
        conn.execute("DELETE FROM action_log WHERE trigger='test'")

    print(f"\nDeleted {total} action_schedule rows + {test_log} action_log rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
