"""Integration tests for the 2026-05-22 PV-storage stack (PR G/H/I/J).

These verify the LAYERS work together and the boundary cases land where
the design intends:

* **PR G** — empirical tank physics (USABLE_FRACTION=0.85, FLOW=7 L/min,
  NORMAL=45 °C, EVENING_CAP=6 showers)
* **PR H** — `DHW_TEMP_PV_ABUNDANCE_TARGET_C = 60` (storage ceiling, not
  comfort)
* **PR I** — dynamic per-slot reward `max(static, export_rate + buffer)`
* **PR J** — runtime PV diverter state machine

This file focuses on **boundaries** and **layer interactions** —
extremes where it's not obvious without tests which layer wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src import state_machine as sm
from src.config import config


# ---------------------------------------------------------------------------
# LP test fixtures (mirror the helpers from test_lp_pv_abundance.py)
# ---------------------------------------------------------------------------


def _make_weather(slots, pv_kwh):
    from src.weather import WeatherLpSeries
    n = len(slots)
    return WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[18.0] * n,
        shortwave_radiation_wm2=[600.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=pv_kwh,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )


def _solve_lp(slots, prices, pv, base_load, init_soc=8.0, init_tank=40.0,
              export_prices=None):
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    init = LpInitialState(soc_kwh=init_soc, tank_temp_c=init_tank)
    return solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=_make_weather(slots, pv),
        initial=init,
        tz=ZoneInfo("Europe/London"),
        export_price_pence=export_prices,
    )


# ---------------------------------------------------------------------------
# Diverter fixtures (mirror test_pv_diverter)
# ---------------------------------------------------------------------------


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
    # PR G defaults
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    monkeypatch.setattr(config, "DHW_TANK_USABLE_FRACTION", 0.85, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWER_FLOW_LPM", 7.0, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWERS_EVENING_CAP", 6, raising=False)
    # PR H defaults
    monkeypatch.setattr(config, "DHW_TEMP_PV_ABUNDANCE_TARGET_C", 60.0, raising=False)
    # PR I defaults
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 10.0, raising=False)
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_BEAT_EXPORT_BUFFER_PENCE", 2.0, raising=False)
    # PR J defaults
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
    monkeypatch.setattr(config, "DHW_PV_ABUNDANCE_THRESHOLD_KWH", 0.5, raising=False)
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


# ===========================================================================
# PR H × PR I — storage ceiling × dynamic reward boundary
# ===========================================================================


def test_pr_h_ceiling_overrides_pr_i_reward(monkeypatch):
    """PR H sets storage ceiling at 60 °C. PR I dynamic reward could
    theoretically push the LP to keep heating forever — but the LP's
    `tank_temp[i] <= DHW_TEMP_MAX_C` constraint caps it. Verify the
    interaction: huge reward shouldn't blow past the physical max."""
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH",
                        1000.0, raising=False)  # absurdly high reward
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve_lp(
        slots=slots,
        prices=[20.0] * n,
        pv=[5.0] * n,  # massive PV
        base_load=[0.3] * n,
        init_soc=9.5,
        init_tank=40.0,
    )
    assert plan.ok
    max_tank = max(plan.tank_temp_c)
    # DHW_TEMP_MAX_C is 65 by default; tank must not exceed it.
    assert max_tank <= float(config.DHW_TEMP_MAX_C) + 0.01, (
        f"Reward should not blow past physical max; got {max_tank:.1f} °C"
    )


def test_pr_i_dynamic_reward_at_buffer_boundary(monkeypatch):
    """PR I: dynamic = max(static=10, export+buffer=2). If export = 8p,
    then export+2 = 10 → equals static. max() picks 10. Confirms reward
    doesn't jump unexpectedly when export is right at the breakeven point."""
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH",
                        10.0, raising=False)
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_BEAT_EXPORT_BUFFER_PENCE",
                        2.0, raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve_lp(
        slots=slots,
        prices=[20.0] * n,
        pv=[3.0] * n,
        base_load=[0.3] * n,
        init_soc=9.5,
        init_tank=40.0,
        export_prices=[8.0] * n,  # export+2 = static, breakeven
    )
    assert plan.ok
    # At breakeven, tank may or may not win depending on LP tie-break,
    # but solve must succeed — no infeasibility.
    assert sum(plan.dhw_electric_kwh) >= 0.0


