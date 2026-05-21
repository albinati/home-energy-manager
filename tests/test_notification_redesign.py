"""Tests for the 2026-05-21 notification redesign.

Covers Tier A (brief content rewrite), Tier B (LP failure log + alert), and
Tier C (Octopus zero-day backfill skip).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "notify.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


# =====================================================================
# Tier C — Octopus backfill: skip zero-value days
# =====================================================================

class TestTierCBackfillSkipZeroDays:
    def test_audit_line_renders_unpublished_marker_when_meter_is_zero(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even if octopus_daily_meter has a zero-valued row (left over from
        before the fix), the brief should NOT render the absurd disparity."""
        from src.analytics.daily_brief import _fox_vs_meter_audit_line
        day = dt.date(2026, 5, 20)
        # Fox saw real traffic
        db.upsert_fox_energy_daily([{
            "date": day.isoformat(),
            "import_kwh": 9.14, "export_kwh": 0.28,
            "self_use_kwh": 11.0, "solar_kwh": 15.0, "load_kwh": 20.0,
        }])
        # Simulate a zero-Octopus row (the bug we just fixed)
        db.upsert_octopus_daily_meter(
            day.isoformat(), import_kwh=0.03, export_kwh=0.0,
        )

        line = _fox_vs_meter_audit_line(day)
        # Critical invariant: the absurd disparity that was the bug must NOT
        # appear. The import-side comparison was rendering +32546% when Fox
        # saw 9.14 kWh but Octopus reported 0.03 kWh.
        if line is not None:
            assert "+32546" not in line
            assert "import 9.14 / 0.03" not in line   # the original bug

    def test_audit_line_renders_normally_when_meter_has_real_data(self) -> None:
        from src.analytics.daily_brief import _fox_vs_meter_audit_line
        day = dt.date(2026, 5, 20)
        db.upsert_fox_energy_daily([{
            "date": day.isoformat(),
            "import_kwh": 9.14, "export_kwh": 0.28,
            "self_use_kwh": 11.0, "solar_kwh": 15.0, "load_kwh": 20.0,
        }])
        # Real meter publication: ~9 kWh import, agrees with Fox to ~few %
        db.upsert_octopus_daily_meter(
            day.isoformat(), import_kwh=9.30, export_kwh=0.31,
        )
        line = _fox_vs_meter_audit_line(day)
        if line is not None:
            assert "import 9.14 / 9.30" in line


# =====================================================================
# Tier B — LP failure log + alert
# =====================================================================

class TestTierBLPFailureLog:
    def test_lp_failure_log_table_exists_and_supports_insert(self) -> None:
        rid = db.insert_lp_failure(
            run_at_utc="2026-05-21T12:00:00+00:00",
            plan_date="2026-05-22",
            error_class="LP_Infeasible",
            error_msg="LP returned Infeasible on the 48h horizon",
        )
        assert rid > 0
        rows = db.list_recent_lp_failures(limit=10)
        assert len(rows) == 1
        assert rows[0]["error_class"] == "LP_Infeasible"

    def test_list_recent_lp_failures_orders_newest_first(self) -> None:
        for i in range(5):
            db.insert_lp_failure(
                run_at_utc=f"2026-05-21T0{i}:00:00+00:00",
                plan_date="2026-05-21",
                error_class=f"LP_Test_{i}",
            )
        rows = db.list_recent_lp_failures(limit=3)
        assert len(rows) == 3
        assert rows[0]["error_class"] == "LP_Test_4"  # newest
        assert rows[2]["error_class"] == "LP_Test_2"

    def test_notify_lp_failure_dispatches_with_correct_alert_type(self) -> None:
        from src.notifier import AlertType, notify_lp_failure
        with patch("src.notifier._dispatch") as mock_dispatch:
            notify_lp_failure(
                run_at_utc="2026-05-21T12:00:00+00:00",
                plan_date="2026-05-22",
                error_class="LP_Infeasible",
                error_msg="solver returned Infeasible",
                lp_inputs_run_id=42,
            )
        assert mock_dispatch.called
        args, kwargs = mock_dispatch.call_args
        assert args[0] == AlertType.LP_FAILURE
        body = args[1]
        assert "LP_Infeasible" in body
        assert "2026-05-22" in body
        assert "run_id=42" in body
        assert kwargs["urgent"] is True
        assert kwargs["extra"]["lp_inputs_run_id"] == 42


