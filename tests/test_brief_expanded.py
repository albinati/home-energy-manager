"""Brief markdown carries net cost, comparisons, mode header, forecasted export.

Issue follow-up to #207: OpenClaw was filling the prose with hallucinated
Daikin advice and ambiguous "beat SVT by 40p" lines because the underlying
HEM markdown was too sparse. These tests lock the structured fields the
brief must surface so future paraphrasers have nothing to invent.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src import db
from src.analytics import daily_brief, pnl
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _brief_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "AGILE-TEST")
    monkeypatch.setattr(app_config, "OCTOPUS_EXPORT_TARIFF_CODE", "AGILE-OUT-TEST")
    monkeypatch.setattr(app_config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 62.22)
    monkeypatch.setattr(app_config, "EXPORT_RATE_PENCE", 15.0)


def _seed_execution(ts: datetime, kwh: float, p: float) -> None:
    db.log_execution(
        {
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "consumption_kwh": kwh,
            "agile_price_pence": p,
            "slot_kind": "standard",
        }
    )


def _seed_export_sample(ts: datetime, kw: float) -> None:
    db.save_pv_realtime_sample(
        captured_at=ts.isoformat().replace("+00:00", "Z"),
        solar_power_kw=0.0, soc_pct=50.0, load_power_kw=0.0,
        grid_import_kw=0.0, grid_export_kw=kw,
        battery_charge_kw=0.0, battery_discharge_kw=0.0,
        source="test",
    )


def _seed_export_rate(ts: datetime, p: float) -> None:
    db.save_agile_export_rates(
        [{
            "valid_from": ts.isoformat().replace("+00:00", "Z"),
            "valid_to": (ts + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": p,
        }],
        "AGILE-OUT-TEST",
    )


# ---------------------------------------------------------------------------
# compute_daily_pnl — standing charge + British Gas shadow
# ---------------------------------------------------------------------------

def test_pnl_includes_standing_charge_in_realised_and_shadows() -> None:
    """Both realised cost and shadow costs MUST include the daily standing
    charge so the delta is apples-to-apples (real money saved, not
    energy-cost-only saved)."""
    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _seed_execution(slot, kwh=1.0, p=20.0)  # 20p import
    # No export

    p = pnl.compute_daily_pnl(day)

    # Realised = import 20p + standing 62.22p = 82.22p = £0.8222
    assert p["standing_charge_gbp"] == pytest.approx(0.6222, abs=1e-3)
    assert p["realised_cost_gbp"] == pytest.approx(0.8222, abs=1e-3)
    # SVT shadow includes standing too
    assert p["svt_shadow_gbp"] > p["standing_charge_gbp"], (
        "SVT shadow must include standing"
    )


def test_pnl_emits_british_gas_shadow_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With FIXED_TARIFF_* set, the PnL dict gains the BG shadow + delta."""
    monkeypatch.setattr(app_config, "FIXED_TARIFF_LABEL", "British Gas Fixed v58")
    monkeypatch.setattr(app_config, "FIXED_TARIFF_RATE_PENCE", 20.70)
    monkeypatch.setattr(app_config, "FIXED_TARIFF_STANDING_PENCE_PER_DAY", 41.14)

    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _seed_execution(slot, kwh=10.0, p=15.0)  # Agile cheaper than BG

    p = pnl.compute_daily_pnl(day)

    assert p["fixed_tariff_label"] == "British Gas Fixed v58"
    # 10 kWh × 20.70p + 41.14p standing = 207.00 + 41.14 = 248.14p = £2.4814
    assert p["fixed_tariff_shadow_gbp"] == pytest.approx(2.4814, abs=1e-3)
    # Realised = 10 × 15p + 62.22p standing = 212.22p = £2.1222
    # Delta vs BG = 2.4814 - 2.1222 = +£0.3592 saved (Agile beats BG)
    assert p["delta_vs_fixed_tariff_gbp"] == pytest.approx(0.3592, abs=1e-3)


