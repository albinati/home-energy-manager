"""Solve+dispatch mutex (#676) — serialize concurrent LP re-solves.

The MPC cooldown is check-at-entry / stamp-at-completion, so a heartbeat-thread
trigger (soc_drift) and an APScheduler worker-thread trigger (tier_boundary,
plan_push, ...) could interleave two `run_optimizer` executions and with them
two Fox V3 uploads. `optimizer_dispatch_lock` closes that window:

- `bulletproof_mpc_job` acquires NON-blocking and skips when a solve is
  already in flight (the in-flight solve reads the freshest state anyway);
- `bulletproof_plan_push_job` acquires BLOCKING (the nightly canonical
  commitment must never be silently skipped);
- the lock is always released, including on solve exceptions.

All solves are stubbed — no LP, no HTTP.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_runner_state(monkeypatch):
    """Clean module state per test; never leak a held lock into the next test."""
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_last_mpc_run_at", None)
    monkeypatch.setattr(runner, "_scheduler_paused", False)
    monkeypatch.setattr(runner.config, "USE_BULLETPROOF_ENGINE", True)
    monkeypatch.setattr(runner.config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(runner.config, "MPC_COOLDOWN_SECONDS", 300)
    monkeypatch.setattr(runner.config, "DAIKIN_CLIENT_ID", "")
    monkeypatch.setattr(runner.config, "DAIKIN_CLIENT_SECRET", "")
    yield
    if runner.optimizer_dispatch_lock.locked():  # pragma: no cover — test bug guard
        runner.optimizer_dispatch_lock.release()


def _job_patches(runner, solve):
    """Patches shared by every stubbed job run: fake optimizer module, no Fox,
    no realtime, no DB run lookups."""
    return (
        patch.dict("sys.modules", {"src.scheduler.optimizer": MagicMock(run_optimizer=solve)}),
        patch.object(runner, "_try_fox", return_value=None),
        patch.object(runner, "get_cached_realtime", side_effect=Exception("no live SoC")),
        patch("src.db.find_run_for_time", return_value=None),
    )


OK_RESULT = {"ok": True, "lp_status": "Optimal", "lp_objective_pence": 100.0}


# -------------------- (1) concurrent MPC entry skips --------------------


def test_second_concurrent_mpc_entry_skips_while_first_holds_lock(caplog):
    from src.scheduler import runner

    entered = threading.Event()
    hold = threading.Event()
    calls: list[str] = []

    def slow_solve(*args, **kwargs):
        calls.append(kwargs.get("trigger_reason", "?"))
        entered.set()
        assert hold.wait(timeout=10), "test never released the solve"
        return dict(OK_RESULT)

    p1, p2, p3, p4 = _job_patches(runner, slow_solve)
    with p1, p2, p3, p4:
        t1 = threading.Thread(
            target=runner.bulletproof_mpc_job, kwargs={"trigger_reason": "soc_drift"}
        )
        t1.start()
        try:
            assert entered.wait(timeout=5), "first solve never started"
            # Second entry while the first solve is in flight → non-blocking skip.
            with caplog.at_level("INFO", logger="src.scheduler.runner"):
                runner.bulletproof_mpc_job(trigger_reason="tier_boundary", bypass_cooldown=True)
        finally:
            hold.set()
            t1.join(timeout=10)
        assert not t1.is_alive()

    assert calls == ["soc_drift"], "the skipped entry must not have solved"
    assert any("MPC skipped (already running" in r.message for r in caplog.records)
    assert not runner.optimizer_dispatch_lock.locked()


# -------------------- (2) plan_push blocks, then runs --------------------


def test_plan_push_waits_for_inflight_solve_then_runs():
    from src.scheduler import runner

    entered = threading.Event()
    hold = threading.Event()
    calls: list[str] = []

    def solve(*args, **kwargs):
        calls.append(kwargs.get("trigger_reason", "?"))
        if len(calls) == 1:  # only the first (MPC) solve is held open
            entered.set()
            assert hold.wait(timeout=10), "test never released the solve"
        return dict(OK_RESULT)

    p1, p2, p3, p4 = _job_patches(runner, solve)
    with p1, p2, p3, p4:
        t_mpc = threading.Thread(
            target=runner.bulletproof_mpc_job, kwargs={"trigger_reason": "soc_drift"}
        )
        t_mpc.start()
        try:
            assert entered.wait(timeout=5), "MPC solve never started"
            t_push = threading.Thread(target=runner.bulletproof_plan_push_job)
            t_push.start()
            # Blocking semantics: plan_push must be parked on the lock, NOT
            # skipped and NOT solving concurrently.
            time.sleep(0.3)
            assert t_push.is_alive(), "plan_push should be blocked on the dispatch lock"
            assert calls == ["soc_drift"], "plan_push must not solve while MPC holds the lock"
        finally:
            hold.set()
            t_mpc.join(timeout=10)
        t_push.join(timeout=10)
        assert not t_push.is_alive()

    assert calls == ["soc_drift", "plan_push"], "plan_push must run after the in-flight solve"
    assert not runner.optimizer_dispatch_lock.locked()


# -------------------- (3) lock released on exception --------------------


def test_mpc_job_releases_lock_when_solve_raises():
    from src.scheduler import runner

    boom = MagicMock(side_effect=RuntimeError("solver exploded"))
    p1, p2, p3, p4 = _job_patches(runner, boom)
    with p1, p2, p3, p4:
        runner.bulletproof_mpc_job(trigger_reason="soc_drift")  # must not raise
    boom.assert_called_once()
    assert not runner.optimizer_dispatch_lock.locked()
    # Reacquirable: the next entry must reach the solver again, not skip.
    ok = MagicMock(return_value=dict(OK_RESULT))
    p1, p2, p3, p4 = _job_patches(runner, ok)
    with p1, p2, p3, p4:
        runner.bulletproof_mpc_job(trigger_reason="tier_boundary")
    ok.assert_called_once()
    assert not runner.optimizer_dispatch_lock.locked()


def test_plan_push_releases_lock_when_solve_raises():
    from src.scheduler import runner

    boom = MagicMock(side_effect=RuntimeError("solver exploded"))
    p1, p2, p3, p4 = _job_patches(runner, boom)
    with p1, p2, p3, p4:
        runner.bulletproof_plan_push_job()  # must not raise
    boom.assert_called_once()
    assert not runner.optimizer_dispatch_lock.locked()
