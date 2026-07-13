"""Tests for the three audit MCP tools added in Story A2 of Epic 13a:

* ``get_audit_report``                — thin wrapper over build_audit_report
* ``get_forgone_peak_export`` — multi-day forgone-export aggregate
* ``get_brief_kpis``                  — structured KPI fields from the brief

Tests exercise each tool via ``mcp.call_tool(...)`` (the same path OpenClaw
takes over HTTP), monkeypatching ``config.DB_PATH`` so the queries hit a
tmp DB with controlled seed data. No live Daikin / Fox / Octopus calls.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import unittest
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="Install the `mcp` package to run MCP server tests.")


def _make_tmp_db(tmp_path: Path) -> Path:
    """Tmp DB with the same minimal schema slice used by audit_report tests."""
    db_path = tmp_path / "mcp.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE optimizer_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            strategy_summary TEXT
        );
        CREATE TABLE pv_realtime_history (
            captured_at TEXT PRIMARY KEY,
            solar_power_kw REAL, soc_pct REAL,
            load_power_kw REAL, grid_import_kw REAL, grid_export_kw REAL,
            battery_charge_kw REAL, battery_discharge_kw REAL,
            source TEXT NOT NULL DEFAULT 'test'
        );
        CREATE TABLE daikin_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at REAL NOT NULL, source TEXT NOT NULL, tank_temp_c REAL
        );
        CREATE TABLE lp_inputs_snapshot (
            run_id INTEGER PRIMARY KEY, run_at_utc TEXT NOT NULL
        );
        CREATE TABLE lp_solution_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL, slot_index INTEGER NOT NULL,
            slot_time_utc TEXT NOT NULL,
            import_kwh REAL, export_kwh REAL, charge_kwh REAL,
            discharge_kwh REAL, pv_use_kwh REAL, dhw_kwh REAL, space_kwh REAL
        );
        CREATE TABLE dispatch_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL, slot_time_utc TEXT NOT NULL,
            lp_kind TEXT NOT NULL, dispatched_kind TEXT NOT NULL,
            export_price_p_kwh REAL, reason TEXT,
            UNIQUE(run_id, slot_time_utc)
        );
        CREATE TABLE fox_schedule_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uploaded_at TEXT NOT NULL
        );
        CREATE TABLE agile_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            valid_from TEXT NOT NULL, value_inc_vat REAL NOT NULL
        );
        CREATE TABLE agile_export_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            valid_from TEXT NOT NULL, value_inc_vat REAL NOT NULL
        );
        """
    )
    db.commit()
    db.close()
    return db_path


class TestGetAuditReport(unittest.IsolatedAsyncioTestCase):
    """Exercise ``get_audit_report`` end-to-end via mcp.call_tool."""

    async def asyncSetUp(self) -> None:
        self._tmp_dir = Path(__import__("tempfile").mkdtemp())
        self._db = _make_tmp_db(self._tmp_dir)

    async def asyncTearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    async def test_returns_structured_report_on_empty_db(self) -> None:
        from unittest.mock import patch

        from src.mcp_server import build_mcp

        mcp = build_mcp()
        with patch("src.analytics.audit_report.config") as cfg:
            cfg.DB_PATH = str(self._db)
            cfg.BATTERY_CAPACITY_KWH = 10.0
            cfg.MIN_SOC_RESERVE_PERCENT = 15.0
            _blocks, out = await mcp.call_tool("get_audit_report", {})
        assert out["ok"] is True
        rep = out["report"]
        assert rep["window_hours"] == 24
        assert rep["held_schedule"]["total"] == 0
        assert rep["plan_vs_execution"]["lp_runs"] == 0
        assert rep["forgone_export"]["slot_count"] == 0

    async def test_rejects_out_of_range_window(self) -> None:
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        _blocks, out = await mcp.call_tool("get_audit_report", {"window_hours": 0})
        assert out["ok"] is False
        assert "window_hours" in out["error"]
        _blocks, out = await mcp.call_tool("get_audit_report", {"window_hours": 999})
        assert out["ok"] is False

    async def test_custom_window_passes_through(self) -> None:
        """Non-default window_hours is honoured by the underlying call."""
        from unittest.mock import patch

        from src.mcp_server import build_mcp

        mcp = build_mcp()
        with patch("src.analytics.audit_report.config") as cfg:
            cfg.DB_PATH = str(self._db)
            cfg.BATTERY_CAPACITY_KWH = 10.0
            cfg.MIN_SOC_RESERVE_PERCENT = 15.0
            _blocks, out = await mcp.call_tool("get_audit_report", {"window_hours": 48})
        assert out["ok"] is True
        assert out["report"]["window_hours"] == 48


