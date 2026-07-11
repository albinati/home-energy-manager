"""DHW forecast recalibration + trailing auto-scale (#534).

The 2026-06 audit showed the static per-slot constants over-budgeted ~45%
with the wrong intra-day shape: measured Onecta 2-hourly splits put almost
all DHW electric in the 12:00-16:00 warmup window (~1.4-1.8 kWh) while the
old model charged 0.50 kWh/slot to the 20:00-22:00 shower window where the
firmware's hysteresis draws only ~0.35-0.45 kWh total. These tests pin the
reshaped totals and the measured/nominal auto-scale loop.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    from src import db as _db
    from src import dhw_policy
    _db.init_db()
    dhw_policy._autoscale_cache.clear()
    yield
    dhw_policy._autoscale_cache.clear()


def _day_slots(n: int = 48) -> list[datetime]:
    base = datetime(2026, 6, 17, 0, 0, tzinfo=UTC)  # a Wednesday, BST
    return [base + timedelta(minutes=30 * i) for i in range(n)]


def _seed_measured_dhw(values: list[float]) -> None:
    """Seed kwh_dhw for the trailing days ending yesterday (local)."""
    from src import db, dhw_policy
    tz = dhw_policy._tz_local()
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()
    for i, v in enumerate(values):
        db.upsert_daikin_consumption_daily(
            date=(yesterday - timedelta(days=i)).isoformat(),
            kwh_total=v, kwh_dhw=v, source="onecta",
        )


# ---------------------------------------------------------------------------
# Reshaped constants
# ---------------------------------------------------------------------------

def test_normal_day_total_matches_measured_reality():
    from src import dhw_policy
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(_day_slots(), mode="normal")
    assert sum(e_dhw) == pytest.approx(3.0, abs=0.05)


def test_guests_day_total_no_longer_doubles_normal():
    from src import dhw_policy
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(_day_slots(), mode="guests")
    assert sum(e_dhw) == pytest.approx(3.36, abs=0.05)


def test_warmup_hour_carries_the_dominant_load():
    """Both half-slots of the warmup hour are transition slots; the warmup
    window must out-weigh the evening shower window (measured shape)."""
    from src import dhw_policy
    slots = _day_slots()
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    tz = dhw_policy._tz_local()
    warmup_h = int(getattr(app_config, "DHW_WARMUP_START_HOUR_LOCAL", 13))
    warmup_kwh = sum(
        e for s, e in zip(slots, e_dhw) if s.astimezone(tz).hour == warmup_h
    )
    shower_kwh = sum(
        e for s, e in zip(slots, e_dhw) if 20 <= s.astimezone(tz).hour < 22
    )
    assert warmup_kwh == pytest.approx(0.9, abs=0.01)   # 2 × 0.45
    assert shower_kwh == pytest.approx(0.48, abs=0.01)  # 4 × 0.12
    assert warmup_kwh > shower_kwh


# ---------------------------------------------------------------------------
# Auto-scale loop
# ---------------------------------------------------------------------------

def test_autoscale_tracks_measured_median():
    from src import dhw_policy
    _seed_measured_dhw([2.0] * 10)  # June-like reality vs nominal 3.0
    factor = dhw_policy._dhw_autoscale_factor("normal")
    assert factor == pytest.approx(2.0 / 3.0, abs=0.01)
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(_day_slots(), mode="normal")
    assert sum(e_dhw) == pytest.approx(2.0, abs=0.1)


def test_autoscale_median_is_robust_to_boost_outliers():
    """A negative-price boost day (7 kWh) must not drag the factor up."""
    from src import dhw_policy
    _seed_measured_dhw([2.0, 2.0, 7.0, 2.0, 2.0, 2.0, 2.0])
    factor = dhw_policy._dhw_autoscale_factor("normal")
    assert factor == pytest.approx(2.0 / 3.0, abs=0.01)


def test_autoscale_clamped():
    from src import dhw_policy
    _seed_measured_dhw([0.0] * 8)  # zero-draw stretch → would be factor 0
    assert dhw_policy._dhw_autoscale_factor("normal") == pytest.approx(
        float(getattr(app_config, "DHW_FORECAST_AUTOSCALE_MIN", 0.5))
    )
    dhw_policy._autoscale_cache.clear()
    _seed_measured_dhw([30.0] * 8)
    assert dhw_policy._dhw_autoscale_factor("normal") == pytest.approx(
        float(getattr(app_config, "DHW_FORECAST_AUTOSCALE_MAX", 1.6))
    )


def test_autoscale_needs_min_days():
    from src import dhw_policy
    _seed_measured_dhw([2.0] * 3)  # below DHW_FORECAST_AUTOSCALE_MIN_DAYS=5
    assert dhw_policy._dhw_autoscale_factor("normal") == 1.0


def test_autoscale_kill_switch(monkeypatch):
    from src import dhw_policy
    _seed_measured_dhw([2.0] * 10)
    monkeypatch.setattr(app_config, "DHW_FORECAST_AUTOSCALE_ENABLED", False, raising=False)
    assert dhw_policy._dhw_autoscale_factor("normal") == 1.0


def test_autoscale_inert_in_vacation():
    from src import dhw_policy
    _seed_measured_dhw([2.0] * 10)
    assert dhw_policy._dhw_autoscale_factor("vacation") == 1.0
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(_day_slots(), mode="vacation")
    assert sum(e_dhw) == 0.0


def test_nominal_matches_forecast_shape():
    """The auto-scale denominator must never drift from the forecast's own
    phase rules — equality between the closed-form walk and a real forecast."""
    from src import dhw_policy
    for mode in ("normal", "guests"):
        e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(_day_slots(), mode=mode)
        assert sum(e_dhw) == pytest.approx(
            dhw_policy._nominal_daily_total_kwh(mode), abs=1e-6
        )


# ---------------------------------------------------------------------------
# Review fixes (#536)
# ---------------------------------------------------------------------------

def test_guests_factor_never_scales_below_one():
    """Mode-blind numerator: right after a normal→guests flip the median
    still reflects normal-mode days. Guests is comfort-critical — the
    factor floors at 1.0 there (review MED-1)."""
    from src import dhw_policy
    _seed_measured_dhw([2.0] * 10)  # normal-mode history, median 2.0
    assert dhw_policy._dhw_autoscale_factor("guests") == 1.0
    dhw_policy._autoscale_cache.clear()
    # Scaling UP still allowed in guests.
    _seed_measured_dhw([6.0] * 10)
    assert dhw_policy._dhw_autoscale_factor("guests") > 1.0


def test_pinned_e_dhw_never_exceeds_heater_capacity(monkeypatch):
    """LP pins e_dhw == forecast against a variable bounded by
    DAIKIN_MAX_HP_KW × 0.5; a scaled-up transition slot must clamp or the
    solve goes Infeasible on small-heater configs (review MED-2)."""
    from src import dhw_policy
    monkeypatch.setattr(app_config, "DAIKIN_MAX_HP_KW", 0.8, raising=False)  # 0.4 kWh/slot
    _seed_measured_dhw([4.8] * 10)  # pushes factor to the 1.6 clamp
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(_day_slots(), mode="normal")
    cap = 0.8 * 0.5
    assert max(e_dhw) <= cap + 1e-9


def test_null_upsert_does_not_clobber_dhw_split():
    """sync_daikin_daily upserts kwh_dhw=None; that must not erase the
    nightly rollup's real split the auto-scale feeds on (review LOW-3)."""
    from src import db, dhw_policy
    tz = dhw_policy._tz_local()
    day = (datetime.now(tz) - timedelta(days=1)).date().isoformat()
    db.upsert_daikin_consumption_daily(
        date=day, kwh_total=5.0, kwh_heating=3.0, kwh_dhw=2.0, source="onecta",
    )
    db.upsert_daikin_consumption_daily(
        date=day, kwh_total=5.5, kwh_heating=5.5, kwh_dhw=None, cop_daily=None,
        source="telemetry_integral",
    )
    row = db.get_daikin_consumption_daily_by_date(day)
    assert row["kwh_dhw"] == 2.0
    assert row["kwh_total"] == 5.5


