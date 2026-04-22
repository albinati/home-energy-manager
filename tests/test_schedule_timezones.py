"""Assert that LP plan UTC slot boundaries map to the correct local (Europe/London)
hour/minute when dispatched to the Fox inverter, and remain UTC "Z" strings in the
Daikin action_schedule. Regression guard for the TZ-mapping issue caught on 2026-04-22
where a user expected 12:00–12:30 BST SelfUse but saw 13:00–13:30.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config
from src.scheduler.lp_dispatch import daikin_dispatch_preview, lp_plan_to_slots
from src.scheduler.lp_optimizer import LpPlan
from src.scheduler.optimizer import HalfHourSlot, _merge_fox_groups
from src.weather import HourlyForecast


@pytest.fixture(autouse=True)
def _london_tz(monkeypatch):
    """Pin BULLETPROOF_TIMEZONE so Fox dispatch always uses Europe/London."""
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")


def _cheap_slot(start_utc: datetime) -> HalfHourSlot:
    return HalfHourSlot(
        start_utc=start_utc,
        end_utc=start_utc + timedelta(minutes=30),
        price_pence=-1.3,
        kind="negative",
        lp_grid_import_w=6000,
        target_soc_pct=100,
    )


def test_fox_group_uses_bst_local_hour_during_summer():
    """In April (BST, UTC+1), a slot at 11:30–12:00 UTC should produce a Fox group
    with start_hour=12 / end_hour=12 (end_minute=59 per minute-rollback convention)."""
    tz = ZoneInfo("Europe/London")
    # Sanity: confirm BST is active on 2026-04-22
    assert datetime(2026, 4, 22, 12, 0, tzinfo=tz).utcoffset() == timedelta(hours=1)

    slot = _cheap_slot(datetime(2026, 4, 22, 11, 30, tzinfo=UTC))
    groups = _merge_fox_groups([slot])
    assert len(groups) == 1
    g = groups[0]
    assert g.start_hour == 12, f"expected start_hour=12 (BST), got {g.start_hour}"
    assert g.start_minute == 30
    # end_utc = 12:00 UTC → 13:00 BST, minute rolled back to 12:59 per convention
    assert g.end_hour == 12, f"expected end_hour=12 (minute rolled back), got {g.end_hour}"
    assert g.end_minute == 59


def test_fox_group_uses_gmt_hour_during_winter():
    """In January (GMT, UTC+0), local hour == UTC hour."""
    tz = ZoneInfo("Europe/London")
    assert datetime(2026, 1, 15, 12, 0, tzinfo=tz).utcoffset() == timedelta(0)

    slot = _cheap_slot(datetime(2026, 1, 15, 11, 30, tzinfo=UTC))
    groups = _merge_fox_groups([slot])
    assert len(groups) == 1
    g = groups[0]
    assert g.start_hour == 11, f"GMT: expected start_hour=11, got {g.start_hour}"
    assert g.start_minute == 30


def test_todays_negative_window_fox_groups_land_on_bst_local_times():
    """Regression for 2026-04-22: the negative Agile run at 11:30–14:30 UTC
    must produce Fox ForceCharge groups spanning 12:30–15:30 **BST local** — i.e.
    what the user sees in the Fox app."""
    base_utc = datetime(2026, 4, 22, 11, 30, tzinfo=UTC)  # first negative slot start
    slots = [_cheap_slot(base_utc + timedelta(minutes=30 * i)) for i in range(6)]
    groups = _merge_fox_groups(slots)
    # Single merged ForceCharge group
    assert len(groups) == 1
    g = groups[0]
    assert g.work_mode == "ForceCharge"
    assert (g.start_hour, g.start_minute) == (12, 30), (
        f"BST start expected 12:30, got {g.start_hour:02d}:{g.start_minute:02d}"
    )
    # 14:30 UTC end → 15:30 BST. No minute-rollback (rollback only fires when minute==0).
    assert (g.end_hour, g.end_minute) == (15, 30), (
        f"BST end expected 15:30, got {g.end_hour:02d}:{g.end_minute:02d}"
    )


def test_daikin_action_schedule_keeps_utc_z_suffix():
    """Daikin action rows are stored/dispatched in UTC ISO with Z suffix, independent of
    BULLETPROOF_TIMEZONE. The write path (``write_daikin_from_lp_plan``) persists what
    ``daikin_dispatch_preview`` returns, so asserting the preview is sufficient."""
    t_start = datetime(2026, 4, 22, 13, 30, tzinfo=UTC)  # negative slot
    plan = LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=-50.0,
        slot_starts_utc=[t_start, t_start + timedelta(minutes=30)],
        price_pence=[-1.3, -1.3],
        import_kwh=[3.0, 3.0],
        export_kwh=[0.0, 0.0],
        battery_charge_kwh=[3.0, 3.0],
        battery_discharge_kwh=[0.0, 0.0],
        pv_use_kwh=[0.5, 0.5],
        pv_curtail_kwh=[0.0, 0.0],
        dhw_electric_kwh=[1.0, 0.0],
        space_electric_kwh=[0.0, 0.5],
        lwt_offset_c=[5.0, 5.0],
        tank_temp_c=[45.0, 55.0, 60.0],
        indoor_temp_c=[21.0, 21.0, 21.0],
        soc_kwh=[5.0, 8.0, 10.0],
        temp_outdoor_c=[10.0, 10.0],
        peak_threshold_pence=30.0,
        cheap_threshold_pence=5.0,
    )
    # Minimal hourly forecast covering the slots
    forecast = [
        HourlyForecast(
            time_utc=datetime(2026, 4, 22, 13, 0, tzinfo=UTC),
            temperature_c=10.0,
            cloud_cover_pct=40.0,
            shortwave_radiation_wm2=400.0,
            estimated_pv_kw=0.5,
            heating_demand_factor=1.0,
        ),
        HourlyForecast(
            time_utc=datetime(2026, 4, 22, 14, 0, tzinfo=UTC),
            temperature_c=10.0,
            cloud_cover_pct=40.0,
            shortwave_radiation_wm2=400.0,
            estimated_pv_kw=0.5,
            heating_demand_factor=1.0,
        ),
    ]
    pairs = daikin_dispatch_preview(plan, forecast)
    assert pairs, "expected at least one action pair for a negative window"
    _, action = pairs[0]
    assert action["start_time"].endswith("Z"), f"{action['start_time']} must end with Z"
    assert action["end_time"].endswith("Z"), f"{action['end_time']} must end with Z"
    # Start must be the exact UTC instant from plan (13:30:00Z), not a local conversion
    assert action["start_time"] == "2026-04-22T13:30:00Z", (
        f"start_time should be UTC 13:30:00Z, got {action['start_time']}"
    )


def test_lp_plan_to_slots_preserves_utc_on_slots():
    """Slots returned to the dispatch layer must keep their LP UTC timestamps
    so downstream consumers (Daikin writes, logging, merging) don't mis-convert."""
    base = datetime(2026, 4, 22, 11, 30, tzinfo=UTC)
    plan = LpPlan(
        ok=True,
        status="Optimal",
        objective_pence=0.0,
        slot_starts_utc=[base, base + timedelta(minutes=30)],
        price_pence=[-1.3, -1.3],
        import_kwh=[3.0, 3.0],
        export_kwh=[0.0, 0.0],
        battery_charge_kwh=[3.0, 3.0],
        battery_discharge_kwh=[0.0, 0.0],
        pv_use_kwh=[0.0, 0.0],
        pv_curtail_kwh=[0.0, 0.0],
        dhw_electric_kwh=[0.0, 0.0],
        space_electric_kwh=[0.0, 0.0],
        soc_kwh=[5.0, 8.0, 10.0],
        peak_threshold_pence=30.0,
    )
    slots = lp_plan_to_slots(plan)
    assert len(slots) == 2
    assert slots[0].start_utc == base
    assert slots[0].start_utc.tzinfo is UTC
    assert slots[1].start_utc == base + timedelta(minutes=30)
    assert slots[1].end_utc == base + timedelta(hours=1)
