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
