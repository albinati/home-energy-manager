"""Seed initial presence_periods rows from human-supplied chat metadata.

Idempotent: safe to re-run. If a presence row with the same start/end/kind
already exists, INSERT OR IGNORE silently skips it.

Initial seed
------------
- 2026-03-27 → 2026-04-10 inclusive: ``travel`` (chat 2026-04-25).

Add new rows here as the household reports them; they are read by future
load-pattern analytics, never by the LP.
"""
from __future__ import annotations

import sys

from src import db


# (start_utc, end_utc, kind, note)
SEED: list[tuple[str, str, str, str]] = [
    (
        "2026-03-27T00:00:00+00:00",
        "2026-04-10T23:59:59+00:00",
        "travel",
        "Reported in chat 2026-04-25 — household away during this window.",
    ),
]


def main() -> int:
    db.init_db()
    inserted = 0
    skipped = 0
    # Idempotency: check existing exact-match rows before inserting.
    from src.db import _lock, get_connection

    for start, end, kind, note in SEED:
        with _lock:
            conn = get_connection()
            try:
                cur = conn.execute(
                    """SELECT id FROM presence_periods
                       WHERE start_utc = ? AND end_utc = ? AND kind = ?""",
                    (start, end, kind),
                )
                if cur.fetchone():
                    skipped += 1
                    continue
            finally:
                conn.close()
        pid = db.add_presence_period(start, end, kind, note)
        print(f"  inserted #{pid}: {start} → {end}  ({kind})")
        inserted += 1
    print()
    print(f"=== Presence seed complete: inserted={inserted}, skipped (already present)={skipped} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
