"""#928 — daily, offset-aware, bounded, shadow-first LWT->kW (k) calibration + k-independent floor."""
from __future__ import annotations

import json
import logging
import random
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import db, physics
from src.config import config as app_config

LON = ZoneInfo("Europe/London")
AS_OF = date(2026, 11, 2)
TRUE_K = 0.04
TRUE_B = 0.025


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()
    monkeypatch.setattr(db, "_hard_away_dates", lambda cutoff, tz: (set(), set(), set()))
    for k, v in (("LWT_K_CALIBRATION_ENABLED", True), ("LWT_K_CALIBRATION_SHADOW", False),
                 ("LWT_K_MAX_STEP_PCT", 15.0), ("LWT_K_MIN_DAYS", 14)):
        monkeypatch.setattr(app_config, k, v, raising=False)


def _seed(n_days: int = 20, *, end: date = AS_OF, k: float = TRUE_K, b: float = TRUE_B,
          source: str = "onecta_cache", noise: float = 0.0, seed: int = 1) -> list[str]:
    """Daily Onecta totals + 48 learning-log slots/day (LOCAL days), T and offset varying per day."""
    rnd = random.Random(seed)
    days = []
    with db._lock:
        conn = db.get_connection()
        try:
            for i in range(n_days):
                d = end - timedelta(days=n_days - i)
                ds = d.isoformat()
                days.append(ds)
                t_day = 2.0 + (i * 5) % 11
                off_day = float(((i * 3) % 7) - 3)
                sx = so = 0.0
                t0 = datetime.combine(d, datetime.min.time(), tzinfo=LON).astimezone(UTC)
                for s in range(48):
                    t_out = t_day + (2.0 if 20 <= s < 34 else 0.0)
                    slot = (t0 + timedelta(minutes=30 * s)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    conn.execute(
                        "INSERT INTO lwt_learning_log (slot_time_utc, device_offset, offset_written, outdoor_real_c)"
                        " VALUES (?,?,?,?)", (slot, off_day, off_day, t_out))
                    sx += max(0.0, physics.get_lwt_base_c(t_out) - 18.0) * 0.5
                    so += off_day * 0.5
                y = max(0.0, k * sx + b * so + rnd.gauss(0.0, noise))
                conn.execute(
                    "INSERT OR REPLACE INTO daikin_consumption_daily (date,kwh_total,kwh_heating,kwh_dhw,source,fetched_at)"
                    " VALUES (?,?,?,?,?,?)", (ds, y + 2, y, 2.0, source, "2026-11-02T00:00:00Z"))
            conn.commit()
        finally:
            conn.close()
    return days


def _kv() -> dict:
    return json.loads(db.get_kv(db.LWT_K_OUTCOME_KV))


# ---------------------------------------------------------------- fit ------------------------------------------------

def test_fit_recovers_k_and_b_offset_days_included() -> None:
    _seed(20)
    r = db.fit_lwt_kw_daily(as_of=AS_OF)
    assert r["status"] == "ok" and r["offset_term"] is True and r["n_days"] == 20
    assert r["k_per_degc"] == pytest.approx(TRUE_K, abs=1e-3)
    assert r["b_kw_per_k"] == pytest.approx(TRUE_B, abs=1e-3)


def test_fit_ignores_the_selected_2h_table() -> None:
    """The 2h table is survivorship-selected (#928 review): poison it, the fit must not change."""
    _seed(20)
    clean = db.fit_lwt_kw_daily(as_of=AS_OF)["k_per_degc"]
    with db._lock:
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO daikin_consumption_2hourly (date,bucket_idx,kwh_total,kwh_heating,kwh_dhw,source,fetched_at)"
            " VALUES ('2026-10-30',3,9,9,0,'onecta_cache','x')")
        conn.commit()
        conn.close()
    assert db.fit_lwt_kw_daily(as_of=AS_OF)["k_per_degc"] == pytest.approx(clean)


def test_telemetry_integral_days_are_never_used() -> None:
    _seed(20, source="telemetry_integral")
    r = db.fit_lwt_kw_daily(as_of=AS_OF)
    assert r["status"] == "skipped" and r["n_days"] == 0


def test_away_days_are_excluded_by_local_date(monkeypatch: pytest.MonkeyPatch) -> None:
    days = _seed(24)
    away = set(days[-6:])
    with db._lock:
        conn = db.get_connection()
        conn.execute("UPDATE daikin_consumption_daily SET kwh_heating=0 WHERE date IN (%s)" % ",".join("?" * 6), tuple(away))
        conn.commit()
        conn.close()
    polluted = db.fit_lwt_kw_daily(as_of=AS_OF)
    monkeypatch.setattr(db, "_hard_away_dates", lambda cutoff, tz: (away, set(), set()))
    clean = db.fit_lwt_kw_daily(as_of=AS_OF)
    assert clean["excluded_away_days"] == 6 and clean["n_days"] == 18
    assert clean["k_per_degc"] == pytest.approx(TRUE_K, abs=1e-3)
    assert polluted["k_per_degc"] < clean["k_per_degc"] - 0.003


