"""Unit tests for Fox ESS client and cache service (mocked)."""
import unittest
from unittest.mock import patch, MagicMock

from src.foxess.client import FoxESSClient
from src.foxess.models import RealTimeData, ChargePeriod
from src.foxess import service as foxess_service


class TestFoxESSClient(unittest.TestCase):

    def setUp(self):
        self.client = FoxESSClient(api_key="test_key", device_sn="TEST_SN")

    @patch.object(FoxESSClient, "_open_post")
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

    @patch.object(FoxESSClient, "_open_post")
    def test_set_work_mode(self, mock_post):
        mock_post.return_value = {}
        self.client.set_work_mode("Self Use")
        mock_post.assert_called_once()

    def test_invalid_work_mode(self):
        with self.assertRaises(ValueError):
            self.client.set_work_mode("Invalid Mode")

    @patch.object(FoxESSClient, "_open_post")
    def test_set_charge_period(self, mock_post):
        mock_post.return_value = {}
        period = ChargePeriod(start_time="00:30", end_time="05:00", target_soc=90)
        self.client.set_charge_period(0, period)
        mock_post.assert_called_once()
        call_body = mock_post.call_args[0][1]
        self.assertEqual(call_body["key"], "times1")


class TestFoxESSCache(unittest.TestCase):
    """Tests for get_cached_realtime caching behavior."""

    def setUp(self):
        # Cold cache so each test starts with no cached data
        foxess_service._last_realtime = None
        foxess_service._last_realtime_updated = None

    @patch("src.foxess.service._get_client")
    def test_first_call_fetches_and_caches(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_realtime.return_value = RealTimeData(soc=50.0, work_mode="Self Use")
        mock_get_client.return_value = mock_client

        with patch("src.foxess.service.time") as mock_time:
            mock_time.monotonic.return_value = 0
            data = foxess_service.get_cached_realtime(max_age_seconds=30)

        self.assertEqual(data.soc, 50.0)
        self.assertEqual(data.work_mode, "Self Use")
        mock_client.get_realtime.assert_called_once()

    @patch("src.foxess.service._get_client")
    def test_second_call_within_max_age_returns_cached(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_realtime.return_value = RealTimeData(soc=50.0, work_mode="Self Use")
        mock_get_client.return_value = mock_client

        with patch("src.foxess.service.time") as mock_time:
            mock_time.monotonic.side_effect = [0, 10]  # first call 0, second call 10 (< 30)
            first = foxess_service.get_cached_realtime(max_age_seconds=30)
            second = foxess_service.get_cached_realtime(max_age_seconds=30)

        self.assertEqual(first.soc, second.soc)
        mock_client.get_realtime.assert_called_once()

    @patch("src.foxess.service._get_client")
    def test_call_after_expiry_refetches(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_realtime.side_effect = [
            RealTimeData(soc=50.0, work_mode="Self Use"),
            RealTimeData(soc=60.0, work_mode="Feed-in Priority"),
        ]
        mock_get_client.return_value = mock_client

        with patch("src.foxess.service.time") as mock_time:
            mock_time.monotonic.side_effect = [0, 100]  # second call sees 100 > 30
            first = foxess_service.get_cached_realtime(max_age_seconds=30)
            second = foxess_service.get_cached_realtime(max_age_seconds=30)

        self.assertEqual(first.soc, 50.0)
        self.assertEqual(second.soc, 60.0)
        self.assertEqual(mock_client.get_realtime.call_count, 2)


class TestFoxESSStatusEndpoint(unittest.TestCase):
    """Integration-style test for GET /api/v1/foxess/status using cached realtime."""

    @patch("src.api.main.get_cached_realtime")
    def test_foxess_status_returns_cached_data(self, mock_get_cached):
        mock_get_cached.return_value = RealTimeData(
            soc=72.0,
            solar_power=2.5,
            grid_power=-0.3,
            battery_power=0.5,
            load_power=2.2,
            work_mode="Self Use",
        )
        from fastapi.testclient import TestClient
        from src.api.main import app

        client = TestClient(app)
        resp = client.get("/api/v1/foxess/status")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["soc"], 72.0)
        self.assertEqual(body["work_mode"], "Self Use")
        self.assertEqual(body["solar_power"], 2.5)
        self.assertEqual(body["load_power"], 2.2)
        mock_get_cached.assert_called_once()


if __name__ == "__main__":
    unittest.main()
