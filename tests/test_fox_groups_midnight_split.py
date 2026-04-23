"""Fox V3 groups: cross-midnight windows are split into two same-key halves.

Fox Scheduler V3 groups are ``HH:MM`` on a single 24 h cycle with no date field,
so a range like 22:00 → 02:00 is undefined. The rolling 24 h planner almost
always produces at least one window that crosses local midnight; the splitter
emits two groups (22:00 → 23:59 and 00:00 → 02:00) so the on-device schedule
is unambiguous.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.config import config as app_config
from src.scheduler.optimizer import HalfHourSlot, _merge_fox_groups


@pytest.fixture(autouse=True)
def _london_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")


def _fc_slot(start_utc: datetime) -> HalfHourSlot:
    return HalfHourSlot(
        start_utc=start_utc,
        end_utc=start_utc + timedelta(minutes=30),
        price_pence=-1.0,
        kind="negative",
        lp_grid_import_w=6000,
        target_soc_pct=100,
    )


def test_forcecharge_spanning_local_midnight_splits_in_two() -> None:
    """In GMT (winter), UTC 22:00 → 02:00 next day IS local 22:00 → 02:00.

    Expect two ForceCharge groups: 22:00 → 23:59 and 00:00 → 01:59 (end minute
    rolled back from :00 per the existing Fox V3 end-of-hour convention), with
    identical ``fd_soc`` / ``fd_pwr`` / ``min_soc_on_grid`` on both halves.
    """
    base = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
    slots = [_fc_slot(base + timedelta(minutes=30 * i)) for i in range(8)]
    groups = _merge_fox_groups(slots)

    assert len(groups) == 2, f"expected 2 groups from cross-midnight window, got {len(groups)}"
    g1, g2 = groups
    assert (g1.start_hour, g1.start_minute) == (22, 0)
    assert (g1.end_hour, g1.end_minute) == (23, 59)
    assert (g2.start_hour, g2.start_minute) == (0, 0)
    assert (g2.end_hour, g2.end_minute) == (1, 59)
    assert g1.work_mode == "ForceCharge"
    assert g2.work_mode == "ForceCharge"
    assert g1.fd_soc == g2.fd_soc
    assert g1.fd_pwr == g2.fd_pwr
    assert g1.min_soc_on_grid == g2.min_soc_on_grid


def test_window_ending_exactly_at_local_midnight_stays_single_group() -> None:
    """Endpoint at 00:00 local is NOT a crossing — keep one group with end 23:59."""
    base = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
    slots = [_fc_slot(base + timedelta(minutes=30 * i)) for i in range(4)]  # 22:00 → 00:00
    groups = _merge_fox_groups(slots)
    assert len(groups) == 1
    g = groups[0]
    assert (g.start_hour, g.start_minute) == (22, 0)
    assert (g.end_hour, g.end_minute) == (23, 59)


def test_midnight_split_during_bst_uses_local_wallclock() -> None:
    """During BST (UTC+1), UTC 21:00 → 01:00 is local 22:00 → 02:00 → still splits at local midnight."""
    base = datetime(2026, 4, 22, 21, 0, tzinfo=UTC)
    slots = [_fc_slot(base + timedelta(minutes=30 * i)) for i in range(8)]  # 21:00 → 01:00 UTC
    groups = _merge_fox_groups(slots)
    assert len(groups) == 2
    g1, g2 = groups
    assert (g1.start_hour, g1.start_minute) == (22, 0)  # local BST
    assert (g1.end_hour, g1.end_minute) == (23, 59)
    assert (g2.start_hour, g2.start_minute) == (0, 0)
    assert (g2.end_hour, g2.end_minute) == (1, 59)  # 02:00 local rolled back


def test_no_split_for_same_day_window() -> None:
    """A morning-only ForceCharge window does not trigger the splitter."""
    base = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)  # GMT → 09:00 local
    slots = [_fc_slot(base + timedelta(minutes=30 * i)) for i in range(4)]  # 09:00 → 11:00
    groups = _merge_fox_groups(slots)
    assert len(groups) == 1
    g = groups[0]
    assert (g.start_hour, g.start_minute) == (9, 0)
    assert (g.end_hour, g.end_minute) == (10, 59)  # 11:00 rolled back per convention
