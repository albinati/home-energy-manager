"""#863 — lift-segment COP for LWT boosts + W3 emergency floor (LP + live tick)."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
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


def solve(*, penalty=0.0, indoor=21.0, t_out=12.0, prices=None, segments=2):
    config.LP_COP_LWT_OFFSET_PENALTY_PER_K = penalty
    config.LP_COP_LWT_LIFT_SEGMENTS = segments
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
    for k in ("LP_COP_LWT_OFFSET_PENALTY_PER_K", "LP_COP_LWT_LIFT_SEGMENTS"):
        config._overrides.pop(k, None)


def lift_kwh(plan, idx, t_out):
    """Electric kWh above the curve draw (offset 0) in the given slots."""
    from src.physics import get_daikin_heating_kw
    floor = get_daikin_heating_kw(t_out) * 0.5
    return sum(max(0.0, plan.space_electric_kwh[i] - floor) for i in idx)


# ── Part A ───────────────────────────────────────────────────────────────────
def test_penalty_zero_is_bit_for_bit(reset_knobs):
    """Golden values were produced by the PRE-#863 code (main @ cab34fd) on this fixture.
    The HP plan is exact; the objective has the MIP-gap tolerance (it also moves with
    the host .env, which the plan vector does not)."""
    p = solve(penalty=0.0)
    gold = [0.0] * 7 + [0.036367612] + [0.333] * 10 + [0.19148936] * 8 + [0.333] * 6 \
        + [0.0] * 4 + [0.13464598] + [0.19148936] * 4 + [0.0] * 7
    assert p.space_electric_kwh == pytest.approx(gold, abs=1e-6)
    assert p.objective_pence == pytest.approx(336.5, rel=0.01)
    assert p.cop_lwt_offset_penalty_per_k == 0.0
    assert p.cop_space_effective == [4.7] * N          # no segments: the single curve COP
    p2 = solve(penalty=0.0, indoor=21.0, t_out=8.0, prices=[38.17] * N)
    gold2 = [0.0, 0.0, 0.0, 0.003483726] + [0.3996] * 11 + [0.29680365] * 20 + [0.0] * 13
    assert p2.space_electric_kwh == pytest.approx(gold2, abs=1e-6)
    # emergency slack exists but is inert when the house never nears the floor
    assert max(p.comfort_slack_emergency_c) == 0.0


def test_lift_penalty_reduces_afternoon_boost(reset_knobs):
    idx = band13_16()
    assert len(idx) == 6
    p0 = solve(penalty=0.0)
    p1 = solve(penalty=0.025)
    assert p0.ok and p1.ok
    l0, l1 = lift_kwh(p0, idx, 12.0), lift_kwh(p1, idx, 12.0)
    tot0 = sum(p0.space_electric_kwh[i] for i in idx)
    tot1 = sum(p1.space_electric_kwh[i] for i in idx)
    print(
        f"[#863 numbers] 13-16 lift kWh: penalty0={l0:.3f} penalty0.025={l1:.3f}; "
        f"13-16 total heat kWh: {tot0:.3f} vs {tot1:.3f}; max offset 13-16: "
        f"{max(p0.lwt_offset_c[i] for i in idx):.1f} vs {max(p1.lwt_offset_c[i] for i in idx):.1f}; "
        f"day heat kWh {sum(p0.space_electric_kwh):.3f} vs {sum(p1.space_electric_kwh):.3f}; "
        f"objective p {p0.objective_pence:.2f} vs {p1.objective_pence:.2f}"
    )
    assert l0 > 0.8, "penalty 0 must boost (use lift segments) in the 13-16 cheap band"
    assert l1 < 0.6 * l0, "a 2.5 %/K lift penalty must materially cut the afternoon boost"
    assert p1.cop_lwt_offset_penalty_per_k == 0.025
    # effective COP falls below the curve COP exactly where lift is used
    assert min(p1.cop_space_effective) < 4.7 - 0.05
    assert p1.objective_pence > p0.objective_pence - 1e-6   # lift is dearer, never cheaper


def test_segments_fill_in_order(reset_knobs):
    """Thermal output implied by the plan's effective COP must equal the IN-ORDER fill of
    the segments (base, then segment 0, then segment 1) for every slot - an out-of-order
    fill would show a lower effective COP than that."""
    from src.physics import get_daikin_heating_kw
    p = solve(penalty=0.04)
    t, cap = 12.0, 2.0 * 0.5
    floor = min(get_daikin_heating_kw(t) * 0.5, cap)
    lift = 10.0
    lv = [floor] + [min(get_daikin_heating_kw(t, lwt_offset_delta=lift * k / 2) * 0.5, cap) for k in (1, 2)]
    widths = [lv[1] - lv[0], lv[2] - lv[1]]
    cops = [4.7 * max(0.5, 1 - 0.04 * lift * (k + 0.5) / 2) for k in range(2)]
    n_lift_slots = n_seg2 = 0
    for i in range(N):
        e = p.space_electric_kwh[i]
        base = min(e, floor)
        rest = max(0.0, e - floor)
        s0 = min(rest, widths[0])
        s1 = max(0.0, rest - widths[0])
        expected_thermal = base * 4.7 + s0 * cops[0] + s1 * cops[1]
        assert p.cop_space_effective[i] * e == pytest.approx(expected_thermal, abs=1e-6), f"slot {i}"
        n_lift_slots += rest > 1e-6
        n_seg2 += s1 > 1e-6
    assert n_lift_slots > 0, "fixture must exercise the lift segments"
    assert n_seg2 > 0, "fixture must exercise the second segment too"


def test_plan_stays_inside_ceiling_for_any_segment_count(reset_knobs):
    from src.physics import get_daikin_heating_kw
    ceil = min(get_daikin_heating_kw(12.0, lwt_offset_delta=10) * 0.5, 1.0)
    for k in (1, 2, 4):
        p = solve(penalty=0.03, segments=k)
        assert p.ok
        assert max(p.space_electric_kwh) <= ceil + 1e-6
        assert max(p.space_electric_kwh) > 0.3


def test_passive_creates_no_segments(reset_knobs, monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    p = solve(penalty=0.05)
    assert p.cop_lwt_offset_penalty_per_k == 0.0


def test_new_settings_registered():
    from src.runtime_settings import SCHEMA
    assert SCHEMA["LP_COP_LWT_OFFSET_PENALTY_PER_K"].env_default() == 0.0
    assert SCHEMA["LP_W3_EMERGENCY_FLOOR_C"].env_default() == 20.0
    assert config.LP_COP_LWT_LIFT_SEGMENTS == 2


# ── Part B: LP ───────────────────────────────────────────────────────────────
def test_emergency_floor_binds_when_house_is_cold(reset_knobs):
    from src.physics import get_daikin_heating_kw
    p = solve(penalty=0.0, indoor=19.0, t_out=2.0, prices=[38.17] * N)
    ceil = get_daikin_heating_kw(2.0, lwt_offset_delta=10) * 0.5
    assert p.space_electric_kwh[0] == pytest.approx(ceil, abs=1e-3)   # max lift at slot 0
    assert p.lwt_offset_c[0] >= 9.5
    assert p.comfort_slack_emergency_c[0] > 0.5      # unavoidable: pump cannot lift 19 -> 20
    assert p.w3_emergency_floor_c == 20.0


def test_emergency_slack_zero_from_a_normal_start(reset_knobs):
    p = solve(penalty=0.0, indoor=21.0, t_out=8.0, prices=[38.17] * N)
    assert len(p.comfort_slack_emergency_c) == N
    assert max(p.comfort_slack_emergency_c) == 0.0


def test_emergency_floor_is_capped_by_the_slot_floor(reset_knobs, monkeypatch):
    """A low night floor is never overridden by the emergency number."""
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5, raising=False)
    monkeypatch.setattr(config, "LP_W3_PEAK_COAST_DELTA_C", 4.0, raising=False)
    p = solve(penalty=0.0, indoor=18.0, t_out=8.0, prices=[38.17] * N)
    assert p.ok
    assert p.comfort_slack_emergency_c[0] == 0.0


def test_plausibility_gate_rejects_emergency_slack_from_a_normal_start(reset_knobs):
    from src.scheduler.lp_dispatch import w3_trajectory_plausible
    p = solve(penalty=0.0)
    p.comfort_slack_emergency_c[10] = 0.5
    assert w3_trajectory_plausible(p) == (False, "emergency_slack")
    # a plan that STARTED under the floor is judged by the usual rules instead
    p.indoor_temp_c[0] = 19.0
    assert w3_trajectory_plausible(p)[1] != "emergency_slack"


def test_emergency_setting_cross_validation():
    from src import runtime_settings as rs
    rs.set_setting("LP_W3_NIGHT_FLOOR_C", 20.0)
    rs.set_setting("LP_W3_PEAK_COAST_DELTA_C", 0.0)
    rs.set_setting("INDOOR_SETPOINT_C", 21.0)
    rs.set_setting("LP_W3_EMERGENCY_FLOOR_C", 20.0)
    with pytest.raises(rs.SettingValidationError):
        rs.set_setting("LP_W3_EMERGENCY_FLOOR_C", 20.5)      # > night floor
    with pytest.raises(rs.SettingValidationError):
        rs.set_setting("LP_W3_PEAK_COAST_DELTA_C", 1.5)      # peak floor 19.5 < emergency
    with pytest.raises(rs.SettingValidationError):
        rs.set_setting("LP_W3_NIGHT_FLOOR_C", 19.0)          # below emergency


# ── Part B: live tick ────────────────────────────────────────────────────────
def _z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def em(monkeypatch):
    monkeypatch.setattr(config, "LWT_EMERGENCY_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LWT_EMERGENCY_TICKS", 2, raising=False)
    monkeypatch.setattr(config, "LWT_EMERGENCY_OFFSET_C", 5, raising=False)
    monkeypatch.setattr(config, "LWT_EMERGENCY_HOLD_MINUTES", 60, raising=False)
    monkeypatch.setattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30, raising=False)
    monkeypatch.setitem(config._overrides, "LP_W3_EMERGENCY_FLOOR_C", 20.0)
    lwt_coast.reset_backstop()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    plan_date = now.date().isoformat()
    applied = []
    ret = {"v": True}
    monkeypatch.setattr(
        "src.daikin_bulletproof.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger, **kw: applied.append((params, trigger, kw)) or ret["v"],
    )
    replan = MagicMock(return_value=True)
    notes = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, **kw: notes.append((msg, kw)))

    def run(indoor):
        monkeypatch.setattr(
            db, "get_latest_indoor_reading",
            lambda max_age_minutes=30: None if indoor is None else {"temp_c": indoor},
        )
        return lwt_coast.emergency_tick(
            now_utc=now, plan_date=plan_date, dev=MagicMock(), client=MagicMock(), replan_fn=replan,
        )

    return run, applied, ret, replan, notes, now, plan_date


def test_emergency_fires_after_two_ticks(em):
    run, applied, _ret, replan, notes, now, _pd = em
    assert run(19.5)["fired"] is False
    assert applied == []
    out = run(19.5)
    assert out["fired"] is True
    assert applied[0][0] == {"lwt_offset": 5} and applied[0][2]["skip_if_matches"] is False
    hold = lwt_coast.get_emergency_hold_until()
    assert hold is not None and abs((hold - now).total_seconds() - 3600) < 5
    replan.assert_called_once_with(force_write_devices=True, trigger_reason="lwt_emergency", bypass_cooldown=True)
    logs = db.get_action_logs(device="daikin", action="lwt_emergency")
    assert len(logs) == 1 and logs[0]["result"] == "ok"
    assert len(notes) == 1 and notes[0][1]["extra"]["warning_key"].startswith("lwt_emergency_")
    from src.scheduler.lp_dispatch import space_heating_gate_state
    assert space_heating_gate_state()["emergency_hold_until"] is not None


def test_emergency_notify_dedupes_per_day(em):
    run, applied, _ret, _replan, notes, _now, _pd = em
    for _ in range(4):
        run(19.5)
    assert len(applied) == 2 and len(notes) == 1


def test_emergency_not_when_above_floor_or_one_tick(em):
    run, applied, _ret, replan, *_ = em
    run(19.5)
    run(20.2)          # recovered: counter resets
    run(19.5)
    assert applied == [] and lwt_coast.get_emergency_hold_until() is None
    replan.assert_not_called()


def test_emergency_strong_positive_row_suppresses_weak_one_does_not(em):
    run, applied, _ret, _replan, _notes, now, plan_date = em
    rid = db.upsert_action(
        plan_date=plan_date, start_time=_z(now - timedelta(minutes=30)), end_time=_z(now + timedelta(hours=2)),
        device="daikin", action_type="lwt_preheat", params={"lwt_offset": 5}, status="active",
    )
    out = {}
    for _ in range(4):
        out = run(19.0)
    assert applied == [] and not out["fired"]
    # downgrade to a weak +2 row: the emergency must now act
    db.mark_action(rid, "completed")
    db.upsert_action(
        plan_date=plan_date, start_time=_z(now - timedelta(minutes=20)), end_time=_z(now + timedelta(hours=1)),
        device="daikin", action_type="lwt_preheat", params={"lwt_offset": 2}, status="active",
    )
    run(19.0)
    assert run(19.0)["fired"] is True and len(applied) == 1


def test_emergency_stale_sensor_holds_counter(em):
    run, applied, *_ = em
    run(19.5)
    assert lwt_coast._emergency_ticks == 1
    run(None)
    assert lwt_coast._emergency_ticks == 1
    assert run(19.5)["fired"] and len(applied) == 1


def test_emergency_apply_false_no_hold_no_notify(em):
    run, applied, ret, replan, notes, *_ = em
    ret["v"] = False
    run(19.5)
    out = run(19.5)
    assert out["fired"] is False and len(applied) == 1
    assert lwt_coast.get_emergency_hold_until() is None
    assert notes == [] and replan.call_count == 0
    logs = db.get_action_logs(device="daikin", action="lwt_emergency")
    assert logs and logs[0]["result"] == "skipped"


def test_emergency_gated_by_read_only_and_passive(em, monkeypatch):
    run, applied, *_ = em
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    for _ in range(3):
        run(19.0)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    for _ in range(3):
        run(19.0)
    assert applied == []


def test_emergency_hold_lifts_dispatch_offsets(em):
    """During the hold, _lp_offsets never writes below the emergency offset."""
    from src.scheduler import lp_dispatch
    from src.scheduler.lp_optimizer import LpPlan
    run, _applied, _ret, _replan, _notes, now, _pd = em
    run(19.5)
    run(19.5)
    n = 6
    starts = [now + timedelta(minutes=30 * i) for i in range(n)]
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0, slot_starts_utc=starts)
    plan.temp_outdoor_c = [5.0] * n
    plan.price_pence = [25.45] * n
    plan.space_electric_kwh = [0.2] * n
    plan.lwt_offset_c = [-3.0] * n
    plan.indoor_temp_c = [20.0] * (n + 1)
    plan.comfort_slack_c = [0.0] * n
    plan.w3_night_floor_c, plan.w3_setpoint_c, plan.w3_peak_coast_delta_c = 17.0, 21.0, 0.0
    plan.w3_ceiling_c = 23.0
    guards: dict = {}
    offs = lp_dispatch._lp_offsets(plan, None, now_utc=now, guards=guards)
    assert offs is not None
    in_hold = [o for s, o in zip(starts, offs) if s < now + timedelta(minutes=60)]
    assert in_hold and all(o is not None and o >= 5 for o in in_hold)
    assert guards.get("emergency_hold", 0) >= 1
