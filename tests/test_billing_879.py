"""#879 — plan_fronts `billing` section: bill-cycle arithmetic + guarded section."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src import db
from src.analytics import plan_fronts as pf
from src.config import config

NOW = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    pf.clear_cache()
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    from src.analytics import fair_compare

    monkeypatch.setattr(fair_compare, "compute_fair_comparison",
                        lambda s, e, max_tariffs=14: {"tariffs": []})


@pytest.mark.parametrize("today,cd,start,end", [
    (date(2026, 10, 28), 21, date(2026, 10, 21), date(2026, 11, 20)),   # mid cycle
    (date(2026, 10, 21), 21, date(2026, 10, 21), date(2026, 11, 20)),   # today = cycle day
    (date(2026, 10, 20), 21, date(2026, 9, 21), date(2026, 10, 20)),    # day before the start
    (date(2026, 1, 5), 21, date(2025, 12, 21), date(2026, 1, 20)),      # year wrap back
    (date(2026, 12, 25), 21, date(2026, 12, 21), date(2027, 1, 20)),    # year wrap forward
    (date(2026, 3, 1), 28, date(2026, 2, 28), date(2026, 3, 27)),       # day 28, short Feb
    (date(2026, 2, 27), 28, date(2026, 1, 28), date(2026, 2, 27)),
    (date(2026, 7, 31), 1, date(2026, 7, 1), date(2026, 7, 31)),        # day 1, 31-day month
    (date(2026, 9, 30), 1, date(2026, 9, 1), date(2026, 9, 30)),
])
def test_billing_cycle_bounds(today, cd, start, end):
    assert pf.billing_cycle_bounds(today, cd) == (start, end)


def test_billing_cycle_day_clamped():
    assert pf.billing_cycle_bounds(date(2026, 10, 30), 31)[0] == date(2026, 10, 28)
    assert pf.billing_cycle_bounds(date(2026, 10, 30), 0)[0] == date(2026, 10, 1)


def test_billing_forecast_formula():
    assert pf.billing_forecast_gbp(12.40, 8, 23) == round(12.40 + 12.40 / 8 * 23, 2)
    assert pf.billing_forecast_gbp(0.0, 0, 30) == 0.0
    assert pf.billing_forecast_gbp(5.0, 3, 0) == 5.0


def test_billing_section_shape(monkeypatch):
    from src.analytics import fair_compare

    monkeypatch.setattr(config, "OCTOPUS_BILLING_CYCLE_DAY", 21)
    monkeypatch.setattr(pf, "_period", lambda s, e: {"n_days": 8, "net_cost_gbp": 12.4})
    monkeypatch.setattr(fair_compare, "cached_fair_comparison", lambda s, e, n=14: {
        "tariffs": [
            {"product_code": "COSY", "display_name": "Cosy", "net_pence": 1240.0, "is_current": True},
            {"product_code": "FIXED", "display_name": "Fixo", "net_pence": 1870.0, "is_current": False},
            {"product_code": "AGILE", "display_name": "Agile", "net_pence": 1350.0, "is_current": False},
        ]})
    b = pf.billing_section(date(2026, 10, 28))
    assert (b["cycle_start"], b["cycle_end"]) == ("2026-10-21", "2026-11-20")
    assert (b["days_elapsed"], b["days_total"]) == (8, 31)
    assert b["realised_gbp"] == 12.4
    assert b["forecast_cycle_gbp"] == round(12.4 + 12.4 / 8 * 23, 2)
    assert {c["tariff"]: c["delta_gbp"] for c in b["compare"]} == {"FIXED": 6.3, "AGILE": 1.1}
    assert b["winner"]["tariff"] == "COSY" and b["winner"]["is_current"] is True


def test_billing_section_guarded(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("pnl down")

    monkeypatch.setattr(pf, "billing_section", boom)
    r = pf.plan_fronts(date(2026, 10, 28), now_utc=NOW, use_cache=False)
    assert "RuntimeError" in r["billing"]["error"]


def test_billing_setting_registered():
    from src import runtime_settings

    spec = runtime_settings.SCHEMA["OCTOPUS_BILLING_CYCLE_DAY"]
    assert (spec.min_value, spec.max_value) == (1, 28)
    assert config.OCTOPUS_BILLING_CYCLE_DAY == 21
