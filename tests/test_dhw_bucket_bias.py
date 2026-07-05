"""DHW bucket-bias corrector: OPEN-LOOP learning from dhw_error_log,
total-preserving normal-mode application in forecast_dhw_load_per_slot, and
the K2-pin Infeasible regressions with SKEWED factor tables (uniform factors
normalize to exactly 1.0 — a uniform-table "extreme" test is vacuous; caught
in adversarial review).

The open-loop regressions at the top are the load-bearing ones: the first cut
of this corrector used the PV-style damped accumulation and compounded to the
clamps while disabled. Idempotence + disabled-stability pin the fix.
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
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MIN", 0.25, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MAX", 3.0, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MIN_FORECAST_KWH", 0.05, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MIN_DAYS", 3, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_MAX_AGE_DAYS", 7, raising=False)
    monkeypatch.setattr(config, "DHW_FORECAST_AUTOSCALE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13, raising=False)
    monkeypatch.setattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22, raising=False)
    dhw_policy._autoscale_cache.clear()
    yield
    dhw_policy._autoscale_cache.clear()


def _seed_error_rows(path, rows):
    """rows: iterable of (day, bucket, forecast, actual[, applied_factor[, mode]])."""
    conn = sqlite3.connect(path)
    for row in rows:
        d, b, f, a = row[0], row[1], row[2], row[3]
        applied = row[4] if len(row) > 4 else 1.0
        mode = row[5] if len(row) > 5 else "normal"
        err = (a - f) if (a is not None and f is not None) else None
        conn.execute(
            "INSERT OR REPLACE INTO dhw_error_log"
            " (day, bucket_idx, forecast_kwh, actual_kwh, error_kwh,"
            "  built_at_utc, applied_factor, mode)"
            " VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z', ?, ?)",
            (d.isoformat(), b, f, a, err, applied, mode),
        )
    conn.commit()
    conn.close()


def _recent_days(n):
    today = datetime.now(UTC).date()
    return [today - timedelta(days=i + 1) for i in range(n)]


# ---------------------------------------------------------------------------
# Open-loop learning — the adversarial-review regressions
# ---------------------------------------------------------------------------


def test_ratio_recovery(tmp_db):
    days = _recent_days(4)
    # bucket 6: consistent 4x over-forecast (actual/forecast = 0.25 at clamp)
    # bucket 3: consistent 2x under-forecast
    _seed_error_rows(tmp_db, [(d, 6, 2.0, 0.5) for d in days])
    _seed_error_rows(tmp_db, [(d, 3, 0.2, 0.4) for d in days])
    factors, samples, days_out, _ = dhw_bias.compute_dhw_bucket_bias()
    assert factors[6] == pytest.approx(0.25)
    assert factors[3] == pytest.approx(2.0)
    assert samples[6] == 4
    assert days_out[6] == 4


def test_refresh_is_idempotent_no_compounding(tmp_db):
    """THE F1 regression: re-running the refresh over the same data must yield
    the SAME factors — no prior-state feedback, nothing compounds while the
    corrector is disabled (or at any other time)."""
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 6, 2.0, 1.0) for d in days])  # ratio 0.5
    assert dhw_bias.refresh_dhw_bucket_bias() == 1
    assert db.get_dhw_bucket_bias()[6] == pytest.approx(0.5)
    for _ in range(5):  # five more "nights" of the same evidence
        dhw_bias.refresh_dhw_bucket_bias()
    assert db.get_dhw_bucket_bias()[6] == pytest.approx(0.5)  # NOT 0.5×0.75^5


def test_debias_learning_converged_state(tmp_db):
    """Rows produced WITH the correction applied (applied_factor=0.5, committed
    forecast already halved) de-bias back to raw — the estimator reports the
    same true ratio, i.e. the converged state is a fixed point."""
    days = _recent_days(4)
    # raw forecast 2.0, applied factor 0.5 → committed 1.0; actual 1.0
    _seed_error_rows(tmp_db, [(d, 6, 1.0, 1.0, 0.5) for d in days])
    factors, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert factors[6] == pytest.approx(0.5)  # actual/raw = 1.0/2.0


def test_shrunk_bucket_cannot_starve_its_own_learning(tmp_db):
    """The F2 regression: committed forecast pushed below the floor by the
    correction itself (0.12 raw × 0.25 = 0.03 committed < 0.05) must still be
    learned — the floor applies to the RAW forecast."""
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 8, 0.03, 0.1, 0.25) for d in days])  # raw 0.12
    factors, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert 8 in factors
    assert factors[8] == pytest.approx(0.1 / 0.12, rel=1e-6)


def test_zero_actual_rows_are_learned_from(tmp_db):
    """actual≈0 against a real forecast IS the over-forecast signal — kept."""
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 7, 1.5, 0.0) for d in days])
    factors, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert factors[7] == pytest.approx(0.25)  # 0/1.5 clamped up to MIN


def test_null_actual_rows_are_dropped(tmp_db):
    """NULL actual = missing Daikin split, NOT measured zero."""
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 7, 1.5, None) for d in days])
    factors, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert 7 not in factors


def test_non_normal_mode_rows_excluded(tmp_db):
    days = _recent_days(6)
    _seed_error_rows(tmp_db, [(d, 6, 1.0, 0.5) for d in days[:3]])
    _seed_error_rows(tmp_db, [(d, 6, 1.0, 3.0, 1.0, "guests") for d in days[3:]])
    factors, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert factors[6] == pytest.approx(0.5)  # guests days never pollute


def test_min_days_gate_and_clamp(tmp_db):
    days = _recent_days(4)
    # bucket 2: only 2 distinct days → gated out (min_days=3)
    _seed_error_rows(tmp_db, [(d, 2, 1.0, 0.1) for d in days[:2]])
    # bucket 5: 10x under-forecast → clamped to MAX 3.0
    _seed_error_rows(tmp_db, [(d, 5, 0.1, 1.0) for d in days])
    factors, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert 2 not in factors
    assert factors[5] == pytest.approx(3.0)


def test_boost_window_decontamination(tmp_db):
    """(day, bucket) pairs under a tank_negative_boost window are excluded —
    including 'skipped' boosts (boost-sized forecast, ordinary actual)."""
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 6, 1.0, 0.5) for d in days])
    conn = sqlite3.connect(tmp_db)
    for d, status in zip(days[:3], ("completed", "active", "skipped")):
        ws = datetime.combine(d, time(12, 0), tzinfo=UTC)
        conn.execute(
            "INSERT INTO action_schedule (date, start_time, end_time, device,"
            " action_type, status, created_at) VALUES (?, ?, ?, 'daikin',"
            " 'tank_negative_boost', ?, '2026-01-01T00:00:00Z')",
            (d.isoformat(), ws.isoformat(), (ws + timedelta(hours=2)).isoformat(), status),
        )
    conn.commit()
    conn.close()
    factors, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    # Only 1 clean day survives < min_days 3 → bucket gated out entirely
    assert 6 not in factors


def test_user_override_decontamination(tmp_db):
    """A user manually overriding a tank action poisons that window's actuals."""
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 8, 1.0, 0.5) for d in days])
    conn = sqlite3.connect(tmp_db)
    for d in days[:2]:
        ws = datetime.combine(d, time(16, 0), tzinfo=UTC)
        conn.execute(
            "INSERT INTO action_schedule (date, start_time, end_time, device,"
            " action_type, status, created_at, overridden_by_user_at)"
            " VALUES (?, ?, ?, 'daikin', 'tank_warmup', 'completed',"
            " '2026-01-01T00:00:00Z', ?)",
            (d.isoformat(), ws.isoformat(), (ws + timedelta(hours=2)).isoformat(),
             ws.isoformat()),
        )
    conn.commit()
    conn.close()
    factors, _, days_out, _ = dhw_bias.compute_dhw_bucket_bias()
    assert 8 not in factors  # only 2 clean days left < min_days 3
    assert days_out == {}


