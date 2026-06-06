"""Heuristic LWT pre-heat — active space-heating control (#481).

Open-loop price-tier offset: boost the leaving-water temperature in cheap
slots, set it back in peak slots, neutral otherwise. Offset is an INTEGER in
the device range, only emitted while the firmware is plausibly heating
(outdoor < curve high anchor), and clamped so we can never exceed the Daikin
quota at the dispatch boundary. A sensor-ready comfort hook is wired but
no-op until a room sensor exists.
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import config
from src.daikin.models import DaikinDevice
from src.scheduler.lp_dispatch import _lwt_preheat_pairs, _preheat_lwt_offset, smooth_lwt_offsets
from src.scheduler.lp_optimizer import LpPlan


# --------------------------------------------------------------------------- #
# smooth_lwt_offsets — thermal coherence (no per-slot chatter)
# --------------------------------------------------------------------------- #
def test_smooth_drops_short_isolated_blocks():
    # A lone 1-slot boost is thermally negligible → dropped to neutral.
    assert smooth_lwt_offsets([0, 0, 3, 0, 0], 4) == [0, 0, 0, 0, 0]


def test_smooth_keeps_sustained_blocks():
    # A 4-slot boost meets the threshold → kept.
    assert smooth_lwt_offsets([3, 3, 3, 3, 0], 4) == [3, 3, 3, 3, 0]


def test_smooth_bridges_short_gap_between_same_blocks():
    # The real chatter case: +3,0,+3,0,+3 around the cheap threshold → one block.
    assert smooth_lwt_offsets([3, 0, 3, 0, 3, 3, 3, 3], 4) == [3, 3, 3, 3, 3, 3, 3, 3]


def test_smooth_does_not_bridge_different_neighbours():
    # boost→0→setback is a genuine transition; the 0 buffer stays.
    out = smooth_lwt_offsets([3, 3, 3, 3, 0, -2, -2, -2, -2], 4)
    assert out == [3, 3, 3, 3, 0, -2, -2, -2, -2]


def test_smooth_passthrough_when_min_block_one():
    assert smooth_lwt_offsets([3, 0, -2, 0, 3], 1) == [3, 0, -2, 0, 3]


def test_smooth_preserves_none():
    assert smooth_lwt_offsets([None, None, 3, 3, 3, 3, None], 4) == [None, None, 3, 3, 3, 3, None]

# Tiers used throughout: cheap ≤ 12, peak ≥ 25 (the prod defaults).
CHEAP = 12.0
PEAK = 25.0
COLD = 5.0   # outdoor < DAIKIN_WEATHER_CURVE_HIGH_C (18) → heating active
WARM = 20.0  # outdoor ≥ 18 → firmware idle


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_BOOST_C", 3)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", -2)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_COMFORT_BAND_C", 0.5)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -10.0)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 5.0)
    monkeypatch.setattr(config, "DAIKIN_WEATHER_CURVE_HIGH_C", 18.0)
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 21.0)
    # Disable thermal-coherence smoothing for the per-slot windowing tests so a
    # short test sequence isn't collapsed; smoothing is exercised separately.
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 1)


def _off(price, outdoor, **kw):
    return _preheat_lwt_offset(price, outdoor, cheap_thr=CHEAP, peak_thr=PEAK, **kw)


# --------------------------------------------------------------------------- #
# _preheat_lwt_offset
# --------------------------------------------------------------------------- #
def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", False)
    assert _off(5.0, COLD) is None


def test_warm_day_returns_none(enabled):
    # Firmware won't run the compressor → emit nothing (climate hands-off, no quota).
    assert _off(5.0, WARM) is None
    assert _off(30.0, WARM) is None


def test_cheap_slot_boosts(enabled):
    assert _off(5.0, COLD) == 3
    assert _off(CHEAP, COLD) == 3  # boundary inclusive


def test_peak_slot_sets_back(enabled):
    assert _off(30.0, COLD) == -2
    assert _off(PEAK, COLD) == -2  # boundary inclusive


def test_mid_price_is_neutral(enabled):
    assert _off(18.0, COLD) == 0


def test_offset_is_int(enabled):
    for p in (5.0, 18.0, 30.0):
        v = _off(p, COLD)
        assert isinstance(v, int)


def test_clamped_to_device_range(enabled, monkeypatch):
    # Absurd boost/setback get clamped to the configured [-10, +5] integer range.
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_BOOST_C", 99)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", -99)
    assert _off(5.0, COLD) == 5     # clamp to MAX
    assert _off(30.0, COLD) == -10  # clamp to MIN


def test_comfort_guard_suppresses_boost_when_room_warm(enabled):
    # Room already above setpoint+band → don't pre-heat (would overshoot).
    assert _off(5.0, COLD, indoor_c=22.0) == 0
    # Just inside the band → boost still allowed.
    assert _off(5.0, COLD, indoor_c=21.2) == 3


def test_comfort_guard_suppresses_setback_when_room_cold(enabled):
    # Room already below setpoint-band → don't set back (would chill further).
    assert _off(30.0, COLD, indoor_c=20.0) == 0
    assert _off(30.0, COLD, indoor_c=20.8) == -2


def test_no_sensor_is_noop(enabled):
    # indoor_c=None (the current reality) → guard never fires.
    assert _off(5.0, COLD, indoor_c=None) == 3
    assert _off(30.0, COLD, indoor_c=None) == -2


# --------------------------------------------------------------------------- #
# _lwt_preheat_pairs
# --------------------------------------------------------------------------- #
def _plan(prices, outdoors):
    n = len(prices)
    t0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    return LpPlan(
        ok=True,
        status="optimal",
        objective_pence=0.0,
        slot_starts_utc=[t0 + timedelta(minutes=30 * i) for i in range(n)],
        price_pence=list(prices),
        temp_outdoor_c=list(outdoors),
        cheap_threshold_pence=CHEAP,
        peak_threshold_pence=PEAK,
    )


def test_pairs_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", False)
    plan = _plan([5, 5, 30, 30], [COLD] * 4)
    assert _lwt_preheat_pairs(plan, []) == []


def test_pairs_merge_consecutive_same_offset(enabled):
    # Two cheap slots then two peak slots → one boost window + one setback window.
    plan = _plan([5, 5, 30, 30], [COLD] * 4)
    pairs = _lwt_preheat_pairs(plan, [])
    actions = [a for _r, a in pairs]
    assert len(actions) == 2
    assert actions[0]["params"]["lwt_offset"] == 3
    assert actions[1]["params"]["lwt_offset"] == -2
    # Boost window spans both cheap slots (00:00 → 01:00).
    assert actions[0]["start_time"].endswith("T00:00:00Z")
    assert actions[0]["end_time"].endswith("T01:00:00Z")


def test_neutral_slots_emit_nothing(enabled):
    # All mid-price → no windows at all.
    plan = _plan([18, 18, 18], [COLD] * 3)
    assert _lwt_preheat_pairs(plan, []) == []


def test_warm_slots_emit_nothing(enabled):
    # Cheap price but warm outdoor → firmware idle → no offset write.
    plan = _plan([5, 5, 5], [WARM] * 3)
    assert _lwt_preheat_pairs(plan, []) == []


def test_restore_returns_offset_to_zero(enabled):
    # A boost window followed by a neutral gap keeps its restore (→ offset 0).
    plan = _plan([5, 18, 18], [COLD] * 3)
    pairs = _lwt_preheat_pairs(plan, [])
    assert len(pairs) == 1
    restore, action = pairs[0]
    assert action["params"]["lwt_offset"] == 3
    assert restore is not None
    assert restore["params"]["lwt_offset"] == 0
    assert restore["action_type"] == "restore"


def test_adjacent_flip_drops_intermediate_restore(enabled):
    # boost immediately followed by setback (no neutral gap) → the boost's
    # restore-to-0 is dropped so the offset goes +3 → -2 directly.
    plan = _plan([5, 30], [COLD, COLD])
    pairs = _lwt_preheat_pairs(plan, [])
    assert len(pairs) == 2
    assert pairs[0][0] is None  # boost restore dropped (superseded)
    assert pairs[0][1]["params"]["lwt_offset"] == 3
    assert pairs[1][1]["params"]["lwt_offset"] == -2


def test_rows_tagged_lp_optimizer_and_device(enabled):
    plan = _plan([5, 5], [COLD, COLD])
    pairs = _lwt_preheat_pairs(plan, [])
    _r, a = pairs[0]
    assert a["device"] == "daikin"
    assert a["action_type"] == "lwt_preheat"
    assert a["params"]["lp_optimizer"] is True


# --------------------------------------------------------------------------- #
# Fire-path gating — the lwt_offset hands-off strip (#300) must yield to #481
# when the feature is enabled, else the emitted rows are silently neutered.
# --------------------------------------------------------------------------- #
def _seed_lwt_row(conn, params: dict) -> int:
    now = datetime.now(UTC)
    start = (now - timedelta(seconds=60)).isoformat()
    end = (now + timedelta(seconds=1800)).isoformat()
    conn.execute(
        """INSERT INTO action_schedule
           (date, start_time, end_time, device, action_type, params, status, created_at)
           VALUES (?, ?, ?, 'daikin', 'lwt_preheat', ?, 'active', ?)""",
        (now.date().isoformat(), start, end, json.dumps(params), now.isoformat()),
    )
    rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return rid


def _run_reconcile_with_offset(monkeypatch, *, enabled_flag: bool, prefire: bool):
    """Seed an lwt_preheat row (wants +3), device at offset 0, run the
    reconciler with a stubbed apply, and return the captured apply params.
    """
    import src.state_machine as sm
    from src import db

    monkeypatch.setattr("src.config.config.DAIKIN_LWT_PREHEAT_ENABLED", enabled_flag)
    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", prefire)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params) or True,
    )

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_lwt_row(conn, {"lwt_offset": 3, "lp_optimizer": True})
        finally:
            conn.close()

        dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, MagicMock(), dev, now_utc, trigger="test")
    return apply_calls


def test_enabled_lets_lwt_offset_reach_apply(monkeypatch):
    # #481 regression: with the feature ON and device offset diverging (0 → 3),
    # the row must fire WITH lwt_offset intact (not stripped by the #300 guard).
    apply_calls = _run_reconcile_with_offset(monkeypatch, enabled_flag=True, prefire=True)
    assert len(apply_calls) == 1
    assert apply_calls[0].get("lwt_offset") == 3


def test_disabled_strips_lwt_offset(monkeypatch):
    # Feature OFF: climate-hands-off preserved — lwt_offset is stripped before
    # apply (prefire disabled here so we observe the stripped apply directly).
    apply_calls = _run_reconcile_with_offset(monkeypatch, enabled_flag=False, prefire=False)
    assert len(apply_calls) == 1
    assert "lwt_offset" not in apply_calls[0]


def test_enabled_idempotency_skips_when_offset_matches(monkeypatch):
    # Quota guard: device already at the target offset → pre-fire match → no apply.
    import src.state_machine as sm
    from src import db

    monkeypatch.setattr("src.config.config.DAIKIN_LWT_PREHEAT_ENABLED", True)
    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params) or True,
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            rid = _seed_lwt_row(conn, {"lwt_offset": 3, "lp_optimizer": True})
        finally:
            conn.close()
        dev = DaikinDevice(id="gw", name="x", lwt_offset=3.0)  # already at target
        now_utc = datetime.now(UTC)
        rows = db.get_actions_for_plan_date(now_utc.date().isoformat(), device="daikin")
        sm._reconcile_daikin_actions(rows, MagicMock(), dev, now_utc, trigger="test")
        row = db.get_action_by_id(rid)
    assert len(apply_calls) == 0
    assert row is not None and row["status"] == "completed"
    assert (row.get("error_msg") or "").startswith("noop")
