"""Dynamic DHW window (#755) — hold-to-peak-edge vs boost-and-coast.

Per plan-date the resolver derives the setback hour + warmup target from that
day's Agile rates + tank physics, persists the decision once (single-writer,
#683 pattern), and every consumer reads the persisted decision. Missing rates
or the kill switch → the static fallback hours.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db, dhw_policy
from src.config import config

TZ = ZoneInfo("Europe/London")
DAY = date(2026, 7, 21)  # a Tuesday, DST (BST) in force


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config.DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setitem(config._overrides, "DHW_WARMUP_START_HOUR_LOCAL", 13)
    monkeypatch.setitem(config._overrides, "DHW_SETBACK_START_HOUR_LOCAL", 16)
    monkeypatch.setitem(config._overrides, "DHW_TEMP_NORMAL_C", 47.0)
    monkeypatch.setitem(config._overrides, "DHW_DYNAMIC_WINDOW_ENABLED", "true")
    monkeypatch.setitem(config._overrides, "DHW_DYNAMIC_BOOST_HOLD_HOURS", 2)
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "normal")
    monkeypatch.setattr(config, "OPTIMIZATION_PEAK_THRESHOLD_PENCE", 27.0)
    monkeypatch.setattr(config, "DHW_FORECAST_AUTOSCALE_ENABLED", False, raising=False)
    db.init_db()
    dhw_policy._window_decision_logged.clear()
    dhw_policy._autoscale_cache.clear()
    yield


def _rates(day: date, *, base_p: float = 12.0, peak_p: float = 32.0,
           peak_start_h: int = 16, peak_end_h: int = 19,
           real: bool = True, spike_slots: list[tuple[int, int]] | None = None):
    """48 half-hour rate dicts for the local day (DST-safe anchors)."""
    rows = []
    for h in range(24):
        for m in (0, 30):
            s = datetime(day.year, day.month, day.day, h, m, tzinfo=TZ).astimezone(UTC)
            p = peak_p if peak_start_h <= h < peak_end_h else base_p
            if spike_slots and (h, m) in spike_slots:
                p = peak_p
            rows.append({
                "valid_from": s.isoformat(),
                "value_inc_vat": p,
                "fetched_at": "real" if real else "prior",
            })
    return rows


# ---------------------------------------------------------------------------
# Peak-entry derivation
# ---------------------------------------------------------------------------


def test_peak_entry_found_at_16():
    price_map, _ = dhw_policy._price_and_real_maps(_rates(DAY))
    assert dhw_policy._evening_peak_entry_hour(DAY, price_map) == 16


def test_flat_day_has_no_peak():
    price_map, _ = dhw_policy._price_and_real_maps(
        _rates(DAY, base_p=15.0, peak_p=15.0))
    assert dhw_policy._evening_peak_entry_hour(DAY, price_map) is None


def test_single_half_hour_spike_is_ignored():
    price_map, _ = dhw_policy._price_and_real_maps(
        _rates(DAY, peak_p=12.0, spike_slots=[(17, 0)]))
    assert dhw_policy._evening_peak_entry_hour(DAY, price_map) is None


def test_morning_spike_does_not_count_as_evening_peak():
    # Expensive 08:00-10:00 only — before _PEAK_SEARCH_START_HOUR.
    price_map, _ = dhw_policy._price_and_real_maps(
        _rates(DAY, peak_start_h=8, peak_end_h=10))
    assert dhw_policy._evening_peak_entry_hour(DAY, price_map) is None


# ---------------------------------------------------------------------------
# Resolver: persist gate + persist-once + arms
# ---------------------------------------------------------------------------


def test_prior_rates_return_static_and_do_not_persist():
    d = dhw_policy.resolve_window_decision_local(DAY, _rates(DAY, real=False))
    assert d.arm == "static"
    assert d.setback_hour_local == 16
    assert dhw_policy._persisted_window_decision(DAY) is None


def test_real_rates_persist_once_verbatim():
    d1 = dhw_policy.resolve_window_decision_local(DAY, _rates(DAY))
    assert d1.arm in ("hold", "boost")
    assert dhw_policy._persisted_window_decision(DAY) is not None
    # Re-resolve against DIFFERENT rates → the frozen decision, verbatim.
    d2 = dhw_policy.resolve_window_decision_local(
        DAY, _rates(DAY, peak_start_h=18, peak_end_h=21))
    assert d2 == d1


def test_below_threshold_evening_is_not_a_peak():
    """A 25p evening below OPTIMIZATION_PEAK_THRESHOLD_PENCE (27) is nothing
    worth avoiding — static day."""
    d = dhw_policy.resolve_window_decision_local(
        DAY, _rates(DAY, base_p=12.0, peak_p=25.0, peak_start_h=17, peak_end_h=18))
    assert d.arm == "static"


def test_hold_wins_short_late_peak():
    """A short peak at 17:00: only one extra hold hour beyond the boost
    runway — the small maintenance cost beats buying a boost lift."""
    d = dhw_policy.resolve_window_decision_local(
        DAY, _rates(DAY, base_p=12.0, peak_p=30.0, peak_start_h=17, peak_end_h=18))
    assert d.peak_entry_hour_local == 17
    assert d.arm == "hold"
    assert d.setback_hour_local == 17
    assert d.warmup_target_c == 47.0
    assert d.cost_hold_p is not None


def test_hold_setback_lands_on_peak_entry():
    d = dhw_policy.resolve_window_decision_local(DAY, _rates(DAY))
    assert d.peak_entry_hour_local == 16
    if d.arm == "hold":
        assert d.setback_hour_local == 16
    else:
        # boost sets back after the hold-hours runway
        assert d.setback_hour_local == 13 + 2


def test_boost_wins_long_expensive_hold():
    """Peak entry at 15:00, warmup at 11:00, and PRICEY pre-peak hours: the
    hold arm pays maintenance through 22p slots while the boost arm buys a
    small lift at 6p and coasts — boost must win by more than the margin, and
    its coast must deliver the hold arm's temperature at shower time."""
    from src.dhw.model import coast_to
    from src.dhw.params import resolve_tank_params

    cfgpatch = config._overrides
    cfgpatch["DHW_WARMUP_START_HOUR_LOCAL"] = 11
    try:
        rates = _rates(DAY, base_p=6.0, peak_p=38.0, peak_start_h=15, peak_end_h=21)
        for r in rates:  # 13:00-15:00 pre-peak shoulder at 22p
            h = datetime.fromisoformat(r["valid_from"]).astimezone(TZ).hour
            if h in (13, 14):
                r["value_inc_vat"] = 22.0
        d = dhw_policy.resolve_window_decision_local(DAY, rates)
        assert d.peak_entry_hour_local == 15
        assert d.arm == "boost"
        assert d.setback_hour_local == 11 + 2
        p = resolve_tank_params()
        assert d.warmup_target_c <= float(p.t_hp_max_c) + 1e-9
        assert d.t_ref_c is not None
        # Round-trip: coasting from the boost target must land >= T_ref.
        landed = coast_to(float(d.warmup_target_c), 20 - d.setback_hour_local, p)
        assert landed >= float(d.t_ref_c) - 0.15
    finally:
        cfgpatch["DHW_WARMUP_START_HOUR_LOCAL"] = 13


