"""Banded-tariff (Cosy) calendar tiers, publisher filter, tier boundaries and
the pessimistic charge floor's peak entry (#803 / #805)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.config import config
from src.google_calendar.tiers import (
    TIER_BAND_CHEAP,
    TIER_BAND_DAY,
    TIER_BAND_PEAK,
    TIER_EXPENSIVE,
    TIER_GREEN_LIGHT,
    Slot,
    classify_day,
    format_event,
)

COSY_CHEAP, COSY_DAY, COSY_PEAK = 12.4868, 25.4461, 38.174
TZ = ZoneInfo("Europe/London")
DAY = date(2026, 10, 14)


def _price(h: int) -> float:
    if 4 <= h < 7 or 13 <= h < 16 or h >= 22:
        return COSY_CHEAP
    if 16 <= h < 19:
        return COSY_PEAK
    return COSY_DAY


def _cosy_slots(d: date) -> list[Slot]:
    out = []
    for h in range(24):
        for m in (0, 30):
            s = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).astimezone(UTC)
            out.append(Slot(start_utc=s, end_utc=s + timedelta(minutes=30), price_p=_price(h)))
    return out


def _cosy_rows(d: date) -> list[dict]:
    return [{
        "valid_from": s.start_utc.isoformat().replace("+00:00", "Z"),
        "valid_to": s.end_utc.isoformat().replace("+00:00", "Z"),
        "value_inc_vat": s.price_p,
    } for s in _cosy_slots(d)]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_STRUCTURE", "auto", raising=False)
    monkeypatch.setattr(config, "TARIFF_DISPLAY_NAME", "", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")


# ── tiers ────────────────────────────────────────────────────────────────────


def test_cosy_day_classifies_into_seven_band_windows():
    wins = classify_day(_cosy_slots(DAY))
    keys = [(w.start_utc.astimezone(TZ).hour, w.tier.key) for w in wins]
    assert keys == [
        (0, "band_day"), (4, "band_cheap"), (7, "band_day"), (13, "band_cheap"),
        (16, "band_peak"), (19, "band_day"), (22, "band_cheap"),
    ]
    # The day band is NEVER "expensive" any more, and the peak band isn't
    # "severe peak by 0.005p".
    assert all(w.tier.key not in (TIER_EXPENSIVE.key, TIER_GREEN_LIGHT.key) for w in wins)


def test_band_event_title_has_single_price_no_range():
    wins = classify_day(_cosy_slots(DAY))
    cheap = next(w for w in wins if w.tier.key == TIER_BAND_CHEAP.key)
    peak = next(w for w in wins if w.tier.key == TIER_BAND_PEAK.key)
    day = next(w for w in wins if w.tier.key == TIER_BAND_DAY.key)
    assert format_event(cheap)[0] == "🟢 Cosy cheap 12.5p"
    assert format_event(peak)[0] == "🚨 Cosy peak 38.2p"
    assert format_event(day)[0] == "🟡 Cosy day 25.4p"
    assert " - " not in format_event(cheap)[0]
    assert "whole window" in format_event(cheap)[1]


def test_band_event_title_shows_range_across_a_reprice():
    from src.google_calendar.tiers import Window

    w = Window(start_utc=datetime(2026, 1, 1, 4, tzinfo=UTC), end_utc=datetime(2026, 1, 1, 7, tzinfo=UTC),
               tier=TIER_BAND_CHEAP, prices=[12.49, 12.49, 12.99, 12.99])
    assert format_event(w)[0] == "🟢 Cosy cheap 12.5p - 13.0p"


def test_agile_day_classification_unchanged(monkeypatch):
    """Dynamic path untouched: an Agile-shaped day still yields the median tiers."""
    import random

    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-H")
    rnd = random.Random(3)
    d = date(2026, 9, 10)
    slots = []
    for i in range(48):
        s = datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(minutes=30 * i)
        slots.append(Slot(start_utc=s, end_utc=s + timedelta(minutes=30), price_p=round(rnd.uniform(5, 35), 2)))
    wins = classify_day(slots)
    assert wins and all(w.tier.key not in ("band_cheap", "band_day", "band_peak") for w in wins)


# ── publisher ────────────────────────────────────────────────────────────────


def _service_with_events(existing: list[dict]) -> MagicMock:
    svc = MagicMock()
    svc.events.return_value.list.return_value.execute.return_value = {"items": existing}
    svc.events.return_value.insert.return_value.execute.return_value = {"id": "new"}
    return svc


def test_publisher_omits_band_day_windows(monkeypatch):
    from src import db
    from src.google_calendar import publisher

    db.init_db()
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "cal")
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_BANDED_TIERS", "band_cheap,band_peak", raising=False)
    monkeypatch.setattr(publisher.db, "get_agile_rates_slots_for_local_day", lambda *a, **k: _cosy_rows(DAY))
    monkeypatch.setattr(publisher.db, "upsert_calendar_event", lambda **k: None)
    svc = _service_with_events([])
    r = publisher._publish_day(svc, DAY, TZ)
    assert r.windows == 4 and r.created == 4
    bodies = [c.kwargs["body"] for c in svc.events.return_value.insert.call_args_list]
    tiers = sorted(b["extendedProperties"]["private"]["hem_tier"] for b in bodies)
    assert tiers == ["band_cheap", "band_cheap", "band_cheap", "band_peak"]
    assert all(" - " not in b["summary"] for b in bodies)


def test_publisher_replaces_stale_agile_style_events_once(monkeypatch):
    """Old 'Expensive' events for the day are deleted and the four band events
    created; a second run with the band events in place is a no-op."""
    from src import db
    from src.google_calendar import publisher

    db.init_db()
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "cal")
    monkeypatch.setattr(publisher.db, "get_agile_rates_slots_for_local_day", lambda *a, **k: _cosy_rows(DAY))
    monkeypatch.setattr(publisher.db, "upsert_calendar_event", lambda **k: None)
    stale = [{
        "id": "old1", "summary": "🟠 Expensive (25.4p - 25.4p)", "colorId": "4",
        "start": {"dateTime": "2026-10-14T06:00:00Z"}, "end": {"dateTime": "2026-10-14T12:00:00Z"},
    }]
    svc = _service_with_events(stale)
    r = publisher._publish_day(svc, DAY, TZ)
    assert r.deleted == 1 and r.created == 4

    # second run: the four band events now exist → unchanged
    svc2 = _service_with_events([
        c.kwargs["body"] | {"id": f"e{i}"} for i, c in enumerate(svc.events.return_value.insert.call_args_list)
    ])
    r2 = publisher._publish_day(svc2, DAY, TZ)
    assert r2.skipped_unchanged and r2.created == 0 and r2.deleted == 0


# ── tier boundaries + charge floor ───────────────────────────────────────────


def test_cosy_registers_seven_boundaries_local_clock(monkeypatch):
    from src.scheduler import runner

    sched = MagicMock()
    sched.get_jobs.return_value = []
    monkeypatch.setattr(runner, "_background_scheduler", sched)
    monkeypatch.setattr(runner, "_scheduler_paused", False, raising=False)
    monkeypatch.setattr(runner.config, "TIER_BOUNDARY_LEAD_MINUTES", 5, raising=False)
    monkeypatch.setattr(runner.config, "TIER_BOUNDARY_MIN_LEAD_MINUTES", 0, raising=False)
    far = datetime.now(UTC).date() + timedelta(days=400)
    monkeypatch.setattr(runner.db, "get_agile_rates_slots_for_local_day",
                        lambda tariff, local_date, tz_name="Europe/London": _cosy_rows(local_date) if local_date >= far else [])
    real_dt = runner.datetime

    class _FakeDateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(far.year, far.month, far.day, 0, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(runner, "datetime", _FakeDateTime)
    out = runner._register_tier_boundary_triggers()
    assert out["status"] == "ok"
    # 7 boundaries/day (00/04/07/13/16/19/22 local) × 2 days, minus the one
    # that starts before the fixture's "now" (00:00 local day 1 < 00:00Z).
    assert len(out["scheduled"]) == 13
    assert {j["tier"] for j in out["scheduled"]} == {"band_cheap", "band_day", "band_peak"}


def test_cosy_floor_index_is_1600_only():
    from src.scheduler.optimizer import _peak_entry_floor_indices

    slots = _cosy_slots(DAY) + _cosy_slots(DAY + timedelta(days=1))
    starts = [s.start_utc for s in slots]
    prices = [s.price_p for s in slots]
    idx = _peak_entry_floor_indices(starts, prices)
    hours = [starts[i].astimezone(TZ).hour for i in idx]
    assert hours == [16, 16]


def test_cosy_floor_partial_tail_day_has_no_midnight_floor():
    """A 48 h horizon starting 02:00 local ends 02:00 two days later: the
    8-slot tail day must not fall back to the Agile tiers (day band read as
    'expensive' → a floor at 00:00)."""
    from src.scheduler.optimizer import _peak_entry_floor_indices

    slots = _cosy_slots(DAY) + _cosy_slots(DAY + timedelta(days=1)) + _cosy_slots(DAY + timedelta(days=2))
    slots = slots[4:4 + 96]  # 02:00 local day 1 → 02:00 local day 3
    starts = [s.start_utc for s in slots]
    prices = [s.price_p for s in slots]
    idx = _peak_entry_floor_indices(starts, prices)
    assert [starts[i].astimezone(TZ).hour for i in idx] == [16, 16]


def test_partial_cosy_day_is_classified_by_band_not_expensive():
    wins = classify_day(_cosy_slots(DAY)[:8])  # 00:00-04:00 local, all day band
    assert [w.tier.key for w in wins] == ["band_day"]


def test_publisher_skips_partial_day(monkeypatch):
    from src import db
    from src.google_calendar import publisher

    db.init_db()
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ID", "cal")
    monkeypatch.setattr(publisher.db, "get_agile_rates_slots_for_local_day", lambda *a, **k: _cosy_rows(DAY)[:8])
    svc = _service_with_events([])
    r = publisher._publish_day(svc, DAY, TZ)
    assert r.skipped_reason == "partial_rates" and r.created == 0
