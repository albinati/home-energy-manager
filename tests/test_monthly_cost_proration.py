"""Standing-charge proration + fixed-tariff shadow in the energy-insights engine.

The bug fixed here: for the *current* (in-progress) month the standing charge
was billed for the full calendar month while energy/import/export covered only
elapsed days, inflating the net bill and the per-day cost-breakdown bars. The
fix prorates standing to the elapsed-day window (``n_days``) while leaving
completed past months on the full month. We also assert the new fixed-tariff
counterfactual (``delta_vs_fixed_*``) is computed on the same basis, and that
the realised export now bills at per-slot Outgoing Agile rates.

All tests use a fake FoxESSClient — never touches the cloud.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date

import pytest

from src.config import config
from src.energy import monthly
from src.energy import tariff_engine


@pytest.fixture(autouse=True)
def _fixed_tariff(monkeypatch):
    """Configure a known manual tariff + fixed-tariff so shadows populate."""
    monkeypatch.setattr(config, "MANUAL_TARIFF_IMPORT_PENCE", 20.0, raising=False)
    monkeypatch.setattr(config, "MANUAL_TARIFF_EXPORT_PENCE", 5.0, raising=False)
    monkeypatch.setattr(config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 50.0, raising=False)
    monkeypatch.setattr(config, "FIXED_TARIFF_LABEL", "Test Fixed", raising=False)
    monkeypatch.setattr(config, "FIXED_TARIFF_RATE_PENCE", 30.0, raising=False)
    monkeypatch.setattr(config, "FIXED_TARIFF_STANDING_PENCE_PER_DAY", 45.0, raising=False)
    # Export tariff: pin deterministically so tests don't depend on prod .env.
    # seg_flat is the household's actual mode; SEG rate matches the shadow's
    # SEG_EXPORT_FALLBACK_PENCE constant so realised and shadow export agree.
    monkeypatch.setattr(config, "EXPORT_TARIFF_MODE", "seg_flat", raising=False)
    monkeypatch.setattr(config, "EXPORT_SEG_RATE_PENCE", monthly.SEG_EXPORT_FALLBACK_PENCE, raising=False)
    monkeypatch.setattr(config, "OCTOPUS_API_KEY", "", raising=False)  # force manual path


def _energy(year, month, import_kwh=100.0, export_kwh=40.0):
    return monthly.MonthlyEnergySummary(
        year=year, month=month, month_str=f"{year:04d}-{month:02d}",
        import_kwh=import_kwh, export_kwh=export_kwh, solar_kwh=0.0,
        load_kwh=0.0, charge_kwh=0.0, discharge_kwh=0.0,
    )


# ── _compute_cost / _best_cost: n_days proration ─────────────────────────────

def test_compute_cost_full_month_when_n_days_none():
    e = _energy(2026, 1)  # 31-day month
    cost = monthly._compute_cost(e, n_days=None)
    assert cost.standing_charge_pence == 31 * 50.0


def test_compute_cost_prorates_to_n_days():
    e = _energy(2026, 1)
    cost = monthly._compute_cost(e, n_days=10)
    assert cost.standing_charge_pence == 10 * 50.0
    # net = import×rate + standing − export×SEG (seg_flat mode → SEG, not the 5p manual rate)
    assert cost.net_cost_pence == pytest.approx(
        100 * 20.0 + 10 * 50.0 - 40 * monthly.SEG_EXPORT_FALLBACK_PENCE
    )


def test_best_cost_threads_n_days(monkeypatch):
    e = _energy(2026, 1)
    cost = monthly._best_cost(e, n_days=7)
    assert cost.standing_charge_pence == 7 * 50.0


# ── fixed-tariff shadow ──────────────────────────────────────────────────────

def test_fixed_shadow_on_same_basis():
    e = _energy(2026, 1, import_kwh=100.0, export_kwh=40.0)
    cost = monthly._compute_cost(e, n_days=10)
    # shadow = import×fixed_rate + n_days×fixed_standing − export×SEG(4p)
    expected_shadow = 100 * 30.0 + 10 * 45.0 - 40 * monthly.SEG_EXPORT_FALLBACK_PENCE
    assert cost.fixed_shadow_pence == pytest.approx(expected_shadow)
    assert cost.delta_vs_fixed_pence == pytest.approx(expected_shadow - cost.net_cost_pence)
    # pounds properties
    assert cost.delta_vs_fixed_pounds == pytest.approx(cost.delta_vs_fixed_pence / 100)


def test_fixed_shadow_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "FIXED_TARIFF_LABEL", "", raising=False)
    monkeypatch.setattr(config, "FIXED_TARIFF_RATE_PENCE", 0.0, raising=False)
    cost = monthly._compute_cost(_energy(2026, 1), n_days=10)
    assert cost.fixed_shadow_pence is None
    assert cost.delta_vs_fixed_pence is None
    assert cost.delta_vs_fixed_pounds is None


# ── get_period_insights month branch: proration matches chart_data ───────────

class _FakeClient:
    """Daily breakdown with `days_with_data` rows; optionally zero-pad to full month."""

    def __init__(self, days_with_data: int, pad_to_full: bool = False):
        self.days_with_data = days_with_data
        self.pad_to_full = pad_to_full

    def get_energy_month_daily_breakdown(self, year, month):
        _, ndays = monthrange(year, month)
        n = ndays if self.pad_to_full else self.days_with_data
        rows = [
            {
                "date": f"{year:04d}-{month:02d}-{d:02d}",
                "import_kwh": 3.0, "export_kwh": 1.0, "solar_kwh": 0.0,
                "load_kwh": 0.0, "charge_kwh": 0.0, "discharge_kwh": 0.0,
            }
            for d in range(1, n + 1)
        ]
        totals = {
            "gridConsumptionEnergyToday": 3.0 * self.days_with_data,
            "feedinEnergyToday": 1.0 * self.days_with_data,
            "pvEnergyToday": 0.0, "loadEnergyToday": 0.0,
            "chargeEnergyToday": 0.0, "dischargeEnergyToday": 0.0,
        }
        return totals, rows


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(monthly, "_client", lambda: client)
    monkeypatch.setattr(monthly, "_get_daikin_heating_kwh", lambda *a, **k: None)
    monkeypatch.setattr(monthly, "_build_heating_analytics", lambda *a, **k: None)
    # These tests cover the Fox/Octopus engine proration + chart_data trimming,
    # which is now the FALLBACK. Force it by making the pnl engine decline, so
    # they stay deterministic and independent of the SQLite pnl path.
    monkeypatch.setattr(monthly, "_pnl_cost_for_range", lambda *a, **k: None)


def test_period_month_current_prorates_standing(monkeypatch):
    today = date.today()
    elapsed = today.day
    _patch_client(monkeypatch, _FakeClient(days_with_data=elapsed))
    out = monthly.get_period_insights("month", month_str=today.strftime("%Y-%m"))
    cost = out.insights.cost
    days = len(out.chart_data)
    # The UI invariant: standing / chart_data.length == standing_per_day.
    assert days == elapsed
    assert cost.standing_charge_pence == pytest.approx(days * 50.0)


def test_period_month_current_clamps_zero_padded_days(monkeypatch):
    """Fox zero-pads the current month to a full breakdown — we trim chart_data
    to elapsed days so standing, day-count, and the UI per-day math all agree."""
    today = date.today()
    _patch_client(monkeypatch, _FakeClient(days_with_data=today.day, pad_to_full=True))
    out = monthly.get_period_insights("month", month_str=today.strftime("%Y-%m"))
    # chart_data trimmed to elapsed days (not the full padded month).
    assert len(out.chart_data) == today.day
    assert out.insights.cost.standing_charge_pence == pytest.approx(today.day * 50.0)
    # The UI invariant: standing / chart_data.length == standing_per_day.
    assert out.insights.cost.standing_charge_pence / len(out.chart_data) == pytest.approx(50.0)


def test_period_month_past_bills_full_month(monkeypatch):
    """Regression guard: a completed past month still bills the full month."""
    today = date.today()
    # pick a month strictly before this one
    py, pm = (today.year - 1, today.month)
    _, ndays = monthrange(py, pm)
    _patch_client(monkeypatch, _FakeClient(days_with_data=ndays))
    out = monthly.get_period_insights("month", month_str=f"{py:04d}-{pm:02d}")
    assert out.insights.cost.standing_charge_pence == pytest.approx(ndays * 50.0)


def test_pnl_cost_for_range_maps_real_money_fields(monkeypatch):
    """_pnl_cost_for_range maps compute_period_pnl's real-money axis correctly:
    import_cost_gbp, export_revenue_gbp, standing_charge_gbp, realised_net_cost_gbp,
    and the FIXED_TARIFF_* shadow → pence on MonthlyCostSummary."""
    import src.analytics.pnl as pnl_mod
    fake = {
        "n_days": 31,
        "import_cost_gbp": 50.91,
        "export_revenue_gbp": 2.26,
        "standing_charge_gbp": 18.37,
        "realised_net_cost_gbp": 67.02,
        "import_kwh": 314.1,
        "export_kwh": 55.2,
        "fixed_tariff_shadow_real_gbp": 75.0,
        "delta_vs_fixed_tariff_real_gbp": 7.98,
    }
    monkeypatch.setattr(pnl_mod, "compute_period_pnl", lambda s, e: fake)
    res = monthly._pnl_cost_for_range(date(2026, 5, 1), date(2026, 5, 31))
    assert res is not None
    cost, imp, exp = res
    assert cost.import_cost_pence == pytest.approx(5091.0)
    assert cost.export_earnings_pence == pytest.approx(226.0)
    assert cost.standing_charge_pence == pytest.approx(1837.0)
    assert cost.net_cost_pence == pytest.approx(6702.0)
    assert cost.fixed_shadow_pence == pytest.approx(7500.0)
    assert cost.delta_vs_fixed_pence == pytest.approx(798.0)
    assert (imp, exp) == pytest.approx((314.1, 55.2))


def test_pnl_cost_for_range_returns_none_when_unpriced(monkeypatch):
    """Empty/pre-Agile range (n_days == 0) → None so the caller falls back."""
    import src.analytics.pnl as pnl_mod
    monkeypatch.setattr(pnl_mod, "compute_period_pnl", lambda s, e: {"n_days": 0})
    assert monthly._pnl_cost_for_range(date(2026, 1, 1), date(2026, 1, 31)) is None


def test_pnl_cost_for_range_no_fixed_shadow(monkeypatch):
    """When FIXED_TARIFF_* isn't configured, pnl omits the bg keys → None shadow."""
    import src.analytics.pnl as pnl_mod
    monkeypatch.setattr(pnl_mod, "compute_period_pnl", lambda s, e: {
        "n_days": 7, "import_cost_gbp": 10.0, "export_revenue_gbp": 1.0,
        "standing_charge_gbp": 4.0, "realised_net_cost_gbp": 13.0,
        "import_kwh": 60.0, "export_kwh": 24.0,
    })
    res = monthly._pnl_cost_for_range(date(2026, 5, 1), date(2026, 5, 7))
    assert res is not None
    cost, _, _ = res
    assert cost.fixed_shadow_pence is None
    assert cost.delta_vs_fixed_pence is None