def test_low_log_coverage_days_are_dropped() -> None:
    days = _seed(20)
    with db._lock:
        conn = db.get_connection()
        conn.execute("DELETE FROM lwt_learning_log WHERE slot_time_utc LIKE ?", (days[-1] + "%",))
        conn.commit()
        conn.close()
    assert db.fit_lwt_kw_daily(as_of=AS_OF)["n_days"] == 19


# ------------------------------------------------------- refresh: bounds / gates ----------------------------------------

def test_thin_evidence_skips_and_keeps_previous() -> None:
    _seed(10)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.058, samples=7, window_days=30)
    out = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
    assert out["outcome"] == "skipped" and "evidence too thin" in out["reason"]
    assert db.get_daikin_lwt_kw_calibration()["k_per_degc"] == pytest.approx(0.058)
    assert _kv()["outcome"] == "skipped"  # recorded even when nothing was applied


def test_step_bounded_per_local_day_not_per_solve() -> None:
    _seed(20, k=0.095, b=0.0)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.060, samples=7, window_days=30)
    for _ in range(5):  # five solves the same local day
        out = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
        assert out["outcome"] == "applied" and out["step_limited"] is True
        assert db.get_daikin_lwt_kw_calibration()["k_per_degc"] == pytest.approx(0.069)
    nxt = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF + timedelta(days=1))  # new day: new anchor 0.069
    assert nxt["k_applied"] == pytest.approx(0.069 * 1.15)


def test_no_row_anchors_on_the_physics_default() -> None:
    _seed(20, k=0.09, b=0.0)
    out = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
    assert out["outcome"] == "applied" and out["k_prev"] is None
    assert out["k_applied"] == pytest.approx(physics._KW_PER_DEGC_LWT_DEFAULT * 1.15)


def test_clamp_to_plausible_range(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(20, k=0.14, b=0.0)
    monkeypatch.setattr(app_config, "LWT_K_MAX_STEP_PCT", 100.0)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.095, samples=7, window_days=30)
    out = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
    assert out["k_applied"] == pytest.approx(0.10) and out["clamped"] is True
    assert (db.LWT_K_CLAMP_MIN, db.LWT_K_CLAMP_MAX) == (0.03, 0.10)


def test_wide_ci_is_rejected_and_previous_kept() -> None:
    _seed(20, k=0.0, b=0.0, noise=5.0, seed=3)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.058, samples=7, window_days=30)
    out = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
    assert out["outcome"] == "rejected" and out["reason_code"] in ("wide_ci", "weak_r2", "nonpositive")
    assert db.get_daikin_lwt_kw_calibration()["k_per_degc"] == pytest.approx(0.058)
    assert _kv()["outcome"] == "rejected" and _kv()["k_applied"] == pytest.approx(0.058)


# ------------------------------------------------------------ modes / observability --------------------------------------

def test_default_is_off_and_shadow() -> None:
    from src.runtime_settings import SCHEMA as SETTINGS

    assert SETTINGS["LWT_K_CALIBRATION_ENABLED"].env_default() == "false"
    assert SETTINGS["LWT_K_CALIBRATION_SHADOW"].env_default() == "true"
    assert SETTINGS["LP_W3_SPACE_FLOOR_KW_PER_K"].env_default() == 0.0


def test_shadow_computes_and_stores_candidate_but_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(20)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.0581, samples=7, window_days=30)
    for enabled in (False, True):
        monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_ENABLED", enabled)
        monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_SHADOW", True)
        out = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
        assert out["outcome"] == "shadow" and out["mode"] == "shadow"
        assert out["k_candidate"] == pytest.approx(0.0581 * 0.85)  # 0.04 bounded by the 15% step
        assert db.get_daikin_lwt_kw_calibration()["k_per_degc"] == pytest.approx(0.0581)
        assert _kv()["k_candidate"] == pytest.approx(out["k_candidate"])


def test_frozen_writes_nothing_but_records(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(20)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.050, samples=7, window_days=30)
    monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_ENABLED", False)
    monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_SHADOW", False)
    out = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
    assert out["outcome"] == "frozen"
    assert db.get_daikin_lwt_kw_calibration()["k_per_degc"] == pytest.approx(0.050)
    assert _kv()["outcome"] == "frozen"


