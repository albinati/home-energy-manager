"""Fox ESS Cloud API client.

Supports two auth modes:
  1. API Key (Official Open API) — requires API Management in foxesscloud.com User Profile
     Some end-user accounts may not see this option; contact Fox ESS support to enable.
  2. Username/Password (Unofficial) — works with any account type
     Uses the same endpoints as the foxesscloud.com web app.

Usage (API key):
    client = FoxESSClient(api_key="...", device_sn="...")

Usage (username/password):
    client = FoxESSClient(username="email@example.com", password="...", device_sn="...")

Docs: https://www.foxesscloud.com/public/i18n/en/OpenApiDocument.html
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
    OPEN_API_BASE = "https://www.foxesscloud.com/op/v0"
    CLOUD_API_BASE = "https://www.foxesscloud.com"
    _BROWSER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.foxesscloud.com/",
    }

    def __init__(
        self,
        device_sn: str,
        api_key: str = "",
        username: str = "",
        password: str = "",
    ):
        self.device_sn = device_sn
        self.api_key = api_key
        self.username = username
        self.password = password
        self._session_token: Optional[str] = None

        if not api_key and not (username and password):
            raise ValueError("Provide either api_key OR username+password.")

    # ── Official Open API (API key auth) ────────────────────────────────────

    def _open_headers(self, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        signature_text = fr'{path}\r\n{self.api_key}\r\n{timestamp}'
        signature = hashlib.md5(signature_text.encode()).hexdigest()
        return {
            **self._BROWSER_HEADERS,
            "token": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "lang": "en",
            "Content-Type": "application/json",
        }

    def _open_post(self, path: str, body: dict) -> dict:
        url = f"{self.OPEN_API_BASE}{path}"
        payload = json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload, headers=self._open_headers(f"/op/v0{path}"))
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise FoxESSError(f"HTTP {e.code}: {e.read().decode()[:200]}")
        errno = data.get("errno", 0)
        if errno not in (0, None):
            raise FoxESSError(f"API error {errno}: {data.get('msg')}")
        return data.get("result", {}) or {}

    def _open_get(self, path: str, params: dict = None) -> dict:
        url = f"{self.OPEN_API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._open_headers(f"/op/v0{path}"))
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise FoxESSError(f"HTTP {e.code}: {e.read().decode()[:200]}")
        errno = data.get("errno", 0)
        if errno not in (0, None):
            raise FoxESSError(f"API error {errno}: {data.get('msg')}")
        return data.get("result", {}) or {}

    # ── Unofficial Cloud API (username/password auth) ────────────────────────

    def _login(self) -> str:
        """Authenticate with username/password, return session token."""
        hashed_pw = hashlib.md5(self.password.encode()).hexdigest()
        payload = json.dumps({
            "user": self.username,
            "password": hashed_pw,
        }).encode()
        req = urllib.request.Request(
            f"{self.CLOUD_API_BASE}/c/v0/user/login",
            data=payload,
            headers={**self._BROWSER_HEADERS, "Content-Type": "application/json", "lang": "en"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise FoxESSError(f"Login HTTP {e.code}: {e.read().decode()[:200]}")
        if data.get("errno") not in (0, None):
            raise FoxESSError(f"Login failed: {data.get('msg')}")
        token = data.get("result", {}).get("token")
        if not token:
            raise FoxESSError("No token in login response")
        self._session_token = token
        return token

    def _cloud_headers(self) -> dict:
        if not self._session_token:
            self._login()
        return {
            **self._BROWSER_HEADERS,
            "token": self._session_token,
            "lang": "en",
            "Content-Type": "application/json",
        }

    def _cloud_post(self, path: str, body: dict) -> dict:
        url = f"{self.CLOUD_API_BASE}{path}"
        payload = json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload, headers=self._cloud_headers())
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise FoxESSError(f"HTTP {e.code}: {e.read().decode()[:200]}")
        errno = data.get("errno", 0)
        if errno == 41808:  # Token expired
            self._session_token = None
            return self._cloud_post(path, body)  # retry once
        if errno not in (0, None):
            raise FoxESSError(f"API error {errno}: {data.get('msg')}")
        return data.get("result", {}) or {}

    # ── Unified interface ────────────────────────────────────────────────────

    def _post(self, path: str, body: dict) -> dict:
        """Route to appropriate API based on configured auth."""
        if self.api_key:
            return self._open_post(path, body)
        # Unofficial: map Open API paths to cloud paths where needed
        cloud_path = path.replace("/op/v0", "/c/v0")
        return self._cloud_post(cloud_path, body)

    # ── Public methods ───────────────────────────────────────────────────────

    def get_realtime(self) -> RealTimeData:
        """Fetch real-time device data (SoC, solar, grid, battery)."""
        variables = [
            "SoC", "pvPower", "gridConsumptionPower", "feedinPower",
            "batChargePower", "batDischargePower", "loadsPower",
            "generationPower", "workMode",
        ]
        if self.api_key:
            result = self._open_post("/device/real/query", {
                "sn": self.device_sn, "variables": variables,
            })
        else:
            result = self._cloud_post("/c/v0/device/real/query", {
                "sn": self.device_sn, "variables": variables,
            })
        items = result if isinstance(result, list) else result.get("datas", [])

        def val(key: str) -> float:
            for item in items:
                if item.get("variable") == key:
                    return float(item.get("value", 0) or 0)
            return 0.0

        def strval(key: str) -> str:
            for item in items:
                if item.get("variable") == key:
                    return str(item.get("value", "") or "")
            return ""

        bat_charge = val("batChargePower")
        bat_discharge = val("batDischargePower")
        feedin = val("feedinPower")
        grid_consumption = val("gridConsumptionPower")

        return RealTimeData(
            soc=val("SoC"),
            solar_power=val("pvPower"),
            grid_power=grid_consumption - feedin,   # positive = importing
            battery_power=bat_charge - bat_discharge,  # positive = charging
            load_power=val("loadsPower"),
            generation_power=val("generationPower"),
            feed_in_power=feedin,
            work_mode=strval("workMode"),
        )

    def get_device_list(self) -> list[DeviceInfo]:
        """List all devices on the account."""
        if self.api_key:
            result = self._open_get("/device/list", {"currentPage": 1, "pageSize": 20})
            devices_raw = result.get("devices", [])
        else:
            result = self._cloud_post("/c/v0/device/list", {
                "pageSize": 20, "currentPage": 1,
            })
            devices_raw = result.get("devices", [])
        return [
            DeviceInfo(
                device_sn=d.get("deviceSN", d.get("sn", "")),
                device_type=d.get("deviceType", ""),
                station_name=d.get("stationName", ""),
                status="online" if d.get("status") == 1 else "offline",
            )
            for d in devices_raw
        ]

    def set_work_mode(self, mode: str) -> None:
        """Set inverter work mode."""
        valid = ["Self Use", "Feed-in Priority", "Back Up", "Force charge", "Force discharge"]
        if mode not in valid:
            raise ValueError(f"Invalid mode '{mode}'. Choose from: {valid}")
        if self.api_key:
            self._open_post("/device/setting/set", {
                "sn": self.device_sn, "key": "workMode", "value": mode,
            })
        else:
            self._cloud_post("/c/v0/device/setting/set", {
                "sn": self.device_sn, "key": "workMode", "value": mode,
            })

    def set_charge_period(self, period_index: int, period: ChargePeriod) -> None:
        """Set a timed charge period (index 0 or 1)."""
        if period_index not in (0, 1):
            raise ValueError("period_index must be 0 or 1")
        key = f"times{period_index + 1}"
        value = {
            "enable": period.enable,
            "startTime": {
                "hour": int(period.start_time.split(":")[0]),
                "minute": int(period.start_time.split(":")[1]),
            },
            "endTime": {
                "hour": int(period.end_time.split(":")[0]),
                "minute": int(period.end_time.split(":")[1]),
            },
            "minSocOnGrid": period.target_soc,
        }
        body = {"sn": self.device_sn, "key": key, "value": value}
        if self.api_key:
            self._open_post("/device/setting/set", body)
        else:
            self._cloud_post("/c/v0/device/setting/set", body)

    def get_energy_today(self) -> dict:
        """Get today's energy summary (kWh)."""
        today = time.strftime("%Y-%m-%d")
        begin = int(time.mktime(time.strptime(today, "%Y-%m-%d"))) * 1000
        end = int(time.time()) * 1000
        variables = [
            "pvEnergyToday", "feedinEnergyToday", "gridConsumptionEnergyToday",
            "chargeEnergyToday", "dischargeEnergyToday", "loadEnergyToday",
        ]
        if self.api_key:
            result = self._open_post("/device/history/query", {
                "sn": self.device_sn, "variables": variables, "begin": begin, "end": end,
            })
        else:
            result = self._cloud_post("/c/v0/device/history/query", {
                "sn": self.device_sn, "variables": variables, "begin": begin, "end": end,
            })
        summary = {}
        items = result if isinstance(result, list) else []
        for item in items:
            key = item.get("variable", "")
            values = item.get("data", [])
            summary[key] = round(sum(v.get("value", 0) or 0 for v in values), 2)
        return summary
