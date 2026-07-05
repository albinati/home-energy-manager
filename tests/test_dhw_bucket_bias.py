"""DHW bucket-bias corrector: learning from dhw_error_log, total-preserving
application in forecast_dhw_load_per_slot, and the K2-pin Infeasible
regression (the corrected forecast feeds a hard LP equality — every extreme
factor combination must still solve Optimal).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db, dhw_bias, dhw_policy
from src.config import config

TZ_LOCAL = ZoneInfo("Europe/London")


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(db, "_db_path", lambda: path)
    db.init_db()
    return path


@pytest.fixture(autouse=True)
def _bias_config(monkeypatch):
    """Deterministic corrector config + no autoscale/legionella interference."""
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_WINDOW_DAYS", 14, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_HALFLIFE_DAYS", 0.0, raising=False)  # flat weights
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_DAMPING", 0.5, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MIN", 0.25, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MAX", 3.0, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MIN_FORECAST_KWH", 0.05, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MIN_DAYS", 3, raising=False)
    monkeypatch.setattr(config, "DHW_FORECAST_AUTOSCALE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13, raising=False)
    monkeypatch.setattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22, raising=False)
    dhw_policy._autoscale_cache.clear()
    yield
    dhw_policy._autoscale_cache.clear()


def _seed_error_rows(path, rows):
    """rows: iterable of (day: date, bucket: int, forecast, actual)."""
    conn = sqlite3.connect(path)
    for d, b, f, a in rows:
        err = (a - f) if (a is not None and f is not None) else None
        conn.execute(
            "INSERT OR REPLACE INTO dhw_error_log"
            " (day, bucket_idx, forecast_kwh, actual_kwh, error_kwh, built_at_utc)"
            " VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
            (d.isoformat(), b, f, a, err),
        )
    conn.commit()
    conn.close()


def _recent_days(n):
    today = datetime.now(UTC).date()
    return [today - timedelta(days=i + 1) for i in range(n)]


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


def test_ratio_recovery_and_warm_start(tmp_db):
    days = _recent_days(4)
    # bucket 6: consistent 4x over-forecast (actual/forecast = 0.25 exactly at clamp)
    # bucket 3: consistent 2x under-forecast
    _seed_error_rows(tmp_db, [(d, 6, 2.0, 0.5) for d in days])
    _seed_error_rows(tmp_db, [(d, 3, 0.2, 0.4) for d in days])
    applied, raw, samples, _ = dhw_bias.compute_dhw_bucket_bias()
    assert raw[6] == pytest.approx(0.25)
    assert raw[3] == pytest.approx(2.0)
    # warm start: applied == raw on a cold table
    assert applied[6] == pytest.approx(0.25)
    assert applied[3] == pytest.approx(2.0)
    assert samples[6] == 4


def test_damped_accumulation_on_second_refresh(tmp_db):
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 6, 2.0, 1.0) for d in days])  # ratio 0.5
    assert dhw_bias.refresh_dhw_bucket_bias() == 1
    assert db.get_dhw_bucket_bias()[6] == pytest.approx(0.5)
    # Second refresh, same residual ratio: prev × (1 + 0.5·(0.5 − 1)) = 0.5 × 0.75
    assert dhw_bias.refresh_dhw_bucket_bias() == 1
    assert db.get_dhw_bucket_bias()[6] == pytest.approx(0.375)


def test_min_days_gate_and_clamp(tmp_db):
    days = _recent_days(4)
    # bucket 2: only 2 distinct days → gated out (min_days=3)
    _seed_error_rows(tmp_db, [(d, 2, 1.0, 0.1) for d in days[:2]])
    # bucket 5: 10x under-forecast → clamped to MAX 3.0
    _seed_error_rows(tmp_db, [(d, 5, 0.1, 1.0) for d in days])
    applied, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert 2 not in applied
    assert applied[5] == pytest.approx(3.0)


def test_zero_actual_rows_are_learned_from(tmp_db):
    """actual≈0 against a real forecast IS the over-forecast signal — the
    asymmetric filter must keep those rows (a PV-style symmetric min-kwh
    filter would silently discard exactly what we're fixing)."""
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 7, 1.5, 0.0) for d in days])
    applied, raw, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert raw[7] == pytest.approx(0.25)  # 0/1.5 clamped up to MIN
    assert applied[7] == pytest.approx(0.25)


def test_tiny_forecast_rows_are_excluded(tmp_db):
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 8, 0.01, 0.5) for d in days])  # denominator < 0.05
    applied, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert 8 not in applied


