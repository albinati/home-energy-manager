"""Tests for the PR J near-real-time PV diverter state machine.

The diverter sits in the heartbeat (every 2 min) and reacts to real-time
Fox ESS export flow. It mirrors the Eddi/Zappi PV-diverter pattern but
adapted for the heat-pump's ~5-10 min ramp-up: multi-tick confirmation
windows + hysteresis bands + lockout periods.

States: ``idle`` (tank at NORMAL) → ``diverting`` (tank at PV_ABUNDANCE_TARGET).
Transitions are gated by consecutive-tick confirmation counters and an
after-transition lockout that ignores opposite signals for a cooldown
period.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db as _db
from src import state_machine as sm
from src.config import config


@dataclass
class _FakeDev:
    id: str = "dev-1"
    name: str = "Altherma"
    tank_on: bool | None = True
    tank_target: float | None = 45.0
    tank_powerful: bool | None = False
    is_on: bool | None = None
    lwt_offset: float | None = None


@dataclass
class _FakeRealtime:
    soc: float = 95.0
    solar_power: float = 2.0
    grid_power: float = -1.5  # negative = exporting (Fox convention)
    battery_power: float = 0.0
    load_power: float = 0.5
    generation_power: float = 2.0
    feed_in_power: float = 1.5
    work_mode: str = "Self Use"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Per-test isolation: fresh DB, fresh diverter state, sane defaults."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    _db.init_db()
    # Config defaults — pin them so a global override doesn't leak in.
    monkeypatch.setattr(config, "PV_DIVERTER_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_EXPORT_THRESHOLD_KW", 1.0, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_DEACTIVATE_THRESHOLD_KW", 0.3, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_MIN_SOC_PCT", 95.0, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_SOC_DEACTIVATE_PCT", 90.0, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_ACTIVATE_CONFIRM_TICKS", 3, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_DEACTIVATE_CONFIRM_TICKS", 5, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_LOCKOUT_TICKS", 8, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_USE_FORECAST", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "USER_OVERRIDE_RESPECT_HOURS", 4.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_PV_ABUNDANCE_TARGET_C", 60.0, raising=False)
    # Reset diverter state machine
    sm._DIVERTER_STATE = "idle"
    sm._DIVERTER_ACTIVATE_COUNT = 0
    sm._DIVERTER_DEACTIVATE_COUNT = 0
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    sm._DIVERTER_LAST_NOTIFIED_STATE = "idle"
    yield


def _mock_fox(monkeypatch, **rt_kwargs):
    """Helper: patch get_cached_realtime to return a _FakeRealtime."""
    rt = _FakeRealtime(**rt_kwargs)
    fake_mod = MagicMock()
    fake_mod.get_cached_realtime.return_value = rt
    monkeypatch.setattr("src.foxess.service.get_cached_realtime",
                        fake_mod.get_cached_realtime)
    return fake_mod


def _mock_apply(monkeypatch):
    """Patch apply_scheduled_daikin_params + notify_risk + notify_critical
    so we can observe diverter writes/alerts without touching Daikin."""
    apply_mock = MagicMock()
    notify_risk = MagicMock()
    notify_critical = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", notify_critical)
    return apply_mock, notify_risk, notify_critical


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


def test_disabled_flag_skips_diverter(monkeypatch):
    monkeypatch.setattr(config, "PV_DIVERTER_ENABLED", False, raising=False)
    _mock_fox(monkeypatch)
    apply_mock, *_ = _mock_apply(monkeypatch)
    sm._check_pv_tank_diverter([], MagicMock(), _FakeDev(),
                                datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
                                trigger="hb")
    apply_mock.assert_not_called()


def test_vacation_mode_skips_diverter(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    _mock_fox(monkeypatch)
    apply_mock, *_ = _mock_apply(monkeypatch)
    sm._check_pv_tank_diverter([], MagicMock(), _FakeDev(),
                                datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
                                trigger="hb")
    apply_mock.assert_not_called()


def test_passive_mode_skips_diverter(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    _mock_fox(monkeypatch)
    apply_mock, *_ = _mock_apply(monkeypatch)
    sm._check_pv_tank_diverter([], MagicMock(), _FakeDev(),
                                datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
                                trigger="hb")
    apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Activate transitions (3-tick confirmation)
# ---------------------------------------------------------------------------


def test_one_tick_export_does_not_activate(monkeypatch):
    """A single tick of activate-conditions doesn't transition. Need
    PV_DIVERTER_ACTIVATE_CONFIRM_TICKS consecutive ticks."""
    _mock_fox(monkeypatch, soc=96, grid_power=-1.5)  # exporting 1.5 kW
    apply_mock, *_ = _mock_apply(monkeypatch)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    sm._check_pv_tank_diverter([], MagicMock(), _FakeDev(tank_target=45.0),
                                now, trigger="hb")
    assert sm._DIVERTER_STATE == "idle"
    assert sm._DIVERTER_ACTIVATE_COUNT == 1
    apply_mock.assert_not_called()


def test_three_consecutive_ticks_activates(monkeypatch):
    """3 consecutive ticks of activate-conditions transition to DIVERTING
    and write tank_temp = PV_ABUNDANCE_TARGET."""
    _mock_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, notify_risk, _ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()
    call_args = apply_mock.call_args
    assert call_args.kwargs["params"]["tank_temp"] == 60
    assert call_args.kwargs["params"]["tank_power"] is True
    assert call_args.kwargs["params"]["tank_powerful"] is True
    notify_risk.assert_called_once()


def test_interrupted_export_resets_activate_counter(monkeypatch):
    """If activate-conditions break mid-confirmation, the counter resets to 0.
    Need to rebuild the streak from scratch."""
    # Tick 1: exporting
    fox = _mock_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    sm._check_pv_tank_diverter([], MagicMock(), dev, now, trigger="hb1")
    assert sm._DIVERTER_ACTIVATE_COUNT == 1

    # Tick 2: NOT exporting (cloud passed); counter resets
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=0.2)
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=2),
                                trigger="hb2")
    assert sm._DIVERTER_ACTIVATE_COUNT == 0
    apply_mock.assert_not_called()

    # Tick 3-5: exporting again; needs 3 more consecutive
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=-1.5)
    for i in range(3, 6):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


def test_low_battery_blocks_activate(monkeypatch):
    """Battery priority: SoC < PV_DIVERTER_MIN_SOC_PCT (95%) blocks activation
    even with sustained high export. Battery fills first."""
    _mock_fox(monkeypatch, soc=85, grid_power=-1.5)  # exporting but battery only 85%
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(5):  # 5 ticks — well over the 3 confirm
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "idle"
    assert sm._DIVERTER_ACTIVATE_COUNT == 0
    apply_mock.assert_not_called()


def test_low_export_blocks_activate(monkeypatch):
    """Export < threshold (1.0 kW) doesn't qualify even if sustained."""
    _mock_fox(monkeypatch, soc=96, grid_power=-0.5)  # only 0.5 kW exporting
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(5):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Deactivate transitions (5-tick confirmation)
# ---------------------------------------------------------------------------


