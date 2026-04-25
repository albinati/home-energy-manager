"""LP per-slot export pricing — Octopus Outgoing Agile.

Covers:
- ``save_agile_export_rates`` + ``get_agile_export_rates_in_range`` round-trip.
- ``_build_export_price_line`` returns None when tariff code is empty.
- ``_build_export_price_line`` returns None when the table is empty.
- ``_build_export_price_line`` matches per-slot rows + falls back to flat for gaps.
- ``solve_lp`` uses per-slot export when supplied; falls back to flat constant otherwise.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config


@pytest.fixture(autouse=True)
def _clean_export_table(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setattr(db, "_DB_PATH", str(db_path), raising=False)
    db.init_db()
    yield


def test_save_get_export_rates_round_trip():
    rows = [
        {"valid_from": "2026-05-01T12:00:00Z", "valid_to": "2026-05-01T12:30:00Z", "value_inc_vat": 18.5},
        {"valid_from": "2026-05-01T12:30:00Z", "valid_to": "2026-05-01T13:00:00Z", "value_inc_vat": 22.0},
    ]
    n = db.save_agile_export_rates(rows, "E-1R-AGILE-OUTGOING-19-05-13-H")
    assert n == 2
    got = db.get_agile_export_rates_in_range("2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z")
    assert len(got) == 2
    assert got[0]["value_inc_vat"] == 18.5
    assert got[1]["value_inc_vat"] == 22.0


def test_save_export_rates_idempotent_upsert():
    rows = [{"valid_from": "2026-05-01T12:00:00Z", "valid_to": "2026-05-01T12:30:00Z", "value_inc_vat": 18.5}]
    db.save_agile_export_rates(rows, "X")
    # Re-save with new value — UPSERT should overwrite.
    rows[0]["value_inc_vat"] = 25.0
    db.save_agile_export_rates(rows, "X")
    got = db.get_agile_export_rates_in_range("2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z")
    assert len(got) == 1
    assert got[0]["value_inc_vat"] == 25.0


def test_build_export_price_line_returns_none_when_tariff_empty(monkeypatch):
    from src.scheduler.optimizer import _build_export_price_line
    monkeypatch.setattr(config, "OCTOPUS_EXPORT_TARIFF_CODE", "")
    slots = [datetime(2026, 5, 1, 12, 0, tzinfo=UTC)]
    assert _build_export_price_line(slots) is None


def test_build_export_price_line_returns_none_when_table_empty(monkeypatch):
    from src.scheduler.optimizer import _build_export_price_line
    monkeypatch.setattr(config, "OCTOPUS_EXPORT_TARIFF_CODE", "E-1R-AGILE-OUTGOING-19-05-13-H")
    slots = [datetime(2026, 5, 1, 12, 0, tzinfo=UTC)]
    assert _build_export_price_line(slots) is None


def test_build_export_price_line_matches_per_slot_rows(monkeypatch):
    from src.scheduler.optimizer import _build_export_price_line
    monkeypatch.setattr(config, "OCTOPUS_EXPORT_TARIFF_CODE", "X")
    monkeypatch.setattr(config, "EXPORT_RATE_PENCE", 15.0)
    db.save_agile_export_rates(
        [
            {"valid_from": "2026-05-01T12:00:00Z", "valid_to": "2026-05-01T12:30:00Z", "value_inc_vat": 8.0},
            {"valid_from": "2026-05-01T13:00:00Z", "valid_to": "2026-05-01T13:30:00Z", "value_inc_vat": 25.0},
        ],
        "X",
    )
    slots = [
        datetime(2026, 5, 1, 12, 0, tzinfo=UTC),  # match → 8.0
        datetime(2026, 5, 1, 12, 30, tzinfo=UTC),  # gap → flat 15.0
        datetime(2026, 5, 1, 13, 0, tzinfo=UTC),  # match → 25.0
    ]
    out = _build_export_price_line(slots)
    assert out == [8.0, 15.0, 25.0]


def test_solve_lp_uses_per_slot_export_when_provided():
    """High export price in slot 1 should make the LP prefer exporting then vs storing for later."""
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    # PV abundant in slot 1, none elsewhere; cheap import always so storing has no advantage.
    pv = [0.0, 5.0, 0.0, 0.0]
    prices = [5.0, 5.0, 5.0, 5.0]
    base_load = [0.3] * n
    # High-export tariff at slot 1 (50p), zero elsewhere → solver should prefer exporting in slot 1.
    export_prices = [0.0, 50.0, 0.0, 0.0]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[50.0] * n,
        pv_kwh_per_slot=pv,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    init = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=21.0)
    pv = pv  # silence linter
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=init,
        tz=ZoneInfo("Europe/London"),
        export_price_pence=export_prices,
    )
    assert plan.ok, plan.status
    # The high-tariff slot 1 should have non-trivial export.
    assert plan.export_kwh[1] > 0.5, f"expected export in slot 1; got {plan.export_kwh}"


def test_solve_lp_export_length_mismatch_raises():
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[50.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    init = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0, indoor_temp_c=21.0)
    with pytest.raises(ValueError, match="export_price_pence length"):
        solve_lp(
            slot_starts_utc=slots,
            price_pence=[5.0] * n,
            base_load_kwh=[0.3] * n,
            weather=weather,
            initial=init,
            tz=ZoneInfo("Europe/London"),
            export_price_pence=[10.0, 20.0],  # wrong length
        )
