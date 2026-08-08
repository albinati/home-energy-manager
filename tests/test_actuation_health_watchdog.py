"""The watchdog that should have caught two multi-day outages (#784).

`db.get_actuation_health` was added on 2026-06-14 "after the ~41h Fox-upload
wedge that nothing alerted on" — but its only caller was the status endpoint,
so it warned nobody unless a human opened the cockpit. On 2026-08-06 a broken
Fox v3 write endpoint wedged uploads for ~37 h and the same thing happened
again: the freshness signal was correct the whole time, and the battery stopped
cycling (measured export collapsed to 0.01-0.06 kWh/day against 1.2-5.3 either
side) until someone noticed a chip in the UI.

These tests are about the wire, not the signal: does a wedge actually page, and
does an ongoing wedge page once a day rather than every hour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.analytics.actuation_health import actuation_issues, evaluate_actuation_health

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _raw(fox_age_h=1.0, tank_age_h=1.0, tank_failed=0, lwt_failed=0):
    iso = lambda h: (NOW - timedelta(hours=h)).isoformat()  # noqa: E731
    return {
        "fox_upload_at": None if fox_age_h is None else iso(fox_age_h),
        "tank_last_at": None if tank_age_h is None else iso(tank_age_h),
        "tank_failed_24h": tank_failed,
        "lwt_failed_24h": lwt_failed,
    }


def _eval(raw, dhw_mode="normal"):
    return evaluate_actuation_health(
        raw, NOW, fox_stale_hours=30, tank_stale_hours=30,
        failed_threshold=3, dhw_mode=dhw_mode,
    )


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------

def test_healthy_system_reports_nothing():
    assert actuation_issues(_eval(_raw())) == []


def test_the_2026_08_06_outage_is_flagged():
    """37 h since the last successful Fox upload — the real incident."""
    block = _eval(_raw(fox_age_h=37.0))

    assert block["fox"]["stale"] is True
    issues = actuation_issues(block)
    assert len(issues) == 1
    # The message must name what stopped and for how long — a page saying only
    # "actuation unhealthy" costs a cockpit round-trip at the worst moment.
    assert "Fox" in issues[0] and "37h" in issues[0]


def test_never_uploaded_counts_as_stale():
    """A NULL timestamp must alarm, not read as 'age 0 = fine'."""
    assert _eval(_raw(fox_age_h=None))["fox"]["stale"] is True


def test_vacation_suppresses_the_tank_age_alarm_but_not_rejections():
    """dhw_policy writes zero tank rows in vacation — age is expected, not a fault.
    A rejected write still means something in any mode."""
    stale_tank = _raw(tank_age_h=40.0, tank_failed=5)

    normal = _eval(stale_tank, dhw_mode="normal")
    vac = _eval(stale_tank, dhw_mode="vacation")

    assert normal["daikin_tank"]["stale"] is True
    assert vac["daikin_tank"]["stale"] is False
    assert vac["daikin_tank"]["failing"] is True, "rejections are mode-independent"


def test_lwt_has_no_age_alarm():
    """LWT is demand-gated and dormant all summer — an age alarm would cry wolf."""
    block = _eval(_raw(tank_age_h=1.0))
    assert "stale" not in block["daikin_lwt"]
    assert block["daikin_lwt"]["failing"] is False


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_dedup():
    db.init_db()
    db.set_runtime_setting("actuation_health_last_alert_sig", "")
    yield
    db.set_runtime_setting("actuation_health_last_alert_sig", "")


@pytest.fixture
def paged(monkeypatch):
    """Capture what the job would send, without touching Telegram."""
    sent: list[list[str]] = []
    from src import notifier
    monkeypatch.setattr(notifier, "notify_actuation_stale", lambda issues: sent.append(issues))
    return sent


def _run_job(monkeypatch, raw):
    from src.scheduler import runner
    monkeypatch.setattr(db, "get_actuation_health", lambda since: raw)
    runner.bulletproof_actuation_health_monitor_job()


def test_a_wedge_actually_pages(monkeypatch, paged):
    """The whole point: this is the call that did not exist for two months."""
    _run_job(monkeypatch, _raw(fox_age_h=37.0))

    assert len(paged) == 1
    assert "Fox" in paged[0][0]


def test_healthy_system_does_not_page(monkeypatch, paged):
    _run_job(monkeypatch, _raw())
    assert paged == []


def test_ongoing_wedge_pages_once_per_day_not_hourly(monkeypatch, paged):
    """Hourly cadence is what makes detection fast; hourly PAGING would make the
    alert ignorable, which is the same failure with extra steps."""
    for extra_hours in (37.0, 38.0, 39.0, 40.0):
        _run_job(monkeypatch, _raw(fox_age_h=extra_hours))

    assert len(paged) == 1, "an ongoing outage must page once, not every tick"


def test_recovery_clears_the_dedup_so_a_relapse_pages_again(monkeypatch, paged):
    _run_job(monkeypatch, _raw(fox_age_h=37.0))
    assert len(paged) == 1

    _run_job(monkeypatch, _raw())                      # fixed
    assert db.get_runtime_setting("actuation_health_last_alert_sig") == ""

    _run_job(monkeypatch, _raw(fox_age_h=31.0))        # broke again
    assert len(paged) == 2, "a relapse must not be swallowed by a stale signature"


def test_job_survives_a_db_failure(monkeypatch, paged):
    """A watchdog that crashes the scheduler is worse than no watchdog."""
    from src.scheduler import runner

    def boom(_since):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_actuation_health", boom)
    runner.bulletproof_actuation_health_monitor_job()   # must not raise
    assert paged == []