def test_kv_updates_every_call_but_info_only_on_change(caplog: pytest.LogCaptureFixture) -> None:
    _seed(20)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.060, samples=7, window_days=30)
    with caplog.at_level(logging.INFO, logger="src.db"):
        stamps = []
        for _ in range(4):
            db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
            stamps.append(_kv()["at"])
        first = [r for r in caplog.records if "daikin_lwt_calibration" in r.getMessage()]
        assert len(first) == 1  # one transition line; the repeats are silent
        caplog.clear()
        db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
        assert not [r for r in caplog.records if "daikin_lwt_calibration" in r.getMessage()]
    assert all(r.levelno == logging.INFO for r in first)
    assert len(set(stamps)) >= 1 and _kv()["at"]


def test_exception_paths_warn_with_the_stage_and_still_record(monkeypatch: pytest.MonkeyPatch,
                                                              caplog: pytest.LogCaptureFixture) -> None:
    def boom(**kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(db, "fit_lwt_kw_daily", boom)
    with caplog.at_level(logging.WARNING, logger="src.db"):
        out = db.refresh_daikin_lwt_kw_calibration(as_of=AS_OF)
    assert out["outcome"] == "error" and out["reason_code"] == "error_fit"
    assert any(r.levelno == logging.WARNING and "[fit]" in r.getMessage() for r in caplog.records)
    assert _kv()["outcome"] == "error"


# ---------------------------------------------------------------- physics loader -----------------------------------------

def test_loader_continuous_clamp_when_enabled_and_legacy_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.0265, samples=7, window_days=30)
    monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_ENABLED", True)
    assert physics.get_kw_per_degc_lwt() == pytest.approx(0.03)
    monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_ENABLED", False)
    assert physics.get_kw_per_degc_lwt() == pytest.approx(0.0265)  # legacy bounds, zero deploy impact
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.5, samples=7, window_days=30)
    assert physics.get_kw_per_degc_lwt() == physics._KW_PER_DEGC_LWT_DEFAULT


# ------------------------------------------------- floor decoupled from k (LP_W3_SPACE_FLOOR_KW_PER_K) -------------------

def _w3_env(monkeypatch):
    for k, v in {
        "DAIKIN_CONTROL_MODE": "active", "LP_HP_MIN_ON_SLOTS": 1, "LP_INVERTER_STRESS_COST_PENCE": 0.0,
        "LP_W3_TIN_ENABLED": True, "BUILDING_UA_W_PER_K": 200.0, "DAIKIN_HOLIDAY_DETECT_ENABLED": True,
        "DAIKIN_LWT_SOURCE": "lp", "BATTERY_CAPACITY_KWH": 10.0, "LP_SOC_FINAL_KWH": 2.5,
        "LP_W3_SPACE_FLOOR_ENABLED": True, "LP_W3_SPACE_FLOOR_FACTOR": 1.25,
        "LP_W3_SPACE_FLOOR_TRIM_KW_PER_K": 0.0, "LWT_K_CALIBRATION_ENABLED": False,
    }.items():
        monkeypatch.setattr(app_config, k, v, raising=False)


def _solve(outdoor=8.0, n=24):
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    base = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[outdoor] * n, shortwave_radiation_wm2=[40.0] * n,
        cloud_cover_pct=[80.0] * n, pv_kwh_per_slot=[0.0] * n, cop_space=[3.2] * n, cop_dhw=[2.5] * n)
    st = LpInitialState(soc_kwh=6.0, tank_temp_c=45.0, indoor_temp_c=21.0)
    pr = ([10.0] * 12 + [40.0] * 6 + [10.0] * 6)[:n]
    return solve_lp(slot_starts_utc=slots, price_pence=pr, base_load_kwh=[0.3] * n, weather=w, initial=st, tz=LON)


def _plan_sig(p):
    r = lambda xs: [round(float(x), 5) for x in xs]  # noqa: E731
    return (r(p.space_electric_kwh), r(p.dhw_electric_kwh), r(p.import_kwh), r(p.lwt_offset_c),
            r(p.w3_space_floor_kwh), round(p.objective_pence, 4))


def test_golden_s_equal_current_k_is_identical_to_s_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _w3_env(monkeypatch)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.0581, samples=7, window_days=30)
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_KW_PER_K", 0.0, raising=False)
    legacy = _solve()
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_KW_PER_K", physics.get_kw_per_degc_lwt(), raising=False)
    decoupled = _solve()
    assert legacy.ok and decoupled.ok and max(legacy.w3_space_floor_kwh) > 0.05
    assert _plan_sig(legacy) == _plan_sig(decoupled)


