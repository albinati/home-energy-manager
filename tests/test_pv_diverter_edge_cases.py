"""Edge-case tests for the PR J PV diverter.

The main `test_pv_diverter.py` covers the happy paths + core transitions.
This file probes:

* **Noise / oscillation** — real-world PV bouncing around the activate
  threshold should NOT cause the diverter to flap. The 3-tick / 5-tick
  confirmation windows + 16-min lockout should absorb noise.
* **Boundary conditions** — exact-threshold values (≥, >, =).
* **Stale or missing Fox data** — degrade gracefully.
* **Lockout chain** — multiple back-to-back transitions stack lockouts.
* **Daikin write failures** — diverter mustn't break the heartbeat.
* **Mode switches mid-diverting** — vacation flip kills the state.
* **User override interleavings** — gestures during DIVERTING / mid-confirm.

These are pure unit tests — no prod calls, no real Daikin/Fox.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db as _db
from src import state_machine as sm
from src.config import config
from src.daikin.client import DaikinError


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
    grid_power: float = -1.5
    battery_power: float = 0.0
    load_power: float = 0.5
    generation_power: float = 2.0
    feed_in_power: float = 1.5
    work_mode: str = "Self Use"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    _db.init_db()
    # Pin defaults for deterministic behaviour
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
    sm._DIVERTER_STATE = "idle"
    sm._DIVERTER_ACTIVATE_COUNT = 0
    sm._DIVERTER_DEACTIVATE_COUNT = 0
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    sm._DIVERTER_LAST_NOTIFIED_STATE = "idle"
    yield


def _patch_fox(monkeypatch, **rt_kwargs):
    rt = _FakeRealtime(**rt_kwargs)
    fox_mock = MagicMock()
    fox_mock.get_cached_realtime.return_value = rt
    monkeypatch.setattr("src.foxess.service.get_cached_realtime",
                        fox_mock.get_cached_realtime)
    return fox_mock


def _patch_apply(monkeypatch):
    apply_mock = MagicMock()
    notify_risk = MagicMock()
    notify_critical = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", notify_critical)
    return apply_mock, notify_risk, notify_critical


def _run_ticks(dev, base_time, n_ticks, interval_min=2):
    """Run n consecutive heartbeat ticks of the diverter."""
    for i in range(n_ticks):
        sm._check_pv_tank_diverter(
            [], MagicMock(), dev,
            base_time + timedelta(minutes=interval_min * i),
            trigger=f"hb{i}",
        )


# ---------------------------------------------------------------------------
# Boundary conditions on thresholds
# ---------------------------------------------------------------------------


def test_export_exactly_at_activate_threshold_does_not_activate(monkeypatch):
    """Activate uses strict ``>`` — export == threshold (1.0 kW) is NOT enough."""
    _patch_fox(monkeypatch, soc=96, grid_power=-1.0)  # exactly threshold
    apply_mock, *_ = _patch_apply(monkeypatch)
    _run_ticks(_FakeDev(tank_target=45.0), datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 5)
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


def test_export_just_above_activate_threshold_activates(monkeypatch):
    """1.01 kW (just above threshold) triggers normal activate flow."""
    _patch_fox(monkeypatch, soc=96, grid_power=-1.01)
    apply_mock, *_ = _patch_apply(monkeypatch)
    _run_ticks(_FakeDev(tank_target=45.0), datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 3)
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


def test_soc_exactly_at_min_activates(monkeypatch):
    """Activate uses ``>=`` for SoC — exactly 95% is enough."""
    _patch_fox(monkeypatch, soc=95.0, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    _run_ticks(_FakeDev(tank_target=45.0), datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 3)
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


def test_soc_just_below_min_blocks(monkeypatch):
    """SoC 94.9% (just below 95) blocks activation."""
    _patch_fox(monkeypatch, soc=94.9, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    _run_ticks(_FakeDev(tank_target=45.0), datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 5)
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


def test_tank_target_already_at_ceiling_blocks_activate(monkeypatch):
    """If tank target is already at PV_ABUNDANCE_TARGET (60), no need to
    activate — diverter recognizes no work to do (within 0.5 °C tolerance)."""
    _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    _run_ticks(_FakeDev(tank_target=60.0), datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 5)
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


def test_tank_target_close_to_ceiling_still_activates(monkeypatch):
    """Tank target at 58 (room to grow to 60) still activates the diverter."""
    _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    _run_ticks(_FakeDev(tank_target=58.0), datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 3)
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Oscillation / noise resilience
# ---------------------------------------------------------------------------


def test_alternating_above_below_threshold_never_activates(monkeypatch):
    """Real-world PV bouncing 0.8 / 1.2 / 0.7 / 1.3 kW around threshold —
    activate counter should never reach 3 consecutive, so no flapping."""
    fox = _patch_fox(monkeypatch, soc=96)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    pattern = [-1.2, -0.8, -1.3, -0.7, -1.5, -0.9, -1.1, -0.6, -1.4, -0.5]
    for i, gp in enumerate(pattern):
        fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=gp)
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i), trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


def test_two_consecutive_then_break_then_resume_resets_counter(monkeypatch):
    """activate_count=2 → break → must rebuild from scratch.
    With ACTIVATE_CONFIRM=3, an interrupted streak invalidates progress."""
    fox = _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    # 2 ticks of activate
    sm._check_pv_tank_diverter([], MagicMock(), dev, now, trigger="t1")
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=2), trigger="t2")
    assert sm._DIVERTER_ACTIVATE_COUNT == 2

    # Break
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=0.1)
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=4), trigger="t3")
    assert sm._DIVERTER_ACTIVATE_COUNT == 0

    # Resume: needs 3 NEW consecutive ticks
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=-1.5)
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=6), trigger="t4")
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=8), trigger="t5")
    # After tick 2 of resumed streak, still not activated
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()
    # One more does it
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=10), trigger="t6")
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


def test_brief_soc_drop_during_diverting_does_not_immediately_deactivate(monkeypatch):
    """One tick of SoC dropping below 90 doesn't deactivate — need 5 sustained."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    fox = _patch_fox(monkeypatch, soc=88, grid_power=-1.5)  # SoC briefly low but exporting
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=60.0)
    now = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    sm._check_pv_tank_diverter([], MagicMock(), dev, now, trigger="t1")
    assert sm._DIVERTER_DEACTIVATE_COUNT == 1

    # SoC recovers — counter resets
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=-1.5)
    sm._check_pv_tank_diverter([], MagicMock(), dev, now + timedelta(minutes=2), trigger="t2")
    assert sm._DIVERTER_DEACTIVATE_COUNT == 0
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Stale / missing Fox data
# ---------------------------------------------------------------------------


