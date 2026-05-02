"""Period PnL: weekly / monthly / MTD / YTD aggregate every breakdown field.

Pre-#213 the period helpers only summed the deltas — losing kWh totals,
import/export breakdown, BG comparison. These tests lock the new aggregated
shape so the brief and MCP can render honest weekly/monthly reports.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src import db
from src.analytics import pnl
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 62.22)
    monkeypatch.setattr(app_config, "SVT_RATE_PENCE", 25.0)
    monkeypatch.setattr(app_config, "FIXED_TARIFF_LABEL", "British Gas Fixed v58")
    monkeypatch.setattr(app_config, "FIXED_TARIFF_RATE_PENCE", 20.70)
    monkeypatch.setattr(app_config, "FIXED_TARIFF_STANDING_PENCE_PER_DAY", 41.14)


def _seed_day(d: date, kwh: float, p_per_kwh: float) -> None:
    """One execution_log row at noon for the given day."""
    slot = datetime.combine(d, datetime.min.time()).replace(hour=12, tzinfo=UTC)
    db.log_execution({
        "timestamp": slot.isoformat().replace("+00:00", "Z"),
        "consumption_kwh": kwh,
        "agile_price_pence": p_per_kwh,
        "slot_kind": "standard",
    })


def test_compute_period_pnl_aggregates_kwh_and_costs() -> None:
    """3 days × 5 kWh @ 20p each → 15 kWh, £3.00 import, 3 × £0.6222 standing."""
    for offset in range(3):
        _seed_day(date(2026, 5, 1) + timedelta(days=offset), kwh=5.0, p_per_kwh=20.0)

    p = pnl.compute_period_pnl(date(2026, 5, 1), date(2026, 5, 3))

    assert p["n_days"] == 3
    assert p["kwh"] == pytest.approx(15.0, abs=1e-3)
    assert p["realised_import_gbp"] == pytest.approx(3.0, abs=1e-3)
    # Standing 62.22p × 3 days = 186.66p = £1.8666
    assert p["standing_charge_gbp"] == pytest.approx(1.8666, abs=1e-3)
    # No export, no BG override
    assert p["export_kwh"] == 0.0
    # Realised = import + standing − export = 3.00 + 1.8666 - 0 = £4.8666
    assert p["realised_cost_gbp"] == pytest.approx(4.8666, abs=1e-3)


def test_compute_period_pnl_aggregates_bg_shadow() -> None:
    """BG shadow rolls up too — 3 days × 5 kWh × 20.70p + 3 × 41.14p standing."""
    for offset in range(3):
        _seed_day(date(2026, 5, 1) + timedelta(days=offset), kwh=5.0, p_per_kwh=20.0)

    p = pnl.compute_period_pnl(date(2026, 5, 1), date(2026, 5, 3))

    assert "fixed_tariff_label" in p
    assert p["fixed_tariff_label"] == "British Gas Fixed v58"
    # 15 kWh × 20.70p + 3 × 41.14p = 310.50 + 123.42 = 433.92p = £4.3392
    assert p["fixed_tariff_shadow_gbp"] == pytest.approx(4.3392, abs=1e-3)
    # Realised £4.8666 vs BG £4.3392 → delta = -£0.5274 (BG cheaper this week)
    assert p["delta_vs_fixed_tariff_gbp"] == pytest.approx(-0.5274, abs=1e-3)


def test_compute_weekly_pnl_is_trailing_7d() -> None:
    """compute_weekly_pnl(end_day) covers end_day-6 .. end_day inclusive."""
    end = date(2026, 5, 7)
    for offset in range(7):
        _seed_day(end - timedelta(days=offset), kwh=2.0, p_per_kwh=20.0)

    p = pnl.compute_weekly_pnl(end)

    assert p["n_days"] == 7
    assert p["period_start"] == "2026-05-01"
    assert p["period_end"] == "2026-05-07"
    assert p["kwh"] == pytest.approx(14.0, abs=1e-3)
    assert p["week_end"] == "2026-05-07"  # back-compat alias preserved


def test_compute_monthly_pnl_full_calendar_month() -> None:
    """compute_monthly_pnl iterates the FULL calendar month, not up to end_day."""
    # Seed only May 1 + May 30 — n_days must still be 31 (May has 31 days)
    _seed_day(date(2026, 5, 1), kwh=2.0, p_per_kwh=20.0)
    _seed_day(date(2026, 5, 30), kwh=2.0, p_per_kwh=20.0)

    p = pnl.compute_monthly_pnl(date(2026, 5, 15))

    assert p["n_days"] == 31
    assert p["period_start"] == "2026-05-01"
    assert p["period_end"] == "2026-05-31"
    # Standing × 31 = 1929.82p = £19.2982 (plus the 4 kWh of import)
    assert p["standing_charge_gbp"] == pytest.approx(19.2882, abs=1e-3)


def test_compute_mtd_pnl_partial_month() -> None:
    """MTD covers 1st → end_day inclusive — partial month, NOT full calendar."""
    for offset in range(5):
        _seed_day(date(2026, 5, 1) + timedelta(days=offset), kwh=1.0, p_per_kwh=20.0)

    p = pnl.compute_mtd_pnl(date(2026, 5, 5))

    assert p["n_days"] == 5
    assert p["period_start"] == "2026-05-01"
    assert p["period_end"] == "2026-05-05"
    assert p["month"] == "2026-05"
    # Standing × 5 = 311.10p = £3.1110
    assert p["standing_charge_gbp"] == pytest.approx(3.111, abs=1e-3)


def test_compute_ytd_pnl_jan_first_to_today() -> None:
    """YTD covers Jan 1 → end_day inclusive."""
    _seed_day(date(2026, 1, 1), kwh=3.0, p_per_kwh=20.0)
    _seed_day(date(2026, 5, 2), kwh=4.0, p_per_kwh=20.0)

    p = pnl.compute_ytd_pnl(date(2026, 5, 2))

    assert p["period_start"] == "2026-01-01"
    assert p["period_end"] == "2026-05-02"
    assert p["year"] == "2026"
    assert p["n_days"] == 122  # Jan 1 to May 2 inclusive
    assert p["kwh"] == pytest.approx(7.0, abs=1e-3)


def test_compute_period_pnl_swaps_reversed_dates() -> None:
    """If end < start, the helper swaps them rather than producing nonsense."""
    _seed_day(date(2026, 5, 1), kwh=1.0, p_per_kwh=20.0)
    p1 = pnl.compute_period_pnl(date(2026, 5, 1), date(2026, 5, 3))
    p2 = pnl.compute_period_pnl(date(2026, 5, 3), date(2026, 5, 1))
    assert p1["n_days"] == p2["n_days"] == 3
    assert p1["realised_cost_gbp"] == p2["realised_cost_gbp"]


def test_compute_monthly_pnl_keeps_old_keys_for_backcompat() -> None:
    """Code that grabbed compute_monthly_pnl(today)['delta_vs_svt_gbp'] must
    still work after the breakdown expansion."""
    _seed_day(date(2026, 5, 15), kwh=1.0, p_per_kwh=20.0)
    p = pnl.compute_monthly_pnl(date(2026, 5, 15))
    assert "month" in p
    assert "delta_vs_svt_gbp" in p
    assert "delta_vs_fixed_gbp" in p
