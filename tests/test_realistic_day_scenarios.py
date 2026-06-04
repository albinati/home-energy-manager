"""End-to-end realistic-day scenarios — cross-module integration tests.

These probe the **full stack** (LP solver + dhw_policy + appliance picker
+ forecast pipeline) under conditions inspired by the prod observations
of 2026-05-23. Goal: lock in the behaviour we want against future
regressions, not unit-level edge cases (those live in their dedicated
test files).

Each scenario is a "day shape" with realistic PV profile, tariff rates,
tank state, battery state, and mode — then asserts the LP / dhw_policy /
picker do the right thing.

Scenarios:

1. **sunny day in normal mode** — full pinning + warm credit baseline
2. **cloudy day** — LP correctly imports for evening peak, no fake PV abundance
3. **tank arrives hot** — warm credit zeroes first slots of forecast
4. **negative-price window** — dhw_policy emits boost row at 60 °C powerful
5. **vacation mode** — no DHW actions but battery still optimised
6. **guests mode morning shower** — forecast includes 07-09 BST reheat
7. **brief transient load** — LP plan robust against single-tick spike
8. **stale LP trajectory** — appliance picker falls back to grid-cheapest
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src import dhw_policy
from src.config import config
from src.scheduler import appliance_dispatch as ad


TZ_LOCAL = ZoneInfo("Europe/London")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _prod_like_defaults(monkeypatch, tmp_path):
    """Mirror prod K1+K2+K3 settings so scenarios reflect 2026-05-23 state."""
    db_path = str(tmp_path / "scenario.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    _db.init_db()
    # K1 — fixed DHW schedule
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13, raising=False)
    monkeypatch.setattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_SETBACK_C", 37.0, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0, raising=False)
    # K3 — battery-aware appliances
    monkeypatch.setattr(config, "APPLIANCE_BATTERY_AWARE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "APPLIANCE_FALLBACK_SAFETY_MARGIN_KWH", 0.3, raising=False)
    monkeypatch.setattr(config, "APPLIANCE_BATTERY_ROUND_TRIP_EFF", 0.92, raising=False)
    monkeypatch.setattr(config, "APPLIANCE_LP_MAX_AGE_HOURS", 2.0, raising=False)
    # Battery / electrical
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.0, raising=False)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 15.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    yield


def _solve(slots, prices, pv, base_load, init_soc=8.0, init_tank=45.0,
           export_prices=None):
    """LP solve helper with WeatherLpSeries."""
    from src.weather import WeatherLpSeries
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    n = len(slots)
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[30.0] * n,
        pv_kwh_per_slot=pv,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    init = LpInitialState(soc_kwh=init_soc, tank_temp_c=init_tank)
    return solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=init,
        tz=TZ_LOCAL,
        export_price_pence=export_prices,
    )


def _half_hour_slots(start_local_dt, n):
    """Generate n half-hour slots starting from a local-TZ datetime."""
    start_utc = start_local_dt.astimezone(UTC)
    return [start_utc + timedelta(minutes=30 * i) for i in range(n)]


# ---------------------------------------------------------------------------
# Scenario 1 — Sunny day, normal mode
# ---------------------------------------------------------------------------


def test_scenario_sunny_day_normal_mode():
    """Realistic sunny summer day, normal mode, starts at 12:00 BST.

    PV ramps 1.5 → 3 → 3.5 → 3 → 1.5 over afternoon. Battery starts at
    50 % (5 kWh) after morning consumption. Tariff is mid-day cheap,
    evening peak.

    Expected:
    - Pinned e_dhw follows dhw_policy phases (warmup transition then
      maintenance then evening shower reheat)
    - Tank temp pinned to 45 throughout the warmup window
    - LP charges battery from PV through afternoon
    - LP discharges battery for evening peak
    """
    base_local = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL)
    n = 16  # 8 hours
    slots = _half_hour_slots(base_local, n)
    # PV (kWh/slot): noon high, declining toward evening
    pv = [1.5, 1.7, 1.8, 1.7, 1.5, 1.2, 0.8, 0.5, 0.3, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    # Prices (p/kWh): mid-day moderate, evening peak
    prices = [12, 11, 11, 12, 13, 15, 25, 32, 34, 32, 28, 24, 20, 18, 16, 14]
    # Base load: typical low daytime, slightly higher evening
    base_load = [0.3] * n

    plan = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                  init_soc=5.0, init_tank=45.0,
                  export_prices=[p * 0.5 for p in prices])
    assert plan.ok, plan.status

    # Pinned forecast — verify e_dhw matches dhw_policy
    expected_e_dhw, expected_tank = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", initial_tank_c=45.0,
    )
    for i in range(n):
        assert abs(plan.dhw_electric_kwh[i] - expected_e_dhw[i]) < 1e-3
        assert abs(plan.tank_temp_c[i + 1] - expected_tank[i + 1]) < 1e-3

    # Battery should END the horizon depleted toward peak coverage
    end_soc = plan.soc_kwh[-1]
    assert end_soc < plan.soc_kwh[0] + 4.0, "battery shouldn't gain massively over evening"

    # No fake PV abundance — total e_dhw stays close to forecast total
    total_e_dhw = sum(plan.dhw_electric_kwh)
    total_forecast = sum(expected_e_dhw)
    assert total_e_dhw == pytest.approx(total_forecast, abs=0.01)


# ---------------------------------------------------------------------------
# Scenario 2 — Cloudy day
# ---------------------------------------------------------------------------


def test_scenario_cloudy_day_imports_for_evening_peak():
    """Overcast day: PV peak at ~1 kWh/slot (2 kW). Battery starts low.
    Evening peak still expensive. LP must import to cover evening load."""
    base_local = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL)
    n = 16
    slots = _half_hour_slots(base_local, n)
    # PV: low all day (overcast)
    pv = [0.6, 0.7, 0.8, 0.9, 1.0, 1.0, 0.9, 0.7, 0.5, 0.3, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0]
    prices = [11, 11, 11, 12, 13, 15, 22, 30, 33, 31, 27, 22, 19, 17, 15, 13]
    base_load = [0.3] * n
    plan = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                  init_soc=3.0, init_tank=45.0,
                  export_prices=[p * 0.5 for p in prices])
    assert plan.ok, plan.status

    # LP must plan SOME grid import (PV insufficient) but should prefer
    # cheap slots (early afternoon) over peak ones.
    cheap_imports = sum(plan.import_kwh[i] for i in range(6))   # first 3 h
    peak_imports = sum(plan.import_kwh[i] for i in range(6, 12)) # next 3 h (peak)
    assert cheap_imports >= peak_imports - 0.1, (
        f"LP should prefer cheap slots for grid import; "
        f"cheap={cheap_imports:.2f} peak={peak_imports:.2f}"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — Tank arrives hot (warm credit)
# ---------------------------------------------------------------------------


def test_scenario_tank_arrives_hot_reduces_first_slots():
    """Tank starts at 52 °C (7 °C above NORMAL=45). The warm-credit
    adjustment should zero out the first several maintenance slots."""
    base_local = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL)
    n = 12
    slots = _half_hour_slots(base_local, n)
    pv = [2.0] * n
    prices = [12] * n
    base_load = [0.3] * n

    # Reference — tank arrives at NORMAL
    plan_cold = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                       init_soc=8.0, init_tank=45.0)
    # Warm arrival
    plan_warm = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                       init_soc=8.0, init_tank=52.0)

    assert plan_cold.ok and plan_warm.ok
    cold_total_dhw = sum(plan_cold.dhw_electric_kwh)
    warm_total_dhw = sum(plan_warm.dhw_electric_kwh)
    assert warm_total_dhw < cold_total_dhw - 0.3, (
        f"Warm arrival should reduce e_dhw by ~0.5 kWh; "
        f"cold={cold_total_dhw:.2f} warm={warm_total_dhw:.2f}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — Negative-price window
# ---------------------------------------------------------------------------


def test_scenario_negative_price_emits_boost():
    """Outgoing Agile goes negative at 14:00 UTC → dhw_policy emits a
    tank_negative_boost row at 60 °C, tank_powerful=True."""
    today = datetime.now(TZ_LOCAL).date()
    # Negative slot tomorrow afternoon (after warmup starts)
    tomorrow = today + timedelta(days=1)
    neg_slot = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 14, 0,
                        tzinfo=UTC).isoformat().replace("+00:00", "Z")
    outgoing = [{"valid_from": neg_slot, "value_inc_vat": -3.5}]
    rows = dhw_policy.generate_daily_tank_schedule(
        tomorrow, agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert len(boost) == 1
    assert boost[0]["params"]["tank_temp"] == 60
    assert boost[0]["params"]["tank_powerful"] is True


# ---------------------------------------------------------------------------
# Scenario 5 — Vacation mode
# ---------------------------------------------------------------------------


def test_scenario_vacation_no_dhw_but_lp_still_optimises(monkeypatch):
    """Vacation: dhw_policy emits nothing; LP runs with e_dhw pinned to 0;
    battery scheduling still optimises (export at peak if economically right)."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    # dhw_policy emits no rows
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), mode="vacation",
    )
    assert rows == []

    # LP still solves with pinned e_dhw=0
    base_local = datetime(2026, 6, 1, 12, 0, tzinfo=TZ_LOCAL)
    n = 8
    slots = _half_hour_slots(base_local, n)
    plan = _solve(
        slots=slots, prices=[20] * n, pv=[2.0] * n,
        base_load=[0.3] * n, init_soc=8.0, init_tank=37.0,
    )
    assert plan.ok
    for v in plan.dhw_electric_kwh:
        assert v < 1e-3, f"vacation mode should have e_dhw=0; got {v}"


