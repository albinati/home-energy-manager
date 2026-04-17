#!/usr/bin/env python3
"""Smoke-test Fox Scheduler V3 (Open API): scheduler flag, get schedule, optional identical re-upload.

Run from repo root with a configured `.env` (FOXESS_API_KEY and device SN).

  python scripts/smoke_test_v3.py
  python scripts/smoke_test_v3.py --write-back   # re-posts current groups unchanged"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-back",
        action="store_true",
        help="Re-post the current hardware groups unchanged (exercises /device/scheduler/enable).",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    from src.config import config
    from src.foxess.client import FoxESSClient, FoxESSError

    try:
        fox = FoxESSClient(**config.foxess_client_kwargs())
    except Exception as e:
        print(f"FAIL: cannot build Fox client: {e}", file=sys.stderr)
        return 2

    try:
        flag = fox.get_scheduler_flag()
        print(f"scheduler_flag={flag}")
    except Exception as e:
        print(f"WARN: get_scheduler_flag: {e}", file=sys.stderr)

    try:
        st = fox.get_scheduler_v3()
    except FoxESSError as e:
        print(f"FAIL: get_scheduler_v3: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"FAIL: get_scheduler_v3: {e}", file=sys.stderr)
        return 3

    print(f"scheduler_v3 enabled={st.enabled} groups={len(st.groups)} max_gc={st.max_group_count}")
    for i, g in enumerate(st.groups[:3]):
        print(
            f"  group[{i}] {g.work_mode} "
            f"{g.start_hour:02d}:{g.start_minute:02d}-{g.end_hour:02d}:{g.end_minute:02d}"
        )
    if len(st.groups) > 3:
        print(f"  ... +{len(st.groups) - 3} more")

    if args.write_back:
        if not st.groups:
            print("SKIP write-back: no groups on device", file=sys.stderr)
        else:
            try:
                fox.set_scheduler_v3(list(st.groups), is_default=False)
                st2 = fox.get_scheduler_v3()
                print(f"write_back ok; re-read groups={len(st2.groups)}")
            except Exception as e:
                print(f"FAIL: write-back: {e}", file=sys.stderr)
                return 4

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
