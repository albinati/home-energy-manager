"""LP-owned LWT offsets (#803 / #808).

``DAIKIN_LWT_SOURCE=lp`` drives the Daikin LWT offset rows from the LP's W3
thermal plan (``plan.lwt_offset_c``) through the SAME pairs → action rows →
reconciler → ``apply_scheduled_daikin_params`` path as the price-tier rule;
``tier`` is the kill switch. Both are computed every dispatch and diffed.
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.scheduler.lp_dispatch import (
    _lp_offsets,
    _lwt_preheat_pairs,
    _pairs_from_offsets,
    _slot_is_near_now,
    _tier_offsets,
    _write_lwt_preheat_actions,
    smooth_lp_offsets,
    w3_trajectory_plausible,
)
from src.scheduler.lp_optimizer import LpInitialState, LpPlan, solve_lp
from src.weather import WeatherLpSeries

COSY_CHEAP, COSY_DAY, COSY_PEAK = 12.4868, 25.4461, 38.174
TZ = ZoneInfo("Europe/London")
DAY = date(2026, 11, 4)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_BOOST_C", 3)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_NEGATIVE_BOOST_C", 5)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", -2)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_COMFORT_BAND_C", 0.5)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -10.0)
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MAX", 10.0)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MIN", -5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_LP_OFFSET_MAX", 5.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_WEATHER_CURVE_HIGH_C", 18.0)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0)
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 21.0)
    monkeypatch.setattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 1)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5, raising=False)
    monkeypatch.setattr(config, "LP_W3_IMPLAUSIBLE_BELOW_FLOOR_C", 2.0, raising=False)
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "tier")


def _plan(n=8, *, outdoor=5.0, lwt=None, space=None, indoor=None, bands=None, start=None):
    t0 = start or datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    p = LpPlan(
        ok=True, status="Optimal", objective_pence=0.0,
        slot_starts_utc=[t0 + timedelta(minutes=30 * i) for i in range(n)],
        price_pence=[COSY_DAY] * n, temp_outdoor_c=[outdoor] * n,
        cheap_threshold_pence=18.97, peak_threshold_pence=31.81,
        tariff_structure_kind="banded", price_band=bands or ["standard"] * n,
    )
    p.lwt_offset_c = list(lwt) if lwt is not None else [0.0] * n
    p.space_electric_kwh = list(space) if space is not None else [0.3] * n
    if indoor is not None:
        p.indoor_temp_c = list(indoor)
    return p


# ── translation rules ────────────────────────────────────────────────────────


def test_lp_source_falls_back_to_tier_without_indoor_trajectory():
    plan = _plan(bands=["cheap"] * 4 + ["peak"] * 4)  # no indoor_temp_c → W3 off
    assert _lp_offsets(plan) is None
    assert w3_trajectory_plausible(plan) == (False, "no_trajectory")
    pairs = _lwt_preheat_pairs(plan, [], source="lp")
    offs = sorted({a["params"]["lwt_offset"] for _, a in pairs})
    assert offs == [-2, 3]  # the tier rule's cheap boost + peak setback


def test_comfort_slack_makes_lp_unavailable():
    """UA=600 in ordinary UK weather keeps the trajectory inside the absolute
    band but the pump cannot hold the floor → slack in many slots → refused."""
    plan = _plan(lwt=[5.0] * 8, indoor=[20.4] * 9, bands=["standard"] * 8)
    plan.comfort_slack_c = [0.0, 0.0, 0.3, 0.6, 0.8, 0.9, 1.0, 1.1]  # 6 slots > 0.1
    ok, reason = w3_trajectory_plausible(plan)
    assert not ok and reason.startswith("comfort_slack:6_slots")
    plan.comfort_slack_c = [0.0] * 6 + [0.2, 0.3]  # 2 slots: a real cold-snap dip is tolerated
    assert w3_trajectory_plausible(plan)[0]


def test_implausible_trajectory_makes_lp_unavailable_and_tier_unchanged():
    """UA unfitted → the RC model cannot hold the house → predicted indoor
    falls far below the night floor. The LP source must be unavailable and the
    tier rule must not change (it never reads the trajectory)."""
    falling = [21.0 - 0.8 * i for i in range(9)]  # down to 14.6 < 17.5 − 2
    plan = _plan(lwt=[5.0] * 8, indoor=falling, bands=["cheap"] * 4 + ["peak"] * 4)
    ok, reason = w3_trajectory_plausible(plan)
    assert not ok and reason.startswith("trajectory_implausible_min")
    assert _lp_offsets(plan) is None
    assert _tier_offsets(plan, [], None) == [3] * 4 + [-2] * 4
    assert _tier_offsets(plan, [], 23.0) == [0] * 4 + [-2] * 4  # live reading still guards boosts


def test_zero_heat_slot_maps_to_setback_not_offset_min():
    """The inverse physics returns OPTIMIZATION_LWT_OFFSET_MIN for a slot with
    no space heat; a deliberate coast must write the setback instead."""
    plan = _plan(lwt=[-10.0] * 8, space=[0.0] * 8, indoor=[21.0] * 9)
    assert _lp_offsets(plan) == [-2] * 8


def test_lp_offsets_clamped_to_tighter_of_both_ranges(monkeypatch):
    plan = _plan(lwt=[9.4, -8.0, 2.2, 4.6, 7.0, -3.0, 0.4, 5.0], indoor=[21.0] * 9)
    assert _lp_offsets(plan) == [5, -5, 2, 5, 5, -3, 0, 5]
    monkeypatch.setattr(config, "OPTIMIZATION_LWT_OFFSET_MIN", -2.0)  # prod pins −2
    assert _lp_offsets(plan) == [5, -2, 2, 5, 5, -2, 0, 5]


def test_warm_outdoor_slots_emit_no_rows():
    plan = _plan(outdoor=16.0, lwt=[4.0] * 8, indoor=[20.0] * 9)
    assert _lp_offsets(plan) == [None] * 8
    assert _lwt_preheat_pairs(plan, [], source="lp") == []


def test_predicted_trajectory_never_vetoes_the_lp_plan():
    """A plan that pre-heats to 22 °C before the peak keeps its boosts and a
    coast to 20.2 °C keeps its setback — the trajectory was computed assuming
    those offsets, so it must not be used to cancel them."""
    plan = _plan(lwt=[4.0] * 4 + [-2.0] * 4, indoor=[21.0, 21.4, 21.8, 22.1, 22.0, 21.3, 20.6, 20.2, 20.0])
    assert _lp_offsets(plan) == [4] * 4 + [-2] * 4


def test_live_reading_guards_only_near_slots_and_only_boosts():
    now = datetime(2026, 11, 4, 0, 10, tzinfo=UTC)
    plan = _plan(lwt=[4.0] * 4 + [-3.0] * 4, indoor=[20.0] * 9)
    assert _slot_is_near_now(plan, 1, now) and not _slot_is_near_now(plan, 2, now)
    assert _lp_offsets(plan, 23.0, now_utc=now) == [0, 0, 4, 4, -3, -3, -3, -3]
    assert _lp_offsets(plan, 19.0, now_utc=now) == [4, 4, 4, 4, -3, -3, -3, -3]  # cold room: setbacks untouched


def test_tier_rule_uses_plan_price_band_on_cosy():
    plan = _plan(bands=["cheap", "cheap", "standard", "standard", "peak", "peak", "standard", "negative"])
    assert _tier_offsets(plan, [], None) == [3, 3, 0, 0, -2, -2, 0, 5]


def test_w3_on_tier_equals_w3_off_tier_for_same_live_reading():
    """Kill-switch equivalence: with the thermal model on, source=tier yields
    exactly the pairs it yields with the model off."""
    bands = ["cheap"] * 6 + ["standard"] * 4 + ["peak"] * 6
    off_plan = _plan(n=16, bands=bands)
    on_plan = _plan(n=16, bands=bands, lwt=[5.0] * 16, indoor=[21.0] * 17)  # PLAUSIBLE trajectory
    assert w3_trajectory_plausible(on_plan)[0]
    for live in (None, 20.0, 22.5):
        assert _lwt_preheat_pairs(on_plan, [], indoor_c=live, source="tier") == \
            _lwt_preheat_pairs(off_plan, [], indoor_c=live, source="tier")


# ── smoothing ────────────────────────────────────────────────────────────────


def test_smooth_lp_offsets_blockifies_by_sign():
    """The LP signal ramps (3,5,5,4,5); value-run smoothing shredded it into
    fragments and restore writes. Sign blocks keep the boost whole, at the
    block's mean."""
    seq = [3, 5, 5, 4, 5, 0, -2, -3, -2, -2, None, None]
    # (#838 H2) spread is bounded at < 2 degC from a block's FIRST slot, but a
    # sub-block shorter than min_block is merged back (never dropped): 3|5,5,4,5 -> mean 4
    assert smooth_lp_offsets(seq, 4) == [4, 4, 4, 4, 4, 0, -2, -2, -2, -2, None, None]
    assert smooth_lp_offsets([3, 3, 4, 4, 3, 0, -2, -3, -2, -2], 4) == [3, 3, 3, 3, 3, 0, -2, -2, -2, -2]
    assert smooth_lp_offsets([3, 4, 0, 0, 5, 5, 5, 5], 4) == [0, 0, 0, 0, 5, 5, 5, 5]
    # a +5 pre-heat followed by a +1 top-up stays two blocks (split at |Δ| ≥ 2)
    assert smooth_lp_offsets([5] * 6 + [1] * 8, 4) == [5] * 6 + [1] * 8
    # a heating ramp whose pieces are all shorter than min_block merges back into one
    # mean block — planned heating is never dropped by the spread split
    assert smooth_lp_offsets([1, 2, 3, 4, 5], 4) == [3] * 5
    assert smooth_lp_offsets([1, 1, 2, 2, 2, 3, 3], 4) == [2, 2, 2, 2, 2, 2, 2]
    assert smooth_lp_offsets([1, 2, 3], 1) == [1, 2, 3]  # min_block 1: as-is


