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


class TestFoxEnergyPvMapping(unittest.TestCase):
    """PV vs generation regression (Fox `generation` = AC output incl. battery
    discharge; `PVEnergyTotal` = true panel-side PV). The energy report path
    must map PV from PVEnergyTotal, not generation, or the "solar produced" /
    self-sufficiency figures inflate (~8x in winter) and the load-balance
    estimate double-counts discharge.
    """

    def setUp(self):
        self.client = FoxESSClient(api_key="test_key", device_sn="TEST_SN")

    @staticmethod
    def _report(generation, pv, *, feedin=0.5, grid=100.0, charge=50.0, discharge=45.0):
        items = [
            {"variable": "feedin", "values": [feedin]},
            {"variable": "gridConsumption", "values": [grid]},
            {"variable": "chargeEnergyToTal", "values": [charge]},
            {"variable": "dischargeEnergyToTal", "values": [discharge]},
        ]
        if generation is not None:
            items.append({"variable": "generation", "values": [generation]})
        if pv is not None:
            items.append({"variable": "PVEnergyTotal", "values": [pv]})
        return items

    @patch.object(FoxESSClient, "_open_post")
    def test_month_report_requests_pvenergytotal(self, mock_post):
        mock_post.return_value = self._report(256.0, 32.1)
        self.client._get_energy_month_report(2026, 1)
        _, body = mock_post.call_args.args
        self.assertIn("PVEnergyTotal", body["variables"])

    @patch.object(FoxESSClient, "_open_post")
    def test_month_report_pv_from_pvenergytotal_not_generation(self, mock_post):
        mock_post.return_value = self._report(256.0, 32.1)
        out = self.client._get_energy_month_report(2026, 1)
        self.assertAlmostEqual(out["pvEnergyToday"], 32.1)
        self.assertNotAlmostEqual(out["pvEnergyToday"], 256.0)

    @patch.object(FoxESSClient, "_open_post")
    def test_month_report_zero_pv_day_stays_zero(self, mock_post):
        # A legitimate zero-PV winter day must NOT fall through to generation.
        mock_post.return_value = self._report(200.0, 0.0)
        out = self.client._get_energy_month_report(2026, 1)
        self.assertEqual(out["pvEnergyToday"], 0.0)

    @patch.object(FoxESSClient, "_open_post")
    def test_month_report_falls_back_to_generation_when_pv_absent(self, mock_post):
        # Older firmware / cloud variant that doesn't return PVEnergyTotal.
        mock_post.return_value = self._report(180.0, None)
        out = self.client._get_energy_month_report(2026, 1)
        self.assertAlmostEqual(out["pvEnergyToday"], 180.0)

    @patch.object(FoxESSClient, "_open_post")
    def test_month_report_load_balance_no_discharge_double_count(self, mock_post):
        # load = grid + PV + discharge - feedin - charge, with TRUE pv.
        mock_post.return_value = self._report(
            256.0, 32.1, feedin=0.5, grid=100.0, charge=50.0, discharge=45.0
        )
        out = self.client._get_energy_month_report(2026, 1)
        expected = 100.0 + 32.1 + 45.0 - 0.5 - 50.0
        self.assertAlmostEqual(out["loadEnergyToday"], round(expected, 2))

    @patch.object(FoxESSClient, "_open_post")
    def test_daily_breakdown_solar_from_pvenergytotal(self, mock_post):
        mock_post.return_value = [
            {"variable": "generation", "values": [8.0, 9.0]},
            {"variable": "PVEnergyTotal", "values": [1.0, 1.2]},
            {"variable": "feedin", "values": [0.0, 0.0]},
            {"variable": "gridConsumption", "values": [5.0, 6.0]},
            {"variable": "chargeEnergyToTal", "values": [3.0, 3.0]},
            {"variable": "dischargeEnergyToTal", "values": [7.0, 7.5]},
        ]
        _, daily = self.client.get_energy_month_daily_breakdown(2026, 1)
        self.assertAlmostEqual(daily[0]["solar_kwh"], 1.0)
        self.assertAlmostEqual(daily[1]["solar_kwh"], 1.2)
        self.assertIn("PVEnergyTotal", mock_post.call_args.args[1]["variables"])
        # Per-row load balance uses true PV → no discharge double-count.
        # load = import + PV + discharge - export - charge (loads absent).
        self.assertAlmostEqual(daily[0]["load_kwh"], round(5.0 + 1.0 + 7.0 - 0.0 - 3.0, 2))
        self.assertAlmostEqual(daily[1]["load_kwh"], round(6.0 + 1.2 + 7.5 - 0.0 - 3.0, 2))

    @patch.object(FoxESSClient, "_open_post")
    def test_daily_breakdown_empty_pv_array_falls_back_to_generation(self, mock_post):
        # API anomaly: PVEnergyTotal present but empty → must not zero the month.
        mock_post.return_value = [
            {"variable": "generation", "values": [8.0, 9.0]},
            {"variable": "PVEnergyTotal", "values": []},
            {"variable": "gridConsumption", "values": [5.0, 6.0]},
        ]
        _, daily = self.client.get_energy_month_daily_breakdown(2026, 1)
        self.assertAlmostEqual(daily[0]["solar_kwh"], 8.0)
        self.assertAlmostEqual(daily[1]["solar_kwh"], 9.0)


if __name__ == "__main__":
    unittest.main()
