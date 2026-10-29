"""#876 story 3 - review fixes: intent-aware verdict, Sunday cadence, refusals, leaks."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import auto_tune, db
from src import auto_tune_judge as judge
from src import runtime_settings as rts
from src import tuning_review as tr
from src.config import config
from tests.test_auto_tune_judge_876 import (  # noqa: F401 - fixtures + helpers are shared
    APPLIED,
    KEY,
    LON,
    NOW_DUE,
    Replays,
    _action,
    _agg,
    _card,
    _change,
    _cn,
    _cold,
    _env,
    _resid,
    _row,
    _run,
    stub_comfort,
)

D = auto_tune.derive_intent


# ------------------------------------------------------------------ intent
def test_intent_derivation():
    assert D(KEY, "comfort_feedback", 20.0, 20.5) == "comfort_up"
    assert D(KEY, "comfort_feedback", 20.0, 19.5) == "comfort_down"
    assert D("LP_W3_PEAK_COAST_DELTA_C", "comfort_feedback", 0.5, 0.0) == "comfort_up"
    assert D("LP_W3_PEAK_COAST_DELTA_C", "comfort_feedback", 0.5, 1.0) == "comfort_down"
    assert D("INDOOR_SETPOINT_C", "comfort_feedback", 21.0, 21.5) == "comfort_up"
    assert D("LP_W3_EMITTER_TAU_MIN", "learned_param", 0, 10) == "learned"
    assert D(KEY, "tuning_review", 20.0, 19.5) == "saving"
    assert D("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "plan_pattern", "p90", "p75") == "saving"


def test_propose_change_stores_and_exposes_intent():
    rts.set_setting(KEY, 20.0, actor="vacation_end")
    res = auto_tune.propose_change(KEY, 19.5, "tuning_review", intent="trade_off",
                                   expected_comfort_hours=1.5, now=NOW_DUE)
    assert res["applied"] and res["intent"] == "trade_off"
    row = _row(res["id"])
    assert row["intent"] == "trade_off" and row["evidence"]["expected_comfort_hours"] == 1.5
    assert auto_tune.ledger_state(KEY)["intent"] == "trade_off"
    assert auto_tune.ledger_summary(5)["rows"][0]["intent"] == "trade_off"
    assert auto_tune.propose_change(KEY, 19.0, "tuning_review", intent="bogus")["reason"] == "invalid"


def test_propose_change_derives_intent_when_missing():
    rts.set_setting(KEY, 20.0, actor="vacation_end")
    res = auto_tune.propose_change(KEY, 20.5, "comfort_feedback", now=NOW_DUE)
    assert res["applied"] and _row(res["id"])["intent"] == "comfort_up"


# ---------------------------------------------------------- decide per intent
def test_decide_trade_off():
    ok = _cn(0, 0)
    d, _r, det = judge.decide(_agg(700), _agg(690, hours=1.4), ok, 7, intent="trade_off", expected_comfort_hours=1.0)
    assert d == "keep" and det["hours_band"] == 1.5
    assert judge.decide(_agg(700), _agg(690, hours=1.6), ok, 7, intent="trade_off",
                        expected_comfort_hours=1.0)[0] == "rollback"
    d, rule, _ = judge.decide(_agg(700), _agg(697), ok, 7, intent="trade_off", expected_comfort_hours=1.0)
    assert d == "rollback" and rule == "failed:cost_not_saved"       # a trade-off must still pay
    assert judge.decide(_agg(700), _agg(690, hours=1.4), ok, 7, intent="trade_off")[0] == "keep"  # default 2x tol
    assert judge.decide(_agg(700), _agg(690), _cn(3, 0), 7, intent="trade_off")[1] == "failed:cold_feedback"


def test_decide_comfort_up():
    ok = _cn(0, 0)
    assert judge.decide(_agg(700), _agg(709.9), ok, 7, intent="comfort_up")[0] == "keep"
    assert judge.decide(_agg(700), _agg(709.9), ok, 7, intent="saving")[0] == "rollback"
    assert judge.decide(_agg(700), _agg(710.1), ok, 7, intent="comfort_up")[1] == "failed:cost"
    assert judge.decide(_agg(700, hours=1.0), _agg(700, hours=1.1), ok, 7,
                        intent="comfort_up")[1] == "failed:hours_below_floor"
    assert judge.decide(_agg(700, hours=1.0), _agg(700, hours=0.0), ok, 7, intent="comfort_up")[0] == "keep"
    assert judge.decide(_agg(), _agg(), _cn(2, 1), 7, intent="comfort_up")[1] == "failed:cold_feedback"
    assert judge.decide(_agg(), _agg(), _cn(1, 1), 7, intent="comfort_up")[0] == "keep"


def test_decide_comfort_down_and_learned():
    ok = _cn(0, 0)
    assert judge.decide(_agg(700), _agg(690), ok, 7, intent="comfort_down")[0] == "keep"
    assert judge.decide(_agg(700), _agg(700.5), ok, 7, intent="comfort_down")[1] == "failed:cost"
    assert judge.decide(_agg(700), _agg(690), _cn(3, 0), 7, intent="comfort_down")[1] == "failed:cold_feedback"
    pe = {"before": 0.4, "after": 0.5}
    assert judge.decide(_agg(700), _agg(704), ok, 7, intent="learned", pred_err=pe)[0] == "keep"
    assert judge.decide(_agg(700), _agg(706), ok, 7, intent="learned", pred_err=pe)[1] == "failed:cost"
    assert judge.decide(_agg(), _agg(), ok, 7, intent="learned",
                        pred_err={"before": 0.4, "after": 0.7})[1] == "failed:pred_err"
    assert judge.decide(_agg(), _agg(), ok, 7, intent="learned",
                        pred_err={"before": 0.1, "after": 0.25})[0] == "keep"   # +150 % but < +0.2 C
    assert judge.decide(_agg(), _agg(), ok, 7, intent="learned",
                        pred_err={"before": None, "after": 0.9})[0] == "keep"


def test_cold_clause_normalises_away_days_in_pre_week():
    c = {"after": 4, "before": 2, "before_normalised": 3.5}   # pre-week had 3 away days
    assert judge.decide(_agg(), _agg(), c, 7)[0] == "keep"
    assert judge.decide(_agg(), _agg(), {**c, "after": 5}, 7)[1] == "failed:cold_feedback"   # 5 > 3.5 + 1
    assert judge.decide(_agg(), _agg(), {"after": 9, "before": 0, "before_normalised": None}, 7)[0] == "keep"


def test_cold_counts_normalise_and_windows():
    cid = _change()
    _cold(APPLIED - timedelta(days=6, hours=23))      # inside the 7x24 h before
    _cold(APPLIED - timedelta(days=7, hours=1))       # outside
    _cold(APPLIED - timedelta(hours=3))               # before (same day, before applied_at)
    _cold(APPLIED + timedelta(hours=3))               # after (same day, after applied_at)
    c = judge._cold_counts(_row(cid), NOW_DUE, set(), date(2026, 10, 18))
    assert c["after"] == 1 and c["before"] == 2 and c["before_normalised"] == 2.0
    away = {"2026-10-05", "2026-10-06", "2026-10-07"}
    c = judge._cold_counts(_row(cid), NOW_DUE, away, date(2026, 10, 18))
    assert c["pre_week_away_days"] == 3 and c["before_normalised"] == pytest.approx(c["before"] * 7 / 4)


# ------------------------------------------------- end to end per intent
def test_comfort_feedback_both_directions_end_to_end(stub_comfort):
    # a "cold" raise of the night floor costs money by design: comfort_up, kept
    cid = _change(old=20.0, new=20.5, source="comfort_feedback")
    out = _run(Replays(lambda v: 100.0 + 0.5 * v))
    assert out["status"] == "kept" and _row(cid)["evidence"]["intent"] == "comfort_up"
    # a "hot" lowering sits below the OLD floor by design: judged under the NEW floors
    cid2 = _change(old=20.5, new=20.0, source="comfort_feedback")
    out = _run(Replays(lambda v: 100.0 + v, hours=lambda v, f: 3.0 if v < f else 0.0))
    ev = _row(cid2)["evidence"]
    assert out["status"] == "kept" and ev["intent"] == "comfort_down" and ev["yardstick"] == "new_value"


def test_saving_is_judged_under_the_old_yardstick(stub_comfort):
    cid = _change(intent="saving")
    out = _run(Replays(lambda v: 100.0, hours=lambda v, f: 3.0 if v < f else 0.0))
    assert out["status"] == "rolled_back" and _row(cid)["evidence"]["yardstick"] == "old_value"


def test_trade_off_uses_expected_comfort_hours(stub_comfort):
    cid = _change(intent="trade_off", evidence={"expected_comfort_hours": 4.0})
    out = _run(Replays(lambda v: 100.0 + 10.0 * v, hours=lambda v, f: 0.5 if v < f else 0.0))
    # 7 days x 0.5 h = 3.5 h worse <= 4.0 + 0.5 band; saves 5 p/day
    assert out["status"] == "kept" and _row(cid)["evidence"]["expected_comfort_hours"] == 4.0


def test_learned_param_verdict_rolls_back_on_pred_err(stub_comfort):
    cid = _change("LP_W3_EMITTER_TAU_MIN", 0.0, 10.0, source="learned_param")
    for i in range(1, 8):
        _resid((date(2026, 10, 10) - timedelta(days=i)).isoformat(), 0.4)
    for i, e in enumerate([1.0, 0.1, 1.0, 0.1, 1.0, 0.1, 1.0]):   # no 2 consecutive bad days: tripwire quiet
        _resid((date(2026, 10, 10) + timedelta(days=i)).isoformat(), e)
    out = _run(Replays(lambda v: 100.0))
    assert out["status"] == "rolled_back"
    ev = _row(cid)["evidence"]
    assert ev["rule"] == "failed:pred_err" and ev["intent"] == "learned"


# --------------------------------------------------------- B2 Sunday cadence
def test_sunday_timeline_ledger_free_before_comfort_job(stub_comfort):
    sun = datetime(2026, 11, 1, 9, 20, tzinfo=UTC)       # Sunday, GMT
    assert sun.weekday() == 6
    cid = _change(applied=sun)
    assert judge.verdict_job(datetime(2026, 11, 7, 7, 40, 30, tzinfo=UTC))["status"] == "not_due"
    next_sun = datetime(2026, 11, 8, 7, 40, 30, tzinfo=UTC)
    out = judge.verdict_job(next_sun, replay=Replays(lambda v: 100.0),
                            list_runs=lambda d: ["r"] if "2026-11-01" <= d <= "2026-11-07" else [])
    assert out["status"] == "kept"
    ev = _row(cid)["evidence"]
    assert ev["window"] == ["2026-11-01", "2026-11-07"] and len(ev["days_replayed"]) == 7
    assert db.get_active_auto_tune_change() is None          # ledger free before the 08:45 comfort job
    assert judge.budget_ends_before_comfort_job()
    rts.set_setting(KEY, 19.5, actor="vacation_end")


def test_budget_clamp_and_comfort_job_guard(monkeypatch, caplog):
    monkeypatch.setenv("AUTO_TUNE_VERDICT_BUDGET_SECONDS", "999999")
    assert judge.verdict_budget_seconds() == 45 * 60
    assert judge.budget_ends_before_comfort_job() is True              # 07:40 + 45 min = 08:25
    monkeypatch.setattr(config, "COSY_SCORECARD_HOUR_LOCAL", 8, raising=False)
    assert judge.budget_ends_before_comfort_job() is False             # 08:10 + 45 min
    sched = SimpleNamespace(add_job=lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        judge.register_jobs(sched, LON)
    assert any("comfort job" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------- S1 refusals
def test_tripwire_rollback_refused_closes_row_and_notifies_once(_env):
    cid = _change()
    rts.set_setting(KEY, 21.0, actor="vacation_end")   # moved by an internal actor: rollback refuses
    _action("lwt_emergency", APPLIED + timedelta(days=1))
    out = judge.tripwire_job(NOW_DUE)
    assert out["status"] == "tripped" and out["rollback"]["rolled_back"] is False
    row = _row(cid)
    assert row["status"] == "superseded_manual" and row["evidence"]["reason"] == "rollback_refused:superseded"
    assert db.get_active_auto_tune_change() is None
    assert len(_env.risk) == 1 and _env.risk[0][1]["warning_key"] == f"auto_tune_rollback_refused_{cid}"
    assert db.get_action_logs(device="auto_tune", action="auto_tune_rollback_refused", limit=5)


def test_verdict_rollback_refused_is_terminal(stub_comfort, _env):
    cid = _change()
    rts.set_setting(KEY, 21.0, actor="vacation_end")
    out = _run(Replays(lambda v: 100.0 - 20.0 * v))
    assert out["status"] == "rollback_refused" and _row(cid)["status"] == "superseded_manual"
    assert len(_env.risk) == 1
    assert judge.verdict_job(NOW_DUE)["status"] == "no_change"       # no daily replay loop


def test_keep_refused_is_not_reported_as_kept(stub_comfort, monkeypatch):
    _change()
    monkeypatch.setattr(auto_tune, "keep", lambda *a, **k: False)
    assert _run(Replays(lambda v: 100.0))["status"] == "keep_refused"


def test_every_verdict_outcome_writes_action_log(stub_comfort):
    _change()
    _run(Replays(lambda v: 100.0))
    rows = db.get_action_logs(device="auto_tune", action="auto_tune_verdict", limit=5)
    assert rows and rows[0]["params"]["status"] == "kept"


def test_rollback_clears_pinned_override(stub_comfort):
    cid = _change()
    setattr(config, KEY, 19.5)            # runtime knobs are properties: this pins an in-memory override
    assert config.has_override(KEY)
    assert auto_tune.rollback(cid, reason="x")["rolled_back"] is True
    assert not config.has_override(KEY) and float(rts.get_setting(KEY)) == 20.0


# ------------------------------------------------------------ S2 exceptions
def test_day_comfort_exception_defers_and_leaves_no_override(stub_comfort, monkeypatch):
    cid = _change()

    def boom(day, yard):
        raise RuntimeError("comfort exploded")
    monkeypatch.setattr(tr, "day_comfort", boom)
    out = _run(Replays(lambda v: 100.0))
    assert out["status"] == "deferred" and out["deferred"] == 1 and "replay_error" in out["reason"]
    assert _row(cid)["evidence"]["deferred"] == 1 and _row(cid)["status"] == "evaluating"
    assert not config.has_override(KEY) and tr._running is False


def test_aggregate_exception_defers(stub_comfort, monkeypatch):
    _change()
    monkeypatch.setattr(tr, "_aggregate", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    assert _run(Replays(lambda v: 100.0))["status"] == "deferred"
    assert tr._running is False and not config.has_override(KEY)


# --------------------------------------------------------- S3 / S4 / S5 / S7
def test_peak_import_only_on_loosening_and_above_baseline():
    c = _change("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p75", "p90")        # more insurance: no wire
    _card("2026-10-13", peak=9.0)
    assert judge.classify(_row(c)) == "tighten" and judge.tripwire_check(_row(c), NOW_DUE) is None
    db.update_auto_tune_change(c, status="superseded_manual")


def test_peak_import_cold_snap_guard():
    base = dict(peak_import_kwh_per_day=2.0, n_days=7, shower_shortfall_days=0)
    c = _change("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p90", "p75", baseline=base)
    _card("2026-10-13", peak=2.8)       # > limit 1.0 but not > baseline 2.0 + 1.0
    assert judge.tripwire_check(_row(c), NOW_DUE) is None
    _card("2026-10-14", peak=3.2)
    assert judge.tripwire_check(_row(c), NOW_DUE)["name"] == "peak_import"


def test_pred_err_needs_ratio_and_absolute_margin():
    c = _change("LP_W3_EMITTER_TAU_MIN", 0.0, 10.0)
    for i in range(1, 8):
        _resid((date(2026, 10, 10) - timedelta(days=i)).isoformat(), -0.1)   # signed: abs() is used
    _resid("2026-10-11", 0.25)          # +150 % but only +0.15 C: not enough
    _resid("2026-10-12", 0.25)
    assert judge.tripwire_check(_row(c), NOW_DUE) is None
    _resid("2026-10-13", 0.35)          # >= +0.2 C and > 1.5x
    _resid("2026-10-14", -0.4)
    assert judge.tripwire_check(_row(c), NOW_DUE)["name"] == "pred_err"


def test_double_tap_and_per_room_double_report_count_once():
    c = _change()
    _cold(APPLIED + timedelta(days=1))
    _cold(APPLIED + timedelta(days=1, minutes=5))
    _cold(APPLIED + timedelta(days=1, minutes=40))
    assert judge.tripwire_check(_row(c), NOW_DUE) is None
    _cold(APPLIED + timedelta(days=1, hours=2))
    assert judge.tripwire_check(_row(c), NOW_DUE)["name"] == "cold_feedback"


def test_fire_not_pushed_out_by_skipped_ticks():
    c = _change()
    _action("lwt_comfort_backstop", APPLIED + timedelta(hours=2), result="ok")
    for i in range(520):
        _action("lwt_comfort_backstop", APPLIED + timedelta(hours=3, seconds=i), result="skipped")
    assert judge.tripwire_check(_row(c), NOW_DUE)["name"] == "comfort_backstop"


def test_warm_side_ceiling_raise_tripwires():
    c = _change("LP_W3_CEILING_C", 23.0, 23.5)
    assert judge.classify(_row(c)) == "warm_side"
    assert judge.tripwire_check(_row(c), NOW_DUE) is None
    db.insert_comfort_feedback(verdict="hot", now=APPLIED + timedelta(days=1))
    assert judge.tripwire_check(_row(c), NOW_DUE) is None
    db.insert_comfort_feedback(verdict="hot", now=APPLIED + timedelta(days=2))
    assert judge.tripwire_check(_row(c), NOW_DUE)["name"] == "hot_feedback"


def test_warm_backstop_trips_ceiling_raise():
    c = _change("LP_W3_CEILING_C", 23.0, 23.5)
    _action("lwt_warm_backstop", APPLIED + timedelta(days=1), result="ok")
    assert judge.tripwire_check(_row(c), NOW_DUE)["name"] == "warm_backstop"


def test_dhw_tripwire_respects_baseline_shortfall_rate():
    base = dict(n_days=7, shower_shortfall_days=7, peak_import_kwh_per_day=0.0)  # misses every day already
    c = _change("DHW_TEMP_NORMAL_C", 45.0, 44.0, baseline=base)
    _card("2026-10-12", tank_bad=True)
    assert judge.tripwire_check(_row(c), NOW_DUE) is None      # 1 miss <= expected (rate 1/day x 1 day)
    db.update_auto_tune_change(c, status="superseded_manual")
    c2 = _change("DHW_TEMP_NORMAL_C", 45.0, 44.0)               # baseline: no misses
    assert judge.tripwire_check(_row(c2), NOW_DUE)["name"] == "shower_floor"


def test_days_failed_and_budget_cut_are_separate(stub_comfort, monkeypatch):
    _change()
    ticks = iter(range(10, 100_000))
    monkeypatch.setattr(judge.time, "monotonic", lambda: next(ticks))
    monkeypatch.setenv("AUTO_TUNE_VERDICT_BUDGET_SECONDS", "0")
    out = _run(Replays(lambda v: 100.0))
    ev = _row(out["id"])["evidence"]
    assert out["status"] == "deferred" and ev["partial"] is True
    assert len(ev["days_budget_cut"]) == 7 and ev["days_failed"] == []


# ------------------------------------------------------------- S6 workbench
def test_workbench_simulate_409_while_replay_runs():
    from src.api.routers import workbench
    tr._acquire()
    try:
        with pytest.raises(HTTPException) as e:
            asyncio.run(workbench.workbench_simulate({"overrides": {}}))
        assert e.value.status_code == 409
    finally:
        tr._release()
