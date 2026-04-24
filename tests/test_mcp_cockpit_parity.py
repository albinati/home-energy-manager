"""MCP parity with cockpit — the tools added for PR A.

Each new tool wraps an existing FastAPI endpoint or db helper so an LLM
client sees the same data as the web cockpit. These tests run the tool
handlers through FastMCP's call_tool machinery so the contract is
exercised end-to-end (tool discovery + argument schema + return shape).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("mcp", reason="Install the `mcp` package to run MCP server tests.")

import unittest

from src import db
from src.mcp_server import build_mcp


class _DBReady(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        db.init_db()


class TestCockpitParityTools(_DBReady):
    async def test_get_system_timezone_shape(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_system_timezone", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["plan_push_tz"], "UTC")
        self.assertIn("planner_tz", out)
        self.assertTrue(out["now_utc"].endswith("Z"))

    async def test_get_cockpit_now_returns_shape(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_cockpit_now", {})
        self.assertTrue(out["ok"])
        # Contract mirrors /api/v1/cockpit/now.
        for key in ("now_utc", "planner_tz", "current_slot", "state",
                    "freshness", "thresholds", "modes", "plan_date"):
            self.assertIn(key, out)

    async def test_get_cockpit_at_rejects_bad_iso(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_cockpit_at", {"when": "not-a-date"})
        # The endpoint raises HTTPException(400); MCP wraps as ok=False.
        self.assertFalse(out["ok"])

    async def test_get_cockpit_at_empty_history_returns_ok_with_nulls(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_cockpit_at", {"when": "2026-04-24T12:00:00Z"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["source"]["run_id"])

    async def test_get_optimization_inputs_shape(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_optimization_inputs", {"horizon_hours": 6})
        self.assertTrue(out["ok"])
        self.assertEqual(out["horizon_hours"], 6)
        self.assertIn("slots", out)
        self.assertIn("initial", out)
        self.assertIn("thresholds", out)
        self.assertIn("config_snapshot", out)

    async def test_get_attribution_day_handles_missing_row(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_attribution_day", {"date": "1999-01-01"}
        )
        self.assertTrue(out["ok"])
        self.assertFalse(out["available"])
        self.assertIsNone(out["shares"])


class TestLpSnapshotTools(_DBReady):
    def _seed_one_run(self) -> int:
        """Log an optimizer run + persist a tiny snapshot so the lookup tools have rows."""
        run_at = datetime.now(UTC).isoformat()
        run_id = db.log_optimizer_run({
            "run_at": run_at, "rates_count": 2, "cheap_slots": 0, "peak_slots": 0,
            "standard_slots": 2, "negative_slots": 0,
            "target_vwap": 18.0, "actual_agile_mean": 20.0, "battery_warning": False,
            "strategy_summary": "t", "fox_schedule_uploaded": True, "daikin_actions_count": 0,
        })
        db.save_lp_snapshots(
            run_id,
            {
                "run_at_utc": run_at,
                "plan_date": "2026-04-24",
                "horizon_hours": 4,
                "soc_initial_kwh": 5.0,
                "tank_initial_c": 46.0,
                "indoor_initial_c": 20.5,
                "soc_source": "fox_realtime_cache",
                "tank_source": "daikin_cache",
                "indoor_source": "daikin_cache",
                "base_load_json": "[]",
                "micro_climate_offset_c": 0.0,
                "config_snapshot_json": "{}",
                "price_quantize_p": 0.0,
                "peak_threshold_p": 25.0,
                "cheap_threshold_p": 12.0,
                "daikin_control_mode": "passive",
                "optimization_preset": "normal",
                "energy_strategy_mode": "savings_first",
            },
            [
                {
                    "slot_index": 0,
                    "slot_time_utc": "2026-04-24T00:00:00+00:00",
                    "price_p": 14.0, "import_kwh": 0.3, "export_kwh": 0.0,
                    "charge_kwh": 0.1, "discharge_kwh": 0.0, "pv_use_kwh": 0.0,
                    "pv_curtail_kwh": 0.0, "dhw_kwh": 0.0, "space_kwh": 0.0,
                    "soc_kwh": 5.1, "tank_temp_c": 46.1, "indoor_temp_c": 20.6,
                    "outdoor_temp_c": 10.0, "lwt_offset_c": 0.0,
                },
            ],
        )
        return run_id

    async def test_get_lp_solution_returns_slots_and_inputs(self) -> None:
        run_id = self._seed_one_run()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_lp_solution", {"run_id": run_id})
        self.assertTrue(out["ok"])
        self.assertEqual(out["run_id"], run_id)
        self.assertEqual(len(out["slots"]), 1)
        self.assertIsNotNone(out["inputs"])
        self.assertEqual(out["inputs"]["plan_date"], "2026-04-24")

    async def test_find_lp_run_for_time_picks_run(self) -> None:
        run_id = self._seed_one_run()
        mcp = build_mcp()
        # Query a "now" far in the future so our seeded run qualifies.
        _blocks, out = await mcp.call_tool(
            "find_lp_run_for_time",
            {"when_utc": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["run_id"], run_id)

    async def test_find_lp_run_for_time_none_before_first_run(self) -> None:
        self._seed_one_run()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "find_lp_run_for_time",
            {"when_utc": "1999-01-01T00:00:00Z"},
        )
        self.assertTrue(out["ok"])
        self.assertIsNone(out["run_id"])


class TestHistoryTools(_DBReady):
    async def test_get_meteo_forecast_history_roundtrip(self) -> None:
        fetch_at = datetime.now(UTC).isoformat()
        db.save_meteo_forecast_history(
            fetch_at,
            [{"slot_time": "2026-04-24T12:00:00+00:00", "temp_c": 12.0, "solar_w_m2": 250.0}],
        )
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_meteo_forecast_history", {"fetch_at_utc": fetch_at}
        )
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["temp_c"], 12.0)

    async def test_get_config_audit_roundtrip(self) -> None:
        db.log_config_change("DHW_TEMP_NORMAL_C", "46.0", op="set", actor="test")
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_config_audit", {"key": "DHW_TEMP_NORMAL_C"}
        )
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["value"], "46.0")

    async def test_get_daikin_telemetry_history_returns_list(self) -> None:
        # Seed one row so the query has something to return.
        import time as _time
        conn = db.get_connection()
        try:
            conn.execute(
                "INSERT INTO daikin_telemetry (fetched_at, source, tank_temp_c) VALUES (?, ?, ?)",
                (_time.time(), "live", 46.0),
            )
            conn.commit()
        finally:
            conn.close()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_daikin_telemetry_history", {"limit": 10})
        self.assertTrue(out["ok"])
        self.assertIsInstance(out["rows"], list)
        self.assertGreaterEqual(len(out["rows"]), 1)
