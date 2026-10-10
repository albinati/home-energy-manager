"""#853 — earliest start within a total-cost tolerance."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config
from src.scheduler import appliance_dispatch as ad
from tests.scheduler.test_appliance_battery_aware import (  # noqa: F401
    _isolated, _seed_appliance, _seed_lp_trajectory,
)

# Prod 2026-10-10: effective cost by start, per 30-min slot of the appliance's
# own energy (see build_marginal_cost_per_slot docstring), local = UTC+1.
BASE = datetime(2026, 10, 10, 8, 30, tzinfo=UTC)  # 09:30 local


def _today_marginal() -> dict[datetime, float]:
    # Hourly effective p/kWh from the issue; per-slot value constant per hour
    # block is enough: one value per slot, equal to the table at that start.
    table = {9.5: 4.15, 10.5: 3.8, 11.5: 3.09, 12.5: 2.39, 13.5: 2.73, 14.5: 4.13,
             15.5: 5.53, 16.5: 5.88, 17.5: 5.19, 18.5: 4.5, 19.5: 3.8, 20.5: 3.09,
             21.5: 2.46, 22.5: 2.04}
    out = {}
    for i in range(0, 40):
        s = BASE + timedelta(minutes=30 * i)
        lh = (s.hour + 1) + s.minute / 60
        key = (int(lh) + 0.5) if lh >= 9.5 else 9.5
        key = min(max(key, 9.5), 22.5)
        out[s] = table[key]
    return out


def _run(marginal, tol, monkeypatch, *, kw=0.327, dur=163, hours=21, aid=None):
    monkeypatch.setattr(config, "APPLIANCE_EARLY_START_TOLERANCE_PENCE", tol, raising=False)
    aid = aid or _seed_appliance(typical_kw=kw)
    slots = sorted(marginal)
    _seed_lp_trajectory(slots, [0.5] * len(slots))  # battery cannot cover
    monkeypatch.setattr(ad, "_now_utc", lambda: BASE - timedelta(minutes=30))
    return ad.find_battery_aware_window(
        earliest_start_utc=BASE, deadline_utc=BASE + timedelta(hours=hours),
        duration_minutes=dur, appliance_id=aid, typical_kw=kw,
        marginal_cost_per_slot=marginal,
    ), aid


def test_todays_case_picks_earliest_within_tolerance(monkeypatch):
    m = _today_marginal()
    (start, _e, _p), aid = _run(m, 10.0, monkeypatch)
    assert start == BASE
    choice = ad._window_choice[aid]
    assert choice["cheapest_start"] != choice["chosen_start"]
    assert 0 < choice["chosen_total_p"] - choice["cheapest_total_p"] <= 10
    txt = ad._tradeoff_text(choice, ad.ZoneInfo("Europe/London"))
    assert txt.startswith("rodo às 09:30") and "esperar até" in txt and "economizaria" in txt


def test_tolerance_zero_is_legacy_cheapest(monkeypatch):
    m = _today_marginal()
    (start, _e, _p), _ = _run(m, 0.0, monkeypatch)
    # 163 min -> 6 slots; cheapest 6-slot sum starts 22:00 local = 21:00Z
    assert start == datetime(2026, 10, 10, 21, 0, tzinfo=UTC)  # 22:00 local, as in prod


def test_waits_for_cheap_band_when_saving_exceeds_tolerance(monkeypatch):
    # Day band 25.45p now, cheap 12.49p later, 1.2 kWh grid-only over 2 slots.
    kw = 1.2  # 60 min -> 1.2 kWh, 0.6 kWh/slot
    n = 16
    m = {}
    for i in range(n):
        s = BASE + timedelta(minutes=30 * i)
        m[s] = 0.6 * (12.49 if i >= 8 else 25.45)
    (start, _e, _p), _ = _run(m, 10.0, monkeypatch, kw=kw, dur=60, hours=8)
    # Starting 12:00 (one slot still in the day band) saves 7.8p of the 15.6p -> inside
    # tolerance, so it is the EARLIEST acceptable start; starting NOW is not.
    assert start == BASE + timedelta(hours=3, minutes=30)
    assert start > BASE


def test_max_delay_hours_caps_wait(monkeypatch):
    m = {}
    for i in range(48):
        s = BASE + timedelta(minutes=30 * i)
        m[s] = 3.0 if i < 8 else 2.0  # first 4 h cost 6p more per 6-slot cycle
    (free, _e, _p), aid = _run(dict(m), 2.0, monkeypatch, dur=180, hours=23)
    assert free > BASE  # 6p saving > 2p tolerance -> waits
    monkeypatch.setattr(config, "APPLIANCE_MAX_DELAY_HOURS", 2.0, raising=False)
    from src import db
    import sqlite3
    c = sqlite3.connect(config.DB_PATH); c.execute("DELETE FROM lp_solution_snapshot"); c.commit(); c.close()
    (capped, _e, _p), _ = _run(dict(m), 2.0, monkeypatch, dur=180, hours=23, aid=aid)
    assert capped == BASE + timedelta(hours=2)  # cheapest window inside the cap


def test_arm_notification_carries_tradeoff():
    from unittest.mock import patch

    from src.notifier import notify_appliance_armed
    with patch("src.notifier._dispatch") as d:
        notify_appliance_armed(
            appliance_name="Washer", planned_start_local="Sat 09:30",
            planned_end_local="12:13", deadline_local="07:00",
            duration_minutes=163, avg_price_pence=4.1,
            tradeoff="lavo às 09:30 — esperar até 22:00 economizaria 2p",
        )
    args, kwargs = d.call_args
    assert "esperar até 22:00 economizaria 2p" in args[1]
    assert kwargs["extra"]["tradeoff"]


def _peak_map():
    m = ad.MarginalCostMap()
    base = datetime(2026, 10, 10, 13, 0, tzinfo=UTC)  # 14:00 local
    peaks = set()
    for i in range(0, 36):
        s = base + timedelta(minutes=30 * i)
        lh = ((s.hour + 1) % 24) + s.minute / 60
        if 16 <= lh < 19:
            m[s] = 0.1635 * 38.17
            peaks.add(s)
        elif lh >= 22 or lh < 7:
            m[s] = 0.1635 * 12.49
        else:
            m[s] = 0.1635 * 25.45
    m.peak_slots = frozenset(peaks)
    return base, m


def test_grid_run_never_enters_peak_within_tolerance(monkeypatch):
    base, m = _peak_map()
    monkeypatch.setattr(config, "APPLIANCE_EARLY_START_TOLERANCE_PENCE", 10.0, raising=False)
    aid = _seed_appliance(typical_kw=0.327)
    _seed_lp_trajectory(sorted(m), [0.5] * len(m))  # battery cannot cover
    monkeypatch.setattr(ad, "_now_utc", lambda: base - timedelta(minutes=30))
    start, _end, _p = ad.find_battery_aware_window(
        earliest_start_utc=base, deadline_utc=base + timedelta(hours=17),
        duration_minutes=163, appliance_id=aid, typical_kw=0.327,
        marginal_cost_per_slot=m,
    )
    assert start != base
    assert not any(s in m.peak_slots for s in (start + timedelta(minutes=30 * k) for k in range(6)))
    s2, _e2, _p2 = ad._cheapest_from_marginal_cost(m, base, base + timedelta(hours=17), 163)
    assert not any(s in m.peak_slots for s in (s2 + timedelta(minutes=30 * k) for k in range(6)))


def test_battery_covered_run_may_overlap_peak(monkeypatch):
    base, m = _peak_map()
    monkeypatch.setattr(config, "APPLIANCE_EARLY_START_TOLERANCE_PENCE", 10.0, raising=False)
    aid = _seed_appliance(typical_kw=0.327)
    _seed_lp_trajectory(sorted(m), [9.0] * len(m))  # battery covers everything
    monkeypatch.setattr(ad, "_now_utc", lambda: base - timedelta(minutes=30))
    start, _e, _p = ad.find_battery_aware_window(
        earliest_start_utc=base, deadline_utc=base + timedelta(hours=17),
        duration_minutes=163, appliance_id=aid, typical_kw=0.327,
        marginal_cost_per_slot=m,
    )
    assert start == base