def test_solved_plan_through_lp_pairs_with_prod_min_block(monkeypatch):
    """A real solve → _lp_offsets → _pairs_from_offsets with the prod
    DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS=4: the 13-16 cheap-band pre-heat
    survives as one boost window and no setback lands inside it."""
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 4)
    plan, prices = _solve_w3(monkeypatch, source="lp")
    offs = _lp_offsets(plan)
    assert offs is not None
    pairs = _pairs_from_offsets(plan, offs, source="lp")
    assert pairs
    cheap_pm = [plan.slot_starts_utc[i] for i, p in enumerate(prices) if p == COSY_CHEAP and 26 <= i < 32]
    covering = []
    for _, act in pairs:
        st = datetime.fromisoformat(act["start_time"].replace("Z", "+00:00"))
        en = datetime.fromisoformat(act["end_time"].replace("Z", "+00:00"))
        hits = [s for s in cheap_pm if st <= s < en]
        if hits:
            assert act["params"]["lwt_offset"] >= 3, act
            covering.append((act, hits))
    # exactly one boost window covers all six 13-16 slots; no setback touches them
    assert len(covering) == 1 and len(covering[0][1]) == 6


# ── source switch + telemetry diff ───────────────────────────────────────────


def test_source_switch_is_runtime_tunable(monkeypatch):
    plan = _plan(lwt=[4.0] * 8, indoor=[21.0] * 9, bands=["peak"] * 8)
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "tier")
    tier_pairs = _lwt_preheat_pairs(plan, [])
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "lp")
    lp_pairs = _lwt_preheat_pairs(plan, [])
    assert tier_pairs[0][1]["params"]["lwt_offset"] == -2
    assert lp_pairs[0][1]["params"]["lwt_offset"] == 4