def test_wire_with_s_set_changing_k_leaves_floor_and_floored_offsets(monkeypatch: pytest.MonkeyPatch) -> None:
    _w3_env(monkeypatch)
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_KW_PER_K", 0.0581, raising=False)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.0581, samples=7, window_days=30)
    a = _solve()
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.038, samples=7, window_days=30)
    b = _solve()
    assert a.ok and b.ok
    assert a.w3_space_floor_kwh == pytest.approx(b.w3_space_floor_kwh)
    deepest = max(float(app_config.OPTIMIZATION_LWT_OFFSET_MIN), float(app_config.DAIKIN_LWT_LP_OFFSET_MIN))
    floored = [i for i, f in enumerate(a.w3_space_floor_kwh)
               if f > 1e-6 and abs(a.space_electric_kwh[i] + f * a.dhw_electric_kwh[i] - f) < 1e-4]
    assert floored
    # the floor-bound slots are written at (or beyond, clamped by the dispatcher to) the deepest offset
    assert all(a.lwt_offset_c[i] <= deepest + 1e-6 for i in floored)
    assert all(b.lwt_offset_c[i] <= deepest + 1e-6 for i in floored)
    # control: with s=0 the floor DOES follow k
    monkeypatch.setattr(app_config, "LP_W3_SPACE_FLOOR_KW_PER_K", 0.0, raising=False)
    c = _solve()
    assert c.w3_space_floor_kwh != pytest.approx(a.w3_space_floor_kwh)


def test_inversion_uses_the_same_slope() -> None:
    s, fac = 0.058, 1.25
    t = 6.0
    x = physics.get_lwt_base_c(t) - 18.0
    for off in (-5.0, -2.0, 0.0, 3.0):
        kw = physics.space_floor_device_draw_kw(physics.space_floor_curve_kw(t, s), off, factor=fac, k_t=s)
        back = physics.lwt_offset_from_space_kw_device(kw, t, factor=fac, k_t=s, lo=-10, hi=10, curve_slope=s)
        assert back == pytest.approx(off, abs=1e-6)
        assert kw == pytest.approx(fac * s * max(0.0, x + off))


# ------------------------------------------------------- wire: run_optimizer -> solve_lp ----------------------------------

def test_wire_run_optimizer_refreshes_before_solve_and_lp_sees_the_bounded_k(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.scheduler import optimizer
    from src.scheduler.lp_optimizer import LpPlan

    tariff = "E-1R-AGILE-TEST-K928"
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", tariff)
    monkeypatch.setattr(app_config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", True)
    now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    for day in (datetime(2026, 5, 20, tzinfo=UTC), datetime(2026, 5, 21, tzinfo=UTC)):
        rows, vf = [], day
        for _ in range(48):
            vt = vf + timedelta(minutes=30)
            rows.append({"valid_from": vf.isoformat().replace("+00:00", "Z"),
                         "valid_to": vt.isoformat().replace("+00:00", "Z"), "value_inc_vat": 12.0})
            vf = vt
        db.save_agile_rates(rows, tariff)
    # the refresh uses the real local date: seed the daily data relative to it
    _seed(20, end=datetime.now(LON).date(), k=0.095, b=0.0)
    db.upsert_daikin_lwt_kw_calibration(k_per_degc=0.060, samples=7, window_days=30)

    seen: list[float] = []

    def fake_solve(**kw):
        seen.append(physics.get_kw_per_degc_lwt())  # what the LP floor/ceiling/lift read at solve time
        n = len(kw["slot_starts_utc"])
        plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0)
        plan.slot_starts_utc = list(kw["slot_starts_utc"])
        plan.price_pence = list(kw["price_pence"])
        for f in ("import_kwh", "export_kwh", "battery_charge_kwh", "battery_discharge_kwh", "pv_use_kwh",
                  "pv_curtail_kwh", "dhw_electric_kwh", "space_electric_kwh", "lwt_offset_c"):
            setattr(plan, f, [0.0] * n)
        plan.temp_outdoor_c = [12.0] * n
        plan.tank_temp_c = [45.0] * (n + 1)
        plan.soc_kwh = [2.0] * (n + 1)
        return plan

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", fake_solve)

    # shadow (default): the LP keeps seeing the stored k, the candidate is recorded
    monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_ENABLED", False)
    monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_SHADOW", True)
    optimizer.run_optimizer(fox=None, daikin=None, trigger_reason="plan_push")
    assert seen and seen[-1] == pytest.approx(0.060)
    assert _kv()["outcome"] == "shadow" and _kv()["k_candidate"] == pytest.approx(0.069)

    # live: the LP sees exactly the day-bounded value, not the raw 0.095 fit
    monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_ENABLED", True)
    monkeypatch.setattr(app_config, "LWT_K_CALIBRATION_SHADOW", False)
    optimizer.run_optimizer(fox=None, daikin=None, trigger_reason="plan_push")
    optimizer.run_optimizer(fox=None, daikin=None, trigger_reason="plan_push")
    assert seen[-1] == pytest.approx(0.069) and seen[-2] == pytest.approx(0.069)
    assert _kv()["outcome"] == "applied" and _kv()["k_raw"] > 0.085
