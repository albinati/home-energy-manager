"""Integration tests for PR C — vacation behaviour.

`OPTIMIZATION_PRESET=vacation` causes the LP/dispatch to:
  * zero out DHW demand (no shower draw, no soft floor)
  * constrain ``chg[i] <= pv_use[i]`` (no grid charging)
  * always commit peak_export through the scenario filter (strict_savings
    drop branch removed)
  * skip the heartbeat tank-power-drift check (tank off is the intent)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src import state_machine as sm
from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    _db.init_db()
    type(config)._overrides.clear()
    sm._TANK_DRIFT_NOTIFIED = False
    yield
    type(config)._overrides.clear()


@pytest.fixture(autouse=True)
def _active_mode(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 2.0, raising=False)
    monkeypatch.setattr(config, "DHW_TANK_LITRES", 200.0, raising=False)
    monkeypatch.setattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 15, raising=False)
    monkeypatch.setattr(config, "LP_INVERTER_STRESS_COST_PENCE", 0.0, raising=False)
    monkeypatch.setattr(config, "DHW_DAILY_SHOWER_LITRES", 0.0, raising=False)
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 0.0, raising=False)


def _weather(n: int, pv: float = 0.0) -> WeatherLpSeries:
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return WeatherLpSeries(
        slot_starts_utc=[base + i * timedelta(minutes=30) for i in range(n)],
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[pv] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.8] * n,
    )


# ---------------------------------------------------------------------------
# Zero DHW demand in vacation
# ---------------------------------------------------------------------------


def test_vacation_zeros_dhw_draw(monkeypatch):
    """Vacation: no shower draw subtracted from tank balance, no shower floor.
    Tank decays naturally via standing loss only; LP plans no DHW heating."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 24
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[12.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_weather(n),
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status
    # No DHW heating allocated — tank coasts on standing loss only.
    assert sum(plan.dhw_electric_kwh) == pytest.approx(0.0, abs=1e-6)


def test_vacation_tank_can_decay_below_normal_floor(monkeypatch):
    """Without the shower floor, tank is free to drop toward the
    anti-freeze lower bound (tank_lo=20) without the LP burning kWh."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 24
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[12.0] * n,
        base_load_kwh=[0.3] * n,
        weather=_weather(n),
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
    )
    # End-of-horizon tank should have dropped (vs starting 45 °C) due to
    # standing loss with zero heating.
    assert plan.tank_temp_c[-1] < 45.0


# ---------------------------------------------------------------------------
# chg <= pv_use constraint (no grid charging)
# ---------------------------------------------------------------------------


def test_vacation_blocks_grid_charging(monkeypatch):
    """Vacation mode: bateria pode carregar SÓ a partir de PV usada. Even
    with cheap import prices, LP must keep ``chg[i] <= pv_use[i]``."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        # First 6 slots cheap → normally LP would ForceCharge from grid.
        price_pence=[5.0] * 6 + [25.0] * 6,
        base_load_kwh=[0.3] * n,
        weather=_weather(n, pv=2.0),  # some PV to make pv_use > 0
        initial=LpInitialState(soc_kwh=2.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status
    for i in range(n):
        assert plan.battery_charge_kwh[i] <= plan.pv_use_kwh[i] + 1e-6, (
            f"vacation: slot {i} grid-charged: chg={plan.battery_charge_kwh[i]} "
            f"pv_use={plan.pv_use_kwh[i]}"
        )


def test_normal_mode_can_still_grid_charge(monkeypatch):
    """Sanity check the vacation constraint is mode-gated: in normal mode
    the LP can grid-charge cheap slots as before (battery rises faster
    than PV alone would allow)."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", False, raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[5.0] * 6 + [25.0] * 6,
        base_load_kwh=[0.3] * n,
        weather=_weather(n, pv=0.0),  # zero PV — any charging must be grid
        initial=LpInitialState(soc_kwh=2.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=[20.0] * n,
    )
    assert plan.ok, plan.status
    # Some slot should have chg > pv_use (i.e. grid → battery happened).
    grid_charged = any(
        plan.battery_charge_kwh[i] > plan.pv_use_kwh[i] + 1e-6
        for i in range(n)
    )
    assert grid_charged, (
        "normal mode should allow grid charging when arbitrage profitable; "
        f"chg={plan.battery_charge_kwh} pv_use={plan.pv_use_kwh}"
    )


# ---------------------------------------------------------------------------
# PR D — peak_export is mode-gated at the LP level
# ---------------------------------------------------------------------------


def test_normal_mode_never_battery_exports(monkeypatch):
    """PR D: in normal mode the LP constraint ``exp[i] <= pv_use[i]`` (no
    contribution from ``dis``) means the LP cannot plan battery-to-grid
    arbitrage. ``dis`` may still flow for self-use (load); ``exp`` only
    surfaces when PV genuinely exceeds local consumption."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    # Big spread (cheap morning, peak afternoon) — would be classic arbitrage
    # under the pre-PR-D rules. Some PV but bounded so dis would be tempted.
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[10.0] * 6 + [40.0] * 6,
        base_load_kwh=[0.3] * n,
        weather=_weather(n, pv=0.5),
        initial=LpInitialState(soc_kwh=8.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=[35.0] * n,
    )
    assert plan.ok, plan.status
    # Critical invariant: every slot where dis > 0 must have exp <= pv_use
    # (i.e. nothing from battery shows up in exp).
    for i in range(n):
        assert plan.export_kwh[i] <= plan.pv_use_kwh[i] + 1e-6, (
            f"normal: slot {i} battery-exported: "
            f"exp={plan.export_kwh[i]:.3f} pv_use={plan.pv_use_kwh[i]:.3f} "
            f"dis={plan.battery_discharge_kwh[i]:.3f}"
        )


def test_guests_mode_never_battery_exports(monkeypatch):
    """Same invariant as normal — guests mode also gates battery export."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[10.0] * 6 + [40.0] * 6,
        base_load_kwh=[0.3] * n,
        weather=_weather(n, pv=0.5),
        initial=LpInitialState(soc_kwh=8.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=[35.0] * n,
    )
    assert plan.ok, plan.status
    for i in range(n):
        assert plan.export_kwh[i] <= plan.pv_use_kwh[i] + 1e-6, (
            f"guests: slot {i} battery-exported: "
            f"exp={plan.export_kwh[i]:.3f} pv_use={plan.pv_use_kwh[i]:.3f} "
            f"dis={plan.battery_discharge_kwh[i]:.3f}"
        )


def test_vacation_mode_can_battery_export(monkeypatch):
    """Vacation mode IS the arbitrage mode — LP is free to plan
    battery-to-grid export when peak prices justify it. The robust filter
    downstream still applies for forecast-error safety."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    # PV in the early cheap slots (charges battery), zero PV in peak slots
    # (battery must discharge alone to export). G98 export cap = 1.84 kWh/slot.
    pv_profile = [2.0] * 6 + [0.0] * 6
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[10.0] * 6 + [40.0] * 6,
        base_load_kwh=[0.3] * n,
        weather=WeatherLpSeries(
            slot_starts_utc=[base + i * timedelta(minutes=30) for i in range(n)],
            temperature_outdoor_c=[15.0] * n,
            shortwave_radiation_wm2=[0.0] * n,
            cloud_cover_pct=[40.0] * n,
            pv_kwh_per_slot=pv_profile,
            cop_space=[3.0] * n,
            cop_dhw=[2.8] * n,
        ),
        initial=LpInitialState(soc_kwh=8.0, tank_temp_c=45.0),
        tz=ZoneInfo("Europe/London"),
        export_price_pence=[35.0] * n,
    )
    assert plan.ok, plan.status
    # In a peak slot with zero PV, any export must come from battery discharge.
    EPS = 1e-3
    battery_exported = any(
        plan.export_kwh[i] > EPS
        and plan.pv_use_kwh[i] < EPS
        and plan.battery_discharge_kwh[i] > EPS
        for i in range(n)
    )
    assert battery_exported, (
        "vacation should plan battery export when peak prices justify; "
        f"dis={plan.battery_discharge_kwh} exp={plan.export_kwh} "
        f"pv_use={plan.pv_use_kwh}"
    )


def test_lp_plan_to_slots_emits_peak_export_only_under_vacation(monkeypatch):
    """End-to-end check via the labeller: vacation produces peak_export
    slots; normal does not (because the LP doesn't plan exp > pv_use)."""
    from src.scheduler.lp_dispatch import lp_plan_to_slots
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    # PV early (charges battery), zero PV at peak (any export must come
    # from battery → labeller must see exp > pv_use → peak_export).
    pv_profile = [2.0] * 6 + [0.0] * 6

    def _solve(preset: str):
        monkeypatch.setattr(config, "OPTIMIZATION_PRESET", preset, raising=False)
        return solve_lp(
            slot_starts_utc=slots,
            price_pence=[10.0] * 6 + [40.0] * 6,
            base_load_kwh=[0.3] * n,
            weather=WeatherLpSeries(
                slot_starts_utc=[base + i * timedelta(minutes=30) for i in range(n)],
                temperature_outdoor_c=[15.0] * n,
                shortwave_radiation_wm2=[0.0] * n,
                cloud_cover_pct=[40.0] * n,
                pv_kwh_per_slot=pv_profile,
                cop_space=[3.0] * n,
                cop_dhw=[2.8] * n,
            ),
            initial=LpInitialState(soc_kwh=8.0, tank_temp_c=45.0),
            tz=ZoneInfo("Europe/London"),
            export_price_pence=[35.0] * n,
        )

    p_vac = _solve("vacation")
    p_norm = _solve("normal")
    kinds_vac = {s.kind for s in lp_plan_to_slots(p_vac)}
    kinds_norm = {s.kind for s in lp_plan_to_slots(p_norm)}
    assert "peak_export" in kinds_vac, f"vacation kinds: {kinds_vac}"
    assert "peak_export" not in kinds_norm, f"normal kinds: {kinds_norm}"


# ---------------------------------------------------------------------------
# Heartbeat tank-drift check is no-op in vacation
# ---------------------------------------------------------------------------


@dataclass
class _FakeDev:
    id: str = "dev-1"
    name: str = "Altherma"
    tank_on: bool | None = False
    tank_target: float | None = None
    tank_powerful: bool | None = None
    is_on: bool | None = None
    lwt_offset: float | None = None


def test_drift_check_disabled_in_vacation_mode(monkeypatch):
    """Tank OFF in vacation is the INTENDED state — heartbeat must not alert
    or force-restore. PR C added an early-return at the top of
    `_check_tank_power_drift` when mode=vacation."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    monkeypatch.setattr(config, "TANK_DRIFT_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TANK_DRIFT_AUTO_RECOVER", True, raising=False)

    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    apply_restore = MagicMock()
    notify_risk = MagicMock()
    notify_critical = MagicMock()
    monkeypatch.setattr(sm, "apply_comfort_restore", apply_restore)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", notify_critical)

    sm._check_tank_power_drift(
        [], client, dev, datetime(2026, 6, 1, 19, 0, tzinfo=UTC), trigger="hb",
    )

    apply_restore.assert_not_called()
    notify_risk.assert_not_called()
    notify_critical.assert_not_called()
    assert sm._TANK_DRIFT_NOTIFIED is False


def test_drift_check_still_fires_in_normal_mode(monkeypatch):
    """Sanity check the vacation exemption doesn't affect normal mode."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "TANK_DRIFT_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TANK_DRIFT_AUTO_RECOVER", True, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "USER_OVERRIDE_RESPECT_HOURS", 4.0, raising=False)

    dev = _FakeDev(tank_on=False)
    client = MagicMock()
    apply_restore = MagicMock()
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "apply_comfort_restore", apply_restore)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", MagicMock())

    sm._check_tank_power_drift(
        [], client, dev, datetime(2026, 6, 1, 19, 0, tzinfo=UTC), trigger="hb",
    )

    apply_restore.assert_called_once()
    notify_risk.assert_called_once()
    assert sm._TANK_DRIFT_NOTIFIED is True


