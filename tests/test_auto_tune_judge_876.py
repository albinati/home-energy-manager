"""#876 story 3 - the judge: tripwires, day-7 replay verdict, rollback."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import auto_tune, db, notifier
from src import auto_tune_judge as judge
from src import runtime_settings as rts
from src import tuning_review as tr
from src.config import config

KEY = "LP_W3_NIGHT_FLOOR_C"
LON = ZoneInfo("Europe/London")
APPLIED = datetime(2026, 10, 10, 11, 0, tzinfo=UTC)          # local day 2026-10-10
NOW_DUE = datetime(2026, 10, 18, 7, 40, tzinfo=UTC)          # yesterday = 10-17 = apply day + 7
_FAKE_BASELINE = {"n_days": 7, "net_gbp_per_day": 1.0, "hours_below_floor_per_day": 0.0,
                  "shower_shortfall_days": 0, "peak_import_kwh_per_day": 0.0}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    conn = db.get_connection()
    try:
        for t in ("auto_tune_changes", "comfort_feedback", "cosy_scorecard_daily", "lwt_learning_daily"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM action_log WHERE action IN "
                     "('lwt_emergency','lwt_comfort_backstop','auto_tune_judge_disabled')")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(auto_tune, "baseline_metrics", lambda *a, **k: dict(_FAKE_BASELINE))
    monkeypatch.setattr(auto_tune, "_is_away", lambda: False)
    rts.clear_cache()
    config._overrides.clear()
    for k in ("AUTO_TUNE_PEAK_TRIP_KWH", "AUTO_TUNE_MIN_GAIN_PENCE", "AUTO_TUNE_VERDICT_BUDGET_SECONDS",
              "AUTO_TUNE_NOTIFY_KEPT"):
        monkeypatch.delenv(k, raising=False)
    risk: list[tuple[str, dict]] = []
    monkeypatch.setattr(notifier, "notify_risk", lambda msg, extra=None: risk.append((msg, extra or {})))
    info: list[str] = []
    monkeypatch.setattr(notifier, "notify_action_confirmation", lambda msg: info.append(msg))
    rts.set_setting("AUTO_TUNE_ENABLED", "true", actor="test")
    yield SimpleNamespace(risk=risk, info=info)
    config._overrides.clear()
    rts.clear_cache()


def _change(key=KEY, old=20.0, new=19.5, applied=APPLIED, source="tuning_review", live=True):
    if live:
        rts.set_setting(key, new, actor="vacation_end")  # internal actor: no ledger row
    return db.insert_auto_tune_change(
        key=key, old_value=old, new_value=new, source=source, status="evaluating",
        applied_at=applied.strftime("%Y-%m-%dT%H:%M:%SZ"), baseline=dict(_FAKE_BASELINE))


def _row(i):
    return db.get_auto_tune_change(i)


def _card(day: str, *, peak=0.0, net=1.0, below_n=0.0, below_p=0.0, tank_bad=False, away=False):
    db.upsert_cosy_scorecard({
        "date": day, "score": "ideal", "peak_import_kwh": peak, "import_kwh": 10, "import_cost_gbp": 1,
        "avg_import_p": 12, "ideal_avg_import_p": 12, "net_cost_gbp": net,
        "payload": {"comfort": {"hours_below_night_floor": below_n, "hours_below_peak_floor": below_p},
                    "tank": {"any_below_floor": tank_bad}, "holiday": None,
                    "vacation": {"active": True} if away else None},
        "built_at_utc": "2026-10-18T00:00:00Z"})


def _action(action, ts: datetime, result="ok"):
    conn = db.get_connection()
    try:
        conn.execute("INSERT INTO action_log (timestamp, device, action, params, result, trigger) "
                     "VALUES (?,?,?,?,?,?)",
                     (ts.isoformat(), "daikin", action, "{}", result, "heartbeat"))
        conn.commit()
    finally:
        conn.close()


def _cold(ts: datetime):
    db.insert_comfort_feedback(verdict="cold", now=ts)


def _resid(day: str, e: float):
    db.upsert_lwt_learning_daily({"date": day, "payload": {"pred_resid": {
        "day": {"n": 20, "mean_c": e}, "night": {"n": 10, "mean_c": e}}}})


# ------------------------------------------------------------------ tripwires
def test_cold_feedback_trips_and_rolls_back(_env):
    cid = _change()
    _cold(APPLIED + timedelta(days=1))
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None  # one is not enough
    _cold(APPLIED + timedelta(days=2, hours=20))
    trip = judge.tripwire_check(_row(cid), NOW_DUE)
    assert trip["name"] == "cold_feedback"
    out = judge.tripwire_job(NOW_DUE)
    assert out["status"] == "tripped" and out["tripped"] == "cold_feedback"
    row = _row(cid)
    assert row["status"] == "rolled_back" and row["evidence"]["reason"] == "tripwire:cold_feedback"
    assert float(rts.get_setting(KEY)) == 20.0
    assert auto_tune.rollback(cid, reason="x")["rolled_back"] is False  # not rollbackable twice
    assert db.auto_tune_frozen_until(KEY) is not None
    assert len(_env.risk) == 1


def test_cold_feedback_spread_beyond_3_days_does_not_trip():
    cid = _change()
    _cold(APPLIED + timedelta(days=1))
    _cold(APPLIED + timedelta(days=4, hours=1))
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None


def test_feedback_before_apply_does_not_count():
    cid = _change()
    _cold(APPLIED - timedelta(hours=5))
    _cold(APPLIED - timedelta(hours=2))
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None


def test_lwt_emergency_trips(_env):
    cid = _change()
    _action("lwt_emergency", APPLIED + timedelta(days=2))
    out = judge.tripwire_job(NOW_DUE)
    assert out["tripped"] == "lwt_emergency" and _row(cid)["status"] == "rolled_back"
    assert len(_env.risk) == 1


def test_comfort_backstop_only_when_fired():
    cid = _change()
    _action("lwt_comfort_backstop", APPLIED + timedelta(days=1), result="skipped")
    _action("lwt_comfort_backstop", APPLIED + timedelta(days=1), result="failure")
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None
    _action("lwt_comfort_backstop", APPLIED + timedelta(days=2), result="ok")
    assert judge.tripwire_check(_row(cid), NOW_DUE)["name"] == "comfort_backstop"


def test_events_before_apply_ignored():
    cid = _change()
    _action("lwt_emergency", APPLIED - timedelta(hours=1))
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None


@pytest.mark.parametrize("key,old,new", [
    ("INDOOR_SETPOINT_C", 21.0, 20.5), ("LP_W3_PEAK_COAST_DELTA_C", 0.0, 0.5), ("LP_W3_CEILING_C", 23.0, 22.5)])
def test_every_comfort_loosening_key_has_cold_tripwire(key, old, new):
    cid = _change(key, old, new)
    _action("lwt_emergency", APPLIED + timedelta(days=1))
    assert judge.classify(_row(cid)) == "comfort_loosen"
    assert judge.tripwire_check(_row(cid), NOW_DUE)["name"] == "lwt_emergency"


@pytest.mark.parametrize("key,old,new", [
    (KEY, 20.0, 20.5), ("INDOOR_SETPOINT_C", 21.0, 21.5), ("LP_W3_PEAK_COAST_DELTA_C", 0.5, 0.0),
    ("LP_W3_CEILING_C", 23.0, 23.5), ("DHW_TEMP_NORMAL_C", 45.0, 46.0)])
def test_tightening_has_no_comfort_tripwire(key, old, new):
    cid = _change(key, old, new)
    _cold(APPLIED + timedelta(days=1))
    _cold(APPLIED + timedelta(days=2))
    _action("lwt_emergency", APPLIED + timedelta(days=2))
    _card("2026-10-12", tank_bad=True, peak=5.0)
    assert judge.classify(_row(cid)) == "tighten"
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None
    assert judge.tripwire_job(NOW_DUE)["tripped"] is None
    assert _row(cid)["status"] == "evaluating"


def test_dhw_tripwire_shower_floor(_env):
    cid = _change("DHW_TEMP_NORMAL_C", 45.0, 44.0)
    _card("2026-10-10", tank_bad=True)  # the apply day itself is mixed: not counted
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None
    _card("2026-10-13", tank_bad=True)
    out = judge.tripwire_job(NOW_DUE)
    assert out["tripped"] == "shower_floor"
    assert _row(cid)["status"] == "rolled_back" and float(rts.get_setting("DHW_TEMP_NORMAL_C")) == 45.0
    assert len(_env.risk) == 1


def test_dhw_comfort_key_also_traps():
    cid = _change("DHW_TEMP_COMFORT_C", 48.0, 47.0)
    _card("2026-10-12", tank_bad=True)
    assert judge.tripwire_check(_row(cid), NOW_DUE)["name"] == "shower_floor"


def test_peak_import_tripwire_and_env_limit(monkeypatch):
    cid = _change("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p75", "p90")
    _card("2026-10-12", peak=1.0)  # == limit, not over
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None
    _card("2026-10-14", peak=1.4)
    assert judge.tripwire_check(_row(cid), NOW_DUE)["name"] == "peak_import"
    monkeypatch.setenv("AUTO_TUNE_PEAK_TRIP_KWH", "2.0")
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None


def test_learned_param_tripwire_two_consecutive_days():
    cid = _change("LP_W3_EMITTER_TAU_MIN", 0.0, 10.0)
    for i in range(1, 8):
        _resid((date(2026, 10, 10) - timedelta(days=i)).isoformat(), 0.4)  # baseline 0.4
    _resid("2026-10-11", 0.7)   # > 0.6
    _resid("2026-10-12", 0.4)   # reset
    _resid("2026-10-13", 0.7)
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None  # not consecutive
    _resid("2026-10-14", 0.65)
    trip = judge.tripwire_check(_row(cid), NOW_DUE)
    assert trip["name"] == "pred_resid" and trip["evidence"]["days"] == ["2026-10-13", "2026-10-14"]


def test_learned_param_without_baseline_never_trips():
    cid = _change("LP_W3_INTERNAL_GAIN_KW", 0.0, 0.05)
    for d in ("2026-10-11", "2026-10-12", "2026-10-13"):
        _resid(d, 5.0)
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None
    judge.tripwire_job(NOW_DUE)
    assert "no_pred_resid_baseline" in json.loads(db.get_kv(f"auto_tune_tripwire_{cid}"))["notes"]


def test_away_days_never_count():
    cid = _change()
    _card("2026-10-11", away=True)
    _card("2026-10-12", away=True)
    _cold(datetime(2026, 10, 11, 12, tzinfo=UTC))
    _cold(datetime(2026, 10, 12, 12, tzinfo=UTC))
    _action("lwt_emergency", datetime(2026, 10, 11, 20, tzinfo=UTC))
    assert judge.tripwire_check(_row(cid), NOW_DUE) is None
    c2 = _change("DHW_TEMP_NORMAL_C", 45.0, 44.0)  # same for scorecard-day wires
    _card("2026-10-13", tank_bad=True, away=True)
    assert judge.tripwire_check(_row(c2), NOW_DUE) is None


def test_scorecard_fields_after_check_and_trip():
    cid = _change()
    judge.tripwire_job(NOW_DUE)
    f = judge.scorecard_fields(_row(cid))
    assert f["tripwire_checked"] == "2026-10-18T07:40:00Z" and f["tripped"] is None
    _action("lwt_emergency", APPLIED + timedelta(days=1))
    judge.tripwire_job(NOW_DUE)
    assert judge.scorecard_fields(_row(cid))["tripped"] == "lwt_emergency"


# -------------------------------------------------------------------- verdict
def _agg(cost=700.0, hours=0.0, shower=0.0, n=7):
    return {"cost_p": cost, "night_below_h": hours, "peak_below_h": 0.0, "hours_below": hours,
            "shower_short_c": 0.0, "shower_day_below": shower, "n_days": float(n)}


def test_decide_cost_noise_band():
    cold = {"after": 0, "before": 0}
    assert judge.decide(_agg(700), _agg(705), cold, 7)[0] == "keep"        # exactly +5 p: inside the band
    d, rule, _ = judge.decide(_agg(700), _agg(705.1), cold, 7)
    assert d == "rollback" and rule == "failed:cost"
    assert judge.decide(_agg(700), _agg(600), cold, 7)[0] == "keep"
    # partial: the band scales with the days (4/7 of 5 p = 2.86 p)
    assert judge.decide(_agg(700, n=4), _agg(703, n=4), cold, 4)[0] == "rollback"
    assert judge.decide(_agg(700, n=4), _agg(702.5, n=4), cold, 4)[0] == "keep"


def test_decide_comfort_band_shower_and_feedback():
    cold = {"after": 1, "before": 0}
    assert judge.decide(_agg(hours=1.0), _agg(hours=1.5), cold, 7)[0] == "keep"       # +0.5 h ok
    d, rule, _ = judge.decide(_agg(hours=1.0), _agg(hours=1.6), cold, 7)
    assert d == "rollback" and rule == "failed:hours_below_floor"
    d, rule, _ = judge.decide(_agg(), _agg(shower=1), cold, 7)
    assert d == "rollback" and rule == "failed:shower_shortfall_days"
    assert judge.decide(_agg(shower=1), _agg(shower=1), cold, 7)[0] == "keep"
    assert judge.decide(_agg(), _agg(), {"after": 3, "before": 2}, 7)[0] == "keep"     # +1 allowed
    d, rule, _ = judge.decide(_agg(), _agg(), {"after": 4, "before": 2}, 7)
    assert d == "rollback" and rule == "failed:cold_feedback"
    d, rule, _ = judge.decide(_agg(700), _agg(800, hours=2.0), cold, 7)
    assert rule == "failed:cost+hours_below_floor"


class Replays:
    """Stub of ``replay_day``: cost / comfort depend on the (patched) knob value."""

    def __init__(self, cost, hours=None, shower=None, boom=False):
        self.cost = cost
        self.hours = hours or (lambda v: 0.0)
        self.shower = shower or (lambda v: 0.0)
        self.calls: list[tuple[str, float]] = []
        self.boom = boom

    def __call__(self, d, cadence, mode):
        v = float(config.LP_W3_NIGHT_FLOOR_C)
        self.calls.append((d, v))
        if self.boom:
            raise RuntimeError("replay exploded")
        return SimpleNamespace(ok=True, total_replayed_cost_p=self.cost(v), plan_date=d,
                               recalc_timestamps_utc=[], runs=[], _v=v, _repl=self)


@pytest.fixture
def stub_comfort(monkeypatch):
    monkeypatch.setattr(tr, "yardstick", lambda: {"floor": float(config.LP_W3_NIGHT_FLOOR_C)})
    monkeypatch.setattr(tr, "day_comfort", lambda day, yard: {
        "night_below_h": 0.0, "peak_below_h": 0.0, "hours_below": day._repl.hours(day._v),
        "shower_short_c": 0.0, "shower_day_below": day._repl.shower(day._v)})


def _run(repl, now=NOW_DUE, days=7):
    d0 = date(2026, 10, 11)
    avail = {(d0 + timedelta(days=i)).isoformat() for i in range(days)}
    return judge.verdict_job(now, replay=repl, list_runs=lambda d: ["r"] if d in avail else [])


def test_not_due_before_7_complete_days(stub_comfort):
    cid = _change()
    out = _run(Replays(lambda v: 100.0), now=datetime(2026, 10, 17, 7, 40, tzinfo=UTC))
    assert out["status"] == "not_due" and _row(cid)["status"] == "evaluating"


def test_verdict_keep_replays_both_arms_and_records_evidence(stub_comfort, _env):
    cid = _change()
    repl = Replays(lambda v: 100.0 + 10.0 * v)   # new (19.5) is 5 p/day cheaper
    out = _run(repl)
    assert out["status"] == "kept"
    row = _row(cid)
    assert row["status"] == "kept"
    ev = row["evidence"]
    assert ev["rule"] == "all_clauses_passed" and ev["lived_arm"] == "replay_with_new_value"
    assert ev["arm_old"]["cost_p"] > ev["arm_new"]["cost_p"] and len(ev["days_replayed"]) == 7
    assert ev["cadence"] == str(config.TUNING_REVIEW_CADENCE) and ev["partial"] is False
    assert {v for _d, v in repl.calls} == {20.0, 19.5}  # old and new arm
    assert float(rts.get_setting(KEY)) == 19.5            # kept: value stays
    assert float(config.LP_W3_NIGHT_FLOOR_C) == 19.5      # patched_config restored
    assert _env.info == [] and _env.risk == []             # kept is muted by default


def test_verdict_keep_notifies_when_enabled(stub_comfort, monkeypatch, _env):
    monkeypatch.setenv("AUTO_TUNE_NOTIFY_KEPT", "true")
    _change()
    _run(Replays(lambda v: 100.0))
    assert len(_env.info) == 1 and _env.risk == []


def test_verdict_rollback_on_cost(stub_comfort, _env):
    cid = _change()
    out = _run(Replays(lambda v: 100.0 - 20.0 * v))  # new value costs +10 p/day
    assert out["status"] == "rolled_back"
    row = _row(cid)
    assert row["status"] == "rolled_back" and row["evidence"]["reason"] == "verdict:failed:cost"
    assert float(rts.get_setting(KEY)) == 20.0 and db.auto_tune_frozen_until(KEY)
    assert len(_env.risk) == 1  # rollback's own message only; the judge sends no second one
    assert _env.info == []


def test_verdict_rollback_on_comfort_hours(stub_comfort):
    cid = _change()
    out = _run(Replays(lambda v: 100.0, hours=lambda v: 3.0 if v < 20 else 0.0))
    assert out["status"] == "rolled_back" and "hours_below_floor" in _row(cid)["evidence"]["rule"]


def test_verdict_rollback_on_shower_day(stub_comfort):
    cid = _change()
    out = _run(Replays(lambda v: 100.0, shower=lambda v: 1.0 if v < 20 else 0.0))
    assert out["status"] == "rolled_back" and "shower_shortfall_days" in _row(cid)["evidence"]["rule"]


def test_cold_feedback_signal_rule_in_verdict(stub_comfort):
    cid = _change(old=19.5, new=20.0)  # a tightening change: no comfort tripwire, the verdict rule still applies
    for d in (1, 4, 6):  # 3 cold after vs 0 before
        _cold(APPLIED + timedelta(days=d))
    out = _run(Replays(lambda v: 100.0))
    assert out["status"] == "rolled_back"
    assert _row(cid)["evidence"]["reason"] == "verdict:failed:cold_feedback"


def test_tripwire_rechecked_at_verdict_end(stub_comfort, monkeypatch):
    cid = _change()
    monkeypatch.setattr(judge, "_evaluate_tripwires", lambda c, n: (None, []))  # silent in the daily pass
    monkeypatch.setattr(judge, "tripwire_check",
                        lambda c, n=None: {"name": "lwt_emergency", "evidence": {"x": 1}})
    out = _run(Replays(lambda v: 100.0))
    assert out["status"] == "rolled_back"
    assert _row(cid)["evidence"]["reason"] == "verdict:tripwire:lwt_emergency"


def test_away_days_skipped_on_both_arms(stub_comfort):
    cid = _change()
    for d in ("2026-10-12", "2026-10-13"):
        _card(d, away=True)
    repl = Replays(lambda v: 100.0)
    out = _run(repl)
    ev = _row(cid)["evidence"]
    assert out["status"] == "kept"
    assert ev["days_skipped_away"] == ["2026-10-12", "2026-10-13"] and len(ev["days_replayed"]) == 5
    assert all(d not in ("2026-10-12", "2026-10-13") for d, _v in repl.calls)
    assert len(repl.calls) == 10


def test_partial_with_4_days_decides(stub_comfort, monkeypatch):
    cid = _change()
    clock = {"t": 0.0}

    class Slow(Replays):
        def __call__(self, d, cadence, mode):
            r = super().__call__(d, cadence, mode)
            clock["t"] += 1.0
            return r
    monkeypatch.setattr(judge.time, "monotonic", lambda: clock["t"])  # same module object as tr.time
    monkeypatch.setenv("AUTO_TUNE_VERDICT_BUDGET_SECONDS", "9")      # 2 replays per day -> 4 days fit
    out = _run(Slow(lambda v: 100.0))
    ev = _row(cid)["evidence"]
    assert out["status"] == "kept" and ev["partial"] is True and 4 <= len(ev["days_replayed"]) < 7


def test_partial_with_fewer_than_4_defers_then_undecidable(stub_comfort, monkeypatch, _env):
    cid = _change()
    monkeypatch.setenv("AUTO_TUNE_VERDICT_BUDGET_SECONDS", "0")
    ticks = iter(range(10, 10_000))
    monkeypatch.setattr(judge.time, "monotonic", lambda: next(ticks))  # always past the 0 s budget
    for n in (1, 2, 3):
        out = _run(Replays(lambda v: 100.0))
        assert out["status"] == "deferred" and out["deferred"] == n
        assert _row(cid)["status"] == "evaluating" and _row(cid)["evidence"]["deferred"] == n
    out = _run(Replays(lambda v: 100.0))
    assert out["status"] == "kept_undecidable"
    row = _row(cid)
    assert row["status"] == "kept" and row["evidence"]["reason"] == "undecidable"
    assert float(rts.get_setting(KEY)) == 19.5
    assert len(_env.risk) == 1 and _env.risk[0][1]["warning_key"] == f"auto_tune_undecidable_{cid}"


def test_too_few_replayable_days_defers(stub_comfort):
    _change()
    assert _run(Replays(lambda v: 100.0), days=3)["status"] == "deferred"


def test_failed_replays_defer(stub_comfort):
    _change()
    assert _run(Replays(lambda v: 100.0, boom=True))["status"] == "deferred"


def test_lock_busy_defers_without_counting(stub_comfort):
    cid = _change()
    tr._acquire()
    try:
        out = _run(Replays(lambda v: 100.0))
    finally:
        tr._release()
    assert out["status"] == "busy_replay"
    row = _row(cid)
    assert row["status"] == "evaluating" and row["evidence"]["busy_retries"] == 1
    assert row["evidence"].get("deferred", 0) == 0
    assert _run(Replays(lambda v: 100.0))["status"] == "kept"  # next day it goes through


def test_unreplayable_key_is_not_judged_as_trivial_keep(stub_comfort, monkeypatch):
    from src.scheduler import lp_overrides
    monkeypatch.delitem(lp_overrides.WHITELIST, "LP_W3_CEILING_C")
    _change("LP_W3_CEILING_C", 23.0, 22.5)
    assert _run(Replays(lambda v: 100.0))["status"] == "deferred"


def test_new_whitelist_entries_make_ceiling_and_gains_replayable():
    from src.scheduler.lp_overrides import WHITELIST, patched_config
    for k in ("LP_W3_CEILING_C", "LP_W3_SOLAR_GAIN_KW_PER_PV_KW", "LP_W3_INTERNAL_GAIN_KW"):
        assert k in WHITELIST
    with patched_config({"LP_W3_CEILING_C": 22.0}):
        assert float(config.LP_W3_CEILING_C) == 22.0
    assert not config.has_override("LP_W3_CEILING_C")


def test_manual_and_superseded_rows_are_never_judged(stub_comfort):
    db.insert_auto_tune_change(key=KEY, old_value=20.0, new_value=19.0, source="manual", status="manual",
                               applied_at="2026-10-10T11:00:00Z")
    assert judge.verdict_job(NOW_DUE)["status"] == "no_change"
    cid = _change()
    db.update_auto_tune_change(cid, status="superseded_manual")
    assert judge.verdict_job(NOW_DUE)["status"] == "no_change"
    assert judge.tripwire_job(NOW_DUE)["status"] == "no_change"


def test_disabled_is_a_noop_and_logs(stub_comfort):
    cid = _change()
    _action("lwt_emergency", APPLIED + timedelta(days=1))
    rts.set_setting("AUTO_TUNE_ENABLED", "false", actor="test")
    assert judge.tripwire_job(NOW_DUE)["status"] == "disabled"
    assert judge.verdict_job(NOW_DUE)["status"] == "disabled"
    assert _row(cid)["status"] == "evaluating"
    assert db.get_action_logs(device="auto_tune", action="auto_tune_judge_disabled", limit=5)


def test_away_now_waits_without_counting(stub_comfort, monkeypatch):
    cid = _change()
    monkeypatch.setattr(auto_tune, "_is_away", lambda: True)
    assert judge.verdict_job(NOW_DUE)["status"] == "away_wait"
    assert _row(cid)["status"] == "evaluating" and not _row(cid)["evidence"]


def test_jobs_never_raise(monkeypatch):
    _change()
    monkeypatch.setattr(db, "get_cosy_scorecards", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert judge.tripwire_job(NOW_DUE)["status"] == "error"
    assert judge.verdict_job(NOW_DUE)["status"] == "error"
    assert judge.tripwire_check({"key": KEY}, NOW_DUE) is None
    monkeypatch.setattr(db, "get_active_auto_tune_change", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert judge.tripwire_job(NOW_DUE)["status"] == "error"


def test_jobs_are_single_flight():
    assert judge._job_lock.acquire(blocking=False)
    try:
        assert judge.tripwire_job(NOW_DUE)["status"] == "busy"
        assert judge.verdict_job(NOW_DUE)["status"] == "busy"
    finally:
        judge._job_lock.release()


# ----------------------------------------------------------------- scheduling
def test_judge_time_is_scorecard_plus_ten():
    assert judge.judge_time(7, 30) == (7, 40)
    assert judge.judge_time(7, 55) == (8, 5)
    assert judge.judge_time(23, 55) == (0, 5)


def test_register_jobs_local_clock_dst_safe():
    added = []
    sched = SimpleNamespace(add_job=lambda fn, trig, **kw: added.append((fn, trig, kw)))
    assert judge.register_jobs(sched, LON)
    assert {kw["id"] for _f, _t, kw in added} == {"auto_tune_tripwires", "auto_tune_verdict"}
    trig = next(t for _f, t, kw in added if kw["id"] == "auto_tune_tripwires")
    h, m = judge.judge_time()
    for start in (datetime(2026, 10, 24, 12, tzinfo=LON), datetime(2026, 3, 28, 12, tzinfo=LON)):
        t1 = trig.get_next_fire_time(None, start)           # day before the change
        t2 = trig.get_next_fire_time(None, t1 + timedelta(minutes=1))  # the day of the change
        t3 = trig.get_next_fire_time(None, t2 + timedelta(minutes=1))  # after the change
        for t in (t1, t2, t3):
            assert (t.astimezone(LON).hour, t.astimezone(LON).minute) == (h, m)


def test_scorecard_section_carries_tripwire_fields():
    cid = _change(applied=datetime.now(UTC) - timedelta(days=1))
    judge.tripwire_job()
    day = datetime.now(LON).date()
    a = datetime.combine(day, datetime.min.time(), tzinfo=LON).astimezone(UTC)
    ac = auto_tune.scorecard_section(day, a, a + timedelta(days=1), LON)["active_change"]
    assert ac["id"] == cid and ac["tripwire_checked"] is not None and ac["tripped"] is None


def test_ledger_rows_carry_parsed_evidence(stub_comfort):
    cid = _change()
    _run(Replays(lambda v: 100.0))
    rows = auto_tune.ledger_summary(5)["rows"]
    assert rows[0]["id"] == cid and isinstance(rows[0]["evidence"], dict) and rows[0]["evidence"]["rule"]
