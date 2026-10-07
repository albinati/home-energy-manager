"""Cosy (banded tariff) end-to-end through the consumers rewired in #804.

Each test feeds a real 48-slot Cosy day (cheap 04-07 / 13-16 / 22-24, peak
16-19, day otherwise) into the consumer that used to misread it and checks the
band lands where the household expects it: the DAY band is never a peak, the
CHEAP band is cheap, the PEAK band is 16-19 only.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db, dhw_policy
from src.config import config as app_config
from src.scheduler.lp_dispatch import lp_plan_to_slots
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

COSY_CHEAP, COSY_DAY, COSY_PEAK = 12.4868, 25.4461, 38.174
TZ = ZoneInfo("Europe/London")
DAY = date(2026, 10, 14)  # a Wednesday, BST still in force


def _cosy_price(h: int) -> float:
    if 4 <= h < 7 or 13 <= h < 16 or h >= 22:
        return COSY_CHEAP
    if 16 <= h < 19:
        return COSY_PEAK
    return COSY_DAY


def _cosy_day(d: date) -> tuple[list[datetime], list[float]]:
    starts, prices = [], []
    for h in range(24):
        for m in (0, 30):
            s = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).astimezone(UTC)
            starts.append(s)
            prices.append(_cosy_price(h))
    return starts, prices


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_STRUCTURE", "auto", raising=False)
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OPTIMIZATION_PEAK_THRESHOLD_PENCE", 25.0)
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 20)
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    db.init_db()


# ── LP + dispatch labels ─────────────────────────────────────────────────────


def test_cosy_day_thresholds_leave_day_band_standard():
    """The LP's thresholds on a Cosy day are the band midpoints, and the
    per-slot ``price_band`` carries the 16/26/6 split for dispatch."""
    starts, prices = _cosy_day(DAY)
    n = len(starts)
    w = WeatherLpSeries(
        slot_starts_utc=starts,
        temperature_outdoor_c=[9.0] * n,
        shortwave_radiation_wm2=[150.0] * n,
        cloud_cover_pct=[60.0] * n,
        pv_kwh_per_slot=[0.2] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.6] * n,
    )
    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=[0.4] * n,
        weather=w,
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=44.0),
        tz=TZ,
    )
    assert plan.ok, plan.status
    assert plan.tariff_structure_kind == "banded"
    assert COSY_CHEAP < plan.cheap_threshold_pence < COSY_DAY
    assert COSY_DAY < plan.peak_threshold_pence < COSY_PEAK
    assert plan.price_band.count("cheap") == 16
    assert plan.price_band.count("standard") == 26
    assert plan.price_band.count("peak") == 6

    # Dispatch labels: the DAY band can never be "peak" (it used to be, which
    # drove 13 h of tank shutdown + LWT setback); the PEAK band is never cheap.
    slots = lp_plan_to_slots(plan)
    assert len(slots) == n
    for s, p in zip(slots, prices):
        if p == COSY_DAY:
            assert s.kind != "peak", f"day-band slot {s.start_utc} labelled peak"
            assert s.kind != "cheap"
        if p == COSY_PEAK:
            assert s.kind not in ("cheap", "negative")
    # and at least one cheap-band slot is a ForceCharge candidate from 4 kWh start
    assert any(s.kind == "cheap" for s, p in zip(slots, prices) if p == COSY_CHEAP)


# ── DHW dynamic window: evening peak entry ───────────────────────────────────


def test_cosy_evening_peak_entry_hour_is_16(monkeypatch):
    """Before #804, q75 == median on a Cosy day → ``None`` → static fallback.
    The band structure identifies 38.17p as the peak level → 16:00."""
    monkeypatch.setattr(app_config, "OPTIMIZATION_PEAK_THRESHOLD_PENCE", 27.0)
    starts, prices = _cosy_day(DAY)
    price_map = dict(zip(starts, prices))
    assert dhw_policy._evening_peak_entry_hour(DAY, price_map) == 16


# ── heartbeat: slot_kind + low-SoC alert threshold ───────────────────────────


def test_heartbeat_slot_kind_cheap_on_cosy_cheap_band():
    """The heartbeat classifies with strict ``<`` / ``>`` against the stored
    daily_target thresholds. With band midpoints stored, the cheap band reads
    "cheap" and the day band does NOT read "peak"."""
    from src.energy.tariff_structure import detect
    from src.scheduler.runner import _peak_alert_threshold_p

    _, prices = _cosy_day(DAY)
    s = detect(prices)
    db.save_daily_target({
        "date": DAY.isoformat(), "cheap_threshold": s.cheap_thr, "peak_threshold": s.peak_thr,
    })
    tgt = db.get_daily_target(DAY)
    assert tgt is not None

    def heartbeat_kind(price: float) -> str:
        # verbatim from bulletproof_heartbeat_tick
        if price < 0:
            return "negative"
        if price > float(tgt.get("peak_threshold") or 99):
            return "peak"
        if price < float(tgt.get("cheap_threshold") or 0):
            return "cheap"
        return "standard"

    assert heartbeat_kind(COSY_CHEAP) == "cheap"
    assert heartbeat_kind(COSY_DAY) == "standard"
    assert heartbeat_kind(COSY_PEAK) == "peak"

    # Low-SoC alert threshold: the day's stored peak threshold, not the static
    # 25p that the 25.45p day band cleared every afternoon.
    thr = _peak_alert_threshold_p(DAY.isoformat())
    assert COSY_DAY < thr < COSY_PEAK


def test_peak_alert_threshold_falls_back_to_static_without_target():
    from src.scheduler.runner import _peak_alert_threshold_p

    assert _peak_alert_threshold_p((DAY + timedelta(days=200)).isoformat()) == 25.0


# ── review follow-ups (H1/H2, M1/M2/M3) ──────────────────────────────────────


def test_agile_low_soc_alert_threshold_stays_static(monkeypatch):
    """H1: on Agile the heartbeat alert must keep the static 25p even when the
    stored daily_target carries the LP's q75."""
    from src.scheduler.runner import _peak_alert_threshold_p

    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-H")
    db.save_daily_target({"date": DAY.isoformat(), "cheap_threshold": 14.0, "peak_threshold": 36.0})
    assert _peak_alert_threshold_p(DAY.isoformat()) == 25.0
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    assert _peak_alert_threshold_p(DAY.isoformat()) == 36.0


