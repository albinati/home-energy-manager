"""db.residual_load_profile_v2 — day-of-week buckets, measured-split-calibrated
residual, away-day exclusion, median + p75 spread (#477)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db

LON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()
    db.clear_residual_profile_cache()


def _seed_pv(t_utc: datetime, load_kw: float) -> None:
    db.save_pv_realtime_sample(t_utc.isoformat().replace("+00:00", "Z"), load_power_kw=load_kw)


def _seed_meteo(t_utc: datetime, temp_c: float) -> None:
    hour_iso = t_utc.replace(minute=0, second=0, microsecond=0).isoformat()
    db.save_meteo_forecast_history(
        (t_utc - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        [{"slot_time": hour_iso, "temp_c": temp_c, "solar_w_m2": 0.0}],
    )


def _seed_local(d: date, hour: int, minute: int, load_kw: float, temp_c: float = 25.0) -> None:
    """Seed one pv+meteo sample at LOCAL (hour:minute) on date d."""
    t_utc = datetime(d.year, d.month, d.day, hour, minute, tzinfo=LON).astimezone(UTC)
    _seed_meteo(t_utc, temp_c)
    _seed_pv(t_utc, load_kw)


def _seed_daikin_2h(d: date, bucket_idx: int, heating: float, dhw: float) -> None:
    db.upsert_daikin_consumption_2hourly(
        date=d.isoformat(), bucket_idx=bucket_idx,
        kwh_total=heating + dhw, kwh_heating=heating, kwh_dhw=dhw, source="test",
    )


def _recent_days_of_weekday(weekday: int, n: int, *, start_ago: int = 4) -> list[date]:
    """The n most recent past dates whose weekday() == weekday."""
    out: list[date] = []
    d = datetime.now(LON).date() - timedelta(days=start_ago)
    while len(out) < n:
        if d.weekday() == weekday:
            out.append(d)
        d -= timedelta(days=1)
    return out


def test_day_of_week_split() -> None:
    """Saturdays high, Tuesdays low at the same (h,m) → weekend bucket > weekday
    bucket, and the plain (h,m) tier sits between."""
    for d in _recent_days_of_weekday(1, 6):   # Tuesdays, low
        _seed_local(d, 19, 0, load_kw=0.6)
        _seed_local(d, 19, 10, load_kw=0.6)
    for d in _recent_days_of_weekday(5, 6):   # Saturdays, high
        _seed_local(d, 19, 0, load_kw=2.0)
        _seed_local(d, 19, 10, load_kw=2.0)

    prof = db.residual_load_profile_v2(window_days=120)
    p = prof["profile"]
    tue = db.lookup_residual_kwh(prof, 1, 19, 0)   # Tuesday
    sat = db.lookup_residual_kwh(prof, 5, 19, 0)   # Saturday
    assert sat > tue + 0.3, (sat, tue)
    # Distinct per-dow buckets exist (warm → residual ≈ load×0.5).
    assert p[(5, 19, 0)] == pytest.approx(1.0, abs=0.1)
    assert p[(1, 19, 0)] == pytest.approx(0.3, abs=0.1)
    # Plain (h,m) tier blends both, between the two.
    assert tue <= p[(19, 0)] <= sat


def test_away_day_excluded() -> None:
    """A day whose daytime residual is anomalously low is flagged away and not
    counted in the medians."""
    normal = _recent_days_of_weekday(2, 8)   # 8 Wednesdays, normal load
    for d in normal:
        for hr in (10, 13, 16, 19):
            _seed_local(d, hr, 0, load_kw=1.0)
    # One extra Wednesday with near-zero daytime load (away).
    away = _recent_days_of_weekday(2, 9)[-1]
    for hr in (10, 13, 16, 19):
        _seed_local(away, hr, 0, load_kw=0.02)

    prof = db.residual_load_profile_v2(window_days=120)
    assert away.isoformat() in prof["away_days"]
    # Wednesday 19:00 median reflects the normal days (~0.5 kWh), not dragged down.
    assert db.lookup_residual_kwh(prof, 2, 19, 0) == pytest.approx(0.5, abs=0.12)


def test_calibration_lowers_daikin_raises_residual() -> None:
    """Cold day: pure physics would subtract a big Daikin chunk. A MEASURED 2h
    split smaller than physics scales it down → residual rises toward raw load."""
    days = _recent_days_of_weekday(3, 6)   # Thursdays
    # cold (5C) at local 08:00 → bucket_idx = 4; load 1.5 kW → 0.75 kWh/slot
    for d in days:
        _seed_local(d, 8, 0, load_kw=1.5, temp_c=5.0)
        _seed_local(d, 8, 10, load_kw=1.5, temp_c=5.0)

    pure = db.residual_load_profile_v2(window_days=120, use_cache=False)
    r_pure = db.lookup_residual_kwh(pure, 3, 8, 0)

    # Now add a measured split far BELOW physics for those days/bucket.
    for d in days:
        _seed_daikin_2h(d, 4, heating=0.02, dhw=0.0)
    cal = db.residual_load_profile_v2(window_days=120, use_cache=False)
    r_cal = db.lookup_residual_kwh(cal, 3, 8, 0)

    assert r_cal > r_pure + 0.1, (r_cal, r_pure)
    assert cal["calibrated_days"] >= 1
    assert pure["calibrated_days"] == 0


def test_calibration_fallback_equals_pure_physics() -> None:
    """No measured split → identical to the pure-physics residual (parity)."""
    days = _recent_days_of_weekday(4, 5)   # Fridays
    for d in days:
        _seed_local(d, 8, 0, load_kw=1.5, temp_c=5.0)
        _seed_local(d, 8, 10, load_kw=1.5, temp_c=5.0)
    prof = db.residual_load_profile_v2(window_days=120)
    assert prof["physics_only_days"] >= 1
    assert prof["calibrated_days"] == 0
    # Matches the legacy builder's residual for the same warm/cold physics.
    legacy = db.half_hourly_residual_load_profile_kwh(window_days=120)
    assert db.lookup_residual_kwh(prof, 4, 8, 0) == pytest.approx(legacy[(8, 0)], abs=0.05)


def _seed_negative_exec(d: date, hour: int, minute: int = 0) -> None:
    """Mark the half-hour slot at LOCAL (hour:minute) on date d as negative-price
    in execution_log (the same source residual_load_profile_v2 reads)."""
    t_utc = datetime(d.year, d.month, d.day, hour, minute, tzinfo=LON).astimezone(UTC)
    db.log_execution({
        "timestamp": t_utc.isoformat().replace("+00:00", "Z"),
        "agile_price_pence": -4.0,
        "consumption_kwh": 1.0,
    })


def test_negative_price_slots_excluded_by_default() -> None:
    """Plunge slots carry deliberately-boosted load — they must be dropped so the
    profile reflects the organic at-home pattern, not price-driven consumption."""
    tues = _recent_days_of_weekday(1, 6)
    for d in tues:
        # 02:00 plunge — boosted high load AND negative price → must be excluded.
        _seed_local(d, 2, 0, load_kw=3.0)
        _seed_negative_exec(d, 2, 0)
        # 19:00 organic evening — normal load, positive price → retained.
        _seed_local(d, 19, 0, load_kw=0.6)

    prof = db.residual_load_profile_v2(window_days=120)
    assert prof["day_counts"]["negative_excluded"] >= 6
    # The boosted plunge bucket is gone → lookup falls back well below 1.5 kWh.
    assert db.lookup_residual_kwh(prof, 1, 2, 0) < 1.0
    # The organic bucket survives untouched.
    assert prof["profile"][(1, 19, 0)] == pytest.approx(0.3, abs=0.1)


def test_negative_price_slots_kept_when_killswitch_off(monkeypatch) -> None:
    """LP_LOAD_EXCLUDE_NEGATIVE_SLOTS=false → legacy behaviour (slots retained)."""
    from src.config import config as cfg
    monkeypatch.setattr(cfg, "LP_LOAD_EXCLUDE_NEGATIVE_SLOTS", False, raising=False)

    tues = _recent_days_of_weekday(1, 6)
    for d in tues:
        _seed_local(d, 2, 0, load_kw=3.0)
        _seed_negative_exec(d, 2, 0)

    prof = db.residual_load_profile_v2(window_days=120, use_cache=False)
    assert prof["day_counts"]["negative_excluded"] == 0
    # Boosted slots retained → bucket median = 3.0 kW × 0.5 h = 1.5 kWh.
    assert prof["profile"][(1, 2, 0)] == pytest.approx(1.5, abs=0.1)


def test_fallback_hierarchy_and_min_samples() -> None:
    """A (dow,h,m) bucket below min_samples is dropped; lookup falls to (h,m).
    Every (h,m) leaf is always present."""
    for d in _recent_days_of_weekday(0, 6):   # Mondays
        _seed_local(d, 20, 0, load_kw=1.0)
    prof = db.residual_load_profile_v2(window_days=120, min_samples_per_bucket=4)
    # Monday 20:00 has ≥4 samples → specific bucket exists.
    assert (0, 20, 0) in prof["profile"]
    # Sunday 20:00 has no samples → lookup falls through to the (h,m) leaf.
    assert (6, 20, 0) not in prof["profile"]
    assert db.lookup_residual_kwh(prof, 6, 20, 0) == pytest.approx(prof["profile"][(20, 0)])
    # All 48 (h,m) leaves filled (hour-aware fallback).
    assert all((h, m) in prof["profile"] for h in range(24) for m in (0, 30))


def test_kill_switch_off_emits_only_hm_tier(monkeypatch) -> None:
    """LP_RESIDUAL_PROFILE_V2=false → no day-of-week tiers, no calibration, no
    away exclusion (≈ legacy). Lookup falls back to the (h,m) leaf."""
    from src.config import config
    monkeypatch.setattr(config, "LP_RESIDUAL_PROFILE_V2", False, raising=False)
    for d in _recent_days_of_weekday(5, 6):
        _seed_local(d, 19, 0, load_kw=2.0)
        _seed_local(d, 19, 10, load_kw=2.0)
        _seed_daikin_2h(d, 9, heating=0.5, dhw=0.0)  # would calibrate if v2 on
    prof = db.residual_load_profile_v2(window_days=120)
    # No (dow,h,m) or (group,h,m) keys — only (h,m) 2-tuples.
    assert all(isinstance(k, tuple) and len(k) == 2 for k in prof["profile"])
    assert prof["away_days"] == []
    assert prof["calibrated_days"] == 0  # calibration skipped


def test_spread_is_present_and_not_below_median() -> None:
    for d in _recent_days_of_weekday(5, 8):   # Saturdays, varied load
        _seed_local(d, 18, 0, load_kw=0.5)
        _seed_local(d, 18, 10, load_kw=2.5)   # wide spread within the bucket
    prof = db.residual_load_profile_v2(window_days=120)
    med = prof["profile"].get((5, 18, 0))
    sp = prof["spread"].get((5, 18, 0))
    assert med is not None and sp is not None
    assert sp >= med  # p75 >= median


def test_cache_returns_same_object_until_cleared() -> None:
    """The TTL cache collapses repeated calls (per solve) to one rebuild."""
    for d in _recent_days_of_weekday(5, 4):
        _seed_local(d, 19, 0, load_kw=1.0)
    a = db.residual_load_profile_v2(window_days=120)
    b = db.residual_load_profile_v2(window_days=120)
    assert a is b  # cached
    c = db.residual_load_profile_v2(window_days=120, use_cache=False)
    assert c is not a  # bypass forces a rebuild
    db.clear_residual_profile_cache()
    d2 = db.residual_load_profile_v2(window_days=120)
    assert d2 is not a  # cleared → rebuilt


def test_end_date_anchors_window_to_past_period() -> None:
    """end_date scopes the trailing window to a past anchor: a load spike AFTER
    the anchor is excluded, one before it is included (#574 item 3)."""
    # A recent Saturday with a high-load slot, and an OLDER Saturday with a low one.
    sats = _recent_days_of_weekday(5, 6, start_ago=4)
    recent, older = sats[0], sats[-1]
    _seed_local(recent, 19, 0, load_kw=3.0)
    _seed_local(recent, 19, 10, load_kw=3.0)
    _seed_local(older, 19, 0, load_kw=0.6)
    _seed_local(older, 19, 10, load_kw=0.6)

    # Anchor the window to END the day before the recent spike → it's excluded,
    # so the Saturday 19:00 median reflects only the older low-load day.
    anchor = (recent - timedelta(days=1)).isoformat()
    prof = db.residual_load_profile_v2(window_days=120, end_date=anchor, use_cache=False)
    assert db.lookup_residual_kwh(prof, 5, 19, 0) == pytest.approx(0.3, abs=0.12)

    # Without the anchor the recent high-load day pulls the median up.
    full = db.residual_load_profile_v2(window_days=120, use_cache=False)
    assert db.lookup_residual_kwh(full, 5, 19, 0) > db.lookup_residual_kwh(prof, 5, 19, 0) + 0.3


def test_end_date_is_part_of_cache_key() -> None:
    """Different end_date anchors don't collide in the TTL cache."""
    for d in _recent_days_of_weekday(5, 4):
        _seed_local(d, 19, 0, load_kw=1.0)
    a = db.residual_load_profile_v2(window_days=120)
    b = db.residual_load_profile_v2(window_days=120, end_date="2026-01-01")
    assert a is not b


def test_residual_profile_endpoint_shape() -> None:
    """GET /api/v1/load/residual-profile returns JSON-serialisable per-dow series."""
    import asyncio
    from src.api.routers import pv as pv_router

    for d in _recent_days_of_weekday(5, 6):
        _seed_local(d, 19, 0, load_kw=2.0)
        _seed_local(d, 19, 10, load_kw=2.0)
    resp = asyncio.run(pv_router.get_residual_load_profile(window_days=120))
    assert set(resp.keys()) >= {"by_dow", "hp_by_dow", "all", "flat", "away_days",
                                "day_counts", "calibrated_days", "physics_only_days"}
    assert len(resp["by_dow"]) == 7
    assert len(resp["hp_by_dow"]) == 7
    assert len(resp["all"]) == 48
    assert len(resp["hp_by_dow"]["5"]) == 48
    sat19 = next(s for s in resp["by_dow"]["5"] if s["h"] == 19 and s["m"] == 0)
    assert sat19["median"] == pytest.approx(1.0, abs=0.1)
    # JSON-serialisable (no tuple keys leaked).
    import json
    json.dumps(resp)


def test_heat_pump_profile_present_on_cold_days() -> None:
    """hp_profile carries the heat-pump (Daikin) split; cold slots → non-zero."""
    days = _recent_days_of_weekday(3, 6)   # Thursdays, cold so physics > 0
    for d in days:
        _seed_local(d, 8, 0, load_kw=1.5, temp_c=2.0)
        _seed_local(d, 8, 10, load_kw=1.5, temp_c=2.0)
    prof = db.residual_load_profile_v2(window_days=120, use_cache=False)
    assert (3, 8, 0) in prof["hp_profile"]
    assert db.lookup_hp_kwh(prof, 3, 8, 0) > 0.0


def test_heat_pump_profile_zero_when_warm() -> None:
    """Warm slots → physics heat-pump estimate ≈ 0, so the hp profile is ~0."""
    for d in _recent_days_of_weekday(2, 6):   # Wednesdays, warm
        _seed_local(d, 14, 0, load_kw=0.6, temp_c=25.0)
        _seed_local(d, 14, 10, load_kw=0.6, temp_c=25.0)
    prof = db.residual_load_profile_v2(window_days=120, use_cache=False)
    assert db.lookup_hp_kwh(prof, 2, 14, 0) == pytest.approx(0.0, abs=0.05)


def test_hp_split_tank_vs_heating_from_measured_ratio() -> None:
    """The combined hp profile splits into TANK (DHW) + HEATING (space) using the
    measured Onecta ratio. A bucket measured 75% DHW / 25% heating splits the
    calibrated heat-pump energy in that proportion, and the two components sum to
    roughly the combined hp (#574 item 2)."""
    days = _recent_days_of_weekday(3, 6)   # Thursdays, cold so physics > 0
    for d in days:
        _seed_local(d, 8, 0, load_kw=1.5, temp_c=2.0)
        _seed_local(d, 8, 10, load_kw=1.5, temp_c=2.0)
        # local 08:00 → bucket_idx 4; measured 0.6 DHW + 0.2 heating (75% tank).
        _seed_daikin_2h(d, 4, heating=0.2, dhw=0.6)

    prof = db.residual_load_profile_v2(window_days=120, use_cache=False)
    combined = db.lookup_hp_kwh(prof, 3, 8, 0)
    tank = db.lookup_hp_component_kwh(prof, 3, 8, 0, component="hp_dhw_profile")
    heating = db.lookup_hp_component_kwh(prof, 3, 8, 0, component="hp_space_profile")

    assert combined > 0.0
    assert tank > heating          # 75% DHW
    assert tank + heating == pytest.approx(combined, abs=0.02)
    assert tank == pytest.approx(0.75 * combined, abs=0.02)


def test_hp_split_defaults_to_heating_without_measured_dhw() -> None:
    """No measured DHW for the slot → the whole heat-pump estimate is attributed
    to HEATING (the physics term is space-heating-shaped). Tank ≈ 0."""
    days = _recent_days_of_weekday(2, 6)   # Wednesdays, cold; no daikin split seeded
    for d in days:
        _seed_local(d, 8, 0, load_kw=1.5, temp_c=2.0)
        _seed_local(d, 8, 10, load_kw=1.5, temp_c=2.0)
    prof = db.residual_load_profile_v2(window_days=120, use_cache=False)
    combined = db.lookup_hp_kwh(prof, 2, 8, 0)
    assert combined > 0.0
    assert db.lookup_hp_component_kwh(prof, 2, 8, 0, component="hp_dhw_profile") == pytest.approx(0.0, abs=1e-6)
    assert db.lookup_hp_component_kwh(prof, 2, 8, 0, component="hp_space_profile") == pytest.approx(combined, abs=0.02)


def test_lookup_hp_kwh_falls_back_to_zero_for_unknown_slot() -> None:
    for d in _recent_days_of_weekday(0, 6):   # Mondays only
        _seed_local(d, 8, 0, load_kw=1.0, temp_c=2.0)
    prof = db.residual_load_profile_v2(window_days=120)
    # Sunday 03:00 was never observed → honest 0.0 (heat pump idles).
    assert db.lookup_hp_kwh(prof, 6, 3, 0) == 0.0
