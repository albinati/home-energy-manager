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


def _raw(fox_age_h=1.0, tank_age_h=1.0, tank_failed=0, lwt_failed=0, anchor=None):
    base = anchor or NOW
    iso = lambda h: (base - timedelta(hours=h)).isoformat()  # noqa: E731
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
    kind, msg = issues[0]
    assert kind == "fox_stale"
    # The message must name what stopped and for how long — a page saying only
    # "actuation unhealthy" costs a cockpit round-trip at the worst moment.
    assert "Fox" in msg and "37h" in msg


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
    db.set_runtime_setting("actuation_health_alerted", "")
    yield
    db.set_runtime_setting("actuation_health_alerted", "")


@pytest.fixture
def paged(monkeypatch):
    """Capture what the job would send, without touching Telegram."""
    sent: list[list[str]] = []
    from src import notifier
    monkeypatch.setattr(notifier, "notify_actuation_stale", lambda issues: sent.append(issues))
    return sent


def _run_job(monkeypatch, **ages):
    """Job-level tests must anchor the fixture rows on the REAL clock.

    The evaluator tests above pass ``NOW`` in explicitly, so a frozen anchor is
    correct there. The JOB calls ``datetime.now(UTC)`` itself — so rows built
    against a frozen ``NOW`` drift one hour staler every hour, and once the
    calendar moves past it every fixture reads as wedged. This suite went red
    on 2026-08-23: a "1 hour old" row was 359 h old, so
    ``test_healthy_system_does_not_page`` paged. Anchoring on the real clock
    keeps the AGES the tests actually care about exact, forever.
    """
    from src.scheduler import runner
    raw = _raw(anchor=datetime.now(UTC), **ages)
    monkeypatch.setattr(db, "get_actuation_health", lambda since: raw)
    runner.bulletproof_actuation_health_monitor_job()


def test_a_wedge_actually_pages(monkeypatch, paged):
    """The whole point: this is the call that did not exist for two months."""
    _run_job(monkeypatch, fox_age_h=37.0)

    assert len(paged) == 1
    assert "Fox" in paged[0][0]


def test_healthy_system_does_not_page(monkeypatch, paged):
    _run_job(monkeypatch)
    assert paged == []


def test_ongoing_wedge_pages_once_per_day_not_hourly(monkeypatch, paged):
    """Hourly cadence is what makes detection fast; hourly PAGING would make the
    alert ignorable, which is the same failure with extra steps."""
    for extra_hours in (37.0, 38.0, 39.0, 40.0):
        _run_job(monkeypatch, fox_age_h=extra_hours)

    assert len(paged) == 1, "an ongoing outage must page once, not every tick"


def test_recovery_clears_the_dedup_so_a_relapse_pages_again(monkeypatch, paged):
    _run_job(monkeypatch, fox_age_h=37.0)
    assert len(paged) == 1

    _run_job(monkeypatch)                      # fixed
    assert db.get_runtime_setting("actuation_health_alerted") == ""

    _run_job(monkeypatch, fox_age_h=31.0)        # broke again
    assert len(paged) == 2, "a relapse must not be swallowed by a stale signature"


def test_job_survives_a_db_failure(monkeypatch, paged):
    """A watchdog that crashes the scheduler is worse than no watchdog."""
    from src.scheduler import runner

    def boom(_since):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_actuation_health", boom)
    runner.bulletproof_actuation_health_monitor_job()   # must not raise
    assert paged == []


def test_a_second_distinct_fault_still_pages_the_same_day(monkeypatch, paged):
    """The dedup must not bucket different faults together.

    An earlier design keyed on `message.split(":")[0]`, so "tank reconciler is
    dead" and "tank writes are being rejected" collapsed into one key — a
    rejection storm starting after a stall was silently suppressed for the rest
    of the day. Different faults, different keys.
    """
    _run_job(monkeypatch, tank_age_h=40.0)          # reconciler stalled
    assert len(paged) == 1 and "reconciler parado" in paged[0][0]

    _run_job(monkeypatch, tank_age_h=1.0, tank_failed=7)  # recovered, now rejected
    assert len(paged) == 2, "a different tank fault must page on its own"
    assert "rejeitadas" in paged[1][0]


def test_a_flapping_component_cannot_re_page_hourly(monkeypatch, paged):
    """`tank_failed_24h` is a ROLLING count, so it oscillates across the
    threshold as old failures age out. Storing only the last signature let that
    re-page every hour alongside a persistent Fox wedge."""
    # Fox is wedged throughout; the tank count crosses the threshold repeatedly.
    for tank_failed in (0, 5, 0, 5, 0, 5):
        _run_job(monkeypatch, fox_age_h=37.0, tank_failed=tank_failed)

    # One page for Fox, one the first time the tank crosses — and nothing for
    # the three later crossings.
    assert len(paged) == 2, f"flapping re-paged: {paged}"
    assert "Fox" in paged[0][0]
    assert "rejeitadas" in paged[1][0]


def test_only_the_new_fault_is_named_in_the_follow_up_page(monkeypatch, paged):
    """Re-sending the already-known problem would train the reader to skim."""
    _run_job(monkeypatch, fox_age_h=37.0)
    _run_job(monkeypatch, fox_age_h=38.0, lwt_failed=9)

    assert len(paged) == 2
    assert all("Fox" not in m for m in paged[1]), "second page must carry only what is new"
    assert "LWT" in paged[1][0]


def test_interval_is_floored_at_one_minute():
    """APScheduler coerces a zero-length interval to 1s — a stray `=0` would
    spin the watchdog rather than disable it."""
    from src.config import config as cfg
    from src.scheduler.runner import _actuation_interval_minutes

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(cfg, "ACTUATION_HEALTH_MONITOR_INTERVAL_MINUTES", 0)
        assert _actuation_interval_minutes() == 1
        mp.setattr(cfg, "ACTUATION_HEALTH_MONITOR_INTERVAL_MINUTES", 15)
        assert _actuation_interval_minutes() == 15
    finally:
        mp.undo()