# ---------------------------------------------------------------------------
# #681 — price-aware warmup hour: autoscale denominator consistency
# ---------------------------------------------------------------------------

def test_nominal_total_shifts_with_resolved_warmup_hour():
    """Moving the warmup earlier lengthens the warmup window (setback slots
    become warmup-maintenance), so the nominal denominator MUST change with the
    resolved hour — else the autoscale drifts."""
    from src import dhw_policy
    n13 = dhw_policy._nominal_daily_total_kwh("normal", 13)
    n11 = dhw_policy._nominal_daily_total_kwh("normal", 11)
    # 11:00 start adds two extra warmup hours (was setback) → strictly larger.
    assert n11 > n13


def test_autoscale_and_bias_normalizer_use_same_resolved_hour(monkeypatch):
    """The autoscale denominator and the bucket-bias normalizer must read the
    SAME resolved warmup hour — else the level double-corrects (issue #681).
    With price-aware ON and a persisted 11:00, both nominal paths key off 11."""
    from datetime import datetime
    from src import db, dhw_policy
    from src.config import config as app_config
    from src.dhw_bias import normalized_factors

    monkeypatch.setattr(app_config, "DHW_WARMUP_PRICE_AWARE_ENABLED", True, raising=False)
    today = datetime.now(dhw_policy._tz_local()).date()
    db.set_runtime_setting(dhw_policy._warmup_setting_key(today), "11")
    dhw_policy._autoscale_cache.clear()

    # Autoscale reads today's resolved hour (11) for its denominator.
    _seed_measured_dhw([3.0] * 10)
    nominal_11 = dhw_policy._nominal_daily_total_kwh("normal", 11)
    factor = dhw_policy._dhw_autoscale_factor("normal")
    assert factor == pytest.approx(min(1.6, max(0.5, 3.0 / nominal_11)), abs=1e-6)

    # The bias normalizer divides by the SAME nominal shares (resolved hour 11).
    shares_default = dhw_policy._nominal_bucket_shares("normal")  # → today's = 11
    shares_11 = dhw_policy._nominal_bucket_shares("normal", 11)
    assert shares_default == shares_11
    # normalized_factors consumes _nominal_bucket_shares(mode) with no explicit
    # hour, so it too tracks the resolved 11:00 — sanity that it returns a
    # complete 12-bucket dict without error.
    norm = normalized_factors({5: 2.0}, "normal")
    assert len(norm) == 12