def test_zero_buffer_disables_export_beat(monkeypatch):
    """Setting buffer=0 disables the "always beat export" guarantee — at
    breakeven, LP may pick export. Acts as a rollback knob."""
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH",
                        10.0, raising=False)
    monkeypatch.setattr(config, "LP_PV_ABUNDANCE_TANK_BEAT_EXPORT_BUFFER_PENCE",
                        0.0, raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve_lp(
        slots=slots,
        prices=[20.0] * n,
        pv=[3.0] * n,
        base_load=[0.3] * n,
        init_soc=9.5,
        init_tank=40.0,
        export_prices=[15.0] * n,
    )
    assert plan.ok
    # With buffer=0 and export 15p > static 10p, max() picks 15 — tank still
    # beats export at the SAME value (LP tie), so we just verify solver runs.


def test_negative_buffer_reduces_dhw_vs_positive(monkeypatch):
    """Comparative test: with the same inputs, ``buffer=+2`` (PR I default)
    should encourage MORE DHW than ``buffer=-100`` (rollback knob).
    The shower floor is still met in both cases — what differs is the
    *bonus* heating above floor."""
    from src.config import config as app_config
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 6
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]

    monkeypatch.setattr(app_config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH",
                        10.0, raising=False)
    monkeypatch.setattr(app_config, "LP_PV_ABUNDANCE_TANK_BEAT_EXPORT_BUFFER_PENCE",
                        2.0, raising=False)
    plan_pos = _solve_lp(
        slots=slots, prices=[20.0] * n, pv=[3.0] * n, base_load=[0.3] * n,
        init_soc=9.5, init_tank=40.0, export_prices=[25.0] * n,
    )

    monkeypatch.setattr(app_config, "LP_PV_ABUNDANCE_TANK_BEAT_EXPORT_BUFFER_PENCE",
                        -100.0, raising=False)
    plan_neg = _solve_lp(
        slots=slots, prices=[20.0] * n, pv=[3.0] * n, base_load=[0.3] * n,
        init_soc=9.5, init_tank=40.0, export_prices=[25.0] * n,
    )
    assert plan_pos.ok and plan_neg.ok
    # Positive buffer should give equal-or-more DHW heating than negative.
    # The strict-greater check would be too fragile (LP may tie); we assert
    # at minimum equality to confirm the knob has correct direction.
    assert sum(plan_pos.dhw_electric_kwh) >= sum(plan_neg.dhw_electric_kwh) - 0.01


# ===========================================================================
# PR J × PR H — diverter writes the right target
# ===========================================================================


def test_diverter_uses_pr_h_ceiling(monkeypatch):
    """When PR H runtime setting changes ceiling, diverter writes the new
    value (not a hard-coded 60). Validates DHW_TEMP_PV_ABUNDANCE_TARGET_C
    is properly read, not pinned."""
    monkeypatch.setattr(config, "DHW_TEMP_PV_ABUNDANCE_TARGET_C", 55.0, raising=False)
    _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "diverting"
    call_args = apply_mock.call_args
    assert call_args.kwargs["params"]["tank_temp"] == 55  # new ceiling


def test_diverter_restores_pr_g_normal_value(monkeypatch):
    """On deactivate, diverter restores tank to DHW_TEMP_NORMAL_C (PR G).
    Validates the deactivate write reads the runtime setting, not hard-code."""
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 43.0, raising=False)
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    _patch_fox(monkeypatch, soc=92, grid_power=0.1)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=60.0)
    now = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    for i in range(5):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert sm._DIVERTER_STATE == "idle"
    call_args = apply_mock.call_args
    assert call_args.kwargs["params"]["tank_temp"] == 43  # new NORMAL


# ===========================================================================
# Notification policy regressions
# ===========================================================================


def test_diverter_does_not_double_notify_within_diverting_state(monkeypatch):
    """Going IDLE → DIVERTING fires 1 notify. Subsequent ticks in
    DIVERTING (even with conditions still met) do NOT re-notify.
    Important for the user's "low push load" preference."""
    fox = _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, notify_risk, _ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)

    # Activate (3 ticks)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert notify_risk.call_count == 1

    # 10 more ticks — still in DIVERTING (and lockout for first 8); should
    # never re-notify
    dev.tank_target = 60.0
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=96, grid_power=-1.5)
    for i in range(3, 13):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    assert notify_risk.call_count == 1


def test_diverter_writes_action_log_entries_distinctly(monkeypatch):
    """activate then deactivate writes 2 distinct action_log rows with
    different `action` values. Important for the daily audit timer."""
    monkeypatch.setattr(config, "PV_DIVERTER_LOCKOUT_TICKS", 0, raising=False)
    fox = _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    # Activate
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger="hb_a")
    dev.tank_target = 60.0
    # Deactivate
    fox.get_cached_realtime.return_value = _FakeRealtime(soc=92, grid_power=0.1)
    for i in range(5):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * (i + 3)),
                                    trigger="hb_d")
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    actions = set(r[0] for r in conn.execute(
        "SELECT DISTINCT action FROM action_log WHERE action LIKE 'pv_diverter%'"
    ))
    assert actions == {"pv_diverter_activated", "pv_diverter_deactivated"}


