"""Labeller priority: negative price outranks the PV-only solar_charge check.

The 2026-06-28 incident's real mechanism (recurred live 2026-07-04): a
negative-price slot whose planned charge was PV-sourced (grid_import ~= 0,
e.g. the PV-sufficiency guard blocked grid→battery) was labelled
`solar_charge` → SelfUse(minSocOnGrid=100), and the H1 firmware does not
honour that floor as a discharge freeze — the battery discharged into the
DHW boost instead of the paid grid. With LP_NEGATIVE_BEATS_SOLAR_CHARGE
(default on) those slots are `negative` → ForceCharge, the discharge-proof
mode.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config
from src.scheduler.lp_dispatch import lp_plan_to_slots
from src.scheduler.lp_optimizer import LpPlan
from src.scheduler.optimizer import _slot_fox_tuple


def _plan(price: float, chg: float, imp: float) -> LpPlan:
    t0 = datetime(2026, 7, 4, 10, 0, tzinfo=UTC)
    return LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=[t0, t0 + timedelta(minutes=30)],
        price_pence=[price, price],
        import_kwh=[imp, imp],
        export_kwh=[0.0, 0.0],
        battery_charge_kwh=[chg, chg],
        battery_discharge_kwh=[0.0, 0.0],
        pv_use_kwh=[chg, chg],
        pv_curtail_kwh=[0.0, 0.0],
        dhw_electric_kwh=[0.0, 0.0],
        space_electric_kwh=[0.0, 0.0],
        soc_kwh=[3.0, 3.5, 4.0],
        peak_threshold_pence=30.0,
    )


@pytest.fixture(autouse=True)
def _normal_preset(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "LP_NEGATIVE_BEATS_SOLAR_CHARGE", True, raising=False)


def test_negative_price_pv_charge_labelled_negative_hold():
    # price < 0, chg > 0, grid_import ~= 0 — the incident shape. No grid fill
    # planned → it is a HOLD slot → Backup (owner decision 2026-07-04).
    slots = lp_plan_to_slots(_plan(price=-4.0, chg=0.8, imp=0.0))
    assert [s.kind for s in slots] == ["negative_hold", "negative_hold"]
    wm, fds, pwr, msg, max_soc = _slot_fox_tuple(slots[0])
    assert wm == "Backup"
    assert msg == int(config.MIN_SOC_RESERVE_PERCENT)
    assert max_soc is None  # unpinned → firmware may top up from the paid grid


def test_negative_hold_forcecharge_fallback_mode(monkeypatch):
    monkeypatch.setattr(config, "LP_NEGATIVE_HOLD_FOX_MODE", "forcecharge", raising=False)
    slots = lp_plan_to_slots(_plan(price=-4.0, chg=0.8, imp=0.0))
    assert [s.kind for s in slots] == ["negative_hold", "negative_hold"]
    wm, fds, pwr, _, _ = _slot_fox_tuple(slots[0])
    assert wm == "ForceCharge"
    assert fds == slots[0].target_soc_pct


def test_positive_price_pv_charge_still_solar_charge():
    slots = lp_plan_to_slots(_plan(price=12.0, chg=0.8, imp=0.0))
    assert [s.kind for s in slots] == ["solar_charge", "solar_charge"]


def test_negative_price_grid_charge_still_negative():
    slots = lp_plan_to_slots(_plan(price=-4.0, chg=0.8, imp=1.5))
    assert [s.kind for s in slots] == ["negative", "negative"]


def test_kill_switch_restores_legacy_labelling(monkeypatch):
    monkeypatch.setattr(config, "LP_NEGATIVE_BEATS_SOLAR_CHARGE", False, raising=False)
    slots = lp_plan_to_slots(_plan(price=-4.0, chg=0.8, imp=0.0))
    assert [s.kind for s in slots] == ["solar_charge", "solar_charge"]


def test_vacation_preset_unaffected(monkeypatch):
    # Vacation's LP forbids grid→battery entirely; its SelfUse dispatch stays.
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    slots = lp_plan_to_slots(_plan(price=-4.0, chg=0.8, imp=0.0))
    assert [s.kind for s in slots] == ["solar_charge", "solar_charge"]

def test_vacation_negative_price_with_grid_import_stays_solar_charge(monkeypatch):
    # Guards the vacation-before-price ordering: reordering would make
    # vacation plans grid-charge against the vacation LP model.
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    slots = lp_plan_to_slots(_plan(price=-4.0, chg=0.8, imp=1.5))
    assert [s.kind for s in slots] == ["solar_charge", "solar_charge"]


def test_kill_switch_negative_grid_charge_still_negative(monkeypatch):
    # The kill-switch only restores the legacy solar_charge mislabel for
    # PV-only slots — a real paid grid fill must stay `negative`.
    monkeypatch.setattr(config, "LP_NEGATIVE_BEATS_SOLAR_CHARGE", False, raising=False)
    slots = lp_plan_to_slots(_plan(price=-4.0, chg=0.8, imp=1.5))
    assert [s.kind for s in slots] == ["negative", "negative"]


def test_zero_price_counts_as_negative_window():
    # price == 0 boundary: `<=` means a 0p slot is inside the negative window.
    slots = lp_plan_to_slots(_plan(price=0.0, chg=0.8, imp=0.0))
    assert [s.kind for s in slots] == ["negative_hold", "negative_hold"]

