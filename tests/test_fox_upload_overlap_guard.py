"""Defensive backstop: Fox V3 uploads with overlapping groups are refused.

Fox V3 stores HH:MM only and cycles daily. Any two groups whose minute-of-day
ranges intersect become indistinguishable on the inverter (firmware appears
to honour the last-registered window per minute bucket). #208 documented a
real prod incident on 2026-05-02 where the heuristic fallback emitted four
ForceCharge groups with overlapping ranges. The upload-time guard here is
the last line of defense — it stops bad payloads from any source (LP,
heuristic, or future regression) from reaching hardware.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from src import db
from src.foxess.models import SchedulerGroup
from src.scheduler.lp_dispatch import (
    _detect_overlapping_groups,
    upload_fox_if_operational,
)


def _g(start_h: int, start_m: int, end_h: int, end_m: int, mode: str = "ForceCharge") -> SchedulerGroup:
    return SchedulerGroup(
        start_hour=start_h,
        start_minute=start_m,
        end_hour=end_h,
        end_minute=end_m,
        work_mode=mode,
        min_soc_on_grid=15,
        fd_soc=95,
        fd_pwr=3000,
    )


def test_detect_overlapping_groups_flags_prod_incident_payload() -> None:
    """The exact payload from upload id=264 on 2026-05-02 must be flagged."""
    groups = [
        _g(1, 0, 5, 59),    # parent
        _g(10, 0, 15, 59),
        _g(1, 0, 1, 59),    # overlaps parent at 01:00-01:59
        _g(2, 30, 3, 59),   # overlaps parent at 02:30-03:59
    ]
    overlaps = _detect_overlapping_groups(groups)
    assert (0, 2) in overlaps, f"expected (0,2) overlap, got {overlaps}"
    assert (0, 3) in overlaps, f"expected (0,3) overlap, got {overlaps}"
    # 01:00-01:59 vs 02:30-03:59 do NOT overlap each other
    assert (2, 3) not in overlaps


def test_detect_overlapping_groups_passes_clean_payload() -> None:
    """A well-formed daily schedule (no overlaps) returns []."""
    groups = [
        _g(7, 30, 23, 59, "SelfUse"),
        _g(0, 0, 0, 30, "SelfUse"),
        _g(0, 30, 1, 59),
        _g(2, 30, 4, 30),
    ]
    assert _detect_overlapping_groups(groups) == []


def test_detect_overlapping_groups_handles_single_group() -> None:
    assert _detect_overlapping_groups([_g(0, 0, 23, 59)]) == []


def test_detect_overlapping_groups_handles_empty() -> None:
    assert _detect_overlapping_groups([]) == []


def test_adjacent_non_overlapping_groups_are_not_flagged() -> None:
    """01:00-01:59 then 02:00-02:59 must NOT be considered overlapping
    (end_minute is inclusive, so 01:59 and 02:00 are distinct minutes)."""
    groups = [_g(1, 0, 1, 59), _g(2, 0, 2, 59)]
    assert _detect_overlapping_groups(groups) == []


def test_upload_refused_when_overlap_detected(monkeypatch: Any, caplog: Any) -> None:
    """upload_fox_if_operational must short-circuit and never call set_scheduler_v3
    when the groups list contains overlaps."""
    from src.config import config as app_config

    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", False)

    fox = MagicMock()
    fox.api_key = "test-key"

    save_called: list[Any] = []
    monkeypatch.setattr(db, "save_fox_schedule_state", lambda *a, **kw: save_called.append((a, kw)))

    bad_groups = [_g(1, 0, 5, 59), _g(1, 0, 1, 59)]
    with caplog.at_level("ERROR"):
        ok = upload_fox_if_operational(fox, bad_groups)

    assert ok is False
    fox.set_scheduler_v3.assert_not_called()
    fox.set_scheduler_flag.assert_not_called()
    assert save_called == []
    assert any("Refusing Fox V3 upload" in r.getMessage() for r in caplog.records)


def test_upload_proceeds_for_clean_groups(monkeypatch: Any) -> None:
    """Clean groups must reach set_scheduler_v3 + set_scheduler_flag(True)."""
    from src.config import config as app_config

    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", False)

    fox = MagicMock()
    fox.api_key = "test-key"
    monkeypatch.setattr(db, "save_fox_schedule_state", lambda *a, **kw: None)

    clean_groups = [_g(0, 30, 1, 59), _g(2, 30, 4, 30)]
    ok = upload_fox_if_operational(fox, clean_groups)

    assert ok is True
    fox.set_scheduler_v3.assert_called_once()
    fox.set_scheduler_flag.assert_called_once_with(True)
