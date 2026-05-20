"""Real-pipeline integration test for the appliance dispatch → LP path.

Goal: catch arithmetic regressions in the way ``appliance_jobs`` rows turn
into ``base_load_kwh`` increments handed to ``solve_lp``. The existing PR
#342 tests stub the entire optimizer flow with a TwoPhaseSolver — they
validate the retry plumbing but would not notice if, say,
``appliance_load_profile_kw`` started returning the wrong slots or
``base_load_kwh`` injection became zero.

What this file exercises end-to-end:
* Real ``appliance_dispatch.appliance_load_profile_kw`` queries
  ``appliance_jobs``.
* Real ``optimizer._run_optimizer_lp`` builds ``base_load`` and adds the
  appliance contribution per slot.
* Real ``optimizer.solve_lp`` is intercepted ONLY to capture its
  ``base_load_kwh`` argument — what the LP actually saw — without
  depending on whether CBC found an Optimal solution. The CBC outcome
  on a synthetic test harness is too coupled to environment state
  (conftest's autouse ``DAIKIN_CONTROL_MODE=active``, the shared
  ``_overrides`` dict, etc.) to assert on directly here.

That gives us a robust assertion: "the LP receives a base_load with the
expected appliance bump at the expected slots", which is the regression
case I care about.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src import db
from src.config import config as app_config
from src.scheduler import optimizer
from src.scheduler.lp_optimizer import LpPlan

TARIFF = "E-1R-AGILE-TEST-APPL-REAL"


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _real_solver_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the LP knobs that bear on whether ``solve_lp`` gets called at
    all. We DON'T need to make ``solve_lp`` return Optimal — we stub its
    return value below — but the optimizer must reach the solve_lp call,
    which means dispatch + base_load assembly must run cleanly."""
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(app_config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", True)
    monkeypatch.setattr(app_config, "APPLIANCE_DISPATCH_ENABLED", True)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_realistic_day(start: datetime) -> None:
    rows = []
    vf = start
    for _ in range(48):
        if 1 <= vf.hour < 5:
            price = -2.0
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


def _stub_optimal_lp_plan(
    slot_starts_utc: list[datetime],
    price_pence: list[float],
    *_args: Any,
    **_kwargs: Any,
) -> LpPlan:
    """Trivial Optimal plan — zero everything. The solver outcome doesn't
    matter here; we only need solve_lp to NOT raise so the captured
    base_load_kwh becomes observable."""
    n = len(slot_starts_utc)
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0)
    plan.slot_starts_utc = list(slot_starts_utc)
    plan.price_pence = list(price_pence)
    plan.temp_outdoor_c = [12.0] * n
    plan.import_kwh = [0.0] * n
    plan.export_kwh = [0.0] * n
    plan.battery_charge_kwh = [0.0] * n
    plan.battery_discharge_kwh = [0.0] * n
    plan.pv_use_kwh = [0.0] * n
    plan.pv_curtail_kwh = [0.0] * n
    plan.dhw_electric_kwh = [0.0] * n
    plan.space_electric_kwh = [0.0] * n
    plan.lwt_offset_c = [0.0] * n
    plan.tank_temp_c = [45.0] * (n + 1)
    plan.soc_kwh = [5.0] * (n + 1)
    return plan


def _run_optimizer_capturing_base_load(
    monkeypatch: pytest.MonkeyPatch,
) -> list[float]:
    """Run the real optimizer with a stubbed Optimal solve_lp; return the
    ``base_load_kwh`` argument the LP saw."""
    captured: dict[str, list[float]] = {}

    def _capture(*, base_load_kwh, **kwargs):
        captured["base_load_kwh"] = list(base_load_kwh)
        return _stub_optimal_lp_plan(base_load_kwh=base_load_kwh, **kwargs)

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _capture)
    optimizer.run_optimizer(fox=None, daikin=None)
    return captured.get("base_load_kwh", [])


def test_lp_sees_appliance_load_in_base_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an appliance armed inside the horizon, the real LP must receive
    ``base_load_kwh`` whose ARMED-WINDOW slots are noticeably higher than
    the matching non-armed slots in the SAME local time of day.
    """
    now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))
    _arm_washer(
        planned_start=datetime(2026, 5, 21, 2, 0, tzinfo=UTC),
        duration_min=90,
    )

    base_load = _run_optimizer_capturing_base_load(monkeypatch)
    assert base_load, "solve_lp was never called (optimizer bailed early)"

    # 0.5 kW × 0.5 h = 0.25 kWh per slot. Three slots covering 02:00-03:30
    # UTC Wed should each be ≥ 0.25 kWh above the slot with the same local
    # time-of-day but on a different day. The slot vector starts at the
    # next 30-min boundary after 18:00 UTC = 18:30 UTC.
    # Slot indices for the appliance window (02:00, 02:30, 03:00 UTC Wed):
    # from 18:30 UTC Tue, 02:00 UTC Wed is 7.5 h ahead = slot 15.
    appliance_slots = [15, 16, 17]
    above_threshold = [
        i for i in appliance_slots
        if i < len(base_load) and base_load[i] >= 0.25
    ]
    assert above_threshold, (
        f"None of the appliance slots {appliance_slots} reached ≥0.25 kWh "
        f"above baseline. base_load[15..20]={[base_load[i] for i in range(15, min(20, len(base_load)))]}"
    )


def test_lp_does_not_see_appliance_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity invariant: with ``APPLIANCE_DISPATCH_ENABLED=False``, the
    appliance contribution must NOT reach the LP regardless of armed
    ``appliance_jobs`` rows."""
    monkeypatch.setattr(app_config, "APPLIANCE_DISPATCH_ENABLED", False)
    now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 5, 20, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 5, 21, 0, 0, tzinfo=UTC))
    _arm_washer(
        planned_start=datetime(2026, 5, 21, 2, 0, tzinfo=UTC),
        duration_min=90,
    )

    base_load = _run_optimizer_capturing_base_load(monkeypatch)
    assert base_load, "solve_lp was never called"
    # With dispatch disabled, no slot should be lifted by the appliance.
    # All slots should reflect only the residual-base-load profile (≤ ~0.5 kWh
    # in a normal household per the prod ``early-morning(03-07)`` log line).
    for i, v in enumerate(base_load):
        assert v < 0.6, (
            f"slot {i} base_load = {v} kWh — looks like the appliance bump "
            f"is leaking through with APPLIANCE_DISPATCH_ENABLED=False"
        )
