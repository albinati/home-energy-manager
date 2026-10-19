"""#863 — exact-marginal lift-segment COP for LWT boosts + W3 emergency floor (LP + live tick)."""
from __future__ import annotations

import math
import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.physics import get_daikin_heating_kw
from src.scheduler import lwt_coast
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

TZ = ZoneInfo("Europe/London")
N = 48
# 2026-10-20 00:00 local (BST, UTC+1) == 2026-10-19 23:00 UTC
BASE = datetime(2026, 10, 19, 23, 0, tzinfo=UTC)
SLOTS = [BASE + timedelta(minutes=30 * i) for i in range(N)]
CURVE = "-7:2.4,2:3.4,7:4.3,12:4.7,20:5.2"


def _local_h(i: int) -> float:
    st = SLOTS[i].astimezone(TZ)
    return st.hour + st.minute / 60.0


def cosy_prices() -> list[float]:
    out = []
    for i in range(N):
        h = _local_h(i)
        if 16 <= h < 19:
            out.append(38.17)
        elif 4 <= h < 7 or 13 <= h < 16 or h >= 22:
            out.append(12.49)
        else:
            out.append(25.45)
    return out


def band13_16() -> list[int]:
    return [i for i in range(N) if 13 <= _local_h(i) < 16 and SLOTS[i].astimezone(TZ).day == 20]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tempfile.mktemp(suffix=".db"), raising=False)
    db.init_db()
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "LP_HP_MIN_ON_SLOTS", 1, raising=False)
    monkeypatch.setattr(config, "LP_INVERTER_STRESS_COST_PENCE", 0.0, raising=False)
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 200.0, raising=False)
    monkeypatch.setattr(config, "BUILDING_THERMAL_MASS_KWH_PER_K", 16.5, raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_LEARNED_AUTO_APPLY", False, raising=False)
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 21.0, raising=False)
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 20.0, raising=False)
    monkeypatch.setattr(config, "LP_W3_PEAK_COAST_DELTA_C", 0.0, raising=False)
    monkeypatch.setitem(config._overrides, "LP_W3_CEILING_C", 23.0)
    monkeypatch.setattr(config, "DAIKIN_COP_CURVE_STR", CURVE, raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    monkeypatch.setattr(config, "LP_BATTERY_WEAR_COST_PENCE_PER_KWH", 5.0, raising=False)
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.36, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_SOURCE", "tier", raising=False)
    monkeypatch.setattr(config, "LP_W3_EMERGENCY_PEN_PENCE_PER_DEGC_SLOT", 0.0, raising=False)
    monkeypatch.setattr(config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.0, raising=False)


def solve(*, penalty=0.0, indoor=21.0, t_out=12.0, prices=None, em_pen=0.0):
    config.LP_COP_LWT_OFFSET_PENALTY_PER_K = penalty
    config.LP_W3_EMERGENCY_PEN_PENCE_PER_DEGC_SLOT = em_pen
    w = WeatherLpSeries(
        slot_starts_utc=SLOTS, temperature_outdoor_c=[t_out] * N,
        shortwave_radiation_wm2=[0.0] * N, cloud_cover_pct=[100.0] * N,
        pv_kwh_per_slot=[0.0] * N, cop_space=[4.7] * N, cop_dhw=[3.0] * N,
    )
    st = LpInitialState(soc_kwh=3.0, tank_temp_c=45.0, indoor_temp_c=indoor)
    return solve_lp(
        slot_starts_utc=SLOTS, price_pence=prices or cosy_prices(), base_load_kwh=[0.3] * N,
        weather=w, initial=st, tz=TZ, export_price_pence=[5.0] * N,
    )


@pytest.fixture()
def reset_knobs():
    yield
    config._overrides.pop("LP_COP_LWT_OFFSET_PENALTY_PER_K", None)


def lift_kwh(plan, idx, t_out):
    floor = get_daikin_heating_kw(t_out) * 0.5
    return sum(max(0.0, plan.space_electric_kwh[i] - floor) for i in idx)


# ── Part A ───────────────────────────────────────────────────────────────────
def test_penalty_zero_is_bit_for_bit_including_grid_import(reset_knobs):
    """Golden values come from the PRE-#863 code (main @ cab34fd) on this fixture: HP plan
    exact, grid import exact (two host .env variants were measured), objective within the
    MIP gap."""
    p = solve(penalty=0.0)
    gold = [0.0] * 7 + [0.036367612] + [0.333] * 10 + [0.19148936] * 8 + [0.333] * 6 \
        + [0.0] * 4 + [0.13464598] + [0.19148936] * 4 + [0.0] * 7
    assert p.space_electric_kwh == pytest.approx(gold, abs=1e-6)
    imp = sum(p.import_kwh)
    assert min(abs(imp - 25.95825513), abs(imp - 25.86442363)) < 1e-6, imp
    assert p.objective_pence == pytest.approx(336.0, rel=0.01)
    assert p.cop_lwt_offset_penalty_per_k == 0.0
    assert p.cop_space_effective == [4.7] * N
    assert p.space_lift_widths_kwh == []
    assert max(p.comfort_slack_emergency_c) == 0.0
    p2 = solve(penalty=0.0, indoor=21.0, t_out=8.0, prices=[38.17] * N)
    gold2 = [0.0, 0.0, 0.0, 0.003483726] + [0.3996] * 11 + [0.29680365] * 20 + [0.0] * 13
    assert p2.space_electric_kwh == pytest.approx(gold2, abs=1e-6)


def test_lift_penalty_reduces_afternoon_boost(reset_knobs):
    idx = band13_16()
    p0 = solve(penalty=0.0)
    p1 = solve(penalty=0.025)
    assert p0.ok and p1.ok
    l0, l1 = lift_kwh(p0, idx, 12.0), lift_kwh(p1, idx, 12.0)
    print(
        f"[#863 numbers] 13-16 lift kWh: {l0:.3f} -> {l1:.3f}; max offset "
        f"{max(p0.lwt_offset_c[i] for i in idx):.1f} -> {max(p1.lwt_offset_c[i] for i in idx):.1f}; "
        f"day heat {sum(p0.space_electric_kwh):.3f} -> {sum(p1.space_electric_kwh):.3f}; "
        f"objective {p0.objective_pence:.2f} -> {p1.objective_pence:.2f}"
    )
    assert l0 > 0.8
    assert l1 < 0.6 * l0
    assert p1.cop_lwt_offset_penalty_per_k == 0.025
    assert p1.objective_pence > p0.objective_pence - 1e-6


def _lp_source(monkeypatch, offset_max=5.0):
    monkeypatch.setattr(config, "DAIKIN_LWT_SOURCE", "lp", raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", offset_max, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_ABS_MAX_C", 60.0, raising=False)


def test_segment_widths_sum_to_ceiling(reset_knobs, monkeypatch):
    """floor + sum(widths) == space_ceil_kwh, one segment per whole K of lift, also when the
    max_hp clamp bites (top segments trimmed) and for a fractional lift."""
    for hp_kw, off_max in ((2.0, 10.0), (0.8, 10.0), (2.0, 7.5)):
        monkeypatch.setattr(config, "DAIKIN_MAX_HP_KW", hp_kw, raising=False)
        monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", off_max, raising=False)
        p = solve(penalty=0.03)
        cap = hp_kw * 0.5
        ceil = min(get_daikin_heating_kw(12.0, lwt_offset_delta=off_max) * 0.5, cap)
        floor = min(get_daikin_heating_kw(12.0) * 0.5, cap)
        for i in (0, 20, 47):
            assert p.space_floor_kwh[i] == pytest.approx(floor)
            assert p.space_floor_kwh[i] + sum(p.space_lift_widths_kwh[i]) == pytest.approx(ceil, abs=1e-9)
            assert len(p.space_lift_widths_kwh[i]) <= math.ceil(off_max)
        if hp_kw == 2.0 and off_max == 10.0:
            assert len(p.space_lift_widths_kwh[0]) == 10


def test_marginal_cop_exact_at_plus_five(reset_knobs, monkeypatch):
    """Thermal output at +5 K = E(5)*cop*m(5): the plan's effective COP at the max-lift slot
    is cop * (1 - 0.025*5) = 12.5 % under the curve COP, NOT ~2 % (mid-point pricing)."""
    _lp_source(monkeypatch, 5.0)
    p = solve(penalty=0.025)
    e5 = min(get_daikin_heating_kw(12.0, lwt_offset_delta=5) * 0.5, 1.0)
    full = [i for i in range(N) if p.space_electric_kwh[i] == pytest.approx(e5, abs=1e-6)]
    assert full, "fixture must reach +5 K in some slot"
    for i in full:
        assert p.cop_space_effective[i] == pytest.approx(4.7 * (1 - 0.025 * 5), abs=1e-6)
    # segment COPs are the analytical marginals and non-increasing
    cops = p.space_lift_cops[full[0]]
    assert cops == sorted(cops, reverse=True)
    e = [get_daikin_heating_kw(12.0, lwt_offset_delta=x) * 0.5 for x in range(6)]
    for k, c in enumerate(cops):
        want = 4.7 * (e[k + 1] * (1 - 0.025 * (k + 1)) - e[k] * (1 - 0.025 * k)) / (e[k + 1] - e[k])
        assert c == pytest.approx(want, abs=1e-9)


def test_segments_fill_in_order(reset_knobs):
    """Implied thermal output equals the IN-ORDER fill of the segments for every slot."""
    p = solve(penalty=0.04)
    n_lift = n_deep = 0
    for i in range(N):
        e = p.space_electric_kwh[i]
        fl = p.space_floor_kwh[i]
        thermal = min(e, fl) * 4.7
        rest = max(0.0, e - fl)
        left = rest
        for w, c in zip(p.space_lift_widths_kwh[i], p.space_lift_cops[i]):
            take = min(left, w)
            thermal += take * c
            left -= take
        assert p.cop_space_effective[i] * e == pytest.approx(thermal, abs=1e-6), f"slot {i}"
        n_lift += rest > 1e-6
        n_deep += rest > p.space_lift_widths_kwh[i][0] + p.space_lift_widths_kwh[i][1] + 1e-6
    assert n_lift > 0 and n_deep > 0


def test_old_lift_derate_not_double_counted(reset_knobs, monkeypatch):
    monkeypatch.setattr(config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.02, raising=False)
    _lp_source(monkeypatch, 5.0)
    p = solve(penalty=0.025)
    # segments are built on the raw curve COP 4.7 (old derate skipped): base slots show 4.7
    assert max(p.cop_space_effective) == pytest.approx(4.7, abs=1e-6)


def test_passive_creates_no_segments(reset_knobs, monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    assert solve(penalty=0.05).cop_lwt_offset_penalty_per_k == 0.0


def test_new_settings_registered_and_defaults_off():
    from src.runtime_settings import SCHEMA
    assert SCHEMA["LP_COP_LWT_OFFSET_PENALTY_PER_K"].env_default() == 0.0
    assert SCHEMA["LP_W3_EMERGENCY_FLOOR_C"].env_default() == 20.0
    import src.config as cfg
    assert cfg.Config.LP_W3_EMERGENCY_PEN_PENCE_PER_DEGC_SLOT == 0.0
    assert cfg.Config.LWT_EMERGENCY_ENABLED is False


# ── Part B: LP ───────────────────────────────────────────────────────────────
def test_emergency_floor_changes_the_plan_when_on(reset_knobs, monkeypatch):
    # cheap ordinary-floor slack (0.5 p/degC-slot): only the emergency penalty makes it heat
    monkeypatch.setattr(config, "LP_W3_COMFORT_PEN_PENCE_PER_DEGC_SLOT", 0.5, raising=False)
    kw = dict(penalty=0.0, indoor=19.0, t_out=2.0, prices=[38.17] * N)
    off = solve(em_pen=0.0, **kw)
    on = solve(em_pen=1000.0, **kw)
    ceil = get_daikin_heating_kw(2.0, lwt_offset_delta=10) * 0.5
    assert on.space_electric_kwh[0] == pytest.approx(ceil, abs=1e-3)
    assert on.comfort_slack_emergency_c[0] > 0.5
    assert max(off.comfort_slack_emergency_c) == 0.0            # off: no variables, zeros
    head_on = sum(on.space_electric_kwh[:8])
    head_off = sum(off.space_electric_kwh[:8])
    assert head_on > head_off + 0.3, (head_on, head_off)
    assert min(on.indoor_temp_c[1:9]) > min(off.indoor_temp_c[1:9]) + 0.05
    assert on.w3_emergency_floor_c == 20.0


def test_emergency_slack_zero_from_a_normal_start(reset_knobs):
    p = solve(penalty=0.0, indoor=21.0, t_out=8.0, prices=[38.17] * N, em_pen=1000.0)
    assert len(p.comfort_slack_emergency_c) == N
    assert max(p.comfort_slack_emergency_c) == 0.0


def test_emergency_floor_is_capped_by_the_slot_floor(reset_knobs, monkeypatch):
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5, raising=False)
    monkeypatch.setattr(config, "LP_W3_PEAK_COAST_DELTA_C", 4.0, raising=False)
    p = solve(penalty=0.0, indoor=18.0, t_out=8.0, prices=[38.17] * N, em_pen=1000.0)
    assert p.ok and p.comfort_slack_emergency_c[0] == 0.0
    assert lwt_coast.effective_emergency_floor_c(17.5) == 17.5
    assert lwt_coast.effective_emergency_floor_c(21.0) == 20.0


def test_plausibility_gate_rejects_emergency_slack_from_a_normal_start(reset_knobs):
    from src.scheduler.lp_dispatch import w3_trajectory_plausible
    p = solve(penalty=0.0, em_pen=1000.0)
    p.comfort_slack_emergency_c[10] = 0.5
    assert w3_trajectory_plausible(p) == (False, "emergency_slack")
    p.indoor_temp_c[0] = 19.0
    assert w3_trajectory_plausible(p)[1] != "emergency_slack"


# ── Part B: live tick (alert + replan only) ──────────────────────────────────
DAY_NOW = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)      # 12:00 BST: day, floor = min(20, 21)
NIGHT_NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)     # 02:00 BST: night


