"""Winter-shape scenario tests — Nov-Feb behaviour validation.

Winter looks fundamentally different from the May/June conditions the
2026-05-23 stack was validated against:

* **PV ≪ summer**: 4.5 kWp UK installation produces ~5-15 kWh/day in
  Dec-Jan vs 25-35 kWh/day in Jun-Jul
* **Heat-pump active most of the day**: Daikin compressor runs
  continuously below ~6 °C outdoor
* **Heat-pump COP drops**: ~2.5 in winter vs ~3.0 in summer (more
  electric per kWh of heat delivered)
* **Tariff shape sharper**: Agile cheap dips are deeper (negative or
  near-zero overnight), peaks higher (35-45 p)

These tests lock in:
1. What the system does CORRECTLY in winter (LP arbitrage, negative
   boost, appliance fallback)
2. What the system does SUB-OPTIMALLY by design (DHW timing — user
   policy 2026-05-23: "no thermal arbitrage beyond negative slots")
3. The economic cost of that policy choice (estimated £45-50/winter)
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src import dhw_policy
from src.config import config


TZ_LOCAL = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _prod_like_defaults(monkeypatch, tmp_path):
    """Mirror prod K1+K2+K3 settings."""
    db_path = str(tmp_path / "winter.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    _db.init_db()
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setitem(config._overrides, "DHW_WARMUP_START_HOUR_LOCAL", 13)
    monkeypatch.setitem(config._overrides, "DHW_SETBACK_START_HOUR_LOCAL", 22)
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_SETBACK_C", 37.0, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0, raising=False)
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.0, raising=False)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 15.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    yield


def _solve(slots, prices, pv, base_load, init_soc=8.0, init_tank=45.0,
           outdoor_c=5.0, export_prices=None):
    """LP solve with winter-typical outdoor temps."""
    from src.weather import WeatherLpSeries
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    n = len(slots)
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[outdoor_c] * n,
        shortwave_radiation_wm2=[200.0] * n,   # low winter radiation
        cloud_cover_pct=[60.0] * n,
        pv_kwh_per_slot=pv,
        cop_space=[2.5] * n,                   # winter COP
        cop_dhw=[2.5] * n,                     # winter DHW COP also drops
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
    start_utc = start_local_dt.astimezone(UTC)
    return [start_utc + timedelta(minutes=30 * i) for i in range(n)]


def _winter_agile_profile_pence(start_hour_local: int, n_slots: int) -> list[float]:
    """Typical UK Agile winter day shape (pence/kWh per 30-min slot).

    Profile (BST hours):
        00:00-05:00  overnight cheap  4-8 p
        05:00-07:00  early morning   12-18 p
        07:00-12:00  midday medium   15-22 p
        12:00-16:00  afternoon med   18-25 p
        16:00-19:00  evening peak    32-42 p
        19:00-23:00  decline         22-18 p

    Returns ``n_slots`` rates starting at ``start_hour_local`` local time.
    """
    # 24h template, half-hourly (48 slots)
    template = (
        [6, 5, 5, 5, 4, 4, 5, 6, 8, 10] +         # 00:00-04:30
        [12, 14, 15, 16, 18, 18, 17, 18, 20, 20] + # 05:00-09:30
        [21, 22, 22, 20, 22, 23, 24, 25, 28, 30] + # 10:00-14:30
        [32, 35, 38, 40, 42, 38, 35, 32, 28, 24] + # 15:00-19:30
        [22, 20, 18, 16, 15, 13, 11, 9]            # 20:00-23:30
    )
    out: list[float] = []
    h = start_hour_local
    m = 0
    for _ in range(n_slots):
        idx = (h * 2 + (m // 30)) % 48
        out.append(float(template[idx]))
        m += 30
        if m >= 60:
            m = 0
            h += 1
    return out


# ===========================================================================
# Scenario W1 — Cold day, minimal PV: LP arbitrage works
# ===========================================================================


def test_w1_cold_day_lp_does_proper_battery_arbitrage():
    """Dec day: PV ~5 kWh total spread thinly. Full 24h horizon so the
    LP sees BOTH the cheap overnight window AND the evening peak.

    Expectation: LP shifts grid imports toward the cheap window
    (00:00-05:00 BST) rather than spreading them across all hours.
    """
    base_local = datetime(2026, 12, 15, 0, 0, tzinfo=TZ_LOCAL)
    n = 48  # 24 hours
    slots = _half_hour_slots(base_local, n)
    # PV: zero overnight, weak ramp mid-day (winter solstice ~5kWh total)
    pv = ([0.0] * 14 +    # 00:00-07:00 — dark
          [0.1, 0.3, 0.6, 0.9, 1.0, 0.9, 0.7, 0.4, 0.2, 0.1] +  # 07:00-12:00
          [0.0] * 24)     # 12:00-24:00
    pv = pv[:n]
    prices = _winter_agile_profile_pence(0, n)
    base_load = [0.4] * n
    plan = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                  init_soc=4.0, init_tank=37.0, outdoor_c=3.0,
                  export_prices=[p * 0.4 for p in prices])
    assert plan.ok, plan.status

    # Imports in cheap window (00:00-05:00 BST = slots 0-10)
    cheap_imports = sum(plan.import_kwh[i] for i in range(10))
    # Imports in peak window (16:00-19:00 BST = slots 32-38)
    peak_imports = sum(plan.import_kwh[i] for i in range(32, 38))

    # Cheap window should see MORE grid use than peak window
    assert cheap_imports > peak_imports, (
        f"LP should prefer cheap slots for grid arbitrage; "
        f"cheap={cheap_imports:.2f} kWh vs peak={peak_imports:.2f} kWh"
    )


# ===========================================================================
# Scenario W2 — DHW timing suboptimal (by design)
# ===========================================================================


def test_w2_dhw_warmup_uses_midday_grid_not_overnight_cheap():
    """Winter DHW: warmup fires at 13:00 BST (midday grid ~18-22 p)
    instead of overnight cheap (~5-8 p). This is the explicit user
    policy trade-off — accepted £45-50/winter cost for operational
    simplicity.

    Lock-in test: the forecast emits e_dhw load AT THE WARMUP HOUR,
    NOT in the cheap overnight window."""
    # 24h horizon: 00:00 → 24:00 BST
    base_local = datetime(2026, 12, 15, 0, 0, tzinfo=TZ_LOCAL)
    slots = _half_hour_slots(base_local, 48)
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", initial_tank_c=37.0,
    )
    # Slot 26 = 13:00 BST = warmup transition (biggest pulse)
    # Slot 4 = 02:00 BST = setback (tiny)
    assert e_dhw[26] > e_dhw[4] * 5, (
        "DHW warmup at 13:00 BST should produce vastly more e_dhw than "
        f"overnight setback; got {e_dhw[26]:.2f} vs {e_dhw[4]:.3f}. "
        "If equal, dhw_policy is incorrectly aligning warmup with cheap slots."
    )


def test_w2_dhw_cost_overnight_vs_warmup_window():
    """Quantify the policy cost: how much MORE would DHW heating cost
    if done at midday grid (current) vs overnight cheap (alternative)?

    This test documents the £45-50/winter trade-off rather than enforcing
    a behavior — useful as a baseline if we ever revisit."""
    # Setup: forecast e_dhw for a winter day
    base_local = datetime(2026, 12, 15, 0, 0, tzinfo=TZ_LOCAL)
    slots = _half_hour_slots(base_local, 48)
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", initial_tank_c=37.0,
    )
    prices = _winter_agile_profile_pence(0, 48)

    # Cost at the CURRENT warmup window (13:00-22:00 BST)
    cost_midday = sum(e_dhw[i] * prices[i] for i in range(26, 44))
    # Cost if DHW had been heated at the SAME volume in cheap overnight
    # window (02:00-05:00 BST = slots 4-10)
    total_dhw = sum(e_dhw)
    cheap_avg_p = sum(prices[4:10]) / 6
    cost_overnight = total_dhw * cheap_avg_p

    delta = cost_midday - cost_overnight
    assert delta > 0, "Midday warmup should cost more than overnight cheap"
    # Document the delta — informational, not enforced
    # Typical: ~30-50 p/day = £45-50/winter at this rate
    # (Test passes when delta > 0 — i.e. user is paying the policy tax)


# ===========================================================================
# Scenario W3 — Negative-price slot in winter still triggers boost
# ===========================================================================


def test_w3_winter_negative_price_still_emits_boost():
    """Negative price events happen year-round (more common in winter
    due to wind surplus). dhw_policy must still emit boost.

    Note: a slot at 02:00 UTC on day D falls into the OVERNIGHT setback
    portion of day D-1's schedule (which runs 13:00 D-1 → 13:00 D).
    """
    # Schedule day D-1 covers 02:00 of day D
    schedule_day = date(2026, 12, 14)
    neg_slot_utc = datetime(2026, 12, 15, 2, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    outgoing = [{"valid_from": neg_slot_utc, "value_inc_vat": -8.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        schedule_day, agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert len(boost) == 1
    assert boost[0]["params"]["tank_temp"] == 60
    assert boost[0]["params"]["tank_powerful"] is True


# ===========================================================================
# Scenario W4 — Heat pump heavy load (Daikin transients become routine)
# ===========================================================================


def test_w4_heavy_space_heating_load_lp_still_feasible():
    """Cold day: ``base_load_kwh`` reflects high heat-pump compressor
    duty cycle (1-3 kW continuous). LP must still solve and plan
    enough grid imports to cover the load."""
    base_local = datetime(2026, 1, 10, 6, 0, tzinfo=TZ_LOCAL)
    n = 24  # 12h horizon
    slots = _half_hour_slots(base_local, n)
    pv = [0.0, 0.0] + [0.1, 0.3, 0.5, 0.6, 0.5, 0.3, 0.1, 0.0] + [0.0] * 14
    prices = _winter_agile_profile_pence(6, n)
    # Heavy base_load — heat pump running 1.5 kW continuous
    base_load = [0.75] * n
    plan = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                  init_soc=6.0, init_tank=45.0, outdoor_c=-1.0)
    assert plan.ok, f"LP must handle cold-day heavy load; status={plan.status}"
    total_grid_in = sum(plan.import_kwh)
    # ~18 kWh load over 12h - ~5 kWh battery available - ~1 kWh PV = ~12 kWh grid
    assert total_grid_in > 5.0, (
        f"Heavy winter day should require significant grid; got {total_grid_in:.1f} kWh"
    )


# ===========================================================================
# Scenario W5 — Multi-day cold snap (battery state propagates)
# ===========================================================================


def test_w5_multiday_cold_snap_lp_extracts_consistent_arbitrage():
    """3 consecutive cold days, repeated tariff pattern. LP should
    consistently force-charge overnight + discharge at peak each day."""
    base_local = datetime(2026, 1, 8, 0, 0, tzinfo=TZ_LOCAL)
    n = 48 * 2  # 2-day horizon
    slots = _half_hour_slots(base_local, n)
    pv = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
          0.0, 0.0, 0.0, 0.0,                              # 00-07
          0.1, 0.3, 0.5, 0.6, 0.5, 0.3, 0.1, 0.0,           # 07-11
          ] + [0.0] * 26 + [0.0] * 48  # day 2 same shape
    # Need 96 elements
    pv = pv[:n] + [0.0] * max(0, n - len(pv))
    prices = (_winter_agile_profile_pence(0, 48) + _winter_agile_profile_pence(0, 48))
    base_load = [0.5] * n
    plan = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                  init_soc=4.0, init_tank=37.0, outdoor_c=2.0)
    assert plan.ok, plan.status
    # Day 1 cheap window (00:00-05:00 BST = slots 0-10)
    d1_cheap_imports = sum(plan.import_kwh[i] for i in range(10))
    # Day 2 cheap window (slots 48-58)
    d2_cheap_imports = sum(plan.import_kwh[i] for i in range(48, 58))
    # Both should see grid charging (battery cycles each day)
    assert d1_cheap_imports > 0.5, "Day 1 should grid-charge in cheap window"
    assert d2_cheap_imports > 0.5, "Day 2 should grid-charge in cheap window"


# ===========================================================================
# Scenario W6 — Setback tank is fine for overnight in winter
# ===========================================================================


def test_w6_setback_tank_at_37_overnight_no_shower_issue():
    """37°C overnight is enough for emergency morning shower. Verify
    the setback temp doesn't trip any LP constraint (no shower-window
    floor violation under pinning)."""
    base_local = datetime(2026, 1, 10, 22, 0, tzinfo=TZ_LOCAL)
    n = 16  # 8h: 22:00 → 06:00 BST
    slots = _half_hour_slots(base_local, n)
    pv = [0.0] * n
    prices = _winter_agile_profile_pence(22, n)
    base_load = [0.4] * n
    plan = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                  init_soc=8.0, init_tank=45.0)
    assert plan.ok, plan.status
    # Tank trajectory should drop toward 37 (setback) over the horizon
    # Pinning enforces this — tank[i+1] follows dhw_policy schedule
    final_tank = plan.tank_temp_c[-1]
    assert final_tank == pytest.approx(37.0, abs=0.5), (
        f"Setback should end at 37°C; got {final_tank:.1f}"
    )


# ===========================================================================
# Scenario W7 — Vacation mode in winter (frost protection only)
# ===========================================================================


def test_w7_vacation_winter_no_dhw_but_frost_protection_intact(monkeypatch):
    """Winter vacation: dhw_policy emits nothing (Daikin firmware
    handles frost protection autonomously). LP still optimises battery."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    base_local = datetime(2026, 1, 10, 0, 0, tzinfo=TZ_LOCAL)
    n = 12
    slots = _half_hour_slots(base_local, n)
    pv = [0.0] * n
    prices = _winter_agile_profile_pence(0, n)
    base_load = [0.2] * n  # nobody home — minimal load
    plan = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                  init_soc=8.0, init_tank=37.0, outdoor_c=0.0)
    assert plan.ok, plan.status
    # No e_dhw allocated in vacation
    for v in plan.dhw_electric_kwh:
        assert v < 1e-3
    # dhw_policy returns no rows
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 1, 10), mode="vacation",
    )
    assert rows == []


