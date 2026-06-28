"""Appliance dispatch tests — reconcile semantics, fire path, cheapest-window
picker, LP load profile, rehydration.

SmartThings is mocked at the service-singleton level so no HTTP traffic.
APScheduler is replaced with a fake that records add_job/remove_job calls.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src import db
from src.config import config
from src.scheduler import appliance_dispatch
from src.smartthings.client import SmartThingsError


# ---------------------------------------------------------------------------
# Fixtures: DB init, fake APScheduler, fake SmartThings client
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


class FakeScheduler:
    """Minimal APScheduler stand-in. Records add/remove for assertions."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.add_calls: list[tuple] = []
        self.remove_calls: list[str] = []

    def add_job(self, func, trigger, *, id, replace_existing=False, args=None, **_):
        run_date = getattr(trigger, "run_date", None)
        self.add_calls.append((id, run_date, args))
        self.jobs[id] = {"func": func, "trigger": trigger, "args": args}

    def remove_job(self, id):
        self.remove_calls.append(id)
        if id not in self.jobs:
            from apscheduler.jobstores.base import JobLookupError
            raise JobLookupError(id)
        del self.jobs[id]


@pytest.fixture
def fake_scheduler():
    sch = FakeScheduler()
    with patch.object(appliance_dispatch, "_register_cron", wraps=appliance_dispatch._register_cron):
        with patch("src.scheduler.runner._background_scheduler", sch):
            with patch("src.scheduler.runner.get_background_scheduler", return_value=sch):
                yield sch


@pytest.fixture
def fake_client():
    """A fake SmartThingsClient — sets remote_control_enabled per test."""
    cli = MagicMock()
    cli.get_remote_control_enabled = MagicMock(return_value=True)
    cli.start_cycle = MagicMock(return_value={"results": [{"status": "ACCEPTED"}]})
    cli.list_devices = MagicMock(return_value=[])
    return cli


@pytest.fixture
def patch_st(fake_client):
    with patch.object(appliance_dispatch, "_get_st_client", return_value=fake_client):
        yield fake_client


@pytest.fixture
def appliance_id():
    """Insert a single appliance and return its id.

    The deadline is ~12 h ahead of *now* (not a fixed "07:00") so the reconcile
    always has room to place the 120-min window regardless of the wall-clock
    time the suite runs at — a fixed early-morning deadline failed when CI ran
    near it (no room before it).
    """
    from zoneinfo import ZoneInfo
    deadline = (datetime.now(ZoneInfo("Europe/London")) + timedelta(hours=12)).strftime("%H:%M")
    return db.add_appliance(
        vendor="smartthings",
        vendor_device_id="dev-test",
        name="Test Washer",
        device_type="washer",
        default_duration_minutes=120,
        deadline_local_time=deadline,
        typical_kw=0.5,
    )


