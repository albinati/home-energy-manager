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
