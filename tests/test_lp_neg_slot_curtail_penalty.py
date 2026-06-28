"""Negative-price slots are exempt from the PV-curtailment penalty.

During a negative-import window the LP is PAID to import, export is impossible
(per-slot import/export mutual exclusion), and grid->battery earns the negative
price while pv->battery earns nothing. The flat 15p curtailment penalty
(``LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH``) wrongly discouraged curtailing PV there,
so the LP self-consumed PV instead of importing from the paid grid. The
``LP_NEG_SLOT_NO_CURTAIL_PENALTY`` exemption (default on) restores the correct
ranking: curtail PV + grid-charge in the deepest-negative slots.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


def _solve_neg_window(*, exempt: bool):
    """Short all-negative midday window with abundant PV, battery starting near
    reserve (room to charge), tank hot (no DHW heating needed). Returns the
    LpPlan. Mirrors the feasible 4-slot pattern in test_lp_export_per_slot."""
    n = 4
    t0 = datetime(2026, 6, 12, 11, 0, tzinfo=UTC)
    slots = [t0 + timedelta(minutes=30 * i) for i in range(n)]
    prices = [-3.0, -6.0, -8.0, -4.0]
    pv = [1.5, 1.6, 1.6, 1.3]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[18.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=pv,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    init = LpInitialState(soc_kwh=2.0, tank_temp_c=52.0)
    config.LP_NEG_SLOT_NO_CURTAIL_PENALTY = exempt
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=[0.3] * n,
        weather=weather,
        initial=init,
        tz=ZoneInfo("Europe/London"),
        # Deep-negative windows are solar oversupply → the Outgoing rate is also
        # <= 0, so exporting the PV is not an option and curtailing it to capture
        # the PAID import is the correct move. (When Outgoing > 0 the export-aware
        # penalty makes the LP export instead — see the adversarial tests below.)
        export_price_pence=[-2.0, -3.0, -2.5, -1.5],
    )
    assert plan.ok, plan.status
    return plan, [i for i in range(n) if prices[i] < 0]


def _neg_totals(plan, neg_idx):
    imp = sum(plan.import_kwh[i] for i in neg_idx)
    curt = sum(plan.pv_curtail_kwh[i] for i in neg_idx)
    chg = sum(plan.battery_charge_kwh[i] for i in neg_idx)
    return imp, curt, chg


def test_curtail_penalty_exemption_prefers_paid_grid(monkeypatch):
    """With the exemption ON the LP curtails PV and imports MORE from the paid
    grid in the negative window, for the SAME battery charge — strictly better
    paid-import economics than the uniform-penalty baseline."""
    monkeypatch.setattr(config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 15.0, raising=False)

    plan_off, neg = _solve_neg_window(exempt=False)
    plan_on, _ = _solve_neg_window(exempt=True)

    imp_off, curt_off, chg_off = _neg_totals(plan_off, neg)
    imp_on, curt_on, chg_on = _neg_totals(plan_on, neg)

    # Baseline never curtails (penalty 15p > any |neg price|).
    assert curt_off < 1e-6, f"baseline should not curtail, got {curt_off}"
    # Exemption curtails PV and imports materially more from the paid grid.
    assert curt_on > 1.0, f"exemption should curtail PV, got {curt_on}"
    assert imp_on > imp_off + 1.0, f"exemption should import more: {imp_on} vs {imp_off}"
    # The extra paid import goes into the battery (or displaces PV that was
    # charging it) — the exemption never charges LESS than the penalised baseline.
    assert chg_on >= chg_off - 0.1, f"exemption should charge >= baseline: {chg_on} vs {chg_off}"
    # The extra import is paid (negative price) → strictly better paid-import £.
    earn_off = sum(plan_off.import_kwh[i] * (-plan_off.price_pence[i]) for i in neg)
    earn_on = sum(plan_on.import_kwh[i] * (-plan_on.price_pence[i]) for i in neg)
    assert earn_on > earn_off + 5.0, f"paid-import earnings should rise: {earn_on} vs {earn_off}"


def test_exemption_off_keeps_uniform_penalty(monkeypatch):
    """Kill-switch: with the exemption OFF, negative slots keep the penalty, so
    the LP does not curtail (legacy behaviour)."""
    monkeypatch.setattr(config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 15.0, raising=False)
    plan_off, neg = _solve_neg_window(exempt=False)
    _, curt_off, _ = _neg_totals(plan_off, neg)
    assert curt_off < 1e-6, f"legacy path must not curtail in negatives, got {curt_off}"


# --------------------------------------------------------------------------
# Adversarial: the OPPOSITE scenario + boundaries. The exemption only touches
# price<0 slots, and even there it must NOT discard PV that has real value
# (exportable at a positive Outgoing rate). It must also leave positive-price
# behaviour untouched.
# --------------------------------------------------------------------------

def _solve(prices, pv, *, exempt, export_price_pence=None, soc=2.0):
    n = len(prices)
    t0 = datetime(2026, 6, 12, 11, 0, tzinfo=UTC)
    slots = [t0 + timedelta(minutes=30 * i) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[18.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=pv,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    config.LP_NEG_SLOT_NO_CURTAIL_PENALTY = exempt
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=[0.3] * n,
        weather=weather,
        initial=LpInitialState(soc_kwh=soc, tank_temp_c=52.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=export_price_pence,
    )
    assert plan.ok, plan.status
    return plan


def test_positive_price_day_identical_with_or_without_exemption(monkeypatch):
    """The exemption must NOT change anything on a positive-price day — it only
    gates price<0 slots. Opposite regime: solar is the asset, not the grid."""
    monkeypatch.setattr(config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 15.0, raising=False)
    prices = [9.0, 10.0, 11.0, 10.0]
    pv = [1.5, 1.6, 1.6, 1.3]
    exp = [15.0, 15.5, 16.0, 15.0]
    p_off = _solve(prices, pv, exempt=False, export_price_pence=exp)
    p_on = _solve(prices, pv, exempt=True, export_price_pence=exp)
    for i in range(len(prices)):
        assert abs(p_off.pv_curtail_kwh[i] - p_on.pv_curtail_kwh[i]) < 1e-6
        assert abs(p_off.import_kwh[i] - p_on.import_kwh[i]) < 1e-6
        assert abs(p_off.export_kwh[i] - p_on.export_kwh[i]) < 1e-6
    # And surplus PV on a positive day is exported, not curtailed.
    assert sum(p_on.export_kwh) > 1.0, f"surplus PV should export, got {p_on.export_kwh}"
    assert sum(p_on.pv_curtail_kwh) < 1e-3, f"must not curtail valuable PV, got {p_on.pv_curtail_kwh}"


def test_neg_import_high_export_prefers_export_over_curtail(monkeypatch):
    """THE adversarial opposite case: negative import BUT a high positive Outgoing
    rate. The exemption makes curtailment free in negatives — it must NOT cause
    the LP to throw away PV that is profitably exportable. The export-revenue term
    must still win, so PV is EXPORTED, not curtailed."""
    monkeypatch.setattr(config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 15.0, raising=False)
    prices = [-3.0, -6.0, -8.0, -4.0]
    pv = [1.5, 1.6, 1.6, 1.3]
    exp = [16.0, 17.0, 18.0, 16.0]  # Outgoing rate far above |import price|
    plan = _solve(prices, pv, exempt=True, export_price_pence=exp)
    total_exp = sum(plan.export_kwh)
    total_curt = sum(plan.pv_curtail_kwh)
    # With export worth ~17p and curtail worth 0, the LP must export the PV.
    assert total_exp > total_curt, (
        f"high export should beat curtail: exp={total_exp:.2f} curt={total_curt:.2f}"
    )
    assert total_exp > 1.0, f"expected real PV export, got {plan.export_kwh}"


def test_neg_import_negative_export_curtails_under_exemption(monkeypatch):
    """Negative import AND negative Outgoing rate (sunny oversupply): export is
    blocked (would pay to export), so curtailing PV to capture paid import is
    correct. The exemption allows it; the legacy penalty wrongly blocks it."""
    monkeypatch.setattr(config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 15.0, raising=False)
    prices = [-3.0, -6.0, -8.0, -4.0]
    pv = [1.5, 1.6, 1.6, 1.3]
    exp = [-2.0, -3.0, -2.5, -1.5]  # negative Outgoing → exp forced to 0
    plan_on = _solve(prices, pv, exempt=True, export_price_pence=exp)
    plan_off = _solve(prices, pv, exempt=False, export_price_pence=exp)
    assert sum(plan_on.export_kwh) < 1e-3, "export must be 0 at negative Outgoing"
    assert sum(plan_on.pv_curtail_kwh) > sum(plan_off.pv_curtail_kwh) + 0.5, (
        "exemption should curtail more than the penalised baseline when export is blocked"
    )


def test_mixed_window_curtails_only_in_negative_slots(monkeypatch):
    """Per-slot gating: in a window mixing positive and negative slots, the
    exemption must curtail (if at all) ONLY in the negative slots; positive slots
    keep the penalty so their PV is exported/stored, never curtailed."""
    monkeypatch.setattr(config, "LP_PV_CURTAIL_PENALTY_PENCE_PER_KWH", 15.0, raising=False)
    prices = [8.0, -6.0, -8.0, 9.0]
    pv = [1.5, 1.6, 1.6, 1.3]
    exp = [5.0, 5.5, 4.0, 5.0]
    plan = _solve(prices, pv, exempt=True, export_price_pence=exp)
    pos_curt = plan.pv_curtail_kwh[0] + plan.pv_curtail_kwh[3]
    assert pos_curt < 1e-3, f"positive slots must not curtail, got {pos_curt}"
