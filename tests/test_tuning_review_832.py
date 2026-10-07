"""#832 weekly fine-tuning review — suggestions only."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import tuning_review as tr
from src.config import config


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    p = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", p)
    monkeypatch.setattr(config, "DB_PATH", p, raising=False)
    from src import db
    db.init_db()
    yield


def _ctrl(cost=1000.0, hours=2.0, shower=0.0):
    return {"cost_p": cost, "hours_below": hours, "shower_day_below": shower, "n_days": 7.0}


def _var(key, value, cost, hours, shower=0.0):
    return {"key": key, "value": value, "current_value": 0, "cost_p": cost,
            "hours_below": hours, "shower_day_below": shower}


def test_ranking_rules():
    rows = tr.rank_variants(_ctrl(), [
        _var("A", 1, 980, 2.2),        # -20p, comfort within tol -> recommended
        _var("B", 1, 970, 5.0),        # saves, comfort worse -> trade-off
        _var("C", 1, 998, 0.0),        # saves 2p only -> dropped, but zero hours
        _var("D", 1, 1010, 2.0),       # costs -> dropped
        _var("E", 1, 985, 2.0, 1.0),   # extra shower day -> trade-off
    ], min_saving_p=5, tol_h=0.5)
    by = {r["key"]: r for r in rows}
    assert by["A"]["verdict"] == "recommended"
    assert by["B"]["verdict"] == "trade-off"
    assert by["E"]["verdict"] == "trade-off"
    assert "D" not in by
    assert by["C"]["verdict"] == "comfort-first"  # control has hours-below > 0
    assert rows[0]["verdict"] == "recommended"


def test_no_comfort_first_when_control_is_clean():
    rows = tr.rank_variants(_ctrl(hours=0.0), [_var("C", 1, 998, 0.0)], min_saving_p=5)
    assert rows == []


def test_best_variant_per_key():
    rows = tr.rank_variants(_ctrl(hours=0.0), [_var("A", 1, 980, 0.0), _var("A", 2, 960, 0.0)])
    assert [r["value"] for r in rows] == [2]


def test_candidate_variants_bounds_and_enum():
    v = tr.candidate_variants({
        "LP_W3_NIGHT_FLOOR_C": 14.0, "LP_W3_PEAK_COAST_DELTA_C": 1.0, "INDOOR_SETPOINT_C": 21.0,
        "DHW_TEMP_NORMAL_C": 45.0, "LP_LOAD_EXPENSIVE_BAND_QUANTILE": "p75",
    })
    got = {(x["key"], x["value"]) for x in v}
    assert ("LP_W3_NIGHT_FLOOR_C", 13.5) not in got and ("LP_W3_NIGHT_FLOOR_C", 14.5) in got
    assert ("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p90") in got
    assert len(v) == 1 + 2 + 2 + 2 + 1


def test_payload_shape():
    p = tr.put_payload("INDOOR_SETPOINT_C", 20.5)
    assert p["body"] == {"value": 20.5}
    assert p["apply"]["path"] == "/api/v1/settings/INDOOR_SETPOINT_C"
    assert p["simulate"]["path"].endswith("/INDOOR_SETPOINT_C/simulate")
    assert "X-Simulation-Id" in p["apply"]["headers"]


def test_external_comfort_signal_is_none():
    assert tr.external_comfort_signal("2026-10-01") is None


def _fake_day(plan_date, night_temp):
    st = datetime.fromisoformat(plan_date + "T00:00:00+00:00")
    slots = [st + timedelta(minutes=30 * i) for i in range(48)]
    plan = SimpleNamespace(slot_starts_utc=slots, indoor_temp_c=[night_temp] * 49,
                           tank_temp_c=[50.0] * 49, price_band=[])
    run = SimpleNamespace(_replayed_plan=plan)
    return SimpleNamespace(ok=True, plan_date=plan_date, runs=[run],
                           recalc_timestamps_utc=[st.isoformat()],
                           total_replayed_cost_p=100.0)


def test_day_comfort_counts_night_and_peak(monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    yard = {"night_floor_c": 17.5, "peak_floor_c": 20.0, "setpoint_c": 21.0, "night_start_h": 22,
            "night_end_h": 7, "shower_floor_c": 45.0}
    c = tr.day_comfort(_fake_day("2026-10-01", 16.0), yard)
    assert c["night_below_h"] == 9.0      # 22-07 UTC
    assert c["peak_below_h"] == 15.0      # dynamic tariff: setpoint is the daytime floor (07-22)
    assert c["shower_short_c"] == 0.0


def test_replay_loop_never_writes_settings_and_restores(monkeypatch):
    from src import runtime_settings
    calls = []
    monkeypatch.setattr(runtime_settings, "set_setting", lambda *a, **k: calls.append(("set", a)))
    orig_rt_set = type(config)._rt_set
    monkeypatch.setattr(type(config), "_rt_set", lambda self, k, v: (calls.append(("rt", k)), orig_rt_set(self, k, v)))
    seen = []
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)

    def fake_replay(d, cadence, mode):
        seen.append((d, cadence, mode, config.INDOOR_SETPOINT_C, config.LP_W3_NIGHT_FLOOR_C))
        r = _fake_day(d, 18.0)
        # a lower setpoint saves money
        r.total_replayed_cost_p = 100.0 + 10.0 * float(config.INDOOR_SETPOINT_C)
        return r

    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    before = config.INDOOR_SETPOINT_C
    res = tr.run_review(end_day=date(2026, 10, 6), dry_run=True, replay=fake_replay,
                        list_runs=lambda d: [1], cadence="stride:5")
    assert res["status"] == "ok" and len(res["days"]) == 7
    assert all(s[1] == "stride:5" and s[2] == "forward" for s in seen)
    n_var = len(tr.candidate_variants(tr.current_values()))
    assert len(seen) == 7 * (1 + n_var)
    assert config.INDOOR_SETPOINT_C == before
    assert not config.has_override("INDOOR_SETPOINT_C")
    # patched_config's pinning is undone; the review itself never calls set_setting
    assert not [c for c in calls if c[0] == "set"]
    sp = [r for r in res["rows"] if r["key"] == "INDOOR_SETPOINT_C"]
    assert sp and sp[0]["suggested_value"] < sp[0]["current_value"]
    assert sp[0]["delta_pence_per_week"] < 0
    # dry_run: nothing persisted
    from src import db
    assert db.list_tuning_suggestions(4) == []


def test_skips_with_too_few_days(monkeypatch):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    res = tr.run_review(end_day=date(2026, 10, 6), dry_run=True,
                        replay=lambda *a, **k: pytest.fail("must not replay"),
                        list_runs=lambda d: [1] if d.endswith(("-05", "-06")) else [])
    assert res["status"] == "skipped" and res["rows"] == []


def test_persist_and_api_shape_and_admin_gating(monkeypatch, tmp_path):
    from src import db
    db.save_tuning_suggestions([{
        "week_start": "2026-09-30", "key": "INDOOR_SETPOINT_C", "current_value": 21.0,
        "suggested_value": 20.5, "delta_pence_per_week": -12.0, "delta_comfort_hours": 0.0,
        "verdict": "recommended", "payload": tr.put_payload("INDOOR_SETPOINT_C", 20.5),
    }])
    from src.api.main import app
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", True, raising=False)
    monkeypatch.setattr(config, "HEM_ADMIN_TOKEN", "adm", raising=False)
    monkeypatch.setattr(config, "HEM_UI_TOKEN_FILE", str(tmp_path / ".u"), raising=False)
    monkeypatch.setattr(config, "HEM_OPENCLAW_TOKEN_FILE", str(tmp_path / ".o"), raising=False)
    with TestClient(app) as c:
        r = c.get("/api/v1/tuning/suggestions?weeks=4")
        assert r.status_code == 200
        s = r.json()["suggestions"]
        assert s[0]["key"] == "INDOOR_SETPOINT_C" and s[0]["payload"]["body"] == {"value": 20.5}
        monkeypatch.setattr(tr, "run_review", lambda **k: {"status": "ok", "rows": []})
        assert c.post("/api/v1/tuning/run").status_code == 401
        ok = c.post("/api/v1/tuning/run", headers={"Authorization": "Bearer adm"})
        assert ok.status_code == 200 and ok.json()["status"] == "ok"
        def busy(**k):
            raise tr.ReviewBusy("busy")
        monkeypatch.setattr(tr, "run_review", busy)
        assert c.post("/api/v1/tuning/run", headers={"Authorization": "Bearer adm"}).status_code == 409


def test_single_flight_lock_has_no_ttl():
    tr._acquire()
    try:
        tr._running  # held: no expiry, a second review is always refused
        with pytest.raises(tr.ReviewBusy):
            tr._acquire()
    finally:
        tr._release()
    tr._acquire(); tr._release()


def test_job_registered_and_muted_when_nothing_recommended(monkeypatch):
    import inspect
    from src.scheduler import runner
    src = inspect.getsource(runner)
    assert "tuning_review_weekly" in src and "TUNING_REVIEW_DOW" in src
    sent = []
    from src import notifier
    monkeypatch.setattr(notifier, "notify_tuning_review", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(tr, "run_review", lambda **k: {"status": "ok", "week_start": "w", "rows": [
        {"verdict": "trade-off", "key": "k", "current_value": 1, "suggested_value": 2,
         "delta_pence_per_week": -9, "delta_comfort_hours": 3}]})
    tr.weekly_review_job()
    assert sent == []
    monkeypatch.setattr(tr, "run_review", lambda **k: {"status": "ok", "week_start": "w", "rows": [
        {"verdict": "recommended", "key": "k", "current_value": 1, "suggested_value": 2,
         "delta_pence_per_week": -9, "delta_comfort_hours": 0}]})
    tr.weekly_review_job()
    assert len(sent) == 1


# --- replay harness: W3 seed (indoor_initial_c) ---------------------------------

def test_replay_seeds_w3_indoor_and_night_floor_binds(monkeypatch):
    from datetime import UTC, datetime
    from src import db
    from src.scheduler import lp_optimizer
    from src.scheduler.lp_replay import replay_run
    from tests import test_lp_replay as h

    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 200.0, raising=False)
    base = datetime(2026, 7, 1, 21, 0, tzinfo=UTC)  # 22:00 BST: whole horizon is night
    plan, slots, prices, load, initial, forecast = h._solve_baseline(16, base)
    orig_save = db.save_lp_snapshots

    def save_with_indoor(run_id, inputs_row, rows):
        inputs_row = {**inputs_row, "indoor_initial_c": 22.0}
        return orig_save(run_id, inputs_row, rows)

    monkeypatch.setattr(db, "save_lp_snapshots", save_with_indoor)
    run_id = h._persist_plan_as_run(plan, slots, prices, load, initial, forecast,
                                    run_at_utc=base, plan_date="2026-07-01")
    monkeypatch.setattr(db, "save_lp_snapshots", orig_save)

    low = replay_run(run_id, mode="forward")
    assert low.ok, low.error
    assert low._replayed_plan.indoor_temp_c, "W3 must engage when the snapshot carries indoor_initial_c"

    from src.scheduler.lp_overrides import patched_config
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5, raising=False)
    with patched_config({"LP_W3_NIGHT_FLOOR_C": 22.0, "INDOOR_SETPOINT_C": 24.0}):
        hi = replay_run(run_id, mode="forward")
    assert hi.ok, hi.error
    assert abs(hi.replayed_objective_pence - low.replayed_objective_pence) > 1e-6


def test_replay_without_snapshot_indoor_keeps_legacy_behaviour():
    from datetime import UTC, datetime
    from src.scheduler.lp_replay import replay_run
    from tests import test_lp_replay as h
    base = datetime(2026, 7, 1, 21, 0, tzinfo=UTC)
    plan, slots, prices, load, initial, forecast = h._solve_baseline(8, base)
    run_id = h._persist_plan_as_run(plan, slots, prices, load, initial, forecast,
                                    run_at_utc=base, plan_date="2026-07-01")
    r = replay_run(run_id, mode="forward")
    assert r.ok and not r._replayed_plan.indoor_temp_c


def test_state_at_propagates_indoor():
    from datetime import UTC, datetime
    from src.scheduler.lp_replay import _state_at
    st = datetime(2026, 7, 1, tzinfo=UTC)
    plan = SimpleNamespace(slot_starts_utc=[st + timedelta(minutes=30 * i) for i in range(4)],
                           soc_kwh=[1, 2, 3, 4, 5], tank_temp_c=[40, 41, 42, 43, 44],
                           indoor_temp_c=[20.0, 20.5, 21.0, 21.5, 22.0])
    assert _state_at(plan, st + timedelta(minutes=60)).indoor_temp_c == 21.0


def test_skips_when_passive_or_w3_off(monkeypatch):
    for attr, val in (("DAIKIN_CONTROL_MODE", "passive"), ("LP_W3_TIN_ENABLED", False)):
        monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
        monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
        monkeypatch.setattr(config, attr, val, raising=False)
        res = tr.run_review(end_day=date(2026, 10, 6), dry_run=True,
                            replay=lambda *a, **k: pytest.fail("must not replay"),
                            list_runs=lambda d: [1])
        assert res["status"] == "skipped" and "W3" in res["reason"]
        assert "context" in res


def test_hold_hours_not_a_knob():
    assert "DHW_DYNAMIC_BOOST_HOLD_HOURS" not in {k.key for k in tr.KNOBS}


def test_comfort_first_is_capped():
    rows = tr.rank_variants(_ctrl(hours=2.0), [_var("C", 1, 1100, 0.0)], min_saving_p=5)
    assert rows == []  # +100p/week for zero hours is not offered


def test_dynamic_tariff_uses_setpoint_as_daytime_floor(monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    yard = {"night_floor_c": 17.5, "peak_floor_c": 20.0, "setpoint_c": 21.0, "night_start_h": 22,
            "night_end_h": 7, "shower_floor_c": None}
    c = tr.day_comfort(_fake_day("2026-10-01", 20.5), yard)  # price_band empty
    assert c["peak_below_h"] == 15.0  # 07-22 below the 21 setpoint