def test_prefer_plan_thresholds_gate(monkeypatch):
    """H2: static-cut-off consumers switch to plan thresholds only on a
    TOU-family tariff (or when forced), never on Agile."""
    from src.energy.tariff_structure import prefer_plan_thresholds

    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-H")
    assert prefer_plan_thresholds() is False
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    assert prefer_plan_thresholds() is True
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_STRUCTURE", "dynamic", raising=False)
    assert prefer_plan_thresholds() is False
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_STRUCTURE", "banded", raising=False)
    assert prefer_plan_thresholds("E-1R-AGILE-24-10-01-H") is True


def test_reprice_horizon_stays_banded():
    """M1: a 48 h horizon spanning an Octopus reprice (6 raw levels) is still
    three bands; the day band is still never a peak."""
    from src.energy.tariff_structure import classify, detect

    _, today = _cosy_day(DAY)
    tomorrow = [p * 1.04 for p in today]
    s = detect(today + tomorrow)
    assert s.is_banded and s.has_cheap and s.has_peak
    kinds = classify(today + tomorrow)
    assert kinds.count("cheap") == 32 and kinds.count("peak") == 12 and kinds.count("standard") == 52


def test_two_level_tie_makes_the_higher_level_the_peak():
    """M2: pinned semantics — on a 50/50 two-level window the dear level is the
    peak (never import at the dear level by mistake)."""
    from src.energy.tariff_structure import detect

    s = detect([COSY_CHEAP] * 12 + [COSY_PEAK] * 12)
    assert s.has_peak and not s.has_cheap


