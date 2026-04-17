#!/usr/bin/env python3
"""Round-trip both Fox ESS and Daikin: read state, apply a small change, restore.

  - Fox: toggles eco mode off then on (or on then off) and restores; if that fails,
    falls back to an idempotent set_work_mode(current_mode) write.
  - Daikin: nudges leaving-water offset by one API step (or1.0) and restores.

Requires a configured .env (FOXESS_* and Daikin OAuth token file). Not dry-run by
default — you are briefly changing hardware. Use --dry-run to only read state.

  python scripts/hardware_roundtrip_test.py
  python scripts/hardware_roundtrip_test.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _clamp(x: float, lo: float | None, hi: float | None) -> float:
    if lo is not None and x < lo:
        return lo
    if hi is not None and x > hi:
        return hi
    return x


def _pick_device(devices: list, index: int):
    if not devices:
        raise RuntimeError("No Daikin devices")
    if index < 0 or index >= len(devices):
        raise RuntimeError(f"Device index {index} out of range (have {len(devices)})")
    return devices[index]


def _fox_idempotent_write(fox, work_mode: str) -> None:
    """Exercise the write API without changing behaviour."""
    from src.foxess.client import WORK_MODE_VALID, FoxESSError

    if work_mode in WORK_MODE_VALID:
        fox.set_work_mode(work_mode)
        print(f"Fox set_work_mode({work_mode!r}) OK (unchanged)")
        return
    try:
        flag = fox.get_scheduler_flag()
        fox.set_scheduler_flag(flag)
        print(f"Fox set_scheduler_flag({flag}) OK (unchanged; work_mode not a known label)")
    except FoxESSError as e:
        print(f"Fox idempotent write failed: {e}")


def run_fox(fox, *, dry_run: bool) -> None:
    from src.foxess.client import FoxESSError

    rt = fox.get_realtime()
    print(f"Fox realtime: SoC={rt.soc}% work_mode={rt.work_mode!r} solar_kW={rt.solar_power}")
    if dry_run:
        st = fox.get_scheduler_v3()
        print(f"Fox scheduler V3: enabled={st.enabled} groups={len(st.groups)}")
        return

    eco_before: bool | None = None
    eco_read_ok = False
    try:
        eco_before = fox.get_eco_mode()
        eco_read_ok = True
        print(f"Fox eco_mode (before)={eco_before}")
    except FoxESSError as e:
        print(f"Fox get_eco_mode skipped: {e}")
    except Exception as e:
        print(f"Fox get_eco_mode skipped: {e}")

    if eco_read_ok:
        try:
            fox.set_eco_mode(not eco_before)
            time.sleep(1.0)
            mid = fox.get_eco_mode()
            print(f"Fox eco_mode (mid, toggled)={mid}")
            fox.set_eco_mode(eco_before)
            time.sleep(0.5)
            restored = fox.get_eco_mode()
            print(f"Fox eco_mode (restored)={restored} (expected {eco_before})")
        except Exception as e:
            print(f"Fox eco toggle failed: {e}")
            _fox_idempotent_write(fox, rt.work_mode)
    else:
        _fox_idempotent_write(fox, rt.work_mode)


def run_daikin(client, *, dry_run: bool, device_index: int) -> None:
    from src.daikin.client import DaikinError

    devices = client.get_devices()
    dev = _pick_device(devices, device_index)
    mode = dev.operation_mode or "heating"
    orig_off = dev.lwt_offset
    step = (dev.lwt_offset_range.step_value or 1.0) if dev.lwt_offset_range else 1.0
    lo = dev.lwt_offset_range.min_value if dev.lwt_offset_range else None
    hi = dev.lwt_offset_range.max_value if dev.lwt_offset_range else None

    print(
        f"Daikin device[{device_index}] {dev.name!r}: LWT offset={orig_off} "
        f"(step={step}, range {lo}..{hi}) mode={mode}"
    )
    if dry_run:
        st = client.get_status(dev)
        print(
            f"Daikin status: room={st.room_temp}°C outdoor={st.outdoor_temp}°C "
            f"LWT={st.lwt}°C tank={st.tank_temp}°C"
        )
        return

    if orig_off is None:
        print("Daikin: skip LWT round-trip (no lwt_offset in gateway state)")
        return
    if dev.lwt_offset_range and dev.lwt_offset_range.settable is False:
        print("Daikin: skip LWT round-trip (offset not settable)")
        return

    up = _clamp(orig_off + step, lo, hi)
    down = _clamp(orig_off - step, lo, hi)
    trial = up if up != orig_off else down
    if trial == orig_off:
        print("Daikin: skip LWT round-trip (cannot nudge within range)")
        return

    try:
        client.set_lwt_offset(dev, trial, mode=mode)
        print(f"Daikin: set LWT offset {orig_off} -> {trial}")
        time.sleep(1.5)
        dev_mid = _pick_device(client.get_devices(), device_index)
        print(f"Daikin: read-back LWT offset={dev_mid.lwt_offset}")
        client.set_lwt_offset(dev_mid, orig_off, mode=mode)
        print(f"Daikin: restored LWT offset -> {orig_off}")
        time.sleep(1.0)
        dev_final = _pick_device(client.get_devices(), device_index)
        print(f"Daikin: read-back LWT offset={dev_final.lwt_offset}")
    except DaikinError as e:
        print(f"Daikin ERROR: {e}")
        try:
            dev_r = _pick_device(client.get_devices(), device_index)
            client.set_lwt_offset(dev_r, orig_off, mode=mode)
            print(f"Daikin: emergency restore LWT offset -> {orig_off}")
        except Exception as e2:
            print(f"Daikin: emergency restore FAILED: {e2}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read only, no writes")
    parser.add_argument("--fox-only", action="store_true", help="Only Fox ESS")
    parser.add_argument("--daikin-only", action="store_true", help="Only Daikin")
    parser.add_argument(
        "--daikin-device",
        type=int,
        default=0,
        help="Index in get_devices() (default 0)",
    )
    args = parser.parse_args()
    if args.fox_only and args.daikin_only:
        print("Use only one of --fox-only / --daikin-only", file=sys.stderr)
        return 2

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    from src.config import config
    from src.daikin.client import DaikinClient, DaikinError
    from src.foxess.client import FoxESSClient, FoxESSError

    ok = True
    do_fox = not args.daikin_only
    do_daikin = not args.fox_only

    # ── Fox ─────────────────────────────────────────────────
    if do_fox:
        try:
            fox = FoxESSClient(**config.foxess_client_kwargs())
            run_fox(fox, dry_run=args.dry_run)
        except ValueError as e:
            print(f"Fox skip (not configured): {e}")
        except FoxESSError as e:
            print(f"Fox FAILED: {e}")
            ok = False
        except Exception as e:
            print(f"Fox FAILED: {type(e).__name__}: {e}")
            ok = False

    # ── Daikin ────────────────────────────────────────────
    if do_daikin:
        try:
            d = DaikinClient()
            run_daikin(d, dry_run=args.dry_run, device_index=args.daikin_device)
        except FileNotFoundError as e:
            print(f"Daikin skip (no token file?): {e}")
        except DaikinError as e:
            print(f"Daikin FAILED: {e}")
            ok = False
        except Exception as e:
            print(f"Daikin FAILED: {type(e).__name__}: {e}")
            ok = False

    if ok:
        print("Done: all attempted steps completed.")
        return 0
    print("Done: one or more steps failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
