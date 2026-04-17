"""Tests for MCP server Fox ESS and Daikin tools (no stdio)."""
import pytest

pytest.importorskip("mcp", reason="Install the `mcp` package to run MCP server tests.")

import unittest
from unittest.mock import MagicMock, patch

from src.daikin.models import DaikinDevice
from src.foxess.models import RealTimeData
from src.mcp_server import build_mcp


def _fake_daikin_device(**kwargs: object) -> DaikinDevice:
    d = DaikinDevice(id="gw1", name="Test", raw={})
    d.model = "Altherma"
    d.is_on = True
    d.operation_mode = "heating"
    d.weather_regulation_enabled = False
    d.tank_target = 48.0
    for k, v in kwargs.items():
        setattr(d, k, v)
    return d


class TestMCPServerFoxTools(unittest.IsolatedAsyncioTestCase):
    async def test_get_soc_ok(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.get_cached_realtime") as gr, patch(
            "src.mcp_server.get_refresh_stats", return_value=(123.0, 5)
        ):
            gr.return_value = RealTimeData(
                soc=62.0,
                solar_power=1.0,
                grid_power=0.0,
                battery_power=0.5,
                load_power=1.2,
                work_mode="Self Use",
            )
            _blocks, out = await mcp.call_tool("get_soc", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["soc"], 62.0)
        self.assertEqual(out["work_mode"], "Self Use")
        self.assertEqual(out["refresh_count_24h"], 5)

    async def test_set_inverter_mode_read_only(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.config") as cfg, patch("src.mcp_server.safeguards.audit_log"):
            cfg.OPENCLAW_READ_ONLY = True
            _blocks, out = await mcp.call_tool("set_inverter_mode", {"mode": "Self Use"})
        self.assertFalse(out["ok"])
        self.assertIn("OPENCLAW_READ_ONLY", out["error"])

    async def test_set_inverter_mode_success(self) -> None:
        mcp = build_mcp()
        mock_client = MagicMock()
        with patch("src.mcp_server.config") as cfg, patch(
            "src.mcp_server._foxess_client", return_value=mock_client
        ):
            cfg.OPENCLAW_READ_ONLY = False
            _blocks, out = await mcp.call_tool("set_inverter_mode", {"mode": "Force charge"})
        self.assertTrue(out["ok"])
        mock_client.set_work_mode.assert_called_once_with("Force charge")


class TestMCPServerDaikinTools(unittest.IsolatedAsyncioTestCase):
    async def test_get_daikin_status_ok(self) -> None:
        mcp = build_mcp()
        dev = _fake_daikin_device()
        mock_client = MagicMock()
        mock_client.get_devices.return_value = [dev]
        mock_client.get_status.return_value = MagicMock(
            device_name="Test",
            is_on=True,
            mode="heating",
            room_temp=20.0,
            target_temp=21.0,
            outdoor_temp=5.0,
            lwt=35.0,
            lwt_offset=0.0,
            tank_temp=45.0,
            tank_target=48.0,
            weather_regulation=False,
        )
        with patch("src.mcp_server._daikin_client", return_value=mock_client):
            _blocks, out = await mcp.call_tool("get_daikin_status", {})
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["devices"]), 1)
        self.assertEqual(out["devices"][0]["device_id"], "gw1")
        self.assertEqual(out["devices"][0]["room_temp"], 20.0)

    async def test_set_daikin_temperature_read_only(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.config") as cfg, patch("src.mcp_server.safeguards.audit_log"):
            cfg.OPENCLAW_READ_ONLY = True
            _blocks, out = await mcp.call_tool(
                "set_daikin_temperature", {"temperature": 21.0}
            )
        self.assertFalse(out["ok"])
        self.assertIn("OPENCLAW_READ_ONLY", out["error"])

    async def test_set_daikin_temperature_weather_regulation(self) -> None:
        mcp = build_mcp()
        dev = _fake_daikin_device(weather_regulation_enabled=True)
        mock_client = MagicMock()
        mock_client.get_devices.return_value = [dev]
        with patch("src.mcp_server.config") as cfg, patch(
            "src.mcp_server._daikin_client", return_value=mock_client
        ), patch("src.mcp_server.safeguards.audit_log"):
            cfg.OPENCLAW_READ_ONLY = False
            _blocks, out = await mcp.call_tool(
                "set_daikin_temperature", {"temperature": 21.0}
            )
        self.assertFalse(out["ok"])
        self.assertIn("weather regulation", out["error"].lower())
        mock_client.set_temperature.assert_not_called()

    async def test_set_daikin_power_success(self) -> None:
        mcp = build_mcp()
        dev = _fake_daikin_device()
        mock_client = MagicMock()
        mock_client.get_devices.return_value = [dev]
        with patch("src.mcp_server.config") as cfg, patch(
            "src.mcp_server._daikin_client", return_value=mock_client
        ):
            cfg.OPENCLAW_READ_ONLY = False
            _blocks, out = await mcp.call_tool("set_daikin_power", {"on": True})
        self.assertTrue(out["ok"])
        mock_client.set_power.assert_called_once_with(dev, True)


if __name__ == "__main__":
    unittest.main()
