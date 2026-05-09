"""PR 5 of plan: Daikin write-budget guard + Sunday legionella skip.

The dispatch layer must:
1. Skip any (restore, action) pair whose action falls inside Sunday 10:30–12:00
   local — Daikin firmware owns the weekly thermal-shock cycle.
2. Coalesce adjacent same-kind low-value pairs (``pre_heat``, ``solar_preheat``)
   when 2 × len(pairs) > headroom.
3. Drop trailing low-value pairs if still over budget after coalescing.
4. NEVER drop or coalesce ``max_heat`` (negative-price) or ``shutdown`` (peak)
   — those time-critical pairs always survive.
5. Notify the operator (via ``notify_strategy_update``) when actions were
   dropped so the budget can be raised if the pattern persists.

These tests exercise the helpers directly with synthetic pair lists so we
don't have to round-trip through a full LP solve.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest


def _pair(action_type: str, start_iso: str, end_iso: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a (restore, action) pair shaped like ``daikin_dispatch_preview``."""
    restore_end = (
        datetime.fromisoformat(end_iso.replace("Z", "+00:00")) + timedelta(minutes=5)
    ).isoformat().replace("+00:00", "Z")
    return (
        {
            "device": "daikin",
            "action_type": "restore",
            "start_time": end_iso,
            "end_time": restore_end,
            "params": {"tank_temp": 45.0, "tank_powerful": False, "tank_power": True,
                       "lwt_offset": 0.0, "climate_on": True},
        },
        {
            "device": "daikin",
            "action_type": action_type,
            "start_time": start_iso,
            "end_time": end_iso,
            "params": {"tank_temp": 60.0, "tank_powerful": True, "tank_power": True,
                       "lwt_offset": 0.0, "climate_on": True},
            "lp_slot_kind": action_type,
        },
    )


# --------------------------------------------------------------------------
# Sunday legionella skip
# --------------------------------------------------------------------------

def test_drops_pair_inside_sunday_legionella_window() -> None:
    """Sunday 11:00 BST = 10:00 UTC — inside 10:30–12:00 BST window when
    BST is active. Test in winter (UTC=local) for unambiguous timing."""
    from src.scheduler.lp_dispatch import _drop_legionella_window_pairs
    # Sunday 2026-01-04 — UTC = local in winter.
    inside = _pair("max_heat", "2026-01-04T11:00:00Z", "2026-01-04T11:30:00Z")
    outside = _pair("max_heat", "2026-01-04T13:00:00Z", "2026-01-04T13:30:00Z")
    out = _drop_legionella_window_pairs([inside, outside])
    assert len(out) == 1
    assert out[0][1]["start_time"] == "2026-01-04T13:00:00Z"


def test_keeps_pair_outside_sunday_window() -> None:
    """Same hour but a different weekday — pair must survive."""
    from src.scheduler.lp_dispatch import _drop_legionella_window_pairs
    # Saturday 2026-01-03 11:00 UTC — same time, wrong day.
    pair = _pair("max_heat", "2026-01-03T11:00:00Z", "2026-01-03T11:30:00Z")
    out = _drop_legionella_window_pairs([pair])
    assert len(out) == 1


def test_keeps_pair_at_sunday_window_edge() -> None:
    """The 12:00 boundary is exclusive — a pair starting exactly at 12:00
    local must survive."""
    from src.scheduler.lp_dispatch import _drop_legionella_window_pairs
    pair = _pair("max_heat", "2026-01-04T12:00:00Z", "2026-01-04T12:30:00Z")
    assert len(_drop_legionella_window_pairs([pair])) == 1


# --------------------------------------------------------------------------
# Coalesce
# --------------------------------------------------------------------------

def test_coalesce_merges_adjacent_solar_preheat_pairs() -> None:
    """Two adjacent ``solar_preheat`` windows → one extended pair, restore
    pushed to end of merged window."""
    from src.scheduler.lp_dispatch import _coalesce_low_value_pairs
    p1 = _pair("solar_preheat", "2026-06-01T13:00:00Z", "2026-06-01T13:30:00Z")
    p2 = _pair("solar_preheat", "2026-06-01T13:30:00Z", "2026-06-01T14:00:00Z")
    out = _coalesce_low_value_pairs([p1, p2])
    assert len(out) == 1
    rest, act = out[0]
    assert act["start_time"] == "2026-06-01T13:00:00Z"
    assert act["end_time"] == "2026-06-01T14:00:00Z"
    # Restore window now starts after the merged action.
    assert rest["start_time"] == "2026-06-01T14:00:00Z"