def test_diff_row_logged_with_both_sources(monkeypatch):
    db.init_db()
    monkeypatch.setattr("src.scheduler.lp_dispatch._space_heating_demand_present", lambda: True)
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 150)
    logged: list[dict] = []
    monkeypatch.setattr(db, "log_action", lambda **kw: logged.append(kw))
    upserts: list[dict] = []
    monkeypatch.setattr(db, "upsert_action", lambda **kw: upserts.append(kw) or len(upserts))
    plan = _plan(lwt=[4.0] * 4 + [-3.0] * 4, indoor=[21.0] * 9, bands=["cheap"] * 4 + ["peak"] * 4)
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "lp")
    n = _write_lwt_preheat_actions(DAY.isoformat(), plan, [])
    assert n >= 2
    diff = [kw for kw in logged if kw["action"] == "lwt_source_diff"]
    assert len(diff) == 1
    p = diff[0]["params"]
    assert p["source_used"] == "lp" and p["lp_available"] is True and p["n_slots"] == 8 and p["lp_reason"] == "ok"
    assert p["n_differ"] == 8 and p["windows"][0]["tier"] == 3 and p["windows"][0]["lp"] == 4
    assert {u["params"]["lwt_offset"] for u in upserts if u["action_type"] == "lwt_preheat"} == {4, -3}


