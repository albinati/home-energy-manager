"""LP-owned DHW timing (#714, part B): the LP times the tank itself.

Three things have to be true before this can ever be enabled in prod, and each
gets its own section here:

1. **flag off is byte-identical.** The K2 pin must survive untouched, so a
   regression here can't reach anyone who hasn't opted in.
2. **flag on is never Infeasible.** Every comfort constraint is soft, with
   penalised slack (the #344 / #422 lessons: a tank floor that can't be met must
   surface as a deficit, never as a dead solver).
3. **it does what the user asked.** The tank stops being heated while the tariff
   is expensive, and hot water still arrives when the household actually draws it
   — which prod telemetry says is the MORNING, not the four evening showers
   ``dhw_demand`` assumes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

TZ = ZoneInfo("UTC")


@pytest.fixture(autouse=True)
def _fast_lp(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "lp.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(_db, "_db_path", lambda: path)
    _db.init_db()
    monkeypatch.setattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 20)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    # DAIKIN_CONTROL_MODE and OPTIMIZATION_PRESET are runtime-tunable PROPERTIES
    # backed by a class-level `_overrides` dict. monkeypatch.setattr on them
    # restores the value but leaves the KEY in the dict forever, so it keeps
    # shadowing whatever later tests set — patch the dict entry instead.
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "normal")
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_TANK_LEARNED_VALUES_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", False)
    monkeypatch.setattr(config, "LP_PLUNGE_PREP_HOURS", 0)


def _learned_tank(*, profile: list[float] | None = None, ua: float = 2.0,
                  cop_mult: float = 0.55) -> None:
    """Persist a measured calibration, shaped like the real one."""
    if profile is None:
        profile = [0.0] * 12
        profile[4] = 0.5   # 08:00-10:00 — the real household's morning draw
        profile[10] = 0.4  # 20:00-22:00 — the smaller evening one
    _db.upsert_dhw_tank_calibration({
        "ua_w_per_k": ua,
        "cop_mult": cop_mult,
        "cop_dhw_median": 2.59,
        "draw_profile_p75_json": json.dumps(profile),
    })


def _starts(n: int, *, first_hour: int = 0) -> list[datetime]:
    base = datetime(2026, 7, 8, first_hour, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def _weather(starts: list[datetime], *, pv: float = 0.0) -> WeatherLpSeries:
    n = len(starts)
    return WeatherLpSeries(
        slot_starts_utc=list(starts),
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n,
        cloud_cover_pct=[100.0] * n,
        pv_kwh_per_slot=[pv] * n,
        cop_space=[3.0] * n,
        cop_dhw=[4.7] * n,
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
# 1. Flag off changes nothing
# ---------------------------------------------------------------------------


def test_flag_off_keeps_the_k2_pin_and_ignores_the_calibration():
    """The measured physics must not leak into the pinned regime. Anyone who has
    not opted in gets exactly today's plan, calibration table or not."""
    _learned_tank()
    starts = _starts(24)
    prices = [15.0] * 24
    plan = _solve(starts, prices)
    assert plan.ok
    assert plan.dhw_lp_owned is False
    assert plan.dhw_measured_draw is False

    # The pinned tank trajectory is dhw_policy's, not a solved one.
    from src import dhw_policy
    _, pinned_tank = dhw_policy.forecast_dhw_load_per_slot(
        list(starts), mode="normal", initial_tank_c=45.0, price_line=list(prices),
    )
    # Slot 0 is the measured initial state, not the policy's view of it; every
    # boundary after it is the pinned trajectory.
    assert plan.tank_temp_c[1:] == pytest.approx(pinned_tank[1:], abs=1e-6)


def test_passive_mode_forces_lp_owned_off(monkeypatch):
    """Two owners of e_dhw means Infeasible on every solve (#639). Passive already
    pins e_dhw to the physics prediction, so LP-owned must yield — even when the
    flag (or the shadow's force) says otherwise."""
    _learned_tank()
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "passive")
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", True, raising=False)
    starts = _starts(24)
    plan = _solve(starts, [15.0] * 24, force=True)
    assert plan.status == "Optimal"
    assert plan.dhw_lp_owned is False


# ---------------------------------------------------------------------------
# 2. Flag on is never Infeasible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tank0", [20.0, 30.0, 37.0, 45.0, 60.0, 66.0])
@pytest.mark.parametrize("soc0", [0.0, 5.0])
def test_lp_owned_never_infeasible_across_initial_states(tank0, soc0):
    """A cold tank at a drawing hour, a tank above the ceiling, a flat battery —
    none of it may kill the solver. Comfort is soft; deficits surface as slack."""
    _learned_tank()
    starts = _starts(24, first_hour=8)  # opens INSIDE the measured morning draw
    plan = _solve(starts, [30.0] * 24, force=True, soc=soc0, tank=tank0)
    assert plan.status == "Optimal", f"tank0={tank0} soc0={soc0} → {plan.status}"
    assert plan.dhw_lp_owned is True


