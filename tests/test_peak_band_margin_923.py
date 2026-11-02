"""#923 peak-band conformal margin (residual basis): error history, conformal
quantile + out-of-sample coverage, away / forecaster-key exclusion, fallback,
cap, rebuild path, the pessimistic base_load wire (max rule), the real
classifier on Cosy + DST days, the optimizer wire and the endpoint field."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.analytics import peak_margin as pm
from src.config import config
from src.scheduler import optimizer, scenarios
from src.scheduler.lp_optimizer import LpInitialState, LpPlan
from src.weather import WeatherLpSeries

LON = ZoneInfo("Europe/London")
BAND = {(16, 0), (16, 30), (17, 0), (17, 30), (18, 0), (18, 30)}
KEYS = ("LP_PEAK_MARGIN_ENABLED", "LP_PEAK_MARGIN_QUANTILE",
        "LP_PEAK_MARGIN_WINDOW_DAYS", "LP_PEAK_MARGIN_MAX_KWH",
        "LP_SCENARIOS_ON_TRIGGER_REASONS")
AS_OF = date(2026, 11, 2)
COSY = "E-1R-COSY-22-12-08-H"


@pytest.fixture(autouse=True)
def _init(monkeypatch):
    db.init_db()
    pm.clear_cache()
    monkeypatch.setattr(db, "_hard_away_dates", lambda cutoff, tz: (set(), set(), set()))
    conn = db.get_connection()
    conn.execute("DELETE FROM load_error_log")
    conn.execute("DELETE FROM lp_inputs_snapshot")
    conn.commit()
    conn.close()
    yield
    pm.clear_cache()
    for k in KEYS:
        config._overrides.pop(k, None)


@pytest.fixture
def fake_band(monkeypatch):
    monkeypatch.setattr(pm, "peak_local_slots", lambda day, tz=None: set(BAND))


# ---- helpers: committed base in load_error_log, measured residual injected ------

RESID: dict[str, float] = {}      # local date -> measured residual band sum
HP: dict[str, float] = {}


@pytest.fixture
def fake_meas(monkeypatch):
    RESID.clear()
    HP.clear()
    calls = {"n": 0}
    monkeypatch.setattr(pm, "_daily_row_complete", lambda day, tz: True)

    def fake(first, last):
        calls["n"] += 1
        out = {}
        d = first
        while d <= last:
            iso = d.isoformat()
            if iso in RESID:
                out[iso] = {k: (RESID[iso] / len(BAND), HP.get(iso, 0.5) / len(BAND)) for k in BAND}
            d += timedelta(days=1)
        return out

    monkeypatch.setattr(db, "measured_residual_by_local_slot", fake)
    return calls


def _seed_base(d: date, band_base: float) -> None:
    conn = db.get_connection()
    try:
        for (h, m) in sorted(BAND):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC)
            b = band_base / len(BAND)
            conn.execute(
                "INSERT OR REPLACE INTO load_error_log "
                "(slot_time_utc, forecast_kwh, forecast_base_kwh, actual_kwh, error_kwh, built_at_utc) "
                "VALUES (?,?,?,?,?,?)",
                (t.strftime("%Y-%m-%dT%H:%M:%SZ"), b + 5.0, b, 9.0, 9.0 - b - 5.0, "2026-11-02T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()


def _seed(errors: list[float], base: float = 3.0) -> list[date]:
    days = [AS_OF - timedelta(days=i + 1) for i in range(len(errors))]
    for d, e in zip(days, errors):
        _seed_base(d, base)
        RESID[d.isoformat()] = base + e
    return days


# ---- quantile / coverage ---------------------------------------------------------

def test_conformal_order_statistic():
    xs = [float(i) for i in range(1, 21)]
    assert pm.conformal_quantile(xs, 0.75) == 16.0        # ceil(21 * .75) = 16th
    assert pm.conformal_quantile([1.0, 2.0, 3.0], 0.95) == 3.0   # capped at n
    assert pm.conformal_quantile([5.0], 0.5) == 5.0


def test_oos_coverage_matches_brute_force():
    errs = [0.2, 0.5, 0.1, 0.9, 0.3, 0.4, 0.8, 0.2, 0.6, 0.5, 0.7, 0.3, 0.4, 0.9, 0.2, 0.5,
            1.4, 0.3, 0.6, 0.4]
    d0 = date(2026, 9, 1)
    hist = [((d0 + timedelta(days=i)).isoformat(), e) for i, e in enumerate(errs)]
    cov, n = pm.oos_coverage(hist, 0.75, 5.0, 42, min_prior=14)
    exp_cov = exp_n = 0
    for i in range(14, len(errs)):
        m = pm.conformal_quantile(errs[:i], 0.75)
        exp_n += 1
        exp_cov += errs[i] <= m
    assert n == exp_n == 6
    assert cov == pytest.approx(100.0 * exp_cov / exp_n)


# ---- history on a residual basis -----------------------------------------------

def test_history_is_residual_minus_committed_base_not_total(fake_band, fake_meas):
    d = _seed([0.4, 1.0, -0.2])
    HP[d[0].isoformat()] = 9.9   # a huge HP must not leak into the error
    hist = pm.peak_band_error_history(AS_OF, 42)
    assert sorted(round(h["error_kwh"], 6) for h in hist) == [-0.2, 0.4, 1.0]
    h0 = next(h for h in hist if h["date"] == d[0].isoformat())
    assert h0["residual_kwh"] == pytest.approx(3.4)
    assert h0["base_kwh"] == pytest.approx(3.0)
    assert h0["hp_kwh"] == pytest.approx(9.9)
    assert h0["error_kwh"] == pytest.approx(0.4)


def test_margin_conformal_and_oos_coverage(fake_band, fake_meas):
    errs = [i / 10.0 for i in range(1, 21)]
    _seed(errs)
    config._overrides["LP_PEAK_MARGIN_QUANTILE"] = 0.75
    config._overrides["LP_PEAK_MARGIN_MAX_KWH"] = 5.0
    info = pm.peak_band_margin(as_of_day=AS_OF, force=True)
    assert info["fallback"] is None and info["n_days"] == 20 and info["min_days"] == 14
    assert info["margin_kwh"] == pytest.approx(1.6)          # 16th of 0.1..2.0
    assert info["n_oos"] == 6 and info["coverage_oos_pct"] is not None
    assert info["forecaster_key"] == pm.current_forecaster_key()


def test_away_return_and_non_normal_days_are_excluded(fake_band, fake_meas, monkeypatch):
    days = _seed([0.5] * 16 + [9.0, 9.0, 9.0])
    monkeypatch.setattr(
        db, "_hard_away_dates",
        lambda cutoff, tz: ({days[16].isoformat()}, {days[17].isoformat()}, {days[18].isoformat()}))
    hist = pm.peak_band_error_history(AS_OF, 42)
    assert len(hist) == 16
    config._overrides["LP_PEAK_MARGIN_MAX_KWH"] = 5.0
    assert pm.peak_band_margin(as_of_day=AS_OF, force=True)["margin_kwh"] == pytest.approx(0.5)


def test_days_from_a_different_base_forecaster_are_excluded(fake_band, fake_meas):
    days = _seed([0.5] * 20)
    conn = db.get_connection()
    try:
        # the 8 oldest days were planned by a v3 run (key differs from the current v2 key)
        for i, d in enumerate(days):
            run_at = datetime(d.year, d.month, d.day, 6, 0, tzinfo=UTC)
            key = "v3|mean|0.50|away=1|bias=0" if i >= 12 else "v2|bias=0"
            conn.execute(
                "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc, exogenous_snapshot_json) VALUES (?,?,?)",
                (9000 + i, run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 '{"base_forecaster_key": "%s"}' % key))
        conn.commit()
    finally:
        conn.close()
    assert pm.current_forecaster_key() == "v2|bias=0"
    hist = pm.peak_band_error_history(AS_OF, 42)
    assert len(hist) == 12
    assert pm.peak_band_error_history(AS_OF, 42, forecaster_key="v3|mean|0.50|away=1|bias=0")[0]["date"] == days[-1].isoformat()
    info = pm.peak_band_margin(as_of_day=AS_OF, force=True)       # 12 < 14 usable days
    assert info["fallback"] == "too_few_days" and info["margin_kwh"] == 0.0 and info["n_days"] == 12


def test_forecaster_key_changes_with_settings(monkeypatch):
    base = pm.current_forecaster_key()
    monkeypatch.setattr(config, "LP_RESIDUAL_PROFILE_V3", True, raising=False)
    k3 = pm.current_forecaster_key()
    assert k3.startswith("v3|") and k3 != base
    monkeypatch.setattr(config, "LOAD_RECENT_BIAS_ENABLED", True, raising=False)
    assert pm.current_forecaster_key().endswith("bias=1")


def test_too_few_days_falls_back_to_zero_margin(fake_band, fake_meas):
    _seed([1.0] * 13)
    info = pm.peak_band_margin(as_of_day=AS_OF, force=True)
    assert info["fallback"] == "too_few_days" and info["margin_kwh"] == 0.0 and info["n_days"] == 13
    assert pm.band_margin_groups([], [], info["margin_kwh"]) is None


def test_cap_and_non_negative_clamp(fake_band, fake_meas):
    _seed([3.0] * 16)
    config._overrides["LP_PEAK_MARGIN_MAX_KWH"] = 2.0
    assert pm.peak_band_margin(as_of_day=AS_OF, force=True)["margin_kwh"] == pytest.approx(2.0)
    pm.clear_cache()
    conn = db.get_connection()
    conn.execute("DELETE FROM load_error_log")
    conn.commit()
    conn.close()
    _seed([-0.8] * 16)
    assert pm.peak_band_margin(as_of_day=AS_OF, force=True)["margin_kwh"] == 0.0


def test_day_errors_are_cached_complete_days_never_recomputed(fake_band, fake_meas):
    _seed([0.5] * 16)
    pm.peak_band_error_history(AS_OF, 42)
    n1 = fake_meas["n"]
    assert n1 >= 1
    pm.peak_band_error_history(AS_OF, 42)
    assert fake_meas["n"] == n1          # nothing left to compute


# ---- rebuild path (off the solve path) ---------------------------------------------

def test_ensure_history_rebuilds_only_days_missing_from_the_log(fake_band, monkeypatch):
    for d in (AS_OF - timedelta(days=2), AS_OF - timedelta(days=3)):
        _seed_base(d, 3.0)               # present
    calls: list[date] = []
    monkeypatch.setattr(db, "rebuild_load_error_log_for_date", lambda u: calls.append(u) or 6)
    n = pm.ensure_history(AS_OF, 14)
    got = {c.isoformat() for c in calls}
    assert (AS_OF - timedelta(days=2)).isoformat() not in got
    assert (AS_OF - timedelta(days=3)).isoformat() not in got
    assert (AS_OF - timedelta(days=1)).isoformat() in got and len(got) == 12 and n == 12
    calls.clear()
    pm.ensure_history(AS_OF, 14, only_yesterday=True)
    assert [c.isoformat() for c in calls] == [(AS_OF - timedelta(days=1)).isoformat()]


def test_solve_path_repairs_only_yesterday(fake_band, fake_meas, monkeypatch):
    _seed([0.5] * 16)
    conn = db.get_connection()
    y = AS_OF - timedelta(days=1)
    conn.execute("DELETE FROM load_error_log WHERE slot_time_utc LIKE ?", (y.isoformat() + "%",))
    conn.commit()
    conn.close()
    rebuilt: list[date] = []
    monkeypatch.setattr(db, "rebuild_load_error_log_for_date", lambda u: rebuilt.append(u) or 0)
    pm.peak_band_error_history(AS_OF, 42)
    assert rebuilt == [y]


# ---- measured residual: the real #922 split ----------------------------------------

def _seed_day_samples(d: date, bucket: int, kw: float) -> None:
    base = datetime(d.year, d.month, d.day, bucket * 2, 0, tzinfo=LON).astimezone(UTC)
    for i in range(40):   # 3-min samples over the 2h bucket
        t = base + timedelta(minutes=3 * i, seconds=5)
        db.save_pv_realtime_sample(t.isoformat().replace("+00:00", "Z"), load_power_kw=kw)


def test_measured_residual_subtracts_measured_hp_and_skips_unmeasured_days():
    d1, d2 = date(2026, 10, 14), date(2026, 10, 15)
    for d in (d1, d2):
        _seed_day_samples(d, 8, 2.0)            # 1.0 kWh/slot, 4.0 per bucket
        _seed_day_samples(d, 9, 2.0)            # bucket 9 (18:00-20:00) has load but NO meter
    db.upsert_daikin_consumption_2hourly(
        date=d1.isoformat(), bucket_idx=8, kwh_total=1.2, kwh_heating=1.2, kwh_dhw=0.0, source="onecta_cache")
    out = db.measured_residual_by_local_slot(d1, d2)
    assert d2.isoformat() not in out                       # no measured HP -> omitted (never modelled)
    res, hp = out[d1.isoformat()][(16, 0)]
    assert hp == pytest.approx(0.3, abs=0.02) and res == pytest.approx(0.7, abs=0.02)
    # the unmetered bucket (no complete daily row) is NOT returned: HP unknown there, the
    # residual would be the full load
    assert (18, 0) not in out[d1.isoformat()] and (18, 30) not in out[d1.isoformat()]


# ---- real classifier: Cosy bands incl. DST days (no band monkeypatching) -----------

def _cosy_price(lt: datetime) -> float:
    h = lt.hour + lt.minute / 60.0
    if 4 <= h < 7 or 13 <= h < 16 or 22 <= h < 24:
        return 12.49
    if 16 <= h < 19:
        return 38.17
    return 25.45


def _seed_cosy(day: date) -> list[datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=LON).astimezone(UTC)
    end = datetime(day.year, day.month, day.day, tzinfo=LON) + timedelta(days=1)
    end = end.astimezone(UTC)
    rows, starts = [], []
    t = start
    while t < end:
        rows.append({"valid_from": t.isoformat().replace("+00:00", "Z"),
                     "valid_to": (t + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                     "value_inc_vat": _cosy_price(t.astimezone(LON))})
        starts.append(t)
        t += timedelta(minutes=30)
    db.save_agile_rates(rows, COSY)
    return starts


@pytest.mark.parametrize("day,n_slots", [
    (date(2026, 11, 3), 48), (date(2026, 10, 25), 50), (date(2026, 3, 29), 46)])
def test_real_classifier_cosy_peak_band_incl_dst(monkeypatch, day, n_slots):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", COSY)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    starts = _seed_cosy(day)
    assert len(starts) == n_slots
    prices = [_cosy_price(s.astimezone(LON)) for s in starts]
    assert pm.peak_local_slots(day) == BAND                       # history side
    groups = pm.peak_band_slot_groups(starts, prices)             # horizon side
    assert len(groups) == 1 and len(groups[0]) == 6
    assert {(starts[i].astimezone(LON).hour, starts[i].astimezone(LON).minute) for i in groups[0]} == BAND
    mg = pm.band_margin_groups(starts, prices, 1.2, band_slots=6)
    assert mg == [(groups[0], pytest.approx(1.2))]


# ---- settings -------------------------------------------------------------------

def test_runtime_settings_validate_bounds_and_default_off():
    from src import runtime_settings as rts

    assert config.LP_PEAK_MARGIN_ENABLED is False
    try:
        rts.set_setting("LP_PEAK_MARGIN_QUANTILE", 0.8, actor="test")
        assert rts.get_setting("LP_PEAK_MARGIN_QUANTILE") == pytest.approx(0.8)
        with pytest.raises(Exception):
            rts.set_setting("LP_PEAK_MARGIN_QUANTILE", 0.99, actor="test")
        with pytest.raises(Exception):
            rts.set_setting("LP_PEAK_MARGIN_WINDOW_DAYS", 7, actor="test")
    finally:
        rts.delete_setting("LP_PEAK_MARGIN_QUANTILE", actor="test")
        config._overrides.pop("LP_PEAK_MARGIN_QUANTILE", None)


def test_enabled_is_read_after_the_cache(fake_band, fake_meas):
    _seed([0.5] * 16)
    a = pm.peak_band_margin(as_of_day=AS_OF)
    assert a["enabled"] is False
    config._overrides["LP_PEAK_MARGIN_ENABLED"] = "true"
    b = pm.peak_band_margin(as_of_day=AS_OF)
    assert b["enabled"] is True and b["margin_kwh"] == a["margin_kwh"]


# ---- wire: the pessimistic scenario's base_load (max rule) ------------------------

def _inputs(n=8):
    t0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    slots = [t0 + timedelta(minutes=30 * i) for i in range(n)]
    prices = [5.0] * (n // 2) + [35.0] * (n - n // 2)
    weather = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[10.0] * n,
        shortwave_radiation_wm2=[0.0] * n, cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n, cop_space=[3.0] * n, cop_dhw=[2.5] * n)
    return slots, prices, weather


BASE = [0.4, 0.4, 0.4, 0.4, 0.3, 0.3, 0.5, 0.5]       # band nominal sum = 1.6


def _capture(monkeypatch, margins, spread=0.6):
    seen: list[list[float]] = []

    def spy(**kw):
        seen.append(list(kw["base_load_kwh"]))
        return LpPlan(ok=True, status="Optimal", objective_pence=0.0)

    monkeypatch.setattr(scenarios, "solve_lp", spy)
    slots, prices, weather = _inputs()
    scenarios.solve_scenarios(
        slot_starts_utc=slots, price_pence=prices, base_load_kwh=BASE,
        weather=weather, initial=LpInitialState(soc_kwh=2.0, tank_temp_c=52.0),
        tz=LON, base_load_spread=[spread] * len(BASE), scenarios=("pessimistic",),
        pessimistic_band_margins=margins)
    return seen[0]


def test_wire_band_is_nominal_plus_margin_by_nominal_shape_when_it_wins(monkeypatch):
    # spread 0.6/slot -> spread band sum 2.4; nominal + margin = 1.6 + 1.5 = 3.1 wins
    pess = _capture(monkeypatch, [([4, 5, 6, 7], 1.5)])
    assert pess[:4] == pytest.approx([0.6] * 4)         # outside the band: legacy spread
    assert sum(pess[4:]) == pytest.approx(sum(BASE[4:]) + 1.5)
    for i in range(4, 8):                                # proportional to the nominal shape
        assert pess[i] == pytest.approx((sum(BASE[4:]) + 1.5) * BASE[i] / sum(BASE[4:]))


def test_wire_never_lowers_the_legacy_pessimistic_band_and_is_continuous(monkeypatch):
    legacy = _capture(monkeypatch, None)
    small = _capture(monkeypatch, [([4, 5, 6, 7], 0.3)])    # 1.6 + 0.3 < 2.4 summed spread
    zero = _capture(monkeypatch, [([4, 5, 6, 7], 0.0)])
    assert small == legacy == zero
    edge = _capture(monkeypatch, [([4, 5, 6, 7], 0.8)])      # exactly the summed spread
    assert sum(edge[4:]) == pytest.approx(sum(legacy[4:]))


def test_kill_switch_off_is_identical(monkeypatch):
    assert _capture(monkeypatch, None) == _capture(monkeypatch, []) == [0.6] * 8


def test_margin_raises_pessimistic_soc_at_peak_entry_real_lp():
    slots, prices, weather = _inputs()
    n = len(slots)
    init = LpInitialState(soc_kwh=2.0, tank_temp_c=52.0)
    common = dict(slot_starts_utc=slots, price_pence=prices, base_load_kwh=[0.2] * n,
                  weather=weather, initial=init, tz=LON, scenarios=("pessimistic",))
    off = scenarios.solve_scenarios(**common)["pessimistic"].plan
    on = scenarios.solve_scenarios(**common, pessimistic_band_margins=[([4, 5, 6, 7], 0.9)])["pessimistic"].plan
    assert off.ok and on.ok
    assert on.soc_kwh[4] > off.soc_kwh[4] + 0.2


def test_partial_band_margin_scaled(monkeypatch):
    slots, prices, _w = _inputs()
    monkeypatch.setattr(pm, "peak_band_slot_groups", lambda s, p: [[6, 7]])
    assert pm.band_margin_groups(slots, prices, 1.2, band_slots=6) == [([6, 7], pytest.approx(0.4))]


# ---- wire: _run_optimizer_lp (switch on/off) ---------------------------------------

TARIFF = "E-1R-AGILE-TEST-PEAK-MARGIN"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_day(start: datetime) -> None:
    rows = []
    vf = start
    for _ in range(48):
        price = 35.0 if 16 <= vf.hour < 19 else (10.0 if 5 <= vf.hour < 16 else 12.0)
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, TARIFF)


def _run_optimizer(monkeypatch, enabled: bool):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    monkeypatch.setattr(config, "LP_PESS_CHARGE_FLOOR_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LP_PESS_CHARGE_FLOOR_SCOPE", "peak_entry", raising=False)
    config._overrides["LP_PEAK_MARGIN_ENABLED"] = "true" if enabled else "false"
    now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(pm, "peak_band_margin", lambda **kw: {
        "margin_kwh": 1.5, "band_slots": 6, "n_days": 20, "fallback": None, "coverage_oos_pct": 70.0})

    floors: list[list[float]] = []

    def fake_solve(**kw):
        bl = kw["base_load_kwh"]
        n = len(kw["slot_starts_utc"])
        plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0)
        plan.slot_starts_utc = list(kw["slot_starts_utc"])
        plan.price_pence = list(kw["price_pence"])
        for f in ("import_kwh", "export_kwh", "battery_charge_kwh", "battery_discharge_kwh",
                  "pv_use_kwh", "pv_curtail_kwh", "dhw_electric_kwh", "space_electric_kwh",
                  "lwt_offset_c"):
            setattr(plan, f, [0.0] * n)
        plan.temp_outdoor_c = [12.0] * n
        plan.tank_temp_c = [45.0] * (n + 1)
        # SoC stand-in: energy needed to cover ALL remaining scenario load, so a higher
        # pessimistic band load means a higher pessimistic SoC at the peak entry.
        plan.soc_kwh = [2.0 + sum(bl[i:]) for i in range(n + 1)]
        if kw.get("soc_floor_kwh") is not None:
            floors.append(list(kw["soc_floor_kwh"]))
            plan.soc_floor_applied = True
        return plan

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", fake_solve)
    monkeypatch.setattr(scenarios, "solve_lp", fake_solve)
    res = optimizer.run_optimizer(fox=None, daikin=None, trigger_reason="plan_push")
    rid = db.find_latest_optimizer_run_id()
    return floors, db.get_lp_inputs(rid) if rid else None, res


def test_optimizer_wire_margin_logged_and_floor_target_rises(monkeypatch):
    import json

    off_floors, off_inputs, _ = _run_optimizer(monkeypatch, False)
    exo_off = json.loads(off_inputs["exogenous_snapshot_json"])
    assert "peak_band_margin" not in exo_off
    assert exo_off["base_forecaster_key"] == pm.current_forecaster_key()
    pm.clear_cache()
    on_floors, on_inputs, _ = _run_optimizer(monkeypatch, True)
    exo_on = json.loads(on_inputs["exogenous_snapshot_json"])
    assert exo_on["peak_band_margin"]["margin_kwh"] == 1.5
    assert exo_on["peak_band_margin"]["applied_slots"] >= 4
    assert off_floors and on_floors, "the charge-floor re-solve never ran"
    assert max(on_floors[0]) > max(off_floors[0]) + 0.5


# ---- endpoint ---------------------------------------------------------------------

def test_endpoint_serves_cache_only_and_never_computes(monkeypatch, fake_band, fake_meas):
    from src.api.routers import pv

    async def _expected(*a, **k):
        return {"bands": []}

    monkeypatch.setattr("src.analytics.load_expected.expected_load_by_band", lambda *a, **k: {"bands": []})

    def boom(*a, **k):
        raise AssertionError("endpoint must not compute the margin")

    monkeypatch.setattr(pm, "peak_band_error_history", boom)
    out = asyncio.run(pv.get_load_expected())
    assert out["peak_margin"]["fallback"] == "not_computed"
    assert out["peak_margin"]["min_days"] == 14 and "enabled" in out["peak_margin"]


def test_endpoint_serves_the_warmed_margin(monkeypatch, fake_band, fake_meas):
    from src.api.routers import pv

    monkeypatch.setattr("src.analytics.load_expected.expected_load_by_band", lambda *a, **k: {"bands": []})
    _seed([0.5] * 16)
    pm.warm(AS_OF)  # the nightly job
    out = asyncio.run(pv.get_load_expected())
    assert out["peak_margin"]["fallback"] is None and out["peak_margin"]["n_days"] == 16
    assert out["peak_margin"]["margin_kwh"] == pytest.approx(0.5)


def test_day_with_an_unmeasured_band_slot_is_dropped(fake_band, monkeypatch):
    d = AS_OF - timedelta(days=1)
    _seed_base(d, 3.0)
    part = {k: (0.5, 0.1) for k in BAND if k not in {(18, 0), (18, 30)}}
    monkeypatch.setattr(db, "measured_residual_by_local_slot", lambda f, l: {d.isoformat(): part})
    monkeypatch.setattr(pm, "_daily_row_complete", lambda day, tz: True)
    assert pm.peak_band_error_history(AS_OF, 14) == []


def test_day_with_incomplete_daily_row_is_not_cached_and_retried_after_ttl(fake_band, fake_meas, monkeypatch):
    _seed([0.5] * 3)
    monkeypatch.setattr(pm, "_daily_row_complete", lambda day, tz: False)
    assert pm.peak_band_error_history(AS_OF, 14) == []
    assert pm._day_cache == {}
    monkeypatch.setattr(pm, "_daily_row_complete", lambda day, tz: True)
    assert pm.peak_band_error_history(AS_OF, 14) == []       # inside the 1 h negative TTL
    for k in list(pm._day_miss):
        pm._day_miss[k] -= pm._NEG_TTL_S + 1
    assert len(pm.peak_band_error_history(AS_OF, 14)) == 3


def _seed_run(run_id: int, run_at: datetime, key: str, status: str, resid: list[float] | None,
              slots: list[datetime] | None = None) -> None:
    import json
    exo = {"base_forecaster_key": key}
    if resid is not None:
        exo["base_load_components"] = {"residual_profile_kwh": resid}
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc, exogenous_snapshot_json, lp_status) VALUES (?,?,?,?)",
            (run_id, run_at.strftime("%Y-%m-%dT%H:%M:%SZ"), json.dumps(exo), status))
        for i, t in enumerate(slots or []):
            conn.execute("INSERT INTO lp_solution_snapshot (run_id, slot_index, slot_time_utc) VALUES (?,?,?)",
                         (run_id, i, t.astimezone(UTC).isoformat()))
        conn.commit()
    finally:
        conn.close()


def test_base_prefers_committed_pre_appliance_residual(fake_band, fake_meas):
    d = AS_OF - timedelta(days=1)
    _seed_base(d, 3.6)        # log base (includes an appliance run) 3.6
    RESID[d.isoformat()] = 3.0
    band_utc = [datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC) for (h, m) in sorted(BAND)]
    _seed_run(1, datetime(d.year, d.month, d.day, 6, 0, tzinfo=UTC), "v2|bias=0", "Optimal",
              [0.4] * len(band_utc), band_utc)          # pre-appliance residual: 6 x 0.4 = 2.4
    h = pm.peak_band_error_history(AS_OF, 14)
    assert len(h) == 1 and h[0]["base_src"] == "pre_appliance"
    assert h[0]["base_kwh"] == pytest.approx(2.4) and h[0]["error_kwh"] == pytest.approx(0.6)


def test_base_falls_back_to_log_without_pre_appliance_residual(fake_band, fake_meas):
    d = AS_OF - timedelta(days=1)
    _seed_base(d, 3.6)
    RESID[d.isoformat()] = 3.0
    _seed_run(1, datetime(d.year, d.month, d.day, 6, 0, tzinfo=UTC), "v2|bias=0", "Optimal", None)
    h = pm.peak_band_error_history(AS_OF, 14)
    assert h[0]["base_src"] == "log" and h[0]["error_kwh"] == pytest.approx(-0.6)


def test_day_runs_prefer_optimal_over_a_later_infeasible_run():
    d = AS_OF - timedelta(days=1)
    _seed_run(1, datetime(d.year, d.month, d.day, 6, 0, tzinfo=UTC), "keyA", "Optimal", None)
    _seed_run(2, datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC), "keyB", "Infeasible", None)
    assert pm.day_runs(d, d)[d.isoformat()] == ("keyA", 1)
    conn = db.get_connection()
    conn.execute("DELETE FROM lp_inputs_snapshot WHERE run_id = 1")
    conn.commit()
    conn.close()
    assert pm.day_runs(d, d)[d.isoformat()] == ("keyB", 2)      # only an infeasible one: still used


def test_load_error_log_is_kept_90_days_by_the_nightly_prune():
    old = datetime.now(UTC) - timedelta(days=60)
    recent_pruned = datetime.now(UTC) - timedelta(days=120)
    conn = db.get_connection()
    for t in (old, recent_pruned):
        conn.execute("INSERT OR REPLACE INTO load_error_log (slot_time_utc, forecast_kwh, built_at_utc) VALUES (?,?,?)",
                     (t.strftime("%Y-%m-%dT%H:%M:%SZ"), 1.0, "x"))
    conn.commit()
    conn.close()
    db.prune_history_tables()
    conn = db.get_connection()
    left = {r[0] for r in conn.execute("SELECT slot_time_utc FROM load_error_log").fetchall()}
    conn.close()
    assert old.strftime("%Y-%m-%dT%H:%M:%SZ") in left
    assert recent_pruned.strftime("%Y-%m-%dT%H:%M:%SZ") not in left
