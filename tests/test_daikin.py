"""Unit tests for Daikin Onecta client (mocked)."""
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch, MagicMock, call
from src.daikin.client import DaikinClient
from src.daikin.models import DaikinDevice, TemperatureControlSettings


MOCK_DEVICE_PAYLOAD = [{
    "id": "abc-123",
    "embeddedId": "living-room",
    "deviceModel": "Altherma",
    "managementPoints": [{
        "embeddedId": "climateControlMainZone",
        "managementPointType": "climateControl",
        "onOffMode": {"value": "on"},
        "operationMode": {"value": "heating"},
        "temperatureControl": {
            "value": {
                "operationModes": {
                    "heating": {
                        "setpoints": {
                            "roomTemperature": {"value": 21.0}
                        }
                    }
                }
            }
        },
        "sensoryData": {
            "value": {
                "roomTemperature": {"value": 19.5},
                "outdoorTemperature": {"value": 7.0},
            }
        },
        "setpointMode": {"value": "weatherDependent"},
    }]
}]

MOCK_ALTHERMA_PAYLOAD = [{
    "id": "alt-456",
    "embeddedId": "altherma-unit",
    "deviceModel": "Altherma",
    "managementPoints": [
        {
            "embeddedId": "climateControlMainZone",
            "managementPointType": "climateControlMainZone",
            "onOffMode": {"value": "off"},
            "operationMode": {"value": "cooling"},
            "temperatureControl": {
                "value": {
                    "operationModes": {
                        "cooling": {
                            "setpoints": {
                                "roomTemperature": {"value": 24.0}
                            }
                        }
                    }
                }
            },
            "sensoryData": {
                "value": {
                    "roomTemperature": {"value": 26.0},
                    "outdoorTemperature": {"value": 30.0},
                    "leavingWaterTemperature": {"value": 35.0},
                }
            },
            "setpointMode": {"value": "fixed"},
        },
        {
            "embeddedId": "domesticHotWaterTank",
            "managementPointType": "domesticHotWaterTank",
            "sensoryData": {
                "value": {
                    "tankTemperature": {"value": 45.0}
                }
            },
            "temperatureControl": {
                "value": {
                    "operationModes": {
                        "heating": {
                            "setpoints": {
                                "domesticHotWaterTemperature": {"value": 50.0}
                            }
                        }
                    }
                }
            },
        },
    ]
}]


