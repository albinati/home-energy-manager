"""Unit tests for the explicit shower demand model (PR B).

Pure functions in ``src.dhw_demand`` — physics math and mode-aware
counts. The LP integration is exercised separately in
``tests/test_lp_shower_demand_model.py``.
"""
from __future__ import annotations

import pytest

from src import dhw_demand as dhw
from src.config import config
from src.presets import OperationPreset


@pytest.fixture(autouse=True)
def _reset_overrides():
    """Clear the class-level config overrides between tests so a
    monkeypatch in one test doesn't bleed into the next."""
    type(config)._overrides.clear()
    yield
    type(config)._overrides.clear()


# ---------------------------------------------------------------------------
# Shower count by mode
# ---------------------------------------------------------------------------


def test_normal_mode_uses_evening_count_only():
    """Normal mode: 4 evening showers (default), 1 morning reserve."""
    assert dhw.total_evening_showers(OperationPreset.NORMAL) == 4
    assert dhw.total_morning_showers(OperationPreset.NORMAL) == 1


def test_guests_mode_adds_per_guest_extras(monkeypatch):
    """Guests mode adds per-guest evening + morning extras."""
    monkeypatch.setattr(config, "DHW_GUEST_COUNT", 2, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST", 1, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST", 1, raising=False)
    # Normal evening (4) + 2 guests × 1 extra = 6
    assert dhw.total_evening_showers(OperationPreset.GUESTS) == 6
    # Normal reserve (1) + 2 guests × 1 extra = 3
    assert dhw.total_morning_showers(OperationPreset.GUESTS) == 3