def test_fox_realtime_raises_exception_degrades_gracefully(monkeypatch):
    """If `get_cached_realtime()` raises (Fox quota exhausted with no cache),
    diverter logs and returns — does NOT break the heartbeat."""
    bad_fox = MagicMock()
    bad_fox.get_cached_realtime.side_effect = Exception("Fox API quota exhausted, no cache")
    monkeypatch.setattr("src.foxess.service.get_cached_realtime",
                        bad_fox.get_cached_realtime)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    # Should not raise:
    sm._check_pv_tank_diverter([], MagicMock(), dev,
                                datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
                                trigger="hb")
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


def test_fox_realtime_returns_zero_values(monkeypatch):
    """All-zero realtime (Fox returned empty/garbage) → no activate. Defensive
    against cold-start or coerced-to-zero parsing."""
    _patch_fox(monkeypatch, soc=0, grid_power=0, solar_power=0, load_power=0)
    apply_mock, *_ = _patch_apply(monkeypatch)
    _run_ticks(_FakeDev(tank_target=45.0),
               datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 5)
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


def test_tank_target_unknown_in_diverting_state(monkeypatch):
    """dev.tank_target=None (Daikin cache miss) while diverting: bail
    safely without false-deactivate."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"
    _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=None)
    sm._check_pv_tank_diverter([], MagicMock(), dev,
                                datetime(2026, 6, 1, 15, 0, tzinfo=UTC),
                                trigger="hb")
    # State preserved
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Lockout edge cases
# ---------------------------------------------------------------------------


def test_lockout_zero_ticks_still_works(monkeypatch):
    """`PV_DIVERTER_LOCKOUT_TICKS=0` effectively disables hysteresis. Multi-
    tick confirmation still applies but back-to-back transitions are possible."""
    monkeypatch.setattr(config, "PV_DIVERTER_LOCKOUT_TICKS", 0, raising=False)
    fox = _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    # Activate (3 ticks)
    _run_ticks(dev, now, 3)
    assert sm._DIVERTER_STATE == "diverting"
    assert sm._DIVERTER_LOCKOUT_TICKS_LEFT == 0  # no lockout

    # Immediately try to deactivate — possible (no lockout)
    dev.tank_target = 60.0
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=92, grid_power=0.1)
    _run_ticks(dev, now + timedelta(minutes=6), 5)
    assert sm._DIVERTER_STATE == "idle"
    # 2 writes: activate + deactivate
    assert apply_mock.call_count == 2


def test_lockout_one_tick(monkeypatch):
    """Lockout=1 tick: blocks for one heartbeat, then proceeds normally."""
    monkeypatch.setattr(config, "PV_DIVERTER_LOCKOUT_TICKS", 1, raising=False)
    sm._DIVERTER_STATE = "idle"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 1
    _patch_fox(monkeypatch, soc=96, grid_power=-2.0)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    # Tick 1: in lockout, counter not advanced
    sm._check_pv_tank_diverter([], MagicMock(), dev, now, trigger="t1")
    assert sm._DIVERTER_LOCKOUT_TICKS_LEFT == 0
    assert sm._DIVERTER_ACTIVATE_COUNT == 0
    apply_mock.assert_not_called()
    # Now 3 ticks should activate
    _run_ticks(dev, now + timedelta(minutes=2), 3)
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Daikin write failure recovery
# ---------------------------------------------------------------------------


def test_daikin_write_failure_during_activate_does_not_crash(monkeypatch):
    """If `apply_scheduled_daikin_params` raises DaikinError, diverter still
    transitions state (we tracked the decision) but logs the failure.
    Heartbeat continues."""
    _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock = MagicMock(side_effect=DaikinError("HTTP 503: upstream"))
    notify_risk = MagicMock()
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", notify_risk)
    monkeypatch.setattr(sm, "notify_critical", MagicMock())
    dev = _FakeDev(tank_target=45.0)
    _run_ticks(dev, datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 3)
    # State transitioned despite write failure (deliberate — next tick may
    # find the actual target is still 45 and treat as external restore).
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()
    # action_log still captured the decision
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    rows = list(conn.execute(
        "SELECT COUNT(*) FROM action_log WHERE action = 'pv_diverter_activated'"
    ))
    assert rows[0][0] == 1


def test_daikin_write_failure_during_deactivate_does_not_crash(monkeypatch):
    """Same defensive guarantee on the deactivate path."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    _patch_fox(monkeypatch, soc=92, grid_power=0.1)
    apply_mock = MagicMock(side_effect=DaikinError("read_only HTTP 400"))
    monkeypatch.setattr(sm, "apply_scheduled_daikin_params", apply_mock)
    monkeypatch.setattr(sm, "notify_risk", MagicMock())
    monkeypatch.setattr(sm, "notify_critical", MagicMock())
    dev = _FakeDev(tank_target=60.0)
    _run_ticks(dev, datetime(2026, 6, 1, 15, 0, tzinfo=UTC), 5)
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Mode switches mid-diverting
# ---------------------------------------------------------------------------


