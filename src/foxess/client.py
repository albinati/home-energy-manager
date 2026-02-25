"""Fox ESS Cloud API client.

Authentication: API key + MD5 signature per request.
Docs: https://www.foxesscloud.com/public/i18n/en/OpenApiInfomation.html

Usage:
    client = FoxESSClient(api_key="...", device_sn="...")
    data = await client.get_realtime()
    await client.set_work_mode("Self Use")
"""
import hashlib
import json
import time
from typing import Optional
import urllib.request
import urllib.error
import urllib.parse

from .models import RealTimeData, ChargePeriod, DeviceInfo


class FoxESSError(Exception):
    pass


class FoxESSClient:
    BASE_URL = "https://www.foxesscloud.com/op/v0"

    def __init__(self, api_key: str, device_sn: str):
        self.api_key = api_key
        self.device_sn = device_sn

    def _headers(self) -> dict:
        """Generate signed request headers."""
        timestamp = str(int(time.time() * 1000))
        signature_raw = f"{self.api_key}{timestamp}"
        token = hashlib.md5(signature_raw.encode()).hexdigest()
        return {
            "token": token,
            "timestamp": timestamp,
            "lang": "en",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise FoxESSError(f"HTTP {e.code}: {e.read().decode()}")
        if data.get("errno") not in (0, None):
            raise FoxESSError(f"API error {data.get('errno')}: {data.get('msg')}")
        return data.get("result", {})

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.BASE_URL}{path}"
        payload = json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload, headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise FoxESSError(f"HTTP {e.code}: {e.read().decode()}")
        if data.get("errno") not in (0, None):
            raise FoxESSError(f"API error {data.get('errno')}: {data.get('msg')}")
        return data.get("result", {})

    def get_realtime(self) -> RealTimeData:
        """Fetch real-time device data."""
        variables = [
            "SoC", "pvPower", "gridConsumptionPower", "feedinPower",
            "batChargePower", "batDischargePower", "loadsPower",
            "generationPower", "workMode",
        ]
        result = self._post("/device/real/query", {
            "sn": self.device_sn,
            "variables": variables,
        })

        def val(key: str) -> float:
            for item in result if isinstance(result, list) else []:
                if item.get("variable") == key:
                    return float(item.get("value", 0))
            return 0.0

        def strval(key: str) -> str:
            for item in result if isinstance(result, list) else []:
                if item.get("variable") == key:
                    return str(item.get("value", ""))
            return ""

        bat_charge = val("batChargePower")
        bat_discharge = val("batDischargePower")
        battery_power = bat_charge - bat_discharge  # positive = charging
        grid_consumption = val("gridConsumptionPower")
        feedin = val("feedinPower")
        grid_power = grid_consumption - feedin  # positive = importing

        return RealTimeData(
            soc=val("SoC"),
            solar_power=val("pvPower"),
            grid_power=grid_power,
            battery_power=battery_power,
            load_power=val("loadsPower"),
            generation_power=val("generationPower"),
            feed_in_power=feedin,
            work_mode=strval("workMode"),
        )

    def get_device_info(self) -> DeviceInfo:
        """Fetch device info."""
        result = self._get("/device/detail", {"sn": self.device_sn})
        return DeviceInfo(
            device_sn=self.device_sn,
            device_type=result.get("deviceType", ""),
            station_name=result.get("stationName", ""),
            status="online" if result.get("online") else "offline",
        )

    def set_work_mode(self, mode: str) -> None:
        """Set inverter work mode.

        Args:
            mode: One of "Self Use", "Feed-in Priority", "Back Up", "Force charge", "Force discharge"
        """
        valid_modes = ["Self Use", "Feed-in Priority", "Back Up", "Force charge", "Force discharge"]
        if mode not in valid_modes:
            raise ValueError(f"Invalid mode '{mode}'. Choose from: {valid_modes}")
        self._post("/device/setting/set", {
            "sn": self.device_sn,
            "key": "workMode",
            "value": mode,
        })

    def set_charge_period(self, period_index: int, period: ChargePeriod) -> None:
        """Set a timed charge period (0 or 1).

        Args:
            period_index: 0 or 1 (Fox ESS supports 2 charge periods)
            period: ChargePeriod with start_time, end_time, target_soc, enable
        """
        if period_index not in (0, 1):
            raise ValueError("period_index must be 0 or 1")
        key = f"times{period_index + 1}"
        self._post("/device/setting/set", {
            "sn": self.device_sn,
            "key": key,
            "value": {
                "enable": period.enable,
                "startTime": {"hour": int(period.start_time.split(":")[0]),
                               "minute": int(period.start_time.split(":")[1])},
                "endTime": {"hour": int(period.end_time.split(":")[0]),
                             "minute": int(period.end_time.split(":")[1])},
                "minSocOnGrid": period.target_soc,
            },
        })

    def get_charge_periods(self) -> list[ChargePeriod]:
        """Read current charge period settings."""
        result = self._post("/device/setting/query", {
            "sn": self.device_sn,
            "keys": ["times1", "times2"],
        })
        periods = []
        for item in result if isinstance(result, list) else []:
            v = item.get("value", {})
            start = v.get("startTime", {})
            end = v.get("endTime", {})
            periods.append(ChargePeriod(
                start_time=f"{start.get('hour', 0):02d}:{start.get('minute', 0):02d}",
                end_time=f"{end.get('hour', 0):02d}:{end.get('minute', 0):02d}",
                target_soc=v.get("minSocOnGrid", 10),
                enable=v.get("enable", False),
            ))
        return periods

    def get_energy_today(self) -> dict:
        """Get today's energy summary (kWh)."""
        today = time.strftime("%Y-%m-%d")
        result = self._post("/device/history/query", {
            "sn": self.device_sn,
            "variables": ["pvEnergyToday", "feedinEnergyToday",
                          "gridConsumptionEnergyToday", "chargeEnergyToday",
                          "dischargeEnergyToday", "loadEnergyToday"],
            "begin": int(time.mktime(time.strptime(today, "%Y-%m-%d"))) * 1000,
            "end": int(time.time()) * 1000,
        })
        summary = {}
        for item in result if isinstance(result, list) else []:
            key = item.get("variable", "")
            values = item.get("data", [])
            if values:
                summary[key] = sum(v.get("value", 0) for v in values)
        return summary