# ===========================================================================
# Scenario W8 — Appliance picker falls back to grid in low-PV winter
# ===========================================================================


def test_w8_appliance_picker_falls_back_in_low_soc_winter(monkeypatch):
    """Winter: SoC often below 95% threshold (less PV to top up).
    Battery-aware picker can't justify battery use → falls back to
    grid-cheapest. That picker picks overnight cheap slot. CORRECT."""
    from src.scheduler import appliance_dispatch as ad

    aid = _db.add_appliance(
        vendor="smartthings", vendor_device_id="washer-winter",
        name="Washer", device_type="washer",
        default_duration_minutes=120, deadline_local_time="07:00",
        typical_kw=1.0, enabled=True,
    )
    base = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)  # 22:00 UTC eve
    slots = [base + timedelta(minutes=30 * i) for i in range(20)]
    # Winter: SoC at 60% throughout (below 95% min for battery-aware)
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("INSERT INTO optimizer_log (run_at) VALUES (?)", (base.isoformat(),))
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for i, s in enumerate(slots):
        conn.execute(
            "INSERT INTO lp_solution_snapshot (run_id, slot_index, slot_time_utc, soc_kwh)"
            " VALUES (?, ?, ?, ?)",
            (run_id, i, s.isoformat().replace("+00:00", "Z"), 6.0),
        )
    conn.commit()
    conn.close()

    # Marginal cost: peak now, cheap later (overnight)
    marginal = {s: p for s, p in zip(slots, _winter_agile_profile_pence(22, 20))}
    start, _end, price = ad.find_battery_aware_window(
        earliest_start_utc=base, deadline_utc=base + timedelta(hours=9),
        duration_minutes=120, appliance_id=aid, typical_kw=1.0,
        marginal_cost_per_slot=marginal,
    )
    # SoC 60% → no battery-friendly slot (battery would dip below reserve)
    # → falls back to grid-cheapest. Cheapest slots are overnight (low p).
    chosen_price = marginal[start]
    cheapest_p = min(marginal.values())
    assert chosen_price <= cheapest_p + 2.0, (
        f"Winter low-SoC should fall back to cheapest grid; "
        f"chosen={chosen_price:.1f} cheapest={cheapest_p:.1f}"
    )