def test_diverting_to_idle_after_5_ticks_no_export(monkeypatch):
    """From DIVERTING state, 5 consecutive ticks of export < deactivate
    threshold trigger transition back to IDLE + restore to NORMAL."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"  # already notified on activate
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    fox = _mock_fox(monkeypatch, soc=92, grid_power=0.1)  # not exporting anymore
    apply_mock, notify_risk, _ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=60.0)
    now = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    for i in range(5):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_called_once()
    call_args = apply_mock.call_args
    assert call_args.kwargs["params"]["tank_temp"] == 45  # restored to NORMAL
    notify_risk.assert_called_once()


def test_brief_no_export_does_not_deactivate(monkeypatch):
    """Single tick of low export doesn't deactivate — need 5 consecutive."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    fox = _mock_fox(monkeypatch, soc=96, grid_power=0.1)  # low export
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=60.0)
    now = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)

    # 2 ticks of low export — counter at 2
    sm._check_pv_tank_diverter([], MagicMock(), dev, now, trigger="hb1")
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=2),
                                trigger="hb2")
    assert sm._DIVERTER_DEACTIVATE_COUNT == 2

    # Sun came back — counter resets
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=-1.2)
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=4),
                                trigger="hb3")
    assert sm._DIVERTER_DEACTIVATE_COUNT == 0
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_not_called()