def test_lp_owned_never_infeasible_in_guests_mode(monkeypatch):
    """Guests raise the tank floors and have no measured history. The regime must
    fall back to the shower model rather than pinning a normal-mode profile onto
    a household of six (the #422 class of bug: a floor nobody can satisfy)."""
    _learned_tank()
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "guests")
    starts = _starts(24, first_hour=18)
    plan = _solve(starts, [30.0] * 24, force=True, tank=37.0)
    assert plan.status == "Optimal"
    assert plan.dhw_lp_owned is True
    assert plan.dhw_measured_draw is False  # measured profile is normal-mode only


def test_unlearned_tank_falls_back_to_the_shower_model():
    """No calibration yet (the steady state for weeks after deploy): LP-owned still
    solves, on dhw_demand's model. It must not wait for data to be safe."""
    starts = _starts(24)
    plan = _solve(starts, [20.0] * 24, force=True)
    assert plan.status == "Optimal"
    assert plan.dhw_lp_owned is True
    assert plan.dhw_measured_draw is False


# ---------------------------------------------------------------------------
# 3. It does what the user asked
# ---------------------------------------------------------------------------


def test_measured_draw_replaces_the_shower_model_and_heats_before_the_morning():
    """The household draws its hot water in the MORNING (08:00-10:00 measured).
    The LP must arrive at that draw with a hot tank — heating BEFORE it, not at
    dhw_policy's 13:00, which is after the water was already used."""
    _learned_tank()
    starts = _starts(28, first_hour=0)  # 00:00 → 14:00, covering the morning draw
    plan = _solve(starts, [20.0] * 28, force=True, tank=38.0)
    assert plan.ok
    assert plan.dhw_measured_draw is True

    def _kwh_between(h0: int, h1: int) -> float:
        return sum(
            e for st, e in zip(plan.slot_starts_utc, plan.dhw_electric_kwh, strict=False)
            if h0 <= st.hour < h1
        )

    assert _kwh_between(0, 8) > 0.2, "the LP must heat ahead of the morning draw"
    # And it arrives hot: the tank clears the delivery floor when water is drawn.
    floor = float(config.DHW_LP_DELIVERY_FLOOR_C)
    drawing = [
        t for st, t in zip(plan.slot_starts_utc, plan.tank_temp_c[1:], strict=False)
        if 8 <= st.hour < 10
    ]
    assert min(drawing) >= floor - 1.0


def test_the_tank_is_not_heated_through_the_expensive_window():
    """The user's own hypothesis: stop heating the tank while the tariff is high —
    the tank coasts at 0.1-0.5 °C/h, so holding costs almost nothing, and the
    reheat can wait for the cheap slots. With a measured draw and a price cliff,
    the LP must move its heating OUT of the peak."""
    _learned_tank()
    starts = _starts(24, first_hour=12)  # 12:00 → 24:00
    # 16:00-19:00 is brutal; everything else is cheap.
    prices = [
        90.0 if 16 <= st.hour < 19 else 8.0
        for st in starts
    ]
    plan = _solve(starts, prices, force=True, tank=45.0)
    assert plan.ok

    peak_kwh = sum(
        e for st, e in zip(plan.slot_starts_utc, plan.dhw_electric_kwh, strict=False)
        if 16 <= st.hour < 19
    )
    cheap_kwh = sum(
        e for st, e in zip(plan.slot_starts_utc, plan.dhw_electric_kwh, strict=False)
        if not (16 <= st.hour < 19)
    )
    assert peak_kwh < 0.05, f"LP heated the tank at 90p/kWh ({peak_kwh:.3f} kWh)"
    assert cheap_kwh > peak_kwh

    # The evening draw is still served — comfort was not bought with slack.
    evening = [
        t for st, t in zip(plan.slot_starts_utc, plan.tank_temp_c[1:], strict=False)
        if 20 <= st.hour < 22
    ]
    assert min(evening) >= float(config.DHW_LP_DELIVERY_FLOOR_C) - 1.0


def test_the_measured_cop_makes_tank_heat_cost_what_it_actually_costs():
    """The LP's curve claims a DHW COP of ~4.70; the tank measures 2.6. Uncorrected,
    the LP times the tank believing its heat is half price — so the multiplier has
    to reach the solver, and the plan's own energy must reflect it."""
    _learned_tank(cop_mult=0.55)
    starts = _starts(24, first_hour=0)
    plan = _solve(starts, [20.0] * 24, force=True, tank=38.0)
    assert plan.ok

    # Heating the tank from 38 °C to the delivery floor takes ~2x the electricity
    # the uncorrected curve would have budgeted. Compare against the same solve
    # with a neutral multiplier.
    _learned_tank(cop_mult=1.0)
    optimistic = _solve(starts, [20.0] * 24, force=True, tank=38.0)
    assert sum(plan.dhw_electric_kwh) > sum(optimistic.dhw_electric_kwh) * 1.3