def test_early_peak_clamps_setback_to_hold_hours():
    """Peak entering before warmup+hold must never truncate the lift+settle
    runway: setback >= warmup + DHW_DYNAMIC_BOOST_HOLD_HOURS."""
    d = dhw_policy.resolve_window_decision_local(
        DAY, _rates(DAY, peak_start_h=14, peak_end_h=20))
    assert d.setback_hour_local >= 13 + 2


def test_flat_day_persists_static_arm():
    d = dhw_policy.resolve_window_decision_local(
        DAY, _rates(DAY, base_p=15.0, peak_p=15.0))
    assert d.arm == "static"
    assert dhw_policy._persisted_window_decision(DAY) is not None
    assert dhw_policy.read_window_decision(DAY).arm == "static"


# ---------------------------------------------------------------------------
# Kill switch + readers
# ---------------------------------------------------------------------------


def test_kill_switch_ignores_persisted_decision():
    dhw_policy.resolve_window_decision_local(DAY, _rates(DAY))
    assert dhw_policy.read_window_decision(DAY).arm in ("hold", "boost")
    config._overrides["DHW_DYNAMIC_WINDOW_ENABLED"] = "false"
    try:
        d = dhw_policy.read_window_decision(DAY)
        assert d.arm == "static"
        assert d.setback_hour_local == 16
        assert d.warmup_target_c == 47.0
    finally:
        config._overrides["DHW_DYNAMIC_WINDOW_ENABLED"] = "true"