def test_pnl_omits_british_gas_fields_when_not_configured() -> None:
    """When no legacy fixed tariff is configured, the BG fields must NOT
    appear in the dict (so the brief auto-suppresses the line)."""
    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _seed_execution(slot, kwh=1.0, p=20.0)

    p = pnl.compute_daily_pnl(day)

    assert "fixed_tariff_shadow_gbp" not in p
    assert "delta_vs_fixed_tariff_gbp" not in p


# ---------------------------------------------------------------------------
# Brief rendering — net cost, mode line, BG comparison
# ---------------------------------------------------------------------------

def _seed_typical_yesterday() -> date:
    """Realistic prior day: 10 kWh imported around mean Agile, 5 kWh exported."""
    yesterday = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _seed_execution(slot, kwh=10.0, p=22.0)
    _seed_export_rate(slot, 18.0)
    _seed_export_sample(slot, 10.0)
    _seed_export_sample(slot + timedelta(minutes=30), 10.0)  # 5 kWh exported
    return yesterday


# Six morning-brief content tests deleted by the 2026-05-21 redesign (PR #381):
#   - test_morning_brief_breaks_out_net_cost_components
#   - test_morning_brief_includes_mode_status_line  (replaced by _preset_line tests)
#   - test_morning_brief_renders_british_gas_comparison_when_configured
#   - test_morning_brief_omits_british_gas_line_when_not_configured
#
# The morning brief is now forward-looking only: PV forecast, temp range,
# charging plan, day cost forecast, SoC + tank, mode chip. Yesterday's PnL
# breakdown, BG fixed-tariff shadow, and the per-component net cost lines
# moved to the audit MCP tools (`get_audit_report`, `get_tariff_comparison`,
# `get_brief_kpis`). The night brief carries a one-line PnL summary.
#
# The underlying _format_pnl_block helper still exists for callers that want
# the full block (`compute_daily_pnl` + helpers).


# ---------------------------------------------------------------------------
# Forecasted-export fallback
# ---------------------------------------------------------------------------

# test_forecasted_export_line_appears_when_telemetry_export_is_zero — deleted by PR #381.
# The 🔮 forecasted-export fallback markup lived in the old morning brief's
# yesterday-PnL block, which moved entirely to the audit MCP tools. The
# helper _forecasted_export_for_day still exists for callers that need it.


