"""#951 — the W3 gate must not veto ceiling slack in slots already at the minimum offset."""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import db
from src.config import config
from src.scheduler.lp_dispatch import (
    _lp_offsets,
    _tier_offsets,
    _write_lwt_preheat_actions,
    read_lwt_source_effective,
    w3_trajectory_plausible,
)
from src.scheduler.lp_optimizer import LpPlan, lp_offset_min_int

N = 12


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # pinned to prod: the default (-10) differs and hid this bug in replays
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -2.0)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", 5.0, raising=False)
    monkeypatch.setattr(config, "LP_W3_MAX_SLACK_SLOTS", 4)
    monkeypatch.setattr(config, "LP_W3_SLACK_TOL_C", 0.4)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", -2)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")


def _plan(offs, hi, min_off=-2.0, traj=None, start=None, tau=0.0):
    t0 = start or datetime(2026, 11, 14, tzinfo=UTC)
    p = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
               slot_starts_utc=[t0 + timedelta(minutes=30 * i) for i in range(N)],
               price_pence=[25.45] * N, temp_outdoor_c=[1.0] * N,
               cheap_threshold_pence=18.97, peak_threshold_pence=31.81,
               tariff_structure_kind="banded", price_band=["standard"] * N)
    p.lwt_offset_c = list(offs)
    p.space_electric_kwh = [0.3] * N  # the pump still draws power at the minimum offset
    # prod shape: house above the ceiling, left to cool
    p.indoor_temp_c = list(traj) if traj is not None else [21.6 - 0.02 * i for i in range(N + 1)]
    p.comfort_slack_c = [0.0] * N
    p.comfort_slack_hi_c = list(hi)
    p.comfort_slack_emergency_c = [0.0] * N
    p.w3_emergency_floor_c = 20.0
    p.w3_night_floor_c = 20.0
    p.w3_ceiling_c = 21.0
    p.w3_min_offset_c = min_off
    p.w3_emitter_tau_min = tau
    return p


def test_above_ceiling_all_at_min_offset_is_not_vetoed():
    plan = _plan([-2.0] * N, [0.6] * N)
    assert w3_trajectory_plausible(plan) == (True, "ok")


def test_raw_offsets_that_round_to_the_minimum_do_not_count():
    # floor(raw + 0.5) == -2 for raw < -1.5, so these are written AT the minimum
    plan = _plan([-1.85, -1.6, -2.4] * 4, [0.6] * N)
    assert w3_trajectory_plausible(plan) == (True, "ok")


def test_above_ceiling_with_offsets_above_min_still_vetoed():
    ok, reason = w3_trajectory_plausible(_plan([3.0] * N, [0.6] * N))
    assert not ok and reason == "comfort_slack:12_slots>4"


def test_offset_that_rounds_above_min_counts():
    # -1.26 is written as -1: one step above the minimum, the plan chose to heat
    ok, reason = w3_trajectory_plausible(_plan([-1.26] * N, [0.6] * N))
    assert not ok and reason.startswith("comfort_slack:12_slots")


def test_mixed_slots_only_those_above_min_are_counted():
    offs = [-2.0] * 6 + [3.0] * 5 + [-1.0]  # 6 at min, 6 above
    ok, reason = w3_trajectory_plausible(_plan(offs, [0.6] * N))
    assert not ok and reason == "comfort_slack:6_slots>4"
    ok, reason = w3_trajectory_plausible(_plan([-2.0] * 9 + [3.0] * 3, [0.6] * N))
    assert (ok, reason) == (True, "ok")  # 3 counted <= 4


def test_floor_and_emergency_slack_unchanged_at_min_offset():
    p = _plan([-2.0] * N, [0.0] * N)
    p.comfort_slack_c = [0.6] * 5 + [0.0] * 7
    assert w3_trajectory_plausible(p)[1].startswith("comfort_slack:5_slots")
    p = _plan([-2.0] * N, [0.0] * N)
    p.comfort_slack_emergency_c = [0.3] + [0.0] * 11
    assert w3_trajectory_plausible(p) == (False, "emergency_slack")


def test_plan_recorded_minimum_wins_over_live_config(monkeypatch):
    plan = _plan([-2.0] * N, [0.6] * N, min_off=-2.0)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -10.0)  # live config drifted
    assert w3_trajectory_plausible(plan) == (True, "ok")
    plan.w3_min_offset_c = None  # older plan: falls back to live config (-5 vs -10 -> -5)
    ok, _ = w3_trajectory_plausible(plan)
    assert not ok


