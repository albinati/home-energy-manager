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
    """Insert a single appliance and return its id."""
    return db.add_appliance(
        vendor="smartthings",
        vendor_device_id="dev-test",
        name="Test Washer",
        device_type="washer",
        default_duration_minutes=120,
        deadline_local_time="07:00",
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
