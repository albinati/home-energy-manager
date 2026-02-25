"""CLI entrypoint.

Usage:
    python -m src.cli status           # Full dashboard
    python -m src.cli foxess status    # Fox ESS only
    python -m src.cli foxess charge --from 00:30 --to 05:00 --soc 90
    python -m src.cli foxess mode "Self Use"
    python -m src.cli daikin status    # Daikin only
    python -m src.cli daikin on|off
    python -m src.cli daikin temp 21
    python -m src.cli daikin mode heating|cooling|auto
    python -m src.cli monitor          # Continuous loop (30s intervals)
"""
import sys
import time
from ..config import config
from ..foxess.client import FoxESSClient, FoxESSError
from ..foxess.models import ChargePeriod
from ..daikin.client import DaikinClient, DaikinError
from ..notifier import notify


def foxess_status():
    client = FoxESSClient(config.FOXESS_API_KEY, config.FOXESS_DEVICE_SN)
    d = client.get_realtime()
    bat_arrow = "⬆ charging" if d.battery_power > 0 else ("⬇ discharging" if d.battery_power < 0 else "idle")
    grid_label = "importing" if d.grid_power > 0 else "exporting"
    print(f"""
┌─ Fox ESS ────────────────────────────
│ Battery  : {d.soc:.0f}%  ({abs(d.battery_power):.2f} kW {bat_arrow})
│ Solar    : {d.solar_power:.2f} kW
│ Grid     : {abs(d.grid_power):.2f} kW ({grid_label})
│ Load     : {d.load_power:.2f} kW
│ Mode     : {d.work_mode}
└──────────────────────────────────────""")


def foxess_charge(start: str, end: str, soc: int, period: int = 0):
    client = FoxESSClient(config.FOXESS_API_KEY, config.FOXESS_DEVICE_SN)
    cp = ChargePeriod(start_time=start, end_time=end, target_soc=soc, enable=True)
    client.set_charge_period(period, cp)
    print(f"✅ Charge period {period + 1} set: {start}–{end}, target SoC {soc}%")


def foxess_mode(mode: str):
    client = FoxESSClient(config.FOXESS_API_KEY, config.FOXESS_DEVICE_SN)
    client.set_work_mode(mode)
    print(f"✅ Work mode set to: {mode}")


def daikin_status():
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        print("No Daikin devices found.")
        return
    for dev in devices:
        s = client.get_status(dev)
        state = "ON" if s.is_on else "OFF"
        print(f"""
┌─ Daikin: {s.device_name} ────────────────────
│ Power    : {state}
│ Mode     : {s.mode}
│ Target   : {s.target_temp}°C
│ Room     : {s.room_temp}°C
│ Outdoor  : {s.outdoor_temp}°C
│ LWT      : {s.lwt}°C
│ Weather reg: {"on" if s.weather_regulation else "off"}
└──────────────────────────────────────""")


def daikin_power(on: bool):
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        print("No Daikin devices found.")
        return
    for dev in devices:
        client.set_power(dev.id, on)
    state = "ON" if on else "OFF"
    print(f"✅ Daikin turned {state}")


def daikin_temp(temperature: float):
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        print("No Daikin devices found.")
        return
    for dev in devices:
        client.set_temperature(dev.id, temperature, dev.operation_mode)
    print(f"✅ Temperature set to {temperature}°C")


def daikin_mode(mode: str):
    client = DaikinClient()
    devices = client.get_devices()
    for dev in devices:
        client.set_operation_mode(dev.id, mode)
    print(f"✅ Mode set to: {mode}")


def full_status():
    print("\n=== Energy Dashboard ===\n")
    if config.FOXESS_API_KEY:
        try:
            foxess_status()
        except FoxESSError as e:
            print(f"Fox ESS error: {e}")
    else:
        print("⚠️  Fox ESS not configured (set FOXESS_API_KEY in .env)")

    try:
        daikin_status()
    except FileNotFoundError as e:
        print(f"⚠️  Daikin not configured: {e}")
    except DaikinError as e:
        print(f"Daikin error: {e}")


def monitor_loop(interval: int = 60):
    """Continuous monitoring loop with alerts."""
    print(f"Starting monitor (interval: {interval}s) — Ctrl+C to stop\n")
    foxess_client = FoxESSClient(config.FOXESS_API_KEY, config.FOXESS_DEVICE_SN)
    daikin_client = DaikinClient()

    while True:
        try:
            # Fox ESS checks
            fd = foxess_client.get_realtime()
            if fd.soc < config.FOXESS_ALERT_LOW_SOC:
                notify(f"Battery low: {fd.soc:.0f}% SoC", urgent=True)
            if fd.soc >= 99 and fd.solar_power > 0.5:
                notify(f"Battery full + {fd.solar_power:.1f}kW solar — consider Feed-in Priority mode")

            # Daikin checks
            try:
                devices = daikin_client.get_devices()
                for dev in devices:
                    s = daikin_client.get_status(dev)
                    if (s.is_on and s.room_temp is not None and s.target_temp is not None
                            and abs(s.room_temp - s.target_temp) > config.DAIKIN_ALERT_TEMP_DEVIATION):
                        notify(
                            f"Heat pump temp deviation: room {s.room_temp}°C vs target {s.target_temp}°C",
                            urgent=False,
                        )
            except Exception:
                pass

        except Exception as e:
            print(f"[monitor] Error: {e}")

        time.sleep(interval)


def main():
    args = sys.argv[1:]
    if not args or args[0] == "status":
        full_status()
    elif args[0] == "foxess":
        sub = args[1] if len(args) > 1 else "status"
        if sub == "status":
            foxess_status()
        elif sub == "charge":
            start = end = None
            soc = 90
            i = 2
            while i < len(args):
                if args[i] == "--from":
                    start = args[i + 1]; i += 2
                elif args[i] == "--to":
                    end = args[i + 1]; i += 2
                elif args[i] == "--soc":
                    soc = int(args[i + 1]); i += 2
                else:
                    i += 1
            if not start or not end:
                print("Usage: foxess charge --from HH:MM --to HH:MM [--soc N]")
                sys.exit(1)
            foxess_charge(start, end, soc)
        elif sub == "mode":
            if len(args) < 3:
                print("Usage: foxess mode <mode>")
                sys.exit(1)
            foxess_mode(args[2])
        else:
            print(f"Unknown foxess command: {sub}")
    elif args[0] == "daikin":
        sub = args[1] if len(args) > 1 else "status"
        if sub == "status":
            daikin_status()
        elif sub in ("on", "off"):
            daikin_power(sub == "on")
        elif sub in ("temp", "set-temp"):
            if len(args) < 3:
                print("Usage: daikin temp <degrees>")
                sys.exit(1)
            daikin_temp(float(args[2]))
        elif sub == "mode":
            if len(args) < 3:
                print("Usage: daikin mode <mode>")
                sys.exit(1)
            daikin_mode(args[2])
        else:
            print(f"Unknown daikin command: {sub}")
    elif args[0] == "monitor":
        interval = int(args[1]) if len(args) > 1 else 60
        monitor_loop(interval)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