def test_coalesce_does_not_merge_high_value_kinds() -> None:
    """Two adjacent ``max_heat`` windows must NOT coalesce — those are
    high-value windows whose timing can't be substituted."""
    from src.scheduler.lp_dispatch import _coalesce_low_value_pairs
    p1 = _pair("max_heat", "2026-06-01T01:00:00Z", "2026-06-01T01:30:00Z")
    p2 = _pair("max_heat", "2026-06-01T01:30:00Z", "2026-06-01T02:00:00Z")
    out = _coalesce_low_value_pairs([p1, p2])
    assert len(out) == 2


def test_coalesce_does_not_merge_non_adjacent_pairs() -> None:
    """Two solar_preheat windows separated by a gap must stay separate."""
    from src.scheduler.lp_dispatch import _coalesce_low_value_pairs
    p1 = _pair("solar_preheat", "2026-06-01T13:00:00Z", "2026-06-01T13:30:00Z")
    p2 = _pair("solar_preheat", "2026-06-01T14:00:00Z", "2026-06-01T14:30:00Z")
    out = _coalesce_low_value_pairs([p1, p2])
    assert len(out) == 2


# --------------------------------------------------------------------------
# Budget guard end-to-end
# --------------------------------------------------------------------------

def test_budget_guard_passes_through_under_headroom() -> None:
    """When 2 × len(pairs) ≤ headroom, no coalesce / drop / notify."""
    from src.scheduler.lp_dispatch import _apply_write_budget
    pairs = [
        _pair("max_heat", "2026-06-01T01:00:00Z", "2026-06-01T01:30:00Z"),
        _pair("solar_preheat", "2026-06-01T13:00:00Z", "2026-06-01T13:30:00Z"),
    ]
    out, dropped = _apply_write_budget(pairs, headroom=10)
    assert len(out) == 2
    assert dropped == []


def test_budget_guard_drops_low_value_when_over_budget() -> None:
    """14-pair plan with headroom=10 → coalesces solar_preheat but keeps all
    high-value pairs. Result must fit within headroom."""
    from src.scheduler.lp_dispatch import _apply_write_budget
    # 4 max_heat (high value) + 10 solar_preheat (low value)
    pairs = [
        _pair("max_heat", f"2026-06-01T0{1+i}:00:00Z", f"2026-06-01T0{1+i}:30:00Z")
        for i in range(4)
    ] + [
        _pair("solar_preheat", f"2026-06-01T{12+i:02d}:00:00Z", f"2026-06-01T{12+i:02d}:30:00Z")
        for i in range(10)
    ]
    out, dropped = _apply_write_budget(pairs, headroom=10)
    # Must fit (each pair = 2 writes).
    assert 2 * len(out) <= 10
    # All max_heat survive.
    high_value_kept = [a.get("action_type") for _r, a in out if a.get("action_type") == "max_heat"]
    assert len(high_value_kept) == 4
    # Some solar_preheat got dropped.
    assert len(dropped) > 0
    assert all("solar_preheat" in d for d in dropped)


def test_budget_guard_zero_headroom_drops_all_low_value() -> None:
    """When headroom is 0, every low-value pair must be dropped but high-
    value pairs survive (they fight for hardware-critical timing)."""
    from src.scheduler.lp_dispatch import _apply_write_budget
    pairs = [
        _pair("max_heat", "2026-06-01T01:00:00Z", "2026-06-01T01:30:00Z"),
        _pair("solar_preheat", "2026-06-01T13:00:00Z", "2026-06-01T13:30:00Z"),
        _pair("shutdown", "2026-06-01T17:00:00Z", "2026-06-01T17:30:00Z"),
    ]
    out, dropped = _apply_write_budget(pairs, headroom=0)
    kept_kinds = {a.get("action_type") for _r, a in out}
    assert kept_kinds == {"max_heat", "shutdown"}
    assert len(dropped) == 1
    assert "solar_preheat" in dropped[0]


def test_budget_guard_never_drops_max_heat_or_shutdown() -> None:
    """Even with absurdly low headroom, max_heat and shutdown survive — they
    target hardware-critical timing windows that other slots can't substitute."""
    from src.scheduler.lp_dispatch import _apply_write_budget
    pairs = [
        _pair("max_heat", "2026-06-01T01:00:00Z", "2026-06-01T01:30:00Z"),
        _pair("shutdown", "2026-06-01T17:00:00Z", "2026-06-01T17:30:00Z"),
    ]
    out, dropped = _apply_write_budget(pairs, headroom=0)
    assert len(out) == 2  # both kept
    assert dropped == []


