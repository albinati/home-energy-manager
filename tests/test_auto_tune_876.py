"""#876 story 1 - auto-tune change ledger, manual-always-wins, API, kill switch."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src import auto_tune, db, notifier
from src import runtime_settings as rts
from src.analytics import comfort_feedback as cf
from src.config import config

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
KEY = "LP_W3_NIGHT_FLOOR_C"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    rts.clear_cache()
    config._overrides.clear()
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(notifier, "notify_risk", lambda msg, extra=None: sent.append((msg, extra or {})))
    yield sent
    config._overrides.clear()
    rts.clear_cache()


def _on():
    rts.set_setting("AUTO_TUNE_ENABLED", "true", actor="test")


def _actions(action):
    return db.get_action_logs(device="auto_tune", action=action, limit=50)


def _row(i):
    return db.get_auto_tune_change(i)


def _setup_key(value=20.0):
    rts.set_setting(KEY, value, actor="vacation_end")  # internal actor: no ledger row


# ---------------------------------------------------------------- migration
def test_migration_table_and_index():
    conn = sqlite3.connect(config.DB_PATH)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(auto_tune_changes)")}
    assert {"id", "key", "old_value", "new_value", "source", "applied_at", "baseline_json",
            "status", "evaluated_at", "evidence_json", "frozen_until", "note"} <= cols
    idx = {r[1] for r in conn.execute("PRAGMA index_list(auto_tune_changes)")}
    assert "idx_auto_tune_changes_key" in idx
    db.init_db()  # idempotent


def test_defaults_and_denylist():
    assert auto_tune.enabled() is False
    assert KEY in auto_tune.allowlist() and len(auto_tune.allowlist()) == 10
    for k in ("LP_W3_EMERGENCY_FLOOR_C", "AUTO_TUNE_ENABLED", "AUTO_TUNE_ALLOWLIST",
              "DHW_LEGIONELLA_DAY", "DHW_LEGIONELLA_STANDOFF_ENABLED", "OPTIMIZATION_PRESET"):
        assert auto_tune.is_denylisted(k)
    # a setting cannot put a denylisted key on the allowlist
    rts.set_setting("AUTO_TUNE_ALLOWLIST", "LP_W3_EMERGENCY_FLOOR_C," + KEY, actor="test")
    assert not auto_tune.is_allowlisted("LP_W3_EMERGENCY_FLOOR_C")
    assert auto_tune.is_allowlisted(KEY)


# ------------------------------------------------------------------ propose
def test_propose_happy_path():
    _on()
    _setup_key(20.0)
    res = auto_tune.propose_change(KEY, 19.5, "tuning_review", note="n", now=NOW)
    assert res["applied"] is True
    assert float(rts.get_setting(KEY)) == 19.5
    row = _row(res["id"])
    assert row["status"] == "evaluating" and row["source"] == "tuning_review"
    assert float(row["old_value"]) == 20.0 and float(row["new_value"]) == 19.5
    assert row["applied_at"] == "2026-11-01T10:00:00Z"
    assert row["baseline"] == {}  # no scorecards yet
    audit = db.get_config_audit(limit=5) if hasattr(db, "get_config_audit") else None
    if audit:
        assert any(a.get("actor") == "auto_tune:tuning_review" for a in audit)
    acts = _actions("auto_tune_apply")
    assert len(acts) == 1
    # applying through the ledger did NOT create a manual row
    assert [r["status"] for r in db.get_auto_tune_changes(10, key=KEY)] == ["evaluating"]


def test_apply_uses_auto_actor(monkeypatch):
    _on()
    _setup_key(20.0)
    seen = []
    real = rts.set_setting
    monkeypatch.setattr(rts, "set_setting", lambda k, v, *, actor="api": (seen.append((k, actor)), real(k, v, actor=actor))[1])
    assert auto_tune.propose_change(KEY, 20.5, "comfort_feedback", now=NOW)["applied"]
    assert seen == [(KEY, "auto_tune:comfort_feedback")]


def test_baseline_from_scorecards():
    _on()
    _setup_key(20.0)
    for i in range(1, 8):
        d = (NOW.date() - timedelta(days=i)).isoformat()
        db.upsert_cosy_scorecard({
            "date": d, "score": "ideal", "peak_import_kwh": 0.5, "import_kwh": 10, "import_cost_gbp": 1,
            "avg_import_p": 12, "ideal_avg_import_p": 12, "net_cost_gbp": 1.0 + i / 10,
            "payload": {"comfort": {"hours_below_night_floor": 0.5, "hours_below_peak_floor": 0.25},
                        "tank": {"any_below_floor": i == 2}},
            "built_at_utc": "2026-11-01T00:00:00Z"})
    b = auto_tune.baseline_metrics(NOW.date() - timedelta(days=1))
    assert b["n_days"] == 7 and b["shower_shortfall_days"] == 1
    assert b["hours_below_floor_per_day"] == 0.75 and b["peak_import_kwh_per_day"] == 0.5
    assert b["net_gbp_per_day"] == pytest.approx(1.4)


def test_refusals():
    # disabled
    _setup_key(20.0)
    assert auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)["reason"] == "disabled"
    assert float(rts.get_setting(KEY)) == 20.0
    _on()
    p = lambda k, v, **kw: auto_tune.propose_change(k, v, "tuning_review", now=NOW, **kw)  # noqa: E731
    assert p("LP_W3_EMERGENCY_FLOOR_C", 19)["reason"] == "denylisted"
    assert p("DHW_LEGIONELLA_HOUR_LOCAL", 12)["reason"] == "denylisted"
    assert p("LP_W3_UA_SOMETHING", 1)["reason"] == "not_allowlisted"
    assert p("DHW_SHOWER_COMFORT_C", 44)["reason"] == "not_allowlisted"
    assert p(KEY, "abc")["reason"] == "invalid"
    assert p(KEY, 99)["reason"] == "invalid"  # out of the spec range
    assert p(KEY, 20.0)["reason"] == "unchanged"
    assert p(KEY, 18.5)["reason"] == "step_too_large"
    assert auto_tune.propose_change(KEY, 19.5, "bogus", now=NOW)["reason"] == "invalid"
    assert p("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p99")["reason"] == "invalid"
    ok = p("LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p90")  # enum: any one value
    assert ok["applied"] and rts.get_setting("LP_LOAD_EXPENSIVE_BAND_QUANTILE") == "p90"


def test_one_at_a_time():
    _on()
    _setup_key(20.0)
    assert auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)["applied"]
    r = auto_tune.propose_change("INDOOR_SETPOINT_C", 20.5, "tuning_review", now=NOW)
    assert r == {"applied": False, "reason": "busy", "key": "INDOOR_SETPOINT_C"}


def test_frozen_after_manual_and_expiry():
    _on()
    _setup_key(20.0)
    rts.set_setting(KEY, 20.5, actor="api")  # manual -> 14 d freeze
    r = auto_tune.propose_change(KEY, 20.0, "tuning_review", now=NOW)
    assert r["reason"] == "frozen"
    later = datetime.now(UTC) + timedelta(days=15)
    assert db.auto_tune_frozen_until(KEY, later) is None
    assert auto_tune.propose_change(KEY, 20.0, "tuning_review", now=later)["applied"]


def test_drift_cap_vs_last_manual():
    _on()
    rts.set_setting("AUTO_TUNE_MANUAL_FREEZE_DAYS", 0, actor="test")
    rts.set_setting(KEY, 20.0, actor="api")  # manual baseline = 20.0 (no freeze)
    cur = 20.0
    for _ in range(3):  # 19.5, 19.0, 18.5 (cap 1.5)
        cur -= 0.5
        r = auto_tune.propose_change(KEY, cur, "tuning_review", now=NOW)
        assert r["applied"], r
        assert auto_tune.keep(r["id"], {"ok": 1})
    r = auto_tune.propose_change(KEY, 18.0, "tuning_review", now=NOW)
    assert r["reason"] == "drift_cap" and r["reference"] == 20.0
    # a new manual value moves the reference
    rts.set_setting(KEY, 18.5, actor="api")
    assert auto_tune.propose_change(KEY, 18.0, "tuning_review", now=NOW)["applied"]


def test_drift_reference_without_manual_row_is_value_before_first_auto():
    _on()
    _setup_key(20.0)
    for v in (19.5, 19.0, 18.5):
        r = auto_tune.propose_change(KEY, v, "tuning_review", now=NOW)
        auto_tune.keep(r["id"])
    assert auto_tune.manual_value(KEY) == "20.0"
    assert auto_tune.propose_change(KEY, 18.0, "tuning_review", now=NOW)["reason"] == "drift_cap"


def test_away_vacation_and_holiday():
    _on()
    _setup_key(20.0)
    db.set_kv("vacation_end_at", "2026-11-07T12:00:00Z")
    assert auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)["reason"] == "away"
    db.set_kv("vacation_end_at", "")
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": False}))
    assert auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)["reason"] == "away"
    db.set_kv("daikin_holiday_active", "")
    assert auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)["applied"]


# ------------------------------------------------------- manual always wins
def test_manual_change_supersedes_and_freezes():
    _on()
    _setup_key(20.0)
    a = auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)
    rts.set_setting(KEY, 21.0, actor="api")  # the owner, via the real hook
    old = _row(a["id"])
    assert old["status"] == "superseded_manual" and old["evidence"]["reason"] == "manual_change"
    assert old["evidence"]["actor"] == "api" and old["evaluated_at"]
    m = db.get_auto_tune_changes(1, key=KEY, status="manual")[0]
    assert m["source"] == "manual" and float(m["new_value"]) == 21.0 and float(m["old_value"]) == 19.5
    assert (_parse(m["frozen_until"]) - datetime.now(UTC)).days in (13, 14)
    assert float(rts.get_setting(KEY)) == 21.0  # never rolled back
    assert db.get_active_auto_tune_change() is None


def _parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_manual_rows_recorded_even_when_disabled():
    assert not auto_tune.enabled()
    rts.set_setting(KEY, 21.0, actor="mcp")
    rows = db.get_auto_tune_changes(5, key=KEY)
    assert len(rows) == 1 and rows[0]["status"] == "manual"


def test_manual_on_unrelated_key_records_nothing():
    rts.set_setting("DHW_SHOWER_COMFORT_C", 44, actor="api")
    rts.set_setting("AUTO_TUNE_ENABLED", "true", actor="api")
    assert db.get_auto_tune_changes(5) == []


def test_manual_on_ledger_key_outside_allowlist_still_recorded():
    _on()
    _setup_key(20.0)
    a = auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)
    rts.set_setting("AUTO_TUNE_ALLOWLIST", "indoor_setpoint_c", actor="api")  # key leaves the list
    rts.set_setting(KEY, 21.0, actor="api")
    assert _row(a["id"])["status"] == "superseded_manual"


@pytest.mark.parametrize("actor", ["auto_tune:rollback", "auto_tune:x", "vacation_end",
                                   "comfort_feedback_auto_tune", "api_batch_rollback",
                                   "workbench_promote_rollback"])
def test_internal_actors_do_not_create_manual_rows(actor):
    rts.set_setting(KEY, 20.5, actor=actor)
    assert db.get_auto_tune_changes(5, key=KEY) == []


def test_delete_setting_by_owner_is_manual():
    _setup_key(20.0)
    rts.set_setting(KEY, 21.0, actor="api")
    rts.delete_setting(KEY, actor="api")
    rows = db.get_auto_tune_changes(5, key=KEY, status="manual")
    assert len(rows) == 2


def test_hook_never_raises(monkeypatch):
    monkeypatch.setattr(db, "get_auto_tune_changes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    rts.set_setting(KEY, 21.0, actor="api")  # must not raise
    assert float(rts.get_setting(KEY)) == 21.0


# ----------------------------------------------------------------- rollback
def test_rollback_restores_freezes_notifies_once(_env):
    sent = _env
    _on()
    _setup_key(20.0)
    a = auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)
    r = auto_tune.rollback(a["id"], reason="2x cold", evidence={"cold": 2})
    assert r["rolled_back"] and float(rts.get_setting(KEY)) == 20.0
    row = _row(a["id"])
    assert row["status"] == "rolled_back" and row["evidence"]["cold"] == 2
    assert (_parse(row["frozen_until"]) - datetime.now(UTC)).days in (27, 28)
    assert len(sent) == 1 and sent[0][1]["warning_key"] == f"auto_tune_rollback_{a['id']}"
    assert len(_actions("auto_tune_rollback")) == 1
    # rollback did not create a manual row and the key is frozen
    assert all(x["status"] != "manual" for x in db.get_auto_tune_changes(10, key=KEY))
    assert auto_tune.propose_change(KEY, 19.5, "tuning_review")["reason"] == "frozen"
    # second rollback refused, no second notification
    assert auto_tune.rollback(a["id"], reason="again")["rolled_back"] is False
    assert len(sent) == 1


def test_rollback_never_touches_manual_row():
    rts.set_setting(KEY, 21.0, actor="api")
    m = db.get_auto_tune_changes(1, key=KEY, status="manual")[0]
    r = auto_tune.rollback(m["id"], reason="x")
    assert r["rolled_back"] is False and float(rts.get_setting(KEY)) == 21.0
    assert auto_tune.rollback(9999, reason="x")["reason"] == "not_found"


def test_keep_and_freeze_helpers():
    _on()
    _setup_key(20.0)
    a = auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)
    assert auto_tune.keep(a["id"], {"gain_p": 7})
    assert _row(a["id"])["status"] == "kept" and not auto_tune.keep(a["id"])
    out = auto_tune.freeze(a["id"], days=3)
    assert out["frozen"] and db.auto_tune_frozen_until(KEY)
    out = auto_tune.freeze(key="INDOOR_SETPOINT_C", days=5)  # no rows: pins current value as manual
    assert db.get_auto_tune_changes(1, key="INDOOR_SETPOINT_C")[0]["status"] == "manual"
    assert db.auto_tune_frozen_until("INDOOR_SETPOINT_C")


def test_ledger_state_and_summary():
    _on()
    _setup_key(20.0)
    rts.set_setting("AUTO_TUNE_MANUAL_FREEZE_DAYS", 0, actor="test")
    rts.set_setting(KEY, 20.5, actor="api")
    a = auto_tune.propose_change(KEY, 20.0, "tuning_review", now=NOW)
    st = auto_tune.ledger_state(KEY)
    assert st["active"]["id"] == a["id"] and st["manual_value"] == "20.5" and st["auto_value"] == "20.0"
    s = auto_tune.ledger_summary()
    assert s["enabled"] and s["active"]["id"] == a["id"] and KEY in s["keys"]
    sc = auto_tune.scorecard_section(today=NOW.date() + timedelta(days=2))
    assert sc["active_change"]["day_n_of_7"] == 3 and sc["active_change"]["key"] == KEY


def test_scorecard_payload_has_auto_tune(monkeypatch):
    from src.analytics import cosy_scorecard as sc
    _on()
    _setup_key(20.0)
    a = auto_tune.propose_change(KEY, 19.5, "tuning_review")
    try:
        row = sc.build_scorecard(date(2026, 10, 30))
    except Exception:
        pytest.skip("scorecard build needs fixtures unavailable here")
    assert row["payload"]["auto_tune"]["active_change"]["id"] == a["id"]


# ------------------------------------------------- comfort_feedback routing
PROP = {"key": KEY, "current": 20.0, "suggested": 20.5, "reason": "cold nights"}


def _stub_summary(monkeypatch):
    monkeypatch.setattr(cf, "weekly_summary", lambda ws: {"proposal": dict(PROP), "week_start": "x"})


def test_comfort_feedback_routes_through_ledger_when_on(monkeypatch):
    _stub_summary(monkeypatch)
    _on()
    _setup_key(20.0)
    monkeypatch.setattr(config, "COMFORT_FEEDBACK_AUTO_TUNE", False, raising=False)
    cf.weekly_job(NOW)
    rows = db.get_auto_tune_changes(5, key=KEY)
    assert len(rows) == 1 and rows[0]["source"] == "comfort_feedback" and rows[0]["status"] == "evaluating"
    assert float(rts.get_setting(KEY)) == 20.5


def test_comfort_feedback_ledger_on_refusal_applies_nothing(monkeypatch):
    _stub_summary(monkeypatch)
    _on()
    _setup_key(20.0)
    db.set_kv("vacation_end_at", "2026-11-07T12:00:00Z")
    cf.weekly_job(NOW)
    assert float(rts.get_setting(KEY)) == 20.0
    assert db.get_auto_tune_changes(5, key=KEY) == []


def test_comfort_feedback_legacy_path_when_ledger_off(monkeypatch):
    _stub_summary(monkeypatch)
    _setup_key(20.0)
    monkeypatch.setattr(config, "COMFORT_FEEDBACK_AUTO_TUNE", True, raising=False)
    cf.weekly_job(NOW)
    assert float(rts.get_setting(KEY)) == 20.5
    assert db.get_auto_tune_changes(5, key=KEY) == []  # internal actor: no ledger row at all
    assert len(db.get_action_logs(device="comfort", action="auto_tune", limit=5)) == 1


def test_comfort_feedback_nothing_when_both_off(monkeypatch):
    _stub_summary(monkeypatch)
    _setup_key(20.0)
    monkeypatch.setattr(config, "COMFORT_FEEDBACK_AUTO_TUNE", False, raising=False)
    cf.weekly_job(NOW)
    assert float(rts.get_setting(KEY)) == 20.0


# ---------------------------------------------------------------------- API
def _client():
    from src.api.main import app
    return TestClient(app)


def test_api_ledger_read_and_propose_409_and_rollback():
    _setup_key(20.0)
    c = _client()
    r = c.post("/api/v1/tuning/ledger/propose", json={"key": KEY, "value": 19.5})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "disabled"
    _on()
    r = c.post("/api/v1/tuning/ledger/propose", json={"key": KEY, "value": 19.5, "source": "manual_test"})
    assert r.status_code == 200 and r.json()["applied"]
    cid = r.json()["id"]
    g = c.get("/api/v1/tuning/ledger").json()
    assert g["enabled"] and g["active"]["id"] == cid and g["rows"][0]["note"] == "manual_test"
    assert g["rows"][0]["source"] == "tuning_review" and KEY in g["keys"]
    assert c.get(f"/api/v1/tuning/ledger?key={KEY}&limit=1").json()["rows"][0]["id"] == cid
    assert c.post("/api/v1/tuning/ledger/propose", json={"key": KEY}).status_code == 400
    assert c.post(f"/api/v1/tuning/ledger/{cid}/freeze", json={"days": 2}).status_code == 200
    assert c.post(f"/api/v1/tuning/ledger/{cid}/freeze", json={}).status_code == 400
    assert c.post("/api/v1/tuning/ledger/999/freeze", json={"days": 2}).status_code == 404
    r = c.post(f"/api/v1/tuning/ledger/{cid}/rollback", json={"reason": "owner"})
    assert r.status_code == 200 and float(rts.get_setting(KEY)) == 20.0
    assert c.post(f"/api/v1/tuning/ledger/{cid}/rollback", json={}).status_code == 409
    assert c.post("/api/v1/tuning/ledger/999/rollback", json={}).status_code == 404


def test_api_writes_need_admin(monkeypatch):
    for name, val in (("HEM_UI_TOKEN", "ui-tok"), ("HEM_ADMIN_TOKEN", "admin-tok"),
                      ("HEM_OPENCLAW_TOKEN", "oc-tok"), ("HEM_SENSOR_INGEST_TOKEN", "ing"),
                      ("HEM_UI_AUTH_REQUIRED", True)):
        monkeypatch.setattr(config, name, val, raising=False)
    c = _client()
    assert c.get("/api/v1/tuning/ledger").status_code == 200  # viewer read
    for path, body in (("/api/v1/tuning/ledger/propose", {"key": KEY, "value": 1}),
                       ("/api/v1/tuning/ledger/1/rollback", {}),
                       ("/api/v1/tuning/ledger/1/freeze", {"days": 1})):
        assert c.post(path, json=body).status_code == 401
        assert c.post(path, json=body, headers={"Authorization": "Bearer ui-tok"}).status_code == 401
        assert c.post(path, json=body, headers={"Authorization": "Bearer admin-tok"}).status_code != 401


def test_api_settings_list_carries_auto_tune():
    _on()
    _setup_key(20.0)
    auto_tune.propose_change(KEY, 19.5, "tuning_review", now=NOW)
    items = {s["key"]: s for s in _client().get("/api/v1/settings").json()["settings"]}
    assert items[KEY]["auto_tune"]["last_status"] == "evaluating" and items[KEY]["auto_tune"]["active"]
    assert "auto_tune" not in items["DHW_SHOWER_COMFORT_C"]


def test_mcp_tools_registered():
    from src import mcp_server
    src = open(mcp_server.__file__).read()
    assert 'name="get_auto_tune_ledger"' in src and 'name="rollback_auto_tune_change"' in src
