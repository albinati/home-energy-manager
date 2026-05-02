"""MCP surfaces the new structured PnL fields so OpenClaw doesn't parse markdown.

Follow-up to #207: the brief used to be markdown-only, leaving OpenClaw to
guess at numbers from prose. These tests lock that the MCP tools
(``get_energy_metrics``, ``get_daily_brief``, ``get_night_brief``,
``get_tariff_comparison``) expose the structured fields the user asked for —
net cost, import/standing/export breakdown, BG comparison when configured,
mode status, forecasted-export estimate.
"""
from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

import pytest

pytest.importorskip("mcp", reason="Install the `mcp` package to run MCP server tests.")

from src import db
from src.config import config as app_config
from src.mcp_server import build_mcp


class TestMCPBriefData(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db.init_db()
        # Set FIXED_TARIFF_* so the BG comparison line is exercised.
        self._orig = {
            k: getattr(app_config, k, None)
            for k in (
                "FIXED_TARIFF_LABEL",
                "FIXED_TARIFF_RATE_PENCE",
                "FIXED_TARIFF_STANDING_PENCE_PER_DAY",
                "MANUAL_STANDING_CHARGE_PENCE_PER_DAY",
                "OCTOPUS_EXPORT_TARIFF_CODE",
                "BULLETPROOF_TIMEZONE",
                "DAIKIN_CONTROL_MODE",
            )
        }
        app_config.FIXED_TARIFF_LABEL = "British Gas Fixed v58"
        app_config.FIXED_TARIFF_RATE_PENCE = 20.70
        app_config.FIXED_TARIFF_STANDING_PENCE_PER_DAY = 41.14
        app_config.MANUAL_STANDING_CHARGE_PENCE_PER_DAY = 62.22
        app_config.OCTOPUS_EXPORT_TARIFF_CODE = "AGILE-OUT-TEST-MCP"
        app_config.BULLETPROOF_TIMEZONE = "Europe/London"
        app_config.DAIKIN_CONTROL_MODE = "passive"

    def tearDown(self) -> None:
        for k, v in self._orig.items():
            if v is None:
                continue
            setattr(app_config, k, v)

    def _seed_yesterday(self) -> date:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/London")
        yesterday = (datetime.now(tz).date() - timedelta(days=1))
        slot = datetime.combine(yesterday, datetime.min.time()).replace(
            hour=12, tzinfo=UTC,
        )
        db.log_execution({
            "timestamp": slot.isoformat().replace("+00:00", "Z"),
            "consumption_kwh": 5.0,
            "agile_price_pence": 15.0,
            "slot_kind": "standard",
        })
        db.save_agile_export_rates([{
            "valid_from": slot.isoformat().replace("+00:00", "Z"),
            "valid_to": (slot + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": 25.0,
        }], "AGILE-OUT-TEST-MCP")
        return yesterday

    async def test_get_energy_metrics_exposes_structured_pnl_fields(self) -> None:
        self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_energy_metrics", {})

        self.assertTrue(out["ok"])
        d = out["pnl"]["daily"]
        # Structured cost components must be present
        for key in (
            "realised_cost_pounds",
            "realised_import_pounds",
            "standing_charge_pounds",
            "export_revenue_pounds",
            "export_kwh",
            "energy_used_kwh",
            "svt_shadow_pounds",
            "fixed_shadow_pounds",
            "delta_vs_svt_pounds",
            "delta_vs_fixed_pounds",
        ):
            self.assertIn(key, d, f"missing key {key!r} in get_energy_metrics output")
        # The BG comparison must surface when FIXED_TARIFF_* env vars are set
        self.assertEqual(d["fixed_tariff_label"], "British Gas Fixed v58")
        self.assertIn("fixed_tariff_shadow_pounds", d)
        self.assertIn("delta_vs_fixed_tariff_pounds", d)
        # Note must clarify standing-charge inclusion
        self.assertIn("standing", d["_note"].lower())

    async def test_get_daily_brief_returns_both_markdown_and_data(self) -> None:
        self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_daily_brief", {})

        self.assertTrue(out["ok"])
        self.assertIn("markdown", out)
        self.assertIn("data", out)
        d = out["data"]
        self.assertIn("mode_status", d)
        self.assertIn("Daikin=passive", d["mode_status"])
        self.assertIn("yesterday_pnl", d)
        # Standing charge present in structured pnl
        self.assertIn("standing_charge_gbp", d["yesterday_pnl"])
        # Note clarifies semantics
        self.assertIn("NET", d["_note"])

    async def test_get_night_brief_returns_both_markdown_and_data(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_night_brief", {})

        self.assertTrue(out["ok"])
        self.assertIn("markdown", out)
        self.assertIn("data", out)
        self.assertIn("today_pnl", out["data"])
        self.assertIn("Daikin=passive", out["data"]["mode_status"])

    async def test_get_tariff_comparison_includes_bg_when_configured(self) -> None:
        yesterday = self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_tariff_comparison", {"date": yesterday.isoformat()}
        )

        self.assertTrue(out["ok"])
        self.assertEqual(out["date"], yesterday.isoformat())
        labels = [c["label"] for c in out["comparisons"]]
        self.assertIn("Octopus Agile (realised)", labels)
        self.assertIn("Octopus SVT (would-have)", labels)
        self.assertIn("British Gas Fixed v58", labels)
        # BG entry has the configured rate
        bg = next(c for c in out["comparisons"] if c["label"] == "British Gas Fixed v58")
        self.assertEqual(bg["rate_pence_per_kwh"], 20.70)
        self.assertEqual(bg["standing_pence_per_day"], 41.14)

    async def test_get_tariff_comparison_omits_bg_when_not_configured(self) -> None:
        app_config.FIXED_TARIFF_LABEL = ""
        app_config.FIXED_TARIFF_RATE_PENCE = 0.0
        app_config.FIXED_TARIFF_STANDING_PENCE_PER_DAY = 0.0
        yesterday = self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_tariff_comparison", {"date": yesterday.isoformat()}
        )

        self.assertTrue(out["ok"])
        labels = [c["label"] for c in out["comparisons"]]
        # Only Agile + SVT, no third comparison
        self.assertEqual(len(labels), 2)
        self.assertNotIn("British Gas Fixed v58", labels)

    async def test_get_tariff_comparison_rejects_bad_date(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_tariff_comparison", {"date": "not-a-date"}
        )
        self.assertFalse(out["ok"])
        self.assertIn("invalid date", out["error"])

    async def test_get_tariff_comparison_period_week(self) -> None:
        """``period='week'`` resolves to a 7-day trailing range with n_days=7."""
        self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_tariff_comparison", {"period": "week"}
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["n_days"], 7)
        self.assertEqual(out["label"], "trailing-7d")

    async def test_get_tariff_comparison_period_mtd(self) -> None:
        from datetime import date as _date_t
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_tariff_comparison", {"period": "mtd"}
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["label"], "month-to-date")
        today = _dt.now(ZoneInfo("Europe/London")).date()
        self.assertEqual(out["period_start"], _date_t(today.year, today.month, 1).isoformat())
        self.assertEqual(out["period_end"], today.isoformat())

    async def test_get_tariff_comparison_period_ytd(self) -> None:
        from datetime import date as _date_t
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_tariff_comparison", {"period": "ytd"}
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["label"], "year-to-date")
        today = _dt.now(ZoneInfo("Europe/London")).date()
        self.assertEqual(out["period_start"], _date_t(today.year, 1, 1).isoformat())

    async def test_get_tariff_comparison_custom_range(self) -> None:
        self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_tariff_comparison",
            {"start_date": "2026-04-25", "end_date": "2026-05-01"},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["period_start"], "2026-04-25")
        self.assertEqual(out["period_end"], "2026-05-01")
        self.assertEqual(out["n_days"], 7)

    async def test_get_tariff_comparison_rejects_bad_period(self) -> None:
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_tariff_comparison", {"period": "decade"}
        )
        self.assertFalse(out["ok"])
        self.assertIn("unknown period", out["error"])

    async def test_get_energy_metrics_surfaces_clamped_metadata(self) -> None:
        """When AGILE_TARIFF_START_DATE clamps a period, the MCP response must
        carry ``clamped`` / ``clamp_reason`` / ``requested_start`` so OpenClaw
        can render an honest "since YYYY-MM-DD" qualifier."""
        from datetime import UTC, date as _date_t, datetime as _dt
        # Switch a clamp date that's after Jan 1 (forces YTD to clamp)
        app_config.AGILE_TARIFF_START_DATE = "2026-04-01"
        # Seed at least one Apr+ day so YTD has data
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/London")
        today = _dt.now(tz).date()
        slot = _dt.combine(_date_t(today.year, 4, 5), _dt.min.time()).replace(hour=12, tzinfo=UTC)
        db.log_execution({
            "timestamp": slot.isoformat().replace("+00:00", "Z"),
            "consumption_kwh": 1.0, "agile_price_pence": 20.0, "slot_kind": "standard",
        })
        try:
            mcp = build_mcp()
            _b, out = await mcp.call_tool("get_energy_metrics", {})
            ytd = out["pnl"]["year_to_date"]
            assert ytd.get("clamped") is True, f"YTD should be clamped: {ytd}"
            assert "AGILE_TARIFF_START_DATE" in ytd["clamp_reason"]
            assert ytd["requested_start"] == f"{today.year}-01-01"
            assert ytd["period_start"] == "2026-04-01"
            assert "(since 2026-04-01)" in ytd["label"]
        finally:
            app_config.AGILE_TARIFF_START_DATE = ""

    async def test_get_tariff_comparison_uses_clamped_dates_not_requested(self) -> None:
        """Pre-#215 bug: get_tariff_comparison echoed pre-clamp start in
        ``period_start`` while ``n_days`` reflected post-clamp count, producing
        internally inconsistent output (Jan 1 → today but n_days=32). Lock fix."""
        app_config.AGILE_TARIFF_START_DATE = "2026-04-01"
        try:
            mcp = build_mcp()
            _b, out = await mcp.call_tool(
                "get_tariff_comparison", {"period": "ytd"}
            )
            assert out["ok"]
            assert out["period_start"] == "2026-04-01", (
                f"period_start should be the clamped (post-Apr-1) value, got {out['period_start']!r}"
            )
            assert out.get("clamped") is True
            assert "(since 2026-04-01)" in out["label"]
            # n_days from same clamped window
            assert out["n_days"] >= 1
        finally:
            app_config.AGILE_TARIFF_START_DATE = ""

    async def test_get_energy_metrics_includes_mtd_and_ytd_blocks(self) -> None:
        self._seed_yesterday()
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_energy_metrics", {})

        self.assertTrue(out["ok"])
        # The pnl envelope must now expose all 5 period scopes
        for scope in ("daily", "weekly", "monthly", "month_to_date", "year_to_date"):
            self.assertIn(scope, out["pnl"], f"missing pnl.{scope}")
        # And the period blocks must carry the breakdown, not just delta
        for scope in ("weekly", "monthly", "month_to_date", "year_to_date"):
            blk = out["pnl"][scope]
            for k in (
                "energy_used_kwh", "export_kwh", "realised_cost_pounds",
                "standing_charge_pounds", "delta_vs_svt_pounds", "n_days",
            ):
                self.assertIn(k, blk, f"missing {k!r} in pnl.{scope}")
