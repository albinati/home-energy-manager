"""#923 peak-band conformal margin: error history + quantile, away exclusion,
too-few-days fallback, cap, the pessimistic base_load wire, kill switch."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.analytics import peak_margin as pm
from src.config import config
from src.scheduler import scenarios
from src.scheduler.lp_optimizer import LpInitialState, LpPlan
from src.weather import WeatherLpSeries

LON = ZoneInfo("Europe/London")
BAND = {(16, 0), (16, 30), (17, 0), (17, 30), (18, 0), (18, 30)}
KEYS = ("LP_PEAK_MARGIN_ENABLED", "LP_PEAK_MARGIN_QUANTILE",
        "LP_PEAK_MARGIN_WINDOW_DAYS", "LP_PEAK_MARGIN_MAX_KWH")
AS_OF = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def _init(monkeypatch):
    db.init_db()
    pm.clear_cache()
    monkeypatch.setattr(pm, "peak_local_slots", lambda day, tz=None: set(BAND))
    monkeypatch.setattr(db, "_hard_away_dates", lambda cutoff, tz: (set(), set(), set()))
    conn = db.get_connection()
    conn.execute("DELETE FROM load_error_log")
    conn.commit()
    conn.close()
    yield
    pm.clear_cache()
    for k in KEYS:
        config._overrides.pop(k, None)


def _seed_err(d: date, band_error: float, fc_per_slot: float = 0.5) -> None:
    """Six load_error_log rows for ``d``'s band: forecast 0.5/slot, actual such
    that the band-sum error == band_error."""
    conn = db.get_connection()
    try:
        for (h, m) in sorted(BAND):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC)
            act = fc_per_slot + band_error / len(BAND)
            conn.execute(
                "INSERT OR REPLACE INTO load_error_log "
                "(slot_time_utc, forecast_kwh, forecast_base_kwh, actual_kwh, error_kwh, built_at_utc) "
                "VALUES (?,?,?,?,?,?)",
                (t.strftime("%Y-%m-%dT%H:%M:%SZ"), fc_per_slot, fc_per_slot, act,
                 act - fc_per_slot, "2026-11-02T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_days(errors: list[float]) -> list[date]:
    days = [AS_OF - timedelta(days=i + 1) for i in range(len(errors))]
    for d, e in zip(days, errors):
        _seed_err(d, e)
    return days


def test_history_is_band_sum_of_actual_minus_committed_total():
    _seed_days([0.4, 1.0, -0.2])
    hist = pm.peak_band_error_history(AS_OF, 42)
    assert sorted(round(h["error_kwh"], 6) for h in hist) == [-0.2, 0.4, 1.0]
    assert all(h["n_slots"] == 6 for h in hist)
    h0 = hist[0]
    assert h0["forecast_kwh"] == pytest.approx(3.0)
    assert h0["actual_kwh"] == pytest.approx(3.0 + h0["error_kwh"])


def test_quantile_and_margin_and_coverage():
    errs = [i / 10.0 for i in range(1, 21)]  # 0.1 .. 2.0
    _seed_days(errs)
    config._overrides["LP_PEAK_MARGIN_QUANTILE"] = 0.75
    config._overrides["LP_PEAK_MARGIN_MAX_KWH"] = 5.0
    info = pm.peak_band_margin(as_of_day=AS_OF, force=True)
    assert info["fallback"] is None
    assert info["n_days"] == 20
    assert info["margin_kwh"] == pytest.approx(pm.quantile(errs, 0.75), abs=1e-3)
    assert info["margin_kwh"] == pytest.approx(1.525, abs=1e-3)
    # coverage = share of days with error <= margin: 15 of 20
    assert info["coverage_pct"] == pytest.approx(75.0)


def test_away_return_and_non_normal_days_are_excluded(monkeypatch):
    days = _seed_days([0.5] * 12 + [9.0, 9.0, 9.0])  # 3 outliers
    bad = {d.isoformat() for d in days[12:]}
    monkeypatch.setattr(
        db, "_hard_away_dates",
        lambda cutoff, tz: ({days[12].isoformat()}, {days[13].isoformat()}, {days[14].isoformat()}))
    hist = pm.peak_band_error_history(AS_OF, 42)
    assert len(hist) == 12
    assert not ({h["date"] for h in hist} & bad)
    config._overrides["LP_PEAK_MARGIN_MAX_KWH"] = 5.0
    assert pm.peak_band_margin(as_of_day=AS_OF, force=True)["margin_kwh"] == pytest.approx(0.5)


def test_too_few_days_falls_back_to_zero_margin():
    _seed_days([1.0] * 9)
    info = pm.peak_band_margin(as_of_day=AS_OF, force=True)
    assert info["fallback"] == "too_few_days"
    assert info["margin_kwh"] == 0.0
    assert pm.band_extra_by_slot([], [], [], info["margin_kwh"]) is None


def test_cap_and_non_negative_clamp():
    _seed_days([3.0] * 12)
    config._overrides["LP_PEAK_MARGIN_MAX_KWH"] = 2.0
    assert pm.peak_band_margin(as_of_day=AS_OF, force=True)["margin_kwh"] == pytest.approx(2.0)
    pm.clear_cache()
    conn = db.get_connection()
    conn.execute("DELETE FROM load_error_log")
    conn.commit()
    conn.close()
    _seed_days([-0.8] * 12)   # forecast always too high -> negative quantile
    assert pm.peak_band_margin(as_of_day=AS_OF, force=True)["margin_kwh"] == 0.0


def test_runtime_settings_validate_bounds():
    from src import runtime_settings as rts

    rts.set_setting("LP_PEAK_MARGIN_QUANTILE", 0.8, actor="test")
    assert rts.get_setting("LP_PEAK_MARGIN_QUANTILE") == pytest.approx(0.8)
    with pytest.raises(Exception):
        rts.set_setting("LP_PEAK_MARGIN_QUANTILE", 0.99, actor="test")
    with pytest.raises(Exception):
        rts.set_setting("LP_PEAK_MARGIN_WINDOW_DAYS", 7, actor="test")


def test_default_is_off():
    assert config.LP_PEAK_MARGIN_ENABLED is False


# --- wire: the pessimistic scenario's base_load ---------------------------------

def _inputs(n=8):
    t0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    slots = [t0 + timedelta(minutes=30 * i) for i in range(n)]
    prices = [5.0] * (n // 2) + [35.0] * (n - n // 2)
    weather = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[10.0] * n,
        shortwave_radiation_wm2=[0.0] * n, cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[0.0] * n, cop_space=[3.0] * n, cop_dhw=[2.5] * n)
    return slots, prices, weather


BASE = [0.4, 0.4, 0.4, 0.4, 0.3, 0.3, 0.5, 0.5]


def _capture(monkeypatch, extra):
    seen: list[list[float]] = []

    def spy(**kw):
        seen.append(list(kw["base_load_kwh"]))
        return LpPlan(ok=True, status="Optimal", objective_pence=0.0)

    monkeypatch.setattr(scenarios, "solve_lp", spy)
    slots, prices, weather = _inputs()
    scenarios.solve_scenarios(
        slot_starts_utc=slots, price_pence=prices, base_load_kwh=BASE,
        weather=weather, initial=LpInitialState(soc_kwh=2.0, tank_temp_c=52.0),
        tz=LON, base_load_spread=[0.6] * len(BASE), scenarios=("pessimistic",),
        pessimistic_band_extra_kwh=extra)
    return seen[0]


def test_wire_pessimistic_band_slots_equal_nominal_plus_margin(monkeypatch):
    slots, prices, _w = _inputs()
    monkeypatch.setattr(pm, "peak_band_slot_groups", lambda s, p: [[4, 5, 6, 7]])
    extra = pm.band_extra_by_slot(slots, prices, BASE, 1.0)
    assert sum(x for x in extra if x is not None) == pytest.approx(1.0)
    assert extra[:4] == [None] * 4
    assert extra[4] == pytest.approx(1.0 * 0.3 / 1.6)   # proportional to the nominal shape
    pess = _capture(monkeypatch, extra)
    for i in range(4):       # outside the band: the legacy spread (p75 0.6)
        assert pess[i] == pytest.approx(0.6)
    for i in range(4, 8):    # in the band: nominal + share of the margin, NOT the spread
        assert pess[i] == pytest.approx(BASE[i] + extra[i])
    assert sum(pess[4:]) == pytest.approx(sum(BASE[4:]) + 1.0)


def test_kill_switch_off_is_identical(monkeypatch):
    pess_none = _capture(monkeypatch, None)
    pess_empty = _capture(monkeypatch, [None] * 8)
    assert pess_none == pess_empty == [0.6] * 8     # legacy spread only


def test_margin_raises_pessimistic_soc_at_peak_entry_real_lp():
    """Real LP: the band margin makes the pessimistic scenario enter the peak
    with a fuller battery (the charge floor is read from that trajectory)."""
    slots, prices, weather = _inputs()
    n = len(slots)
    init = LpInitialState(soc_kwh=2.0, tank_temp_c=52.0)
    common = dict(slot_starts_utc=slots, price_pence=prices, base_load_kwh=[0.2] * n,
                  weather=weather, initial=init, tz=LON, scenarios=("pessimistic",))
    off = scenarios.solve_scenarios(**common)["pessimistic"].plan
    on = scenarios.solve_scenarios(
        **common, pessimistic_band_extra_kwh=[None] * 4 + [0.6 / 4] * 4)["pessimistic"].plan
    assert off.ok and on.ok
    assert on.soc_kwh[4] > off.soc_kwh[4] + 0.2


def test_partial_band_margin_scaled(monkeypatch):
    slots, prices, _w = _inputs()
    monkeypatch.setattr(pm, "peak_band_slot_groups", lambda s, p: [[6, 7]])
    extra = pm.band_extra_by_slot(slots, prices, [0.5] * 8, 1.2, band_slots=6)
    assert sum(x for x in extra if x is not None) == pytest.approx(1.2 * 2 / 6)
