#!/usr/bin/env python3
"""Round-trip both Fox ESS and Daikin: read state, apply small changes, restore.

  - Fox: idempotent write only (set_work_mode if label known, else set_scheduler_flag).
  - Daikin (when settable): LWT offset step, weather-regulation flip, room setpoint nudge.

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
    rt = fox.get_realtime()
    print(f"Fox realtime: SoC={rt.soc}% work_mode={rt.work_mode!r} solar_kW={rt.solar_power}")
    if dry_run:
        st = fox.get_scheduler_v3()
        print(f"Fox scheduler V3: enabled={st.enabled} groups={len(st.groups)}")
        return

    _fox_idempotent_write(fox, rt.work_mode)


def _daikin_refresh(client, device_index: int):
    return _pick_device(client.get_devices(), device_index)


def _daikin_settle_sleep(seconds: float = 2.0) -> None:
    time.sleep(seconds)


def run_daikin(client, *, dry_run: bool, device_index: int) -> None:
    devices = client.get_devices()
    dev = _pick_device(devices, device_index)
    mode = dev.operation_mode or "heating"

    print(
        f"Daikin device[{device_index}] {dev.name!r} mode={mode} "
        f"weather_reg={dev.weather_regulation_enabled} (settable={dev.weather_regulation_settable})"
    )
    if dry_run:
        st = client.get_status(dev)
        off = dev.lwt_offset
        step_o = (dev.lwt_offset_range.step_value or 1.0) if dev.lwt_offset_range else 1.0
        lo_o = dev.lwt_offset_range.min_value if dev.lwt_offset_range else None
        hi_o = dev.lwt_offset_range.max_value if dev.lwt_offset_range else None
        sp = dev.temperature.set_point
        step_r = (dev.room_temp_range.step_value or 0.5) if dev.room_temp_range else 0.5
        lo_r = dev.room_temp_range.min_value if dev.room_temp_range else None
        hi_r = dev.room_temp_range.max_value if dev.room_temp_range else None
        print(
            f"Daikin dry-run caps: LWT off={off} step={step_o} range {lo_o}..{hi_o} "
            f"settable={getattr(dev.lwt_offset_range, 'settable', True)}"
        )
        print(
            f"Daikin dry-run room: setpoint={sp} step={step_r} range {lo_r}..{hi_r} "
            f"settable={getattr(dev.room_temp_range, 'settable', True)}"
        )
        print(
            f"Daikin status: room={st.room_temp}°C outdoor={st.outdoor_temp}°C "
            f"LWT={st.lwt}°C tank={st.tank_temp}°C"
        )
        return

    from src.daikin.client import DaikinError

    failures: list[str] = []
    for label, fn in (
        ("LWT offset", lambda: _daikin_roundtrip_lwt(client, device_index, mode)),
        ("weather regulation", lambda: _daikin_roundtrip_weather_reg(client, device_index)),
        ("room setpoint", lambda: _daikin_roundtrip_room_temp(client, device_index, mode)),
    ):
        try:
            fn()
        except DaikinError as e:
            failures.append(f"{label}: {e}")
    if failures:
        raise SystemExit("Daikin round-trip had errors:\n" + "\n".join(failures))


def _daikin_roundtrip_lwt(client, device_index: int, mode: str) -> None:
    from src.daikin.client import DaikinError

    dev = _daikin_refresh(client, device_index)
    orig_off = dev.lwt_offset
    step = (dev.lwt_offset_range.step_value or 1.0) if dev.lwt_offset_range else 1.0
    lo = dev.lwt_offset_range.min_value if dev.lwt_offset_range else None
    hi = dev.lwt_offset_range.max_value if dev.lwt_offset_range else None

    print(f"Daikin [LWT offset]: current={orig_off} step={step} range {lo}..{hi}")
    if orig_off is None:
        print("Daikin [LWT offset]: skip (no value in gateway state)")
        return
    if dev.lwt_offset_range and dev.lwt_offset_range.settable is False:
        print("Daikin [LWT offset]: skip (not settable)")
        return

    up = _clamp(orig_off + step, lo, hi)
    down = _clamp(orig_off - step, lo, hi)
    trial = up if up != orig_off else down
    if trial == orig_off:
        print("Daikin [LWT offset]: skip (cannot nudge within range)")
        return

    try:
        client.set_lwt_offset(dev, trial, mode=mode)
        print(f"Daikin [LWT offset]: set {orig_off} -> {trial}")
        _daikin_settle_sleep()
        print(f"Daikin [LWT offset]: read-back={_daikin_refresh(client, device_index).lwt_offset}")
        dev2 = _daikin_refresh(client, device_index)
        client.set_lwt_offset(dev2, orig_off, mode=mode)
        print(f"Daikin [LWT offset]: restored -> {orig_off}")
        _daikin_settle_sleep()
        print(f"Daikin [LWT offset]: read-back={_daikin_refresh(client, device_index).lwt_offset}")
    except DaikinError as e:
        print(f"Daikin [LWT offset] ERROR: {e}")
        try:
            dev_r = _daikin_refresh(client, device_index)
            client.set_lwt_offset(dev_r, orig_off, mode=mode)
            print(f"Daikin [LWT offset]: emergency restore -> {orig_off}")
        except Exception as e2:
            print(f"Daikin [LWT offset]: emergency restore FAILED: {e2}")
        raise


def _daikin_roundtrip_weather_reg(client, device_index: int) -> None:
    from src.daikin.client import DaikinError

    dev = _daikin_refresh(client, device_index)
    if not dev.weather_regulation_settable:
        print("Daikin [weather reg]: skip (not settable)")
        return
    orig = dev.weather_regulation_enabled
    flipped = not orig
    try:
        client.set_weather_regulation(dev, flipped)
        print(f"Daikin [weather reg]: set {orig} -> {flipped}")
        _daikin_settle_sleep()
        mid = _daikin_refresh(client, device_index).weather_regulation_enabled
        print(f"Daikin [weather reg]: read-back={mid}")
        dev2 = _daikin_refresh(client, device_index)
        client.set_weather_regulation(dev2, orig)
        print(f"Daikin [weather reg]: restored -> {orig}")
        _daikin_settle_sleep()
        print(f"Daikin [weather reg]: read-back={_daikin_refresh(client, device_index).weather_regulation_enabled}")
    except DaikinError as e:
        print(f"Daikin [weather reg] ERROR: {e}")
        try:
            dev_r = _daikin_refresh(client, device_index)
            client.set_weather_regulation(dev_r, orig)
            print(f"Daikin [weather reg]: emergency restore -> {orig}")
        except Exception as e2:
            print(f"Daikin [weather reg]: emergency restore FAILED: {e2}")
        raise


def _daikin_roundtrip_room_temp(client, device_index: int, mode: str) -> None:
    from src.daikin.client import DaikinError

    dev = _daikin_refresh(client, device_index)
    orig_sp = dev.temperature.set_point
    if orig_sp is None:
        print("Daikin [room setpoint]: skip (no setpoint in gateway state)")
        return
    if dev.room_temp_range and dev.room_temp_range.settable is False:
        print("Daikin [room setpoint]: skip (not settable)")
        return
    step = dev.room_temp_range.step_value or 0.5
    lo = dev.room_temp_range.min_value if dev.room_temp_range else None
    hi = dev.room_temp_range.max_value if dev.room_temp_range else None
    up = _clamp(orig_sp + step, lo, hi)
    down = _clamp(orig_sp - step, lo, hi)
    trial = up if up != orig_sp else down
    if trial == orig_sp:
        print("Daikin [room setpoint]: skip (cannot nudge within range)")
        return

    try:
        client.set_temperature(dev, trial, mode=mode)
        print(f"Daikin [room setpoint]: set {orig_sp}°C -> {trial}°C ({mode})")
        _daikin_settle_sleep()
        print(f"Daikin [room setpoint]: read-back={_daikin_refresh(client, device_index).temperature.set_point}°C")
        dev2 = _daikin_refresh(client, device_index)
        client.set_temperature(dev2, orig_sp, mode=mode)
        print(f"Daikin [room setpoint]: restored -> {orig_sp}°C")
        _daikin_settle_sleep()
        print(f"Daikin [room setpoint]: read-back={_daikin_refresh(client, device_index).temperature.set_point}°C")
    except DaikinError as e:
        print(f"Daikin [room setpoint] ERROR: {e}")
        try:
            dev_r = _daikin_refresh(client, device_index)
            client.set_temperature(dev_r, orig_sp, mode=mode)
            print(f"Daikin [room setpoint]: emergency restore -> {orig_sp}°C")
        except Exception as e2:
            print(f"Daikin [room setpoint]: emergency restore FAILED: {e2}")
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
