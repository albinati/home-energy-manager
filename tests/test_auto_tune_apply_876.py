"""#876 story 2 - weekly apply of the tuning review (one change per ISO week)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src import auto_tune, auto_tune_apply as ap, db, notifier
from src import runtime_settings as rts
from src.analytics import comfort_feedback as cf
from src.config import config

LON = ZoneInfo("Europe/London")
# Sunday 2026-11-01 (GMT). Review at 09:00, apply at 09:20; comfort job 08:45.
APPLY_AT = datetime(2026, 11, 1, 9, 20, tzinfo=UTC)
COMFORT_AT = datetime(2026, 11, 1, 8, 45, tzinfo=UTC)
WS = "2026-10-25"  # (Oct 31) - 6 days: the week the review of that Sunday covers
KEY = "LP_W3_NIGHT_FLOOR_C"
_FAKE_BASELINE = {"n_days": 7, "net_gbp_per_day": 1.0, "hours_below_floor_per_day": 0.0,
                  "shower_shortfall_days": 0, "peak_import_kwh_per_day": 0.0}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    monkeypatch.setattr(auto_tune, "baseline_metrics", lambda *a, **k: dict(_FAKE_BASELINE))
    rts.clear_cache()
    config._overrides.clear()
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "TUNING_REVIEW_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AUTO_TUNE_NOTIFY_SKIPPED", False, raising=False)
    sent: list[dict] = []

    def fake_dispatch(kind, message, *, urgent=False, extra=None, telegram_header_override=None):
        sent.append({"kind": kind, "message": message, "extra": extra or {},
                     "header": telegram_header_override})

    monkeypatch.setattr(notifier, "_dispatch", fake_dispatch)
    monkeypatch.setattr(notifier, "notify_risk", lambda msg, extra=None: None)
    yield sent
    config._overrides.clear()
    rts.clear_cache()


def _on():
    rts.set_setting("AUTO_TUNE_ENABLED", "true", actor="test")


def _setup(value=20.0, key=KEY):
    rts.set_setting(key, value, actor="vacation_end")


def _row(key, cur, new, pence, hrs, verdict, payload=None, ws=WS):
    return {"week_start": ws, "key": key, "current_value": cur, "suggested_value": new,
            "delta_pence_per_week": pence, "delta_comfort_hours": hrs, "verdict": verdict,
            "payload": {"body": {"value": new}, "hours_below_current": 0.0, **(payload or {})}}


def _fb(verdict, n=1, hours_ago=30):
    for i in range(n):
        db.insert_comfort_feedback(verdict=verdict, source="api",
                                   now=APPLY_AT - timedelta(hours=hours_ago + i))


def _last_log():
    logs = db.get_action_logs(device="auto_tune", action="weekly_apply", limit=5)
    return logs[0] if logs else None


# ---------------------------------------------------------------- selection
def _sel(rows, signal=None):
    return ap.select_weekly_change(rows, signal or {}, APPLY_AT)


def test_recommended_top_by_saving_ties_smaller_comfort_cost():
    _setup(20.0)
    rts.set_setting("INDOOR_SETPOINT_C", 21.0, actor="vacation_end")
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")
    rows = [
        _row(KEY, 20.0, 19.5, -10, 0.0, "recommended"),
        _row("INDOOR_SETPOINT_C", 21.0, 20.5, -25, 0.2, "recommended"),
        _row("DHW_TEMP_NORMAL_C", 45.0, 44.0, -25, 0.0, "recommended"),
    ]
    assert _sel(rows)["key"] == "DHW_TEMP_NORMAL_C"  # tie on 25p -> smaller comfort cost
    rows[2]["delta_comfort_hours"] = 0.5
    assert _sel(rows)["key"] == "INDOOR_SETPOINT_C"


def test_tradeoff_needs_comfort_signal():
    _setup(20.0)
    r = [_row(KEY, 20.0, 19.5, -32, 0.3, "trade-off")]
    assert _sel(r, {"n": 0, "cold": 0}) is None                     # silence is not agreement
    assert _sel(r, {"n": 4, "cold": 1}) is None                     # someone was cold
    assert _sel(r, {"n": 4, "cold": 0, "ok": 4, "n_days": 1}) is None  # one press must not license it
    assert _sel(r, {"n": 4, "cold": 0, "ok": 4, "n_days": 2})["key"] == KEY
    # a trade-off that adds a bad shower day is never applied
    r2 = [_row(KEY, 20.0, 19.5, -32, 0.3, "trade-off",
               {"shower_days_below_current": 0, "shower_days_below_suggested": 1})]
    assert _sel(r2, {"n": 4, "cold": 0, "n_days": 3}) is None


def test_comfort_first_needs_cold_and_hours_below():
    _setup(20.0)
    r = [_row(KEY, 20.0, 20.5, 15, -1.5, "comfort-first", {"hours_below_current": 2.0})]
    assert _sel(r, {"n": 3, "cold": 0}) is None
    assert _sel(r, {"n": 3, "cold": 2})["key"] == KEY
    r0 = [_row(KEY, 20.0, 20.5, 15, -1.5, "comfort-first", {"hours_below_current": 0.0})]
    assert _sel(r0, {"n": 3, "cold": 2}) is None


def test_stale_current_value_skipped():
    _setup(20.5)  # owner moved it since the review saw 20.0
    chosen, cons = ap.evaluate_candidates([_row(KEY, 20.0, 19.5, -10, 0.0, "recommended")], {}, APPLY_AT)
    assert chosen is None and cons[0]["skipped"] == "stale"


def test_step_larger_than_one_skipped_next_candidate_taken():
    _setup(20.0)
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")
    rows = [_row(KEY, 20.0, 19.0, -50, 0.0, "recommended"),             # 2 steps
            _row("DHW_TEMP_NORMAL_C", 45.0, 44.0, -5, 0.0, "recommended")]
    chosen, cons = ap.evaluate_candidates(rows, {}, APPLY_AT)
    assert chosen["key"] == "DHW_TEMP_NORMAL_C"
    assert cons[0]["skipped"] == "step_too_large"


def test_enum_key_one_step():
    rts.set_setting("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p75", actor="vacation_end")
    r = [_row("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p75", "p90", -12, 0.0, "recommended")]
    assert _sel(r)["suggested_value"] == "p90"


# ------------------------------------------------------------------- apply
def _seed(rows):
    db.save_tuning_suggestions(rows)


def test_disabled():
    _setup(20.0)
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])
    out = ap.apply_weekly(APPLY_AT)
    assert out == {"applied": False, "reason": "disabled"} or out["reason"] == "disabled"
    assert float(rts.get_setting(KEY)) == 20.0
    assert _last_log()["params"]["reason"] == "disabled"


def test_no_review():
    _on()
    _setup(20.0)
    out = ap.apply_weekly(APPLY_AT)
    assert out["reason"] == "no_review"
    # a review of an OLDER week does not count either
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended", ws="2026-10-18")])
    assert ap.apply_weekly(APPLY_AT)["reason"] == "no_review"
    assert _last_log()["params"]["reason"] == "no_review"


def test_applies_once_with_telegram_and_log(_env):
    sent = _env
    _on()
    _setup(20.0)
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])
    out = ap.apply_weekly(APPLY_AT)
    assert out["applied"] is True and out["key"] == KEY
    assert float(rts.get_setting(KEY)) == 19.5
    row = db.get_auto_tune_change(out["id"])
    assert row["source"] == "tuning_review" and row["status"] == "evaluating"
    assert row["note"] == f"review {WS}: recommended, saves 32p/week"
    # exactly one Telegram, pt-BR label, rollback hint
    assert len(sent) == 1
    m = sent[0]["message"]
    assert "Mínimo à noite 20 °C → 19,5 °C" in m and "Simulação da revisão (7 dias): −32p/sem, +0,3 h abaixo do piso" in m
    assert f"/api/v1/tuning/ledger/{out['id']}/rollback" in m and "reverto sozinho" in m
    assert sent[0]["header"].endswith("Ajuste automático")
    assert sent[0]["kind"] == notifier.AlertType.AUTO_TUNE
    log = _last_log()
    assert log["result"] == "success" and log["params"]["applied"] is True
    assert log["params"]["candidates"][0]["key"] == KEY
    assert db.get_kv("auto_tune_last_apply_week") == ap.iso_week_label(APPLY_AT)
    # second run in the same week: quota
    out2 = ap.apply_weekly(APPLY_AT + timedelta(minutes=5))
    assert out2["reason"] == "weekly_quota" and len(sent) == 1


def test_weekly_quota_shared_with_comfort_source(_env, monkeypatch):
    _on()
    _setup(20.0)
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])
    monkeypatch.setattr(cf, "weekly_summary", lambda ws: {
        "proposal": {"key": KEY, "current": 20.0, "suggested": 20.5, "reason": "cold"}, "week_start": "x"})
    cf.weekly_job(COMFORT_AT)  # 08:45 applies 20 -> 20.5 through the ledger
    assert float(rts.get_setting(KEY)) == 20.5
    assert ap.weekly_quota_used(COMFORT_AT) == 1
    out = ap.apply_weekly(APPLY_AT)  # 09:20
    assert out["reason"] == "weekly_quota"
    assert _env == []  # no Telegram when nothing is applied
    assert _last_log()["params"]["reason"] == "weekly_quota"


def test_comfort_job_skips_when_quota_used(monkeypatch):
    _on()
    _setup(20.0)
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")
    auto_tune.propose_change("DHW_TEMP_NORMAL_C", 44.0, "tuning_review", now=COMFORT_AT)
    auto_tune.keep(db.get_active_auto_tune_change()["id"])  # no longer busy
    monkeypatch.setattr(cf, "weekly_summary", lambda ws: {
        "proposal": {"key": KEY, "current": 20.0, "suggested": 20.5, "reason": "cold"}, "week_start": "x"})
    cf.weekly_job(COMFORT_AT + timedelta(minutes=1))
    assert float(rts.get_setting(KEY)) == 20.0
    log = db.get_action_logs(device="comfort", action="auto_tune", limit=1)[0]
    assert log["params"]["skipped"] == "weekly_quota"


def test_quota_resets_next_iso_week():
    _on()
    _setup(20.0)
    auto_tune.propose_change(KEY, 19.5, "tuning_review", now=APPLY_AT)
    assert ap.weekly_quota_used(APPLY_AT) == 1
    assert ap.weekly_quota_used(datetime(2026, 11, 2, 0, 5, tzinfo=UTC)) == 0  # Monday
    assert ap.weekly_quota_used(datetime(2026, 10, 25, 22, 0, tzinfo=UTC)) == 0  # Sunday of the previous week
    assert ap.weekly_quota_used(datetime(2026, 10, 26, 0, 5, tzinfo=UTC)) == 1  # Monday of the same ISO week


def test_busy_is_surfaced_and_held_notification(_env, monkeypatch):
    sent = _env
    _on()
    _setup(20.0)
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")
    # an evaluating change from LAST week (so the weekly quota is free)
    auto_tune.propose_change("DHW_TEMP_NORMAL_C", 44.0, "tuning_review",
                             now=APPLY_AT - timedelta(days=8))
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])
    out = ap.apply_weekly(APPLY_AT)
    assert out["reason"] == "busy" and sent == []                   # muted by default
    assert _last_log()["params"]["reason"] == "busy"
    monkeypatch.setattr(config, "AUTO_TUNE_NOTIFY_SKIPPED", True, raising=False)
    ap.apply_weekly(APPLY_AT + timedelta(minutes=1))
    assert len(sent) == 1 and "−32p/semana em espera" in sent[0]["message"]
    assert sent[0]["extra"]["held"] is True


def test_frozen_candidate_skipped_with_held_notification(_env, monkeypatch):
    sent = _env
    _on()
    _setup(20.0)
    now = APPLY_AT
    monkeypatch.setattr(auto_tune, "_now", lambda n=None: (n or APPLY_AT).astimezone(UTC))
    auto_tune.freeze(key=KEY, days=10)
    ws = ap._expected_week_start(now)
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended", ws=ws)])
    out = ap.apply_weekly(now)
    assert out["reason"] == "nothing_eligible" and out["candidates"][0]["skipped"] == "frozen"
    assert float(rts.get_setting(KEY)) == 20.0 and sent == []       # muted by default
    monkeypatch.setattr(config, "AUTO_TUNE_NOTIFY_SKIPPED", True, raising=False)
    ap.apply_weekly(now)
    assert len(sent) == 1 and "−32p/semana em espera" in sent[0]["message"]
    assert "congelada até" in sent[0]["message"]


def test_ledger_refusal_surfaces_reason_no_telegram(_env):
    _on()
    _setup(20.0)
    db.set_kv("vacation_end_at", "2026-11-07T12:00:00Z")           # away
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])
    out = ap.apply_weekly(APPLY_AT)
    assert out["reason"] == "away" and _env == []
    assert float(rts.get_setting(KEY)) == 20.0
    assert _last_log()["result"] == "skipped"


def test_review_disabled():
    _on()
    _setup(20.0)
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])
    config.TUNING_REVIEW_ENABLED = False
    assert ap.apply_weekly(APPLY_AT)["reason"] == "review_disabled"


def test_never_raises(monkeypatch):
    _on()
    monkeypatch.setattr(db, "list_tuning_suggestions", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    out = ap.apply_weekly(APPLY_AT)
    assert out["applied"] is False and out["reason"] == "error"
    monkeypatch.setattr(ap, "weekly_quota_used", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("y")))
    assert ap.apply_weekly(APPLY_AT)["reason"] == "error"
    ap.weekly_apply_job()  # swallowed


# -------------------------------------------------------------- scheduling
def test_apply_schedule_arithmetic(monkeypatch):
    assert ap.apply_schedule() == (6, 9, 20)
    monkeypatch.setattr(config, "AUTO_TUNE_APPLY_DELAY_MINUTES", 200)
    assert ap.apply_schedule() == (6, 12, 20)
    monkeypatch.setattr(config, "TUNING_REVIEW_HOUR_LOCAL", 23)
    monkeypatch.setattr(config, "AUTO_TUNE_APPLY_DELAY_MINUTES", 90)
    assert ap.apply_schedule() == (0, 0, 30)  # Sunday 23:00 + 90 min = Monday 00:30


def test_next_apply_at_dst_safe():
    # GMT (after the clocks go back): Sunday 09:20 local == 09:20Z
    assert ap.next_apply_at(datetime(2026, 10, 31, 12, 0, tzinfo=UTC)) == "2026-11-01T09:20:00Z"
    # BST: Sunday 18 Oct 09:20 local == 08:20Z
    assert ap.next_apply_at(datetime(2026, 10, 14, 12, 0, tzinfo=UTC)) == "2026-10-18T08:20:00Z"
    # spring-forward Sunday (28 Mar 2027): 09:20 BST == 08:20Z
    assert ap.next_apply_at(datetime(2027, 3, 27, 12, 0, tzinfo=UTC)) == "2027-03-28T08:20:00Z"
    # just after this week's slot -> next week
    assert ap.next_apply_at(datetime(2026, 11, 1, 9, 21, tzinfo=UTC)) == "2026-11-08T09:20:00Z"


def test_job_registration():
    from src.scheduler import runner

    class Fake:
        def __init__(self):
            self.jobs = []

        def add_job(self, fn, trigger=None, **kw):
            self.jobs.append((fn, trigger, kw))

    f = Fake()
    runner.register_auto_tune_apply_job(f, LON)
    fn, trig, kw = f.jobs[0]
    assert kw["id"] == "auto_tune_weekly_apply" and fn is ap.weekly_apply_job
    t = str(trig)
    assert "day_of_week='6'" in t and "hour='9'" in t and "minute='20'" in t
    # runs 20 min after the review's 09:00 slot on a BST Sunday and a GMT Sunday
    nxt = trig.get_next_fire_time(None, datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
    assert nxt.astimezone(UTC) == datetime(2026, 10, 18, 8, 20, tzinfo=UTC)
    nxt = trig.get_next_fire_time(None, datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    assert nxt.astimezone(UTC) == datetime(2026, 11, 1, 9, 20, tzinfo=UTC)


# ---------------------------------------------------------------- surfaces
def test_ledger_api_has_next_apply_and_quota():
    from src.api.main import app

    _on()
    _setup(20.0)
    auto_tune.propose_change(KEY, 19.5, "tuning_review", now=datetime.now(UTC))
    body = TestClient(app).get("/api/v1/tuning/ledger").json()
    assert body["weekly_quota_used"] == 1 and body["next_apply_at"].endswith("Z")
    assert body["apply_enabled"] is True
    assert body["rows"]


def test_recommendations_ledger_status_and_button(monkeypatch):
    from src.analytics import recommendations as rec

    today = datetime.now(LON).date()
    ws = (today - timedelta(days=3)).isoformat()
    # the composer reads the live value from config; read the persisted one instead
    monkeypatch.setattr(rec, "_live_value", lambda k: float(rts.get_setting(k)))
    db.save_tuning_suggestions([
        _row(KEY, 20, 19.5, -32, 0.3, "recommended", ws=ws),
        _row("DHW_TEMP_NORMAL_C", 45, 44, -9, 0.0, "recommended", ws=ws),
    ])
    # a kept change on DHW 4 days ago: ledger shown, button kept (suggestion still open)
    _on()
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")
    rts.set_setting(KEY, 20.0, actor="vacation_end")
    r = auto_tune.propose_change("DHW_TEMP_NORMAL_C", 44.0, "tuning_review",
                                 now=datetime.now(UTC) - timedelta(days=4))
    assert r.get("applied"), r
    auto_tune.keep(r["id"])
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")   # owner/internal moved back
    items = {i["key"]: i for i in rec.tuning_items(today, LON)}
    d = items["tune_DHW_TEMP_NORMAL_C"]
    assert d["kind"] == "setting" and d["ledger"]["status"] == "kept" and d["ledger"]["id"] == r["id"]
    assert "ledger" not in items["tune_LP_W3_NIGHT_FLOOR_C"]
    # now the auto-tune applies the night floor: the item becomes a status line
    rts.clear_cache()
    res = auto_tune.propose_change(KEY, 19.5, "tuning_review", now=datetime.now(UTC))
    assert res["applied"]
    items = {i["key"]: i for i in rec.tuning_items(today, LON)}
    n = items["tune_LP_W3_NIGHT_FLOOR_C"]
    assert n["kind"] == "status" and n["action"] is None
    assert n["headline"].startswith("Aplicado ") and "avaliando dia 1/7" in n["headline"]
    assert n["ledger"]["status"] == "evaluating" and n["ledger"]["day_n"] == 1


# ------------------------------------------------------------ review fixes
def _fb_at(verdict, t):
    db.insert_comfort_feedback(verdict=verdict, source="api", now=t)


def test_s1_cold_week_never_loosens_comfort():
    _on()
    _setup(20.0)
    _fb_at("cold", APPLY_AT - timedelta(hours=40))
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])        # +0.3 h below the floor
    out = ap.apply_weekly(APPLY_AT)
    assert out["reason"] == "nothing_eligible" and out["candidates"][0]["skipped"] == "cold_feedback"
    assert float(rts.get_setting(KEY)) == 20.0


def test_s1_cold_week_same_row_without_comfort_cost_applies():
    _on()
    _setup(20.0)
    _fb_at("cold", APPLY_AT - timedelta(hours=40))
    _seed([_row(KEY, 20.0, 19.5, -32, 0.0, "recommended")])
    assert ap.apply_weekly(APPLY_AT)["applied"] is True
    # and without any cold feedback the +0.3 h row is fine (A/B)
    db.get_connection  # noqa: B018
    from src.db import get_connection  # reset the ledger for the B arm
    c = get_connection()
    c.execute("DELETE FROM auto_tune_changes")
    c.execute("DELETE FROM comfort_feedback")
    c.execute("DELETE FROM tuning_suggestions")
    c.commit()
    c.close()
    _setup(20.0)
    rts.clear_cache()
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])
    assert ap.apply_weekly(APPLY_AT)["applied"] is True


def test_s1_conflicts_with_comfort_job_proposal():
    _on()
    _setup(20.0)
    now = datetime.now(UTC)   # action_log is stamped with the real clock
    db.log_action(device="comfort", action="weekly_summary",
                  params={"proposal": {"key": KEY, "current": 20.0, "suggested": 20.5, "reason": "cold"}},
                  result="success", trigger="cron")
    _seed([_row(KEY, 20.0, 19.5, -32, 0.0, "recommended", ws=ap._expected_week_start(now))])
    out = ap.apply_weekly(now)
    assert out["applied"] is False, out
    assert out["candidates"][0]["skipped"] == "conflicts_comfort"
    # same direction as the comfort job is not a conflict
    chosen, cons = ap.evaluate_candidates(
        [_row(KEY, 20.0, 20.5, -4, 0.0, "recommended")],
        {"comfort_job_proposal": {"key": KEY, "current": 20.0, "suggested": 20.5}}, now)
    assert chosen is not None


def test_s2_tradeoff_size_cap_and_two_days():
    _setup(20.0)
    sig = {"n": 4, "cold": 0, "n_days": 2}
    assert _sel([_row(KEY, 20.0, 19.5, -32, 1.5, "trade-off")], sig) is None   # > 2 x 0.5 h
    chosen, cons = ap.evaluate_candidates([_row(KEY, 20.0, 19.5, -32, 1.5, "trade-off")], sig, APPLY_AT)
    assert cons[0]["skipped"] == "comfort_cost_too_large"
    assert _sel([_row(KEY, 20.0, 19.5, -32, 0.9, "trade-off")], sig)["key"] == KEY


def test_s3_apply_after_local_midnight(monkeypatch):
    monkeypatch.setattr(config, "TUNING_REVIEW_HOUR_LOCAL", 23)
    monkeypatch.setattr(config, "AUTO_TUNE_APPLY_DELAY_MINUTES", 90)
    _on()
    _setup(20.0)
    at = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)               # Monday 00:30, review was Sunday 23:00
    assert ap._expected_week_start(at) == WS
    _seed([_row(KEY, 20.0, 19.5, -32, 0.3, "recommended")])
    assert ap.apply_weekly(at)["applied"] is True


def test_n1_only_newest_build_of_the_week():
    _on()
    _setup(20.0)
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")
    _seed([_row(KEY, 20.0, 19.5, -90, 0.0, "recommended")])     # older build, bigger saving
    _seed([_row("DHW_TEMP_NORMAL_C", 45.0, 44.0, -5, 0.0, "recommended")])
    out = ap.apply_weekly(APPLY_AT)
    assert out["applied"] is True and out["key"] == "DHW_TEMP_NORMAL_C"


def test_partial_review_is_recorded(_env):
    _on()
    _setup(20.0)
    _seed([_row(KEY, 20.0, 19.5, 8, -0.4, "comfort-first", {"status": "partial", "hours_below_current": 1.0})])
    _fb_at("cold", APPLY_AT - timedelta(hours=40))
    out = ap.apply_weekly(APPLY_AT)
    assert out["applied"] is True
    row = db.get_auto_tune_change(out["id"])
    assert row["note"] == f"review {WS}: comfort-first, costs 8p/week, partial review"
    assert _last_log()["params"]["review_status"] == "partial"
    assert "(parcial)" in _env[0]["message"]


def test_message_omits_zero_hours_clause(_env):
    _on()
    _setup(20.0)
    _seed([_row(KEY, 20.0, 19.5, -32, 0.0, "recommended")])
    ap.apply_weekly(APPLY_AT)
    assert "−32p/sem." in _env[0]["message"] and " h abaixo" not in _env[0]["message"]


def test_stale_between_review_and_apply():
    _on()
    _setup(20.0)
    _seed([_row(KEY, 20.0, 19.5, -32, 0.0, "recommended")])
    rts.set_setting(KEY, 20.5, actor="owner")                        # owner moves it after 09:00
    out = ap.apply_weekly(APPLY_AT)
    assert out["reason"] == "nothing_eligible" and out["candidates"][0]["skipped"] == "stale"
    assert float(rts.get_setting(KEY)) == 20.5


def test_n3_quota_fails_closed(monkeypatch):
    monkeypatch.setattr(db, "get_auto_tune_changes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert ap.weekly_quota_used(APPLY_AT) == 1


def test_n4_held_ping_names_eligible_row(_env, monkeypatch):
    sent = _env
    _on()
    _setup(20.0)
    rts.set_setting("INDOOR_SETPOINT_C", 21.5, actor="vacation_end")   # review saw 21 -> stale
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")
    auto_tune.propose_change("DHW_TEMP_NORMAL_C", 44.0, "tuning_review", now=APPLY_AT - timedelta(days=8))
    _seed([_row("INDOOR_SETPOINT_C", 21.0, 20.5, -80, 0.0, "recommended"),
           _row(KEY, 20.0, 19.5, -32, 0.0, "recommended")])
    monkeypatch.setattr(config, "AUTO_TUNE_NOTIFY_SKIPPED", True, raising=False)
    assert ap.apply_weekly(APPLY_AT)["reason"] == "busy"
    assert len(sent) == 1 and "Mínimo à noite" in sent[0]["message"] and "−32p" in sent[0]["message"]


def test_n7_ledger_extras_when_disabled():
    ex = ap.ledger_extras(APPLY_AT)
    assert ex["next_apply_at"] is None and ex["apply_enabled"] is False
    _on()
    assert ap.ledger_extras(APPLY_AT)["next_apply_at"].endswith("Z")
    config.TUNING_REVIEW_ENABLED = False
    assert ap.ledger_extras(APPLY_AT)["apply_enabled"] is False


def test_n8_review_running_backs_off_once(monkeypatch):
    from src import tuning_review
    from src.scheduler import runner

    class Fake:
        def __init__(self):
            self.jobs = []

        def add_job(self, fn, trigger=None, **kw):
            self.jobs.append((fn, trigger, kw))

    f = Fake()
    monkeypatch.setattr(runner, "_background_scheduler", f)
    monkeypatch.setattr(tuning_review, "_running", True)
    _on()
    ap.weekly_apply_job()
    assert len(f.jobs) == 1 and f.jobs[0][2]["id"] == "auto_tune_weekly_apply_retry"
    assert f.jobs[0][2]["kwargs"] == {"retry": True} and _last_log() is None
    ap.weekly_apply_job(retry=True)                                  # second time: no more back-off
    assert len(f.jobs) == 1 and _last_log()["params"]["reason"] == "review_running"


def test_s4_auto_tune_route_is_audible():
    r = db.get_notification_route("auto_tune")
    assert r and r["enabled"] and not r["silent"]


def test_n5_other_rows_wait_while_a_change_is_evaluating(monkeypatch):
    from src.analytics import recommendations as rec

    today = datetime.now(LON).date()
    ws = (today - timedelta(days=3)).isoformat()
    monkeypatch.setattr(rec, "_live_value", lambda k: float(rts.get_setting(k)))
    _on()
    _setup(20.0)
    rts.set_setting("DHW_TEMP_NORMAL_C", 45.0, actor="vacation_end")
    db.save_tuning_suggestions([_row("DHW_TEMP_NORMAL_C", 45, 44, -9, 0.0, "recommended", ws=ws)])
    assert rec.tuning_items(today, LON)[0]["kind"] == "setting"
    assert auto_tune.propose_change(KEY, 19.5, "tuning_review", now=datetime.now(UTC))["applied"]
    it = rec.tuning_items(today, LON)[0]
    assert it["kind"] == "suggestion" and it["action"] is None
    assert it["detail"].startswith("Aguarde: outra mudança em avaliação.")


def test_n6_day_n_uses_the_composer_day(monkeypatch):
    from src.analytics import recommendations as rec

    today = datetime.now(LON).date()
    monkeypatch.setattr(rec, "_live_value", lambda k: float(rts.get_setting(k)))
    _on()
    _setup(20.0)
    ws = (today - timedelta(days=3)).isoformat()
    db.save_tuning_suggestions([_row(KEY, 20, 19.5, -32, 0.3, "recommended", ws=ws)])
    assert auto_tune.propose_change(KEY, 19.5, "tuning_review", now=datetime.now(UTC))["applied"]
    it = rec.tuning_items(today + timedelta(days=2), LON)[0]
    assert it["ledger"]["day_n"] == 3 and "avaliando dia 3/7" in it["headline"]


@pytest.mark.parametrize("verdict,hrs,intent", [
    ("recommended", 0.0, "saving"), ("trade-off", 0.4, "trade_off"), ("comfort-first", -0.5, "comfort_up")])
def test_intent_per_verdict_reaches_the_ledger(verdict, hrs, intent):
    _on()
    _setup(20.0)
    pence = 8 if verdict == "comfort-first" else -32
    payload = {"hours_below_current": 1.0} if verdict == "comfort-first" else {}
    if verdict != "recommended":
        _fb_at("cold" if verdict == "comfort-first" else "ok", APPLY_AT - timedelta(hours=30))
        _fb_at("cold" if verdict == "comfort-first" else "ok", APPLY_AT - timedelta(hours=60))
    _seed([_row(KEY, 20.0, 19.5 if verdict != "comfort-first" else 20.5, pence, hrs, verdict, payload)])
    out = ap.apply_weekly(APPLY_AT)
    assert out["applied"] is True, out
    assert db.get_auto_tune_change(out["id"])["intent"] == intent


def test_comfort_source_keeps_derived_intent(monkeypatch):
    _on()
    _setup(20.0)
    monkeypatch.setattr(cf, "weekly_summary", lambda ws: {
        "proposal": {"key": KEY, "current": 20.0, "suggested": 20.5, "reason": "cold"}, "week_start": "x"})
    cf.weekly_job(COMFORT_AT)
    assert db.get_auto_tune_changes(1, key=KEY)[0]["intent"] == "comfort_up"
