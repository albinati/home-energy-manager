"""Proactive appliance load-nudge (2026-06-07).

When a negative / notably-cheap Agile window is upcoming and a registered
appliance is idle, HEM pushes one nudge to LOAD + Smart-Control the machine
(the physical button is the consent gate — HEM can only prompt). Debounced once
per appliance per window.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.scheduler import appliance_dispatch


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


def _setup_cfg(monkeypatch):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-TARIFF", raising=False)
    monkeypatch.setattr(config, "APPLIANCE_WINDOW_NUDGE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "APPLIANCE_WINDOW_NUDGE_PRICE_THRESHOLD_P", "", raising=False)
    monkeypatch.setattr(config, "APPLIANCE_WINDOW_NUDGE_HORIZON_HOURS", 24.0, raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)


def _capture_notify(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        "src.notifier.notify_appliance_window_nudge",
        lambda **kw: calls.append(kw),
    )
    return calls


def _add_washer(deadline_hours_ahead: float = 12.0):
    tz = ZoneInfo("Europe/London")
    deadline = (datetime.now(tz) + timedelta(hours=deadline_hours_ahead)).strftime("%H:%M")
    return db.add_appliance(
        vendor="smartthings", vendor_device_id="dev-test", name="Washer",
        device_type="washer", default_duration_minutes=120,
        deadline_local_time=deadline, typical_kw=0.5,
    )


def _seed_rates(start_utc: datetime, prices: list[float]) -> None:
    rates, t = [], start_utc
    for p in prices:
        rates.append({
            "valid_from": t.isoformat().replace("+00:00", "Z"),
            "valid_to": (t + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": p,
        })
        t += timedelta(minutes=30)
    db.save_agile_rates(rates, "TEST-TARIFF")


def _now_top_of_hour() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


# --- pure detector --------------------------------------------------------

def test_detect_price_windows_groups_contiguous():
    start = datetime(2026, 6, 7, 11, 0, tzinfo=UTC)
    rates = [
        {"valid_from": "2026-06-07T11:00:00Z", "value_inc_vat": -4.0},
        {"valid_from": "2026-06-07T11:30:00Z", "value_inc_vat": -3.0},
        {"valid_from": "2026-06-07T12:00:00Z", "value_inc_vat": 5.0},   # gap
        {"valid_from": "2026-06-07T12:30:00Z", "value_inc_vat": -2.0},
    ]
    w = appliance_dispatch._detect_price_windows(
        rates, start, start + timedelta(hours=4), max_price_p=0.0, strict=True,
    )
    assert len(w) == 2
    assert w[0] == (datetime(2026, 6, 7, 11, 0, tzinfo=UTC),
                    datetime(2026, 6, 7, 12, 0, tzinfo=UTC))


def test_detect_price_windows_strict_vs_threshold():
    start = datetime(2026, 6, 7, 11, 0, tzinfo=UTC)
    rates = [{"valid_from": "2026-06-07T11:00:00Z", "value_inc_vat": 0.0}]
    # strict < 0 excludes a 0.00 slot...
    assert appliance_dispatch._detect_price_windows(
        rates, start, start + timedelta(hours=1), max_price_p=0.0, strict=True,
    ) == []
    # ...but a ≤ 8p threshold includes it.
    assert len(appliance_dispatch._detect_price_windows(
        rates, start, start + timedelta(hours=1), max_price_p=8.0, strict=False,
    )) == 1


# --- nudge integration ----------------------------------------------------

def test_nudge_fires_for_negative_window_idle(monkeypatch):
    _setup_cfg(monkeypatch)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    _add_washer()
    _seed_rates(now + timedelta(hours=1), [-4.0, -4.5, -3.0, -2.0, 8.0, 8.0])

    fired = appliance_dispatch.nudge_appliance_windows(now=now)
    assert len(fired) == 1
    assert len(calls) == 1
    c = calls[0]
    assert c["appliance_name"] == "Washer"
    assert c["is_negative"] is True
    assert c["est_cost_pence"] < 0  # paid to run
    assert "deadline_local" in c


def test_nudge_skips_when_job_active(monkeypatch):
    _setup_cfg(monkeypatch)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    aid = _add_washer()
    _seed_rates(now + timedelta(hours=1), [-4.0, -4.5, -3.0, -2.0])
    db.create_appliance_job(
        appliance_id=aid, armed_at_utc=now.isoformat(),
        deadline_utc=(now + timedelta(hours=12)).isoformat(),
        duration_minutes=120,
        planned_start_utc=(now + timedelta(hours=1)).isoformat(),
        planned_end_utc=(now + timedelta(hours=3)).isoformat(),
        status="scheduled",
    )
    appliance_dispatch.nudge_appliance_windows(now=now)
    assert calls == []


def test_nudge_skips_when_flag_off(monkeypatch):
    _setup_cfg(monkeypatch)
    monkeypatch.setattr(config, "APPLIANCE_WINDOW_NUDGE_ENABLED", False, raising=False)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    _add_washer()
    _seed_rates(now + timedelta(hours=1), [-4.0, -4.5, -3.0, -2.0])
    assert appliance_dispatch.nudge_appliance_windows(now=now) == []
    assert calls == []


def test_nudge_skips_when_no_negative_window(monkeypatch):
    _setup_cfg(monkeypatch)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    _add_washer()
    _seed_rates(now + timedelta(hours=1), [10.0, 12.0, 15.0, 20.0])  # all positive
    assert appliance_dispatch.nudge_appliance_windows(now=now) == []
    assert calls == []


def test_nudge_debounced_within_window(monkeypatch):
    _setup_cfg(monkeypatch)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    aid = _add_washer()
    _seed_rates(now + timedelta(hours=1), [-4.0, -4.5, -3.0, -2.0])

    appliance_dispatch.nudge_appliance_windows(now=now)
    appliance_dispatch.nudge_appliance_windows(now=now + timedelta(minutes=5))
    assert len(calls) == 1  # second call debounced
    assert db.get_runtime_setting(f"appliance_nudge_last_{aid}") is not None


def test_nudge_refires_for_new_window(monkeypatch):
    _setup_cfg(monkeypatch)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    _add_washer(deadline_hours_ahead=12)
    # First: a window at now+3h → recommended start = now+3h, nudge once.
    _seed_rates(now + timedelta(hours=3), [-3.0, -3.0, -3.0, -3.0])
    appliance_dispatch.nudge_appliance_windows(now=now)
    assert len(calls) == 1
    # A cheaper, EARLIER window appears (now+1h) → new cheapest → recommended start
    # shifts to now+1h ≠ now+3h → re-fires (same now, no deadline wrap).
    _seed_rates(now + timedelta(hours=1), [-5.0, -5.0, -5.0, -5.0])
    appliance_dispatch.nudge_appliance_windows(now=now)
    assert len(calls) == 2


def test_nudge_no_fit_before_deadline_skips(monkeypatch):
    _setup_cfg(monkeypatch)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    _add_washer(deadline_hours_ahead=1.0)  # deadline only 1h away, wash needs 2h
    _seed_rates(now + timedelta(minutes=30), [-4.0, -4.5, -3.0])
    appliance_dispatch.nudge_appliance_windows(now=now)
    assert calls == []


def test_nudge_skips_when_negative_window_unreachable_before_deadline(monkeypatch):
    """Review MEDIUM: a negative window exists in the horizon but the deadline is
    BEFORE it → the recommended (pre-deadline) window can't overlap it → no nudge
    (and no fabricated _fallback_window=0.0p push)."""
    _setup_cfg(monkeypatch)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    _add_washer(deadline_hours_ahead=2.0)  # deadline ~2h away
    # Negative window is 5h out — after the deadline → unreachable.
    _seed_rates(now + timedelta(hours=5), [-4.0, -4.5, -3.0, -2.0])
    assert appliance_dispatch.nudge_appliance_windows(now=now) == []
    assert calls == []


def test_nudge_multiple_appliances_one_each(monkeypatch):
    _setup_cfg(monkeypatch)
    calls = _capture_notify(monkeypatch)
    now = _now_top_of_hour()
    _add_washer()
    db.add_appliance(
        vendor="smartthings", vendor_device_id="dev-2", name="Dishwasher",
        device_type="dishwasher", default_duration_minutes=120,
        deadline_local_time=(datetime.now(ZoneInfo("Europe/London")) + timedelta(hours=12)).strftime("%H:%M"),
        typical_kw=0.6,
    )
    _seed_rates(now + timedelta(hours=1), [-4.0, -4.5, -3.0, -2.0])
    fired = appliance_dispatch.nudge_appliance_windows(now=now)
    assert len(fired) == 2
    names = {c["appliance_name"] for c in calls}
    assert names == {"Washer", "Dishwasher"}


# --- brief line -----------------------------------------------------------

def test_brief_suggestion_line_for_cheap_window(monkeypatch):
    _setup_cfg(monkeypatch)
    monkeypatch.setattr(config, "APPLIANCE_WINDOW_NUDGE_BRIEF_THRESHOLD_P", 8.0, raising=False)
    now = _now_top_of_hour()
    _add_washer()
    _seed_rates(now + timedelta(hours=1), [3.0, 4.0, 3.5, 5.0])  # cheap, not negative
    from src.analytics import daily_brief
    line = daily_brief._appliance_window_suggestion_line(ZoneInfo("Europe/London"))
    assert line is not None
    assert "Washer" in line and "Smart Control" in line


def test_brief_suggestion_line_none_when_no_window(monkeypatch):
    _setup_cfg(monkeypatch)
    monkeypatch.setattr(config, "APPLIANCE_WINDOW_NUDGE_BRIEF_THRESHOLD_P", 8.0, raising=False)
    now = _now_top_of_hour()
    _add_washer()
    _seed_rates(now + timedelta(hours=1), [20.0, 22.0, 25.0])  # expensive
    from src.analytics import daily_brief
    assert daily_brief._appliance_window_suggestion_line(ZoneInfo("Europe/London")) is None
