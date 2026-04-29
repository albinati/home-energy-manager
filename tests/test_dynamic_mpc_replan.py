"""Dynamic MPC re-plan helper: schedules a one-shot APScheduler job to fire
shortly before a truncated Fox V3 plan tail runs out, so the inverter is never
left without a fresh schedule.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


class _FakeJob:
    def __init__(self, jid: str, trigger: Any, replace_existing: bool, kwargs: dict[str, Any] | None = None):
        self.id = jid
        self.trigger = trigger
        self.replace_existing = replace_existing
        self.kwargs = kwargs or {}


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[_FakeJob] = []
        self.add_calls: list[tuple[str, Any, bool, dict[str, Any]]] = []

    def add_job(
        self,
        func: Any,
        trigger: Any,
        *,
        id: str,  # noqa: A002 — APScheduler API
        replace_existing: bool = False,
        kwargs: dict[str, Any] | None = None,
    ) -> _FakeJob:
        # Mimic replace_existing semantics so the test can assert no piling up.
        if replace_existing:
            self.jobs = [j for j in self.jobs if j.id != id]
        job = _FakeJob(id, trigger, replace_existing, kwargs)
        self.jobs.append(job)
        self.add_calls.append((id, trigger, replace_existing, kwargs or {}))
        return job


@pytest.fixture
def fake_scheduler(monkeypatch: pytest.MonkeyPatch) -> _FakeScheduler:
    from src.scheduler import runner

    fake = _FakeScheduler()
    monkeypatch.setattr(runner, "_background_scheduler", fake)
    monkeypatch.setattr(runner, "_scheduler_paused", False)
    monkeypatch.setattr(runner.config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(runner.config, "REPLAN_SAFETY_MARGIN_MINUTES", 15)
    monkeypatch.setattr(runner.config, "DYNAMIC_REPLAN_MIN_LEAD_MINUTES", 120)
    return fake


def test_schedule_dynamic_mpc_replan_happy_path_schedules_one_shot(
    fake_scheduler: _FakeScheduler,
) -> None:
    from src.scheduler.runner import schedule_dynamic_mpc_replan

    when = datetime.now(UTC) + timedelta(hours=6)
    out = schedule_dynamic_mpc_replan(when)
    assert out["status"] == "scheduled"
    assert len(fake_scheduler.jobs) == 1
    job = fake_scheduler.jobs[0]
    assert job.id == "dynamic_mpc_replan"
    assert job.replace_existing is True


def test_schedule_dynamic_mpc_replan_inactive_when_no_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.scheduler import runner
    from src.scheduler.runner import schedule_dynamic_mpc_replan

    monkeypatch.setattr(runner, "_background_scheduler", None)
    out = schedule_dynamic_mpc_replan(datetime.now(UTC) + timedelta(hours=6))
    assert out["status"] == "inactive"


def test_schedule_dynamic_mpc_replan_skipped_when_lead_too_short(
    fake_scheduler: _FakeScheduler,
) -> None:
    from src.scheduler.runner import schedule_dynamic_mpc_replan

    # 30 min from now → minus 15 min margin = 15 min lead, way below 120.
    when = datetime.now(UTC) + timedelta(minutes=30)
    out = schedule_dynamic_mpc_replan(when)
    assert out["status"] == "skipped_lead_too_short"
    assert fake_scheduler.jobs == []


def test_schedule_dynamic_mpc_replan_skipped_when_paused(
    fake_scheduler: _FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.scheduler import runner
    from src.scheduler.runner import schedule_dynamic_mpc_replan

    monkeypatch.setattr(runner, "_scheduler_paused", True)
    out = schedule_dynamic_mpc_replan(datetime.now(UTC) + timedelta(hours=6))
    assert out["status"] == "paused"
    assert fake_scheduler.jobs == []


def test_schedule_dynamic_mpc_replan_no_longer_dedupes_against_fixed_cron(
    fake_scheduler: _FakeScheduler,
) -> None:
    """V12 removed the fixed-hour MPC cron + its overlap dedup. With the
    cron gone, ``schedule_dynamic_mpc_replan`` always schedules the
    one-shot when lead is sufficient — there's nothing to coalesce with.
    Cooldown via ``_can_run_mpc_now`` still gates back-to-back fires at
    runtime if needed."""
    from src.scheduler.runner import schedule_dynamic_mpc_replan

    when = datetime.now(UTC) + timedelta(hours=5)
    out = schedule_dynamic_mpc_replan(when)
    assert out["status"] == "scheduled"
    assert len(fake_scheduler.jobs) == 1


def test_schedule_dynamic_mpc_replan_replace_existing_does_not_pile_up(
    fake_scheduler: _FakeScheduler,
) -> None:
    """Back-to-back overflow plans must reuse the same job id, not stack."""
    from src.scheduler.runner import schedule_dynamic_mpc_replan

    when_1 = datetime.now(UTC) + timedelta(hours=6)
    when_2 = datetime.now(UTC) + timedelta(hours=4)
    schedule_dynamic_mpc_replan(when_1)
    schedule_dynamic_mpc_replan(when_2)
    assert len(fake_scheduler.jobs) == 1
    assert fake_scheduler.jobs[0].id == "dynamic_mpc_replan"


def test_schedule_dynamic_mpc_replan_passes_event_kwargs_to_mpc_job(
    fake_scheduler: _FakeScheduler,
) -> None:
    """The one-shot must invoke bulletproof_mpc_job with force_write_devices=True
    and trigger_reason='dynamic_replan' so the cooldown gate + log shape are honoured."""
    from src.scheduler.runner import schedule_dynamic_mpc_replan

    schedule_dynamic_mpc_replan(datetime.now(UTC) + timedelta(hours=6))
    assert len(fake_scheduler.jobs) == 1
    assert fake_scheduler.jobs[0].kwargs == {
        "force_write_devices": True,
        "trigger_reason": "dynamic_replan",
    }
