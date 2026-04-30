"""Active-mode safety guardrails: soak budget + circuit breaker + comfort floor.

These exist as a defensive net for flipping ``DAIKIN_CONTROL_MODE=active`` in
production. They're independent of the LP's optimisation; the goal is "if Onecta
misbehaves or the LP picks something stupid, the user's tank can't get cold and
the daily Daikin quota can't run out before noon."
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import api_quota, db
from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries


@pytest.fixture(autouse=True)
def _init_db_and_clear() -> None:
    """Each test starts with a fresh api_call_log + clean runtime_settings."""
    db.init_db()
    api_quota.ensure_table()
    # Wipe call log so consecutive-failure logic starts fresh
    from src.db import _lock, get_connection
    with _lock:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM api_call_log")
            conn.commit()
        finally:
            conn.close()
    db.delete_runtime_setting("daikin_active_mode_started_at")


# ---------------------------------------------------------------------------
# Active-mode soak budget
# ---------------------------------------------------------------------------

def test_passive_mode_uses_full_budget(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "passive")
    monkeypatch.setattr(app_config, "DAIKIN_DAILY_BUDGET", 180)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAILY_BUDGET", 100)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAYS", 3)
    assert api_quota._budget("daikin") == 180


def test_active_mode_first_write_starts_soak(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(app_config, "DAIKIN_DAILY_BUDGET", 180)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAILY_BUDGET", 100)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAYS", 3)

    # No marker yet — budget is full (180); cap activates only after the first
    # active-mode write. Avoids capping budgets during fixture setup or pre-flip.
    assert api_quota._budget("daikin") == 180
    assert db.get_runtime_setting("daikin_active_mode_started_at") is None

    # First write records the start timestamp; budget then collapses to soak cap.
    api_quota.record_call("daikin", kind="write", ok=True)
    started = db.get_runtime_setting("daikin_active_mode_started_at")
    assert started is not None
    assert abs(time.time() - float(started)) < 10
    assert api_quota._budget("daikin") == 100


def test_active_mode_after_soak_window_uses_full_budget(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(app_config, "DAIKIN_DAILY_BUDGET", 180)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAILY_BUDGET", 100)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAYS", 3)

    # Plant a marker that's older than the soak window
    long_ago = time.time() - (4 * 86400)
    db.set_runtime_setting("daikin_active_mode_started_at", str(long_ago))
    assert api_quota._budget("daikin") == 180  # Soak expired


def test_flipping_back_to_passive_clears_soak_marker(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setattr(app_config, "DAIKIN_DAILY_BUDGET", 180)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAILY_BUDGET", 100)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAYS", 3)
    api_quota.record_call("daikin", kind="write", ok=True)
    assert db.get_runtime_setting("daikin_active_mode_started_at") is not None

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "passive")
    api_quota.record_call("daikin", kind="write", ok=True)  # Even a "passive write" — actor=api fall-through
    assert db.get_runtime_setting("daikin_active_mode_started_at") is None


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def test_circuit_breaker_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_FAILS", 0)
    api_quota.record_call("daikin", "write", ok=False)
    api_quota.record_call("daikin", "write", ok=False)
    api_quota.record_call("daikin", "write", ok=False)
    assert not api_quota.daikin_circuit_open()


def test_circuit_breaker_opens_after_three_consecutive_fails(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_FAILS", 3)
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_WINDOW_MINUTES", 15)
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_COOLDOWN_MINUTES", 30)

    for _ in range(3):
        api_quota.record_call("daikin", "write", ok=False)
    assert api_quota.daikin_circuit_open()


def test_success_resets_consecutive_streak(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_FAILS", 3)
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_WINDOW_MINUTES", 15)

    api_quota.record_call("daikin", "write", ok=False)
    api_quota.record_call("daikin", "write", ok=False)
    api_quota.record_call("daikin", "write", ok=True)   # success → streak broken
    api_quota.record_call("daikin", "write", ok=False)  # only 1 consecutive fail now
    assert not api_quota.daikin_circuit_open()


def test_should_block_returns_true_when_breaker_open(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_DAILY_BUDGET", 180)
    monkeypatch.setattr(app_config, "DAIKIN_ACTIVE_SOAK_DAILY_BUDGET", 0)
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_FAILS", 3)
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_WINDOW_MINUTES", 15)
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_COOLDOWN_MINUTES", 30)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "passive")
    for _ in range(3):
        api_quota.record_call("daikin", "write", ok=False)
    assert api_quota.should_block("daikin")  # Quota fine but breaker open


def test_breaker_does_not_open_outside_window(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_FAILS", 3)
    monkeypatch.setattr(app_config, "DAIKIN_CIRCUIT_BREAKER_WINDOW_MINUTES", 5)

    # Insert three failures spread over an hour (outside the 5-min window)
    from src.db import _lock, get_connection
    now = time.time()
    with _lock:
        conn = get_connection()
        try:
            for offset in (0, 1800, 3600):  # 0, 30, 60 min ago
                conn.execute(
                    "INSERT INTO api_call_log (vendor, kind, ts_utc, ok) VALUES (?, ?, ?, ?)",
                    ("daikin", "write", now - offset, 0),
                )
            conn.commit()
        finally:
            conn.close()
    assert not api_quota.daikin_circuit_open()


# ---------------------------------------------------------------------------
# Comfort-floor invariant — LP cannot drive tank or indoor below the floor
# ---------------------------------------------------------------------------

def test_lp_respects_tank_floor_under_extreme_prices(monkeypatch):
    """Build a scenario with very high import prices that would economically
    favour zero DHW heating, and verify the LP keeps tank ≥ tank_lo (20 °C
    hardcoded) and indoor temp ≥ overnight floor.

    This protects against a future config bug or pricing combination silently
    relaxing the comfort floor — should be impossible by construction (LP
    variable bounds), but a regression test pins it."""
    monkeypatch.setattr(app_config, "LP_HIGHS_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)

    base = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)  # Mild spring afternoon
    n = 12  # 6h horizon — long enough for heat-loss to bite, short enough to stay feasible
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[8.0] * n,    # cool — Daikin will fire
        shortwave_radiation_wm2=[100.0] * n,
        cloud_cover_pct=[60.0] * n,
        pv_kwh_per_slot=[0.0] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    prices = [60.0] * n  # Expensive imports — LP wants to minimise DHW heating
    base_load = [0.3] * n
    st = LpInitialState(soc_kwh=8.0, tank_temp_c=45.0, indoor_temp_c=21.0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=st,
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, f"LP did not solve: {plan.status}"
    # Hard-coded tank_lo in lp_optimizer.py; LP variable bounds prevent below.
    assert all(t >= 20.0 - 1e-6 for t in plan.tank_temp_c), (
        f"Tank fell below 20 °C floor: min={min(plan.tank_temp_c):.2f}"
    )
    # Indoor floor is 10 °C (variable bound in lp_optimizer.py); also assert no
    # extreme dip even at 10 (catch a regression where comfort_pen disappears).
    assert all(t >= 10.0 - 1e-6 for t in plan.indoor_temp_c), (
        f"Indoor fell below 10 °C bound: min={min(plan.indoor_temp_c):.2f}"
    )
