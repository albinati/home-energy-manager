"""Heuristic Daikin restore window must match the LP path's safety floor (#253).

The 2026-04-30 active-mode rollout hit a race: a 1-minute restore window
(end_utc + 1 min) was narrower than the 2-minute heartbeat interval. When
a heartbeat tick landed just past the restore window, the state machine
silently marked the action ``completed`` without firing — leaving Daikin
in shutdown / preheat settings until manual recovery.

The LP dispatch path was fixed in PR #218 to use
``max(2, LP_RESTORE_WINDOW_MINUTES)`` for the restore window. The heuristic
fallback in :func:`src.scheduler.optimizer._write_daikin_schedule` kept
the legacy 1-minute window until #253. This test guards the parity so a
future refactor can't reintroduce the 1-min window.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src import db
from src.config import config as app_config
from src.scheduler.optimizer import HalfHourSlot, _write_daikin_schedule
from src.weather import HourlyForecast


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "heuristic.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


def _peak_slot_with_neighbours() -> tuple[list[HalfHourSlot], list[HourlyForecast]]:
    """One non-standard slot ('peak') flanked by standard slots so the
    heuristic writer emits exactly one action + one restore."""
    base = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
    slots: list[HalfHourSlot] = []
    kinds = ["standard", "peak", "standard"]
    for i, kind in enumerate(kinds):
        start = base + timedelta(minutes=30 * i)
        slots.append(HalfHourSlot(
            start_utc=start,
            end_utc=start + timedelta(minutes=30),
            price_pence=10.0 if kind == "standard" else 40.0,
            kind=kind,
        ))
    forecast = [
        HourlyForecast(
            time_utc=base + timedelta(hours=i),
            temperature_c=12.0,
            cloud_cover_pct=40.0,
            shortwave_radiation_wm2=400.0,
            estimated_pv_kw=0.5,
            heating_demand_factor=1.0,
        )
        for i in range(3)
    ]
    return slots, forecast


def _restore_action_rows() -> list[dict[str, Any]]:
    """Pull every restore row written to ``action_schedule`` after the test
    seeded a heuristic plan."""
    import sqlite3
    conn = sqlite3.connect(app_config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT start_time, end_time, device, action_type "
            "FROM action_schedule WHERE action_type = 'restore' "
            "ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _window_minutes(row: dict[str, Any]) -> float:
    st = datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))
    en = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
    return (en - st).total_seconds() / 60.0


def test_heuristic_restore_window_uses_lp_restore_window_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the default config (5 min), the heuristic writer must emit a
    restore action spanning 5 minutes — matching the LP path."""
    monkeypatch.setattr(app_config, "LP_RESTORE_WINDOW_MINUTES", 5)
    slots, forecast = _peak_slot_with_neighbours()

    n = _write_daikin_schedule(plan_date="2026-07-01", slots=slots, forecast=forecast)
    assert n > 0, "heuristic should write at least one action + restore for the peak slot"

    rows = _restore_action_rows()
    assert len(rows) >= 1
    for r in rows:
        width = _window_minutes(r)
        assert width >= 5.0, (
            f"heuristic restore window {width:.1f}min < 5min — "
            f"regressed to pre-#253 1-minute behaviour"
        )


def test_heuristic_restore_window_floor_is_2_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with absurd config, the window can't drop below the 2-min
    heartbeat floor (mirrors the LP path's safety clamp)."""
    monkeypatch.setattr(app_config, "LP_RESTORE_WINDOW_MINUTES", 0)
    slots, forecast = _peak_slot_with_neighbours()

    _write_daikin_schedule(plan_date="2026-07-01", slots=slots, forecast=forecast)
    rows = _restore_action_rows()
    assert len(rows) >= 1
    for r in rows:
        width = _window_minutes(r)
        assert width >= 2.0, (
            f"heuristic restore window {width:.1f}min < 2min floor — "
            f"a config typo must NOT reintroduce the 2026-04-30 race"
        )


def test_heuristic_restore_window_honours_config_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wider explicit override widens the heuristic window same as the LP path."""
    monkeypatch.setattr(app_config, "LP_RESTORE_WINDOW_MINUTES", 10)
    slots, forecast = _peak_slot_with_neighbours()

    _write_daikin_schedule(plan_date="2026-07-01", slots=slots, forecast=forecast)
    rows = _restore_action_rows()
    assert len(rows) >= 1
    for r in rows:
        width = _window_minutes(r)
        assert width >= 10.0, (
            f"heuristic restore window {width:.1f}min < requested 10min "
            f"override — config not threaded through"
        )
