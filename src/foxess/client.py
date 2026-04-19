"""Fox ESS Cloud API client.

Supports two auth modes:
  1. API Key (Official Open API) — requires API Management in foxesscloud.com User Profile
     Some end-user accounts may not see this option; contact Fox ESS support to enable.
  2. Username/Password (Unofficial) — works with any account type
     Uses the same endpoints as the foxesscloud.com web app.

Usage (API key):
    client = FoxESSClient(api_key="...", device_sn="...", scheduler_sn="...")  # scheduler_sn optional: datalogger SN for V3

Usage (username/password):
    client = FoxESSClient(username="email@example.com", password="...", device_sn="...")

Docs: https://www.foxesscloud.com/public/i18n/en/OpenApiDocument.html
"""
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from .models import ChargePeriod, DeviceInfo, RealTimeData, SchedulerGroup, SchedulerState

# Known work mode strings (set_work_mode accepts these)
WORK_MODE_VALID = frozenset({"Self Use", "Feed-in Priority", "Back Up", "Force charge", "Force discharge"})
# Numeric code → label (Fox API may return code instead of string)
WORK_MODE_BY_CODE = {
    "0": "Self Use",
    "1": "Feed-in Priority",
    "2": "Back Up",
    "3": "Force charge",
    "4": "Force discharge",
}


