"""Unit tests for Fox ESS client and cache service (mocked)."""
import unittest
from unittest.mock import MagicMock, patch

import pytest

from src.foxess import service as foxess_service
from src.foxess.client import FoxESSClient
from src.foxess.models import ChargePeriod, RealTimeData


class TestFoxESSClient(unittest.TestCase):

    def setUp(self):
        self.client = FoxESSClient(api_key="test_key", device_sn="TEST_SN")

    def test_sn_scheduler_prefers_datalogger_serial(self):
        c = FoxESSClient(api_key="k", device_sn="INV_SN", scheduler_sn="DL_SN")
        self.assertEqual(c._sn_scheduler(), "DL_SN")
        c2 = FoxESSClient(api_key="k", device_sn="ONLY")
        self.assertEqual(c2._sn_scheduler(), "ONLY")

    @patch.object(FoxESSClient, "_open_post")
    def test_get_realtime(self, mock_post):
        mock_post.return_value = [
            {
                "deviceSN": "TEST_SN",
                "datas": [
                    {"variable": "SoC", "value": 75.0},
                    {"variable": "pvPower", "value": 3.2},
                    {"variable": "gridConsumptionPower", "value": 0.0},
                    {"variable": "feedinPower", "value": 1.5},
                    {"variable": "batChargePower", "value": 1.7},
                    {"variable": "batDischargePower", "value": 0.0},
                    {"variable": "loadsPower", "value": 1.9},
                    {"variable": "generationPower", "value": 3.2},
                    {"variable": "workMode", "value": "Self Use"},
                ],
            }
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
    def test_set_work_mode_payload_is_pascalcase(self, mock_post):
        """S10.6 (#173): Fox API expects key='WorkMode' (PascalCase) and value
        without spaces ('SelfUse', not 'Self Use'). Lowercase or spaces return
        API error 40257.
        """
        mock_post.return_value = {}
        self.client.set_work_mode("Self Use")
        path, body = mock_post.call_args.args
        assert path == "/device/setting/set"
        assert body["key"] == "WorkMode"
        assert body["value"] == "SelfUse"

    @patch.object(FoxESSClient, "_open_post")
    def test_set_work_mode_translates_force_charge(self, mock_post):
        mock_post.return_value = {}
        self.client.set_work_mode("Force charge")
        _, body = mock_post.call_args.args
        assert body["value"] == "ForceCharge"

    @patch.object(FoxESSClient, "_open_post")
    def test_set_min_soc_payload_is_pascalcase(self, mock_post):
        """S10.6 (#173): key='MinSocOnGrid' (PascalCase) + string-formatted value."""
        mock_post.return_value = {}
        self.client.set_min_soc(15)
        path, body = mock_post.call_args.args
        assert path == "/device/setting/set"
        assert body["key"] == "MinSocOnGrid"
        assert body["value"] == "15"  # string, not int

    def test_set_min_soc_validates_range(self):
        with self.assertRaises(ValueError):
            self.client.set_min_soc(5)
        with self.assertRaises(ValueError):
            self.client.set_min_soc(101)

    @patch.object(FoxESSClient, "_open_post")
    def test_get_realtime_work_mode_numeric(self, mock_post):
        """API may return workMode as numeric code (e.g. 0 = Self Use)."""
        mock_post.return_value = [
            {
                "deviceSN": "TEST_SN",
                "datas": [
                    {"variable": "SoC", "value": 50.0},
                    {"variable": "pvPower", "value": 0.0},
                    {"variable": "gridConsumptionPower", "value": 0.0},
                    {"variable": "feedinPower", "value": 0.0},
                    {"variable": "batChargePower", "value": 0.0},
                    {"variable": "batDischargePower", "value": 0.0},
                    {"variable": "loadsPower", "value": 0.0},
                    {"variable": "generationPower", "value": 0.0},
                    {"variable": "workMode", "value": 0},
                ],
            }
        ]
        data = self.client.get_realtime()
        self.assertEqual(data.work_mode, "Self Use")

    @patch.object(FoxESSClient, "_open_post")
    def test_get_realtime_work_mode_empty(self, mock_post):
        """When workMode is missing or empty, return 'unknown' not empty string."""
        mock_post.return_value = [
            {
                "deviceSN": "TEST_SN",
                "datas": [
                    {"variable": "SoC", "value": 50.0},
                    {"variable": "pvPower", "value": 0.0},
                    {"variable": "gridConsumptionPower", "value": 0.0},
                    {"variable": "feedinPower", "value": 0.0},
                    {"variable": "batChargePower", "value": 0.0},
                    {"variable": "batDischargePower", "value": 0.0},
                    {"variable": "loadsPower", "value": 0.0},
                    {"variable": "generationPower", "value": 0.0},
                ],
            }
        ]
        data = self.client.get_realtime()
        self.assertEqual(data.work_mode, "unknown")

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
        foxess_service._last_realtime_updated_monotonic = None
        foxess_service._last_realtime_wallclock = None
        foxess_service._refresh_timestamps = []

    @patch("src.foxess.service._get_client")
    def test_first_call_fetches_and_caches(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_realtime.return_value = RealTimeData(soc=50.0, work_mode="Self Use")
        mock_get_client.return_value = mock_client

        with patch("src.foxess.service.time") as mock_time:
            mock_time.monotonic.return_value = 0
            mock_time.time.return_value = 1000.0  # floats so _record_realtime_refresh() comparisons work
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
            mock_time.time.side_effect = [1000.0, 1010.0]  # one per get_cached_realtime -> _record_realtime_refresh
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
            mock_time.time.side_effect = [1000.0, 1100.0]  # two refreshes
            first = foxess_service.get_cached_realtime(max_age_seconds=30)
            second = foxess_service.get_cached_realtime(max_age_seconds=30)

        self.assertEqual(first.soc, 50.0)
        self.assertEqual(second.soc, 60.0)
        self.assertEqual(mock_client.get_realtime.call_count, 2)


class TestFoxESSStatusEndpoint(unittest.TestCase):
    """Integration-style test for GET /api/v1/foxess/status using cached realtime."""

    def setUp(self):
        # Avoid get_refresh_stats() seeing MagicMocks from cache tests (it uses _refresh_timestamps / _last_realtime_wallclock)
        foxess_service._refresh_timestamps = []
        foxess_service._last_realtime_wallclock = None

    def test_foxess_status_returns_cached_data(self) -> None:
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        import src.api.main as api_main

        with patch.object(api_main, "get_cached_realtime") as mock_get_cached:
            mock_get_cached.return_value = RealTimeData(
                soc=72.0,
                solar_power=2.5,
                grid_power=-0.3,
                battery_power=0.5,
                load_power=2.2,
                work_mode="Self Use",
            )
            client = TestClient(api_main.app)
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