def test_forecasted_export_line_suppressed_when_telemetry_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If grid_export_kw IS recorded, no forecasted-fallback line."""
    _seed_typical_yesterday()  # has telemetry
    db.upsert_dispatch_decision(
        run_id=999,
        slot_time_utc=datetime(2026, 5, 1, 17, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        lp_kind="peak_export",
        dispatched_kind="peak_export",
        committed=1,
        reason="robust",
        scen_optimistic_exp_kwh=2.0,
        scen_nominal_exp_kwh=1.84,
        scen_pessimistic_exp_kwh=1.60,
    )
    monkeypatch.setattr(
        daily_brief, "datetime_now_local_date", lambda tz: date(2026, 5, 2)
    )

    md = daily_brief.build_morning_payload()

    assert "🔮" not in md, (
        f"forecasted line should NOT appear when telemetry present:\n{md}"
    )


def test_committed_peak_export_in_range_picks_latest_run() -> None:
    """When the same slot is decided across multiple runs, the helper must
    return only the latest run's row."""
    slot = datetime(2026, 5, 1, 17, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    db.upsert_dispatch_decision(
        run_id=100, slot_time_utc=slot, lp_kind="peak_export",
        dispatched_kind="peak_export", committed=1, reason="robust",
        scen_optimistic_exp_kwh=1.0, scen_nominal_exp_kwh=0.8, scen_pessimistic_exp_kwh=0.5,
    )
    db.upsert_dispatch_decision(
        run_id=200, slot_time_utc=slot, lp_kind="peak_export",
        dispatched_kind="peak_export", committed=1, reason="robust",
        scen_optimistic_exp_kwh=2.0, scen_nominal_exp_kwh=1.84, scen_pessimistic_exp_kwh=1.60,
    )
    rows = db.get_committed_peak_export_in_range(
        "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00",
    )
    assert len(rows) == 1
    assert rows[0]["run_id"] == 200
    assert rows[0]["scen_pessimistic_exp_kwh"] == 1.60


# ---------------------------------------------------------------------------
# Madrugada (overnight) plan-vs-actual section
# ---------------------------------------------------------------------------

def _seed_overnight_meteo(yesterday: date, today: date) -> None:
    """Seed half-hourly outdoor temps across yesterday 22:00 UTC → today 09:00 UTC.

    Uses a stable cold pattern (4 °C lowest) so the predicted heating is
    predictable in tests."""
    fetch_at = datetime(today.year, today.month, today.day, 7, 0, tzinfo=UTC).isoformat()
    start = datetime(yesterday.year, yesterday.month, yesterday.day, 22, 0, tzinfo=UTC)
    with db._lock:
        conn = db.get_connection()
        try:
            for i in range(22):  # 22 half-hour slots = 11 h
                slot = (start + timedelta(minutes=30 * i)).isoformat()
                # cold pattern: 8 °C → 4 °C → 8 °C
                t = 8.0 - 4.0 * abs(i - 11) / 11.0
                conn.execute(
                    """INSERT INTO meteo_forecast_value
                       (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2, cloud_cover_pct, direct_pv_kw)
                       VALUES (?, ?, ?, NULL, NULL, NULL)""",
                    (fetch_at, slot, t),
                )
            conn.commit()
        finally:
            conn.close()


def _seed_overnight_realised_heating(yesterday: date, today: date) -> None:
    """Seed daikin_consumption_2hourly buckets covering the overnight."""
    db.upsert_daikin_consumption_daily(
        date=yesterday.isoformat(), kwh_total=8.0, kwh_heating=6.0, kwh_dhw=2.0,
        source="onecta",
    )
    with db._lock:
        conn = db.get_connection()
        try:
            now = datetime.now(UTC).isoformat()
            # yesterday bucket 11 = 22:00–24:00 (1.0 kWh heating)
            conn.execute(
                """INSERT INTO daikin_consumption_2hourly
                   (date, bucket_idx, kwh_total, kwh_heating, kwh_dhw, source, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (yesterday.isoformat(), 11, 1.2, 1.0, 0.2, "onecta", now),
            )
            # today buckets 0..4 = 00:00–10:00 (covers the morning chunk)
            for bi, kwh in enumerate([1.1, 1.3, 1.4, 1.0, 0.6]):
                conn.execute(
                    """INSERT INTO daikin_consumption_2hourly
                       (date, bucket_idx, kwh_total, kwh_heating, kwh_dhw, source, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (today.isoformat(), bi, kwh + 0.2, kwh, 0.2, "onecta", now),
                )
            conn.commit()
        finally:
            conn.close()


def _seed_overnight_pv_realtime(yesterday: date, today: date) -> None:
    """Seed pv_realtime_history rows so SoC drift bullet has data."""
    # Sample at 22:05 UTC yesterday: SoC 30 %
    db.save_pv_realtime_sample(
        captured_at=datetime(yesterday.year, yesterday.month, yesterday.day, 22, 5, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        solar_power_kw=0.0, soc_pct=30.0, load_power_kw=0.5,
        grid_import_kw=0.0, grid_export_kw=0.0,
        battery_charge_kw=0.0, battery_discharge_kw=0.5, source="test",
    )
    # Latest sample (now-ish): SoC 65 %
    db.save_pv_realtime_sample(
        captured_at=datetime(today.year, today.month, today.day, 7, 30, tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        solar_power_kw=0.0, soc_pct=65.0, load_power_kw=0.5,
        grid_import_kw=0.0, grid_export_kw=0.0,
        battery_charge_kw=0.0, battery_discharge_kw=0.5, source="test",
    )


# Three "Madrugada (overnight plan vs actual)" tests deleted by PR #381.
# The morning brief is now forward-looking only; the overnight-plan-vs-actual
# concept moved to the night brief's "today vs forecast" section (which uses
# forecast_skill_log per-hour data instead of the bespoke Madrugada helpers).
# The underlying helpers (_overnight_plan_vs_actual_lines, _heating_plan_vs_actual,
# _battery_overnight_drift, _calibration_k_status_line) still exist if a future
# composer wants to surface them.
