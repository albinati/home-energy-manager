"""#954 - SoC roll-forward honours an in-force ForceCharge during the inverter start lag."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config
from src.foxess.models import RealTimeData
from src.scheduler import lp_initial_state
from src.scheduler.lp_roll_forward import (
    find_inforce_group,
    find_schedule_onset,
    roll_soc_with_onset,
)

LON = ZoneInfo("Europe/London")
KW = dict(capacity_kwh=10.0, reserve_kwh=1.0, max_power_kw=6.0)
LAG = 600.0


def _fc(sh, sm, eh, em, fd=80, pwr=5000, mode="ForceCharge"):
    return {"workMode": mode, "startHour": sh, "startMinute": sm, "endHour": eh,
            "endMinute": em, "extraParam": {"fdSoc": fd, "fdPwr": pwr}}


def _roll(groups, reading, slot0, soc, power=0.0, age=0.0, lag=LAG):
    now = reading + timedelta(seconds=age)
    gap = (slot0 - now).total_seconds() + age
    onset = find_schedule_onset(groups, reading, slot0, LON, max_power_kw=6.0)
    inf = find_inforce_group(groups, reading, LON)
    return roll_soc_with_onset(soc, power, age, gap, onset, inforce=inf, start_lag_s=lag, **KW)


T = datetime(2026, 11, 16, 22, 3, 0, tzinfo=UTC)      # London == UTC in November
S0 = datetime(2026, 11, 16, 22, 30, tzinfo=UTC)
G = [_fc(22, 0, 22, 29, fd=90)]                        # started 180 s before T


def test_inside_lag_window_rolls_at_group_power():
    # 27 min = 1620 s at 5 kW = 2.25 kWh
    r = _roll(G, T, S0, soc=1.0)
    assert r.soc_kwh == pytest.approx(3.25)
    assert r.reason == "ok+inforce_start_lag"


def test_inverter_limit_caps_group_power():
    g = [_fc(22, 0, 22, 29, fd=90, pwr=9000)]
    assert _roll(g, T, S0, soc=1.0).soc_kwh == pytest.approx(1.0 + 6.0 * 1620 / 3600)


def test_fdsoc_bounds_the_lag_roll():
    g = [_fc(22, 0, 22, 29, fd=27)]
    assert _roll(g, T, S0, soc=1.0).soc_kwh == pytest.approx(2.7)


def test_outside_lag_window_zero_reading_is_trusted():
    g = [_fc(21, 40, 22, 29, fd=90)]                   # started 1380 s before T
    r = _roll(g, T, S0, soc=1.0)
    assert r.soc_kwh == 1.0 and "start_lag" not in r.reason


def test_soc_already_at_or_above_fdsoc_unchanged():
    # Discharging reading (p < 0) would trigger the lag path if SoC were below fdSoc.
    # SoC 4.0 >= fdSoc 30 % (3.0): legacy measured roll, 4.0 - 1.2 * 1620/3600 = 3.46.
    g = [_fc(22, 0, 22, 29, fd=30)]
    r = _roll(g, T, S0, soc=4.0, power=-1.2)
    assert r.soc_kwh == pytest.approx(3.46) and r.reason == "ok"
    # same reading with SoC below fdSoc (2.0 < 3.0) takes the lag path: 2.0 + 2.25 -> bound 3.0
    r = _roll(g, T, S0, soc=2.0, power=-1.2)
    assert r.soc_kwh == pytest.approx(3.0) and r.reason == "ok+inforce_start_lag"


@pytest.mark.parametrize("power", [0.0, 0.51, 1.5, 3.0, 4.4, -1.2])
def test_ramp_below_90pct_rolls_at_group_power(power):
    # fdPwr 5 kW -> cut-off 4.5 kW. 1620 s at 5 kW = +2.25 kWh whatever p is below it.
    r = _roll(G, T, S0, soc=1.0, power=power)
    assert r.soc_kwh == pytest.approx(3.25) and r.reason == "ok+inforce_start_lag"


def test_at_or_above_90pct_uses_measured_power():
    # 4.5 kW and 4.9 kW: measured path, 1.0 + p * 0.45
    for power, exp in ((4.5, 3.025), (4.9, 3.205)):
        r = _roll(G, T, S0, soc=1.0, power=power)
        assert r.soc_kwh == pytest.approx(exp) and r.reason == "ok"


def test_just_outside_window_measured_power_is_used():
    g = [_fc(21, 40, 22, 29, fd=90)]                   # started 1380 s before T (> 600)
    r = _roll(g, T, S0, soc=1.0, power=1.5)
    assert r.soc_kwh == pytest.approx(1.0 + 1.5 * 0.45) and r.reason == "ok"
    g = [_fc(21, 53, 22, 29, fd=90)]                   # exactly 600 s before the reading: inside
    assert _roll(g, T, S0, soc=1.0, power=1.5).reason == "ok+inforce_start_lag"


def test_knob_zero_is_legacy():
    assert _roll(G, T, S0, soc=1.0, lag=0.0).soc_kwh == 1.0


def test_group_ending_before_slot0_stops_contributing():
    # group 22:00-22:29 ends at 22:30; slot 0 = 22:45 -> only 22:03 -> 22:30 (1620 s) charges
    s0 = datetime(2026, 11, 16, 22, 45, tzinfo=UTC)
    assert _roll(G, T, s0, soc=1.0).soc_kwh == pytest.approx(1.0 + 5.0 * 1620 / 3600)


def test_midnight_wrapping_group():
    g = [_fc(23, 30, 0, 29, fd=80)]
    reading = datetime(2026, 11, 16, 23, 36, tzinfo=UTC)   # 360 s after start
    s0 = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    r = _roll(g, reading, s0, soc=2.0)
    assert r.soc_kwh == pytest.approx(2.0 + 5.0 * 1440 / 3600) and r.reason == "ok+inforce_start_lag"
    # group started 11 min ago across midnight: outside window
    reading2 = datetime(2026, 11, 17, 0, 10, tzinfo=UTC)
    g2 = [_fc(23, 59, 1, 29, fd=80)]
    s0b = datetime(2026, 11, 17, 0, 30, tzinfo=UTC)
    assert _roll(g2, reading2, s0b, soc=2.0).soc_kwh == 2.0


def test_composes_with_later_group_onset_before_slot0():
    # in-force 22:00-22:29 (fd 30 -> 3.0 kWh), later group starts 22:30 < slot0 22:45
    g = [_fc(22, 0, 22, 29, fd=30), _fc(22, 30, 23, 59, fd=60, pwr=4000)]
    s0 = datetime(2026, 11, 16, 22, 45, tzinfo=UTC)
    r = _roll(g, T, s0, soc=1.0)
    # lag part: 22:03->22:30 at 5 kW = 2.25 -> 3.25, bounded by fd 30 % = 3.0;
    # onset part: 22:30->22:45 = 900 s at 4 kW = +1.0 -> 4.0 (< 6.0 fd cap)
    assert r.soc_kwh == pytest.approx(4.0)
    assert r.reason == "ok+inforce_start_lag+schedule_onset"


def test_force_discharge_not_covered():
    g = [_fc(22, 0, 22, 29, fd=5, mode="ForceDischarge")]
    r = _roll(g, T, S0, soc=5.0)
    assert r.soc_kwh == 5.0 and "start_lag" not in r.reason


# --- wire: through _roll_soc with the stored schedule (prod 2026-11-16) -------
CAP = 10.36


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", CAP, raising=False)
    monkeypatch.setattr(config, "MIN_SOC_RESERVE_PERCENT", 10.0, raising=False)
    monkeypatch.setattr(config, "MAX_INVERTER_KW", 6.0, raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    monkeypatch.setattr(config, "LP_SOC_ROLL_START_LAG_S", 600.0, raising=False)

    def _set(groups, age):
        monkeypatch.setattr(lp_initial_state, "get_cached_realtime_age_seconds",
                            lambda rt=None: age)
        monkeypatch.setattr(lp_initial_state.db, "get_latest_fox_schedule_state",
                            lambda: {"enabled": 1, "groups": groups})
    return _set


def test_wire_prod_2026_11_16_2203(wire):
    # Prod data: the 21:55:47 upload was 22:00-22:30 fdSoc 27, 22:30-23:59 fdSoc 77.
    wire([_fc(22, 0, 22, 29, fd=27), _fc(22, 30, 23, 59, fd=77)], 11.1)
    now = datetime(2026, 11, 16, 22, 3, 19, tzinfo=UTC)
    soc, info = lp_initial_state._roll_soc(
        1.036, RealTimeData(soc=10.0, battery_power=0.0), now, datetime(2026, 11, 16, 22, 30, tzinfo=UTC))
    # unbounded: 1.036 + 5 kW * 1612.1 s = 3.275; fdSoc 27 % * 10.36 = 2.7972 binds
    assert soc == pytest.approx(2.797, abs=1e-3)
    assert info["reason"] == "ok+inforce_start_lag" and info["from_kwh"] == 1.036
    assert info["inforce_start_lag"]["since_start_s"] == pytest.approx(188, abs=1.0)
    assert info["inforce_start_lag"]["group_power_kw"] == 5.0


def test_wire_prod_2026_11_16_2304(wire):
    wire([_fc(23, 0, 23, 59, fd=73)], 69.1)
    now = datetime(2026, 11, 16, 23, 4, 17, tzinfo=UTC)
    soc, info = lp_initial_state._roll_soc(
        3.212, RealTimeData(soc=31.0, battery_power=0.0), now, datetime(2026, 11, 16, 23, 30, tzinfo=UTC))
    # 3.212 + 5 kW * (1543 + 69.1) s / 3600 = 5.451 ; fdSoc 73 % = 7.56 does not bind
    assert soc == pytest.approx(5.451, abs=2e-3)
    assert info["reason"] == "ok+inforce_start_lag"


def test_wire_knob_zero_and_wedged_past_window(wire, monkeypatch):
    wire([_fc(23, 0, 23, 59, fd=73)], 69.1)
    now = datetime(2026, 11, 16, 23, 4, 17, tzinfo=UTC)
    s0 = datetime(2026, 11, 16, 23, 30, tzinfo=UTC)
    rt = RealTimeData(soc=31.0, battery_power=0.0)
    monkeypatch.setattr(config, "LP_SOC_ROLL_START_LAG_S", 0.0, raising=False)
    assert lp_initial_state._roll_soc(3.212, rt, now, s0)[0] == 3.212
    monkeypatch.setattr(config, "LP_SOC_ROLL_START_LAG_S", 600.0, raising=False)
    late = datetime(2026, 11, 16, 23, 14, 17, tzinfo=UTC)   # group 13+ min old
    assert lp_initial_state._roll_soc(3.212, rt, late, s0)[0] == 3.212
