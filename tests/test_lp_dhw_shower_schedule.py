"""PR 4 of plan: time-of-day-aware DHW shower schedule.

Tests:
1. ``_parse_shower_schedule`` handles single, multi, malformed inputs.
2. ``_window_set_slot_mask`` flags slots inside any window correctly.
3. ``_resolve_active_shower_windows`` picks guests vs default schedule based
   on preset, and falls back to the legacy ``LP_SHOWER_MORNING_LOCAL`` /
   ``LP_SHOWER_EVENING_LOCAL`` scalars when ``DHW_SHOWER_SCHEDULE`` is empty.
4. **LP integration**: synthetic 48-slot day with all-positive prices and the
   default ``"19:00-22:00"`` schedule — LP solves cleanly; the terminal floor
   relaxation lets the LP let the tank cool overnight when the horizon ends
   outside a shower window.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.scheduler.lp_optimizer import (
    _parse_shower_schedule,
    _resolve_active_shower_windows,
    _window_set_slot_mask,
)


# --------------------------------------------------------------------------
# Schedule parser
# --------------------------------------------------------------------------

def test_parse_single_window() -> None:
    assert _parse_shower_schedule("19:00-22:00") == [(19 * 60, 22 * 60)]


def test_parse_multi_window_comma_separated() -> None:
    assert _parse_shower_schedule("07:00-09:00,19:00-22:00") == [
        (7 * 60, 9 * 60),
        (19 * 60, 22 * 60),
    ]


def test_parse_with_spaces_and_zero_padding() -> None:
    assert _parse_shower_schedule(" 7:30-08:30 , 19:00-22:00 ") == [
        (7 * 60 + 30, 8 * 60 + 30),
        (19 * 60, 22 * 60),
    ]


def test_parse_skips_malformed_silently() -> None:
    """Garbage entries are dropped, well-formed ones survive."""
    assert _parse_shower_schedule("19:00-22:00,not-a-window,07:00-08:00") == [
        (19 * 60, 22 * 60),
        (7 * 60, 8 * 60),
    ]


def test_parse_rejects_inverted_window() -> None:
    """End must be after start. Inverted ranges are dropped."""
    assert _parse_shower_schedule("22:00-19:00") == []


def test_parse_empty_input() -> None:
    assert _parse_shower_schedule("") == []
    assert _parse_shower_schedule("   ") == []


# --------------------------------------------------------------------------
# Slot mask
# --------------------------------------------------------------------------

def test_window_set_slot_mask_single_window() -> None:
    """Slots whose midpoint is inside the window are True; others False."""
    base = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)  # 19:00 BST
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    tz = ZoneInfo("Europe/London")
    mask = _window_set_slot_mask(slots, tz, windows=[(19 * 60, 22 * 60)])
    # 19:00 BST = slot 0; 22:00 BST = slot 6 (start). Slot 0..5 inside; slot 6, 7 outside.
    assert mask[0] is True   # 19:00-19:30 (mid 19:15)
    assert mask[5] is True   # 21:30-22:00 (mid 21:45)
    assert mask[6] is False  # 22:00-22:30 (mid 22:15)
    assert mask[7] is False


def test_window_set_slot_mask_empty_windows() -> None:
    """Empty windows list → all-False mask, regardless of slot count."""
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    slots = [base + timedelta(minutes=30 * i) for i in range(48)]
    mask = _window_set_slot_mask(slots, ZoneInfo("Europe/London"), windows=[])
    assert all(v is False for v in mask)


def test_window_set_slot_mask_handles_dst_correctly() -> None:
    """Slots are tagged on local time, not UTC. In summer 19:00 local = 18:00 UTC."""
    # June 1 = BST (UTC+1)
    base_utc = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    slots = [base_utc + timedelta(minutes=30 * i) for i in range(2)]
    mask = _window_set_slot_mask(slots, ZoneInfo("Europe/London"), windows=[(19 * 60, 22 * 60)])
    assert mask[0] is True  # 19:00 local
    assert mask[1] is True  # 19:30 local


# --------------------------------------------------------------------------
# Schedule resolution (config + back-compat)
# --------------------------------------------------------------------------

def test_resolve_picks_default_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default config DHW_SHOWER_SCHEDULE='19:00-22:00' returns one window."""
    from src.config import config as app_config
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    assert _resolve_active_shower_windows(guests_preset=False) == [(19 * 60, 22 * 60)]


