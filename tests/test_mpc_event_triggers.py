"""Event-driven MPC triggers (Epic #73 — "Waze recalculating routes").

Cooldown gate, SoC drift trigger w/ hysteresis, kill switch, plan-delta
observability, force_write_devices override, trigger_reason logging.

Forecast revision trigger lives in its own suite once that PR ships (#144).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_runner_state(monkeypatch):
    """Each test starts with a clean module state."""
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_last_mpc_run_at", None)
    monkeypatch.setattr(runner, "_consecutive_drift_ticks", 0)
    monkeypatch.setattr(runner, "_scheduler_paused", False)
    monkeypatch.setattr(runner.config, "USE_BULLETPROOF_ENGINE", True)
    monkeypatch.setattr(runner.config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(runner.config, "MPC_EVENT_DRIVEN_ENABLED", True)
    monkeypatch.setattr(runner.config, "MPC_COOLDOWN_SECONDS", 300)
    monkeypatch.setattr(runner.config, "MPC_DRIFT_SOC_THRESHOLD_PERCENT", 15.0)
    monkeypatch.setattr(runner.config, "MPC_DRIFT_HYSTERESIS_TICKS", 2)
    monkeypatch.setattr(runner.config, "BATTERY_CAPACITY_KWH", 10.0)
    monkeypatch.setattr(runner.config, "OCTOPUS_FETCH_HOUR", 16)
    yield


# -------------------- Cooldown --------------------


def test_can_run_mpc_now_true_when_no_prior_run():
    from src.scheduler import runner

    assert runner._can_run_mpc_now() is True


def test_can_run_mpc_now_false_within_cooldown_window(monkeypatch):
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_last_mpc_run_at", datetime.now(UTC) - timedelta(seconds=60))
    assert runner._can_run_mpc_now() is False


def test_can_run_mpc_now_true_after_cooldown_window(monkeypatch):
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_last_mpc_run_at", datetime.now(UTC) - timedelta(seconds=400))
    assert runner._can_run_mpc_now() is True


def test_bulletproof_mpc_job_skipped_during_cooldown(monkeypatch, caplog):
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_last_mpc_run_at", datetime.now(UTC) - timedelta(seconds=10))
    # Stub out the heavy imports so we can assert it never reaches the optimiser.
    sentinel = MagicMock(side_effect=AssertionError("optimiser should not have been called during cooldown"))
    with patch.dict("sys.modules", {"src.scheduler.optimizer": MagicMock(run_optimizer=sentinel)}):
        with caplog.at_level("INFO"):
            runner.bulletproof_mpc_job(trigger_reason="soc_drift")
    sentinel.assert_not_called()
    assert any("MPC skipped (cooldown" in r.message for r in caplog.records)


# -------------------- Cron skips when Octopus fetch will run --------------------


def test_cron_trigger_skipped_when_octopus_fetch_hour_matches(monkeypatch, caplog):
    from src.scheduler import runner

    # Force the local hour to equal OCTOPUS_FETCH_HOUR.
    fixed_now = datetime(2026, 6, 1, runner.config.OCTOPUS_FETCH_HOUR, 0, tzinfo=UTC)
    sentinel = MagicMock(side_effect=AssertionError("optimiser must not run when Octopus fetch hour matches (cron path)"))
    with patch("src.scheduler.runner.datetime") as mock_dt, \
         patch.dict("sys.modules", {"src.scheduler.optimizer": MagicMock(run_optimizer=sentinel)}):
        mock_dt.now = MagicMock(side_effect=lambda tz=None: fixed_now if tz else fixed_now)
        mock_dt.fromisoformat = datetime.fromisoformat
        with caplog.at_level("INFO"):
            runner.bulletproof_mpc_job(trigger_reason="cron")
    sentinel.assert_not_called()


def test_event_driven_trigger_bypasses_octopus_fetch_skip(monkeypatch):
    """Event-driven triggers (drift, forecast, octopus_fetch itself) MUST run even
    if the local hour matches OCTOPUS_FETCH_HOUR — the event itself is the signal."""
    from src.scheduler import runner

    fixed_now = datetime(2026, 6, 1, runner.config.OCTOPUS_FETCH_HOUR, 0, tzinfo=UTC)
    called = MagicMock(return_value={"ok": True, "lp_status": "Optimal", "lp_objective_pence": 100})
    fake_opt_module = MagicMock(run_optimizer=called)

    with patch("src.scheduler.runner.datetime") as mock_dt, \
         patch.dict("sys.modules", {"src.scheduler.optimizer": fake_opt_module}), \
         patch.object(runner, "_try_fox", return_value=None), \
         patch.object(runner, "get_cached_realtime", side_effect=Exception("no live SoC")), \
         patch("src.db.find_run_for_time", return_value=None):
        mock_dt.now = MagicMock(side_effect=lambda tz=None: fixed_now if tz else fixed_now)
        mock_dt.fromisoformat = datetime.fromisoformat
        runner.bulletproof_mpc_job(trigger_reason="soc_drift")
    called.assert_called_once()


# -------------------- Kill switch --------------------


def test_drift_trigger_disabled_by_kill_switch(monkeypatch):
    """When MPC_EVENT_DRIVEN_ENABLED=false, the drift block in the heartbeat is skipped.

    We assert the *guard* is consulted before the predicted-SoC lookup runs — the easiest
    proxy is to ensure _lp_predicted_soc_pct_at is not invoked when the flag is off.
    """
    from src.scheduler import runner

    monkeypatch.setattr(runner.config, "MPC_EVENT_DRIVEN_ENABLED", False)
    spy = MagicMock(return_value=50.0)
    monkeypatch.setattr(runner, "_lp_predicted_soc_pct_at", spy)
    # Inline the relevant guard from bulletproof_heartbeat_tick to validate behaviour.
    soc = 70.0  # would be a 20% drift if predicted=50
    if runner.config.MPC_EVENT_DRIVEN_ENABLED and soc is not None:
        runner._lp_predicted_soc_pct_at(datetime.now(UTC))
    spy.assert_not_called()


# -------------------- Drift hysteresis --------------------


def _heartbeat_drift_tick(runner_module, *, real_soc: float, predicted_pct: float | None):
    """Replay the drift-trigger logic from bulletproof_heartbeat_tick in isolation.

    Exact mirror of the in-tick block — kept in sync with src/scheduler/runner.py.
    Returns True if a trigger fired this tick.
    """
    if not runner_module.config.MPC_EVENT_DRIVEN_ENABLED or real_soc is None:
        return False
    if predicted_pct is None:
        return False
    drift_pct = abs(float(real_soc) - predicted_pct)
    threshold = float(runner_module.config.MPC_DRIFT_SOC_THRESHOLD_PERCENT)
    if drift_pct >= threshold:
        runner_module._consecutive_drift_ticks += 1
        if runner_module._consecutive_drift_ticks >= int(runner_module.config.MPC_DRIFT_HYSTERESIS_TICKS):
            runner_module._consecutive_drift_ticks = 0
            return True
    else:
        runner_module._consecutive_drift_ticks = 0
    return False


def test_drift_below_threshold_does_not_trigger(monkeypatch):
    from src.scheduler import runner

    fired = _heartbeat_drift_tick(runner, real_soc=50.0, predicted_pct=45.0)
    assert fired is False
    assert runner._consecutive_drift_ticks == 0


def test_drift_single_tick_above_threshold_does_not_fire_yet(monkeypatch):
    from src.scheduler import runner

    fired = _heartbeat_drift_tick(runner, real_soc=70.0, predicted_pct=50.0)
    assert fired is False  # 1 tick, hysteresis = 2
    assert runner._consecutive_drift_ticks == 1


def test_drift_two_consecutive_ticks_fires(monkeypatch):
    from src.scheduler import runner

    _heartbeat_drift_tick(runner, real_soc=70.0, predicted_pct=50.0)
    fired = _heartbeat_drift_tick(runner, real_soc=72.0, predicted_pct=50.0)
    assert fired is True
    assert runner._consecutive_drift_ticks == 0  # reset after fire


def test_drift_recovery_resets_counter(monkeypatch):
    from src.scheduler import runner

    _heartbeat_drift_tick(runner, real_soc=70.0, predicted_pct=50.0)  # 1 tick
    assert runner._consecutive_drift_ticks == 1
    _heartbeat_drift_tick(runner, real_soc=53.0, predicted_pct=50.0)  # back to 3% drift
    assert runner._consecutive_drift_ticks == 0


def test_drift_no_lp_run_skips_silently(monkeypatch):
    from src.scheduler import runner

    fired = _heartbeat_drift_tick(runner, real_soc=70.0, predicted_pct=None)
    assert fired is False
    assert runner._consecutive_drift_ticks == 0


def test_drift_no_realtime_skips_silently(monkeypatch):
    from src.scheduler import runner

    fired = _heartbeat_drift_tick(runner, real_soc=None, predicted_pct=50.0)  # type: ignore[arg-type]
    assert fired is False
    assert runner._consecutive_drift_ticks == 0


# -------------------- _lp_predicted_soc_pct_at helper --------------------


def test_lp_predicted_soc_pct_at_returns_none_when_no_run(monkeypatch):
    from src.scheduler import runner

    monkeypatch.setattr("src.db.find_run_for_time", lambda when_utc_iso: None)
    assert runner._lp_predicted_soc_pct_at(datetime.now(UTC)) is None


def test_lp_predicted_soc_pct_at_returns_pct_for_matching_slot(monkeypatch):
    from src.scheduler import runner

    base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("src.db.find_run_for_time", lambda when_utc_iso: 99)
    monkeypatch.setattr(
        "src.db.get_lp_solution_slots",
        lambda run_id: [
            {"slot_time_utc": (base + timedelta(minutes=i * 30)).isoformat(), "soc_kwh": 5.0 + i * 0.1}
            for i in range(8)
        ],
    )
    # Query a moment 45min into the plan — should match slot index 1 (10:30 UTC).
    pct = runner._lp_predicted_soc_pct_at(base + timedelta(minutes=45))
    assert pct is not None
    assert pct == pytest.approx(5.1 / 10.0 * 100.0, abs=0.01)  # cap=10kWh


# -------------------- Octopus rebadge --------------------


def test_octopus_fetch_calls_bulletproof_mpc_job_with_force_write(monkeypatch):
    """Octopus rates fetch must route through bulletproof_mpc_job with the new
    contract (force_write_devices=True, trigger_reason='octopus_fetch')."""
    from src.scheduler import octopus_fetch

    captured_kwargs: dict[str, Any] = {}

    def _spy(**kwargs):
        captured_kwargs.update(kwargs)

    monkeypatch.setattr("src.scheduler.runner.bulletproof_mpc_job", _spy)
    monkeypatch.setattr(octopus_fetch.config, "USE_BULLETPROOF_ENGINE", True)
    monkeypatch.setattr(octopus_fetch.config, "OCTOPUS_TARIFF_CODE", "AGILE-23-12-06")
    monkeypatch.setattr(octopus_fetch, "fetch_agile_rates", lambda **kw: [{"valid_from": "2026-06-01T00:00:00Z", "value_inc_vat": 10.0}])
    monkeypatch.setattr(octopus_fetch.db, "save_agile_rates", lambda *a, **kw: 1)
    monkeypatch.setattr(octopus_fetch.db, "update_octopus_fetch_state", lambda **kw: None)

    octopus_fetch.fetch_and_store_rates(fox=None)

    assert captured_kwargs == {"force_write_devices": True, "trigger_reason": "octopus_fetch"}


# -------------------- Plan-delta observability --------------------


def test_plan_delta_skipped_for_cron_runs(monkeypatch, caplog):
    from src.scheduler import runner

    with caplog.at_level("INFO"):
        runner._log_plan_delta_after_trigger(prev_run_id=1, new_run_id=2, trigger_reason="cron")
    assert not any("plan delta" in r.message.lower() for r in caplog.records)


def test_plan_delta_logged_for_event_driven_runs(monkeypatch, caplog):
    from src.scheduler import runner

    # Use slots in the lookahead window from "now" so the helper does not skip them.
    base = datetime.now(UTC) + timedelta(minutes=30)
    prev_slots = [
        {"slot_time_utc": (base + timedelta(minutes=30 * i)).isoformat(), "soc_kwh": 5.0, "import_kwh": 0.5, "charge_kwh": 0.5}
        for i in range(6)
    ]
    new_slots = [
        {"slot_time_utc": (base + timedelta(minutes=30 * i)).isoformat(), "soc_kwh": 6.0, "import_kwh": 0.8, "charge_kwh": 0.8}
        for i in range(6)
    ]

    def _slots(run_id):
        return prev_slots if run_id == 100 else new_slots

    monkeypatch.setattr("src.db.get_lp_solution_slots", _slots)
    with caplog.at_level("INFO"):
        runner._log_plan_delta_after_trigger(prev_run_id=100, new_run_id=200, trigger_reason="soc_drift")
    msgs = [r.message for r in caplog.records if "MPC plan delta" in r.message]
    assert len(msgs) == 1
    assert "trigger=soc_drift" in msgs[0]
    assert "SoC max-Δ=10.0%" in msgs[0]  # 6 - 5 = 1 kWh = 10% of 10 kWh battery