@pytest.fixture()
def em(monkeypatch):
    monkeypatch.setattr(config, "LWT_EMERGENCY_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LWT_EMERGENCY_TICKS", 2, raising=False)
    monkeypatch.setattr(config, "LWT_EMERGENCY_REPLAN_COOLDOWN_MINUTES", 60, raising=False)
    monkeypatch.setattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30, raising=False)
    monkeypatch.setitem(config._overrides, "LP_W3_EMERGENCY_FLOOR_C", 20.0)
    lwt_coast.reset_backstop()
    wrote = []
    monkeypatch.setattr(
        "src.daikin_bulletproof.apply_scheduled_daikin_params",
        lambda *a, **k: wrote.append((a, k)) or True,
    )
    replan = MagicMock(return_value=True)
    notes = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, **kw: notes.append((msg, kw)))

    def run(indoor, now=DAY_NOW, **kw):
        monkeypatch.setattr(
            db, "get_latest_indoor_reading",
            lambda max_age_minutes=30: None if indoor is None else {"temp_c": indoor},
        )
        return lwt_coast.emergency_tick(now_utc=now, plan_date="2026-10-20", replan_fn=replan, **kw)

    return run, wrote, replan, notes


def test_emergency_alerts_and_replans_after_two_ticks_never_writes(em):
    run, wrote, replan, notes = em
    assert run(19.5)["fired"] is False
    replan.assert_not_called()
    out = run(19.5)
    assert out["fired"] is True
    replan.assert_called_once_with(force_write_devices=True, trigger_reason="lwt_emergency", bypass_cooldown=True)
    logs = db.get_action_logs(device="daikin", action="lwt_emergency")
    assert len(logs) == 1 and logs[0]["result"] == "ok"
    assert len(notes) == 1 and notes[0][1]["extra"]["warning_key"] == "lwt_emergency_2026-10-20"
    assert wrote == []                                   # NEVER a device write
    assert db.get_kv("lwt_emergency_hold_until") is None  # and no dispatch hold