def test_bank_then_coast_is_vetoed_with_emitter_lag():
    # reviewer probe, now with an emitter lag (tau=60 -> lag_skip 3; with tau=0 a rise at the
    # minimum offset is gains, not banked heat, and is exempt by design): +3 for 3 slots then
    # min offset, indoor keeps rising through slots 3-5 and then falls: 3 + 3 = 6 > 4.
    traj = [21.0 + 0.2 * i for i in range(7)] + [22.2 - 0.18 * k for k in range(1, 6)]
    plan = _plan([3.0] * 3 + [-2.0] * 9, [0.6] * N, traj=traj, tau=60.0)
    ok, reason = w3_trajectory_plausible(plan)
    assert not ok and reason == "comfort_slack:6_slots>4"


def test_lagged_heat_rising_min_offset_slots_are_vetoed():
    # heating slots below the ceiling (no slack), then rising min-offset slots above it,
    # within lag_skip (tau=120 -> 5) of the above-min heating: slots 3-7 count
    hi = [0.0] * 3 + [0.6] * 9
    traj = [20.0, 20.4, 20.8, 21.2] + [21.2 + 0.3 * k for k in range(1, 10)]
    plan = _plan([3.0] * 3 + [-2.0] * 9, hi, traj=traj, tau=120.0)
    ok, reason = w3_trajectory_plausible(plan)
    assert not ok and reason == "comfort_slack:5_slots>4"


def test_rising_at_min_offset_without_lookback_heating_passes_with_lag():
    traj = [21.0 + 0.1 * i for i in range(N + 1)]
    plan = _plan([-2.0] * N, [0.6] * N, traj=traj, tau=60.0)
    assert w3_trajectory_plausible(plan) == (True, "ok")


def test_gains_raise_indoor_at_min_offset_without_lag_passes():
    # prod default LP_W3_EMITTER_TAU_MIN=0: solar/internal gains lift the indoor at -2 in every slot
    traj = [21.05 + 0.1 * i for i in range(N + 1)]
    plan = _plan([-2.0] * N, [0.6] * N, traj=traj, tau=0.0)
    assert w3_trajectory_plausible(plan) == (True, "ok")


def test_falling_trajectory_at_min_offset_still_passes():
    traj = [21.6 - 0.05 * i for i in range(N + 1)]
    assert w3_trajectory_plausible(_plan([-2.0] * N, [0.6] * N, traj=traj)) == (True, "ok")


def test_min_offset_matches_dispatcher_integer_clamp(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -2.5)
    assert lp_offset_min_int() == -2  # int() truncates toward zero, as _lp_offsets does
    plan = _plan([-2.0] * N, [0.6] * N, min_off=None)  # older plan: live-config fallback
    assert w3_trajectory_plausible(plan) == (True, "ok")
    assert set(_lp_offsets(plan)) == {-2}
    plan.lwt_offset_c = [-1.0] * N  # written as -1: above the clamp, so it counts
    assert w3_trajectory_plausible(plan)[0] is False


def test_wire_gate_pass_writes_lp_rows_and_reaches_set_lwt_offset(monkeypatch):
    """Above-ceiling / min-offset / falling shape through the production writer:
    the action_schedule rows carry the LP's -2 (tier would write 0) and the
    reconciler hands -2 to set_lwt_offset."""
    import src.state_machine as sm
    from src.daikin.models import DaikinDevice

    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.scheduler.lp_dispatch._space_heating_demand_present", lambda: True)
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 150)
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "lp")
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()
    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params) or True,
    )
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
        now_utc = datetime.now(UTC).replace(second=0, microsecond=0)
        start = now_utc - timedelta(minutes=10)
        plan = _plan([-2.0] * N, [0.6] * N, start=start)
        assert w3_trajectory_plausible(plan) == (True, "ok")
        plan_date = now_utc.date().isoformat()

        assert _write_lwt_preheat_actions(plan_date, plan, []) >= 1
        rows = db.get_actions_for_plan_date(plan_date, device="daikin")

        def _p(r):
            return r["params"] if isinstance(r["params"], dict) else json.loads(r["params"])
        offs = {_p(r).get("lwt_offset") for r in rows if r["action_type"] == "lwt_preheat"}
        assert offs == {-2}
        assert read_lwt_source_effective()["source"] == "lp"

        dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)
        sm._reconcile_daikin_actions(rows, MagicMock(), dev, now_utc, trigger="test")
        assert apply_calls and apply_calls[-1].get("lwt_offset") == -2
        assert set(_tier_offsets(plan, [], None)) == {0}  # what tier would have written
