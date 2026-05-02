"""Daikin Onecta Cloud API client.

API: https://developer.cloud.daikineurope.com
Auth: OAuth2 (run src.daikin.auth first)

Usage:
    client = DaikinClient()
    devices = client.get_devices()
    client.set_temperature(devices[0].id, 21.0)
    client.set_power(devices[0].id, on=True)
"""
import datetime as _datetime  # noqa: F401 — used in type hint of get_daily_consumption_from_cache
import json
import time
import urllib.error
import urllib.request

from ..api_quota import record_call
from ..config import config
from .auth import get_valid_access_token
from .models import DaikinDevice, DaikinStatus, SetpointRange


class DaikinError(Exception):
    pass


class DaikinClient:
    BASE_URL = config.DAIKIN_BASE_URL

    @staticmethod
    def _retry_after_seconds(err: urllib.error.HTTPError, default: float = 2.0) -> float:
        hdrs = getattr(err, "headers", None) or getattr(err, "hdrs", None)
        if hdrs is None:
            return default
        ra = hdrs.get("Retry-After") if hasattr(hdrs, "get") else None
        if ra is None:
            return default
        try:
            return min(120.0, max(1.0, float(ra)))
        except (TypeError, ValueError):
            return default

    def _headers(self, *, force_refresh: bool = False) -> dict:
        return {
            "Authorization": f"Bearer {get_valid_access_token(force_refresh=force_refresh)}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _safe_record(kind: str, ok: bool) -> None:
        """Quota accounting must never shadow the HTTP outcome — swallow SQLite errors."""
        try:
            record_call("daikin", kind, ok=ok)
        except Exception:
            pass

    def _get(self, path: str) -> dict | list:
        url = f"{self.BASE_URL}{path}"
        max_429 = max(0, int(config.DAIKIN_HTTP_429_MAX_RETRIES))
        for auth_try in range(2):
            req = urllib.request.Request(url, headers=self._headers(force_refresh=auth_try > 0))
            retry_auth = False
            for r429 in range(max_429 + 1):
                try:
                    resp = urllib.request.urlopen(req, timeout=15)
                except urllib.error.HTTPError as e:
                    self._safe_record("read", ok=False)
                    body = e.read().decode()
                    if e.code == 401 and auth_try == 0:
                        retry_auth = True
                        break
                    if e.code == 429 and r429 < max_429:
                        time.sleep(self._retry_after_seconds(e))
                        continue
                    raise DaikinError(f"HTTP {e.code}: {body}")
                except Exception:
                    self._safe_record("read", ok=False)
                    raise
                self._safe_record("read", ok=True)
                return json.loads(resp.read())
            if retry_auth:
                continue
        raise DaikinError("HTTP 401: authorization failed after retry")

    def _patch(self, path: str, body: dict) -> dict:
        url = f"{self.BASE_URL}{path}"
        payload = json.dumps(body).encode()
        max_429 = max(0, int(config.DAIKIN_HTTP_429_MAX_RETRIES))
        for auth_try in range(2):
            req = urllib.request.Request(
                url,
                data=payload,
                headers=self._headers(force_refresh=auth_try > 0),
                method="PATCH",
            )
            retry_auth = False
            for r429 in range(max_429 + 1):
                try:
                    resp = urllib.request.urlopen(req, timeout=15)
                except urllib.error.HTTPError as e:
                    self._safe_record("write", ok=False)
                    err_body = e.read().decode()
                    if e.code == 401 and auth_try == 0:
                        retry_auth = True
                        break
                    if e.code == 429 and r429 < max_429:
                        time.sleep(self._retry_after_seconds(e))
                        continue
                    if e.code == 400 and "READ_ONLY_CHARACTERISTIC" in err_body:
                        raise DaikinError(f"[read_only] HTTP 400: {err_body}")
                    raise DaikinError(f"HTTP {e.code}: {err_body}")
                except Exception:
                    self._safe_record("write", ok=False)
                    raise
                self._safe_record("write", ok=True)
                rb = resp.read()
                return json.loads(rb) if rb else {}
            if retry_auth:
                continue
        raise DaikinError("HTTP 401: authorization failed after retry")

    def get_devices(self) -> list[DaikinDevice]:
        """List all gateway devices."""
        data = self._get("/gateway-devices")
        devices = []
        for gw in data if isinstance(data, list) else []:
            mgmt = gw.get("managementPoints", [])
            device = self._parse_device(gw.get("id", ""), gw.get("embeddedId", ""), mgmt, gw)
            if device:
                devices.append(device)
        return devices

    def _parse_device(self, gw_id: str, name: str, mgmt_points: list, raw: dict) -> DaikinDevice | None:
        """Extract readable state from management points."""
        device = DaikinDevice(id=gw_id, name=name, raw=raw)
        device.model = raw.get("deviceModel", "")

        for mp in mgmt_points:
            mp_type = mp.get("managementPointType", "").lower()

            if "climatecontrol" in mp_type:
                device.climate_mp_id = mp.get("embeddedId", device.climate_mp_id)

                on_off = mp.get("onOffMode", {}).get("value")
                if on_off is not None:
                    device.is_on = (on_off == "on")

                op_mode = mp.get("operationMode", {}).get("value")
                if op_mode:
                    device.operation_mode = op_mode

                temp_ctrl = mp.get("temperatureControl", {}).get("value", {})
                active_ops = temp_ctrl.get("operationModes", {})
                mode_data = active_ops.get(device.operation_mode, active_ops.get("auto", {}))
                setpoints = mode_data.get("setpoints", {})
                if "roomTemperature" in setpoints:
                    rt = setpoints["roomTemperature"]
                    device.temperature.set_point = rt.get("value")
                    device.room_temp_range = SetpointRange(
                        min_value=rt.get("minValue"),
                        max_value=rt.get("maxValue"),
                        step_value=rt.get("stepValue"),
                        settable=rt.get("settable", True),
                    )
                if "leavingWaterOffset" in setpoints:
                    lwo = setpoints["leavingWaterOffset"]
                    device.lwt_offset = lwo.get("value")
                    device.lwt_offset_range = SetpointRange(
                        min_value=lwo.get("minValue"),
                        max_value=lwo.get("maxValue"),
                        step_value=lwo.get("stepValue"),
                        settable=lwo.get("settable", True),
                    )

                sensor = mp.get("sensoryData", {}).get("value", {})
                room = sensor.get("roomTemperature", {}).get("value")
                if room is not None:
                    device.temperature.room_temperature = room
                outdoor = sensor.get("outdoorTemperature", {}).get("value")
                if outdoor is not None:
                    device.temperature.outdoor_temperature = outdoor
                lwt = sensor.get("leavingWaterTemperature", {}).get("value")
                if lwt is not None:
                    device.leaving_water_temperature = lwt

                sp_mode_data = mp.get("setpointMode", {})
                sp_mode = sp_mode_data.get("value", "")
                if sp_mode == "weatherDependent":
                    device.weather_regulation_enabled = True
                device.weather_regulation_settable = sp_mode_data.get("settable", True)

            elif "domestichotwater" in mp_type:
                device.dhw_mp_id = mp.get("embeddedId", device.dhw_mp_id)

                tank_on_off = mp.get("onOffMode", {}).get("value")
                if tank_on_off is not None:
                    device.tank_on = (tank_on_off == "on")
                tank_powerful = mp.get("powerfulMode", {}).get("value")
                if tank_powerful is not None:
                    device.tank_powerful = (tank_powerful == "on")

                sensor = mp.get("sensoryData", {}).get("value", {})
                tank = sensor.get("tankTemperature", {}).get("value")
                if tank is not None:
                    device.tank_temperature = tank

                temp_ctrl = mp.get("temperatureControl", {}).get("value", {})
                dhw_setpoint = (
                    temp_ctrl.get("operationModes", {})
                    .get("heating", {})
                    .get("setpoints", {})
                    .get("domesticHotWaterTemperature", {})
                )
                if isinstance(dhw_setpoint, dict):
                    if dhw_setpoint.get("value") is not None:
                        device.tank_target = dhw_setpoint["value"]
                    device.tank_target_min = dhw_setpoint.get("minValue")
                    device.tank_target_max = dhw_setpoint.get("maxValue")
                    device.tank_temp_range = SetpointRange(
                        min_value=dhw_setpoint.get("minValue"),
                        max_value=dhw_setpoint.get("maxValue"),
                        step_value=dhw_setpoint.get("stepValue"),
                        settable=dhw_setpoint.get("settable", True),
                    )

        return device

    def get_status(self, device: DaikinDevice) -> DaikinStatus:
        return DaikinStatus(
            device_name=device.name,
            # Coerce Optional[bool] to bool for the external-facing status model;
            # unknown treated as off for display/API consumers.
            is_on=bool(device.is_on),
            mode=device.operation_mode,
            room_temp=device.temperature.room_temperature,
            target_temp=device.temperature.set_point,
            outdoor_temp=device.temperature.outdoor_temperature,
            lwt=device.leaving_water_temperature,
            lwt_offset=device.lwt_offset,
            tank_temp=device.tank_temperature,
            tank_target=device.tank_target,
            weather_regulation=device.weather_regulation_enabled,
        )

    def _climate_path(self, device: DaikinDevice, characteristic: str) -> str:
        return f"/gateway-devices/{device.id}/management-points/{device.climate_mp_id}/characteristics/{characteristic}"

    def _dhw_path(self, device: DaikinDevice, characteristic: str) -> str:
        return f"/gateway-devices/{device.id}/management-points/{device.dhw_mp_id}/characteristics/{characteristic}"

    def set_power(self, device: DaikinDevice, on: bool) -> None:
        """Turn climate control on or off."""
        self._patch(self._climate_path(device, "onOffMode"), {"value": "on" if on else "off"})

    def set_temperature(self, device: DaikinDevice, temperature: float, mode: str = "heating") -> None:
        """Set target room temperature."""
        self._patch(
            self._climate_path(device, "temperatureControl"),
            {"value": {"operationModes": {mode: {"setpoints": {"roomTemperature": {"value": temperature}}}}}},
        )

    def set_lwt_offset(self, device: DaikinDevice, offset: float, mode: str = "heating") -> None:
        """Set leaving-water-temperature offset from weather curve (Altherma)."""
        self._patch(
            self._climate_path(device, "temperatureControl"),
            {"value": {"operationModes": {mode: {"setpoints": {"leavingWaterOffset": {"value": offset}}}}}},
        )

    def set_operation_mode(self, device: DaikinDevice, mode: str) -> None:
        """Set operation mode: heating / cooling / auto / fan_only / dry."""
        valid = ["heating", "cooling", "auto", "fan_only", "dry"]
        if mode not in valid:
            raise ValueError(f"Invalid mode '{mode}'. Choose from: {valid}")
        self._patch(self._climate_path(device, "operationMode"), {"value": mode})

    def set_weather_regulation(self, device: DaikinDevice, enabled: bool) -> None:
        """Enable/disable weather compensation (Altherma feature)."""
        self._patch(
            self._climate_path(device, "setpointMode"),
            {"value": "weatherDependent" if enabled else "fixed"},
        )

    def set_fixed_setpoint_mode(self, device: DaikinDevice) -> None:
        """Disable weather-dependent curve so LWT offset / fixed logic drives the plant (V7 full API path)."""
        self.set_weather_regulation(device, False)

    def set_tank_temperature(self, device: DaikinDevice, temperature: float) -> None:
        """Set domestic hot water tank target temperature."""
        if device.tank_target_min is not None and temperature < device.tank_target_min:
            raise ValueError(f"Min tank temperature is {device.tank_target_min}°C")
        if device.tank_target_max is not None and temperature > device.tank_target_max:
            raise ValueError(f"Max tank temperature is {device.tank_target_max}°C")
        self._patch(
            self._dhw_path(device, "temperatureControl"),
            {"value": {"operationModes": {"heating": {"setpoints": {"domesticHotWaterTemperature": {"value": temperature}}}}}},
        )

    def set_tank_power(self, device: DaikinDevice, on: bool) -> None:
        """Turn domestic hot water tank on or off."""
        self._patch(self._dhw_path(device, "onOffMode"), {"value": "on" if on else "off"})

    def set_tank_powerful(self, device: DaikinDevice, on: bool) -> None:
        """Enable/disable powerful mode on DHW tank (fast heat-up)."""
        self._patch(self._dhw_path(device, "powerfulMode"), {"value": "on" if on else "off"})

    def get_heating_consumption_kwh(
        self, year: int, month: int
    ) -> float | None:
        """
        Get heating electrical consumption (kWh) for a given month from Daikin when available.
        Onecta exposes this for some devices (e.g. Altherma) via electricalConsumption.
        Returns None if not available or on error.
        """
        try:
            devices = self.get_devices()
            total = 0.0
            for device in devices:
                val = self._device_heating_kwh_for_month(device, year, month)
                if val is not None:
                    total += val
            return round(total, 2) if total else None
        except Exception:
            return None

    def _device_heating_kwh_for_month(
        self, device: DaikinDevice, year: int, month: int
    ) -> float | None:
        """Extract heating consumption (kWh) for the given month from one device.
        Aligned with daikin_onecta: consumptionData.value.electrical.heating with period 'd'/'w'/'m'.
        """
        for mp in device.raw.get("managementPoints", []):
            if "climatecontrol" not in mp.get("managementPointType", "").lower():
                continue
            val = self._parse_consumption_data_mp(mp, year, month, "heating")
            if val is not None:
                return val
        return None

    def _parse_consumption_data_mp(
        self, mp: dict, year: int, month: int, mode: str
    ) -> float | None:
        """Parse consumption from management point consumptionData (Daikin Onecta shape).
        See: https://github.com/jwillemsen/daikin_onecta
        consumptionData.value.electrical.{heating|cooling} -> period key 'd'|'w'|'m' (array).
        For 'm' (monthly), array index 11+month is current month; we use month index (0-based)."""
        cd = mp.get("consumptionData")
        if not isinstance(cd, dict):
            return None
        cdv = cd.get("value")
        if not isinstance(cdv, dict):
            return None
        electrical = cdv.get("electrical")
        if not isinstance(electrical, dict):
            return None
        mode_data = electrical.get(mode, electrical.get("heating"))
        if not isinstance(mode_data, dict):
            return None
        # Period key 'm' = monthly (daikin_onecta uses SENSOR_PERIODS_ARRAY["m"] for monthly)
        period_arr = mode_data.get("m")
        if isinstance(period_arr, list) and len(period_arr) > 0:
            # daikin_onecta: start_index = 11 + date.today().month → indices 12..23 = Jan..Dec
            idx = 11 + month
            if idx < len(period_arr):
                v = period_arr[idx]
                if isinstance(v, (int, float)) and v is not None:
                    return round(float(v), 2)
            # Fallback: 0-based month index (0=Jan, 11=Dec)
            idx0 = month - 1
            if idx0 < len(period_arr):
                v = period_arr[idx0]
                if isinstance(v, (int, float)) and v is not None:
                    return round(float(v), 2)
        return None

    def get_daily_consumption_from_cache(
        self, today_utc: "_datetime.date | None" = None
    ) -> dict[str, dict[str, float]]:
        """Parse the cached ``/gateway-devices`` payload's per-day consumption arrays.

        S10.12 (#178). Daikin Onecta exposes ``consumptionData.value.electrical.<mode>.w``
        as a 14-element list: the last 7 days of LAST week (indices 0–6, Mon→Sun)
        plus the 7 days of THIS week (indices 7–13, Mon→Sun). Future days are
        ``None``. We map array indices → calendar dates and accumulate per-day
        kWh across all management points (climateControl + domesticHotWaterTank
        usually live separately, contributing to ``heating_kwh`` vs ``dhw_kwh``).

        Returns ``{date_iso: {"heating_kwh": x, "dhw_kwh": y, "total_kwh": z}}``.

        ``today_utc`` is injectable for tests; defaults to ``date.today()`` (UTC).
        Zero extra Daikin API quota — read-only over an already-cached payload.
        """
        from datetime import date as _date, timedelta as _td

        if today_utc is None:
            today_utc = _date.today()
        # Anchor: this-week's Monday in the array layout. arr[7] = this Monday;
        # arr[7+i] = this Monday + i days; arr[i] = last week's Monday + i days.
        this_week_monday = today_utc - _td(days=today_utc.weekday())

        out: dict[str, dict[str, float]] = {}
        try:
            devices = self.get_devices()
        except Exception:
            return out

        for device in devices:
            for mp in device.raw.get("managementPoints", []):
                mp_type = mp.get("managementPointType", "")
                cd = mp.get("consumptionData")
                if not isinstance(cd, dict):
                    continue
                cdv = cd.get("value")
                if not isinstance(cdv, dict):
                    continue
                electrical = cdv.get("electrical")
                if not isinstance(electrical, dict):
                    continue
                # Decide which energy-bucket this management point contributes to.
                # ``domesticHotWaterTank`` → DHW; everything else (climateControl,
                # heatPump, etc.) → space heating.
                is_dhw = "domesticHotWater" in mp_type or "dhw" in mp_type.lower()
                bucket_key = "dhw_kwh" if is_dhw else "heating_kwh"

                # Both ``heating`` and (rarely) ``cooling`` modes count as electrical
                # consumption — sum them together, attributed to the same bucket.
                for mode_name in ("heating", "cooling"):
                    mode_data = electrical.get(mode_name)
                    if not isinstance(mode_data, dict):
                        continue
                    arr = mode_data.get("w")
                    if not isinstance(arr, list):
                        continue
                    for idx, val in enumerate(arr):
                        if val is None:
                            continue
                        try:
                            kwh = float(val)
                        except (TypeError, ValueError):
                            continue
                        day = this_week_monday + _td(days=idx - 7)
                        day_iso = day.isoformat()
                        bucket = out.setdefault(day_iso, {"heating_kwh": 0.0, "dhw_kwh": 0.0})
                        bucket[bucket_key] += kwh

        # Add the total field for convenience
        for day_iso, b in out.items():
            b["total_kwh"] = round(b["heating_kwh"] + b["dhw_kwh"], 3)
            b["heating_kwh"] = round(b["heating_kwh"], 3)
            b["dhw_kwh"] = round(b["dhw_kwh"], 3)
        return out

    def get_2hourly_consumption_from_cache(
        self, today_local: "_datetime.date | None" = None
    ) -> dict[str, dict[int, dict[str, float]]]:
        """Parse 2-hourly consumption from the cached ``/gateway-devices`` payload (#238).

        Onecta exposes ``consumptionData.value.electrical.<mode>.d`` as a
        24-element array layout: indices 0–11 = yesterday's 2-hour buckets
        (00:00–02:00 ... 22:00–24:00), indices 12–23 = today's 2-hour buckets,
        with future buckets reported as ``None``. The Onecta app's "DAY"
        view labels this resolution as **2-hourly average** — that's the
        finest granularity the public API offers. Anchored to the user's
        local TZ (matches the app's bar-chart x-axis).

        Returns ``{date_iso: {bucket_idx: {"heating_kwh": x, "dhw_kwh": y, "total_kwh": z}}}``
        with ``bucket_idx`` in ``[0, 11]`` (0 = 00:00–02:00, 11 = 22:00–24:00).
        Buckets reported as ``None`` are silently skipped — the upsert's
        ``ON CONFLICT DO UPDATE`` lets later polls overwrite once the value
        materialises.

        ``today_local`` is injectable for tests; defaults to ``date.today()``.
        Zero extra Daikin API quota — read-only over an already-cached payload.
        """
        from datetime import date as _date, timedelta as _td

        if today_local is None:
            today_local = _date.today()
        yesterday_local = today_local - _td(days=1)

        out: dict[str, dict[int, dict[str, float]]] = {}
        try:
            devices = self.get_devices()
        except Exception:
            return out

        def _bucket(date_iso: str, idx: int) -> dict[str, float]:
            day_buckets = out.setdefault(date_iso, {})
            return day_buckets.setdefault(idx, {"heating_kwh": 0.0, "dhw_kwh": 0.0})

        for device in devices:
            for mp in device.raw.get("managementPoints", []):
                mp_type = mp.get("managementPointType", "")
                cd = mp.get("consumptionData")
                if not isinstance(cd, dict):
                    continue
                cdv = cd.get("value")
                if not isinstance(cdv, dict):
                    continue
                electrical = cdv.get("electrical")
                if not isinstance(electrical, dict):
                    continue
                is_dhw = "domesticHotWater" in mp_type or "dhw" in mp_type.lower()
                bucket_key = "dhw_kwh" if is_dhw else "heating_kwh"

                for mode_name in ("heating", "cooling"):
                    mode_data = electrical.get(mode_name)
                    if not isinstance(mode_data, dict):
                        continue
                    arr = mode_data.get("d")
                    if not isinstance(arr, list) or len(arr) != 24:
                        continue  # defensive — only proceed when the array is the expected shape
                    for i, val in enumerate(arr):
                        if val is None:
                            continue
                        try:
                            kwh = float(val)
                        except (TypeError, ValueError):
                            continue
                        if i < 12:
                            date_iso = yesterday_local.isoformat()
                            bucket_idx = i
                        else:
                            date_iso = today_local.isoformat()
                            bucket_idx = i - 12
                        b = _bucket(date_iso, bucket_idx)
                        b[bucket_key] += kwh

        for date_iso, day in out.items():
            for bucket_idx, b in day.items():
                b["total_kwh"] = round(b["heating_kwh"] + b["dhw_kwh"], 3)
                b["heating_kwh"] = round(b["heating_kwh"], 3)
                b["dhw_kwh"] = round(b["dhw_kwh"], 3)
        return out

    def get_heating_daily_kwh(self, year: int, month: int) -> list[float] | None:
        """
        Get daily heating consumption (kWh) for the month when available.
        Returns list of 28–31 values (index 0 = day 1). None if not available.
        """
        try:
            devices = self.get_devices()
            # Sum daily from all devices (usually one)
            result: list[float] = []
            for device in devices:
                daily = self._device_heating_daily_kwh(device, year, month)
                if daily:
                    if not result:
                        result = [0.0] * len(daily)
                    for i, v in enumerate(daily):
                        if i < len(result):
                            result[i] = round(result[i] + v, 2)
            return result if result else None
        except Exception:
            return None

    def _device_heating_daily_kwh(
        self, device: DaikinDevice, year: int, month: int
    ) -> list[float] | None:
        """Daily heating (kWh) for one device from consumptionData.value.electrical.heating.d."""
        from calendar import monthrange
        _, ndays = monthrange(year, month)
        for mp in device.raw.get("managementPoints", []):
            if "climatecontrol" not in mp.get("managementPointType", "").lower():
                continue
            cd = mp.get("consumptionData")
            if not isinstance(cd, dict):
                continue
            cdv = cd.get("value")
            if not isinstance(cdv, dict):
                continue
            electrical = cdv.get("electrical")
            if not isinstance(electrical, dict):
                continue
            mode_data = electrical.get("heating")
            if not isinstance(mode_data, dict):
                continue
            arr = mode_data.get("d")  # daily period
            if not isinstance(arr, list) or len(arr) == 0:
                continue
            out = []
            for i in range(ndays):
                if i < len(arr) and arr[i] is not None:
                    try:
                        out.append(round(float(arr[i]), 2))
                    except (TypeError, ValueError):
                        out.append(0.0)
                else:
                    out.append(0.0)
            return out
        return None
