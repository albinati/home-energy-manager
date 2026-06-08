"""Phase 2 backend: today-cumulative savings fields + /appliances/suggestions.

The hero needs today's real-money savings; the appliance widget needs the
cheapest upcoming run window per idle appliance. Both reuse existing computations
(compute_daily_pnl, compute_appliance_window_suggestions).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from src import db
from src.config import config


def _client(monkeypatch):
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    from src.api.main import app
    return TestClient(app)


def test_today_cumulative_exposes_savings_fields(monkeypatch):
    db.init_db()
    client = _client(monkeypatch)
    r = client.get("/api/v1/energy/today-cumulative")
    assert r.status_code == 200
    body = r.json()
    for k in (
        "import_kwh", "export_kwh", "import_cost_gbp", "export_revenue_gbp",
        "realised_net_cost_gbp", "earnings_today_gbp", "negative_import_credit_gbp",
        # The standing charge is surfaced so the credit math is explicit
        # (earned − standing = net) and doesn't "look too small".
        "standing_charge_gbp",
        # Total consumption — the headline kWh the hero leads with.
        "consumption_kwh",
        # The CONFIGURED fixed tariff (British Gas) — correct shadow, not the generic.
        "fixed_tariff_label", "delta_vs_fixed_tariff_real_gbp", "fixed_tariff_shadow_real_gbp",
    ):
        assert k in body, f"hero needs {k} in today-cumulative"


def test_monthly_exposes_bg_real_delta(monkeypatch):
    """/energy/monthly must surface the authoritative slot-level British-Gas
    delta (compute_monthly_pnl) as `delta_vs_fixed_real_pounds` — the hero's
    lifetime "saved vs fixed" sums THIS, not the coarse Fox-energy delta which
    flips sign on Agile months."""
    db.init_db()
    client = _client(monkeypatch)

    from src.energy.monthly import (
        MonthlyCostSummary,
        MonthlyEnergySummary,
        MonthlyInsights,
    )

    fake = MonthlyInsights(
        energy=MonthlyEnergySummary(year=2026, month=5, month_str="2026-05"),
        cost=MonthlyCostSummary(
            net_cost_pence=8696.0,
            # The coarse delta disagrees with the real one (the bug under test).
            delta_vs_fixed_pence=-1263.0,
        ),
    )
    monkeypatch.setattr("src.api.main._foxess_configured", lambda: True)
    monkeypatch.setattr("src.api.main.get_monthly_insights", lambda y, m: fake)
    monkeypatch.setattr(
        "src.analytics.pnl.compute_monthly_pnl",
        lambda anchor: {"delta_vs_fixed_tariff_real_gbp": 6.19},
    )

    r = client.get("/api/v1/energy/monthly?month=2026-05")
    assert r.status_code == 200, r.text
    cost = r.json()["cost"]
    # The new authoritative field carries the real (positive = Agile won) delta…
    assert cost["delta_vs_fixed_real_pounds"] == 6.19
    # …while the legacy coarse field is left untouched (kept for other consumers).
    assert cost["delta_vs_fixed_pounds"] == -12.63


def _seed_rates(start_utc: datetime, prices: list[float]) -> None:
    rates, t = [], start_utc
    for p in prices:
        rates.append({
            "valid_from": t.isoformat().replace("+00:00", "Z"),
            "valid_to": (t + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": p,
        })
        t += timedelta(minutes=30)
    db.save_agile_rates(rates, "TEST-TARIFF")


def test_appliance_suggestions_endpoint(monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-TARIFF", raising=False)
    monkeypatch.setattr(config, "APPLIANCE_WINDOW_NUDGE_BRIEF_THRESHOLD_P", 8.0, raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    # idle, enabled washer with a far deadline + an upcoming cheap window
    db.add_appliance(
        vendor="smartthings", vendor_device_id="dev-x", name="Washer",
        device_type="washer", default_duration_minutes=120,
        deadline_local_time=(datetime.now(ZoneInfo("Europe/London")) + timedelta(hours=12)).strftime("%H:%M"),
        typical_kw=0.5,
    )
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    _seed_rates(now + timedelta(hours=1), [-4.0, -4.5, -3.0, -2.0, 8.0, 8.0])

    client = _client(monkeypatch)
    r = client.get("/api/v1/appliances/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    s = body["suggestions"][0]
    assert s["appliance_name"] == "Washer"
    assert s["is_negative"] is True
    assert s["est_cost_pence"] < 0  # paid to run
    # ISO strings, parseable
    datetime.fromisoformat(s["recommended_start_utc"])
    assert "deadline_local" in s


def test_appliance_suggestions_empty_when_no_appliance(monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-TARIFF", raising=False)
    client = _client(monkeypatch)
    r = client.get("/api/v1/appliances/suggestions")
    assert r.status_code == 200
    assert r.json() == {"suggestions": [], "count": 0}