def test_all_readers_agree_with_one_persisted_decision(monkeypatch):
    """Rows, forecast pin, nominal walk and the detector hour must all reflect
    the SAME persisted decision."""
    cfgpatch = config._overrides
    cfgpatch["DHW_WARMUP_START_HOUR_LOCAL"] = 11
    try:
        rates = _rates(DAY, base_p=6.0, peak_p=38.0, peak_start_h=15, peak_end_h=21)
        for r in rates:  # pre-peak shoulder at 22p — makes boost clear the margin
            h = datetime.fromisoformat(r["valid_from"]).astimezone(TZ).hour
            if h in (13, 14):
                r["value_inc_vat"] = 22.0
        d = dhw_policy.resolve_window_decision_local(DAY, rates)
        assert d.arm == "boost"

        # Rows: warmup row targets the boost temp and ends at the boost setback.
        rows = dhw_policy.generate_daily_tank_schedule(DAY, allow_past=True)
        warmups = [r for r in rows if r["action_type"] == "tank_warmup"]
        assert len(warmups) == 1
        params = warmups[0]["params"]
        assert params["tank_temp"] == int(round(d.warmup_target_c))
        end_local = datetime.fromisoformat(
            warmups[0]["end_time"].replace("Z", "+00:00")).astimezone(TZ)
        assert end_local.hour == d.setback_hour_local

        # Forecast pin: warmup-window slots end at the decision's setback and
        # the boost lift energy lands inside the window (more than plain
        # transition+maintenance would give).
        starts = [
            datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ).astimezone(UTC)
            for h in range(24) for m in (0, 30)
        ]
        e_dhw, tank_temps = dhw_policy.forecast_dhw_load_per_slot(starts)
        idx_by_hour = {}
        for i, s in enumerate(starts):
            idx_by_hour.setdefault(s.astimezone(TZ).hour, []).append(i)
        # Trajectory: boost target inside the window, setback right after it.
        assert tank_temps[idx_by_hour[d.setback_hour_local][0]] == pytest.approx(37.0)
        assert tank_temps[idx_by_hour[d.setback_hour_local - 1][0]] == pytest.approx(
            float(d.warmup_target_c))
        window_kwh = sum(
            e_dhw[i] for h in range(11, d.setback_hour_local) for i in idx_by_hour[h]
        )
        plain_kwh = dhw_policy._WARMUP_TRANSITION_KWH * 2 + dhw_policy._WARMUP_MAINTENANCE_KWH * 2
        from src.dhw.model import electric_kwh_to_raise
        from src.dhw.params import resolve_tank_params as _rtp
        extra = electric_kwh_to_raise(
            47.0, float(d.warmup_target_c), dhw_policy._DEFAULT_T_OUT_C, _rtp())
        assert extra > 0
        assert window_kwh == pytest.approx(plain_kwh + extra, abs=0.02)

        # Per-slot cap respected (the LP pin equality must stay feasible).
        max_hp = max(0.05, float(getattr(config, "DAIKIN_MAX_HP_KW", 2.0)) * 0.5)
        assert all(v <= max_hp + 1e-9 for v in e_dhw)

        # Nominal walk uses today's decision — only meaningful when DAY is
        # today; instead assert the detector hour agrees.
        import src.state_machine  # noqa: F401 — detector reads via dhw_policy
        assert dhw_policy.read_window_decision(DAY).setback_hour_local == d.setback_hour_local
    finally:
        cfgpatch["DHW_WARMUP_START_HOUR_LOCAL"] = 13


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def test_window_decision_keys_are_swept():
    old_day = DAY - timedelta(days=30)
    db.set_runtime_setting(dhw_policy._window_decision_key(old_day), "{}")
    dhw_policy._sweep_stale_warmup_keys(DAY)
    assert db.get_runtime_setting(dhw_policy._window_decision_key(old_day)) is None


def test_static_decision_when_nothing_persisted():
    d = dhw_policy.read_window_decision(DAY)
    assert d.arm == "static"
    assert d.setback_hour_local == 16
    assert d.warmup_target_c == 47.0