def test_boost_window_decontamination(tmp_db):
    """(day, bucket) pairs under a tank_negative_boost window are excluded."""
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 6, 1.0, 0.5) for d in days])
    # Boost windows covering bucket 6 (12:00-14:00 UTC==local) on 3 of 4 days
    conn = sqlite3.connect(tmp_db)
    for d in days[:3]:
        ws = datetime.combine(d, time(12, 0), tzinfo=UTC)
        conn.execute(
            "INSERT INTO action_schedule (date, start_time, end_time, device,"
            " action_type, status, created_at) VALUES (?, ?, ?, 'daikin',"
            " 'tank_negative_boost', 'completed', '2026-01-01T00:00:00Z')",
            (d.isoformat(), ws.isoformat(), (ws + timedelta(hours=2)).isoformat()),
        )
    conn.commit()
    conn.close()
    applied, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    # Only 1 clean day survives < min_days 3 → bucket gated out entirely
    assert 6 not in applied


def test_legionella_bucket_decontamination(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 12, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", 0, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120, raising=False)
    days = _recent_days(8)
    dow = days[0].weekday()  # make "today-1" the legionella day
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", dow, raising=False)
    # bucket 6 (12:00-14:00) seeded every day; the legionella-DOW day carries a
    # poisoned actual (firmware cycle) that must not enter the ratio
    for d in days:
        poisoned = 5.0 if d.weekday() == dow else 0.5
        _seed_error_rows(tmp_db, [(d, 6, 1.0, poisoned)])
    applied, raw, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert raw[6] == pytest.approx(0.5)  # clean days only


def test_normalization_invariant():
    """Σ share·normalized_factor == 1 for the mode's nominal shares, and the
    returned dict is complete (unlearned buckets carry the renormalization)."""
    factors = {4: 0.3, 5: 0.3, 6: 0.4, 7: 0.5, 0: 1.2}
    for mode in ("normal", "guests"):
        norm = dhw_bias.normalized_factors(factors, mode)
        assert len(norm) == 12
        shares = dhw_policy._nominal_bucket_shares(mode)
        assert sum(s * norm[b] for b, s in shares.items()) == pytest.approx(1.0)


def test_nominal_bucket_shares_sum_to_one():
    for mode in ("normal", "guests"):
        shares = dhw_policy._nominal_bucket_shares(mode)
        assert sum(shares.values()) == pytest.approx(1.0)
    assert dhw_policy._nominal_bucket_shares("vacation") == {}


# ---------------------------------------------------------------------------
# Application in forecast_dhw_load_per_slot
# ---------------------------------------------------------------------------


def _day_slots(day_local: date, tz=UTC):
    # The autouse fixture pins BULLETPROOF_TIMEZONE=UTC, so UTC==local here.
    start = datetime.combine(day_local, time(0, 0), tzinfo=tz)
    return [(start + timedelta(minutes=30 * i)).astimezone(UTC) for i in range(48)]


def _seed_factors(path, factors):
    conn = sqlite3.connect(path)
    for b, f in factors.items():
        conn.execute(
            "INSERT OR REPLACE INTO dhw_bucket_bias"
            " (bucket_idx, factor, raw_ratio, samples, days, computed_at)"
            " VALUES (?, ?, ?, 4, 4, '2026-01-01T00:00:00Z')",
            (b, f, f),
        )
    conn.commit()
    conn.close()


def test_application_preserves_daily_total_and_reshapes(tmp_db, monkeypatch):
    slots = _day_slots(date(2026, 7, 10))
    base_e, base_tank = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    # Shrink the warmup-afternoon buckets, as prod data says we should
    _seed_factors(tmp_db, {6: 0.3, 7: 0.3})
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    e, tank = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert sum(e) == pytest.approx(sum(base_e), abs=1e-6)  # total preserved
    assert e != base_e  # shape changed
    # the shrunk buckets went down; everything else got the normalization boost
    for v, bv, s in zip(e, base_e, slots):
        b = s.astimezone(UTC).hour // 2  # BULLETPROOF_TIMEZONE=UTC in tests
        if b in (6, 7):
            assert v < bv
        else:
            assert v > bv
    assert tank == base_tank  # temperatures are comfort targets — untouched


def test_application_disabled_and_vacation_are_noops(tmp_db, monkeypatch):
    slots = _day_slots(date(2026, 7, 10))
    base_e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    _seed_factors(tmp_db, {6: 0.3})
    # disabled (default) → byte-identical
    e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert e == base_e
    # vacation → all zeros regardless
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    ev, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="vacation")
    assert all(v == 0.0 for v in ev)


