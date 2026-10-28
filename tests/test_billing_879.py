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
    # 7 completed days at 10.0, today partial 1.0 (< avg -> counts as an avg day), 23 days after today
    avg = 10.0 / 7
    assert pf.billing_forecast_gbp(10.0, 7, 1.0, 23) == round(10.0 + avg + avg * 23, 2)
    # today already above the average -> its own value
    assert pf.billing_forecast_gbp(10.0, 7, 3.0, 23) == round(10.0 + 3.0 + avg * 23, 2)
    # day 1 of the cycle: no completed day -> no forecast
    assert pf.billing_forecast_gbp(0.0, 0, 0.5, 30) is None
    assert pf.billing_forecast_gbp(5.0, 3, 1.0, 0) == round(5.0 + 5.0 / 3, 2)


def test_short_tariff_label():
    f = pf.short_tariff_label
    assert f("Cosy (your tariff)", "E-1R-COSY-22-12-08-H") == "Cosy"
    assert f("Agile Octopus", "AGILE-24-10-01") == "Agile"
    assert f("Octopus Flux", "FLUX-IMP-23-02-14") == "Flux"
    assert f("Octopus Go", "GO-VAR-22-10-14") == "Go"
    assert f("British Gas Fixed v58", "BG-FIXED") == "fixo"
    assert f("Standard Variable (SVT)", "SVT") == "variável"
    assert f("Intelligent Tracker", "TRACKER-1") == "Tracker"
    assert f("Cosy Octopus 12M Fixed", "COSY-FIX-12M-26-10-14") == "Cosy fixo"


def _seed_compare(monkeypatch, rows, done, today_net=None):
    from src.analytics import fair_compare

    monkeypatch.setattr(config, "OCTOPUS_BILLING_CYCLE_DAY", 21)
    monkeypatch.setattr(pf, "_period", lambda s, e: done if s != e else {"n_days": 1, "net_cost_gbp": today_net or 0.0})
    monkeypatch.setattr(pf, "_past_compare", lambda s, e: {"tariffs": rows})
    monkeypatch.setattr(fair_compare, "cached_fair_comparison", lambda s, e, n=14: {"tariffs": []})


def _rows(**net):
    out = []
    for code, (name, pence, cur, approx) in net.items():
        out.append({"product_code": code, "display_name": name, "net_pence": pence,
                    "is_current": cur, "approximate": approx})
    return out


def test_billing_section_shape(monkeypatch):
    rows = _rows(COSY=("Cosy (your tariff)", 1240.0, True, False),
                 FIXED=("British Gas Fixed", 1870.0, False, False),
                 AGILE=("Agile Octopus", 1350.0, False, False))
    _seed_compare(monkeypatch, rows, {"n_days": 7, "net_cost_gbp": 10.4}, today_net=2.0)
    b = pf.billing_section(date(2026, 10, 28))
    assert (b["cycle_start"], b["cycle_end"]) == ("2026-10-21", "2026-11-20")
    assert (b["days_elapsed"], b["days_total"]) == (8, 31)
    assert b["realised_gbp"] == 12.4
    avg = 10.4 / 7
    assert b["forecast_cycle_gbp"] == round(10.4 + max(2.0, avg) + avg * 23, 2)
    by = {c["tariff"]: c for c in b["compare"]}
    assert by["FIXED"]["delta_gbp"] == 6.3 and by["FIXED"]["short_label"] == "fixo"
    assert by["AGILE"]["delta_gbp"] == 1.1 and by["AGILE"]["tie"] is False
    assert b["current_label"] == "Cosy"
    assert b["winner"]["is_current"] is True and b["since"] is None


def test_billing_winner_needs_a_real_saving(monkeypatch):
    # Agile is cheaper by only 0.5 -> tie, current stays the winner.
    rows = _rows(COSY=("Cosy (your tariff)", 1240.0, True, False),
                 AGILE=("Agile Octopus", 1190.0, False, False))
    _seed_compare(monkeypatch, rows, {"n_days": 7, "net_cost_gbp": 10.4})
    b = pf.billing_section(date(2026, 10, 28))
    assert b["compare"][0]["tie"] is True and b["winner"]["is_current"] is True
    # Cheaper by 3 pounds but only a proxy estimate -> still a tie.
    rows = _rows(COSY=("Cosy (your tariff)", 1240.0, True, False),
                 TRACKER=("Intelligent Tracker", 940.0, False, True))
    _seed_compare(monkeypatch, rows, {"n_days": 7, "net_cost_gbp": 10.4})
    assert pf.billing_section(date(2026, 10, 28))["winner"]["is_current"] is True
    # Genuinely cheaper by >= 1 pound, exact -> that tariff wins.
    rows = _rows(COSY=("Cosy (your tariff)", 1240.0, True, False),
                 AGILE=("Agile Octopus", 1000.0, False, False))
    _seed_compare(monkeypatch, rows, {"n_days": 7, "net_cost_gbp": 10.4})
    w = pf.billing_section(date(2026, 10, 28))["winner"]
    assert w["is_current"] is False and w["label"] == "Agile"


def test_billing_clamp_reports_since_and_uses_covered_days(monkeypatch):
    from src.analytics import pnl

    rows = _rows(COSY=("Cosy (your tariff)", 600.0, True, False))
    # PnL only covers 3 of the 7 completed days (tariff start clamp).
    _seed_compare(monkeypatch, rows, {"n_days": 3, "net_cost_gbp": 6.0})
    monkeypatch.setattr(pnl, "_agile_start_date", lambda: date(2026, 10, 25))
    b = pf.billing_section(date(2026, 10, 28))
    assert b["since"] == "2026-10-25"
    assert b["forecast_cycle_gbp"] == round(6.0 + 2.0 + 2.0 * 23, 2)  # avg over the 3 covered days


def test_billing_day_one_has_no_forecast(monkeypatch):
    rows = _rows(COSY=("Cosy (your tariff)", 100.0, True, False))
    _seed_compare(monkeypatch, rows, {"n_days": 0, "net_cost_gbp": 0.0}, today_net=0.4)
    b = pf.billing_section(date(2026, 10, 21))
    assert b["days_elapsed"] == 1 and b["forecast_cycle_gbp"] is None and b["realised_gbp"] == 0.4


def test_plan_fronts_single_flight(monkeypatch):
    import threading
    import time

    calls = []

    def slow(*a, **k):
        calls.append(1)
        time.sleep(0.3)
        return {"windows": []}

    monkeypatch.setattr(pf, "tariff_section", slow)
    out = []
    ts = [threading.Thread(target=lambda: out.append(pf.plan_fronts(date(2026, 10, 28)))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and len(out) == 4


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