def test_resolve_picks_guests_schedule_when_preset_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guests preset → DHW_SHOWER_SCHEDULE_GUESTS supersedes the default."""
    from src.config import config as app_config
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(
        app_config, "DHW_SHOWER_SCHEDULE_GUESTS", "07:00-09:00,19:00-22:00", raising=False
    )
    assert _resolve_active_shower_windows(guests_preset=True) == [
        (7 * 60, 9 * 60),
        (19 * 60, 22 * 60),
    ]


def test_resolve_falls_back_to_legacy_scalars_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty DHW_SHOWER_SCHEDULE → derive from LP_SHOWER_MORNING_LOCAL/EVENING_LOCAL.
    Backward-compat for installs that haven't set the new env."""
    from src.config import config as app_config
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "", raising=False)
    monkeypatch.setattr(app_config, "LP_SHOWER_MORNING_LOCAL", "07:30", raising=False)
    monkeypatch.setattr(app_config, "LP_SHOWER_EVENING_LOCAL", "20:00", raising=False)
    monkeypatch.setattr(app_config, "LP_SHOWER_WINDOW_MINUTES", 60, raising=False)
    out = _resolve_active_shower_windows(guests_preset=False)
    # 07:30 ± 30 min = 07:00–08:00; 20:00 ± 30 min = 19:30–20:30.
    assert (7 * 60, 8 * 60) in out
    assert (19 * 60 + 30, 20 * 60 + 30) in out


def test_resolve_returns_empty_when_all_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both new schedule AND legacy scalars empty → no shower constraint
    (matches prior code path, LP applies no DHW floor)."""
    from src.config import config as app_config
    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "", raising=False)
    monkeypatch.setattr(app_config, "LP_SHOWER_MORNING_LOCAL", "", raising=False)
    monkeypatch.setattr(app_config, "LP_SHOWER_EVENING_LOCAL", "", raising=False)
    assert _resolve_active_shower_windows(guests_preset=False) == []


# --------------------------------------------------------------------------
# LP integration
# --------------------------------------------------------------------------

def test_lp_solves_cleanly_with_default_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default schedule + 48-slot horizon ending outside any shower window:
    LP solves; terminal floor relaxes to DHW_TEMP_MIN_FLOOR_C (default 30)
    so no infeasibility from a forced overnight reheat."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(app_config, "DHW_TEMP_MIN_FLOOR_C", 30.0, raising=False)
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "normal", raising=False)
    # Active control mode so the LP actually applies the DHW floor.
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)

    # Start at 12:00 BST, 48 slots = 24 h → ends 12:00 next day BST. Last slot
    # is far from any 19:00–22:00 window, so the relaxed terminal floor applies.
    base_utc = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
    n = 48
    slots = [base_utc + timedelta(minutes=30 * i) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[20.0] * n,  # uniform 20p — no negative slots
        base_load_kwh=[0.3] * n,
        weather=WeatherLpSeries(
            slot_starts_utc=slots,
            temperature_outdoor_c=[18.0] * n,
            shortwave_radiation_wm2=[300.0] * n,
            cloud_cover_pct=[40.0] * n,
            pv_kwh_per_slot=[1.0] * n,
            cop_space=[3.5] * n,
            cop_dhw=[3.0] * n,
        ),
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=45.0, indoor_temp_c=21.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status
    # Terminal tank temperature must satisfy the relaxed floor (30 °C).
    # The previous code path enforced 43 °C here — would have forced expensive
    # reheat at the 11:30-next-day slot. With the relaxation, tank can land
    # below 43 °C if the LP finds a cheaper plan.
    assert plan.tank_temp_c[-1] >= 30.0 - 0.01, plan.tank_temp_c[-1]


def test_lp_enforces_tight_floor_when_horizon_ends_in_shower_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the last slot lands inside a shower window, the tight 43°C-ish
    terminal floor still applies (we don't want to start a shower with a
    cold tank). This is the conservative half of the relaxation."""
    from src.config import config as app_config
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    monkeypatch.setattr(app_config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    monkeypatch.setattr(app_config, "DHW_TEMP_MIN_FLOOR_C", 30.0, raising=False)
    monkeypatch.setattr(app_config, "TARGET_DHW_TEMP_MIN_NORMAL_C", 45.0, raising=False)
    monkeypatch.setattr(app_config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)

    # Start at 16:00 BST, 8 slots × 30 min = 4 h → ends 20:00 BST (inside window).
    base_utc = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    n = 8
    slots = [base_utc + timedelta(minutes=30 * i) for i in range(n)]
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[20.0] * n,
        base_load_kwh=[0.3] * n,
        weather=WeatherLpSeries(
            slot_starts_utc=slots,
            temperature_outdoor_c=[18.0] * n,
            shortwave_radiation_wm2=[300.0] * n,
            cloud_cover_pct=[40.0] * n,
            pv_kwh_per_slot=[1.0] * n,
            cop_space=[3.5] * n,
            cop_dhw=[3.0] * n,
        ),
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=45.0, indoor_temp_c=21.0),
        tz=ZoneInfo("Europe/London"),
    )
    assert plan.ok, plan.status
    # Last slot (slot 7) is 19:30–20:00 BST → midpoint 19:45 BST → inside window.
    # Tight floor (45 - 2 = 43 °C) applies, NOT the relaxed 30 °C floor.
    assert plan.tank_temp_c[-1] >= 42.99, plan.tank_temp_c[-1]
