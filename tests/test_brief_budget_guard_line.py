"""Daily brief surfaces Daikin budget-guard drops as a pull-based warning.

#309 partial: when the dispatch budget guard prunes low-value pairs (CHEAP /
SOLAR_PREHEAT) due to tight Daikin quota, the morning brief shows a one-line
summary so the user knows their plan was reshaped without being pinged on
Telegram (see memory: feedback_low_push_load.md).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.analytics import daily_brief


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _log_drop(when: datetime, *, n_dropped: int, headroom: int, kinds: list[str]) -> None:
    """Insert a budget_guard_drop row for the given timestamp."""
    dropped_entries = [f"{k}@{when.isoformat()}" for k in kinds]
    db.log_action(
        device="daikin",
        action="budget_guard_drop",
        params={
            "dropped": dropped_entries,
            "headroom": headroom,
            "reserve": 30,
            "n_dropped": n_dropped,
        },
        result="dropped",
        trigger="lp_dispatch",
    )
    # Override timestamp by direct UPDATE (log_action sets to now).
    with db._lock:
        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE action_log SET timestamp = ? WHERE rowid = (SELECT MAX(rowid) FROM action_log)",
                (when.isoformat().replace("+00:00", "Z"),),
            )
            conn.commit()
        finally:
            conn.close()


def test_budget_guard_line_summarises_drops_for_day() -> None:
    """Two drop events on the same day → one summary line aggregating them."""
    day = datetime(2026, 5, 10, 0, 0, tzinfo=UTC).date()
    _log_drop(
        datetime(2026, 5, 10, 6, 5, tzinfo=UTC),
        n_dropped=3,
        headroom=2,
        kinds=["pre_heat", "pre_heat", "solar_preheat"],
    )
    _log_drop(
        datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
        n_dropped=1,
        headroom=1,
        kinds=["solar_preheat"],
    )

    line = daily_brief._budget_guard_summary_line(day)

    assert line is not None
    assert "dropped 4 low-value pair(s)" in line
    # min headroom across both events
    assert "headroom=1" in line
    assert "pre_heat" in line and "solar_preheat" in line


def test_budget_guard_line_returns_none_when_no_drops() -> None:
    day = datetime(2026, 5, 10, 0, 0, tzinfo=UTC).date()
    assert daily_brief._budget_guard_summary_line(day) is None


def test_budget_guard_line_only_today_not_yesterday() -> None:
    """A drop yesterday must not appear in today's brief."""
    today = datetime(2026, 5, 10, 0, 0, tzinfo=UTC).date()
    _log_drop(
        datetime(2026, 5, 9, 14, 0, tzinfo=UTC),
        n_dropped=2,
        headroom=5,
        kinds=["pre_heat", "pre_heat"],
    )

    assert daily_brief._budget_guard_summary_line(today) is None
    # But it appears for yesterday's brief
    yesterday = today - timedelta(days=1)
    line = daily_brief._budget_guard_summary_line(yesterday)
    assert line is not None
    assert "dropped 2" in line