# ---------------------------------------------------------------------------
# strict_savings drop branch is gone — peak_export passes through to filter
# ---------------------------------------------------------------------------


def test_filter_robust_peak_export_no_strict_savings_drop(monkeypatch):
    """PR C removed the ENERGY_STRATEGY_MODE=strict_savings kill switch.
    A peak_export slot now reaches the scenario filter regardless of mode."""
    from src.scheduler.lp_dispatch import filter_robust_peak_export
    from src.scheduler.lp_optimizer import LpPlan

    # Construct a minimal plan with one peak_export slot.
    base = datetime(2026, 6, 1, 16, 30, tzinfo=UTC)
    plan = LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=[base],
        price_pence=[35.0],
        import_kwh=[0.0],
        export_kwh=[1.84],
        battery_charge_kwh=[0.0],
        battery_discharge_kwh=[2.0],
        pv_use_kwh=[0.0],
        pv_curtail_kwh=[0.0],
        dhw_electric_kwh=[0.0],
        space_electric_kwh=[0.0],
        soc_kwh=[5.0, 3.0],
        tank_temp_c=[45.0, 45.0],
        temp_outdoor_c=[15.0],
        peak_threshold_pence=30.0,
        cheap_threshold_pence=10.0,
    )
    # No scenarios → filter commits by default rule
    slots, decisions = filter_robust_peak_export(plan, scenarios=None)
    pe = [d for d in decisions if d["lp_kind"] == "peak_export"]
    assert len(pe) == 1
    assert pe[0]["committed"] is True
    assert pe[0]["reason"] == "no_scenarios_run"