def test_period_month_prefers_pnl_and_overrides_kwh(monkeypatch):
    """When the pnl engine prices the range, get_period_insights uses ITS cost
    and import/export kWh (so Home matches Hero + Insights), not the Fox sum."""
    today = date.today()
    _patch_client(monkeypatch, _FakeClient(days_with_data=today.day))
    # Fox would report import=3×days, export=1×days. pnl says otherwise.
    pnl_cost = monthly.MonthlyCostSummary(
        import_cost_pence=5000.0, export_earnings_pence=226.0,
        standing_charge_pence=1837.0, net_cost_pence=6611.0,
        fixed_shadow_pence=7000.0, delta_vs_fixed_pence=389.0,
    )
    monkeypatch.setattr(
        monthly, "_pnl_cost_for_range",
        lambda start, end: (pnl_cost, 314.1, 55.2),
    )
    out = monthly.get_period_insights("month", month_str=today.strftime("%Y-%m"))
    # Cost comes from pnl…
    assert out.insights.cost.net_cost_pence == pytest.approx(6611.0)
    assert out.insights.cost.export_earnings_pence == pytest.approx(226.0)
    # …and so do the displayed import/export kWh (overridden from Fox).
    assert out.insights.energy.import_kwh == pytest.approx(314.1)
    assert out.insights.energy.export_kwh == pytest.approx(55.2)
    # Equation balances: paid + standing − earned == net.
    c = out.insights.cost
    assert c.import_cost_pence + c.standing_charge_pence - c.export_earnings_pence == pytest.approx(c.net_cost_pence)