def test_legionella_bucket_decontamination(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 12, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", 0, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120, raising=False)
    days = _recent_days(8)
    dow = days[0].weekday()  # make "today-1" the legionella day
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", dow, raising=False)
    for d in days:
        poisoned = 5.0 if d.weekday() == dow else 0.5
        _seed_error_rows(tmp_db, [(d, 6, 1.0, poisoned)])
    factors, _, _, _ = dhw_bias.compute_dhw_bucket_bias()
    assert factors[6] == pytest.approx(0.5)  # clean days only


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


def _seed_factors(path, factors, computed_at=None):
    # Default computed_at = NOW: the staleness cutoff would silently no-op
    # the application (and re-vacuize every test below) on a fixed old date.
    ca = computed_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(path)
    for b, f in factors.items():
        conn.execute(
            "INSERT OR REPLACE INTO dhw_bucket_bias"
            " (bucket_idx, factor, raw_ratio, samples, days, computed_at)"
            " VALUES (?, ?, ?, 4, 4, ?)",
            (b, f, f, ca),
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


def test_application_disabled_guests_vacation_are_noops(tmp_db, monkeypatch):
    slots = _day_slots(date(2026, 7, 10))
    base_e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    base_g, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="guests")
    _seed_factors(tmp_db, {6: 0.3})
    # disabled (default) → byte-identical
    e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert e == base_e
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    # guests → comfort-critical, never biased
    eg, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="guests")
    assert eg == base_g
    # vacation → all zeros regardless
    ev, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="vacation")
    assert all(v == 0.0 for v in ev)