class TestGetStrictSavingsForgoneExport(unittest.IsolatedAsyncioTestCase):
    """Exercise ``get_forgone_peak_export`` for a small range."""

    async def asyncSetUp(self) -> None:
        from src import db as _db
        _db.init_db()

    async def test_empty_range_returns_zero_totals(self) -> None:
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_forgone_peak_export",
            {"start_date": "2026-05-01", "end_date": "2026-05-03"},
        )
        assert out["ok"] is True
        assert out["n_days"] == 3
        assert out["totals"]["slot_count"] == 0
        assert out["totals"]["kwh"] == 0.0
        assert out["totals"]["pence"] == 0.0
        assert out["totals"]["pounds"] == 0.0
        assert len(out["daily"]) == 3
        # Each day enumerated with explicit ISO date string
        assert [d["date"] for d in out["daily"]] == [
            "2026-05-01", "2026-05-02", "2026-05-03",
        ]

    async def test_invalid_date_returns_error(self) -> None:
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_forgone_peak_export",
            {"start_date": "not-a-date", "end_date": "2026-05-01"},
        )
        assert out["ok"] is False
        assert "invalid date" in out["error"].lower()

    async def test_inverted_range_rejected(self) -> None:
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_forgone_peak_export",
            {"start_date": "2026-05-10", "end_date": "2026-05-01"},
        )
        assert out["ok"] is False
        assert ">=" in out["error"]

    async def test_range_cap_enforced(self) -> None:
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_forgone_peak_export",
            {"start_date": "2024-01-01", "end_date": "2026-01-02"},
        )
        assert out["ok"] is False
        assert "367" in out["error"]

    async def test_aggregates_real_seeded_data(self) -> None:
        """Seed two days with one forgone-export slot each, verify totals."""
        from src import db as _db

        # PR #350's helper reads dispatch_decisions + lp_solution_snapshot;
        # seed one downgrade slot per day at different export prices.
        conn = sqlite3.connect(_db.config.DB_PATH)
        try:
            for day, slot_iso, export_p in (
                ("2026-05-01", "2026-05-01T13:00:00+00:00", 25.0),
                ("2026-05-02", "2026-05-02T13:00:00+00:00", 30.0),
            ):
                conn.execute(
                    "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc) "
                    "VALUES (?, ?)",
                    (int(day.replace("-", "")), slot_iso),
                )
                conn.execute(
                    "INSERT INTO lp_solution_snapshot "
                    "(run_id, slot_index, slot_time_utc, export_kwh, import_kwh) "
                    "VALUES (?, 0, ?, 1.0, 0.0)",
                    (int(day.replace("-", "")), slot_iso),
                )
                conn.execute(
                    "INSERT INTO dispatch_decisions "
                    "(run_id, slot_time_utc, lp_kind, dispatched_kind, "
                    "committed, reason, export_price_p_kwh, created_at) "
                    "VALUES (?, ?, 'peak_export', 'standard', 1, "
                    "'pessimistic_disagrees', ?, ?)",
                    (int(day.replace("-", "")), slot_iso, export_p, slot_iso),
                )
            conn.commit()
        finally:
            conn.close()

        from src.mcp_server import build_mcp
        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_forgone_peak_export",
            {"start_date": "2026-05-01", "end_date": "2026-05-02"},
        )
        assert out["ok"] is True
        assert out["totals"]["slot_count"] == 2
        assert out["totals"]["kwh"] == pytest.approx(2.0, abs=0.05)
        # 1 kWh × 25p + 1 kWh × 30p = 55p
        assert out["totals"]["pence"] == pytest.approx(55.0, abs=1.0)
        assert out["totals"]["pounds"] == pytest.approx(0.55, abs=0.02)


class TestGetBriefKpis(unittest.IsolatedAsyncioTestCase):
    """Exercise ``get_brief_kpis`` for default + explicit date arguments."""

    async def asyncSetUp(self) -> None:
        from src import db as _db
        _db.init_db()

    async def test_handles_empty_db_gracefully(self) -> None:
        """Empty DB → all KPI numeric fields are None or zero; tool stays ``ok=True``."""
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_brief_kpis", {"date": "2026-05-15"}
        )
        assert out["ok"] is True
        assert out["date"] == "2026-05-15"
        # MTD requires day > 1 — 2026-05-15 qualifies but DB is empty
        # so the block may be present but with zeros / Nones, or None.
        assert "mtd" in out
        assert out["forgone_export"] == {
            "kwh": 0.0, "pence": 0.0, "slot_count": 0,
        }
        # Scorecard on empty DB renders N/A grade
        assert out["lp_scorecard"]["grade"] == "N/A"

    async def test_first_of_month_skips_mtd_block(self) -> None:
        """On the 1st of the month there's no previous-days MTD window —
        the helper returns None instead of fabricating empty stats."""
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_brief_kpis", {"date": "2026-05-01"}
        )
        assert out["ok"] is True
        assert out["mtd"] is None

    async def test_invalid_date_returns_error(self) -> None:
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        _blocks, out = await mcp.call_tool(
            "get_brief_kpis", {"date": "not-a-date"}
        )
        assert out["ok"] is False
        assert "invalid date" in out["error"].lower()


class TestNewToolsRegistered(unittest.IsolatedAsyncioTestCase):
    """Sanity check that all three new tools appear in tools/list."""

    async def test_all_three_audit_tools_registered(self) -> None:
        from src.mcp_server import build_mcp

        mcp = build_mcp()
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert "get_audit_report" in names
        assert "get_forgone_peak_export" in names
        assert "get_brief_kpis" in names