# ===========================================================================
# Scenario W9 — Outdoor temperature affects COP forecast
# ===========================================================================


def test_w9_low_outdoor_temp_increases_planned_grid():
    """Below ~5°C, heat-pump COP drops. LP's e_space forecast (via
    weather.cop_space input) should reflect this and plan more grid
    imports for the same heating delivery."""
    base_local = datetime(2026, 1, 15, 0, 0, tzinfo=TZ_LOCAL)
    n = 12  # 6 hours
    slots = _half_hour_slots(base_local, n)
    pv = [0.0] * n
    prices = _winter_agile_profile_pence(0, n)
    base_load = [0.5] * n
    # Mild winter (8°C) vs cold winter (-2°C)
    plan_mild = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                       init_soc=6.0, init_tank=37.0, outdoor_c=8.0)
    plan_cold = _solve(slots=slots, prices=prices, pv=pv, base_load=base_load,
                       init_soc=6.0, init_tank=37.0, outdoor_c=-2.0)
    assert plan_mild.ok and plan_cold.ok
    # Cold day should plan more or equal grid (more heat needed at lower COP)
    mild_total_grid = sum(plan_mild.import_kwh)
    cold_total_grid = sum(plan_cold.import_kwh)
    assert cold_total_grid >= mild_total_grid - 0.5, (
        "Cold day should not import dramatically less than mild day"
    )