class TestDaikinClient(unittest.TestCase):

    @patch.object(DaikinClient, "_get")
    def test_get_devices(self, mock_get):
        mock_get.return_value = MOCK_DEVICE_PAYLOAD
        client = DaikinClient()
        devices = client.get_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].id, "abc-123")
        self.assertTrue(devices[0].is_on)
        self.assertEqual(devices[0].operation_mode, "heating")
        self.assertEqual(devices[0].temperature.set_point, 21.0)
        self.assertEqual(devices[0].temperature.room_temperature, 19.5)

    @patch.object(DaikinClient, "_patch")
    @patch.object(DaikinClient, "_get")
    def test_set_temperature(self, mock_get, mock_patch):
        mock_get.return_value = MOCK_DEVICE_PAYLOAD
        mock_patch.return_value = {}
        client = DaikinClient()
        dev = client.get_devices()[0]
        client.set_temperature(dev, 22.0, "heating")
        mock_patch.assert_called_once()

    @patch.object(DaikinClient, "_patch")
    @patch.object(DaikinClient, "_get")
    def test_set_power_off(self, mock_get, mock_patch):
        mock_get.return_value = MOCK_DEVICE_PAYLOAD
        mock_patch.return_value = {}
        client = DaikinClient()
        dev = client.get_devices()[0]
        client.set_power(dev, False)
        call_body = mock_patch.call_args[0][1]
        self.assertEqual(call_body["value"], "off")

    @patch.object(DaikinClient, "_get")
    def test_get_devices_altherma(self, mock_get):
        mock_get.return_value = MOCK_ALTHERMA_PAYLOAD
        client = DaikinClient()
        devices = client.get_devices()
        self.assertEqual(len(devices), 1)
        dev = devices[0]
        self.assertEqual(dev.id, "alt-456")
        self.assertFalse(dev.is_on)
        self.assertEqual(dev.operation_mode, "cooling")
        self.assertEqual(dev.temperature.set_point, 24.0)
        self.assertEqual(dev.temperature.room_temperature, 26.0)
        self.assertEqual(dev.temperature.outdoor_temperature, 30.0)
        self.assertEqual(dev.leaving_water_temperature, 35.0)
        self.assertEqual(dev.tank_temperature, 45.0)
        self.assertEqual(dev.tank_target, 50.0)
        self.assertFalse(dev.weather_regulation_enabled)

    @patch.object(DaikinClient, "_get")
    def test_get_status(self, mock_get):
        mock_get.return_value = MOCK_DEVICE_PAYLOAD
        client = DaikinClient()
        devices = client.get_devices()
        status = client.get_status(devices[0])
        self.assertEqual(status.device_name, "living-room")
        self.assertTrue(status.is_on)
        self.assertEqual(status.mode, "heating")
        self.assertEqual(status.target_temp, 21.0)
        self.assertEqual(status.room_temp, 19.5)
        self.assertEqual(status.outdoor_temp, 7.0)
        self.assertTrue(status.weather_regulation)

    @patch.object(DaikinClient, "_patch")
    @patch.object(DaikinClient, "_get")
    def test_set_weather_regulation(self, mock_get, mock_patch):
        mock_get.return_value = MOCK_DEVICE_PAYLOAD
        mock_patch.return_value = {}
        client = DaikinClient()
        dev = client.get_devices()[0]
        client.set_weather_regulation(dev, True)
        call_body = mock_patch.call_args[0][1]
        self.assertEqual(call_body["value"], "weatherDependent")

    @patch.object(DaikinClient, "_patch")
    @patch.object(DaikinClient, "_get")
    def test_set_operation_mode(self, mock_get, mock_patch):
        mock_get.return_value = MOCK_DEVICE_PAYLOAD
        mock_patch.return_value = {}
        client = DaikinClient()
        dev = client.get_devices()[0]
        client.set_operation_mode(dev, "cooling")
        mock_patch.assert_called_once()

    @patch.object(DaikinClient, "_get")
    def test_invalid_mode(self, mock_get):
        mock_get.return_value = MOCK_DEVICE_PAYLOAD
        client = DaikinClient()
        dev = client.get_devices()[0]
        with self.assertRaises(ValueError):
            client.set_operation_mode(dev, "turbo_mode")

    @patch.object(DaikinClient, "_patch")
    @patch.object(DaikinClient, "_get")
    def test_set_tank_temperature(self, mock_get, mock_patch):
        mock_get.return_value = MOCK_ALTHERMA_PAYLOAD
        mock_patch.return_value = {}
        client = DaikinClient()
        dev = client.get_devices()[0]
        client.set_tank_temperature(dev, 48.0)
        path, body = mock_patch.call_args[0]
        self.assertIn("domesticHotWaterTank", path)
        self.assertEqual(
            body["value"]["operationModes"]["heating"]["setpoints"]["domesticHotWaterTemperature"]["value"],
            48.0,
        )

    @patch.object(DaikinClient, "_get")
    def test_empty_device_list(self, mock_get):
        mock_get.return_value = []
        client = DaikinClient()
        devices = client.get_devices()
        self.assertEqual(devices, [])

    @patch("src.daikin.client.get_valid_access_token")
    @patch("urllib.request.urlopen")
    def test_get_retries_once_on_401(self, mock_urlopen, mock_token):
        mock_token.side_effect = ["first", "second"]
        err401 = urllib.error.HTTPError(
            "http://example", 401, "Unauthorized", None, BytesIO(b"{}")
        )
        ok = MagicMock()
        ok.read.return_value = b"[]"
        mock_urlopen.side_effect = [err401, ok]
        client = DaikinClient()
        result = client._get("/gateway-devices")
        self.assertEqual(result, [])
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertEqual(
            mock_token.call_args_list,
            [call(force_refresh=False), call(force_refresh=True)],
        )

    @patch("src.daikin.client.time.sleep", autospec=True)
    @patch("src.daikin.client.get_valid_access_token", return_value="t")
    @patch("urllib.request.urlopen")
    def test_get_retries_on_429_then_ok(self, mock_urlopen, _mock_token, mock_sleep):
        err429 = urllib.error.HTTPError(
            "http://example", 429, "Too Many", None, BytesIO(b"{}")
        )
        err429.headers = {"Retry-After": "1"}
        ok = MagicMock()
        ok.read.return_value = b"[]"
        mock_urlopen.side_effect = [err429, ok]
        client = DaikinClient()
        result = client._get("/gateway-devices")
        self.assertEqual(result, [])
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