def test_budget_guard_coalesce_alone_can_fit_under_budget() -> None:
    """When coalescing brings us under budget, no pair should be dropped."""
    from src.scheduler.lp_dispatch import _apply_write_budget
    # 8 adjacent solar_preheat slots × 2 writes = 16; coalesce → 1 pair × 2 = 2.
    pairs = [
        _pair(
            "solar_preheat",
            f"2026-06-01T{12 + i//2:02d}:{(i%2)*30:02d}:00Z",
            f"2026-06-01T{12 + (i+1)//2:02d}:{((i+1)%2)*30:02d}:00Z",
        )
        for i in range(8)
    ]
    out, dropped = _apply_write_budget(pairs, headroom=2)
    assert len(out) == 1
    assert dropped == []


# --------------------------------------------------------------------------
# Notification side-effect
# --------------------------------------------------------------------------

def test_write_daikin_notifies_when_dropping(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the budget guard drops actions, ``notify_strategy_update`` must
    be called with the drop summary."""
    from src.config import config as app_config
    from src.scheduler import lp_dispatch
    from src.scheduler.lp_optimizer import LpPlan

    # Stub quota_remaining tiny to force drops.
    def _fake_remaining(vendor: str) -> int:
        return 4 if vendor == "daikin" else 9999
    monkeypatch.setattr("src.api_quota.quota_remaining", _fake_remaining)
    monkeypatch.setattr(app_config, "DAIKIN_RESERVE_FOR_HEARTBEAT", 0, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)

    # Stub upsert + clear so we don't touch SQLite.
    monkeypatch.setattr(lp_dispatch.db, "upsert_action", lambda **kw: 1)
    monkeypatch.setattr(lp_dispatch.db, "clear_actions_in_range", lambda *a, **kw: 0)
    monkeypatch.setattr(lp_dispatch.db, "clear_actions_for_date", lambda *a, **kw: 0)
    monkeypatch.setattr(lp_dispatch.db, "update_action_restore_link", lambda *a, **kw: None)

    # Build a minimal LpPlan
    base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [base + timedelta(minutes=30 * i) for i in range(2)]

    # Adjacent solar_preheat pairs (each pair contiguous: 12:00-12:30,
    # 12:30-13:00, …) → coalesce to 1 → 2 writes. Headroom 4. No drops, no notify.
    def _fake_preview_adjacent(plan: LpPlan, forecast: list) -> list:
        out = []
        for i in range(8):
            start_h = 12 + i // 2
            start_m = (i % 2) * 30
            end_h = 12 + (i + 1) // 2
            end_m = ((i + 1) % 2) * 30
            out.append(_pair(
                "solar_preheat",
                f"2026-06-01T{start_h:02d}:{start_m:02d}:00Z",
                f"2026-06-01T{end_h:02d}:{end_m:02d}:00Z",
            ))
        return out
    monkeypatch.setattr(lp_dispatch, "daikin_dispatch_preview", _fake_preview_adjacent)

    with patch("src.notifier.notify_strategy_update") as mock_notify:
        lp_dispatch.write_daikin_from_lp_plan("2026-06-01", plan, forecast=[])
        # 8 solar_preheat pairs adjacent → coalesce → 1 pair = 2 writes ≤ 4. No drops.
        assert mock_notify.call_count == 0, (
            f"Coalesce alone should fit headroom; mock called with: {mock_notify.call_args}"
        )

    # Non-adjacent pairs (1h gap each) so coalesce can't help → forces drops.
    def _fake_preview_non_adjacent(plan: LpPlan, forecast: list) -> list:
        # 6 pairs at 12,14,16,18,20,22 → all in 24 h, no contiguity.
        return [
            _pair(
                "solar_preheat",
                f"2026-06-01T{12 + 2*i:02d}:00:00Z",
                f"2026-06-01T{12 + 2*i:02d}:30:00Z",
            )
            for i in range(6)
        ]
    monkeypatch.setattr(lp_dispatch, "daikin_dispatch_preview", _fake_preview_non_adjacent)

    with patch("src.notifier.notify_strategy_update") as mock_notify:
        lp_dispatch.write_daikin_from_lp_plan("2026-06-01", plan, forecast=[])
        assert mock_notify.call_count == 1
        msg = mock_notify.call_args[0][0]
        assert "Daikin write-budget guard" in msg
        assert "dropped" in msg
