#!/usr/bin/env python3
"""Print median DHW standing loss (°C/h) from execution_log when tank heating is off (#24)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--limit",
        type=int,
        default=2016,
        help="execution_log rows to scan (newest first)",
    )
    args = p.parse_args()
    db.init_db()
    est = db.estimate_dhw_standing_loss_c_per_hour_p50(limit=args.limit)
    if est is None:
        print(
            "dhw_standing_loss_c_per_h=None (need ≥3 cooldown samples with "
            "daikin_tank_power_on=0 and tank temps)"
        )
        print(
            "Tip: ensure the heartbeat logs real tank power state into execution_log, "
            "then collect a few hours of history."
        )
        sys.exit(1)
    print(f"dhw_standing_loss_c_per_h={est:.4f} (p50, limit={args.limit})")
    print(
        "Compare with DHW_TANK_UA_W_PER_K and tank size when calibrating tank loss in the LP."
    )


if __name__ == "__main__":
    main()