# ── _compute_cost_octopus: per-slot Outgoing export billing ──────────────────

def test_octopus_export_billed_per_slot(monkeypatch):
    """In outgoing_agile mode, export revenue uses per-slot rates, not a flat rate."""
    from datetime import datetime, UTC

    import src.db as db_mod
    import src.energy.octopus_client as oc
    import src.scheduler.agile as agile_mod

    monkeypatch.setattr(config, "OCTOPUS_API_KEY", "key", raising=False)
    monkeypatch.setattr(config, "OCTOPUS_EXPORT_TARIFF_CODE", "E-1R-AGILE-OUTGOING", raising=False)
    monkeypatch.setattr(config, "MANUAL_TARIFF_EXPORT_PENCE", 5.0, raising=False)
    monkeypatch.setattr(config, "EXPORT_TARIFF_MODE", "outgoing_agile", raising=False)

    class _Slot:
        def __init__(self, interval_start, consumption_kwh):
            self.interval_start = interval_start
            self.consumption_kwh = consumption_kwh

    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)

    monkeypatch.setattr(oc, "get_mpan_roles", lambda *a, **k: oc.MpanRoles(
        import_mpan="imp", import_serial="is", export_mpan="exp", export_serial="es",
        gsp="_C", source="test"))

    def _fake_consumption(mpan, serial, pf, pt):
        if mpan == "imp":
            return [_Slot(t0, 2.0), _Slot(t1, 2.0)]
        return [_Slot(t0, 1.0), _Slot(t1, 1.0)]  # 1 kWh export each slot
    monkeypatch.setattr(oc, "fetch_consumption", _fake_consumption)
    monkeypatch.setattr(agile_mod, "fetch_agile_rates", lambda **k: [
        {"valid_from": t0.isoformat(), "value_inc_vat": 10.0},
        {"valid_from": t1.isoformat(), "value_inc_vat": 10.0},
    ])
    # Per-slot OUTGOING rates: 30p at noon, 50p at 12:30 — far above the 5p flat.
    monkeypatch.setattr(db_mod, "get_agile_export_rates_in_range", lambda a, b: [
        {"valid_from": t0.isoformat(), "value_inc_vat": 30.0},
        {"valid_from": t1.isoformat(), "value_inc_vat": 50.0},
    ])

    e = _energy(2026, 1, import_kwh=4.0, export_kwh=2.0)
    cost = monthly._compute_cost_octopus(e, n_days=1)
    assert cost is not None
    # 1×30 + 1×50 = 80p, NOT 2×5 = 10p flat.
    assert cost.export_earnings_pence == pytest.approx(80.0)