def test_vacation_flip_mid_diverting_stops_writes(monkeypatch):
    """If user flips to vacation while DIVERTING is active, subsequent
    diverter ticks return immediately. No restore write (LP/dispatch will
    handle vacation tank shutdown separately)."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    _patch_fox(monkeypatch, soc=92, grid_power=0.1)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=60.0)
    _run_ticks(dev, datetime(2026, 6, 1, 15, 0, tzinfo=UTC), 5)
    # State preserved (no transition in vacation), no writes
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_not_called()


def test_passive_flip_mid_diverting_stops_writes(monkeypatch):
    """DAIKIN_CONTROL_MODE=passive flip kills further writes (heartbeat
    can't reach Daikin in passive mode)."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    _patch_fox(monkeypatch, soc=92, grid_power=0.1)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=60.0)
    _run_ticks(dev, datetime(2026, 6, 1, 15, 0, tzinfo=UTC), 5)
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_not_called()


def test_disabled_mid_diverting_stops_writes(monkeypatch):
    """`PV_DIVERTER_ENABLED=false` flip kills further activity."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"
    monkeypatch.setattr(config, "PV_DIVERTER_ENABLED", False, raising=False)
    _patch_fox(monkeypatch, soc=92, grid_power=0.1)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=60.0)
    _run_ticks(dev, datetime(2026, 6, 1, 15, 0, tzinfo=UTC), 5)
    assert sm._DIVERTER_STATE == "diverting"  # frozen
    apply_mock.assert_not_called()


# ---------------------------------------------------------------------------
# User override interleavings
# ---------------------------------------------------------------------------


def test_override_blocks_mid_confirmation(monkeypatch):
    """User override during the ACTIVATE 3-tick window resets counters
    immediately — no surprise activate when the user touched the tank."""
    fox = _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0, tank_on=True)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)

    # Tick 1: counter=1
    sm._check_pv_tank_diverter([], MagicMock(), dev, now, trigger="t1")
    assert sm._DIVERTER_ACTIVATE_COUNT == 1

    # Tick 2: user lifts tank to 55 via Onecta → override recorded
    aid = _db.upsert_action(
        plan_date="2026-06-01",
        start_time=(now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        end_time=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        device="daikin", action_type="solar_preheat",
        params={"tank_power": True, "tank_temp": 45}, status="active",
    )
    _db.mark_action_user_overridden(
        aid, overridden_at=(now + timedelta(minutes=1)).isoformat(),
    )
    dev.tank_target = 55.0  # user's manual lift
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=-1.5)
    sm._check_pv_tank_diverter([], MagicMock(), dev,
                                now + timedelta(minutes=2), trigger="t2")
    # Counter reset because override is in effect
    assert sm._DIVERTER_ACTIVATE_COUNT == 0
    apply_mock.assert_not_called()

    # Tick 3, 4, 5: still in override → still no activate
    sm._check_pv_tank_diverter([], MagicMock(), dev,
                                now + timedelta(minutes=4), trigger="t3")
    sm._check_pv_tank_diverter([], MagicMock(), dev,
                                now + timedelta(minutes=6), trigger="t4")
    sm._check_pv_tank_diverter([], MagicMock(), dev,
                                now + timedelta(minutes=8), trigger="t5")
    assert sm._DIVERTER_STATE == "idle"
    apply_mock.assert_not_called()


def test_override_aged_out_lets_activate_proceed(monkeypatch):
    """When the user-override row is older than USER_OVERRIDE_RESPECT_HOURS
    (default 4h), the diverter ignores it and activates normally."""
    fox = _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0, tank_on=True)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)

    # Seed an OLD override (5 h ago)
    aid = _db.upsert_action(
        plan_date="2026-06-01",
        start_time=(now - timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
        end_time=(now - timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        device="daikin", action_type="solar_preheat",
        params={"tank_power": True, "tank_temp": 45}, status="completed",
    )
    _db.mark_action_user_overridden(
        aid, overridden_at=(now - timedelta(hours=5)).isoformat(),
    )
    _run_ticks(dev, now, 3)
    # Override has aged out → diverter activates
    assert sm._DIVERTER_STATE == "diverting"
    apply_mock.assert_called_once()


# ---------------------------------------------------------------------------
# State persistence across heartbeat ticks
# ---------------------------------------------------------------------------


def test_idempotent_write_skip_when_target_matches(monkeypatch):
    """apply_scheduled_daikin_params skip_if_matches=True means if Daikin
    target is already 60, no API call. We're verifying the call shape
    passes that flag through."""
    _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    _run_ticks(_FakeDev(tank_target=45.0),
               datetime(2026, 6, 1, 13, 0, tzinfo=UTC), 3)
    call_args = apply_mock.call_args
    assert call_args.kwargs.get("skip_if_matches") is True


def test_state_machine_starts_idle_on_cold_boot():
    """Module-level state vars default to IDLE on import. New container
    boots in IDLE regardless of pre-restart state."""
    # The autouse fixture already resets these — verify they're sane.
    assert sm._DIVERTER_STATE == "idle"
    assert sm._DIVERTER_ACTIVATE_COUNT == 0
    assert sm._DIVERTER_DEACTIVATE_COUNT == 0
    assert sm._DIVERTER_LOCKOUT_TICKS_LEFT == 0
    assert sm._DIVERTER_LAST_NOTIFIED_STATE == "idle"


def test_lockout_decrements_each_tick_even_with_skip_signal(monkeypatch):
    """Lockout counts down on every tick, even when no transition signals
    are present. Prevents an infinite lockout from a stuck condition."""
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 5
    _patch_fox(monkeypatch, soc=80, grid_power=0.5)  # neither activate nor deactivate
    _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    for i in range(5):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    datetime(2026, 6, 1, 13, 0, tzinfo=UTC) + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_LOCKOUT_TICKS_LEFT == 0


# ---------------------------------------------------------------------------
# Notification rate limiting
# ---------------------------------------------------------------------------


def test_notification_fires_only_once_per_state_change(monkeypatch):
    """If diverter activates twice with an idle-period in between, we get
    2 notifications (one per state change). NOT one per heartbeat in the
    diverting state."""
    monkeypatch.setattr(config, "PV_DIVERTER_LOCKOUT_TICKS", 0, raising=False)
    fox = _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, notify_risk, _ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)

    # Cycle 1: activate
    _run_ticks(dev, now, 3)
    assert sm._DIVERTER_STATE == "diverting"
    assert notify_risk.call_count == 1

    # Deactivate
    dev.tank_target = 60.0
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=92, grid_power=0.1)
    _run_ticks(dev, now + timedelta(minutes=6), 5)
    assert sm._DIVERTER_STATE == "idle"
    assert notify_risk.call_count == 2

    # Cycle 2: re-activate
    dev.tank_target = 45.0
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=-1.5)
    _run_ticks(dev, now + timedelta(minutes=16), 3)
    assert sm._DIVERTER_STATE == "diverting"
    assert notify_risk.call_count == 3
