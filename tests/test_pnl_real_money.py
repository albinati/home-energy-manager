"""Real-money PnL fields use measured grid import — issue #306.

The legacy ``realised_cost_gbp`` field bills HOUSEHOLD LOAD at Agile rates.
On a solar-rich day where load is 22 kWh but only 6 kWh comes from the grid,
that inflates the absolute £ figure ~3-4× because PV + battery self-supply
gets billed as if grid-bought.

The new ``realised_net_cost_gbp`` (and matching ``*_real_gbp`` shadows) bill
the *measured* grid import (from ``pv_realtime_history.grid_import_kw``) at
per-slot Agile rates. This is what actually moved through the meter.
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


def _save_pv(ts: datetime, *, import_kw: float = 0.0, export_kw: float = 0.0,
             load_kw: float = 0.0) -> None:
    db.save_pv_realtime_sample(
        captured_at=ts.isoformat().replace("+00:00", "Z"),
        solar_power_kw=0.0,
        soc_pct=50.0,
        load_power_kw=load_kw,
        grid_import_kw=import_kw,
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


def test_half_hourly_grid_import_helper_returns_kwh_per_slot() -> None:
    """Two samples 30 min apart with constant 2 kW import → 1 kWh in
    the bucket containing the earlier sample."""
    day = date(2026, 5, 1)
    t0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    _save_pv(t0, import_kw=2.0)
    _save_pv(t0 + timedelta(minutes=30), import_kw=2.0)

    out = db.half_hourly_grid_import_kwh_for_day(day)
    key = "2026-05-01T12:00:00Z"
    assert key in out
    assert out[key] == pytest.approx(1.0, abs=1e-3)


def test_realised_net_cost_uses_grid_import_not_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline #306 fix: load and grid_import diverge on a solar day,
    and ``realised_net_cost_gbp`` must bill grid_import (not load) at Agile.

    Setup: heartbeat row says load=4 kWh consumed at 20p (legacy field
    sees this as £0.80 of import). PV telemetry says only 1 kWh was
    grid-imported in the same slot. New field should bill 1 kWh × 20p =
    £0.20, not £0.80.
    """
    monkeypatch.setattr(app_config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 0.0)
    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    # Heartbeat says household consumed 4 kWh at 20p (load × Agile = £0.80)
    _save_execution(slot, kwh=4.0, agile_p=20.0)

    # Real PV telemetry: only 1 kWh of that came from the grid (rest from PV/battery)
    _save_pv(slot, import_kw=2.0, load_kw=8.0)
    _save_pv(slot + timedelta(minutes=30), import_kw=2.0, load_kw=8.0)
    # 0.5h × 2 kW = 1 kWh imported

    p = pnl.compute_daily_pnl(day)

    # Real-money: 1 kWh × 20p = £0.20
    assert p["import_kwh"] == pytest.approx(1.0, abs=1e-2)
    assert p["import_cost_gbp"] == pytest.approx(0.20, abs=1e-3)
    assert p["realised_net_cost_gbp"] == pytest.approx(0.20, abs=1e-3)

    # Legacy load-billed (what the bug used to surface): 4 kWh × 20p = £0.80
    assert p["realised_import_gbp"] == pytest.approx(0.80, abs=1e-3)
    assert p["realised_cost_gbp"] == pytest.approx(0.80, abs=1e-3)