def test_agile_plunge_day_with_few_positive_prices_is_dynamic():
    """M3: a plunge day with four positive prices among 40 negatives must not
    read as banded."""
    from src.energy.tariff_structure import detect

    assert detect([-1.0] * 40 + [5.0, 6.0, 7.0, 8.0] * 2).kind == "dynamic"


# ── remaining consumers on a Cosy day ────────────────────────────────────────


def test_legacy_classify_slots_on_cosy_day_with_solar_skip():
    from src.scheduler.optimizer import HalfHourSlot, _classify_slots
    from src.weather import HourlyForecast

    starts, prices = _cosy_day(DAY)
    slots = [HalfHourSlot(start_utc=s, end_utc=s + timedelta(minutes=30), price_pence=p, kind="standard")
             for s, p in zip(starts, prices)]
    # PV > 2 kW over the 13-16 local band → cheap becomes standard (solar skip)
    fc = [HourlyForecast(time_utc=s, temperature_c=10.0, cloud_cover_pct=10.0, shortwave_radiation_wm2=500.0,
                         estimated_pv_kw=3.0 if 12 <= s.astimezone(TZ).hour < 16 else 0.0,
                         heating_demand_factor=0.5)
          for s in starts if s.minute == 0]
    _classify_slots(slots, fc)
    kinds = [s.kind for s in slots]
    assert kinds.count("peak") == 6
    assert all(k != "peak" for k, p in zip(kinds, prices) if p == COSY_DAY)
    assert kinds.count("cheap") == 16 - 6  # 13-16 band (6 slots) skipped for solar


