"""Daily PnL must subtract export earnings (issue #207).

Before this fix, ``compute_daily_pnl`` summed ``import_kwh × import_tariff``
only and silently dropped export earnings. On the user's tariff (Outgoing
Agile, peak export 30-60p/kWh), missing the export term flipped the
delta-vs-SVT sign on solar-heavy days — e.g. the night brief on 2026-05-01
reported -£0.30 deficit when the energy_metrics endpoint (which uses
``_compute_cost_octopus`` with export accounting) reported +£0.40 surplus.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from src import db
from src.analytics import pnl
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _save_export_rate(slot: datetime, p: float, tariff: str) -> None:
    db.save_agile_export_rates(
        [
            {
                "valid_from": slot.isoformat().replace("+00:00", "Z"),
                "valid_to": (slot + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                "value_inc_vat": p,
            }
        ],
        tariff,
    )


def _save_pv_sample(ts: datetime, export_kw: float) -> None:
    db.save_pv_realtime_sample(
        captured_at=ts.isoformat().replace("+00:00", "Z"),
        solar_power_kw=0.0,
        soc_pct=50.0,
        load_power_kw=0.0,
        grid_import_kw=0.0,
        grid_export_kw=export_kw,
        battery_charge_kw=0.0,
        battery_discharge_kw=0.0,
        source="test",
    )


def _save_execution(ts: datetime, kwh: float, agile_p: float) -> None:
    db.log_execution(
        {
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "consumption_kwh": kwh,
            "agile_price_pence": agile_p,
            "slot_kind": "standard",
        }
    )


def test_half_hourly_export_helper_sums_into_correct_slots() -> None:
    """Two samples 30 min apart with constant 2 kW export → 1 kWh in the
    bucket containing the earlier sample."""
    day = date(2026, 5, 1)
    t0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _save_pv_sample(t0, 2.0)
    _save_pv_sample(t0 + timedelta(minutes=30), 2.0)

    out = db.half_hourly_grid_export_kwh_for_day(day)
    key = "2026-05-01T12:00:00Z"
    assert key in out, f"expected slot {key} in {out}"
    assert out[key] == pytest.approx(1.0, abs=1e-3)


def test_half_hourly_export_helper_returns_empty_for_no_telemetry() -> None:
    assert db.half_hourly_grid_export_kwh_for_day(date(2026, 5, 1)) == {}


def test_half_hourly_export_helper_caps_long_gaps() -> None:
    """A 4-hour gap between samples must NOT extrapolate constant power; it's
    capped at ``max_gap_seconds`` (default 30 min)."""
    day = date(2026, 5, 1)
    t0 = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    _save_pv_sample(t0, 4.0)
    _save_pv_sample(t0 + timedelta(hours=4), 4.0)  # 4 h gap

    out = db.half_hourly_grid_export_kwh_for_day(day)
    # With cap at 30 min: 0.5 h × 4 kW = 2 kWh in the 10:00 bucket.
    # Without cap: 4 h × 4 kW = 16 kWh — would be wildly wrong.
    assert out["2026-05-01T10:00:00Z"] == pytest.approx(2.0, abs=1e-3)


def test_compute_daily_pnl_subtracts_export_revenue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #207: with 1 kWh consumed at 20p AND 1 kWh exported at 30p,
    realised cost is 20 - 30 = -10p (i.e. net earnings)."""
    tariff = "E-1R-AGILE-OUTGOING-TEST-207"
    monkeypatch.setattr(app_config, "OCTOPUS_EXPORT_TARIFF_CODE", tariff)
    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _save_execution(slot, kwh=1.0, agile_p=20.0)
    _save_export_rate(slot, 30.0, tariff)
    _save_pv_sample(slot, 2.0)
    _save_pv_sample(slot + timedelta(minutes=30), 2.0)  # 1 kWh exported

    p = pnl.compute_daily_pnl(day)

    assert p["realised_import_gbp"] == pytest.approx(0.20, abs=1e-3)
    assert p["export_kwh"] == pytest.approx(1.0, abs=1e-3)
    assert p["export_revenue_gbp"] == pytest.approx(0.30, abs=1e-3)
    # Net: 20p import - 30p export = -10p net cost
    assert p["realised_cost_gbp"] == pytest.approx(-0.10, abs=1e-3)


def test_compute_daily_pnl_falls_back_to_flat_export_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Outgoing tariff configured → flat ``EXPORT_RATE_PENCE`` is used."""
    monkeypatch.setattr(app_config, "OCTOPUS_EXPORT_TARIFF_CODE", "")
    monkeypatch.setattr(app_config, "EXPORT_RATE_PENCE", 15.0)
    day = date(2026, 5, 1)
    t0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _save_execution(t0, kwh=0.5, agile_p=10.0)
    _save_pv_sample(t0, 4.0)
    _save_pv_sample(t0 + timedelta(minutes=30), 4.0)  # 2 kWh exported

    p = pnl.compute_daily_pnl(day)

    assert p["export_kwh"] == pytest.approx(2.0, abs=1e-3)
    # 2 kWh × 15p = 30p revenue
    assert p["export_revenue_gbp"] == pytest.approx(0.30, abs=1e-3)
    # Net: 5p import - 30p export = -25p net cost
    assert p["realised_cost_gbp"] == pytest.approx(-0.25, abs=1e-3)


def test_compute_daily_pnl_with_no_export_keeps_old_behaviour() -> None:
    """A day with zero export telemetry must report the same realised cost
    as the import sum (back-compat — no surprise behaviour change)."""
    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
    _save_execution(slot, kwh=2.0, agile_p=25.0)
    # No PV samples → no export

    p = pnl.compute_daily_pnl(day)

    assert p["export_kwh"] == 0.0
    assert p["export_revenue_gbp"] == 0.0
    assert p["realised_import_gbp"] == pytest.approx(0.50, abs=1e-3)
    assert p["realised_cost_gbp"] == pytest.approx(0.50, abs=1e-3)


def test_delta_vs_svt_flips_sign_when_export_is_significant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline incident from #207: a heavy-export day where the daily
    brief reports a deficit but the system actually saved money vs SVT."""
    tariff = "E-1R-AGILE-OUTGOING-TEST-207"
    monkeypatch.setattr(app_config, "OCTOPUS_EXPORT_TARIFF_CODE", tariff)
    monkeypatch.setattr(app_config, "SVT_RATE_PENCE", 25.0)

    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # 10 kWh imported at 28p (above SVT) — looks like a deficit on import alone
    _save_execution(slot, kwh=10.0, agile_p=28.0)
    # 7.5 kWh exported at 30p — turns the day profitable
    _save_export_rate(slot, 30.0, tariff)
    _save_pv_sample(slot, 15.0)
    _save_pv_sample(slot + timedelta(minutes=30), 15.0)  # 7.5 kWh exported

    p = pnl.compute_daily_pnl(day)
    # SVT cost: 10 kWh × 25p = 250p
    # Realised: (10 × 28) - (7.5 × 30) = 280 - 225 = 55p
    # Delta vs SVT: (250 - 55) / 100 = +£1.95 surplus
    assert p["delta_vs_svt_gbp"] > 0, (
        f"Expected positive delta vs SVT (export wins), got {p['delta_vs_svt_gbp']}"
    )
    assert p["delta_vs_svt_gbp"] == pytest.approx(1.95, abs=1e-3)