def test_stale_table_is_treated_as_absent(tmp_db, monkeypatch):
    slots = _day_slots(date(2026, 7, 10))
    base_e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    stale = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_factors(tmp_db, {6: 0.3}, computed_at=stale)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert e == base_e  # fossil correction must not apply


# Skewed tables (uniform factors normalize to exactly 1.0 → vacuous; these are
# the real "extreme" shapes: one bucket at a clamp against the rest at the
# other → normalized swings up to ~12x on a single bucket).
SKEW_UP_SHOWER = {**{b: 0.25 for b in range(12)}, 10: 3.0}
SKEW_DOWN_SHOWER = {**{b: 3.0 for b in range(12)}, 10: 0.25, 6: 0.25, 7: 0.25}


@pytest.mark.parametrize("factors", [SKEW_UP_SHOWER, SKEW_DOWN_SHOWER])
def test_skewed_factors_reshape_but_respect_heater_cap(tmp_db, monkeypatch, factors):
    """RISK 1 regression, non-vacuous: skewed normalization (≫1 on one bucket)
    must still be clamped to DAIKIN_MAX_HP_KW × 0.5 after application."""
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 1.0, raising=False)  # small heater
    _seed_factors(tmp_db, factors)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    slots = _day_slots(date(2026, 7, 10))
    on_e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", False, raising=False)
    off_e, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    assert on_e != off_e  # the skew actually did something (non-vacuous)
    assert all(v <= 0.5 + 1e-9 for v in on_e)


def test_negative_boost_slots_unaffected_by_skewed_bias(tmp_db, monkeypatch):
    """The boost ramp overwrites its slots AFTER the bias application point —
    deliberate max-heating is never scaled, even under a skewed table."""
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0, raising=False)
    slots = _day_slots(date(2026, 7, 10))
    prices = [10.0] * 48
    for i in range(20, 26):  # negative window 10:00-13:00 local
        prices[i] = -3.0
    base_e, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", price_line=prices, initial_tank_c=40.0
    )
    _seed_factors(tmp_db, SKEW_DOWN_SHOWER)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    e, _ = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", price_line=prices, initial_tank_c=40.0
    )
    assert e != base_e  # non-vacuous
    for i in range(20, 26):
        assert e[i] == pytest.approx(base_e[i])


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