def test_octopus_export_billed_flat_seg_by_default(monkeypatch):
    """Default seg_flat mode values export at the flat SEG rate, ignoring Agile."""
    from datetime import datetime, UTC

    import src.db as db_mod
    import src.energy.octopus_client as oc
    import src.scheduler.agile as agile_mod

    monkeypatch.setattr(config, "OCTOPUS_API_KEY", "key", raising=False)
    monkeypatch.setattr(config, "OCTOPUS_EXPORT_TARIFF_CODE", "E-1R-AGILE-OUTGOING", raising=False)
    monkeypatch.setattr(config, "EXPORT_TARIFF_MODE", "seg_flat", raising=False)
    monkeypatch.setattr(config, "EXPORT_SEG_RATE_PENCE", 4.10, raising=False)

    class _Slot:
        def __init__(self, interval_start, consumption_kwh):
            self.interval_start = interval_start
            self.consumption_kwh = consumption_kwh

    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)

    monkeypatch.setattr(oc, "get_mpan_roles", lambda *a, **k: oc.MpanRoles(
        import_mpan="imp", import_serial="is", export_mpan="exp", export_serial="es",
        gsp="_C", source="test"))

    def _fake_consumption(mpan, serial, pf, pt):
        if mpan == "imp":
            return [_Slot(t0, 2.0), _Slot(t1, 2.0)]
        return [_Slot(t0, 1.0), _Slot(t1, 1.0)]  # 1 kWh export each slot
    monkeypatch.setattr(oc, "fetch_consumption", _fake_consumption)
    monkeypatch.setattr(agile_mod, "fetch_agile_rates", lambda **k: [
        {"valid_from": t0.isoformat(), "value_inc_vat": 10.0},
        {"valid_from": t1.isoformat(), "value_inc_vat": 10.0},
    ])
    # High Agile rates must be IGNORED in seg_flat mode.
    monkeypatch.setattr(db_mod, "get_agile_export_rates_in_range", lambda a, b: [
        {"valid_from": t0.isoformat(), "value_inc_vat": 30.0},
        {"valid_from": t1.isoformat(), "value_inc_vat": 50.0},
    ])

    e = _energy(2026, 1, import_kwh=4.0, export_kwh=2.0)
    cost = monthly._compute_cost_octopus(e, n_days=1)
    assert cost is not None
    # 2 kWh × 4.10p flat SEG = 8.2p, NOT the 80p per-slot Agile.
    assert cost.export_earnings_pence == pytest.approx(8.2)


# ── tariff_engine usage-days proration ───────────────────────────────────────

def test_usage_days_current_month_is_elapsed(monkeypatch):
    import src.foxess as foxess
    today = date.today()
    monkeypatch.setattr(
        foxess, "get_cached_energy_month",
        lambda y, m: {"gridConsumptionEnergyToday": 50.0, "feedinEnergyToday": 10.0},
        raising=False,
    )
    imp, exp, days = tariff_engine._get_usage_data(months_back=1)
    assert days == today.day

