"""#951 — the W3 gate must not veto ceiling slack in slots already at the minimum offset."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config
from src.scheduler.lp_dispatch import (
    _lp_offsets,
    _lwt_preheat_pairs,
    _tier_offsets,
    w3_trajectory_plausible,
)
from src.scheduler.lp_optimizer import LpPlan

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


def _plan(offs, hi, min_off=-2.0):
    t0 = datetime(2026, 11, 14, tzinfo=UTC)
    p = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
               slot_starts_utc=[t0 + timedelta(minutes=30 * i) for i in range(N)],
               price_pence=[25.45] * N, temp_outdoor_c=[1.0] * N,
               cheap_threshold_pence=18.97, peak_threshold_pence=31.81,
               tariff_structure_kind="banded", price_band=["standard"] * N)
    p.lwt_offset_c = list(offs)
    p.space_electric_kwh = [0.3] * N  # the pump still draws power at the minimum offset
    p.indoor_temp_c = [21.6] * (N + 1)
    p.comfort_slack_c = [0.0] * N
    p.comfort_slack_hi_c = list(hi)
    p.comfort_slack_emergency_c = [0.0] * N
    p.w3_emergency_floor_c = 20.0
    p.w3_night_floor_c = 20.0
    p.w3_ceiling_c = 21.0
    p.w3_min_offset_c = min_off
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


def test_wire_rows_come_from_lp_not_tier_when_above_ceiling_at_min():
    plan = _plan([-2.0] * N, [0.6] * N)
    lp = _lp_offsets(plan)
    assert lp is not None and set(lp) == {-2}
    assert set(_tier_offsets(plan, [], None)) == {0}  # standard band: tier would write 0
    pairs = _lwt_preheat_pairs(plan, [], source="lp")
    assert pairs and {a["params"]["lwt_offset"] for _, a in pairs} == {-2}
