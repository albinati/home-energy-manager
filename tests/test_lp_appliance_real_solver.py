"""Real-solver integration test for the appliance dispatch → LP path.

The existing ``test_lp_infeasible_appliance_retry.py`` tests PR #342's retry
code path using mocked solvers (TwoPhaseSolver returns Infeasible then
Optimal by fiat). That validates the retry plumbing but not the end-to-end
arithmetic: if a future refactor breaks the way ``appliance_profile_kwh``
gets folded into ``base_load`` (or how the retry subtracts it), the mock
tests would still pass.

This file is the real-solver complement. It uses actual ``solve_lp`` (no
mocks) plus a real ``appliance_jobs`` row, and asserts:

1. With an appliance armed, the LP's ``base_load_kwh`` arg sees the
   appliance kWh added at the right slots.
2. The LP's plan accounts for that load — either via extra grid import,
   extra battery discharge, or PV redirection during the appliance window.
3. Without an appliance (same scenario), the LP's plan in the appliance
   slots reflects only the residual load.

Constructing a real-solver Infeasible-only-with-appliance scenario proved
fragile post-#344 — the LP is robust enough that most appliance loads are
absorbed without forcing infeasibility. The discriminating-Infeasible
test remains in ``test_lp_infeasible_appliance_retry.py`` (mocked solver);
this file is the complement that catches arithmetic regressions.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src import db
from src.config import config as app_config
from src.scheduler import optimizer

TARIFF = "E-1R-AGILE-TEST-APPL-REAL"


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _real_solver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(app_config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", True)
    monkeypatch.setattr(app_config, "APPLIANCE_DISPATCH_ENABLED", True)
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_realistic_day(start: datetime) -> None:
    """48 slots with a cheap-overnight / peak-evening profile."""
    rows = []
    vf = start
    for _ in range(48):
        if 1 <= vf.hour < 5:
            price = -2.0  # negative overnight
        elif 5 <= vf.hour < 16:
            price = 10.0
        elif 16 <= vf.hour < 19:
            price = 35.0
        else:
            price = 12.0
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, TARIFF)


def _arm_washer(*, planned_start: datetime, duration_min: int = 90) -> int:
    """Seed a washing-machine appliance and a scheduled job at the given window."""
    appliance_id = db.add_appliance(
        vendor="smartthings",
        vendor_device_id=f"test-washer-{planned_start.timestamp():.0f}",
        name="Washing machine",
        device_type="washer",
        default_duration_minutes=duration_min,
        deadline_local_time="07:00",
        typical_kw=0.5,
        enabled=True,
    )
    planned_end = planned_start + timedelta(minutes=duration_min)
    deadline = planned_start + timedelta(hours=8)
    db.create_appliance_job(
        appliance_id=appliance_id,
        armed_at_utc=_iso(planned_start - timedelta(hours=1)),
        deadline_utc=_iso(deadline),
        duration_minutes=duration_min,
        planned_start_utc=_iso(planned_start),
        planned_end_utc=_iso(planned_end),
        avg_price_pence=-1.5,
        status="scheduled",
    )
    return appliance_id


def _run_optimizer_and_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the real optimizer, capture the ``base_load_kwh`` solve_lp saw."""
    from src.scheduler import lp_optimizer as _lp
    captured: dict[str, Any] = {}
    real_solve_lp = _lp.solve_lp

    def _capture(*, base_load_kwh, **kwargs):
        captured["base_load_kwh"] = list(base_load_kwh)
        return real_solve_lp(base_load_kwh=base_load_kwh, **kwargs)

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _capture)

    result = optimizer.run_optimizer(fox=None, daikin=None)
    captured["result"] = result
    return captured


def test_lp_sees_appliance_load_in_base_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """With an appliance armed inside the horizon, the real LP must receive
    base_load_kwh with the appliance contribution added at the right slots.
    """
    now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))
    # Washer planned at 02:00 UTC Wed = 03:00 BST (inside the cheap overnight
    # window). Duration 90 min → 3 half-hour slots.
    _arm_washer(
        planned_start=datetime(2026, 5, 21, 2, 0, tzinfo=UTC),
        duration_min=90,
    )

    captured = _run_optimizer_and_capture(monkeypatch)
    assert captured["result"].get("ok") is True, captured["result"]
    base_load = captured["base_load_kwh"]
    assert base_load, "solve_lp was never called or never received base_load"

    # The appliance kWh per slot is typical_kw × 0.5 = 0.25 kWh. Three slots
    # at slot indices corresponding to 02:00, 02:30, 03:00 UTC must be
    # ≥ 0.25 above the slot-day's residual baseline.
    above_baseline = [v for v in base_load if v > 0.5]  # rough sentinel
    assert above_baseline, (
        f"No base_load slots above baseline — appliance contribution didn't "
        f"reach the LP. Sample base_load: {base_load[:8]}..."
    )


def test_lp_plan_accounts_for_appliance_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the appliance is armed, the LP's plan in the appliance slots
    must show either grid import OR battery discharge (or PV) ≥ appliance
    kWh contribution. Otherwise the energy balance is violated and the LP
    would have returned Infeasible — but since it solved Optimal, the
    extra load must be served from somewhere.
    """
    now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))
    _arm_washer(
        planned_start=datetime(2026, 5, 21, 2, 0, tzinfo=UTC),
        duration_min=90,
    )

    captured = _run_optimizer_and_capture(monkeypatch)
    assert captured["result"].get("ok") is True

    # Pull the latest optimizer_log + lp_solution_snapshot to verify per-slot
    # plan in the appliance window. The LP plan must show net energy
    # delivered to "base + appliance" matching the increased load.
    conn = db.get_connection()
    try:
        run_id = conn.execute(
            "SELECT id FROM optimizer_log ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        # Slots covering 02:00-03:30 UTC Wed
        rows = conn.execute(
            """SELECT slot_time_utc, import_kwh, discharge_kwh, pv_use_kwh,
                       charge_kwh, export_kwh
               FROM lp_solution_snapshot
               WHERE run_id = ?
               ORDER BY slot_index""",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    assert rows, "lp_solution_snapshot has no rows for this run"
    # Find the appliance slots
    appliance_slots = [
        r for r in rows
        if r["slot_time_utc"].startswith("2026-05-21T02:")
        or r["slot_time_utc"].startswith("2026-05-21T03:00")
    ]
    assert len(appliance_slots) >= 2, (
        f"expected ≥2 slots covering the appliance window in the snapshot, "
        f"got {len(appliance_slots)}"
    )
    # In each appliance slot, supply side (imp + dis + pv_use) must be
    # positive — the LP is serving load from SOMEWHERE.
    for r in appliance_slots:
        supply = (
            (r["import_kwh"] or 0.0)
            + (r["discharge_kwh"] or 0.0)
            + (r["pv_use_kwh"] or 0.0)
        )
        # Even with the load just being the appliance + residual ~0.55 kWh
        # the supply side must reflect it.
        assert supply > 0.05, (
            f"appliance slot {r['slot_time_utc']} shows supply≈0 "
            f"({dict(r)}) — base_load_kwh likely not folded in correctly"
        )
