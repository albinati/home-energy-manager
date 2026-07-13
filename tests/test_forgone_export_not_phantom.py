"""PV surplus export must NEVER be reported as 'forgone' revenue.

Regression for a live prod bug: both the audit report and the DB helper counted
ANY slot with ``export_kwh > 0`` that didn't dispatch as ``peak_export`` as
"forgone export". But in the normal/guests presets the LP constrains
``exp <= pv_use`` — so ``export_kwh`` there is PV SURPLUS, which Fox V3 SelfUse
exports passively and which already earns money.

Result on prod: over 14 days the audit report claimed **97.9 kWh across 220
slots** of "forgone export" — every kWh of which was actually exported and paid
for — and advised the user to set ``ENERGY_STRATEGY_MODE=savings_first``, a
variable removed in PR C. Fixed by requiring ``lp_kind == 'peak_export'``: the
only forgone revenue is a battery export the LP chose and the pessimistic
scenario filter blocked.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from src.analytics.audit_report import _effective_plan_export_kwh, _forgone_export_kwh


def _slot(lp_kind: str, dispatched_kind: str, export_kwh: float) -> dict:
    return {
        "lp_kind": lp_kind,
        "dispatched_kind": dispatched_kind,
        "export_kwh": export_kwh,
    }


# --------------------------------------------------------------------------
# The unit: which slots count as forgone?
# --------------------------------------------------------------------------

def test_pv_surplus_export_is_not_forgone() -> None:
    """The prod shape: sunny slots that exported PV. NOT a loss — it earned."""
    for lp_kind in ("solar_charge", "peak", "standard", "tank_idle_overnight",
                    "cheap", "negative_hold"):
        slot = _slot(lp_kind, dispatched_kind=lp_kind, export_kwh=0.45)
        assert _forgone_export_kwh(slot) == 0.0, (
            f"{lp_kind}: PV surplus counted as forgone — this is the phantom-loss bug"
        )


def test_pre_negative_drain_is_not_forgone() -> None:
    """A pre-negative battery drain ships as ForceDischarge — it isn't blocked."""
    slot = _slot("pre_negative_export", "pre_negative_export", export_kwh=1.35)
    assert _forgone_export_kwh(slot) == 0.0


def test_pre_negative_drain_counts_as_EFFECTIVE_export() -> None:
    """`pre_negative_export` ships as a Fox ForceDischarge, exactly like
    `peak_export` (`optimizer._slot_fox_tuple` maps both to the same action).

    Counting only `peak_export` under-reported the export credit and turned every
    pre-negative drain into a phantom top-N "disparity" row in the 07:30 audit —
    the same species of phantom as the forgone bug, one function above it.
    """
    slot = _slot("pre_negative_export", "pre_negative_export", export_kwh=1.35)
    assert _effective_plan_export_kwh(slot) == pytest.approx(1.35)


def test_peak_export_counts_as_effective_export() -> None:
    slot = _slot("peak_export", "peak_export", export_kwh=0.8)
    assert _effective_plan_export_kwh(slot) == pytest.approx(0.8)


def test_pv_surplus_is_not_effective_battery_export() -> None:
    """PV surplus leaves via SelfUse — no discharge group, not battery export."""
    slot = _slot("solar_charge", "solar_charge", export_kwh=0.45)
    assert _effective_plan_export_kwh(slot) == 0.0


def test_downgraded_peak_export_IS_forgone() -> None:
    """The one real case: LP chose peak_export, robustness filter downgraded it."""
    slot = _slot("peak_export", dispatched_kind="standard", export_kwh=0.8)
    assert _forgone_export_kwh(slot) == pytest.approx(0.8)


def test_peak_export_that_shipped_is_not_forgone() -> None:
    slot = _slot("peak_export", dispatched_kind="peak_export", export_kwh=0.8)
    assert _forgone_export_kwh(slot) == 0.0


# --------------------------------------------------------------------------
# The DB helper carries the same rule
# --------------------------------------------------------------------------

def test_db_helper_ignores_pv_surplus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src import db
    from src.config import config

    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE lp_solution_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            slot_time_utc TEXT NOT NULL, export_kwh REAL
        );
        CREATE TABLE dispatch_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            slot_time_utc TEXT NOT NULL, lp_kind TEXT NOT NULL,
            dispatched_kind TEXT NOT NULL, export_price_p_kwh REAL, reason TEXT,
            UNIQUE(run_id, slot_time_utc)
        );
        """
    )
    day = dt.date(2026, 5, 14)   # fixed: a date.today() here flakes across UTC midnight
    noon = dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.timezone.utc)
    rows = [
        # sunny PV-surplus slots — must NOT be reported
        ("solar_charge", "solar_charge", 0.5),
        ("peak", "peak", 0.4),
        # the real one — LP wanted battery export, filter blocked it
        ("peak_export", "standard", 0.9),
    ]
    for i, (lp_kind, disp, kwh) in enumerate(rows):
        t = (noon + dt.timedelta(minutes=30 * i)).isoformat()
        conn.execute(
            "INSERT INTO lp_solution_snapshot (run_id, slot_time_utc, export_kwh) VALUES (1,?,?)",
            (t, kwh))
        conn.execute(
            "INSERT INTO dispatch_decisions "
            "(run_id, slot_time_utc, lp_kind, dispatched_kind, export_price_p_kwh) "
            "VALUES (1,?,?,?,10.0)", (t, lp_kind, disp))
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", str(db_path), raising=False)
    out = db.list_forgone_peak_export_for_day(day.isoformat())

    assert len(out) == 1, f"expected only the blocked peak_export slot, got {out}"
    assert out[0]["export_kwh"] == pytest.approx(0.9)
