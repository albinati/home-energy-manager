"""PLAN_PROPOSED Telegram ping fires only on the nightly plan_push or a
user-initiated manual run. In-day MPC re-solves auto-apply silently.

Pre-cutoff (before 2026-05-10), every event-driven re-solve that produced a
new content hash sent another "📋 New energy plan" message — 3-5 redundant
pings per day on top of the morning brief. This regression test pins the
gate so a future refactor can't quietly re-enable the noise.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src import db
from src.config import config as app_config
from src.scheduler.optimizer import _write_plan_consent


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    monkeypatch.setattr(app_config, "PLAN_AUTO_APPROVE", True, raising=False)
    monkeypatch.setattr(app_config, "PLAN_NOTIFY_MIN_INTERVAL_SECONDS", 0, raising=False)
    db.init_db()


@pytest.mark.parametrize("trigger", ["plan_push", "manual"])
def test_notifies_for_user_facing_triggers(trigger: str) -> None:
    with patch("src.notifier.notify_plan_proposed") as mock_notify:
        _write_plan_consent("2026-06-01", "test plan", trigger_reason=trigger)
    assert mock_notify.call_count == 1, f"trigger={trigger} should ping"
    assert mock_notify.call_args.kwargs.get("auto_applied") is True


@pytest.mark.parametrize(
    "trigger",
    ["cron", "tier_boundary", "soc_drift", "forecast_revision",
     "dynamic_replan", "octopus_fetch", "pv_drift_up", "pv_drift_down"],
)
def test_silent_for_in_day_mpc_triggers(trigger: str) -> None:
    """Every in-day re-solve trigger must auto-apply silently — Telegram is
    pull-based for these. Users get the new plan via get_plan_timeline."""
    with patch("src.notifier.notify_plan_proposed") as mock_notify:
        _write_plan_consent("2026-06-01", "test plan", trigger_reason=trigger)
    assert mock_notify.call_count == 0, (
        f"trigger={trigger} should NOT ping — in-day re-solves are silent"
    )
    # But the consent row IS written + auto-approved.
    consent = db.get_plan_consent("2026-06-01")
    assert consent is not None
    assert consent["status"] == "approved"