def test_diff_row_reports_fallback_when_lp_unavailable(monkeypatch):
    db.init_db()
    monkeypatch.setattr("src.scheduler.lp_dispatch._space_heating_demand_present", lambda: True)
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 150)
    logged: list[dict] = []
    monkeypatch.setattr(db, "log_action", lambda **kw: logged.append(kw))
    monkeypatch.setattr(db, "upsert_action", lambda **kw: 1)
    plan = _plan(bands=["cheap"] * 8)  # no trajectory
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "lp")
    _write_lwt_preheat_actions(DAY.isoformat(), plan, [])
    p = [kw for kw in logged if kw["action"] == "lwt_source_diff"][0]["params"]
    assert p["source_used"] == "tier" and p["lp_available"] is False and p["lp_reason"] == "no_trajectory"


def test_gate_state_exposes_source_and_last_diff(monkeypatch):
    from src.scheduler.lp_dispatch import space_heating_gate_state

    db.init_db()
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "lp")
    db.log_action(device="daikin", action="lwt_source_diff",
                  params={"source_used": "lp", "n_differ": 3}, result="ok", trigger="dispatch")
    st = space_heating_gate_state()
    assert st["lwt_source"] == "lp"
    assert st["lwt_source_last_diff"]["n_differ"] == 3
    assert st["lwt_source_last_diff"]["logged_at"]


# ── the wire: LP offset → action row → reconciler → apply ────────────────────


def test_wire_lp_offset_reaches_set_lwt_offset(monkeypatch):
    """Rows written from the LP source fire through `_reconcile_daikin_actions`
    → `apply_scheduled_daikin_params` with the LP's offset intact; flipping
    DAIKIN_LWT_SOURCE to tier changes the WRITTEN offset on the next plan."""
    import src.state_machine as sm
    from src.daikin.models import DaikinDevice

    monkeypatch.setattr("src.config.config.PREFIRE_STATE_MATCH_ENABLED", True)
    monkeypatch.setattr("src.config.config.USER_OVERRIDE_RESPECT_HOURS", 4.0)
    monkeypatch.setattr("src.daikin_bulletproof.config.OPENCLAW_READ_ONLY", False)
    monkeypatch.setattr("src.scheduler.lp_dispatch._space_heating_demand_present", lambda: True)
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 150)
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()
    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda dev, client, params, trigger: apply_calls.append(params) or True,
    )
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
        now_utc = datetime.now(UTC).replace(second=0, microsecond=0)
        start = now_utc - timedelta(minutes=10)
        plan = _plan(n=4, lwt=[-4.0] * 4, indoor=[21.0] * 5, bands=["peak"] * 4, start=start)
        plan_date = now_utc.date().isoformat()

        monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "lp")
        assert _write_lwt_preheat_actions(plan_date, plan, []) >= 1
        rows = db.get_actions_for_plan_date(plan_date, device="daikin")
        def _p(r):
            return r["params"] if isinstance(r["params"], dict) else json.loads(r["params"])
        assert any(_p(r).get("lwt_offset") == -4 for r in rows if r["action_type"] == "lwt_preheat")
        dev = DaikinDevice(id="gw", name="x", lwt_offset=0.0)
        sm._reconcile_daikin_actions(rows, MagicMock(), dev, now_utc, trigger="test")
        assert apply_calls and apply_calls[-1].get("lwt_offset") == -4

        # kill switch: same plan under the tier rule writes the −2 setback
        apply_calls.clear()
        monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", "tier")
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t2.db"))  # fresh DB
        db.init_db()
        assert _write_lwt_preheat_actions(plan_date, plan, []) >= 1
        rows = db.get_actions_for_plan_date(plan_date, device="daikin")
        sm._FIRST_APPLIED_SESSION.clear()
        sm._reconcile_daikin_actions(rows, MagicMock(), dev, now_utc, trigger="test")
        assert apply_calls and apply_calls[-1].get("lwt_offset") == -2