@pytest.mark.parametrize("factors", [SKEW_UP_SHOWER, SKEW_DOWN_SHOWER])
@pytest.mark.parametrize("mode", ["normal", "guests"])
def test_k2_pin_stays_optimal_under_skewed_factors(tmp_db, monkeypatch, factors, mode):
    """Full solve with pinning on, SKEWED factors, a small heater, a negative
    window mid-day, and the horizon ending INSIDE the evening shower window
    (the #422 Infeasible shape). Guests runs bias-free by design — kept in the
    matrix to pin that Optimal too."""
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", mode, raising=False)
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 1.0, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    _seed_factors(tmp_db, factors)

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
    floor) is NOT pin-gated. A skewed pin (shower bucket eating the whole
    1 kW heater) must still solve on a −8 °C day."""
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", 1.0, raising=False)
    _seed_factors(tmp_db, SKEW_UP_SHOWER)

    start = datetime(2026, 1, 15, 0, 0, tzinfo=TZ_LOCAL).astimezone(UTC)
    slots = [start + timedelta(minutes=30 * i) for i in range(48)]
    plan = _solve(slots, [15.0] * 48, temp_c=-8.0, init_tank=40.0)
    assert plan is not None and plan.status == "Optimal"


# ---------------------------------------------------------------------------
# Rebuild stamps + plumbing
# ---------------------------------------------------------------------------


def _seed_consumption(path, day, bucket=6, kwh=0.5):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO daikin_consumption_2hourly (date, bucket_idx, kwh_dhw, source, fetched_at)"
        " VALUES (?, ?, ?, 'test', '2026-01-01T00:00:00Z')",
        (day.isoformat(), bucket, kwh),
    )
    conn.commit()
    conn.close()


def test_rebuild_stamps_applied_factor_and_mode(tmp_db, monkeypatch):
    """The error-log rebuild records the factor in force (1.0 while disabled)
    + mode, and never overwrites an existing stamp on re-run."""
    day = datetime.now(UTC).date() - timedelta(days=1)  # the cron's regime
    _seed_consumption(tmp_db, day)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    assert db.rebuild_dhw_error_log_for_date(day) == 1
    rows = db.get_dhw_error_log_range(day.isoformat(), day.isoformat())
    assert rows[0]["applied_factor"] == pytest.approx(1.0)  # disabled → 1.0
    assert rows[0]["mode"] == "normal"

    # enable + seed factors, re-run: the historical stamp must be preserved
    _seed_factors(tmp_db, {6: 0.5})
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    assert db.rebuild_dhw_error_log_for_date(day) == 1
    rows = db.get_dhw_error_log_range(day.isoformat(), day.isoformat())
    assert rows[0]["applied_factor"] == pytest.approx(1.0)  # COALESCE kept it


def test_rebuild_enabled_stamps_in_force_for_yesterday(tmp_db, monkeypatch):
    """With the corrector enabled, yesterday's rebuild stamps the normalized
    in-force factor — the value the learner will de-bias by."""
    day = datetime.now(UTC).date() - timedelta(days=1)
    _seed_consumption(tmp_db, day)
    _seed_factors(tmp_db, {6: 0.5})
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    assert db.rebuild_dhw_error_log_for_date(day) == 1
    rows = db.get_dhw_error_log_range(day.isoformat(), day.isoformat())
    expected = dhw_bias.normalized_factors({6: 0.5}, "normal")[6]
    assert rows[0]["applied_factor"] == pytest.approx(expected)


def test_rebuild_old_day_backfill_stamps_neutral(tmp_db, monkeypatch):
    """Round-2 guard: a catch-up rebuild of an OLDER day must stamp 1.0 even
    with the corrector enabled — today's factors were never applied to that
    day's committed forecast, and de-biasing history by them would poison a
    full learning window."""
    day = datetime.now(UTC).date() - timedelta(days=5)
    _seed_consumption(tmp_db, day)
    _seed_factors(tmp_db, {6: 0.5})
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    assert db.rebuild_dhw_error_log_for_date(day) == 1
    rows = db.get_dhw_error_log_range(day.isoformat(), day.isoformat())
    assert rows[0]["applied_factor"] == pytest.approx(1.0)


def test_rebuild_preserves_null_actual(tmp_db):
    """A NULL kwh_dhw split stays NULL — missing is not zero."""
    day = date(2026, 1, 21)
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO daikin_consumption_2hourly (date, bucket_idx, kwh_dhw, source, fetched_at)"
        " VALUES (?, 4, NULL, 'test', '2026-01-01T00:00:00Z')",
        (day.isoformat(),),
    )
    conn.commit()
    conn.close()
    assert db.rebuild_dhw_error_log_for_date(day) == 1
    rows = db.get_dhw_error_log_range(day.isoformat(), day.isoformat())
    assert rows[0]["actual_kwh"] is None


def test_refresh_on_empty_table_is_quiet_noop(tmp_db):
    assert dhw_bias.refresh_dhw_bucket_bias() == 0
    assert db.get_dhw_bucket_bias() == {}


def test_refresh_persists_days_column(tmp_db):
    days = _recent_days(4)
    _seed_error_rows(tmp_db, [(d, 6, 2.0, 0.5) for d in days])
    assert dhw_bias.refresh_dhw_bucket_bias() == 1
    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT days, samples FROM dhw_bucket_bias WHERE bucket_idx=6").fetchone()
    conn.close()
    assert row == (4, 4)


def test_backtest_shape_and_honesty(tmp_db):
    days = _recent_days(8)
    _seed_error_rows(tmp_db, [(d, 6, 2.0, 0.5) for d in days])
    _seed_error_rows(tmp_db, [(d, 3, 0.2, 0.4) for d in days])
    out = dhw_bias.backtest_dhw_bucket_bias(14)
    assert out["in_sample"] is not None
    assert out["in_sample"]["mae_reduction_kwh"] > 0
    assert out["out_of_sample"] is not None
    # the gate evaluates the SAME object production applies: normalized
    # open-loop factors — cross-check against a live refresh + normalization
    dhw_bias.refresh_dhw_bucket_bias()
    live = dhw_bias.normalized_factors(db.get_dhw_bucket_bias(), "normal")
    for b_str, f in out["factor_by_bucket"].items():
        assert live[int(b_str)] == pytest.approx(f, abs=1e-3)


def test_budget_state_surfaces_bias(tmp_db, monkeypatch):
    _seed_factors(tmp_db, {6: 0.3})
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    state = dhw_policy.dhw_budget_state("normal")
    assert state["bucket_bias_enabled"] is True
    assert state["bucket_bias_factors"]["6"] == pytest.approx(0.3)
    assert "6" in state["bucket_bias_in_force"]
    # guests: nothing in force even when enabled
    state_g = dhw_policy.dhw_budget_state("guests")
    assert state_g["bucket_bias_in_force"] == {}