def test_emergency_replan_cooldown_and_daily_notify(em):
    run, _wrote, replan, notes = em
    run(19.5)
    run(19.5)
    assert replan.call_count == 1
    out = run(19.5, now=DAY_NOW + timedelta(minutes=10))
    assert out.get("cooldown") and replan.call_count == 1
    out = run(19.5, now=DAY_NOW + timedelta(minutes=61))
    assert out["fired"] and replan.call_count == 2
    assert len(notes) == 1                                # one notify per local day


def test_emergency_skipped_when_a_backstop_replanned_this_tick(em):
    run, _wrote, replan, notes = em
    run(19.5, skip_replan=True)
    out = run(19.5, skip_replan=True)
    assert out["fired"] is False and out.get("skipped")
    replan.assert_not_called()
    assert notes == []
    assert run(19.5)["fired"] is True and replan.call_count == 1   # counter stayed armed


def test_emergency_uses_the_slot_floor(em, monkeypatch):
    run, _wrote, replan, _notes = em
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5, raising=False)
    for _ in range(3):
        run(19.0, now=NIGHT_NOW)                          # night: min(20, 17.5) = 17.5
    replan.assert_not_called()
    for _ in range(2):
        out = run(17.0, now=NIGHT_NOW)
    assert out["fired"] and out["floor"] == 17.5
    # peak band with delta 2: floor min(20, 21-2) = 19
    lwt_coast.reset_backstop()
    monkeypatch.setattr(config, "LP_W3_PEAK_COAST_DELTA_C", 2.0, raising=False)
    monkeypatch.setattr(db, "get_kv", lambda k: None)
    for _ in range(3):
        out = run(19.5, in_peak=True)
    assert not out["fired"] and out["floor"] == 19.0


def test_emergency_ok_reading_resets_stale_holds_and_gating(em, monkeypatch):
    run, _wrote, replan, _notes = em
    run(19.5)
    run(20.5)                                             # recovered: resets
    run(19.5)
    assert replan.call_count == 0
    run(None)                                             # stale: holds the counter
    assert lwt_coast._emergency_ticks == 1
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", False, raising=False)
    for _ in range(3):
        run(19.0)
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    for _ in range(3):
        run(19.0)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "LWT_EMERGENCY_ENABLED", False, raising=False)
    for _ in range(3):
        run(19.0)
    assert replan.call_count == 0


def test_dispatch_has_no_emergency_hold():
    import inspect

    from src.scheduler import lp_dispatch
    assert "emergency" not in inspect.getsource(lp_dispatch._lp_offsets)
    assert "emergency" not in inspect.getsource(lp_dispatch._tier_offsets)
    assert "emergency_hold_until" not in lp_dispatch.space_heating_gate_state()
