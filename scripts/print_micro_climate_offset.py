#!/usr/bin/env python3
"""Print current micro-climate offset (mean Daikin − forecast) from execution_log (#20)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Project root on path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db  # noqa: E402
from src.config import config  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--lookback",
        type=int,
        default=None,
        help="Rows to average (default: DAIKIN_MICRO_CLIMATE_LOOKBACK)",
    )
    args = p.parse_args()
    lb = args.lookback if args.lookback is not None else int(config.DAIKIN_MICRO_CLIMATE_LOOKBACK)
    db.init_db()
    off = db.get_micro_climate_offset_c(lookback=lb)
    print(f"micro_climate_offset_c={off:.4f} (lookback={lb})")
    print(
        "Interpretation: positive means Daikin outdoor reads warmer than forecast; "
        "solver subtracts this from forecast T_out."
    )


if __name__ == "__main__":
    main()
