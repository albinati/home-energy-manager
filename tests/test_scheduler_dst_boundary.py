"""DST-boundary regression tests for UTC→local peak-window classification (#47).

Phase-1 audit of #47 (two independent forensic reviews) confirmed the scheduler
is already timezone-correct: every peak-window comparison site converts UTC
via ``.astimezone(ZoneInfo("Europe/London"))`` before matching
``SCHEDULER_PEAK_START`` / ``SCHEDULER_PEAK_END``, and LP slot classification
is price-based (DST-immune by construction). These tests lock the invariant
across UK DST transitions so that a future refactor cannot silently regress —
and close #47 with explicit coverage over the scenarios the OpenClaw agent
worried about.

UK DST (Europe/London):
- GMT → BST spring-forward: last Sunday of March at 01:00 UTC (02:00 local).
  For 2026: **2026-03-29 01:00 UTC**.
- BST → GMT fall-back: last Sunday of October at 01:00 UTC (01:00 local).
  For 2026: **2026-10-25 01:00 UTC**.

The same UTC wall-clock hour can fall on different sides of the 16:00-local
peak boundary depending on BST state — that is exactly the scenario #47
worried about. These tests pin the correct behaviour on both sides of both
transitions.
"""
from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from src.scheduler import daikin as daikin_mod
from src.scheduler.agile import (
    scheduler_peak_contains_wall_time,
    utc_instant_in_scheduler_peak,
)
from src.scheduler.daikin import compute_lwt_adjustment

PEAK_START, PEAK_END = "16:00", "19:00"
TZ_LONDON = "Europe/London"


# ── utc_instant_in_scheduler_peak across the GMT → BST spring-forward ────────

def test_peak_day_before_spring_forward_gmt_not_peak() -> None:
    """2026-03-28 (GMT): 15:30 UTC = 15:30 local → peak starts at 16:00 → NOT peak."""
    t = datetime(2026, 3, 28, 15, 30, tzinfo=UTC)
    assert utc_instant_in_scheduler_peak(t, PEAK_START, PEAK_END, TZ_LONDON) is False


def test_peak_on_spring_forward_day_post_jump_bst_is_peak() -> None:
    """2026-03-29 spring-forward day, AFTER 01:00 UTC jump: BST active.
    15:30 UTC = 16:30 BST → peak.
    """
    t = datetime(2026, 3, 29, 15, 30, tzinfo=UTC)
    assert utc_instant_in_scheduler_peak(t, PEAK_START, PEAK_END, TZ_LONDON) is True


def test_peak_day_after_spring_forward_bst_is_peak() -> None:
    """2026-03-30 (BST): 15:30 UTC = 16:30 local → in 16:00–19:00 peak."""
    t = datetime(2026, 3, 30, 15, 30, tzinfo=UTC)
    assert utc_instant_in_scheduler_peak(t, PEAK_START, PEAK_END, TZ_LONDON) is True


# ── utc_instant_in_scheduler_peak across the BST → GMT fall-back ─────────────

def test_peak_last_bst_afternoon_before_fall_back_is_peak() -> None:
    """2026-10-24 (still BST): 15:30 UTC = 16:30 BST → peak."""
    t = datetime(2026, 10, 24, 15, 30, tzinfo=UTC)
    assert utc_instant_in_scheduler_peak(t, PEAK_START, PEAK_END, TZ_LONDON) is True


def test_peak_first_gmt_afternoon_after_fall_back_not_peak() -> None:
    """2026-10-26 (back to GMT): 15:30 UTC = 15:30 GMT → NOT peak."""
    t = datetime(2026, 10, 26, 15, 30, tzinfo=UTC)
    assert utc_instant_in_scheduler_peak(t, PEAK_START, PEAK_END, TZ_LONDON) is False


# ── scheduler_peak_contains_wall_time is DST-agnostic ────────────────────────

def test_wall_time_helper_is_dst_agnostic() -> None:
    """The wall-clock helper only sees a local ``time`` — DST has already been
    resolved by the caller. Same local 16:30 → peak regardless of date/season."""
    assert scheduler_peak_contains_wall_time(time(16, 30), PEAK_START, PEAK_END) is True
    assert scheduler_peak_contains_wall_time(time(15, 30), PEAK_START, PEAK_END) is False


# ── compute_lwt_adjustment end-to-end across DST (freeze datetime.now) ───────

def _freeze_now(monkeypatch: pytest.MonkeyPatch, fixed: datetime) -> None:
    """Freeze ``src.scheduler.daikin.datetime.now(UTC)`` to *fixed*."""

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            if tz is UTC:
                return fixed
            return datetime.now(tz)

    monkeypatch.setattr(daikin_mod, "datetime", _FrozenDatetime)


def test_compute_lwt_gmt_afternoon_not_peak(monkeypatch: pytest.MonkeyPatch) -> None:
    """GMT (winter): 15:30 UTC = 15:30 GMT → not peak → no LWT setback, returns 0."""
    _freeze_now(monkeypatch, datetime(2026, 11, 1, 15, 30, tzinfo=UTC))
    assert compute_lwt_adjustment(35.0, 10.0, PEAK_START, PEAK_END, preheat_boost=3.0) == 0.0


def test_compute_lwt_bst_afternoon_is_peak(monkeypatch: pytest.MonkeyPatch) -> None:
    """BST (summer): 15:30 UTC = 16:30 BST → peak → LWT setback (clamped to -2)."""
    _freeze_now(monkeypatch, datetime(2026, 6, 15, 15, 30, tzinfo=UTC))
    assert compute_lwt_adjustment(35.0, 10.0, PEAK_START, PEAK_END, preheat_boost=3.0) == -2.0


def test_compute_lwt_spring_forward_boundary_flips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same 15:30 UTC wall-clock on consecutive days around spring-forward
    flips from NOT-peak (GMT side) to peak (BST side). Exactly the 1-hour
    scheduling drift #47 was worried about — proven correct here."""
    # 2026-03-28 (day BEFORE spring-forward, GMT): 15:30 UTC → 15:30 local → not peak
    _freeze_now(monkeypatch, datetime(2026, 3, 28, 15, 30, tzinfo=UTC))
    assert compute_lwt_adjustment(35.0, 10.0, PEAK_START, PEAK_END, preheat_boost=3.0) == 0.0

    # 2026-03-30 (day AFTER spring-forward, BST): 15:30 UTC → 16:30 local → peak
    _freeze_now(monkeypatch, datetime(2026, 3, 30, 15, 30, tzinfo=UTC))
    assert compute_lwt_adjustment(35.0, 10.0, PEAK_START, PEAK_END, preheat_boost=3.0) == -2.0


def test_compute_lwt_fall_back_boundary_flips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror of the spring test — same UTC wall-clock, opposite flip."""
    # 2026-10-24 (BST, last full summer afternoon): 15:30 UTC → 16:30 local → peak
    _freeze_now(monkeypatch, datetime(2026, 10, 24, 15, 30, tzinfo=UTC))
    assert compute_lwt_adjustment(35.0, 10.0, PEAK_START, PEAK_END, preheat_boost=3.0) == -2.0

    # 2026-10-26 (first full GMT afternoon): 15:30 UTC → 15:30 local → not peak
    _freeze_now(monkeypatch, datetime(2026, 10, 26, 15, 30, tzinfo=UTC))
    assert compute_lwt_adjustment(35.0, 10.0, PEAK_START, PEAK_END, preheat_boost=3.0) == 0.0
