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

def test_mtd_context_returns_none_on_first_of_month() -> None:
    """No MTD comparison available on the 1st."""
    line = daily_brief._mtd_context_line(date(2026, 5, 1), {"realised_net_cost_gbp": 1.0})
    assert line is None


def test_mtd_context_returns_none_when_no_history() -> None:
    """No execution_log rows → compute_period_pnl returns n_days=0 → None."""
    line = daily_brief._mtd_context_line(date(2026, 5, 15), {"realised_net_cost_gbp": 1.0})
    assert line is None


# ---------------------------------------------------------------------------
# _mean_agile_rate_line
# ---------------------------------------------------------------------------

def test_mean_rate_line_returns_none_with_no_imports() -> None:
    line = daily_brief._mean_agile_rate_line(date(2026, 5, 15), {"import_kwh": 0, "import_cost_gbp": 0})
    assert line is None


def test_mean_rate_line_today_only_when_no_mtd_history() -> None:
    """On day-of-month > 1 but no MTD data, render today-only flavour."""
    line = daily_brief._mean_agile_rate_line(
        date(2026, 5, 15),
        {"import_kwh": 5.0, "import_cost_gbp": 1.25},  # 25 p/kWh
    )
    assert line is not None
    assert "25.0 p/kWh" in line
    assert "5.0 kWh imported" in line


def test_mean_rate_line_on_first_of_month_today_only() -> None:
    """Day 1 has no MTD context — still emits the today-only line."""
    line = daily_brief._mean_agile_rate_line(
        date(2026, 5, 1),
        {"import_kwh": 3.0, "import_cost_gbp": 0.60},  # 20 p/kWh
    )
    assert line is not None
    assert "20.0 p/kWh" in line


# ---------------------------------------------------------------------------
# _strict_savings_forgone_line
# ---------------------------------------------------------------------------

def test_forgone_line_returns_none_when_not_strict_savings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forgone-export counterfactual only makes sense under strict_savings."""
    monkeypatch.setattr(app_config, "ENERGY_STRATEGY_MODE", "savings_first")
    line = daily_brief._strict_savings_forgone_line(date(2026, 5, 15), ZoneInfo("Europe/London"))
    assert line is None


def test_forgone_line_returns_none_when_no_downgraded_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even under strict_savings, returns None when no slot had a downgrade."""
    monkeypatch.setattr(app_config, "ENERGY_STRATEGY_MODE", "strict_savings")
    line = daily_brief._strict_savings_forgone_line(date(2026, 5, 15), ZoneInfo("Europe/London"))
    assert line is None


def test_forgone_line_summarises_downgrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When dispatch_decisions has slots with lp.export_kwh > 0 but
    dispatched_kind != peak_export, sum the would-have-earned revenue."""
    monkeypatch.setattr(app_config, "ENERGY_STRATEGY_MODE", "strict_savings")
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")

    # Seed an optimizer_log row + lp_inputs_snapshot + lp_solution_snapshot
    # + dispatch_decisions for a peak slot the LP wanted to export but
    # strict_savings downgraded.
    day = date(2026, 5, 14)
    slot_t = "2026-05-14T16:30:00+00:00"  # 17:30 BST (in peak)
    run_id = db.log_optimizer_run({
        "run_at": "2026-05-14T15:55:00+00:00",
        "rates_count": 48,
        "cheap_slots": 0,
        "peak_slots": 1,
        "standard_slots": 0,
        "negative_slots": 0,
        "target_vwap": 20.0,
        "actual_agile_mean": 22.0,
        "battery_warning": False,
        "strategy_summary": "test",
        "fox_schedule_uploaded": True,
        "daikin_actions_count": 0,
    })

    # Minimal lp_inputs_snapshot row for the FK join.
    db.save_lp_snapshots(
        run_id=run_id,
        inputs_row={
            "run_at_utc": "2026-05-14T15:55:00+00:00",
            "plan_date": "2026-05-14",
            "horizon_hours": 48,
            "soc_initial_kwh": 5.0, "tank_initial_c": 45.0, "indoor_initial_c": None,
            "soc_source": "test", "tank_source": "test", "indoor_source": "removed_phase_b",
            "base_load_json": "[]", "micro_climate_offset_c": 0.0,
            "forecast_fetch_at_utc": None, "exogenous_snapshot_json": None,
            "config_snapshot_json": "{}", "price_quantize_p": 0.0,
            "peak_threshold_p": 30.0, "cheap_threshold_p": 10.0,
            "daikin_control_mode": "active", "optimization_preset": "normal",
            "energy_strategy_mode": "strict_savings",
        },
        solution_rows=[{
            "slot_index": 0,
            "slot_time_utc": slot_t,
            "price_p": 35.0,
            "import_kwh": 0.0,
            "export_kwh": 1.84,     # LP wanted to export
            "charge_kwh": 0.0,
            "discharge_kwh": 2.0,
            "pv_use_kwh": 0.0,
            "pv_curtail_kwh": 0.0,
            "dhw_kwh": 0.0,
            "space_kwh": 0.0,
            "soc_kwh": 5.0,
            "tank_temp_c": 45.0,
            "indoor_temp_c": None,
            "outdoor_temp_c": 15.0,
            "lwt_offset_c": 0.0,
        }],
    )
    db.upsert_dispatch_decision(
        run_id=run_id,
        slot_time_utc=slot_t,
        lp_kind="standard",          # strict_savings classifier never marked it peak_export
        dispatched_kind="standard",
        committed=True,
        reason="not_peak_export",
        scen_optimistic_exp_kwh=None,
        scen_nominal_exp_kwh=None,
        scen_pessimistic_exp_kwh=None,
        export_price_p_kwh=25.0,     # would-have-earned price
        refill_price_p_kwh=None,
        economic_margin_p_kwh=None,
        outgoing_rate_percentile=None,
    )

    line = daily_brief._strict_savings_forgone_line(day, ZoneInfo("Europe/London"))
    assert line is not None, "expected a forgone-export line"
    # 1.84 kWh * 25p = 46p = £0.46
    assert "£0.46" in line, line
    assert "1.8 kWh" in line
    assert "1 slot" in line
    assert "savings_first" in line