# ── W3 in the LP: peak-band floor + ceiling clamp ────────────────────────────


def _cosy_day(d: date):
    starts, prices = [], []
    for h in range(24):
        for m in (0, 30):
            starts.append(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).astimezone(UTC))
            if 4 <= h < 7 or 13 <= h < 16 or h >= 22:
                prices.append(COSY_CHEAP)
            elif 16 <= h < 19:
                prices.append(COSY_PEAK)
            else:
                prices.append(COSY_DAY)
    return starts, prices


def _solve_w3(monkeypatch, *, source: str):
    monkeypatch.setattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 25)
    monkeypatch.setattr(config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LP_W3_PEAK_COAST_DELTA_C", 1.0, raising=False)
    monkeypatch.setattr(config, "THERMAL_LEARNED_VALUES_ENABLED", False, raising=False)
    # UA small enough that the pump's ceiling (≈2.2 kW thermal at 10 °C) can
    # hold the house, so WHEN to heat is a real choice for the LP.
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 150.0, raising=False)
    monkeypatch.setattr(config, "BUILDING_THERMAL_MASS_KWH_PER_K", 12.0, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active")
    monkeypatch.setitem(config._overrides, "DAIKIN_LWT_SOURCE", source)
    starts, prices = _cosy_day(DAY)
    n = len(starts)
    w = WeatherLpSeries(
        slot_starts_utc=starts, temperature_outdoor_c=[10.0] * n, shortwave_radiation_wm2=[50.0] * n,
        cloud_cover_pct=[80.0] * n, pv_kwh_per_slot=[0.05] * n, cop_space=[3.0] * n, cop_dhw=[2.6] * n,
    )
    plan = solve_lp(
        slot_starts_utc=starts, price_pence=prices, base_load_kwh=[0.4] * n, weather=w,
        initial=LpInitialState(soc_kwh=5.0, tank_temp_c=48.0, indoor_temp_c=20.8), tz=TZ,
    )
    assert plan.ok, plan.status
    return plan, prices


def test_cosy_plan_heats_in_cheap_bands_and_coasts_in_peak(monkeypatch):
    plan, prices = _solve_w3(monkeypatch, source="tier")
    assert plan.indoor_temp_c and len(plan.indoor_temp_c) == len(prices) + 1
    assert len(plan.comfort_slack_c) == len(prices) and w3_trajectory_plausible(plan)[0]
    peak = [i for i, p in enumerate(prices) if p == COSY_PEAK]
    cheap_pm = [i for i, p in enumerate(prices) if p == COSY_CHEAP and 26 <= i < 32]  # 13-16 local
    e_peak = sum(plan.space_electric_kwh[i] for i in peak)
    e_cheap = sum(plan.space_electric_kwh[i] for i in cheap_pm)
    assert e_cheap > e_peak
    # the peak-band floor is setpoint − 1 °C: the house may dip below 21 but not below 20 (minus tiny slack)
    assert min(plan.indoor_temp_c[i + 1] for i in peak) >= 19.9


def test_space_ceiling_respects_lp_offset_clamp(monkeypatch):
    plan, _ = _solve_w3(monkeypatch, source="lp")
    assert max(plan.lwt_offset_c) <= 5.0 + 1e-6


def test_heating_ramp_is_never_dropped():
    from src.scheduler.lp_dispatch import smooth_lp_offsets

    for ramp in ([1, 2, 3, 4, 5], [1, 2, 3, 4, 5, 6, 7, 8], [5, 4, 3, 2, 1], [2, 4, 6, 8]):
        out = smooth_lp_offsets(ramp, 4)
        assert all(v and v > 0 for v in out), (ramp, out)
        assert abs(sum(out) - sum(ramp)) <= len(ramp)        # energy ~ preserved
    # with masks/bands too (band change inside a heating run)
    out = smooth_lp_offsets([2, 2, 3, 3, 5, 5], 4, heating=[True] * 6, bands=["cheap"] * 3 + ["standard"] * 3)
    assert all(v > 0 for v in out)
    # only a WHOLE short run is dropped (legacy rule)
    assert smooth_lp_offsets([3, 4, 0, 0, 5, 5, 5, 5], 4) == [0, 0, 0, 0, 5, 5, 5, 5]
