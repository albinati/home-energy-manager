"""Unit tests for Daikin Onecta client (mocked)."""
import unittest
from unittest.mock import patch, MagicMock
from src.daikin.client import DaikinClient
from src.daikin.models import DaikinDevice, TemperatureControlSettings


MOCK_DEVICE_PAYLOAD = [{
    "id": "abc-123",
    "embeddedId": "living-room",
    "modelInfo": "EHBX16CB3V",
    "managementPoints": [{
        "managementPointType": "climateControl",
        "characteristics": {
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
            "weatherRegulatedControl": {"value": "on"},
        }
    }]
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
        client.set_temperature("abc-123", 22.0, "heating")
        mock_patch.assert_called_once()

    @patch.object(DaikinClient, "_patch")
    @patch.object(DaikinClient, "_get")
    def test_set_power_off(self, mock_get, mock_patch):
        mock_get.return_value = MOCK_DEVICE_PAYLOAD
        mock_patch.return_value = {}
        client = DaikinClient()
        client.set_power("abc-123", False)
        call_body = mock_patch.call_args[0][1]
        self.assertEqual(call_body["value"], "off")

    def test_invalid_mode(self):
        client = DaikinClient()
        with self.assertRaises(ValueError):
            client.set_operation_mode("abc-123", "turbo_mode")


if __name__ == "__main__":
    unittest.main()