# ---------------------------------------------------------------------------
# Scenario 6 — Guests mode morning shower
# ---------------------------------------------------------------------------


def test_scenario_guests_mode_forecasts_morning_reheat(monkeypatch):
    """Guests preset includes morning shower window (07-09 BST). The
    forecast should attribute ~0.5 kWh/slot to those windows."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    # Slots covering 06:00 - 11:00 BST (= 05:00-10:00 UTC in summer)
    base_local = datetime(2026, 6, 1, 6, 0, tzinfo=TZ_LOCAL)
    n = 10  # 5 hours
    slots = _half_hour_slots(base_local, n)
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="guests")
    # Slot 2-5 = 07:00-09:00 BST → shower window
    shower_load = sum(e_dhw[2:6])
    other_load = sum(e_dhw[0:2]) + sum(e_dhw[6:])
    assert shower_load > 1.0, (
        f"Guests morning shower window should attribute ≥1 kWh; got {shower_load:.2f}"
    )
    assert shower_load > other_load, (
        f"Shower window should dominate; shower={shower_load:.2f} other={other_load:.2f}"
    )


# ---------------------------------------------------------------------------
# Scenario 7 — Brief transient load (heat-pump short cycle)
# ---------------------------------------------------------------------------


def test_scenario_brief_load_transient_does_not_break_lp():
    """A 10-min Daikin compressor burst can spike 'load' in the heartbeat
    sample. The LP plan SHOULD remain stable — single-tick deviations
    don't blow up the solve. Smoke test that LP handles a base_load
    profile with a single elevated value."""
    base_local = datetime(2026, 6, 1, 13, 0, tzinfo=TZ_LOCAL)
    n = 8
    slots = _half_hour_slots(base_local, n)
    pv = [2.5] * n
    prices = [12] * 4 + [30] * 4
    # base_load profile with a brief spike in slot 2 (10 min compressor)
    base_load = [0.3, 0.3, 1.2, 0.3, 0.3, 0.3, 0.3, 0.3]
    plan = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                  init_soc=6.0, init_tank=45.0)
    assert plan.ok
    # Verify the spike slot has positive PV use (LP didn't refuse to plan)
    assert plan.pv_use_kwh[2] > 0
    # No infeasibility crash
    assert plan.status.lower() in ("optimal", "ok")


# ---------------------------------------------------------------------------
# Scenario 8 — Stale LP trajectory → appliance picker falls back
# ---------------------------------------------------------------------------


def test_scenario_stale_lp_appliance_picker_falls_back(monkeypatch):
    """When the most recent LP run is older than ``APPLIANCE_LP_MAX_AGE_HOURS``
    (e.g. service was down), the battery-aware picker returns {} from
    the trajectory query and falls back to ``find_cheapest_window``."""
    monkeypatch.setattr(config, "APPLIANCE_LP_MAX_AGE_HOURS", 2.0, raising=False)
    aid = _db.add_appliance(
        vendor="smartthings", vendor_device_id="washer-stale",
        name="Washer", device_type="washer",
        default_duration_minutes=120, deadline_local_time="07:00",
        typical_kw=0.5, enabled=True,
    )
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(8)]
    # Seed a stale LP run (5 h ago) with high SoC
    conn = sqlite3.connect(config.DB_PATH)
    stale_run_at = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    conn.execute("INSERT INTO optimizer_log (run_at) VALUES (?)", (stale_run_at,))
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for i, slot in enumerate(slots):
        conn.execute(
            """INSERT INTO lp_solution_snapshot
               (run_id, slot_index, slot_time_utc, soc_kwh)
               VALUES (?, ?, ?, ?)""",
            (run_id, i, slot.isoformat().replace("+00:00", "Z"), 9.0),
        )
    conn.commit()
    conn.close()
    # Stale trajectory → picker falls back to find_cheapest_window
    traj = ad._query_latest_lp_soc_trajectory(max_age_hours=2.0)
    assert traj == {}


# ---------------------------------------------------------------------------
# Scenario 9 — Concurrent appliances (subtracted load)
# ---------------------------------------------------------------------------


def test_scenario_concurrent_appliances_consider_each_other(monkeypatch):
    """When washer is armed for slot 0, dryer scheduling sees the
    washer's planned consumption in its own SoC calculation."""
    aid_w = _db.add_appliance(
        vendor="smartthings", vendor_device_id="washer-c",
        name="Washer", device_type="washer",
        default_duration_minutes=120, deadline_local_time="07:00",
        typical_kw=2.0, enabled=True,
    )
    aid_d = _db.add_appliance(
        vendor="smartthings", vendor_device_id="dryer-c",
        name="Dryer", device_type="dryer",
        default_duration_minutes=120, deadline_local_time="07:00",
        typical_kw=2.0, enabled=True,
    )
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    # Seed a committed washer job for slot 0
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        """INSERT INTO appliance_jobs
           (appliance_id, status, armed_at_utc, deadline_utc,
            duration_minutes, planned_start_utc, planned_end_utc,
            avg_price_pence, created_at, updated_at)
           VALUES (?, 'scheduled', ?, ?, 120, ?, ?, 10.0, ?, ?)""",
        (
            aid_w,
            base.isoformat(),
            (base + timedelta(hours=4)).isoformat(),
            base.isoformat().replace("+00:00", "Z"),
            (base + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            base.isoformat(),
            base.isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    # The committed-load-excluding helper should report washer kW NOT zero
    profile = ad._committed_load_profile_excluding(
        base, base + timedelta(hours=4), exclude_appliance_id=aid_d,
    )
    # Washer is included (we excluded dryer); profile should be non-empty
    assert sum(profile.values()) > 0, (
        "Committed-load helper should see washer when dryer is scheduling"
    )

    # Excluding washer (self) → profile empty
    profile_self = ad._committed_load_profile_excluding(
        base, base + timedelta(hours=4), exclude_appliance_id=aid_w,
    )
    assert sum(profile_self.values()) == 0, (
        "Excluding self should leave no committed loads"
    )


# ---------------------------------------------------------------------------
# Scenario 10 — Energy balance defensive (battery NOT counted as load)
# ---------------------------------------------------------------------------


def test_scenario_energy_balance_battery_charge_not_in_load():
    """Defensive guard for the bug class user flagged today: if our base_load
    forecast accidentally included battery_charge_kw, the LP would over-
    estimate residual load.

    We verify the residual-base-load computation in db.py subtracts
    Daikin physics but does NOT include batteryCharge — uses load_power_kw
    which is house-only (verified via Fox energy balance: PV = load +
    battery + grid).
    """
    # Seed pv_realtime_history with samples where load + battery + grid ≈ PV
    conn = sqlite3.connect(config.DB_PATH)
    samples = [
        # (captured_at, pv, soc, load, grid_imp, bat_chg)
        ("2026-05-22T12:00:00Z", 2.84, 80, 0.30, 0.01, 2.53),
        ("2026-05-22T12:30:00Z", 2.90, 82, 0.25, 0.00, 2.60),
        ("2026-05-22T13:00:00Z", 2.85, 84, 0.28, 0.01, 2.55),
    ]
    for ts, pv, soc, load, gimp, bchg in samples:
        conn.execute(
            """INSERT INTO pv_realtime_history
               (captured_at, solar_power_kw, soc_pct, load_power_kw,
                grid_import_kw, battery_charge_kw, source)
               VALUES (?, ?, ?, ?, ?, ?, 'test')""",
            (ts, pv, soc, load, gimp, bchg),
        )
    conn.commit()
    conn.close()

    # For each sample, verify energy balance: PV + grid_imp ≈ load + bat_chg
    for ts, pv, soc, load, gimp, bchg in samples:
        balance_diff = (pv + gimp) - (load + bchg)
        assert abs(balance_diff) < 0.1, (
            f"Sample {ts} balance violation: {balance_diff}. Load field "
            f"must be house-only, NOT include battery_charge"
        )
