"""CLI entrypoint.

Production deployments run the API under systemd: ``python -m src.cli serve`` (see CLAUDE.md).
The ``daemon`` subcommands remain for optional background use on a workstation; they are not
the supported server model on the VPS.

Usage:
    python -m src.cli status               # Full dashboard
    python -m src.cli foxess status        # Fox ESS only
    python -m src.cli foxess charge --from 00:30 --to 05:00 --soc 90
    python -m src.cli foxess mode "Self Use"
    python -m src.cli daikin status        # Daikin only
    python -m src.cli daikin on|off
    python -m src.cli daikin temp 21
    python -m src.cli daikin lwt-offset -3
    python -m src.cli daikin tank-temp 45
    python -m src.cli daikin mode heating|cooling|auto
    python -m src.cli monitor               # Continuous loop (30s intervals)
    python -m src.cli serve                 # Start API server (foreground; use with systemd in prod)
    python -m src.cli daemon start          # Optional: background API (non-systemd)
    python -m src.cli daemon stop           # Stop background daemon
    python -m src.cli daemon status         # Daemon status

Options:
    --json                                 # Output in JSON format (for OpenClaw)
    --api                                  # Route commands through API server
"""
import json as json_module
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Configure logging BEFORE importing modules that create loggers, so app-level
# logger.info() reaches stdout/stderr (and via Docker journald → journalctl).
# Previously every logger.info from src/scheduler/*, src/api/*, etc. was
# silently swallowed because no root handler existed; only uvicorn access logs
# surfaced. ``LOG_LEVEL`` env var defaults to INFO and can be lowered to DEBUG
# for deep-dive diagnostics.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)

from ..config import config
from ..daikin.client import DaikinClient, DaikinError
from ..foxess.client import FoxESSClient, FoxESSError
from ..foxess.models import ChargePeriod
from ..notifier import notify

API_BASE_URL = f"http://{config.API_HOST}:{config.API_PORT}/api/v1"

OUTPUT_JSON = False
USE_API = False


def output(data: dict | str, success: bool = True):
    """Output data in the appropriate format."""
    if OUTPUT_JSON:
        if isinstance(data, str):
            data = {"message": data, "success": success}
        print(json_module.dumps(data, indent=2, default=str))
    else:
        if isinstance(data, dict):
            if "message" in data:
                print(data["message"])
            else:
                for k, v in data.items():
                    print(f"{k}: {v}")
        else:
            print(data)