def _seed_agile_rates(start_utc: datetime, prices_pence: list[float]) -> None:
    """Insert half-hour agile_rates rows starting at start_utc."""
    rates = []
    t = start_utc
    for p in prices_pence:
        rates.append({
            "valid_from": t.isoformat().replace("+00:00", "Z"),
            "valid_to": (t + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": p,
        })
        t += timedelta(minutes=30)
    db.save_agile_rates(rates, config.OCTOPUS_TARIFF_CODE or "TEST-TARIFF")


# ---------------------------------------------------------------------------
# find_cheapest_window
# ---------------------------------------------------------------------------

class TestCheapestWindow:
    def test_picks_cheap_window_from_rates(self, monkeypatch):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        # 8 half-hour slots: cheap stretch is slots 4-5 (2h cycle = 4 slots needs window)
        # Actually with duration_minutes=60 (2 slots), we want slots 4-5 to be cheapest.
        now = datetime(2026, 5, 1, 22, 0, tzinfo=UTC)
        prices = [20.0, 19.0, 18.0, 15.0, 5.0, 6.0, 14.0, 16.0]  # cheap at slots 4-5
        _seed_agile_rates(now, prices)

        deadline = now + timedelta(hours=4)
        start, end, avg = appliance_dispatch.find_cheapest_window(now, deadline, 60)
        # Cheapest 1h window is slots 4-5: (5+6)/2 = 5.5p
        expected_start = now + timedelta(hours=2)
        assert start == expected_start
        assert end == expected_start + timedelta(minutes=60)
        assert abs(avg - 5.5) < 0.01

    def test_falls_back_when_rates_empty(self, monkeypatch):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        monkeypatch.setattr(config, "APPLIANCE_FALLBACK_WINDOW_LOCAL", "02:00-05:00")
        # No rates seeded.
        now = datetime(2026, 5, 1, 22, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=10)  # gives plenty of room
        start, end, avg = appliance_dispatch.find_cheapest_window(now, deadline, 120)
        assert avg == 0.0
        # Window duration matches request.
        assert end - start == timedelta(minutes=120)

    def test_raises_when_deadline_too_close(self, monkeypatch):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        now = datetime(2026, 5, 1, 22, 0, tzinfo=UTC)
        deadline = now + timedelta(minutes=10)  # 10 min < 60 duration
        with pytest.raises(ValueError):
            appliance_dispatch.find_cheapest_window(now, deadline, 60)


# ---------------------------------------------------------------------------
# reconcile() — state machine
# ---------------------------------------------------------------------------

class TestReconcile:
    def test_remote_mode_true_arms_session(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        # Seed enough cheap slots so the cheapest-window picker can find one.
        now = datetime.now(UTC)
        seed_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        _seed_agile_rates(seed_start, [10.0, 5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        patch_st.get_remote_control_enabled.return_value = True

        appliance_dispatch.reconcile()

        job = db.get_active_appliance_job(appliance_id)
        assert job is not None
        assert job["status"] == "scheduled"
        # Cron registered with the right id.
        assert any(c[0] == f"appliance_fire_{appliance_id}" for c in fake_scheduler.add_calls)

    def test_remote_mode_true_no_replan_when_window_unchanged(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        now = datetime.now(UTC)
        seed_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        _seed_agile_rates(seed_start, [10.0, 5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        patch_st.get_remote_control_enabled.return_value = True

        appliance_dispatch.reconcile()
        first_calls = len(fake_scheduler.add_calls)

        # Second tick with same rates → no cron churn
        appliance_dispatch.reconcile()
        assert len(fake_scheduler.add_calls) == first_calls

    def test_remote_mode_false_cancels_scheduled_session(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        now = datetime.now(UTC)
        seed_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        _seed_agile_rates(seed_start, [10.0, 5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])

        patch_st.get_remote_control_enabled.return_value = True
        appliance_dispatch.reconcile()
        job = db.get_active_appliance_job(appliance_id)
        assert job is not None and job["status"] == "scheduled"

        patch_st.get_remote_control_enabled.return_value = False
        appliance_dispatch.reconcile()

        # Active job slot is now empty because status flipped to 'cancelled'.
        assert db.get_active_appliance_job(appliance_id) is None
        cancelled = db.get_appliance_jobs(status="cancelled", appliance_id=appliance_id)
        assert len(cancelled) == 1
        assert cancelled[0]["error_msg"] == "remote_mode_dropped"
        assert f"appliance_fire_{appliance_id}" in fake_scheduler.remove_calls

    def test_remote_mode_false_no_session_no_op(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        patch_st.get_remote_control_enabled.return_value = False
        appliance_dispatch.reconcile()
        assert db.get_active_appliance_job(appliance_id) is None
        assert fake_scheduler.add_calls == []
        assert fake_scheduler.remove_calls == []

    def test_arm_fires_armed_hook(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        """First-time arm must hit notify_appliance_armed with replan=False."""
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        now = datetime.now(UTC)
        seed_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        _seed_agile_rates(seed_start, [10.0, 5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        patch_st.get_remote_control_enabled.return_value = True

        with patch("src.notifier._dispatch") as mock_dispatch:
            appliance_dispatch.reconcile()

        from src.notifier import AlertType
        armed_calls = [
            c for c in mock_dispatch.call_args_list
            if c.args and c.args[0] == AlertType.APPLIANCE_ARMED
        ]
        assert len(armed_calls) == 1, f"expected 1 armed hook, got {len(armed_calls)}"
        kwargs = armed_calls[0].kwargs
        assert kwargs["extra"]["replan"] is False
        assert kwargs["extra"]["appliance"]  # name populated

    def test_replan_ping_muted_by_default(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        """Window-shift re-plan must NOT ping by default (pull-based policy) —
        only the first arm and the finished summary reach the user."""
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        monkeypatch.setattr(config, "APPLIANCE_NOTIFY_REPLAN", False)
        patch_st.get_remote_control_enabled.return_value = True
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        base = now.replace(minute=0 if now.minute < 30 else 30) + timedelta(hours=1)
        w1 = (base, base + timedelta(hours=2), 5.0)
        w2 = (base + timedelta(hours=1), base + timedelta(hours=3), 4.0)
        with patch.object(
            appliance_dispatch, "find_battery_aware_window", side_effect=[w1, w2]
        ):
            appliance_dispatch.reconcile()  # first arm → w1
            assert db.get_active_appliance_job(appliance_id)[
                "planned_start_utc"
            ] == appliance_dispatch._iso(w1[0])
            with patch("src.notifier._dispatch") as mock_dispatch:
                appliance_dispatch.reconcile()  # re-plan → w2
        from src.notifier import AlertType
        armed = [
            c for c in mock_dispatch.call_args_list
            if c.args and c.args[0] == AlertType.APPLIANCE_ARMED
        ]
        assert armed == [], "re-plan must be silent by default"
        # The window still shifted — only the ping was suppressed.
        assert db.get_active_appliance_job(appliance_id)[
            "planned_start_utc"
        ] == appliance_dispatch._iso(w2[0])

    def test_replan_ping_fires_when_enabled(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        monkeypatch.setattr(config, "APPLIANCE_NOTIFY_REPLAN", True)
        patch_st.get_remote_control_enabled.return_value = True
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        base = now.replace(minute=0 if now.minute < 30 else 30) + timedelta(hours=1)
        w1 = (base, base + timedelta(hours=2), 5.0)
        w2 = (base + timedelta(hours=1), base + timedelta(hours=3), 4.0)
        with patch.object(
            appliance_dispatch, "find_battery_aware_window", side_effect=[w1, w2]
        ):
            appliance_dispatch.reconcile()
            with patch("src.notifier._dispatch") as mock_dispatch:
                appliance_dispatch.reconcile()
        from src.notifier import AlertType
        armed = [
            c for c in mock_dispatch.call_args_list
            if c.args and c.args[0] == AlertType.APPLIANCE_ARMED
        ]
        assert len(armed) == 1
        assert armed[0].kwargs["extra"]["replan"] is True

    def test_cancel_fires_cancelled_hook(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        """Flipping remote_mode false on an armed job must emit the cancelled
        hook with reason='remote_mode_dropped'."""
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        now = datetime.now(UTC)
        seed_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        _seed_agile_rates(seed_start, [10.0, 5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])

        # Arm
        patch_st.get_remote_control_enabled.return_value = True
        appliance_dispatch.reconcile()
        assert db.get_active_appliance_job(appliance_id) is not None

        # Drop remote control
        patch_st.get_remote_control_enabled.return_value = False
        with patch("src.notifier._dispatch") as mock_dispatch:
            appliance_dispatch.reconcile()

        from src.notifier import AlertType
        cancelled_calls = [
            c for c in mock_dispatch.call_args_list
            if c.args and c.args[0] == AlertType.APPLIANCE_CANCELLED
        ]
        assert len(cancelled_calls) == 1, (
            f"expected 1 cancelled hook, got {len(cancelled_calls)}"
        )
        kwargs = cancelled_calls[0].kwargs
        assert kwargs["extra"]["reason"] == "remote_mode_dropped"
        assert "planned_start_local" in kwargs["extra"]

    def test_smartthings_error_records_then_pings_at_threshold(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        monkeypatch.setattr(config, "APPLIANCE_RECONCILE_ERROR_PING_THRESHOLD", 3)
        # Reset the per-process counter that may have ridden over from another test.
        appliance_dispatch._reconcile_errors.clear()
        appliance_dispatch._pat_invalid_notified = False

        patch_st.get_remote_control_enabled.side_effect = SmartThingsError(
            "transport", "boom"
        )
        with patch.object(appliance_dispatch, "notify_risk") as ping:
            for _ in range(5):
                appliance_dispatch.reconcile()
            # notify_risk fires exactly once (at the threshold).
            assert ping.call_count == 1

    def test_pat_invalid_pings_once(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        appliance_dispatch._reconcile_errors.clear()
        appliance_dispatch._pat_invalid_notified = False
        patch_st.get_remote_control_enabled.side_effect = SmartThingsError(
            "pat_invalid", "401", http_status=401
        )
        with patch.object(appliance_dispatch, "notify_risk") as ping:
            for _ in range(3):
                appliance_dispatch.reconcile()
            assert ping.call_count == 1


# ---------------------------------------------------------------------------
# _fire_cron — the moment of truth
# ---------------------------------------------------------------------------

class TestFireCron:
    def _make_armed_job(self, appliance_id: int, planned_start_utc: datetime) -> int:
        return db.create_appliance_job(
            appliance_id=appliance_id,
            status="scheduled",
            armed_at_utc=datetime.now(UTC).isoformat(),
            deadline_utc=(planned_start_utc + timedelta(hours=4)).isoformat(),
            duration_minutes=120,
            planned_start_utc=planned_start_utc.isoformat(),
            planned_end_utc=(planned_start_utc + timedelta(hours=2)).isoformat(),
            avg_price_pence=5.0,
            last_replan_at_utc=datetime.now(UTC).isoformat(),
        )

    def test_read_only_marks_skipped(self, monkeypatch, appliance_id, patch_st):
        monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
        job_id = self._make_armed_job(appliance_id, datetime.now(UTC))
        appliance_dispatch._fire_cron(job_id)
        row = db.get_appliance_job(job_id)
        assert row["status"] == "skipped_readonly"
        # start_cycle must NOT have been called.
        patch_st.start_cycle.assert_not_called()

    def test_pre_fire_remote_false_cancels_no_start(
        self, monkeypatch, appliance_id, patch_st
    ):
        monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
        patch_st.get_remote_control_enabled.return_value = False
        job_id = self._make_armed_job(appliance_id, datetime.now(UTC))
        with patch.object(appliance_dispatch, "notify_risk") as ping:
            appliance_dispatch._fire_cron(job_id)
        row = db.get_appliance_job(job_id)
        assert row["status"] == "cancelled"
        assert row["error_msg"] == "remote_mode_dropped_at_fire"
        patch_st.start_cycle.assert_not_called()
        assert ping.call_count == 1

    def test_pre_fire_safety_check_fails(
        self, monkeypatch, appliance_id, patch_st
    ):
        monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
        patch_st.get_remote_control_enabled.side_effect = SmartThingsError(
            "transport", "down"
        )
        job_id = self._make_armed_job(appliance_id, datetime.now(UTC))
        with patch.object(appliance_dispatch, "notify_risk"):
            appliance_dispatch._fire_cron(job_id)
        row = db.get_appliance_job(job_id)
        assert row["status"] == "failed"
        assert row["error_msg"].startswith("safety_check_failed")
        patch_st.start_cycle.assert_not_called()

    def test_successful_fire_marks_running(
        self, monkeypatch, appliance_id, patch_st
    ):
        monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
        patch_st.get_remote_control_enabled.return_value = True
        job_id = self._make_armed_job(appliance_id, datetime.now(UTC))
        appliance_dispatch._fire_cron(job_id)
        row = db.get_appliance_job(job_id)
        assert row["status"] == "running"
        assert row["actual_start_utc"] is not None
        patch_st.start_cycle.assert_called_once_with("dev-test")

    def test_start_cycle_failure_marks_failed(
        self, monkeypatch, appliance_id, patch_st
    ):
        monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
        patch_st.get_remote_control_enabled.return_value = True
        patch_st.start_cycle.side_effect = SmartThingsError("http_error", "500")
        job_id = self._make_armed_job(appliance_id, datetime.now(UTC))
        with patch.object(appliance_dispatch, "notify_risk"):
            appliance_dispatch._fire_cron(job_id)
        row = db.get_appliance_job(job_id)
        assert row["status"] == "failed"

    def test_starting_ping_muted_by_default(
        self, monkeypatch, appliance_id, patch_st
    ):
        """Default (APPLIANCE_NOTIFY_STARTING=False): the fire path marks the
        job running but sends NO starting ping — only arm + finished pings
        reach the user."""
        monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
        monkeypatch.setattr(config, "APPLIANCE_NOTIFY_STARTING", False)
        patch_st.get_remote_control_enabled.return_value = True
        job_id = self._make_armed_job(appliance_id, datetime.now(UTC))
        with patch("src.notifier._dispatch") as mock_dispatch:
            appliance_dispatch._fire_cron(job_id)
        from src.notifier import AlertType
        starting = [
            c for c in mock_dispatch.call_args_list
            if c.args and c.args[0] == AlertType.APPLIANCE_STARTING
        ]
        assert starting == []
        assert db.get_appliance_job(job_id)["status"] == "running"

    def test_starting_ping_fires_when_enabled(
        self, monkeypatch, appliance_id, patch_st
    ):
        monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
        monkeypatch.setattr(config, "APPLIANCE_NOTIFY_STARTING", True)
        patch_st.get_remote_control_enabled.return_value = True
        job_id = self._make_armed_job(appliance_id, datetime.now(UTC))
        with patch("src.notifier._dispatch") as mock_dispatch:
            appliance_dispatch._fire_cron(job_id)
        from src.notifier import AlertType
        starting = [
            c for c in mock_dispatch.call_args_list
            if c.args and c.args[0] == AlertType.APPLIANCE_STARTING
        ]
        assert len(starting) == 1


# ---------------------------------------------------------------------------
# pending_arm_change — the heartbeat's EDGE-triggered transition detector
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_remote_mode_cache():
    """Each test starts with a clean last-seen cache so edge detection is
    deterministic regardless of test order."""
    appliance_dispatch._last_remote_mode.clear()
    yield
    appliance_dispatch._last_remote_mode.clear()


class TestPendingArmChange:
    def test_first_observation_seeds_without_firing(self, appliance_id, patch_st):
        """Restart safety: the first observation never fires, even with Smart
        Control already on — otherwise a restart would auto-arm a leftover
        state (e.g. a finished cycle the user never switched off)."""
        patch_st.get_remote_control_enabled.return_value = True
        assert appliance_dispatch.pending_arm_change() is False

    def test_rising_edge_fires_when_no_job(self, appliance_id, patch_st):
        patch_st.get_remote_control_enabled.return_value = False
        assert appliance_dispatch.pending_arm_change() is False  # seed off
        patch_st.get_remote_control_enabled.return_value = True
        assert appliance_dispatch.pending_arm_change() is True   # off → on

    def test_steady_on_never_refires(self, appliance_id, patch_st):
        patch_st.get_remote_control_enabled.return_value = False
        appliance_dispatch.pending_arm_change()                  # seed off
        patch_st.get_remote_control_enabled.return_value = True
        assert appliance_dispatch.pending_arm_change() is True   # edge
        # Steady-on across subsequent heartbeats → no re-fire (storm guard).
        assert appliance_dispatch.pending_arm_change() is False
        assert appliance_dispatch.pending_arm_change() is False

    def test_declined_arm_does_not_storm(
        self, monkeypatch, appliance_id, patch_st
    ):
        """Rising edge with NO room before the deadline: reconcile would
        decline to create a job. The detector must still go quiet on the next
        heartbeat (edge already consumed) — no forced-solve storm."""
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        patch_st.get_remote_control_enabled.return_value = False
        appliance_dispatch.pending_arm_change()                  # seed off
        patch_st.get_remote_control_enabled.return_value = True
        # First edge fires (caller then runs reconcile, which may decline).
        assert appliance_dispatch.pending_arm_change() is True
        # No job got created (simulate the decline) — but no transition now.
        assert db.get_active_appliance_job(appliance_id) is None
        assert appliance_dispatch.pending_arm_change() is False
        assert appliance_dispatch.pending_arm_change() is False

    def test_no_rerun_after_completed_cycle_with_remote_left_on(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        """A finished cycle leaves no active job; if the user never switched
        Smart Control off there is no new edge, so the heartbeat must NOT
        auto-arm a second wash."""
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        now = datetime.now(UTC)
        seed_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        _seed_agile_rates(seed_start, [10.0, 5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        # Seed off, then rising edge arms a job.
        patch_st.get_remote_control_enabled.return_value = False
        appliance_dispatch.pending_arm_change()
        patch_st.get_remote_control_enabled.return_value = True
        assert appliance_dispatch.pending_arm_change() is True
        appliance_dispatch.reconcile()
        job = db.get_active_appliance_job(appliance_id)
        assert job is not None
        # Cycle completes; remote stays on (no toggle).
        db.update_appliance_job(int(job["id"]), status="completed")
        assert db.get_active_appliance_job(appliance_id) is None
        assert appliance_dispatch.pending_arm_change() is False
        assert appliance_dispatch.pending_arm_change() is False

    def test_falling_edge_fires_when_job_still_scheduled(
        self, monkeypatch, appliance_id, fake_scheduler, patch_st
    ):
        monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE")
        now = datetime.now(UTC)
        seed_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        _seed_agile_rates(seed_start, [10.0, 5.0, 5.0, 5.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        patch_st.get_remote_control_enabled.return_value = False
        appliance_dispatch.pending_arm_change()
        patch_st.get_remote_control_enabled.return_value = True
        appliance_dispatch.pending_arm_change()
        appliance_dispatch.reconcile()
        assert db.get_active_appliance_job(appliance_id) is not None
        # User cancels on the unit → falling edge with a job still scheduled.
        patch_st.get_remote_control_enabled.return_value = False
        assert appliance_dispatch.pending_arm_change() is True

    def test_smartthings_error_leaves_cache_untouched(self, appliance_id, patch_st):
        patch_st.get_remote_control_enabled.return_value = False
        appliance_dispatch.pending_arm_change()                  # seed off
        patch_st.get_remote_control_enabled.side_effect = SmartThingsError(
            "transport", "boom"
        )
        assert appliance_dispatch.pending_arm_change() is False
        # Recovery to 'on' must read as a real off→on edge, not a phantom one.
        patch_st.get_remote_control_enabled.side_effect = None
        patch_st.get_remote_control_enabled.return_value = True
        assert appliance_dispatch.pending_arm_change() is True

    def test_false_when_dispatch_disabled(self, monkeypatch, appliance_id, patch_st):
        monkeypatch.setattr(config, "APPLIANCE_DISPATCH_ENABLED", False)
        patch_st.get_remote_control_enabled.return_value = True
        assert appliance_dispatch.pending_arm_change() is False


# ---------------------------------------------------------------------------
# appliance_load_profile_kw — what the LP sees
# ---------------------------------------------------------------------------

class TestApplianceLoadProfile:
    def test_empty_when_no_active_jobs(self, monkeypatch):
        out = appliance_dispatch.appliance_load_profile_kw(
            datetime(2026, 5, 1, tzinfo=UTC),
            datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert out == {}

    def test_one_armed_session_contributes_per_slot(self, monkeypatch, appliance_id):
        # Plan a 2-hour cycle starting at 02:00 UTC, typical_kw=0.5.
        start = datetime(2026, 5, 2, 2, 0, tzinfo=UTC)
        end = start + timedelta(hours=2)
        db.create_appliance_job(
            appliance_id=appliance_id,
            status="scheduled",
            armed_at_utc=start.isoformat(),
            deadline_utc=(start + timedelta(hours=5)).isoformat(),
            duration_minutes=120,
            planned_start_utc=start.isoformat(),
            planned_end_utc=end.isoformat(),
            avg_price_pence=5.0,
        )
        profile = appliance_dispatch.appliance_load_profile_kw(
            datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 2, 6, 0, tzinfo=UTC),
        )
        # 4 half-hour slots, 0.5 kW each.
        expected_slots = [start + timedelta(minutes=30 * i) for i in range(4)]
        for s in expected_slots:
            assert profile.get(s) == pytest.approx(0.5)
        # Slots before/after window are absent.
        assert (start - timedelta(minutes=30)) not in profile
        assert end not in profile

    def test_disabled_returns_empty(self, monkeypatch, appliance_id):
        monkeypatch.setattr(config, "APPLIANCE_DISPATCH_ENABLED", False)
        # Even with an armed job in the DB.
        start = datetime(2026, 5, 2, 2, 0, tzinfo=UTC)
        db.create_appliance_job(
            appliance_id=appliance_id, status="scheduled",
            armed_at_utc=start.isoformat(),
            deadline_utc=(start + timedelta(hours=5)).isoformat(),
            duration_minutes=120,
            planned_start_utc=start.isoformat(),
            planned_end_utc=(start + timedelta(hours=2)).isoformat(),
        )
        out = appliance_dispatch.appliance_load_profile_kw(
            datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 2, 6, 0, tzinfo=UTC),
        )
        assert out == {}


# ---------------------------------------------------------------------------
# rehydrate_crons — survives HEM restart
# ---------------------------------------------------------------------------

class TestRehydrate:
    def test_future_scheduled_job_is_re_registered(
        self, appliance_id, fake_scheduler
    ):
        future = datetime.now(UTC) + timedelta(hours=4)
        job_id = db.create_appliance_job(
            appliance_id=appliance_id, status="scheduled",
            armed_at_utc=datetime.now(UTC).isoformat(),
            deadline_utc=(future + timedelta(hours=2)).isoformat(),
            duration_minutes=120,
            planned_start_utc=future.isoformat(),
            planned_end_utc=(future + timedelta(hours=2)).isoformat(),
        )
        summary = appliance_dispatch.rehydrate_crons()
        assert summary["registered"] == 1
        assert summary["expired"] == 0
        assert any(c[0] == f"appliance_fire_{appliance_id}" for c in fake_scheduler.add_calls)

    def test_past_scheduled_job_is_marked_expired(
        self, appliance_id, fake_scheduler
    ):
        past = datetime.now(UTC) - timedelta(hours=2)
        job_id = db.create_appliance_job(
            appliance_id=appliance_id, status="scheduled",
            armed_at_utc=past.isoformat(),
            deadline_utc=(past + timedelta(hours=2)).isoformat(),
            duration_minutes=120,
            planned_start_utc=past.isoformat(),
            planned_end_utc=(past + timedelta(hours=2)).isoformat(),
        )
        with patch.object(appliance_dispatch, "notify_risk"):
            summary = appliance_dispatch.rehydrate_crons()
        assert summary["expired"] == 1
        row = db.get_appliance_job(job_id)
        assert row["status"] == "expired"
