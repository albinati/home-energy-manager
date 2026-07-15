"""The DHW block wired into the real solver (#714).

The mini-LP tests (tests/dhw/test_lp.py) prove the block's economics in isolation.
These prove the wiring: flag OFF is byte-identical, flag ON stays feasible across the
awkward states, and the behaviour survives the full solver with all its other
constraints present.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

TZ = ZoneInfo("UTC")


@pytest.fixture(autouse=True)
def _fast_lp(tmp_path, monkeypatch):
    path = tmp_path / "wire.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(_db, "_db_path", lambda: path)
    _db.init_db()
    monkeypatch.setattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 20)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    # Runtime-property config keys leak through monkeypatch.setattr (they are backed
    # by a class-level _overrides dict) — patch the dict entry instead.
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "normal")
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", False)
    monkeypatch.setattr(config, "LP_PLUNGE_PREP_HOURS", 0)


def _starts(n, first_hour=0):
    base = datetime(2026, 7, 8, first_hour, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def _weather(starts, pv=0.0):
    n = len(starts)
    return WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[pv] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.7] * n,
    )


def _solve(starts, prices, *, force=False, soc=5.0, tank=45.0, pv=0.0):
    return solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=[0.3] * len(starts),
        weather=_weather(starts, pv=pv),
        initial=LpInitialState(soc_kwh=soc, tank_temp_c=tank),
        tz=TZ,
        force_dhw_lp_owned=force,
    )


# ---------------------------------------------------------------------------
# Flag off is byte-identical
# ---------------------------------------------------------------------------


def test_flag_off_is_the_k2_pin_untouched():
    """No one who has not opted in may see any change. The pinned regime must produce
    exactly the plan it did before the block existed."""
    starts = _starts(24)
    prices = [15.0] * 24
    plan = _solve(starts, prices)
    assert plan.ok
    assert plan.dhw_lp_owned is False

    from src import dhw_policy
    _, pinned = dhw_policy.forecast_dhw_load_per_slot(
        list(starts), mode="normal", initial_tank_c=45.0, price_line=list(prices))
    # Slot 0 is the measured initial state; the rest is the pinned trajectory.
    assert plan.tank_temp_c[1:] == pytest.approx(pinned[1:], abs=1e-6)


def test_passive_forces_the_regime_off(monkeypatch):
    """Two owners of e_dhw means Infeasible (#639). Passive pins e_dhw to the physics
    prediction already, so LP-owned must yield even when forced."""
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "passive")
    plan = _solve(_starts(24), [15.0] * 24, force=True)
    assert plan.status == "Optimal"
    assert plan.dhw_lp_owned is False


# ---------------------------------------------------------------------------
# Flag on: feasible everywhere, and it does what it should
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tank0", [20.0, 30.0, 37.0, 45.0, 50.0, 60.0])
@pytest.mark.parametrize("soc0", [0.0, 5.0])
def test_never_infeasible_across_states(tank0, soc0):
    """A cold tank opening inside the shower window, a flat battery, a tank above the
    cliff — none may kill the solver. Comfort is soft."""
    starts = _starts(24, first_hour=8)  # opens well before the 20:00 window
    plan = _solve(starts, [30.0] * 24, force=True, soc=soc0, tank=tank0)
    assert plan.status == "Optimal", f"tank0={tank0} soc0={soc0} → {plan.status}"
    assert plan.dhw_lp_owned is True


def test_it_moves_the_heat_out_of_the_expensive_evening():
    """The owner's ask, through the full solver. Showers at 20:00, a price cliff at
    16-19h: the tank must be hot at 20:00 with its heating bought earlier and cheaper,
    not during the peak."""
    starts = _starts(28, first_hour=12)  # 12:00 → 02:00, covers the evening
    prices = [90.0 if 16 <= st.hour < 19 else 8.0 for st in starts]
    plan = _solve(starts, prices, force=True, tank=45.0)
    assert plan.ok

    peak = sum(
        e for st, e in zip(plan.slot_starts_utc, plan.dhw_electric_kwh, strict=False)
        if 16 <= st.hour < 19
    )
    assert peak < 0.05, f"heated the tank through the peak: {peak:.3f} kWh"

    # Comfort is delivered by ENTERING the window hot, not by heating during it. The
    # tank clears the floor at the first 20:00 slot (tank_temp_c[i] is the boundary
    # entering slot i, where the entry floor applies)...
    starts = list(plan.slot_starts_utc)
    entry_idx = next(i for i, st in enumerate(starts) if st.hour == 20)
    assert plan.tank_temp_c[entry_idx] >= 44.0

    # ...and it does NOT reheat during the showers — the owner's explicit ask. The
    # heat that got it there was bought earlier, in the cheap slots, not in the peak
    # and not in the shower window itself.
    in_window = sum(
        e for st, e in zip(starts, plan.dhw_electric_kwh, strict=False)
        if 20 <= st.hour < 21
    )
    assert in_window < 0.05, f"reheated during the showers: {in_window:.3f} kWh"
    cheap = sum(
        e for st, e in zip(starts, plan.dhw_electric_kwh, strict=False)
        if not (16 <= st.hour < 19)
    )
    assert cheap > peak


def test_it_does_not_push_the_tank_past_the_cliff_on_positive_prices():
    """Cheap positive electricity must never command the tank above 50 °C — above the
    cliff is the immersion heater at COP 1, and the old 60 °C ceiling let PV pour into
    it (#718). The block caps it."""
    plan = _solve(_starts(24), [2.0] * 24, force=True, tank=45.0, pv=3.0)
    assert plan.ok
    assert max(plan.tank_temp_c) <= 50.0 + 1e-3