def test_api_classify_tariff_kinds_cosy_and_agile_identity():
    import random

    from src.api.main import _classify_tariff_kinds

    _, prices = _cosy_day(DAY)
    rows = [{"p": p} for p in prices]
    _classify_tariff_kinds(rows)
    kinds = [r["kind"] for r in rows]
    assert kinds.count("cheap") == 16 and kinds.count("standard") == 26 and kinds.count("peak") == 6

    rnd = random.Random(7)
    agile = [round(rnd.uniform(-3, 40), 2) for _ in range(48)]
    rows = [{"p": p} for p in agile]
    _classify_tariff_kinds(rows)
    sp = sorted(agile)
    n = len(sp)
    cheap_thr = min(sum(sp) / n * 0.85, sp[max(0, n // 4 - 1)])
    peak_thr = max(sp[min(n - 1, (3 * n) // 4)], 25.0)
    for r in rows:
        p = float(r["p"])
        exp = "negative" if p <= 0 else "cheap" if p < cheap_thr else "peak" if p > peak_thr else "standard"
        assert r["kind"] == exp


def _save_cosy_rows(code: str, d: date) -> None:
    starts, prices = _cosy_day(d)
    db.save_agile_rates([{
        "valid_from": s.isoformat().replace("+00:00", "Z"),
        "valid_to": (s + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "value_inc_vat": p,
    } for s, p in zip(starts, prices)], code)


def test_brief_peak_summary_on_cosy_day_is_16_to_19(monkeypatch):
    from src.analytics.daily_brief import _tariff_peak_windows_summary

    code = "E-1R-COSY-22-12-08-H"
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", code)
    _save_cosy_rows(code, DAY)
    out = _tariff_peak_windows_summary(DAY, TZ)
    assert out is not None and "16:00–19:00" in out and "6 slots" in out


def test_patterns_cheap_peak_frequency_on_cosy_days(monkeypatch):
    from src.analytics import patterns

    code = "E-1R-COSY-22-12-08-H"
    for d in (DAY, DAY + timedelta(days=1)):
        _save_cosy_rows(code, d)
    out = patterns.cheap_peak_slot_frequency(code, DAY.isoformat(), (DAY + timedelta(days=1)).isoformat())
    assert out["kinds"]["cheap"]["count"] == 32 and out["kinds"]["peak"]["count"] == 12


def test_plan_window_fills_tail_from_band_profile_across_dst(monkeypatch):
    """A COSY code with real rows ending at local midnight after the 2026-10-25
    fall-back: the 48 h tail is synthesised from the LOCAL-clock band profile —
    04:00 LOCAL on 26 Oct (= 04:00Z, GMT) is cheap, 16:00 LOCAL is peak."""
    from src.scheduler import optimizer
    from src.scheduler.optimizer import _resolve_plan_window

    code = "E-1R-COSY-22-12-08-H"
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", code)
    monkeypatch.setattr(app_config, "LP_HORIZON_HOURS", 48)
    for d in (date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)):
        _save_cosy_rows(code, d)
    now = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    w = _resolve_plan_window(code)
    assert w is not None
    synth = [r for r in w.rates if r.get("fetched_at") == "prior"]
    assert synth and all(r.get("prior_source") == "prior_band" for r in synth)
    by_start = {r["valid_from"]: float(r["value_inc_vat"]) for r in synth}
    assert by_start["2026-10-26T04:00:00Z"] == pytest.approx(COSY_CHEAP, abs=0.01)   # 04:00 GMT local
    assert by_start["2026-10-26T16:00:00Z"] == pytest.approx(COSY_PEAK, abs=0.01)    # 16:00 GMT local
    assert by_start["2026-10-26T08:00:00Z"] == pytest.approx(COSY_DAY, abs=0.01)
    assert w.horizon_end == now + timedelta(hours=48, minutes=30)


# ── #810: tariff-neutral naming / filters ────────────────────────────────────


def test_smart_tariff_start_date_alias(monkeypatch):
    from src.analytics.pnl import _agile_start_date

    monkeypatch.setattr(app_config, "SMART_TARIFF_START_DATE", "2026-04-17", raising=False)
    monkeypatch.setattr(app_config, "AGILE_TARIFF_START_DATE", "", raising=False)
    assert _agile_start_date() == date(2026, 4, 17)
    monkeypatch.setattr(app_config, "SMART_TARIFF_START_DATE", "", raising=False)
    monkeypatch.setattr(app_config, "AGILE_TARIFF_START_DATE", "2026-04-20", raising=False)
    assert _agile_start_date() == date(2026, 4, 20)


def test_fair_compare_current_code_from_cosy_tariff(monkeypatch):
    from src.analytics.fair_compare import _current_product_code

    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    assert _current_product_code() == "COSY-22-12-08"


def test_agile_today_exposes_tariff_name_and_structure(monkeypatch):
    import asyncio

    from src.api.main import agile_today

    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    out = asyncio.run(agile_today())
    assert out["tariff_display_name"] == "Cosy" and out["tariff_structure"] == "banded"


def test_strategy_summary_and_brief_use_tariff_neutral_wording():
    import inspect

    from src.analytics import daily_brief
    from src.scheduler import optimizer

    assert "mean Agile" not in inspect.getsource(optimizer)
    assert "mean Agile" not in inspect.getsource(daily_brief._day_cost_forecast_line) if hasattr(daily_brief, "_day_cost_forecast_line") else True
    assert "mean import" in inspect.getsource(optimizer)


def test_export_rates_in_range_filters_by_tariff_code(monkeypatch):
    code_a, code_b = "E-1R-AGILE-OUTGOING-19-05-13-H", "E-1R-OUTGOING-FIX-12M-H"
    rows = []
    base = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
    for i in range(4):
        vf = (base + timedelta(minutes=30 * i)).isoformat().replace("+00:00", "Z")
        vt = (base + timedelta(minutes=30 * (i + 1))).isoformat().replace("+00:00", "Z")
        rows.append({"valid_from": vf, "valid_to": vt, "value_inc_vat": 10.0 + i})
    db.save_agile_export_rates(rows, code_a)
    db.save_agile_export_rates([dict(r, value_inc_vat=99.0) for r in rows], code_b)
    monkeypatch.setattr(app_config, "OCTOPUS_EXPORT_TARIFF_CODE", code_a)
    got = db.get_agile_export_rates_in_range(rows[0]["valid_from"], rows[-1]["valid_to"])
    assert len(got) == 4 and all(r["tariff_code"] == code_a for r in got)
    assert len(db.get_agile_export_rates_in_range(rows[0]["valid_from"], rows[-1]["valid_to"], tariff_code="")) == 8
