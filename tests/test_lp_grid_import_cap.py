"""LP total grid-import cap is the main service fuse, decoupled from the
inverter's battery-charge rate (FOX_FORCE_CHARGE_MAX_PWR).

Total import = direct AC load (heat pump / DHW boost) + battery charge. The old
cap (= FOX_FORCE_CHARGE_MAX_PWR / 2000 = 5 kW) conflated the two, so the LP could
not plan a full-rate battery charge AND a concurrent grid-fed load at the paid
negative price. ``LP_GRID_IMPORT_MAX_KW`` sets the real (higher) total-import
ceiling; the battery charge stays bounded by ``MAX_INVERTER_KW``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


def _solve(*, import_cap_kw: float):
    """Negative-price window with a HEAVY concurrent load (≈3 kW, like a heat pump
    running) and a battery with room to charge from the paid grid. Low PV so the
    grid must cover both load and charge."""
    n = 4
    t0 = datetime(2026, 1, 15, 11, 0, tzinfo=UTC)  # winter: heat-pump season
    slots = [t0 + timedelta(minutes=30 * i) for i in range(n)]
    prices = [-3.0, -6.0, -8.0, -4.0]
    config.LP_GRID_IMPORT_MAX_KW = import_cap_kw
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[8.0] * n,
        shortwave_radiation_wm2=[150.0] * n,
        cloud_cover_pct=[70.0] * n,
        pv_kwh_per_slot=[0.2, 0.2, 0.2, 0.2],  # low PV → grid must do the work
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=[1.5] * n,  # ~3 kW concurrent household/heat load
        weather=weather,
        initial=LpInitialState(soc_kwh=2.0, tank_temp_c=52.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status
    return plan


def test_higher_import_cap_lets_lp_charge_and_grid_feed_load_in_negatives():
    """With a 5 kW total cap the LP cannot do full-rate charge + the 3 kW load at
    once; raising the cap to the real main fuse lets it import BOTH from the paid
    grid (more paid import, the battery still charges no faster than the inverter)."""
    plan_5kw = _solve(import_cap_kw=5.0)   # legacy: cap == inverter rating
    plan_10kw = _solve(import_cap_kw=10.0)  # decoupled: main fuse

    imp_5 = sum(plan_5kw.import_kwh)
    imp_10 = sum(plan_10kw.import_kwh)
    chg_5 = sum(plan_5kw.battery_charge_kwh)
    chg_10 = sum(plan_10kw.battery_charge_kwh)

    # The old 5 kW cap (2.5 kWh/slot) binds when load(1.5)+charge wants > 2.5.
    assert max(plan_5kw.import_kwh) <= 2.5 + 1e-6
    # The higher cap lets a slot import load + full charge (> 2.5 kWh/slot).
    assert max(plan_10kw.import_kwh) > 2.5 + 1e-3, plan_10kw.import_kwh
    # Net: more paid import AND at least as much battery charge.
    assert imp_10 > imp_5 + 0.5, f"expected more import: {imp_10} vs {imp_5}"
    assert chg_10 >= chg_5 - 1e-6, f"charge should not drop: {chg_10} vs {chg_5}"


def test_battery_charge_still_bounded_by_inverter_not_the_fuse():
    """Raising the import cap must NOT let the battery charge faster than the
    inverter (MAX_INVERTER_KW) — that limit is separate."""
    plan = _solve(import_cap_kw=20.0)  # absurdly high fuse
    max_batt_kwh = float(config.MAX_INVERTER_KW) * 0.5
    assert max(plan.battery_charge_kwh) <= max_batt_kwh + 1e-6, (
        f"charge {max(plan.battery_charge_kwh)} exceeded inverter cap {max_batt_kwh}"
    )