def test_external_restore_silently_resets_state(monkeypatch):
    """If something else (LP write, user) restores the tank to NORMAL while
    we're in DIVERTING, we silently transition to IDLE without writing
    again — no duplicate write, but enter lockout."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    _mock_fox(monkeypatch, soc=96, grid_power=-1.5)  # still exporting
    apply_mock, *_ = _mock_apply(monkeypatch)
    # Tank already at NORMAL (external restore)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    sm._check_pv_tank_diverter([], MagicMock(), dev, now, trigger="hb")
    assert sm._DIVERTER_STATE == "idle"
    assert sm._DIVERTER_LOCKOUT_TICKS_LEFT > 0
    apply_mock.assert_not_called()  # no write — external restore did the work


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


def test_lockout_blocks_immediate_reactivate(monkeypatch):
    """After DEACTIVATE, lockout prevents immediate ACTIVATE even if export
    spikes back up. Counter resets each lockout tick."""
    sm._DIVERTER_STATE = "idle"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 5  # mid-lockout
    _mock_fox(monkeypatch, soc=96, grid_power=-2.0)  # huge export
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    for i in range(3):  # try to activate
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()
    # Lockout counted down by 3
    assert sm._DIVERTER_LOCKOUT_TICKS_LEFT == 2


def test_after_lockout_reactivate_possible(monkeypatch):
    """When lockout expires, normal activation flow resumes."""
    sm._DIVERTER_STATE = "idle"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 2  # almost done
    _mock_fox(monkeypatch, soc=96, grid_power=-2.0)
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    # 2 ticks consume the lockout
    sm._check_pv_tank_diverter([], MagicMock(), dev, now, trigger="hb1")
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=2),
                                trigger="hb2")
    assert sm._DIVERTER_LOCKOUT_TICKS_LEFT == 0
    # Now 3 ticks should activate (lockout cleared)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * (i + 2)),
                                    trigger=f"hb_a{i}")
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


# ---------------------------------------------------------------------------
# User override respect
# ---------------------------------------------------------------------------


def test_recent_user_override_skips_diverter(monkeypatch):
    """If the user just set the tank manually (override row present + live
    state still differs from the override row's params), the diverter
    defers — same pattern as the tank-power drift check."""
    _mock_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=55.0, tank_on=True)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    # Seed an override row: scheduled was tank_temp=45, user set higher
    aid = _db.upsert_action(
        plan_date="2026-06-01",
        start_time=(now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        end_time=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        device="daikin", action_type="solar_preheat",
        params={"tank_power": True, "tank_temp": 45}, status="active",
    )
    _db.mark_action_user_overridden(
        aid, overridden_at=(now - timedelta(minutes=10)).isoformat(),
    )
    for i in range(5):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Forecast confirmation
# ---------------------------------------------------------------------------


def test_forecast_disagreement_blocks_activate(monkeypatch):
    """When PV_DIVERTER_USE_FORECAST is on and the next 60 min forecast
    PV is below the viable threshold, activate is blocked — protects
    against brief sun gaps."""
    monkeypatch.setattr(config, "PV_DIVERTER_USE_FORECAST", True, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_FORECAST_MIN_PV_KW", 1.5, raising=False)
    _mock_fox(monkeypatch, soc=96, grid_power=-1.5)
    # Stub the forecast helper to return low value
    monkeypatch.setattr("src.scheduler.runner._get_forecast_pv_avg_kw",
                        lambda *a, **kw: 0.5)
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(5):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


def test_forecast_agrees_activate_proceeds(monkeypatch):
    """Forecast says PV will continue; activate proceeds normally."""
    monkeypatch.setattr(config, "PV_DIVERTER_USE_FORECAST", True, raising=False)
    monkeypatch.setattr(config, "PV_DIVERTER_FORECAST_MIN_PV_KW", 1.5, raising=False)
    _mock_fox(monkeypatch, soc=96, grid_power=-1.5)
    monkeypatch.setattr("src.scheduler.runner._get_forecast_pv_avg_kw",
                        lambda *a, **kw: 2.2)  # plenty of forecasted PV
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


def test_forecast_unavailable_falls_back_to_instantaneous(monkeypatch):
    """When forecast helper returns None (no data / error), diverter falls
    back to instantaneous-only mode — still activates if live conditions
    are met."""
    monkeypatch.setattr(config, "PV_DIVERTER_USE_FORECAST", True, raising=False)
    _mock_fox(monkeypatch, soc=96, grid_power=-1.5)
    monkeypatch.setattr("src.scheduler.runner._get_forecast_pv_avg_kw",
                        lambda *a, **kw: None)  # no forecast
    apply_mock, *_ = _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "diverting"  # activated despite no forecast
    apply_mock.assert_called_once()


# ---------------------------------------------------------------------------
# action_log audit trail
# ---------------------------------------------------------------------------


def test_activate_writes_action_log(monkeypatch):
    """Activate transition writes a `pv_diverter_activated` action_log row
    with diagnostic params so the audit timer can see what triggered the lift."""
    _mock_fox(monkeypatch, soc=96, grid_power=-1.5)
    _mock_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger="hb")
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT * FROM action_log WHERE action = 'pv_diverter_activated'"
    ))
    assert len(rows) == 1
    import json
    params = json.loads(rows[0]["params"])
    assert params["export_kw"] == pytest.approx(1.5, abs=0.1)
    assert params["soc_pct"] == pytest.approx(96, abs=0.1)
    assert params["tank_target_after_c"] == pytest.approx(60.0)
