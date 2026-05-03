"""Legionella DHW uplift in predict_passive_daikin_load.

Daikin Onecta firmware fires the thermal-shock cycle autonomously on a fixed
weekday/hour configured in the Onecta app. The LP can't command it; it can
only *predict* it so the passive-mode load forecast accounts for the pulse.
These tests pin that prediction:

  1. With DHW_LEGIONELLA_DAY=-1 (default) → no uplift, ever.
  2. On the matching local weekday + hour window → uplift on those slots.
  3. Outside the window (any other weekday, or earlier/later hour) → no uplift.
  4. 60-min duration spreads across exactly two consecutive 30-min slots.
  5. max_kwh_per_slot still caps the final value.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db, runtime_settings as rts
from src.physics import predict_passive_daikin_load


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()
    rts.clear_cache()
    yield
    # Clean up any settings these tests leak.
    for key in (
        "DHW_LEGIONELLA_DAY",
        "DHW_LEGIONELLA_HOUR_LOCAL",
        "DHW_LEGIONELLA_DURATION_MIN",
        "DHW_LEGIONELLA_TANK_TARGET_C",
    ):
        try:
            rts.delete_setting(key, actor="test_cleanup")
        except Exception:
            pass
    rts.clear_cache()


def _make_horizon(start_local: datetime, n: int, tz: ZoneInfo) -> list[datetime]:
    """Return n half-hourly UTC slot starts beginning at ``start_local`` (in ``tz``)."""
    base = start_local.replace(tzinfo=tz).astimezone(UTC)
    return [base + timedelta(minutes=30 * i) for i in range(n)]


def test_no_uplift_when_disabled() -> None:
    tz = ZoneInfo("Europe/London")
    # Wed 2026-05-06 12:00 → 6 slots covering 12:00–15:00 local
    starts = _make_horizon(datetime(2026, 5, 6, 12, 0), 6, tz)
    space, dhw = predict_passive_daikin_load(
        [10.0] * 6, [3.0] * 6, [3.0] * 6,
        slot_starts_utc=starts, tz=tz,
    )
    # Every slot is steady-state baseline only — identical because t_out + COP are flat.
    assert all(abs(d - dhw[0]) < 1e-9 for d in dhw), dhw


def test_uplift_on_matching_wednesday_13() -> None:
    tz = ZoneInfo("Europe/London")
    rts.set_setting("DHW_LEGIONELLA_DAY", 2, actor="test")  # Wed
    rts.set_setting("DHW_LEGIONELLA_HOUR_LOCAL", 13, actor="test")
    rts.set_setting("DHW_LEGIONELLA_DURATION_MIN", 60, actor="test")
    rts.set_setting("DHW_LEGIONELLA_TANK_TARGET_C", 60.0, actor="test")
    starts = _make_horizon(datetime(2026, 5, 6, 12, 0), 6, tz)  # Wed 12:00–14:30
    space, dhw = predict_passive_daikin_load(
        [10.0] * 6, [3.0] * 6, [3.0] * 6,
        slot_starts_utc=starts, tz=tz,
    )
    # Slots: 12:00, 12:30, 13:00, 13:30, 14:00, 14:30
    # Cycle window 13:00 → 14:00 → slots 2 and 3 get the uplift.
    base = dhw[0]
    assert dhw[0] == pytest.approx(base), "12:00 outside cycle"
    assert dhw[1] == pytest.approx(base), "12:30 outside cycle"
    assert dhw[2] > base + 1e-3, "13:00 must include uplift"
    assert dhw[3] > base + 1e-3, "13:30 must include uplift"
    assert dhw[4] == pytest.approx(base), "14:00 outside cycle (window ends)"
    assert dhw[5] == pytest.approx(base), "14:30 outside cycle"
    # Both uplifted slots get the same per-slot share.
    assert dhw[2] == pytest.approx(dhw[3])


def test_no_uplift_on_non_matching_day() -> None:
    tz = ZoneInfo("Europe/London")
    rts.set_setting("DHW_LEGIONELLA_DAY", 2, actor="test")  # Wed
    rts.set_setting("DHW_LEGIONELLA_HOUR_LOCAL", 13, actor="test")
    rts.set_setting("DHW_LEGIONELLA_DURATION_MIN", 60, actor="test")
    starts = _make_horizon(datetime(2026, 5, 7, 12, 0), 6, tz)  # Thu 12:00–14:30
    space, dhw = predict_passive_daikin_load(
        [10.0] * 6, [3.0] * 6, [3.0] * 6,
        slot_starts_utc=starts, tz=tz,
    )
    assert all(abs(d - dhw[0]) < 1e-9 for d in dhw), dhw


def test_uplift_magnitude_matches_water_thermal_capacity() -> None:
    """Σ(uplift_dhw - baseline) should equal litres × c_water × ΔT / 3.6e6 / COP."""
    tz = ZoneInfo("Europe/London")
    rts.set_setting("DHW_LEGIONELLA_DAY", 2, actor="test")
    rts.set_setting("DHW_LEGIONELLA_HOUR_LOCAL", 13, actor="test")
    rts.set_setting("DHW_LEGIONELLA_DURATION_MIN", 60, actor="test")
    rts.set_setting("DHW_LEGIONELLA_TANK_TARGET_C", 60.0, actor="test")
    starts = _make_horizon(datetime(2026, 5, 6, 12, 0), 6, tz)
    cop = 3.0
    _, dhw = predict_passive_daikin_load(
        [10.0] * 6, [cop] * 6, [cop] * 6,
        slot_starts_utc=starts, tz=tz,
    )
    _, baseline = predict_passive_daikin_load(
        [10.0] * 6, [cop] * 6, [cop] * 6,
        slot_starts_utc=None, tz=None,
    )
    delta = sum(dhw) - sum(baseline)
    from src.config import config
    expected_thermal_kwh = (
        float(config.DHW_TANK_LITRES)
        * float(config.DHW_WATER_CP)
        * (60.0 - float(config.DHW_TEMP_NORMAL_C))
        / 3.6e6
    )
    assert delta == pytest.approx(expected_thermal_kwh / cop, rel=1e-6)


def test_uplift_capped_by_max_kwh_per_slot() -> None:
    """When max_kwh_per_slot is small, the cap still binds (no LP-blowing huge value)."""
    tz = ZoneInfo("Europe/London")
    rts.set_setting("DHW_LEGIONELLA_DAY", 2, actor="test")
    rts.set_setting("DHW_LEGIONELLA_HOUR_LOCAL", 13, actor="test")
    rts.set_setting("DHW_LEGIONELLA_DURATION_MIN", 60, actor="test")
    starts = _make_horizon(datetime(2026, 5, 6, 12, 0), 6, tz)
    cap = 0.05
    _, dhw = predict_passive_daikin_load(
        [10.0] * 6, [3.0] * 6, [3.0] * 6,
        slot_starts_utc=starts, tz=tz,
        max_kwh_per_slot=cap,
    )
    assert max(dhw) <= cap + 1e-12


def test_backwards_compatible_without_slot_args() -> None:
    """db.py:1343 calls without slot_starts_utc/tz; uplift must be no-op there."""
    tz = ZoneInfo("Europe/London")
    rts.set_setting("DHW_LEGIONELLA_DAY", 2, actor="test")
    rts.set_setting("DHW_LEGIONELLA_HOUR_LOCAL", 13, actor="test")
    _, dhw_with = predict_passive_daikin_load(
        [10.0] * 6, [3.0] * 6, [3.0] * 6,
    )
    rts.delete_setting("DHW_LEGIONELLA_DAY", actor="test")
    rts.clear_cache()
    _, dhw_disabled = predict_passive_daikin_load(
        [10.0] * 6, [3.0] * 6, [3.0] * 6,
    )
    assert dhw_with == dhw_disabled, "no slot context → uplift must be skipped"
