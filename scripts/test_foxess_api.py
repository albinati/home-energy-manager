#!/usr/bin/env python3
"""Test Fox ESS Open API real-time query. Prints raw response and parsed data.

Run from project root: python scripts/test_foxess_api.py
Requires .env with FOXESS_API_KEY (or FOX_API_KEY) and FOXESS_DEVICE_SN (or INVERTER_SERIAL_NUMBER).
"""
import json
import os
import sys
from pathlib import Path

# Load .env from project root; PYTHONPATH must include project root (e.g. PYTHONPATH=. python scripts/test_foxess_api.py)
root = Path(__file__).resolve().parent.parent
dotenv = root / ".env"
if dotenv.exists():
    from dotenv import load_dotenv
    load_dotenv(dotenv)

if str(root) not in sys.path:
    sys.path.insert(0, str(root))

def main():
    api_key = os.getenv("FOX_API_KEY") or os.getenv("FOXESS_API_KEY") or os.getenv("FOXESS_PRIVATE_TOKEN", "")
    device_sn = os.getenv("FOXESS_DEVICE_SN") or os.getenv("INVERTER_SERIAL_NUMBER", "")

    if not api_key or not device_sn:
        print("Missing env: set FOXESS_API_KEY (or FOX_API_KEY) and FOXESS_DEVICE_SN (or INVERTER_SERIAL_NUMBER)")
        sys.exit(1)

    from src.foxess.client import FoxESSClient
    from src.foxess.models import RealTimeData

    client = FoxESSClient(device_sn=device_sn, api_key=api_key)

    # 1) Raw request to see exact API response shape
    path = "/device/real/query"
    body = {
        "sn": device_sn,
        "variables": [
            "SoC", "pvPower", "gridConsumptionPower", "feedinPower",
            "batChargePower", "batDischargePower", "loadsPower",
            "generationPower", "workMode",
        ],
    }
    raw = client._open_post(path, body)
    print("--- Raw API result (type: {}) ---".format(type(raw).__name__))
    print(json.dumps(raw, indent=2, default=str))
    print()

    # 2) Parsed real-time data (what the app uses)
    rt = client.get_realtime()
    print("--- Parsed RealTimeData ---")
    print(f"  soc: {rt.soc}%")
    print(f"  solar_power: {rt.solar_power} kW")
    print(f"  grid_power: {rt.grid_power} kW")
    print(f"  battery_power: {rt.battery_power} kW")
    print(f"  load_power: {rt.load_power} kW")
    print(f"  work_mode: {rt.work_mode}")
    print("OK")

if __name__ == "__main__":
    main()
