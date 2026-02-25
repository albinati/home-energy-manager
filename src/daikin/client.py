"""Daikin Onecta Cloud API client.

API: https://developer.cloud.daikineurope.com
Auth: OAuth2 (run src.daikin.auth first)

Usage:
    client = DaikinClient()
    devices = client.get_devices()
    client.set_temperature(devices[0].id, 21.0)
    client.set_power(devices[0].id, on=True)
"""
import json
import urllib.request
import urllib.error

from .auth import get_valid_access_token
from .models import DaikinDevice, DaikinStatus, TemperatureControlSettings
from ..config import config


class DaikinError(Exception):
    pass


class DaikinClient:
    BASE_URL = config.DAIKIN_BASE_URL

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {get_valid_access_token()}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict | list:
        req = urllib.request.Request(f"{self.BASE_URL}{path}", headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise DaikinError(f"HTTP {e.code}: {e.read().decode()}")

    def _patch(self, path: str, body: dict) -> dict:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.BASE_URL}{path}",
            data=payload,
            headers=self._headers(),
            method="PATCH",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            body = resp.read()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise DaikinError(f"HTTP {e.code}: {e.read().decode()}")

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
                    device.temperature.set_point = setpoints["roomTemperature"].get("value")
                if "leavingWaterOffset" in setpoints:
                    device.lwt_offset = setpoints["leavingWaterOffset"].get("value")

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

                sp_mode = mp.get("setpointMode", {}).get("value", "")
                if sp_mode == "weatherDependent":
                    device.weather_regulation_enabled = True

            elif "domestichotwater" in mp_type:
                device.dhw_mp_id = mp.get("embeddedId", device.dhw_mp_id)

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

        return device

    def get_status(self, device: DaikinDevice) -> DaikinStatus:
        return DaikinStatus(
            device_name=device.name,
            is_on=device.is_on,
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
