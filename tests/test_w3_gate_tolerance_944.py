"""#944 — W3 plausibility gate tolerates a small, economically chosen undershoot."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from src.config import config
from src.scheduler import lp_dispatch as ld
from src.scheduler.lp_dispatch import _lp_offsets, _tier_offsets, w3_trajectory_plausible
from src.scheduler.lp_optimizer import LpPlan


def _plan(slack=None, em=None, hi=None, n=12):
    t0 = datetime(2026, 11, 4, tzinfo=UTC)
    p = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
               slot_starts_utc=[t0 + timedelta(minutes=30 * i) for i in range(n)],
               price_pence=[25.45] * n, temp_outdoor_c=[5.0] * n,
               cheap_threshold_pence=18.97, peak_threshold_pence=31.81,
               tariff_structure_kind="banded", price_band=["standard"] * n)
    p.lwt_offset_c = [4.0] * n
    p.space_electric_kwh = [0.3] * n
    p.indoor_temp_c = [20.87] * (n + 1)
    p.comfort_slack_c = list(slack) if slack is not None else [0.0] * n
    p.comfort_slack_hi_c = list(hi) if hi is not None else [0.0] * n
    p.comfort_slack_emergency_c = list(em) if em is not None else [0.0] * n
    p.w3_emergency_floor_c = 20.0
    p.w3_night_floor_c = 20.0
    p.w3_ceiling_c = 23.0
    p.w3_emitter_tau_min = 0.0
    return p


def test_defaults_are_runtime_settings():
    from src.runtime_settings import SCHEMA
    assert float(config.LP_W3_SLACK_TOL_C) == 0.5
    assert int(config.LP_W3_MAX_SLACK_SLOTS) == 4
    assert "LP_W3_SLACK_TOL_C" in SCHEMA and "LP_W3_MAX_SLACK_SLOTS" in SCHEMA


def test_seven_slots_at_013_slack_accepted():
    assert w3_trajectory_plausible(_plan(slack=[0.13] * 7 + [0.0] * 5)) == (True, "ok")


def test_old_tolerance_would_have_rejected_it(monkeypatch):
    monkeypatch.setattr(config, "LP_W3_SLACK_TOL_C", 0.1)
    ok, reason = w3_trajectory_plausible(_plan(slack=[0.13] * 7 + [0.0] * 5))
    assert not ok and reason.startswith("comfort_slack:7_slots")


def test_model_blowup_still_rejected():
    plan = _plan(slack=[0.0, 0.6, 0.8, 1.0, 1.3, 1.6, 2.0, 2.4, 2.8, 3.2, 3.6, 4.0])
    plan.indoor_temp_c = [21.0 - 0.8 * i for i in range(13)]
    assert w3_trajectory_plausible(plan)[0] is False


def test_slack_above_tolerance_in_many_slots_rejected():
    ok, reason = w3_trajectory_plausible(_plan(slack=[0.6] * 5 + [0.0] * 7))
    assert not ok and reason.startswith("comfort_slack:5_slots")


def test_emergency_check_keeps_tight_tolerance():
    # 0.2 is inside the new floor tolerance but is an emergency-floor breach
    assert w3_trajectory_plausible(_plan(em=[0.2] + [0.0] * 11)) == (False, "emergency_slack")


def test_ceiling_check_keeps_tight_tolerance():
    ok, reason = w3_trajectory_plausible(_plan(hi=[0.2] * 6 + [0.0] * 6))
    assert not ok and reason.startswith("comfort_slack:6_slots")


def test_wire_gate_pass_writes_lp_offsets_not_tier():
    plan = _plan(slack=[0.13] * 7 + [0.0] * 5)
    lp = _lp_offsets(plan)
    assert lp is not None and set(lp) == {4}
    assert _tier_offsets(plan, [], None) != lp  # standard band: tier says 0


def test_settings_round_trip(monkeypatch, tmp_path):
    import pytest

    from src import db
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()

    from src import runtime_settings as rs
    spec = rs.SCHEMA["LP_W3_SLACK_TOL_C"]
    assert (spec.min_value, spec.max_value) == (0.0, 2.0)
    rs.set_setting("LP_W3_SLACK_TOL_C", 0.3)
    rs.set_setting("LP_W3_MAX_SLACK_SLOTS", 6)
    assert rs.get_setting("LP_W3_SLACK_TOL_C") == 0.3
    assert rs.get_setting("LP_W3_MAX_SLACK_SLOTS") == 6
    # the gate reads the live setting: 6 slots at 0.4 > 0.3 are allowed up to 6 slots
    assert w3_trajectory_plausible(_plan(slack=[0.4] * 6 + [0.0] * 6)) == (True, "ok")
    assert w3_trajectory_plausible(_plan(slack=[0.4] * 7 + [0.0] * 5))[0] is False
    with pytest.raises(rs.SettingValidationError):
        rs.set_setting("LP_W3_SLACK_TOL_C", 5.0)


def test_gate_snapshot_and_effective_source_kv(monkeypatch):
    snap = ld.w3_gate_snapshot(_plan(slack=[0.13] * 7 + [0.0] * 5))
    assert snap == {"ok": True, "reason": "ok", "slack_tol_c": 0.5, "max_slack_slots": 4}
    store: dict[str, str] = {}
    monkeypatch.setattr(ld.db, "set_kv", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(ld.db, "get_kv", lambda k, d=None: store.get(k, d))
    ld.record_lwt_source_effective("tier", "comfort_slack:6_slots>4")
    eff = ld.read_lwt_source_effective()
    assert eff["source"] == "tier" and eff["reason"] == "comfort_slack:6_slots>4"
    json.dumps(eff)
