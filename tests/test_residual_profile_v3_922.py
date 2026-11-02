"""#922 residual-load profile v3 — per-slot heat-pump subtraction from measured
Onecta energy, mean/median statistic, weekday shrinkage, hard away exclusion,
and the kill switch (off == v2 exactly)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config

LON = ZoneInfo("Europe/London")
SAMPLES_PER_SLOT = 10  # 3-min telemetry => ~10 samples per 30-min slot


@pytest.fixture(autouse=True)
def _init(monkeypatch) -> None:
    db.init_db()
    db.clear_residual_profile_cache()
    monkeypatch.setattr(config, "LP_RESIDUAL_PROFILE_V3", True, raising=False)
    monkeypatch.setattr(config, "LP_RESIDUAL_STATISTIC", "mean", raising=False)
    monkeypatch.setattr(config, "LP_RESIDUAL_DOW_SHRINK", 1.0, raising=False)
    monkeypatch.setattr(config, "LP_RESIDUAL_AWAY_HARD_EXCLUDE", True, raising=False)
    yield
    db.clear_residual_profile_cache()
    for k in ("LP_RESIDUAL_PROFILE_V3", "LP_RESIDUAL_STATISTIC",
              "LP_RESIDUAL_DOW_SHRINK", "LP_RESIDUAL_AWAY_HARD_EXCLUDE"):
        config._overrides.pop(k, None)


def _seed_slot(d: date, hour: int, minute: int, load_kw: float, temp_c: float = 25.0) -> None:
    """~10 samples inside ONE 30-min local slot, constant load_kw."""
    base = datetime(d.year, d.month, d.day, hour, minute, tzinfo=LON).astimezone(UTC)
    for i in range(SAMPLES_PER_SLOT):
        t = base + timedelta(minutes=3 * i, seconds=5)
        hour_iso = t.replace(minute=0, second=0, microsecond=0).isoformat()
        db.save_meteo_forecast_history(
            (t - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            [{"slot_time": hour_iso, "temp_c": temp_c, "solar_w_m2": 0.0}],
        )
        db.save_pv_realtime_sample(t.isoformat().replace("+00:00", "Z"), load_power_kw=load_kw)


def _seed_bucket(d: date, bucket: int, load_kw: float | list[float], temp_c: float = 25.0) -> None:
    """The four 30-min slots of local 2h bucket ``bucket``."""
    loads = load_kw if isinstance(load_kw, list) else [load_kw] * 4
    for j, kw in enumerate(loads):
        _seed_slot(d, bucket * 2 + j // 2, 30 * (j % 2), kw, temp_c)


def _days(weekday: int, n: int, start_ago: int = 4) -> list[date]:
    out: list[date] = []
    d = datetime.now(LON).date() - timedelta(days=start_ago)
    while len(out) < n:
        if d.weekday() == weekday:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _recent(n: int, start_ago: int = 4) -> list[date]:
    base = datetime.now(LON).date() - timedelta(days=start_ago)
    return [base - timedelta(days=i) for i in range(n)]


def _onecta(d: date, bucket: int, heating: float, dhw: float = 0.0, source: str = "onecta_cache") -> None:
    db.upsert_daikin_consumption_2hourly(
        date=d.isoformat(), bucket_idx=bucket, kwh_total=heating + dhw,
        kwh_heating=heating, kwh_dhw=dhw, source=source,
    )


def test_calibration_with_realistic_samples_per_slot_subtracts_the_measured_hp() -> None:
    """The v2 bug: physics summed per SAMPLE (10/slot) made k ~10x too small, so
    the residual kept the HP draw. v3 subtracts the measured Onecta bucket."""
    days = _recent(6)
    for d in days:
        _seed_bucket(d, 8, 2.0, temp_c=2.0)      # 1.0 kWh/slot, 4.0 per 2h bucket
        _onecta(d, 8, heating=1.2)               # measured 1.2 kWh in the bucket
    prof = db.residual_load_profile_v2(window_days=60)
    # equal load -> equal split: 0.3 kWh HP per slot, 0.7 residual
    assert prof["profile"][(16, 0)] == pytest.approx(0.7, abs=0.02)
    assert prof["hp_profile"][(16, 0)] == pytest.approx(0.3, abs=0.02)
    assert prof["version"] == 3


def test_hp_allocated_by_load_shape_and_model_rows_ignored() -> None:
    """telemetry_integral rows are a model: ignored. daily - Onecta is spread
    over the model buckets, then allocated to slots by load shape."""
    days = _recent(6)
    for d in days:
        # one slot is 1 kWh above the other three -> it takes the whole HP excess
        _seed_bucket(d, 8, [2.0, 2.0, 2.0, 4.0], temp_c=2.0)
        _onecta(d, 8, heating=3.0, source="telemetry_integral")   # model: must be ignored
        db.upsert_daikin_consumption_daily(date=d.isoformat(), kwh_total=1.0, source="onecta")
    prof = db.residual_load_profile_v2(window_days=60)
    p = prof["profile"]
    # daily 1.0 (not the 3.0 model) all lands on the shape-excess slot (17:30)
    assert p[(17, 30)] == pytest.approx(2.0 - 1.0, abs=0.02)
    assert p[(16, 0)] == pytest.approx(1.0, abs=0.02)
    assert prof["hp_profile"][(17, 30)] == pytest.approx(1.0, abs=0.02)


def test_days_without_measured_data_fall_back_to_physics() -> None:
    days = _recent(6)
    for d in days:
        _seed_bucket(d, 8, 1.0, temp_c=-2.0)   # cold: physics HP > 0, no Onecta/daily rows
    prof = db.residual_load_profile_v2(window_days=60)
    assert prof["physics_only_days"] >= 6
    assert prof["hp_profile"][(16, 0)] > 0.0
    assert prof["profile"][(16, 0)] == pytest.approx(0.5 - prof["hp_profile"][(16, 0)], abs=0.02)


def test_statistic_switch_mean_vs_median() -> None:
    days = _recent(8)
    for i, d in enumerate(days):
        # 7 days at 1 kW (0.5/slot), one outlier day at 5 kW (2.5/slot)
        _seed_bucket(d, 8, 5.0 if i == 0 else 1.0)
    config._overrides["LP_RESIDUAL_STATISTIC"] = "mean"
    mean_p = db.residual_load_profile_v2(window_days=60, use_cache=False)["profile"][(16, 0)]
    config._overrides["LP_RESIDUAL_STATISTIC"] = "median"
    med_p = db.residual_load_profile_v2(window_days=60, use_cache=False)["profile"][(16, 0)]
    assert med_p == pytest.approx(0.5, abs=0.02)
    assert mean_p == pytest.approx((7 * 0.5 + 2.5) / 8, abs=0.03)
    assert mean_p > med_p + 0.1


def test_dow_shrink_interpolates_toward_all_days() -> None:
    for d in _days(1, 5):          # Tuesdays 2 kW -> 1.0 kWh/slot
        _seed_bucket(d, 8, 2.0)
    for d in _days(2, 5):          # Wednesdays 1 kW -> 0.5 kWh/slot
        _seed_bucket(d, 8, 1.0)
    got = {}
    for w in (1.0, 0.5, 0.0):
        config._overrides["LP_RESIDUAL_DOW_SHRINK"] = w
        prof = db.residual_load_profile_v2(window_days=60, use_cache=False)
        got[w] = db.lookup_residual_kwh(prof, 1, 16, 0)
        # spread never drops below the central value
        assert db.lookup_residual_spread_kwh(prof, 1, 16, 0) >= got[w] - 1e-9
    assert got[1.0] == pytest.approx(1.0, abs=0.02)        # today's behaviour
    assert got[0.0] == pytest.approx(0.75, abs=0.02)       # all days only
    assert got[0.5] == pytest.approx(0.875, abs=0.02)


def _snapshot(plan_date: date, preset: str) -> None:
    run_at = datetime(plan_date.year, plan_date.month, plan_date.day, 12, tzinfo=UTC)
    with db._lock:
        conn = db.get_connection()
        try:
            conn.execute(
                "INSERT INTO lp_inputs_snapshot (run_at_utc, plan_date, optimization_preset) VALUES (?,?,?)",
                (run_at.isoformat().replace("+00:00", "Z"), plan_date.isoformat(), preset),
            )
            conn.commit()
        finally:
            conn.close()


def test_hard_away_excludes_vacation_days_and_the_return_day() -> None:
    days = _recent(8, start_ago=4)          # newest first
    away_day, return_day = days[3], days[2]  # return_day is the day AFTER away_day
    for d in days:
        # away and return days are HIGH (0.9 kWh/slot) so the 0.4 x median
        # heuristic would never remove them; normal days 0.5 kWh/slot
        _seed_bucket(d, 8, 1.8 if d in (away_day, return_day) else 1.0)
    _snapshot(away_day, "vacation")
    for d in days:
        if d != away_day:
            _snapshot(d, "normal")

    prof = db.residual_load_profile_v2(window_days=60, use_cache=False)
    assert away_day.isoformat() in prof["away_days"]
    assert return_day.isoformat() in prof["away_days"]
    assert prof["return_days"] == [return_day.isoformat()]
    assert prof["profile"][(16, 0)] == pytest.approx(0.5, abs=0.02)

    config._overrides["LP_RESIDUAL_AWAY_HARD_EXCLUDE"] = False
    prof = db.residual_load_profile_v2(window_days=60, use_cache=False)
    assert away_day.isoformat() not in prof["away_days"]
    assert prof["profile"][(16, 0)] > 0.56   # the high days are back in


def test_hard_away_uses_daikin_holiday_flag() -> None:
    days = _recent(8, start_ago=4)
    hol = days[4]
    for d in days:
        _seed_bucket(d, 8, 1.8 if d in (hol, days[3]) else 1.0)
    noon = datetime(hol.year, hol.month, hol.day, 12, tzinfo=LON).astimezone(UTC).timestamp()
    for i in range(4):
        db.insert_daikin_telemetry({"fetched_at": noon + 600 * i, "source": "live", "holiday_active": 1})
    prof = db.residual_load_profile_v2(window_days=60, use_cache=False)
    assert hol.isoformat() in prof["away_days"]
    assert days[3].isoformat() in prof["return_days"]
    assert prof["profile"][(16, 0)] == pytest.approx(0.5, abs=0.02)


def test_cache_key_includes_new_settings() -> None:
    for d in _recent(6):
        _seed_bucket(d, 8, 1.0 if d.day % 2 else 3.0)
    config._overrides["LP_RESIDUAL_STATISTIC"] = "mean"
    a = db.residual_load_profile_v2(window_days=60)
    config._overrides["LP_RESIDUAL_STATISTIC"] = "median"
    b = db.residual_load_profile_v2(window_days=60)
    assert a is not b and a["statistic"] == "mean" and b["statistic"] == "median"


def test_kill_switch_off_is_exactly_v2(monkeypatch) -> None:
    for d in _recent(6):
        _seed_bucket(d, 8, 2.0, temp_c=2.0)
        _onecta(d, 8, heating=1.2)
    monkeypatch.setattr(config, "LP_RESIDUAL_PROFILE_V3", False, raising=False)
    off = db.residual_load_profile_v2(window_days=60, use_cache=False)
    assert "version" not in off
    # reference: the v2 builder with the v3 settings absent altogether
    for k in ("LP_RESIDUAL_STATISTIC", "LP_RESIDUAL_DOW_SHRINK", "LP_RESIDUAL_AWAY_HARD_EXCLUDE"):
        config._overrides[k] = {"LP_RESIDUAL_STATISTIC": "mean", "LP_RESIDUAL_DOW_SHRINK": 0.0,
                                "LP_RESIDUAL_AWAY_HARD_EXCLUDE": False}[k]
    off2 = db.residual_load_profile_v2(window_days=60, use_cache=False)
    assert off == off2                      # none of the v3 knobs touch v2
    # and v2 still shows the old per-sample under-subtraction (the bug v3 fixes)
    assert off["profile"][(16, 0)] > 0.8


def test_runtime_settings_are_editable_and_validated() -> None:
    from src import runtime_settings as rts

    for key in ("LP_RESIDUAL_PROFILE_V3", "LP_RESIDUAL_STATISTIC",
                "LP_RESIDUAL_DOW_SHRINK", "LP_RESIDUAL_AWAY_HARD_EXCLUDE"):
        assert key in rts.SCHEMA
    rts.set_setting("LP_RESIDUAL_DOW_SHRINK", 0.3, actor="test")
    assert rts.get_setting("LP_RESIDUAL_DOW_SHRINK") == pytest.approx(0.3)
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("LP_RESIDUAL_DOW_SHRINK", 1.5, actor="test")
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("LP_RESIDUAL_STATISTIC", "mode", actor="test")


# -- wire: what the LP solver actually receives -------------------------------


def test_wire_lp_receives_v3_base_load_when_switch_on(monkeypatch) -> None:
    """The real optimizer builds ``base_load_kwh`` from the residual profile and
    hands it to ``solve_lp``. With v3 on the 16-18 slots carry load minus the
    MEASURED heat-pump energy; with v3 off (v2) the per-sample under-subtraction
    leaves the HP in the residual."""
    from src.scheduler import optimizer
    from tests.test_lp_appliance_real_solver import (
        TARIFF, _seed_realistic_day, _stub_optimal_lp_plan,
    )

    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    monkeypatch.setattr(config, "APPLIANCE_DISPATCH_ENABLED", False)
    now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))

    for d in _recent(21):                       # every weekday has history
        _seed_bucket(d, 8, 1.5, temp_c=2.0)     # 0.75 kWh per peak slot
        _onecta(d, 8, heating=2.0)              # measured 0.5 kWh HP per slot

    def run() -> float:
        cap: dict = {}

        def _capture(*, base_load_kwh, **kwargs):
            cap["b"] = list(base_load_kwh)
            cap["s"] = list(kwargs["slot_starts_utc"])
            return _stub_optimal_lp_plan(base_load_kwh=base_load_kwh, **kwargs)

        monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _capture)
        db.clear_residual_profile_cache()
        optimizer.run_optimizer(fox=None, daikin=None)
        assert cap, "solve_lp was never called"
        peak = [b for b, s in zip(cap["b"], cap["s"])
                if 16 <= s.astimezone(LON).hour < 18 and s.astimezone(LON).date() == date(2026, 5, 21)]
        assert len(peak) == 4
        return sum(peak) / 4

    monkeypatch.setattr(config, "LP_RESIDUAL_PROFILE_V3", True, raising=False)
    on = run()
    monkeypatch.setattr(config, "LP_RESIDUAL_PROFILE_V3", False, raising=False)
    off = run()
    assert on == pytest.approx(0.25, abs=0.03)      # 0.75 load - 0.5 measured HP
    assert off > on + 0.15                           # v2 kept most of the HP in the residual
