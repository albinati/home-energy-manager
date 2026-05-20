"""Gap bridge removal (issue #28): dispatch must mirror PuLP slot kinds, not fill gaps."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.scheduler.lp_dispatch import lp_dispatch_slots_for_hardware, lp_plan_to_slots
from src.scheduler.lp_optimizer import LpPlan


def _four_slot_plan_with_standard_gap() -> LpPlan:
    """LP chose grid charge only in slot 0 and 3; slots 1–2 stay idle (standard).

    The old ``apply_lp_dispatch_gap_bridge`` incorrectly promoted those standard slots to
    ``cheap`` so Fox would ForceCharge through the gap, overriding the MILP.
    """
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    starts = [t0 + i * timedelta(minutes=30) for i in range(4)]
    # Peak threshold 30p — mid slots priced 15p stay "standard" when LP does not charge.
    price = [10.0, 15.0, 15.0, 10.0]
    chg = [0.15, 0.0, 0.0, 0.15]
    imp = [0.2, 0.0, 0.0, 0.2]
    return LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=starts,
        price_pence=price,
        import_kwh=imp,
        export_kwh=[0.0, 0.0, 0.0, 0.0],
        battery_charge_kwh=chg,
        battery_discharge_kwh=[0.0, 0.0, 0.0, 0.0],
        pv_use_kwh=[0.0, 0.0, 0.0, 0.0],
        pv_curtail_kwh=[0.0, 0.0, 0.0, 0.0],
        dhw_electric_kwh=[0.0, 0.0, 0.0, 0.0],
        space_electric_kwh=[0.0, 0.0, 0.0, 0.0],
        soc_kwh=[5.0, 6.0, 6.0, 6.0, 7.0],
        peak_threshold_pence=30.0,
    )


def test_lp_dispatch_slots_match_lp_plan_no_gap_bridge_promotion() -> None:
    """The original intent: gap slots between cheap slots must NOT be
    promoted to ``cheap`` (which would ForceCharge through the gap and
    override the MILP). Slots 1+2 are between cheap slot 0 and cheap
    slot 3 — they must end up as anything BUT cheap.

    Updated 2026-05-20 (#323): with the default ``DHW_SHOWER_SCHEDULE``
    of ``19:00-22:00`` and the synthetic plan running 01:00-03:00 BST
    (post-shower idle window), the second-pass overnight-tank-idle
    classifier now correctly marks slots 1+2 as ``tank_idle_overnight``
    rather than leaving them ``standard`` — same gap-bridge guarantee
    (not promoted to cheap), more accurate kind. The dispatch surface
    must mirror the raw classifier output exactly."""
    plan = _four_slot_plan_with_standard_gap()
    raw = lp_plan_to_slots(plan)
    kinds = [s.kind for s in raw]
    assert kinds[0] == "cheap" and kinds[3] == "cheap"
    # The non-cheap-promotion guarantee: slots 1+2 are NOT cheap.
    assert kinds[1] != "cheap"
    assert kinds[2] != "cheap"

    dispatched = lp_dispatch_slots_for_hardware(plan)
    assert [s.kind for s in dispatched] == [s.kind for s in raw]