def api_call(endpoint: str, method: str = "GET", body: dict = None) -> dict:
    """Make an API call to the local server."""
    url = f"{API_BASE_URL}{endpoint}"
    headers = {"Content-Type": "application/json"}
    
    data = None
    if body:
        data = json_module.dumps(body).encode()
    
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json_module.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        try:
            error_data = json_module.loads(error_body)
            raise RuntimeError(error_data.get("detail", f"HTTP {e.code}"))
        except json_module.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {error_body[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot connect to API server: {e.reason}")


def foxess_status():
    if USE_API:
        data = api_call("/foxess/status")
        if OUTPUT_JSON:
            output(data)
        else:
            d = data
            bat_arrow = "⬆ charging" if d["battery_power"] > 0 else ("⬇ discharging" if d["battery_power"] < 0 else "idle")
            grid_label = "importing" if d["grid_power"] > 0 else "exporting"
            print(f"""
┌─ Fox ESS ────────────────────────────
│ Battery  : {d['soc']:.0f}%  ({abs(d['battery_power']):.2f} kW {bat_arrow})
│ Solar    : {d['solar_power']:.2f} kW
│ Grid     : {abs(d['grid_power']):.2f} kW ({grid_label})
│ Load     : {d['load_power']:.2f} kW
│ Mode     : {d['work_mode']}
└──────────────────────────────────────""")
        return
    
    client = FoxESSClient(**config.foxess_client_kwargs())
    d = client.get_realtime()
    
    if OUTPUT_JSON:
        output({
            "soc": d.soc,
            "solar_power": d.solar_power,
            "grid_power": d.grid_power,
            "battery_power": d.battery_power,
            "load_power": d.load_power,
            "work_mode": d.work_mode,
        })
    else:
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
    if USE_API:
        data = api_call("/foxess/charge-period", "POST", {
            "start_time": start,
            "end_time": end,
            "target_soc": soc,
            "period_index": period,
        })
        output(data)
        return
    
    client = FoxESSClient(**config.foxess_client_kwargs())
    cp = ChargePeriod(start_time=start, end_time=end, target_soc=soc, enable=True)
    client.set_charge_period(period, cp)
    output(f"✅ Charge period {period + 1} set: {start}–{end}, target SoC {soc}%")


def foxess_mode(mode: str):
    if USE_API:
        data = api_call("/foxess/mode", "POST", {"mode": mode, "skip_confirmation": True})
        output(data)
        return
    
    client = FoxESSClient(**config.foxess_client_kwargs())
    client.set_work_mode(mode)
    output(f"✅ Work mode set to: {mode}")


def _fmt(value, unit="°C"):
    return f"{value}{unit}" if value is not None else "--"


def daikin_status():
    if USE_API:
        data = api_call("/daikin/status")
        if OUTPUT_JSON:
            output(data)
        else:
            for dev in data:
                state = "ON" if dev["is_on"] else "OFF"
                lines = [
                    f"┌─ Daikin: {dev['model'] or dev['device_name'] or dev['device_id']} ────────────────────",
                    f"│ Power       : {state}",
                    f"│ Mode        : {dev['mode']}",
                ]
                if dev.get("room_temp") is not None:
                    lines.append(f"│ Room        : {_fmt(dev['room_temp'])}")
                if dev.get("target_temp") is not None:
                    lines.append(f"│ Target      : {_fmt(dev['target_temp'])}")
                lines.append(f"│ Outdoor     : {_fmt(dev.get('outdoor_temp'))}")
                lines.append(f"│ Radiator    : {_fmt(dev.get('lwt'))}")
                if dev.get("lwt_offset") is not None:
                    lines.append(f"│ Curve adj.  : {dev['lwt_offset']:+g}")
                if dev.get("tank_temp") is not None or dev.get("tank_target") is not None:
                    lines.append(f"│ DHW tank    : {_fmt(dev.get('tank_temp'))} (target {_fmt(dev.get('tank_target'))})")
                lines.append(f"│ Weather reg : {'on' if dev.get('weather_regulation') else 'off'}")
                lines.append("└──────────────────────────────────────")
                print("\n" + "\n".join(lines))
        return
    
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        output("No Daikin devices found.", success=False)
        return
    
    if OUTPUT_JSON:
        result = []
        for dev in devices:
            s = client.get_status(dev)
            result.append({
                "device_id": dev.id,
                "device_name": s.device_name,
                "model": dev.model,
                "is_on": s.is_on,
                "mode": s.mode,
                "room_temp": s.room_temp,
                "target_temp": s.target_temp,
                "outdoor_temp": s.outdoor_temp,
                "lwt": s.lwt,
                "lwt_offset": s.lwt_offset,
                "tank_temp": s.tank_temp,
                "tank_target": s.tank_target,
                "weather_regulation": s.weather_regulation,
            })
        output(result)
    else:
        for dev in devices:
            s = client.get_status(dev)
            state = "ON" if s.is_on else "OFF"
            lines = [
                f"┌─ Daikin: {dev.model or s.device_name or dev.id} ────────────────────",
                f"│ Power       : {state}",
                f"│ Mode        : {s.mode}",
            ]
            if s.room_temp is not None:
                lines.append(f"│ Room        : {_fmt(s.room_temp)}")
            if s.target_temp is not None:
                lines.append(f"│ Target      : {_fmt(s.target_temp)}")
            lines.append(f"│ Outdoor     : {_fmt(s.outdoor_temp)}")
            lines.append(f"│ Radiator    : {_fmt(s.lwt)}")
            if s.lwt_offset is not None:
                lines.append(f"│ Curve adj.  : {s.lwt_offset:+g}")
            if s.tank_temp is not None or s.tank_target is not None:
                lines.append(f"│ DHW tank    : {_fmt(s.tank_temp)} (target {_fmt(s.tank_target)})")
            lines.append(f"│ Weather reg : {'on' if s.weather_regulation else 'off'}")
            lines.append("└──────────────────────────────────────")
            print("\n" + "\n".join(lines))


def daikin_power(on: bool):
    if USE_API:
        data = api_call("/daikin/power", "POST", {"on": on, "skip_confirmation": True})
        output(data)
        return
    
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        output("No Daikin devices found.", success=False)
        return
    for dev in devices:
        client.set_power(dev, on)
    output(f"✅ Daikin turned {'ON' if on else 'OFF'}")


def daikin_temp(temperature: float):
    if USE_API:
        data = api_call("/daikin/temperature", "POST", {"temperature": temperature})
        output(data)
        return
    
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        output("No Daikin devices found.", success=False)
        return
    for dev in devices:
        client.set_temperature(dev, temperature, dev.operation_mode)
    output(f"✅ Temperature set to {temperature}°C")


def daikin_lwt_offset(offset: float):
    if USE_API:
        data = api_call("/daikin/lwt-offset", "POST", {"offset": offset})
        output(data)
        return
    
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        output("No Daikin devices found.", success=False)
        return
    for dev in devices:
        client.set_lwt_offset(dev, offset, dev.operation_mode)
    output(f"✅ LWT offset set to {offset:+g}")


def daikin_tank_temp(temperature: float):
    if USE_API:
        data = api_call("/daikin/tank-temperature", "POST", {"temperature": temperature})
        output(data)
        return
    
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        output("No Daikin devices found.", success=False)
        return
    for dev in devices:
        if dev.tank_target is not None:
            client.set_tank_temperature(dev, temperature)
            rng = ""
            if dev.tank_target_min is not None:
                rng = f" (range: {dev.tank_target_min}–{dev.tank_target_max}°C)"
            output(f"✅ DHW tank target set to {temperature}°C{rng}")
        else:
            output(f"Device {dev.model or dev.id} has no DHW tank.", success=False)


def daikin_mode(mode: str):
    if USE_API:
        data = api_call("/daikin/mode", "POST", {"mode": mode})
        output(data)
        return
    
    client = DaikinClient()
    devices = client.get_devices()
    if not devices:
        output("No Daikin devices found.", success=False)
        return
    for dev in devices:
        client.set_operation_mode(dev, mode)
    output(f"✅ Mode set to: {mode}")


def full_status():
    print("\n=== Energy Dashboard ===\n")
    if config.FOXESS_API_KEY or (config.FOXESS_USERNAME and config.FOXESS_PASSWORD):
        try:
            foxess_status()
        except FoxESSError as e:
            print(f"Fox ESS error: {e}")
    else:
        print("⚠️  Fox ESS not configured (set FOXESS_API_KEY in .env)")
        print("    Generate one at foxesscloud.com → User Profile → API Management")

    try:
        daikin_status()
    except FileNotFoundError as e:
        print(f"⚠️  Daikin not configured: {e}")
    except DaikinError as e:
        print(f"Daikin error: {e}")


def monitor_loop(interval: int = 60):
    """Continuous monitoring loop with alerts."""
    print(f"Starting monitor (interval: {interval}s) — Ctrl+C to stop\n")
    foxess_client = FoxESSClient(**config.foxess_client_kwargs())
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


def serve(host: str = None, port: int = None):
    """Start the API server."""
    from ..api.main import run_server
    h = host or config.API_HOST
    p = port or config.API_PORT
    display_host = "localhost" if h in ("0.0.0.0", "::") else h
    print(f"Starting API server — bound to {h}:{p}")
    print(f"  Dashboard : http://{display_host}:{p}/")
    print(f"  API docs  : http://{display_host}:{p}/docs")
    print(f"  (WSL2)    : http://172.28.1.21:{p}/  ← use this if localhost doesn't work in Windows Chrome")
    run_server(h, p)


def _daemon_pidfile() -> Path:
    """Path to daemon PID file (project root or cwd)."""
    base = Path(__file__).resolve().parents[2]
    if not (base / ".env").exists() and not (base / "requirements.txt").exists():
        base = Path.cwd()
    return base / ".home-energy-manager.pid"


def _daemon_logfile() -> Path:
    """Path to daemon log file."""
    return _daemon_pidfile().parent / "daemon.log"


def daemon_start(host: str = None, port: int = None):
    """Start the API server as a background daemon (data updates + OpenClaw API)."""
    import subprocess
    pidfile = _daemon_pidfile()
    logfile = _daemon_logfile()
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, 0)
            print(f"Daemon already running (PID {pid}). Stop with: python -m src.cli daemon stop")
            return
        except (ProcessLookupError, ValueError):
            pidfile.unlink(missing_ok=True)
    h = host or config.API_HOST
    p = port or config.API_PORT
    env = os.environ.copy()
    env["API_HOST"] = str(h)
    env["API_PORT"] = str(p)
    env["PYTHONPATH"] = str(pidfile.parent)  # so "python -m src.cli serve" finds src
    cmd = [sys.executable, "-m", "src.cli", "serve"]
    with open(logfile, "a") as f:
        proc = subprocess.Popen(
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=str(pidfile.parent),
            start_new_session=True,
        )
    pidfile.write_text(str(proc.pid))
    print(f"Daemon started (PID {proc.pid})")
    display_host = "localhost" if h in ("0.0.0.0", "::") else h
    print(f"  Dashboard : http://{display_host}:{p}/")
    print(f"  API docs  : http://{display_host}:{p}/docs")
    print(f"  OpenClaw  : http://{display_host}:{p}/api/v1/openclaw/")
    print(f"  (WSL2 IP) : http://172.28.1.21:{p}/  ← fallback if localhost blocked in Windows Chrome")
    print(f"  Log: {logfile}")
    print("  Stop: python -m src.cli daemon stop")


def daemon_stop():
    """Stop the daemon if running."""
    pidfile = _daemon_pidfile()
    if not pidfile.exists():
        print("Daemon not running (no PID file).")
        return
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        pidfile.unlink(missing_ok=True)
        print(f"Daemon stopped (was PID {pid}).")
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        print("Daemon was not running (stale PID file removed).")
    except ValueError:
        pidfile.unlink(missing_ok=True)
        print("Invalid PID file removed.")


def daemon_status():
    """Print daemon status (running or not, PID, URL)."""
    pidfile = _daemon_pidfile()
    if not pidfile.exists():
        print("Daemon not running.")
        return
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        host = getattr(config, "API_HOST", "0.0.0.0")
        port = getattr(config, "API_PORT", 8000)
        print(f"Daemon running (PID {pid})")
        print(f"  API: http://{host}:{port}/  OpenClaw: http://{host}:{port}/api/v1/openclaw/")
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        print("Daemon not running (stale PID file removed).")
    except ValueError:
        pidfile.unlink(missing_ok=True)
        print("Daemon not running (invalid PID file removed).")


def main():
    global OUTPUT_JSON, USE_API
    
    args = sys.argv[1:]
    
    if "--json" in args:
        OUTPUT_JSON = True
        args.remove("--json")
    
    if "--api" in args:
        USE_API = True
        args.remove("--api")
    
    if not args or args[0] == "status":
        full_status()
    elif args[0] == "serve":
        host = None
        port = None
        i = 1
        while i < len(args):
            if args[i] == "--host":
                host = args[i + 1]
                i += 2
            elif args[i] == "--port":
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        serve(host, port)
    elif args[0] == "daemon":
        sub = args[1] if len(args) > 1 else "status"
        host = None
        port = None
        i = 2
        while i < len(args):
            if args[i] == "--host":
                host = args[i + 1]
                i += 2
            elif args[i] == "--port":
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        if sub == "start":
            daemon_start(host, port)
        elif sub == "stop":
            daemon_stop()
        elif sub == "status":
            daemon_status()
        else:
            print("Usage: daemon start|stop|status  [--host H] [--port P]")
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
        elif sub == "lwt-offset":
            if len(args) < 3:
                print("Usage: daikin lwt-offset <offset>  (e.g. -3 or +2)")
                sys.exit(1)
            daikin_lwt_offset(float(args[2]))
        elif sub == "tank-temp":
            if len(args) < 3:
                print("Usage: daikin tank-temp <degrees>  (e.g. 45)")
                sys.exit(1)
            daikin_tank_temp(float(args[2]))
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