def test_real_shadow_deltas_use_real_import_axis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``svt_shadow_real_gbp`` and ``fixed_shadow_real_gbp`` bill the same
    *real* grid_import × shadow rate, so the delta-vs-Agile reflects what
    the household would actually have paid on each tariff."""
    monkeypatch.setattr(app_config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 0.0)
    monkeypatch.setattr(app_config, "SVT_RATE_PENCE", 30.0)
    monkeypatch.setattr(app_config, "MANUAL_TARIFF_IMPORT_PENCE", 30.0)
    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)

    _save_execution(slot, kwh=4.0, agile_p=20.0)
    _save_pv(slot, import_kw=2.0)
    _save_pv(slot + timedelta(minutes=30), import_kw=2.0)
    # → 1 kWh imported × 20p = £0.20 on Agile
    # → 1 kWh × 30p = £0.30 on SVT/Fixed
    # → real delta = +£0.10 saved

    p = pnl.compute_daily_pnl(day)

    assert p["realised_net_cost_gbp"] == pytest.approx(0.20, abs=1e-3)
    assert p["svt_shadow_real_gbp"] == pytest.approx(0.30, abs=1e-3)
    assert p["delta_vs_svt_real_gbp"] == pytest.approx(0.10, abs=1e-3)
    # Legacy delta is inflated (4× too high)
    assert p["delta_vs_svt_gbp"] == pytest.approx(0.40, abs=1e-3)


def test_fixed_tariff_real_shadow_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured BG-style fixed tariff produces both real and load deltas."""
    monkeypatch.setattr(app_config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 0.0)
    monkeypatch.setattr(app_config, "FIXED_TARIFF_LABEL", "British Gas Fixed v58")
    monkeypatch.setattr(app_config, "FIXED_TARIFF_RATE_PENCE", 25.0)
    monkeypatch.setattr(app_config, "FIXED_TARIFF_STANDING_PENCE_PER_DAY", 40.0)
    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)

    _save_execution(slot, kwh=4.0, agile_p=20.0)
    _save_pv(slot, import_kw=2.0)
    _save_pv(slot + timedelta(minutes=30), import_kw=2.0)

    p = pnl.compute_daily_pnl(day)

    # Real: 1 kWh × 25p + 40p standing = 65p = £0.65
    assert p["fixed_tariff_shadow_real_gbp"] == pytest.approx(0.65, abs=1e-3)
    # Real Agile: 1 × 20p = 20p
    # Delta: BG would cost £0.65, Agile cost £0.20 → +£0.45 saved
    assert p["delta_vs_fixed_tariff_real_gbp"] == pytest.approx(0.45, abs=1e-3)

    # Legacy: 4 kWh × 25p + 40p = 140p = £1.40
    assert p["fixed_tariff_shadow_gbp"] == pytest.approx(1.40, abs=1e-3)


def test_backfill_dedupes_stragglers_in_slot_window() -> None:
    """update_execution_log_metered must leave exactly one canonical row per
    slot bucket. Pre-fix LIMIT 1 left straggler heartbeat rows intact, which
    double-counted in the legacy realised_cost_gbp SUM path."""
    slot_start = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)
    # Three estimated heartbeat rows landed within the same 30-min slot
    # at different microsecond offsets — typical when the heartbeat fires
    # multiple times before the half-hour deduper kicks in.
    _save_execution(slot_start + timedelta(seconds=12), kwh=0.4, agile_p=20.0)
    _save_execution(slot_start + timedelta(minutes=5, seconds=33), kwh=0.5, agile_p=20.0)
    _save_execution(slot_start + timedelta(minutes=18, seconds=47), kwh=0.3, agile_p=20.0)

    # Pre-backfill: 3 rows in the slot
    pre = db.get_execution_logs(
        from_ts=slot_start.isoformat(),
        to_ts=(slot_start + timedelta(minutes=30)).isoformat(),
        limit=10,
    )
    assert len(pre) == 3

    ok = db.update_execution_log_metered(
        slot_start.isoformat().replace("+00:00", "Z"),
        consumption_kwh=1.234,
    )
    assert ok is True

    # Post-backfill: exactly one canonical row remains, with the metered kWh
    post = db.get_execution_logs(
        from_ts=slot_start.isoformat(),
        to_ts=(slot_start + timedelta(minutes=30)).isoformat(),
        limit=10,
    )
    assert len(post) == 1
    assert post[0]["consumption_kwh"] == pytest.approx(1.234, abs=1e-6)
    assert post[0]["source"] == "metered"


def test_no_pv_telemetry_returns_zero_real_cost() -> None:
    """When ``pv_realtime_history`` has nothing for the day, real-money
    fields safely report zero (no inflation from missing data)."""
    day = date(2026, 5, 1)
    slot = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)
    _save_execution(slot, kwh=4.0, agile_p=20.0)
    # No PV samples

    p = pnl.compute_daily_pnl(day)

    assert p["import_kwh"] == 0.0
    assert p["import_cost_gbp"] == 0.0
    # Net = 0 import + standing - 0 export
    standing = p["standing_charge_gbp"]
    assert p["realised_net_cost_gbp"] == pytest.approx(standing, abs=1e-3)
