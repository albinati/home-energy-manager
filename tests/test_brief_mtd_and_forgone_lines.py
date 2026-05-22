"""Daily brief best-practice KPI lines:
  - ``_mtd_context_line`` — today's cost as % of MTD daily average.
  - ``_mean_agile_rate_line`` — import-weighted mean rate vs MTD.
  - ``_strict_savings_forgone_line`` — counterfactual export revenue.

Each helper returns None when the data isn't there (1st of month, no
imports, non-strict_savings mode) so legacy briefs don't gain noise.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.analytics import daily_brief
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# _mtd_context_line
# ---------------------------------------------------------------------------

def test_mtd_summary_returns_none_on_first_of_month() -> None:
    """No MTD aggregate available on the 1st — caller passes None to consumers."""
    assert daily_brief._mtd_summary(date(2026, 5, 1)) is None


def test_mtd_context_returns_none_when_mtd_missing() -> None:
    """When the shared MTD summary is None (1st of month / aggregator failed),
    the context line bails cleanly."""
    line = daily_brief._mtd_context_line(date(2026, 5, 15), {"realised_net_cost_gbp": 1.0}, mtd=None)
    assert line is None


def test_mtd_context_returns_none_when_n_days_zero() -> None:
    """Shared MTD summary with n_days=0 → bail."""
    line = daily_brief._mtd_context_line(
        date(2026, 5, 15),
        {"realised_net_cost_gbp": 1.0},
        mtd={"n_days": 0, "realised_net_cost_gbp": 0.0},
    )
    assert line is None


def test_mtd_context_formats_pct_when_data_present() -> None:
    """Today £0.85 vs MTD avg £1.30 → 65% of avg, ↓ direction."""
    line = daily_brief._mtd_context_line(
        date(2026, 5, 15),
        {"realised_net_cost_gbp": 0.85},
        mtd={"n_days": 14, "realised_net_cost_gbp": 18.20},  # avg 1.30/d
    )
    assert line is not None
    assert "£+0.85" in line
    assert "65%" in line
    assert "↓" in line


# ---------------------------------------------------------------------------
# _mean_agile_rate_line
# ---------------------------------------------------------------------------

def test_mean_rate_line_returns_none_with_no_imports() -> None:
    line = daily_brief._mean_agile_rate_line(
        date(2026, 5, 15), {"import_kwh": 0, "import_cost_gbp": 0}, mtd=None,
    )
    assert line is None


def test_mean_rate_line_today_only_when_mtd_missing() -> None:
    """No MTD data → render today-only flavour."""
    line = daily_brief._mean_agile_rate_line(
        date(2026, 5, 15),
        {"import_kwh": 5.0, "import_cost_gbp": 1.25},  # 25 p/kWh
        mtd=None,
    )
    assert line is not None
    assert "25.0 p/kWh" in line
    assert "5.0 kWh imported" in line


def test_mean_rate_line_with_mtd_compares() -> None:
    """Today 20 p/kWh, MTD 25 p/kWh → -20%, ↓ direction."""
    line = daily_brief._mean_agile_rate_line(
        date(2026, 5, 15),
        {"import_kwh": 5.0, "import_cost_gbp": 1.0},  # 20 p/kWh
        mtd={"import_kwh": 100.0, "import_cost_gbp": 25.0},  # 25 p/kWh
    )
    assert line is not None
    assert "20.0 p/kWh" in line
    assert "25.0 p/kWh" in line
    assert "-20%" in line
    assert "↓" in line


# ---------------------------------------------------------------------------
# _strict_savings_forgone_line — PR C removed the underlying mode.
# The helper now always returns None so the brief composer keeps its shape.
# ---------------------------------------------------------------------------

def test_forgone_line_is_permanently_inactive() -> None:
    """PR C — `ENERGY_STRATEGY_MODE=strict_savings` is gone. The forgone-export
    line is a no-op; the historical MCP `get_strict_savings_forgone_export`
    tool still serves DB queries for audit, but the brief line is gone."""
    line = daily_brief._strict_savings_forgone_line(date(2026, 5, 15), ZoneInfo("Europe/London"))
    assert line is None


# PR C — `test_forgone_line_summarises_downgrades` removed.
# ENERGY_STRATEGY_MODE is gone; the line always returns None now.
# The MCP `get_strict_savings_forgone_export` tool still serves historical
# queries against existing dispatch_decisions rows with `reason='strict_savings'`,
# but the brief no longer surfaces them.
