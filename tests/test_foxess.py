"""Unit tests for Fox ESS client (mocked)."""
import unittest
from unittest.mock import patch, MagicMock
from src.foxess.client import FoxESSClient
from src.foxess.models import RealTimeData, ChargePeriod


class TestFoxESSClient(unittest.TestCase):

    def setUp(self):
        self.client = FoxESSClient(api_key="test_key", device_sn="TEST_SN")

    @patch.object(FoxESSClient, "_post")
    def test_get_realtime(self, mock_post):
        mock_post.return_value = [
            {"variable": "SoC", "value": 75.0},
            {"variable": "pvPower", "value": 3.2},
            {"variable": "gridConsumptionPower", "value": 0.0},
            {"variable": "feedinPower", "value": 1.5},
            {"variable": "batChargePower", "value": 1.7},
            {"variable": "batDischargePower", "value": 0.0},
            {"variable": "loadsPower", "value": 1.9},
            {"variable": "generationPower", "value": 3.2},
            {"variable": "workMode", "value": "Self Use"},
        ]
        data = self.client.get_realtime()
        self.assertEqual(data.soc, 75.0)
        self.assertEqual(data.solar_power, 3.2)
        self.assertEqual(data.work_mode, "Self Use")
        self.assertAlmostEqual(data.battery_power, 1.7)
        self.assertAlmostEqual(data.grid_power, -1.5)

    @patch.object(FoxESSClient, "_post")
    def test_set_work_mode(self, mock_post):
        mock_post.return_value = {}
        self.client.set_work_mode("Self Use")
        mock_post.assert_called_once()

    def test_invalid_work_mode(self):
        with self.assertRaises(ValueError):
            self.client.set_work_mode("Invalid Mode")

    @patch.object(FoxESSClient, "_post")
    def test_set_charge_period(self, mock_post):
        mock_post.return_value = {}
        period = ChargePeriod(start_time="00:30", end_time="05:00", target_soc=90)
        self.client.set_charge_period(0, period)
        mock_post.assert_called_once()
        call_body = mock_post.call_args[0][1]
        self.assertEqual(call_body["key"], "times1")


if __name__ == "__main__":
    unittest.main()