def _parse_work_mode(raw: str) -> str:
    """Return display work mode string; handle empty or numeric API response."""
    if not raw:
        return "unknown"
    s = raw.strip()
    if s in WORK_MODE_VALID:
        return s
    return WORK_MODE_BY_CODE.get(s, s or "unknown")


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
        scheduler_sn: str | None = None,
    ):
        self.device_sn = device_sn
        s = (scheduler_sn or "").strip()
        self.scheduler_sn: str | None = s or None
        self.api_key = api_key
        self.username = username
        self.password = password
        self._session_token: str | None = None

        if not api_key and not (username and password):
            raise ValueError("Provide either api_key OR username+password.")

    def _sn_scheduler(self) -> str:
        """Value for Open API scheduler JSON field ``deviceSN`` (inverter SN by default)."""
        return self.scheduler_sn if self.scheduler_sn else self.device_sn

    # ── Official Open API (API key auth) ────────────────────────────────────

    def _open_headers(self, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        # Doc example uses fr'{path}\r\n{token}\r\n{timestamp}'; in raw f-string \r\n are literal \ r \ n (4 chars).
        signature_text = path + r"\r\n" + self.api_key + r"\r\n" + timestamp
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

    def _open_post_v3(self, path: str, body: dict) -> dict:
        """Open API v3 (API key only). path e.g. '/device/scheduler/get'."""
        if not self.api_key:
            raise FoxESSError("Open API v3 endpoints require FOXESS_API_KEY (Scheduler V3).")
        url = f"https://www.foxesscloud.com/op/v3{path}"
        payload = json.dumps(body).encode()
        sig_path = f"/op/v3{path}"
        req = urllib.request.Request(url, data=payload, headers=self._open_headers(sig_path))
        try:
            resp = urllib.request.urlopen(req, timeout=20)
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
        """Fetch real-time device data (SoC, solar, grid, battery).

        Open API returns result as list of devices; each has deviceSN and datas (list of variable/value).
        See: https://www.foxesscloud.com/public/i18n/en/OpenApiDocument.html
        """
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
        # API returns result as list of { deviceSN, datas: [ {variable, value, ...} ] } or single { deviceSN, datas }
        if isinstance(result, list) and result:
            device = next((d for d in result if isinstance(d, dict) and d.get("deviceSN") == self.device_sn), result[0])
            items = (device.get("datas") if isinstance(device, dict) else None) or []
        elif isinstance(result, dict):
            items = result.get("datas") or []
        else:
            items = []

        def val(key: str) -> float:
            for item in items:
                if item.get("variable") == key:
                    return float(item.get("value", 0) or 0)
            return 0.0

        def strval(key: str) -> str:
            for item in items:
                if item.get("variable") == key:
                    v = item.get("value")
                    if v is None:
                        return ""
                    s = str(v).strip()
                    return s if s else ""
            return ""

        # workMode: API may return numeric code (0=Self Use, 1=Feed-in Priority, etc.) or string
        _work_mode_raw = strval("workMode")
        if not _work_mode_raw:
            _work_mode_raw = strval("work_mode")  # fallback key
        work_mode_str = _parse_work_mode(_work_mode_raw)

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
            work_mode=work_mode_str,
        )

    def get_device_list(self) -> list[DeviceInfo]:
        """List all devices on the account."""
        if self.api_key:
            # Open API: POST /op/v0/device/list with JSON body (GET + query returns 40257).
            result = self._open_post("/device/list", {"currentPage": 1, "pageSize": 20})
            devices_raw = result.get("data", []) or result.get("devices", [])
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

    def get_device_setting(self, key: str) -> dict:
        """Fetch one setting by key (Open API requires both ``sn`` and ``key``)."""
        body = {"sn": self.device_sn, "key": key}
        if self.api_key:
            return self._open_post("/device/setting/get", body)
        return self._cloud_post("/c/v0/device/setting/get", body)

    def set_device_setting(self, key: str, value: str | int | float | dict) -> None:
        """Set a single device setting by key (same endpoint as work mode / charge times).

        Examples (device-dependent): ``minSoc``, ``workMode``. Prefer typed helpers when they exist.
        """
        body = {"sn": self.device_sn, "key": key, "value": value}
        if self.api_key:
            self._open_post("/device/setting/set", body)
        else:
            self._cloud_post("/c/v0/device/setting/set", body)

    def get_battery_schedule(self) -> dict:
        """Return battery schedule payload (charge windows) if supported by the account/device."""
        body = {"sn": self.device_sn}
        if self.api_key:
            return self._open_post("/device/battery/schedule/get", body)
        return self._cloud_post("/c/v0/device/battery/schedule/get", body)

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

    def set_min_soc(self, min_soc: int) -> None:
        """Set Min SoC (on grid) limit (V7 Safeties)."""
        if not (10 <= min_soc <= 100):
            raise ValueError("Min SoC must be between 10 and 100")
        self.set_device_setting("minSocOnGrid", min_soc)

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

    def get_energy_range(self, begin_date: date, end_date: date) -> dict:
        """Get energy summary (kWh) for a date range via history/query.

        Doc: variables can be empty to obtain all variable data. For range queries,
        "Today" variables may be invalid so we pass [] and map common keys from response.
        Returns dict with keys: pvEnergyToday, feedinEnergyToday, gridConsumptionEnergyToday,
        chargeEnergyToday, dischargeEnergyToday, loadEnergyToday (same as get_energy_today).
        """
        begin_ts = int(datetime.combine(begin_date, datetime.min.time()).timestamp()) * 1000
        end_ts = int(datetime.combine(end_date, datetime.max.time()).timestamp()) * 1000
        # Empty variables per doc: "can obtain all variable data by default without passing variables"
        body = {"sn": self.device_sn, "variables": [], "begin": begin_ts, "end": end_ts}
        if self.api_key:
            result = self._open_post("/device/history/query", body)
        else:
            result = self._cloud_post("/c/v0/device/history/query", body)
        summary = {}
        items = result if isinstance(result, list) else []
        for item in items:
            key = item.get("variable", "")
            values = item.get("data", [])
            summary[key] = round(sum(v.get("value", 0) or 0 for v in values), 2)
        # Map possible response keys to get_energy_today-style keys (support various namings)
        return {
            "pvEnergyToday": summary.get("pvEnergyToday") or summary.get("generation") or summary.get("todayYield") or 0,
            "feedinEnergyToday": summary.get("feedinEnergyToday") or summary.get("feedin") or 0,
            "gridConsumptionEnergyToday": summary.get("gridConsumptionEnergyToday") or summary.get("gridConsumption") or 0,
            "chargeEnergyToday": summary.get("chargeEnergyToday") or summary.get("chargeEnergyToTal") or summary.get("chargeEnergyTotal") or 0,
            "dischargeEnergyToday": summary.get("dischargeEnergyToday") or summary.get("dischargeEnergyToTal") or summary.get("dischargeEnergyTotal") or 0,
            "loadEnergyToday": summary.get("loadEnergyToday") or summary.get("loads") or 0,
        }

    def get_energy_month(self, year: int, month: int) -> dict:
        """Get energy summary (kWh) for a calendar month.

        Tries report/query first (recommended for monthly). Falls back to history/query
        range if report is not available. Returned dict uses same keys as get_energy_today
        for compatibility: pvEnergyToday, feedinEnergyToday, gridConsumptionEnergyToday,
        chargeEnergyToday, dischargeEnergyToday, loadEnergyToday.
        """
        try:
            return self._get_energy_month_report(year, month)
        except FoxESSError:
            from calendar import monthrange
            start = date(year, month, 1)
            _, last_day = monthrange(year, month)
            end = date(year, month, last_day)
            return self.get_energy_range(start, end)

    def _get_energy_month_report(self, year: int, month: int) -> dict:
        """Monthly totals via report/query. Uses exact parameter names from Open API doc."""
        # Doc example: dimension "day" with day; for "month" use year+month only (no day).
        # Variable names per doc: chargeEnergyToTal, dischargeEnergyToTal (capital T).
        # Only variables accepted by report/query per Open API doc (40257 if unknown vars sent)
        variables = [
            "generation", "feedin", "gridConsumption",
            "chargeEnergyToTal", "dischargeEnergyToTal",
        ]
        body = {
            "sn": self.device_sn,
            "year": year,
            "month": month,
            "dimension": "month",
            "variables": variables,
        }
        if self.api_key:
            result = self._open_post("/device/report/query", body)
        else:
            result = self._cloud_post("/c/v0/device/report/query", body)
        # Report API returns result: [ { variable, unit, values: [num, num, ...] } ]; sum per variable
        summary_raw = {}
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = result.get("report") or result.get("list") or result.get("data") or result.get("result") or []
            if not isinstance(items, list):
                items = []
        else:
            items = []
        for item in items:
            key = item.get("variable", "")
            total = 0
            vals = item.get("values", [])
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, (int, float)) and v is not None:
                        total += float(v)
                    elif isinstance(v, dict):
                        total += float(v.get("value", 0) or 0)
            if total == 0 and item.get("data"):
                for v in item.get("data", []) if isinstance(item.get("data"), list) else []:
                    total += float(v.get("value", 0) or 0) if isinstance(v, dict) else 0
            summary_raw[key] = round(total, 2)
        # Map to same keys as get_energy_today (support alternate names if API returns them)
        charge = summary_raw.get("chargeEnergyToTal") or summary_raw.get("chargeEnergyTotal") or 0
        discharge = summary_raw.get("dischargeEnergyToTal") or summary_raw.get("dischargeEnergyTotal") or 0
        pv_val = summary_raw.get("generation") or summary_raw.get("generationPower") or 0
        loads_val = summary_raw.get("loads") or summary_raw.get("load") or 0
        feedin_val = summary_raw.get("feedin", 0)
        grid_val = summary_raw.get("gridConsumption", 0)
        if not loads_val and (pv_val or grid_val or discharge or feedin_val or charge):
            # Estimate load from balance: consumption = import + solar + discharge - export - charge
            loads_val = max(0.0, float(grid_val) + float(pv_val) + float(discharge) - float(feedin_val) - float(charge))
        return {
            "pvEnergyToday": pv_val,
            "feedinEnergyToday": feedin_val,
            "gridConsumptionEnergyToday": grid_val,
            "chargeEnergyToday": charge,
            "dischargeEnergyToday": discharge,
            "loadEnergyToday": round(loads_val, 2) if loads_val else loads_val,
        }

    def get_energy_day(self, year: int, month: int, day: int) -> dict:
        """Get energy summary (kWh) for a single day via report/query dimension=day."""
        variables = [
            "generation", "feedin", "gridConsumption",
            "chargeEnergyToTal", "dischargeEnergyToTal",
        ]
        body = {
            "sn": self.device_sn,
            "year": year,
            "month": month,
            "day": day,
            "dimension": "day",
            "variables": variables,
        }
        if self.api_key:
            result = self._open_post("/device/report/query", body)
        else:
            result = self._cloud_post("/c/v0/device/report/query", body)
        summary_raw = {}
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = result.get("report") or result.get("list") or result.get("data") or result.get("result") or []
            if not isinstance(items, list):
                items = []
        else:
            items = []
        for item in items:
            key = item.get("variable", "")
            vals = item.get("values", [])
            total = 0
            if isinstance(vals, list) and len(vals) > 0:
                v = vals[0]
                total = float(v) if isinstance(v, (int, float)) else float(v.get("value", 0) or 0) if isinstance(v, dict) else 0
            summary_raw[key] = round(total, 2)
        charge = summary_raw.get("chargeEnergyToTal") or summary_raw.get("chargeEnergyTotal") or 0
        discharge = summary_raw.get("dischargeEnergyToTal") or summary_raw.get("dischargeEnergyTotal") or 0
        pv_val = summary_raw.get("generation") or summary_raw.get("generationPower") or 0
        loads_val = summary_raw.get("loads") or summary_raw.get("load") or 0
        feedin_val = summary_raw.get("feedin", 0)
        grid_val = summary_raw.get("gridConsumption", 0)
        if not loads_val and (pv_val or grid_val or discharge or feedin_val or charge):
            loads_val = max(0.0, float(grid_val) + float(pv_val) + float(discharge) - float(feedin_val) - float(charge))
        return {
            "pvEnergyToday": pv_val,
            "feedinEnergyToday": feedin_val,
            "gridConsumptionEnergyToday": grid_val,
            "chargeEnergyToday": charge,
            "dischargeEnergyToday": discharge,
            "loadEnergyToday": round(loads_val, 2) if loads_val else loads_val,
        }

    def get_energy_month_daily_breakdown(self, year: int, month: int) -> tuple[dict, list[dict]]:
        """Get monthly totals and per-day breakdown for charts. Returns (totals_dict, daily_list).
        daily_list items: { date: YYYY-MM-DD, import_kwh, export_kwh, solar_kwh, load_kwh, charge_kwh, discharge_kwh }.
        """
        from calendar import monthrange
        _, ndays = monthrange(year, month)
        variables = [
            "generation", "feedin", "gridConsumption",
            "chargeEnergyToTal", "dischargeEnergyToTal",
        ]
        body = {
            "sn": self.device_sn,
            "year": year,
            "month": month,
            "dimension": "month",
            "variables": variables,
        }
        if self.api_key:
            result = self._open_post("/device/report/query", body)
        else:
            result = self._cloud_post("/c/v0/device/report/query", body)
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = result.get("report") or result.get("list") or result.get("data") or result.get("result") or []
            if not isinstance(items, list):
                items = []
        else:
            items = []
        by_var = {}
        for item in items:
            key = item.get("variable", "")
            vals = item.get("values", [])
            if isinstance(vals, list):
                by_var[key] = [float(v) if isinstance(v, (int, float)) else float(v.get("value", 0) or 0) if isinstance(v, dict) else 0 for v in vals]
            else:
                by_var[key] = []
        gen_arr = by_var.get("generation", [])
        feedin_arr = by_var.get("feedin", [])
        grid_arr = by_var.get("gridConsumption", [])
        loads_arr = by_var.get("loads", []) or by_var.get("load", [])
        charge_arr = by_var.get("chargeEnergyToTal", []) or by_var.get("chargeEnergyTotal", [])
        discharge_arr = by_var.get("dischargeEnergyToTal", []) or by_var.get("dischargeEnergyTotal", [])
        max_len = max(len(gen_arr), len(feedin_arr), len(grid_arr), len(loads_arr), len(charge_arr), len(discharge_arr), ndays)
        daily = []
        for idx in range(min(ndays, max_len)):
            d = idx + 1
            imp = round(grid_arr[idx], 2) if idx < len(grid_arr) else 0
            exp = round(feedin_arr[idx], 2) if idx < len(feedin_arr) else 0
            sol = round(gen_arr[idx], 2) if idx < len(gen_arr) else 0
            ch = round(charge_arr[idx], 2) if idx < len(charge_arr) else 0
            dis = round(discharge_arr[idx], 2) if idx < len(discharge_arr) else 0
            load_val = round(loads_arr[idx], 2) if idx < len(loads_arr) else 0
            if not load_val and (imp or sol or dis or exp or ch):
                load_val = round(max(0.0, imp + sol + dis - exp - ch), 2)
            daily.append({
                "date": f"{year:04d}-{month:02d}-{d:02d}",
                "import_kwh": imp,
                "export_kwh": exp,
                "solar_kwh": sol,
                "load_kwh": load_val,
                "charge_kwh": ch,
                "discharge_kwh": dis,
            })
        totals = {
            "pvEnergyToday": sum(r["solar_kwh"] for r in daily),
            "feedinEnergyToday": sum(r["export_kwh"] for r in daily),
            "gridConsumptionEnergyToday": sum(r["import_kwh"] for r in daily),
            "chargeEnergyToday": sum(r["charge_kwh"] for r in daily),
            "dischargeEnergyToday": sum(r["discharge_kwh"] for r in daily),
            "loadEnergyToday": sum(r["load_kwh"] for r in daily),
        }
        return totals, daily

    # ── Scheduler V3 + extras (Open API key only where noted) ─────────────────

    def get_scheduler_v3(self) -> SchedulerState:
        """Fetch hardware schedule (v3)."""
        raw = self._open_post_v3(
            "/device/scheduler/get", {"deviceSN": self._sn_scheduler()}
        )
        return _parse_scheduler_v3_result(raw)

    def set_scheduler_v3(self, groups: list[SchedulerGroup], is_default: bool = False) -> None:
        """Upload full day schedule (one API call)."""
        payload = {
            "deviceSN": self._sn_scheduler(),
            "isDefault": bool(is_default),
            "groups": [g.to_api_dict() for g in groups],
        }
        self._open_post_v3("/device/scheduler/enable", payload)

    def get_scheduler_flag(self) -> bool:
        """Return True if inverter time scheduler master switch is enabled."""
        sn = self._sn_scheduler()
        if self.api_key:
            raw = self._open_post("/device/scheduler/get/flag", {"deviceSN": sn})
        else:
            raw = self._cloud_post("/c/v0/device/scheduler/get/flag", {"sn": sn})
        if isinstance(raw, dict):
            v = raw.get("enable")
            if v is None:
                v = raw.get("flag")
            if isinstance(v, str) and v.strip() in ("0", "1"):
                return v.strip() == "1"
            return bool(v)
        return bool(raw)

    def set_scheduler_flag(self, enable: bool) -> None:
        sn = self._sn_scheduler()
        en = 1 if enable else 0
        if self.api_key:
            self._open_post(
                "/device/scheduler/set/flag", {"deviceSN": sn, "enable": en}
            )
        else:
            self._cloud_post("/c/v0/device/scheduler/set", {"sn": sn, "enable": enable})

    def get_peak_shaving(self) -> dict:
        body = {"sn": self.device_sn}
        if self.api_key:
            return self._open_post("/device/peakShaving/get", body)
        return self._cloud_post("/c/v0/device/peakShaving/get", body)

    def set_peak_shaving(self, import_limit_w: int, soc: int) -> None:
        body = {"sn": self.device_sn, "importLimit": import_limit_w, "soc": soc}
        if self.api_key:
            self._open_post("/device/peakShaving/set", body)
        else:
            self._cloud_post("/c/v0/device/peakShaving/set", body)


