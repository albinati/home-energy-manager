"""Tests for simulate_plan MCP tool (Phase 4.4, #43).

Contract:
- Valid whitelisted overrides → returns plan dict.
- Non-whitelisted override key → returns {ok: False, error: "unsupported override key"}.
- No record_call("daikin", ...) during a simulate call (simulation is quota-free).
- No writes to action_schedule / optimization_plans during a simulate call.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("mcp", reason="Install the `mcp` package to run MCP server tests.")

from src.mcp_server import build_mcp


def _fake_sim_result() -> MagicMock:
    """Canned LpSimulationResult stand-in."""
    result = MagicMock()
    result.ok = True
    result.plan_date = "2026-04-22"
    result.plan_window = "full_day"
    result.slot_count = 48
    result.objective_pence = -125.50
    result.status = "Optimal"
    result.actual_mean_agile_pence = 18.4
    result.forecast_solar_kwh_horizon = 6.2
    result.pv_scale_factor = 0.87
    result.mu_load_kwh = 0.42
    result.initial = MagicMock(soc_kwh=5.2, tank_temp_c=45.0, indoor_temp_c=20.1)
    result.plan = MagicMock(ok=True, objective_pence=-125.50, status="Optimal")
    result.error = None
    return result


class TestSimulatePlanTool(unittest.IsolatedAsyncioTestCase):
    async def test_simulate_plan_valid_overrides_returns_plan(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation", return_value=_fake_sim_result()) as sim:
            _blocks, out = await mcp.call_tool(
                "simulate_plan",
                {"overrides": {"dhw_temp_normal_c": 48.0, "target_dhw_min_guests_c": 55.0}},
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["plan_date"], "2026-04-22")
        self.assertEqual(out["slot_count"], 48)
        self.assertEqual(out["status"], "Optimal")
        sim.assert_called_once()

    async def test_simulate_plan_rejects_unknown_override_key(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation") as sim:
            _blocks, out = await mcp.call_tool(
                "simulate_plan",
                {"overrides": {"secret_backdoor": "oops"}},
            )
        self.assertFalse(out["ok"])
        self.assertIn("unsupported override key", out["error"])
        sim.assert_not_called()

    async def test_simulate_plan_no_overrides_still_works(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation", return_value=_fake_sim_result()):
            _blocks, out = await mcp.call_tool("simulate_plan", {})
        self.assertTrue(out["ok"])

    async def test_simulate_plan_does_not_call_record_call(self) -> None:
        """simulate_plan must not burn Daikin quota."""
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation", return_value=_fake_sim_result()):
            with patch("src.api_quota.record_call") as rc:
                _blocks, _out = await mcp.call_tool(
                    "simulate_plan",
                    {"overrides": {"optimization_preset": "guests"}},
                )
        rc.assert_not_called()

    async def test_simulate_plan_restores_config_after_override(self) -> None:
        """Overrides are applied only during the call; config is restored afterwards."""
        from src.config import config as cfg

        mcp = build_mcp()
        original = cfg.DHW_TEMP_NORMAL_C
        with patch("src.mcp_server.run_lp_simulation", return_value=_fake_sim_result()):
            await mcp.call_tool(
                "simulate_plan",
                {"overrides": {"dhw_temp_normal_c": original + 5.0}},
            )
        self.assertEqual(cfg.DHW_TEMP_NORMAL_C, original)

    # Phase 4 review C2: input validation ───────────────────────────────────

    async def test_simulate_plan_rejects_out_of_range_dhw_temp(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation") as sim:
            _blocks, out = await mcp.call_tool(
                "simulate_plan", {"overrides": {"dhw_temp_normal_c": 9999.0}}
            )
        self.assertFalse(out["ok"])
        self.assertIn("out of range", out["error"])
        sim.assert_not_called()

    async def test_simulate_plan_rejects_non_numeric_dhw_temp(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation") as sim:
            _blocks, out = await mcp.call_tool(
                "simulate_plan", {"overrides": {"dhw_temp_normal_c": "hot"}}
            )
        self.assertFalse(out["ok"])
        self.assertIn("must be a number", out["error"])
        sim.assert_not_called()

    async def test_simulate_plan_rejects_invalid_preset(self) -> None:
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation") as sim:
            _blocks, out = await mcp.call_tool(
                "simulate_plan", {"overrides": {"optimization_preset": "nuclear_meltdown"}}
            )
        self.assertFalse(out["ok"])
        self.assertIn("optimization_preset", out["error"])
        sim.assert_not_called()

    # Phase 4 review C3: whitelist drift ────────────────────────────────────

    async def test_simulate_plan_reports_ignored_overrides(self) -> None:
        """residents / extra_visitors / occupancy_mode are validated but not yet wired
        to config; response must show them as ignored rather than silently applied."""
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation", return_value=_fake_sim_result()):
            _blocks, out = await mcp.call_tool(
                "simulate_plan",
                {"overrides": {"residents": 4, "extra_visitors": 2, "dhw_temp_normal_c": 48.0}},
            )
        self.assertTrue(out["ok"])
        self.assertEqual(out["ignored_overrides"], {"residents": 4, "extra_visitors": 2})
        self.assertEqual(out["applied_overrides"], {"dhw_temp_normal_c": 48.0})

    # Phase 4 review C4: response shape ─────────────────────────────────────

    async def test_simulate_plan_response_shape_matches_across_ok_and_error(self) -> None:
        """Consumers must be able to parse the response without branching on ok."""
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation", return_value=_fake_sim_result()):
            _b, ok_resp = await mcp.call_tool("simulate_plan", {})
        _b, err_resp = await mcp.call_tool(
            "simulate_plan", {"overrides": {"secret": "bad"}}
        )
        self.assertEqual(set(ok_resp.keys()), set(err_resp.keys()))

    # Phase 4 review C10: no cache refresh (quota burn) during simulate ─────

    async def test_simulate_plan_passes_allow_daikin_refresh_false(self) -> None:
        """simulate_plan must ask run_lp_simulation to forbid any Daikin cache refresh
        so a cold MCP cache cannot burn quota during 'read-only' simulation."""
        mcp = build_mcp()
        with patch("src.mcp_server.run_lp_simulation", return_value=_fake_sim_result()) as sim:
            await mcp.call_tool("simulate_plan", {})
        sim.assert_called_once()
        kwargs = sim.call_args.kwargs
        self.assertIn("allow_daikin_refresh", kwargs)
        self.assertIs(kwargs["allow_daikin_refresh"], False)