def test_negative_boost_slots_unaffected_by_bias(tmp_db, monkeypatch):
    """The boost ramp overwrites its slots AFTER the bias application point —
    deliberate max-heating is never scaled."""
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0, raising=False)
    slots = _day_slots(date(2026, 7, 10))
    prices = [10.0] * 48
    for i in range(20, 26):  # negative window 10:00-13:00 local
        prices[i] = -3.0
    base_e, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", price_line=prices, initial_tank_c=40.0
    )
    _seed_factors(tmp_db, {b: 0.3 for b in range(12)})
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    e, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", price_line=prices, initial_tank_c=40.0
    )
    for i in range(20, 26):
        assert e[i] == pytest.approx(base_e[i])


def test_extreme_factors_stay_below_heater_cap(tmp_db, monkeypatch):
    """RISK 1 regression: bias × autoscale can exceed the heater's per-slot
    capacity; the clamp AFTER the bias application must keep every pinned
    value ≤ DAIKIN_MAX_HP_KW × 0.5."""
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 1.0, raising=False)  # small heater
    _seed_factors(tmp_db, {b: 3.0 for b in range(12)})
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    slots = _day_slots(date(2026, 7, 10))
    e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert all(v <= 0.5 + 1e-9 for v in e)


# ---------------------------------------------------------------------------
# K2 pin — the corrected forecast must never make the LP Infeasible
# ---------------------------------------------------------------------------


def _make_weather(slots, pv_kwh, temp_c=18.0):
    from src.weather import WeatherLpSeries
    n = len(slots)
    return WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[temp_c] * n,
        shortwave_radiation_wm2=[600.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=pv_kwh,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )


def _solve(slots, prices, *, temp_c=18.0, init_tank=40.0):
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    init = LpInitialState(soc_kwh=8.0, tank_temp_c=init_tank)
    return solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=[0.25] * len(slots),
        weather=_make_weather(slots, [0.1] * len(slots), temp_c=temp_c),
        initial=init,
        tz=TZ_LOCAL,
    )


@pytest.mark.parametrize("factor", [0.25, 3.0])
@pytest.mark.parametrize("mode", ["normal", "guests"])
def test_k2_pin_stays_optimal_under_extreme_factors(tmp_db, monkeypatch, factor, mode):
    """Full solve with pinning on, extreme factors, a small heater, a negative
    window mid-day, and the horizon ending INSIDE the evening shower window
    (the #422 Infeasible shape)."""
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", mode, raising=False)
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 1.0, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    _seed_factors(tmp_db, {b: factor for b in range(12)})

    # 08:00 local → 21:30 local (ends inside the 20:00-22:00 shower window)
    start = datetime(2026, 7, 10, 8, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [start + timedelta(minutes=30 * i) for i in range(28)]
    prices = [12.0] * 28
    for i in range(6, 12):  # negative window 11:00-14:00 local
        prices[i] = -4.0
    plan = _solve(slots, prices, init_tank=38.0)
    assert plan is not None and plan.status == "Optimal"

    # pinned equality holds: plan e_dhw == forecast with bias applied
    e_fc, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode=mode, initial_tank_c=38.0, price_line=prices
    )
    for got, want in zip(plan.dhw_electric_kwh, e_fc):
        assert got == pytest.approx(want, abs=1e-6)


def test_k2_pin_optimal_with_active_space_floor(tmp_db, monkeypatch):
    """RISK 2 regression: the climate-curve space floor (e_space+e_dhw >=
    floor) is NOT pin-gated. A down-scaled pinned e_dhw pushes required
    e_space up — must still solve on a cold day."""
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    _seed_factors(tmp_db, {b: 0.25 for b in range(12)})

    start = datetime(2026, 1, 15, 0, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [start + timedelta(minutes=30 * i) for i in range(48)]
    plan = _solve(slots, [15.0] * 48, temp_c=-2.0, init_tank=40.0)
    assert plan is not None and plan.status == "Optimal"


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def test_refresh_on_empty_table_is_quiet_noop(tmp_db):
    assert dhw_bias.refresh_dhw_bucket_bias() == 0
    assert db.get_dhw_bucket_bias() == {}


def test_backtest_shape(tmp_db):
    days = _recent_days(8)
    _seed_error_rows(tmp_db, [(d, 6, 2.0, 0.5) for d in days])
    _seed_error_rows(tmp_db, [(d, 3, 0.2, 0.4) for d in days])
    out = dhw_bias.backtest_dhw_bucket_bias(14)
    assert out["in_sample"] is not None
    assert out["in_sample"]["mae_reduction_kwh"] > 0
    assert out["out_of_sample"] is not None
    assert "factor_by_bucket" in out


def test_budget_state_surfaces_bias(tmp_db, monkeypatch):
    _seed_factors(tmp_db, {6: 0.3})
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    state = dhw_policy.dhw_budget_state("normal")
    assert state["bucket_bias_enabled"] is True
    assert state["bucket_bias_factors"]["6"] == pytest.approx(0.3)
    assert "6" in state["bucket_bias_normalized"]