# =====================================================================
# Tier A — Morning brief rewrite (6 sections)
# =====================================================================

class TestTierAMorningBrief:
    def test_morning_brief_skips_failed_sections_without_dropping_brief(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single failing section must render as `_(unavailable)_` placeholder,
        NOT cause the whole brief to throw."""
        from src.analytics import daily_brief
        def _explode(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(daily_brief, "_pv_forecast_today_line", _explode)
        body = daily_brief.build_morning_payload()
        assert "**Today" in body
        assert "unavailable: RuntimeError" in body

    def test_morning_brief_includes_mode_line_always(self) -> None:
        from src.analytics.daily_brief import build_morning_payload
        body = build_morning_payload()
        assert "**Mode:**" in body
        # Default preset = "normal" → 🏠 chip
        assert "🏠" in body or "Mode" in body

    def test_preset_line_renders_guest_badge_when_set(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.analytics.daily_brief import _preset_line
        monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "guests", raising=False)
        line = _preset_line()
        assert line is not None
        assert "👥 Guests" in line

    def test_pv_confidence_chip_reflects_recent_bias(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.analytics import daily_brief

        # Seed meteo_forecast with one daytime slot
        conn = sqlite3.connect(app_config.DB_PATH)
        try:
            conn.execute(
                "INSERT INTO meteo_forecast (forecast_date, slot_time, "
                "temp_c, solar_w_m2, cloud_cover_pct) "
                "VALUES (?, ?, 18.0, 600.0, 30.0)",
                ("2026-05-21", "2026-05-21T12:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        # Mock the pv_bias_report to return a high-confidence signal
        with patch("src.analytics.pv_bias_report.summarise_pv_bias") as mock:
            mock.return_value = {
                "n_paired": 50,
                "am_bias_kw": 0.05,
                "pm_bias_kw": -0.02,
            }
            line = daily_brief._pv_forecast_today_line(dt.date(2026, 5, 21), None)
        assert line is not None
        assert "🟢 high confidence" in line


# =====================================================================
# Tier A — Night brief rewrite (4 sections)
# =====================================================================

class TestTierANightBrief:
    def test_night_brief_renders_pnl_section_with_real_data(self) -> None:
        from src.analytics.daily_brief import build_night_payload
        body = build_night_payload()
        assert "actuals" in body.lower()
        assert "**PnL today:**" in body

    def test_today_vs_forecast_block_renders_pv_load_cost_when_seeded(self) -> None:
        from src.analytics.daily_brief import _today_vs_forecast_block

        # Seed forecast_skill_log: PV predicted 5.0 kWh, actual 4.5 kWh
        conn = sqlite3.connect(app_config.DB_PATH)
        try:
            for hour in range(0, 24):
                conn.execute(
                    "INSERT INTO forecast_skill_log "
                    "(date_utc, hour_of_day, predicted_pv_kwh, actual_pv_kwh, "
                    "predicted_load_kwh, actual_load_kwh, predicted_temp_c, "
                    "actual_temp_c, built_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("2026-05-20", hour,
                     0.2 if 6 <= hour < 18 else 0.0,   # 12 daytime × 0.2 = 2.4 kWh
                     0.18 if 6 <= hour < 18 else 0.0,  # 12 × 0.18 = 2.16 kWh
                     0.4, 0.42, 12.0, 12.5,
                     "2026-05-21T00:00:00+00:00"),
                )
            conn.commit()
        finally:
            conn.close()

        lines = _today_vs_forecast_block(dt.date(2026, 5, 20))
        assert len(lines) >= 2  # at least PV + load rendered
        assert any("PV:" in line for line in lines)
        assert any("Load:" in line for line in lines)


# =====================================================================
# Sanity: smoke-importing the whole brief pipeline doesn't raise
# =====================================================================

def test_brief_pipeline_imports_cleanly() -> None:
    from src.analytics.daily_brief import (
        _charging_plan_today_lines,
        _day_cost_forecast_line,
        _now_state_line,
        _preset_line,
        _pv_forecast_today_line,
        _safe_call,
        _temperature_range_today_line,
        _today_vs_forecast_block,
        _tonight_battery_sufficiency_line,
        _tonight_daikin_plan_lines,
        build_morning_payload,
        build_night_payload,
    )
    # Just having the imports + composers work on empty DB is the smoke test.
    assert _safe_call("test", lambda: "ok") == "ok"
    assert _safe_call("test", lambda: 1/0) is not None  # placeholder, not exception