def _groups_from_api_dicts(groups: list) -> list[SchedulerGroup]:
    groups_out: list[SchedulerGroup] = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        ep = g.get("extraParam") or g.get("extra_param") or {}
        if not isinstance(ep, dict):
            ep = {}
        groups_out.append(
            SchedulerGroup(
                start_hour=int(g.get("startHour", g.get("start_hour", 0))),
                start_minute=int(g.get("startMinute", g.get("start_minute", 0))),
                end_hour=int(g.get("endHour", g.get("end_hour", 0))),
                end_minute=int(g.get("endMinute", g.get("end_minute", 0))),
                work_mode=str(g.get("workMode", g.get("work_mode", "SelfUse"))),
                min_soc_on_grid=int(ep.get("minSocOnGrid", ep.get("min_soc_on_grid", 10))),
                fd_soc=ep.get("fdSoc") if ep.get("fdSoc") is not None else ep.get("fd_soc"),
                fd_pwr=ep.get("fdPwr") if ep.get("fdPwr") is not None else ep.get("fd_pwr"),
                max_soc=ep.get("maxSoc") if ep.get("maxSoc") is not None else ep.get("max_soc"),
                import_limit=ep.get("importLimit") if ep.get("importLimit") is not None else ep.get("import_limit"),
                export_limit=ep.get("exportLimit") if ep.get("exportLimit") is not None else ep.get("export_limit"),
            )
        )
    return groups_out


def scheduler_groups_from_stored_json(groups: list) -> list[SchedulerGroup]:
    """Rebuild `SchedulerGroup` list from DB `groups_json` / API-shaped dicts."""
    return _groups_from_api_dicts(groups)


def _parse_scheduler_v3_result(raw: dict) -> SchedulerState:
    en = raw.get("enable", raw.get("enabled", True))
    if isinstance(en, str) and en.strip() in ("0", "1"):
        enabled = en.strip() == "1"
    else:
        enabled = bool(en)
    max_gc = int(raw.get("maxGroupCount", raw.get("max_group_count", 8)) or 8)
    props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    groups_out = _groups_from_api_dicts(raw.get("groups", []) or [])
    return SchedulerState(enabled=enabled, groups=groups_out, max_group_count=max_gc, properties=props)