def test_guests_mode_scales_with_visitor_count(monkeypatch):
    """A 3-visitor household sees more demand than a 2-visitor one."""
    monkeypatch.setattr(config, "DHW_GUEST_COUNT", 3, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST", 1, raising=False)
    assert dhw.total_evening_showers(OperationPreset.GUESTS) == 4 + 3


def test_vacation_mode_zero_showers():
    """Vacation: zero showers in both windows (tank off)."""
    assert dhw.total_evening_showers(OperationPreset.VACATION) == 0
    assert dhw.total_morning_showers(OperationPreset.VACATION) == 0


# ---------------------------------------------------------------------------
# Mixer math
# ---------------------------------------------------------------------------


def test_mix_litres_per_shower_uses_duration_and_flow():
    """5 min × 9 L/min = 45 L mix per shower."""
    assert dhw.mix_litres_per_shower() == pytest.approx(45.0)


def test_hot_litres_per_shower_increases_as_tank_cools(monkeypatch):
    """As the tank cools toward the mixer temp, the hot fraction climbs.
    At tank=mixer the formula returns the full mix-out litres (no dilution
    headroom). At tank=hot (e.g. 60) only ~half the mix-out is hot."""
    # Defaults: mixer=38, cold=10, duration=5, flow=9 → mix = 45 L
    hot_at_60 = dhw.hot_litres_per_shower(60.0)
    hot_at_45 = dhw.hot_litres_per_shower(45.0)
    hot_at_42 = dhw.hot_litres_per_shower(42.0)
    assert hot_at_60 < hot_at_45 < hot_at_42


def test_hot_litres_per_shower_handles_tank_at_cold():
    """Tank at cold-inlet temp: mixer math undefined; we return mix litres."""
    monkeypatch_target = 10.0  # default cold inlet
    assert dhw.hot_litres_per_shower(monkeypatch_target) == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# required_tank_temp_for_n_showers
# ---------------------------------------------------------------------------


def test_required_tank_temp_zero_showers_returns_mixer_plus_one():
    """No showers planned → minimal floor (only the mixer + 1 °C). Used
    when the morning-reserve count is 0 in normal mode."""
    val = dhw.required_tank_temp_for_n_showers(0)
    assert val == pytest.approx(config.DHW_SHOWER_MIXER_TEMP_C + 1.0)


def test_required_tank_temp_small_n_collapses_to_mixer_safety():
    """When mix_required <= usable hot capacity, required = mixer + safety
    margin (2 °C default). 1 shower = 45 L mix; usable capacity is 0.7 × 200
    = 140 L hot — well above 45 L. Required = 38 + 2 = 40 °C."""
    val = dhw.required_tank_temp_for_n_showers(1)
    assert val == pytest.approx(40.0)


def test_required_tank_temp_large_n_pushes_above_mixer():
    """When mix_required > usable hot capacity, required rises with N.
    4 showers = 180 L mix > 140 L usable. Required ≈ cold + 180×(mixer-cold)/140 + safety.
    With cold=10, mixer=38: 10 + 180×28/140 + 2 = 10 + 36 + 2 = 48 °C."""
    val = dhw.required_tank_temp_for_n_showers(4)
    assert val == pytest.approx(48.0, abs=0.5)


def test_required_tank_temp_scales_monotonically():
    """Required tank temp is monotonically non-decreasing in N."""
    vals = [dhw.required_tank_temp_for_n_showers(n) for n in range(1, 8)]
    for a, b in zip(vals, vals[1:]):
        assert b >= a


def test_required_tank_temp_with_lower_flow_drops(monkeypatch):
    """Lower flow rate → less mix-out litres → lower required tank temp."""
    monkeypatch.setattr(config, "DHW_SHOWER_FLOW_LPM", 7.0, raising=False)
    with_low = dhw.required_tank_temp_for_n_showers(4)
    monkeypatch.setattr(config, "DHW_SHOWER_FLOW_LPM", 9.0, raising=False)
    with_high = dhw.required_tank_temp_for_n_showers(4)
    assert with_low < with_high


def test_required_tank_temp_with_higher_mixer_lifts(monkeypatch):
    """A higher comfort mixer temp (e.g. 40 °C) raises the required floor."""
    base = dhw.required_tank_temp_for_n_showers(4)
    monkeypatch.setattr(config, "DHW_SHOWER_MIXER_TEMP_C", 40.0, raising=False)
    lifted = dhw.required_tank_temp_for_n_showers(4)
    assert lifted > base


# ---------------------------------------------------------------------------
# derive_overnight_target_c
# ---------------------------------------------------------------------------


def test_derive_overnight_target_normal_clamped():
    """Normal mode: morning reserve 1 → derive 40 °C, clamped into
    [NORMAL-5, NORMAL+5] = [40, 50] with NORMAL=45 (test fixture default)."""
    val = dhw.derive_overnight_target_c(OperationPreset.NORMAL)
    assert 40.0 <= val <= 50.0


def test_derive_overnight_target_vacation_floor():
    """Vacation: anti-freeze floor (DHW_TEMP_MIN_FLOOR_C default 30 °C)."""
    val = dhw.derive_overnight_target_c(OperationPreset.VACATION)
    assert val == pytest.approx(config.DHW_TEMP_MIN_FLOOR_C, abs=0.1)


def test_derive_overnight_target_guests_higher_than_normal(monkeypatch):
    """Guests mode: more morning showers → higher derived target than normal,
    clamped into the safe band."""
    monkeypatch.setattr(config, "DHW_GUEST_COUNT", 2, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST", 1, raising=False)
    normal_target = dhw.derive_overnight_target_c(OperationPreset.NORMAL)
    guests_target = dhw.derive_overnight_target_c(OperationPreset.GUESTS)
    assert guests_target >= normal_target


# ---------------------------------------------------------------------------
# daily_shower_litres_drawn — legacy escape hatch
# ---------------------------------------------------------------------------


def test_daily_litres_legacy_override(monkeypatch):
    """DHW_DAILY_SHOWER_LITRES > 0 in env preempts the new derivation
    (operator escape hatch for rollback)."""
    monkeypatch.setattr(config, "DHW_DAILY_SHOWER_LITRES", 144.0, raising=False)
    assert dhw.daily_shower_litres_drawn(OperationPreset.NORMAL) == pytest.approx(144.0)


def test_daily_litres_normal_excludes_morning_reserve(monkeypatch):
    """Normal mode: morning reserve is a floor, NOT a draw. Daily litres
    = evening × mix = 4 × 45 = 180."""
    monkeypatch.setattr(config, "DHW_DAILY_SHOWER_LITRES", 0.0, raising=False)
    val = dhw.daily_shower_litres_drawn(OperationPreset.NORMAL)
    assert val == pytest.approx(4 * 5 * 9.0)


def test_daily_litres_guests_includes_morning_extras(monkeypatch):
    """Guests mode: morning visitor extras ARE actual draw. Daily litres
    = (evening + guests×evening_extra) × mix + (guests × morning_extra) × mix.
    With defaults: (4+2)×45 + 2×45 = 360 L."""
    monkeypatch.setattr(config, "DHW_DAILY_SHOWER_LITRES", 0.0, raising=False)
    monkeypatch.setattr(config, "DHW_GUEST_COUNT", 2, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWERS_GUESTS_EVENING_EXTRA_PER_GUEST", 1, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWERS_GUESTS_MORNING_EXTRA_PER_GUEST", 1, raising=False)
    val = dhw.daily_shower_litres_drawn(OperationPreset.GUESTS)
    # 6 evening + 2 morning extras = 8 × 45 = 360
    assert val == pytest.approx(8 * 45.0)


def test_daily_litres_vacation_zero(monkeypatch):
    """Vacation: zero draw."""
    monkeypatch.setattr(config, "DHW_DAILY_SHOWER_LITRES", 0.0, raising=False)
    assert dhw.daily_shower_litres_drawn(OperationPreset.VACATION) == 0.0


# ---------------------------------------------------------------------------
# kwh_electric_to_reheat
# ---------------------------------------------------------------------------


def test_kwh_to_reheat_typical_scenario():
    """200 L tank, 38 → 47.5 °C, COP 3.0 → ~0.74 kWh electric.
    Plan's worked example."""
    val = dhw.kwh_electric_to_reheat(from_c=38.0, to_c=47.5, cop=3.0, tank_litres=200.0)
    assert val == pytest.approx(0.74, abs=0.05)


def test_kwh_to_reheat_returns_zero_for_cooling():
    """from_c >= to_c → zero (no heating needed)."""
    assert dhw.kwh_electric_to_reheat(50.0, 45.0, cop=3.0, tank_litres=200.0) == 0.0


def test_kwh_to_reheat_scales_with_temp_lift():
    """Doubling the temp lift doubles the kWh."""
    small = dhw.kwh_electric_to_reheat(40.0, 45.0, cop=3.0, tank_litres=200.0)
    big = dhw.kwh_electric_to_reheat(40.0, 50.0, cop=3.0, tank_litres=200.0)
    assert big == pytest.approx(2 * small, rel=0.01)


def test_kwh_to_reheat_inverse_with_cop():
    """Halving the COP doubles the electric kWh for the same thermal lift."""
    high = dhw.kwh_electric_to_reheat(40.0, 50.0, cop=3.0, tank_litres=200.0)
    low = dhw.kwh_electric_to_reheat(40.0, 50.0, cop=1.5, tank_litres=200.0)
    assert low == pytest.approx(2 * high, rel=0.01)
