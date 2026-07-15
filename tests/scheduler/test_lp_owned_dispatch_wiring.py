"""write_daikin_from_lp_plan routes an LP-owned plan to the tank translator (#714).

The pure compression is tested in tests/dhw/test_dispatch.py. This checks the wiring:
the branch is gated on the PLAN flag (not config), it writes lp_owned rows and does NOT
call dhw_policy, and it is mutually exclusive with the K1 branch.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db as _db
from src.config import config
from src.scheduler.lp_dispatch import write_daikin_from_lp_plan
from src.scheduler.lp_optimizer import LpPlan


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "disp.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(_db, "_db_path", lambda: path)
    _db.init_db()
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "normal")
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True)
    return path


def _plan(*, lp_owned: bool, n=24, first_hour=12):
    base = datetime(2026, 7, 8, first_hour, 0, tzinfo=UTC)
    starts = [base + i * timedelta(minutes=30) for i in range(n)]
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0)
    plan.slot_starts_utc = starts
    plan.price_pence = [10.0] * n
    # Warm to 45 for the evening, then let it fall — a real two-row shape.
    plan.tank_temp_c = [37.0] * (n + 1)
    for i in range(n + 1):
        st_hour = (first_hour + i * 0.5)
        plan.tank_temp_c[i] = 45.0 if 19 <= st_hour % 24 < 22 else 40.0
    plan.dhw_electric_kwh = [0.5 if 18 <= (first_hour + i * 0.5) % 24 < 19 else 0.0
                             for i in range(n)]
    plan.dhw_lp_owned = lp_owned
    return plan


def test_an_lp_owned_plan_writes_lp_owned_rows_and_skips_dhw_policy(tmp_db, monkeypatch):
    called = {"dhw_policy": False}

    import src.dhw_policy as dhw_policy

    def _boom(*a, **k):
        called["dhw_policy"] = True
        return 0

    monkeypatch.setattr(dhw_policy, "write_daily_tank_schedule", _boom)

    plan = _plan(lp_owned=True)
    n = write_daikin_from_lp_plan("2026-07-08", plan, [])
    assert n > 0
    assert called["dhw_policy"] is False, "LP-owned must not fall through to dhw_policy"

    rows = _db.get_actions_for_plan_date("2026-07-08", device="daikin")
    tank_rows = [r for r in rows if (r.get("action_type") or "").startswith("tank_")]
    assert tank_rows
    import json
    assert all((r["params"] if isinstance(r["params"], dict) else json.loads(r["params"] or "{}")).get("lp_owned") for r in tank_rows)
    # Few rows, not one per slot.
    assert len(tank_rows) <= 6


def test_a_pinned_plan_still_uses_dhw_policy(tmp_db, monkeypatch):
    """Flag off on the plan → the K1 branch runs, exactly as before."""
    called = {"dhw_policy": 0}
    import src.dhw_policy as dhw_policy

    orig = dhw_policy.write_daily_tank_schedule

    def _count(*a, **k):
        called["dhw_policy"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(dhw_policy, "write_daily_tank_schedule", _count)

    plan = _plan(lp_owned=False)
    write_daikin_from_lp_plan("2026-07-08", plan, [])
    assert called["dhw_policy"] > 0, "pinned plan must go through dhw_policy"

    rows = _db.get_actions_for_plan_date("2026-07-08", device="daikin")
    tank_rows = [r for r in rows if (r.get("action_type") or "").startswith("tank_")]
    import json
    assert not any((r["params"] if isinstance(r["params"], dict) else json.loads(r["params"] or "{}")).get("lp_owned") for r in tank_rows)
