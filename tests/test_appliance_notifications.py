"""Laundry start/finish notifications via OpenClaw hook (PR #234).

Two new ``AlertType`` values + helpers + a 5-min reconcile-driven completion
poll. The poll fires the finished hook exactly once per cycle (DB transition
is the dedup key).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import db
from src.config import config as app_config
from src.notifier import (
    AlertType,
    notify_appliance_finished,
    notify_appliance_starting,
)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


# ---------- Notifier helpers ----------

def test_notify_appliance_starting_dispatches_with_correct_alert_type() -> None:
    with patch("src.notifier._dispatch") as mock_dispatch:
        notify_appliance_starting(
            appliance_name="Washer",
            planned_start_local="Sat 04:30",
            deadline_local="Sun 06:00",
            avg_price_pence=3.7,
            duration_minutes=77,
            brief_md="🔆 Today: ...",
        )
    assert mock_dispatch.called
    args, kwargs = mock_dispatch.call_args
    assert args[0] == AlertType.APPLIANCE_STARTING
    body = args[1]
    assert "Washer" in body
    assert "Sat 04:30" in body
    assert "77 min" in body
    assert "3.7p/kWh" in body
    assert "🔆 Today" in body
    assert kwargs["urgent"] is False
    extra = kwargs["extra"]
    assert extra["appliance"] == "Washer"
    assert extra["duration_minutes"] == 77


def test_notify_appliance_finished_dispatches_with_correct_alert_type() -> None:
    """Default path (kwh_is_measured=False) → uses ≈ prefix + estimated_* keys."""
    with patch("src.notifier._dispatch") as mock_dispatch:
        notify_appliance_finished(
            appliance_name="Washer",
            started_local="04:30",
            ended_local="05:47",
            duration_minutes=77,
            avg_price_pence=3.7,
            estimated_kwh=0.6,
            estimated_cost_p=2.22,
            brief_md="🔆 Today: ...",
        )
    assert mock_dispatch.called
    args, kwargs = mock_dispatch.call_args
    assert args[0] == AlertType.APPLIANCE_FINISHED
    body = args[1]
    assert "Washer" in body
    assert "04:30" in body and "05:47" in body
    assert "77 min" in body
    assert "≈ 0.60 kWh" in body
    assert kwargs["extra"]["estimated_kwh"] == 0.6
    assert kwargs["extra"]["estimated_cost_pence"] == 2.22
    assert kwargs["extra"]["kwh_is_measured"] is False


def test_notify_appliance_finished_measured_path_drops_approx_prefix() -> None:
    """PR #235: when Samsung powerConsumptionReport gave us a real delta,
    body uses the value without the ≈ ambiguity marker, and the structured
    extra carries actual_kwh + actual_cost_pence (not the estimated_* keys)."""
    with patch("src.notifier._dispatch") as mock_dispatch:
        notify_appliance_finished(
            appliance_name="Washer",
            started_local="16:16",
            ended_local="18:00",
            duration_minutes=104,
            avg_price_pence=3.7,
            estimated_kwh=0.87,
            estimated_cost_p=3.22,
            kwh_is_measured=True,
        )
    args, kwargs = mock_dispatch.call_args
    body = args[1]
    assert "0.87 kWh" in body
    assert "≈" not in body                          # measured → no approximation marker
    extra = kwargs["extra"]
    assert extra["kwh_is_measured"] is True
    assert extra["actual_kwh"] == 0.87
    assert extra["actual_cost_pence"] == 3.22
    assert "estimated_kwh" not in extra
    assert "estimated_cost_pence" not in extra


def test_notify_appliance_finished_handles_missing_cost_estimate() -> None:
    """No estimated_kwh + no avg_price → still emits a clean body without crashing."""
    with patch("src.notifier._dispatch") as mock_dispatch:
        notify_appliance_finished(
            appliance_name="Washer",
            started_local="04:30",
            ended_local="05:47",
            duration_minutes=77,
        )
    assert mock_dispatch.called


# ---------- build_brief_48h_summary degrades gracefully ----------

def test_brief_48h_summary_returns_string_with_no_data() -> None:
    """Empty DB → still produces 4 lines, all gracefully reading n/a."""
    from src.analytics.daily_brief import build_brief_48h_summary
    out = build_brief_48h_summary()
    lines = out.split("\n")
    assert len(lines) == 4
    assert lines[0].startswith("🔆 Today:")
    assert lines[1].startswith("🔆 Tomorrow:")
    assert lines[2].startswith("💰 Today PnL:")
    assert lines[3].startswith("🔋 Battery")


# ---------- Completion poll ----------

def _seed_running_job(
    state_returns: list[str | None],
    state_timestamps: list[str | None] | None = None,
) -> tuple[int, MagicMock]:
    """Insert one running job + a mock SmartThings client whose
    ``get_machine_state`` returns each ``(state, timestamp)`` tuple per call.

    ``state_timestamps`` defaults to ``[None] * len(state_returns)`` (no
    Samsung timestamp returned → caller falls back to ``now()``).
    """
    appliance_id = db.add_appliance(
        vendor="smartthings", vendor_device_id="dev-test",
        name="Washer", device_type="washer",
        default_duration_minutes=77, deadline_local_time="07:00",
        typical_kw=0.5,
    )
    now = datetime.now(UTC)
    job_id = db.create_appliance_job(
        appliance_id=appliance_id,
        armed_at_utc=now.isoformat().replace("+00:00", "Z"),
        deadline_utc=(now + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
        duration_minutes=77,
        planned_start_utc=now.isoformat().replace("+00:00", "Z"),
        planned_end_utc=(now + timedelta(minutes=77)).isoformat().replace("+00:00", "Z"),
        avg_price_pence=3.7,
        status="scheduled",
    )
    db.update_appliance_job(
        job_id, status="running",
        actual_start_utc=now.isoformat().replace("+00:00", "Z"),
    )
    mock_client = MagicMock()
    if state_timestamps is None:
        state_timestamps = [None] * len(state_returns)
    mock_client.get_machine_state.side_effect = list(
        zip(state_returns, state_timestamps, strict=True)
    )
    return job_id, mock_client


def test_poll_skips_when_state_still_run() -> None:
    """Job stays running, no notification, no DB change."""
    job_id, mock_client = _seed_running_job(state_returns=["run"])
    with patch("src.scheduler.appliance_dispatch._get_st_client", return_value=mock_client), \
         patch("src.notifier._dispatch") as mock_dispatch:
        from src.scheduler.appliance_dispatch import _poll_running_jobs
        _poll_running_jobs()
    job = db.get_appliance_job(job_id)
    assert job["status"] == "running"
    assert job.get("completed_at_utc") is None
    assert not mock_dispatch.called


def test_poll_marks_completed_and_fires_finish_hook_on_state_transition() -> None:
    """Job sees state='stop' → marked completed + finish notification fires once."""
    job_id, mock_client = _seed_running_job(state_returns=["stop"])
    with patch("src.scheduler.appliance_dispatch._get_st_client", return_value=mock_client), \
         patch("src.notifier._dispatch") as mock_dispatch:
        from src.scheduler.appliance_dispatch import _poll_running_jobs
        _poll_running_jobs()
    job = db.get_appliance_job(job_id)
    assert job["status"] == "completed"
    assert job["completed_at_utc"] is not None
    # Exactly one APPLIANCE_FINISHED dispatch
    finish_calls = [
        c for c in mock_dispatch.call_args_list
        if c.args[0] == AlertType.APPLIANCE_FINISHED
    ]
    assert len(finish_calls) == 1


def test_poll_idempotent_after_completion() -> None:
    """Once marked completed, second poll pass should NOT re-fire (status filter excludes it)."""
    job_id, mock_client = _seed_running_job(state_returns=["finish", "stop"])
    with patch("src.scheduler.appliance_dispatch._get_st_client", return_value=mock_client), \
         patch("src.notifier._dispatch") as mock_dispatch:
        from src.scheduler.appliance_dispatch import _poll_running_jobs
        _poll_running_jobs()
        _poll_running_jobs()                                # second pass — no rows where status='running'
    finish_calls = [
        c for c in mock_dispatch.call_args_list
        if c.args[0] == AlertType.APPLIANCE_FINISHED
    ]
    assert len(finish_calls) == 1


def test_poll_treats_finish_state_as_completion() -> None:
    """Samsung uses 'finish' too — must trigger completion same as 'stop'."""
    job_id, mock_client = _seed_running_job(state_returns=["finish"])
    with patch("src.scheduler.appliance_dispatch._get_st_client", return_value=mock_client), \
         patch("src.notifier._dispatch") as mock_dispatch:
        from src.scheduler.appliance_dispatch import _poll_running_jobs
        _poll_running_jobs()
    job = db.get_appliance_job(job_id)
    assert job["status"] == "completed"


def test_poll_uses_samsung_timestamp_over_detection_time() -> None:
    """PR #235: when Samsung returns the actual state-change timestamp, use that
    as ``completed_at_utc`` instead of ``now()``. Eliminates the up-to-5-min
    skew from the reconcile cadence."""
    actual_end_iso = "2026-05-02T17:00:10Z"
    job_id, mock_client = _seed_running_job(
        state_returns=["stop"],
        state_timestamps=[actual_end_iso],
    )
    with patch("src.scheduler.appliance_dispatch._get_st_client", return_value=mock_client), \
         patch("src.notifier._dispatch") as mock_dispatch:
        from src.scheduler.appliance_dispatch import _poll_running_jobs
        _poll_running_jobs()
    job = db.get_appliance_job(job_id)
    assert job["status"] == "completed"
    # Must persist Samsung's timestamp (normalised to canonical UTC Z), not now().
    assert job["completed_at_utc"] == actual_end_iso
    finish_call = next(
        c for c in mock_dispatch.call_args_list
        if c.args[0] == AlertType.APPLIANCE_FINISHED
    )
    extra = finish_call.kwargs["extra"]
    # The dispatched ``ended_local`` field is rendered from Samsung's timestamp.
    # Independent of the test runner's wall clock — locks in the fix.
    assert "17:00" in extra["ended_local"] or "18:00" in extra["ended_local"]
    # 18:00 BST during DST, 17:00 UTC otherwise — both acceptable.


def test_poll_falls_back_to_now_when_samsung_timestamp_missing() -> None:
    """No timestamp → must still mark complete; ended_at degrades to now()."""
    job_id, mock_client = _seed_running_job(
        state_returns=["stop"], state_timestamps=[None],
    )
    with patch("src.scheduler.appliance_dispatch._get_st_client", return_value=mock_client), \
         patch("src.notifier._dispatch") as mock_dispatch:
        from src.scheduler.appliance_dispatch import _poll_running_jobs
        _poll_running_jobs()
    job = db.get_appliance_job(job_id)
    assert job["status"] == "completed"
    assert job["completed_at_utc"] is not None       # populated, just from now()
    assert any(
        c.args[0] == AlertType.APPLIANCE_FINISHED for c in mock_dispatch.call_args_list
    )