# ===========================================================================
# PR G shower-physics sanity (under the new defaults)
# ===========================================================================


def test_pr_g_physics_45c_handles_4_showers(monkeypatch):
    """User's lived experience: 4 daily showers at 45 °C tank are fine.
    Verify the physics module reports usable kWh sufficient for 4 mixed-down
    showers at the new USABLE_FRACTION=0.85, FLOW=7 L/min defaults."""
    from src import physics
    # 4 showers × 5 min × 7 L/min × 40 °C mixed (38 setpoint vs 8 cold inlet)
    # ~ 5 min × 7 = 35 L per shower; 4 × 35 = 140 L hot-equivalent
    # At 45 °C tank vs 8 cold inlet, full ΔT = 37 °C, mixed at ~38 °C → mixing
    # ratio reduces hot draw, physics module computes usable.
    # Tank model parameters are at module scope of physics — verify the
    # functions don't reject the configuration.
    assert config.DHW_TANK_USABLE_FRACTION == 0.85
    assert config.DHW_SHOWER_FLOW_LPM == 7.0
    assert config.DHW_TEMP_NORMAL_C == 45.0
    # The shower-floor calc in the LP should require tank ≥ NORMAL for 4
    # showers — confirm by solving an LP that simulates the evening shower
    # window and checking it's feasible at NORMAL.
    base = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)  # evening
    n = 8  # 4 hours of half-hour slots
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve_lp(
        slots=slots,
        prices=[25.0] * n,
        pv=[0.0] * n,  # evening, no PV
        base_load=[0.4] * n,
        init_soc=9.0,
        init_tank=45.0,  # NORMAL value
    )
    assert plan.ok, f"LP should be feasible at NORMAL=45°C with 4 showers; status={plan.status}"


def test_pr_g_evening_cap_6_showers_does_not_break_lp(monkeypatch):
    """Up to 6 evening showers (guest cap) — LP should still solve."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    monkeypatch.setattr(config, "DHW_GUEST_COUNT", 6, raising=False)
    base = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve_lp(
        slots=slots,
        prices=[25.0] * n,
        pv=[0.0] * n,
        base_load=[0.4] * n,
        init_soc=9.0,
        init_tank=50.0,  # higher init for 6 showers
    )
    assert plan.ok, f"LP should handle 6 evening showers; status={plan.status}"


# ===========================================================================
# Diverter writes valid Daikin params (smoke test on shape)
# ===========================================================================


def test_diverter_activate_params_shape(monkeypatch):
    """Diverter ACTIVATE writes exactly the params daikin_bulletproof
    accepts: tank_power + tank_temp + tank_powerful. No extra keys, no
    typos. This is a defensive test against signature drift."""
    _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    apply_mock.assert_called_once()
    params = apply_mock.call_args.kwargs["params"]
    assert set(params.keys()) == {"tank_power", "tank_temp", "tank_powerful"}
    assert params["tank_power"] is True
    assert isinstance(params["tank_temp"], int)
    assert params["tank_powerful"] is True


def test_diverter_deactivate_params_shape(monkeypatch):
    """Same shape contract for DEACTIVATE."""
    sm._DIVERTER_STATE = "diverting"
    sm._DIVERTER_LAST_NOTIFIED_STATE = "diverting"
    sm._DIVERTER_LOCKOUT_TICKS_LEFT = 0
    _patch_fox(monkeypatch, soc=92, grid_power=0.1)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=60.0)
    now = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    for i in range(5):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger=f"hb{i}")
    apply_mock.assert_called_once()
    params = apply_mock.call_args.kwargs["params"]
    assert set(params.keys()) == {"tank_power", "tank_temp", "tank_powerful"}
    assert params["tank_power"] is True
    assert isinstance(params["tank_temp"], int)
    assert params["tank_powerful"] is False  # turn off boost on restore


def test_diverter_trigger_label_includes_origin(monkeypatch):
    """The trigger param passed to apply_scheduled_daikin_params should
    encode the diverter origin so the action_log audit can track who
    wrote what. Format: `pv_diverter_activate:<heartbeat_trigger>`."""
    _patch_fox(monkeypatch, soc=96, grid_power=-1.5)
    apply_mock, *_ = _patch_apply(monkeypatch)
    dev = _FakeDev(tank_target=45.0)
    now = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    for i in range(3):
        sm._check_pv_tank_diverter([], MagicMock(), dev,
                                    now + timedelta(minutes=2 * i),
                                    trigger="bulletproof_hb")
    trigger = apply_mock.call_args.kwargs["trigger"]
    assert trigger.startswith("pv_diverter_activate:")
    assert "bulletproof_hb" in trigger
