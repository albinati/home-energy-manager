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
            return json.loads(resp.read()) if resp.read() else {}
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
        device.model = raw.get("modelInfo", "")

        for mp in mgmt_points:
            mp_type = mp.get("managementPointType", "")
            # climateControl or climateControlMainZone
            if "climateControl" in mp_type.lower():
                data_points = mp.get("characteristics", {})
                # on/off
                on_off = data_points.get("onOffMode", {}).get("value")
                if on_off is not None:
                    device.is_on = (on_off == "on")
                # operation mode
                op_mode = data_points.get("operationMode", {}).get("value")
                if op_mode:
                    device.operation_mode = op_mode
                # temperatures
                temp_ctrl = data_points.get("temperatureControl", {}).get("value", {})
                if temp_ctrl:
                    ops = temp_ctrl.get("operationModes", {})
                    for mode_name, mode_data in ops.items():
                        if mode_name == device.operation_mode:
                            device.temperature.set_point = (
                                mode_data.get("setpoints", {})
                                .get("roomTemperature", {})
                                .get("value")
                            )
                # room temp
                room_temp = data_points.get("sensoryData", {}).get("value", {}).get("roomTemperature", {}).get("value")
                if room_temp is not None:
                    device.temperature.room_temperature = room_temp
                # outdoor temp
                outdoor_temp = data_points.get("sensoryData", {}).get("value", {}).get("outdoorTemperature", {}).get("value")
                if outdoor_temp is not None:
                    device.temperature.outdoor_temperature = outdoor_temp
                # weather regulation
                wr = data_points.get("weatherRegulatedControl", {}).get("value")
                if wr is not None:
                    device.weather_regulation_enabled = (wr == "on")
            # Altherma leaving water temperature
            if "domesticHotWater" in mp_type.lower() or "heatPump" in mp_type.lower():
                chars = mp.get("characteristics", {})
                lwt = chars.get("leavingWaterTemperature", {}).get("value")
                if lwt is not None:
                    device.leaving_water_temperature = lwt

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
            weather_regulation=device.weather_regulation_enabled,
        )

    def set_power(self, device_id: str, on: bool) -> None:
        """Turn heat pump on or off."""
        self._patch(
            f"/gateway-devices/{device_id}/management-points/climateControl/characteristics/onOffMode",
            {"value": "on" if on else "off"},
        )

    def set_temperature(self, device_id: str, temperature: float, mode: str = "heating") -> None:
        """Set target room temperature.

        Args:
            device_id: Device UUID from get_devices()
            temperature: Target temperature in °C
            mode: "heating" or "cooling" or "auto"
        """
        self._patch(
            f"/gateway-devices/{device_id}/management-points/climateControl/characteristics/temperatureControl",
            {
                "value": {
                    "operationModes": {
                        mode: {
                            "setpoints": {
                                "roomTemperature": {"value": temperature}
                            }
                        }
                    }
                }
            },
        )

    def set_operation_mode(self, device_id: str, mode: str) -> None:
        """Set operation mode: heating / cooling / auto / fan_only / dry."""
        valid = ["heating", "cooling", "auto", "fan_only", "dry"]
        if mode not in valid:
            raise ValueError(f"Invalid mode '{mode}'. Choose from: {valid}")
        self._patch(
            f"/gateway-devices/{device_id}/management-points/climateControl/characteristics/operationMode",
            {"value": mode},
        )

    def set_weather_regulation(self, device_id: str, enabled: bool) -> None:
        """Enable/disable weather compensation (Altherma feature)."""
        self._patch(
            f"/gateway-devices/{device_id}/management-points/climateControl/characteristics/weatherRegulatedControl",
            {"value": "on" if enabled else "off"},
        )

    def set_leaving_water_temperature(self, device_id: str, temperature: float) -> None:
        """Set leaving water temperature target (Altherma only)."""
        self._patch(
            f"/gateway-devices/{device_id}/management-points/climateControl/characteristics/temperatureControl",
            {
                "value": {
                    "operationModes": {
                        "heating": {
                            "setpoints": {
                                "leavingWaterOffset": {"value": temperature}
                            }
                        }
                    }
                }
            },
        )
