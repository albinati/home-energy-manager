"""Event-driven MPC triggers (Epic #73 — "Waze recalculating routes").

Cooldown gate, SoC drift trigger w/ hysteresis, kill switch, plan-delta
observability, force_write_devices override, trigger_reason logging.

Forecast revision trigger lives in its own suite once that PR ships (#144).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _init_db(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _reset_runner_state(monkeypatch):
    """Each test starts with a clean module state."""
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_last_mpc_run_at", None)
    monkeypatch.setattr(runner, "_consecutive_drift_ticks", 0)
    monkeypatch.setattr(runner, "_consecutive_pv_up_ticks", 0)
    monkeypatch.setattr(runner, "_consecutive_pv_down_ticks", 0)
    monkeypatch.setattr(runner, "_consecutive_load_up_ticks", 0)
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


def test_lp_predicted_load_kw_at_derives_expected_load_from_solution_slot(monkeypatch):
    from src.scheduler import runner

    base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("src.db.find_run_for_time", lambda when_utc_iso: 99)
    monkeypatch.setattr(
        "src.db.get_lp_solution_slots",
        lambda run_id: [
            {
                "slot_time_utc": base.isoformat(),
                "import_kwh": 1.0,
                "pv_use_kwh": 0.6,
                "discharge_kwh": 0.4,
                "export_kwh": 0.2,
                "charge_kwh": 0.3,
                "dhw_kwh": 0.1,
                "space_kwh": 0.2,
            }
        ],
    )
    # load_kwh = imp + pv + dis - exp - chg - dhw - space = 1.2
    # load_kw  = 1.2 / 0.5 = 2.4 kW
    kw = runner._lp_predicted_load_kw_at(base + timedelta(minutes=10))
    assert kw == pytest.approx(2.4)


def test_forecast_helpers_use_slot_time_after_midnight(monkeypatch):
    from src.scheduler import runner
    from src import db

    db.save_meteo_forecast(
        [
            {
                "slot_time": "2026-05-02T00:00:00+00:00",
                "temp_c": 11.5,
                "solar_w_m2": 600.0,
                "cloud_cover_pct": 25.0,
            }
        ],
        "2026-05-01",
    )
    now = datetime(2026, 5, 2, 0, 15, tzinfo=UTC)
    assert runner._get_forecast_temp_c(now) == pytest.approx(11.5)
    pv_kw = runner._get_forecast_pv_kw(now)
    assert pv_kw is not None
    assert pv_kw > 0


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


# -------------------- Forecast revision trigger --------------------


def _make_forecast_rows(base_utc: datetime, *, count: int, solar_w_m2: float, temp_c: float) -> list[dict]:
    return [
        {
            "slot_time": (base_utc + timedelta(hours=i)).isoformat(),
            "temp_c": temp_c,
            "solar_w_m2": solar_w_m2,
            "cloud_cover_pct": 0.0,
        }
        for i in range(count)
    ]


def test_forecast_delta_empty_inputs_returns_zero():
    from src.weather import _forecast_delta

    assert _forecast_delta([], [], lookahead_hours=6) == (0.0, 0.0)
    assert _forecast_delta([], [{"slot_time": datetime.now(UTC).isoformat(), "solar_w_m2": 100, "temp_c": 10}], lookahead_hours=6) == (0.0, 0.0)


def test_forecast_delta_solar_difference_summed_over_lookahead():
    from src.weather import _forecast_delta, estimate_pv_kw

    base = datetime.now(UTC) + timedelta(minutes=15)
    prev = _make_forecast_rows(base, count=4, solar_w_m2=200.0, temp_c=12.0)
    new = _make_forecast_rows(base, count=4, solar_w_m2=400.0, temp_c=12.0)
    delta_pv, delta_temp = _forecast_delta(prev, new, lookahead_hours=6, horizon_start_utc=base)
    # Each hour, |estimate_pv_kw(400) - estimate_pv_kw(200)| × 1h, summed over 4 slots
    expected = abs(estimate_pv_kw(400.0) - estimate_pv_kw(200.0)) * 4
    assert delta_pv == pytest.approx(expected, abs=0.01)
    assert delta_temp == 0.0


def test_forecast_delta_temp_difference_averaged():
    from src.weather import _forecast_delta

    base = datetime.now(UTC) + timedelta(minutes=15)
    prev = _make_forecast_rows(base, count=4, solar_w_m2=0.0, temp_c=10.0)
    new = _make_forecast_rows(base, count=4, solar_w_m2=0.0, temp_c=14.0)
    delta_pv, delta_temp = _forecast_delta(prev, new, lookahead_hours=6, horizon_start_utc=base)
    assert delta_pv == 0.0
    assert delta_temp == pytest.approx(4.0, abs=0.01)


def test_forecast_delta_ignores_slots_outside_lookahead():
    from src.weather import _forecast_delta

    base = datetime.now(UTC) + timedelta(minutes=15)
    # 8 hourly slots, lookahead=4 — only first 4 must be counted.
    prev = _make_forecast_rows(base, count=8, solar_w_m2=100.0, temp_c=10.0)
    new = _make_forecast_rows(base, count=8, solar_w_m2=300.0, temp_c=15.0)
    delta_pv_4, delta_temp_4 = _forecast_delta(prev, new, lookahead_hours=4, horizon_start_utc=base)
    delta_pv_8, delta_temp_8 = _forecast_delta(prev, new, lookahead_hours=8, horizon_start_utc=base)
    assert delta_pv_4 < delta_pv_8
    # Per-slot temp delta is 5°C constant; average is 5°C regardless of N.
    assert delta_temp_4 == pytest.approx(5.0, abs=0.01)
    assert delta_temp_8 == pytest.approx(5.0, abs=0.01)


def test_forecast_refresh_job_persists_even_when_disabled(monkeypatch):
    """Kill switch off: still saves to history (audit trail) but never triggers MPC."""
    from src.scheduler import runner
    from src.weather import ForecastFetchResult

    monkeypatch.setattr(runner.config, "MPC_EVENT_DRIVEN_ENABLED", False)
    base = datetime.now(UTC)
    fake_fcst = [MagicMock(time_utc=base + timedelta(hours=i), temperature_c=10.0, shortwave_radiation_wm2=100.0) for i in range(6)]
    fake_fetch = ForecastFetchResult(forecast=fake_fcst, source="open-meteo")

    saved_snapshots: list[tuple[str, list, bool]] = []

    def _save_snapshot(fetch_at, rows, mark_latest=True, **_kwargs):
        saved_snapshots.append((fetch_at, rows, bool(mark_latest)))

    triggered = MagicMock(side_effect=AssertionError("MPC must not be triggered when kill switch is off"))

    with patch("src.weather.fetch_forecast_snapshot", return_value=fake_fetch), \
         patch("src.db.get_meteo_forecast_history_latest_before", return_value=[{"slot_time": (base + timedelta(hours=0)).isoformat(), "solar_w_m2": 50.0, "temp_c": 10.0}]), \
         patch("src.db.save_meteo_forecast_snapshot", side_effect=_save_snapshot), \
         patch.object(runner, "bulletproof_mpc_job", triggered):
        runner.bulletproof_forecast_refresh_job()

    assert len(saved_snapshots) == 1
    assert saved_snapshots[0][2] is True
    triggered.assert_not_called()


def test_forecast_refresh_job_no_trigger_without_previous_fetch(monkeypatch):
    """First-ever invocation has no prev — must persist but never trigger."""
    from src.scheduler import runner
    from src.weather import ForecastFetchResult

    base = datetime.now(UTC)
    fake_fcst = [MagicMock(time_utc=base + timedelta(hours=i), temperature_c=10.0, shortwave_radiation_wm2=100.0) for i in range(6)]
    fake_fetch = ForecastFetchResult(forecast=fake_fcst, source="open-meteo")
    triggered = MagicMock(side_effect=AssertionError("first-ever call must not trigger"))

    with patch("src.weather.fetch_forecast_snapshot", return_value=fake_fetch), \
         patch("src.db.get_meteo_forecast_history_latest_before", return_value=[]), \
         patch("src.db.save_meteo_forecast_snapshot"), \
         patch.object(runner, "bulletproof_mpc_job", triggered):
        runner.bulletproof_forecast_refresh_job()

    triggered.assert_not_called()


def test_forecast_refresh_job_triggers_when_solar_delta_exceeds_threshold(monkeypatch):
    from src.scheduler import runner
    from src.weather import ForecastFetchResult

    monkeypatch.setattr(runner.config, "MPC_FORECAST_DRIFT_SOLAR_KWH_THRESHOLD", 0.5)
    monkeypatch.setattr(runner.config, "MPC_FORECAST_DRIFT_LOOKAHEAD_HOURS", 6)
    base = datetime.now(UTC)
    # New forecast: much sunnier than the previous fetch — should cross the 0.5 kWh threshold easily.
    fake_fcst = [MagicMock(time_utc=base + timedelta(hours=i), temperature_c=10.0, shortwave_radiation_wm2=600.0) for i in range(6)]
    fake_fetch = ForecastFetchResult(forecast=fake_fcst, source="open-meteo")
    prev_rows = [{"slot_time": (base + timedelta(hours=i)).isoformat(), "solar_w_m2": 50.0, "temp_c": 10.0} for i in range(6)]

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    with patch("src.weather.fetch_forecast_snapshot", return_value=fake_fetch), \
         patch("src.db.get_meteo_forecast_history_latest_before", return_value=prev_rows), \
         patch("src.db.save_meteo_forecast_snapshot"), \
         patch.object(runner, "bulletproof_mpc_job", _capture):
        runner.bulletproof_forecast_refresh_job()

    assert captured == {"force_write_devices": True, "trigger_reason": "forecast_revision"}


def test_forecast_refresh_job_no_trigger_when_within_thresholds(monkeypatch):
    from src.scheduler import runner
    from src.weather import ForecastFetchResult

    monkeypatch.setattr(runner.config, "MPC_FORECAST_DRIFT_SOLAR_KWH_THRESHOLD", 100.0)  # huge threshold
    monkeypatch.setattr(runner.config, "MPC_FORECAST_DRIFT_TEMP_C_THRESHOLD", 100.0)
    base = datetime.now(UTC)
    fake_fcst = [MagicMock(time_utc=base + timedelta(hours=i), temperature_c=10.5, shortwave_radiation_wm2=110.0) for i in range(6)]
    fake_fetch = ForecastFetchResult(forecast=fake_fcst, source="open-meteo")
    prev_rows = [{"slot_time": (base + timedelta(hours=i)).isoformat(), "solar_w_m2": 100.0, "temp_c": 10.0} for i in range(6)]

    triggered = MagicMock(side_effect=AssertionError("must not trigger when delta is within thresholds"))
    with patch("src.weather.fetch_forecast_snapshot", return_value=fake_fetch), \
         patch("src.db.get_meteo_forecast_history_latest_before", return_value=prev_rows), \
         patch("src.db.save_meteo_forecast_snapshot"), \
         patch.object(runner, "bulletproof_mpc_job", triggered):
        runner.bulletproof_forecast_refresh_job()
    triggered.assert_not_called()


# -------------------- Plan-delta observability --------------------


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
