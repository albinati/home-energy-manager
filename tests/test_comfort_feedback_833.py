"""#833 — comfort feedback loop."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import db, runtime_settings, telegram_transport
from src.analytics import comfort_feedback as cf
from src.config import config

COSY = "E-1R-COSY-22-12-08-H"
LON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 7, 17, 0, tzinfo=UTC)  # 18:00 BST = peak


def _z(t):
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _price(h):
    return 38.17 if 16 <= h < 19 else 12.49 if (4 <= h < 7 or 13 <= h < 16 or h >= 22) else 25.45


def _seed_rates(d: date):
    rows = []
    for h in range(24):
        for m in (0, 30):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC)
            rows.append({"valid_from": _z(t), "valid_to": _z(t + timedelta(minutes=30)),
                         "value_inc_vat": _price(h)})
    db.save_agile_rates(rows, COSY)


def _seed_rooms(rooms, at=None):
    at = (at or datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    db.save_indoor_readings([{"captured_at": at, "room": r, "temp_c": t} for r, t in rooms.items()])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    from src.analytics import load_expected
    load_expected.clear_cache()
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", COSY)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    monkeypatch.setattr(config, "INDOOR_COMFORT_AGGREGATE", "mean")
    monkeypatch.setattr(config, "DAIKIN_LWT_SOURCE", "lp", raising=False)
    runtime_settings.clear_cache()


def test_writer_enriches_context():
    _seed_rates(NOW.date())
    _seed_rooms({"cozinha": 18.9, "corredor": 20.1})
    db.insert_daikin_telemetry({"source": "live", "outdoor_temp_c": 7.5})
    st = _z(NOW - timedelta(hours=1))
    en = _z(NOW + timedelta(hours=1))
    with db._lock:
        c = db.get_connection()
        c.execute("INSERT INTO action_schedule (date,start_time,end_time,device,action_type,params,status,created_at)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (NOW.date().isoformat(), st, en, "daikin", "lwt_preheat", json.dumps({"lwt_offset": -2}),
                   "active", st))
        c.commit(); c.close()
    # the writer reads indoor within the stale window of REAL now -> seed uses real now
    row = db.insert_comfort_feedback(verdict="cold", source="telegram", room="Cozinha", note="x", now=NOW)
    assert row["room"] == "cozinha" and row["verdict"] == "cold"
    assert row["indoor_c"] == pytest.approx(19.5)
    assert row["rooms_c"] == {"cozinha": 18.9, "corredor": 20.1}
    assert row["outdoor_c"] == pytest.approx(7.5)
    assert row["band"] == "peak"
    assert row["lwt_offset_c"] == -2.0
    assert row["lwt_source"] == "lp"
    assert len(db.get_comfort_feedback(30)) == 1


def test_writer_rejects_bad_input():
    with pytest.raises(ValueError):
        db.insert_comfort_feedback(verdict="meh")
    with pytest.raises(ValueError):
        db.insert_comfort_feedback(verdict="ok", source="smoke")


def test_api_post_admin_gated_get_viewer(monkeypatch):
    from starlette.testclient import TestClient
    from src.api.main import app
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", True, raising=False)
    monkeypatch.setattr(config, "HEM_ADMIN_TOKEN", "ADMINTOK", raising=False)
    monkeypatch.setenv("HEM_ADMIN_TOKEN", "ADMINTOK")
    cl = TestClient(app)
    assert cl.post("/api/v1/comfort/feedback", json={"verdict": "cold"}).status_code == 401
    r = cl.post("/api/v1/comfort/feedback", json={"verdict": "cold", "room": "sala"},
                headers={"Authorization": "Bearer ADMINTOK"})
    assert r.status_code == 200 and r.json()["verdict"] == "cold"
    assert cl.post("/api/v1/comfort/feedback", json={"verdict": "bogus"},
                   headers={"Authorization": "Bearer ADMINTOK"}).status_code in (400, 422)
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", True, raising=False)
    g = cl.get("/api/v1/comfort/feedback?days=30")  # no token: viewer
    assert g.status_code == 200
    j = g.json()
    assert len(j["rows"]) == 1 and j["by_room"]["sala"]["cold"] == 1 and "weekly" in j


@pytest.mark.parametrize(("text", "kind", "verdict", "room", "note"), [
    ("/conforto frio", "feedback", "cold", None, None),
    ("/conforto quente cozinha muito abafado", "feedback", "hot", "cozinha", "muito abafado"),
    ("/comfort cold Kitchen bad night", "feedback", "cold", None, "Kitchen bad night"),
    ("/comfort@hembot ok", "feedback", "ok", None, None),
    ("/conforto", "usage", None, None, None),
    ("/conforto morno", "unknown", None, None, None),
    ("/other x", "not_comfort", None, None, None),
    ("hello", "not_comfort", None, None, None),
])
def test_parser(text, kind, verdict, room, note):
    p = cf.parse_command(text, ["cozinha", "corredor"])
    assert p["kind"] == kind
    if kind == "feedback":
        assert (p["verdict"], p["room"], p["note"]) == (verdict, room, note)


def test_parser_room_accent_and_en():
    p = cf.parse_command("/comfort hot Cozinha", ["cozinha"])
    assert p["room"] == "cozinha"
    p = cf.parse_command("/conforto frio sótão", ["sotao"])
    assert p["room"] == "sotao"


# ------------------------------------------------------------------ poller
class _Tg:
    def __init__(self, updates, status=200, payload_extra=None):
        self.updates = updates
        self.status = status
        self.extra = payload_extra or {}
        self.sent = []
        self.kw = []
        self.offsets = []

    def install(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "1:abc", raising=False)
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42", raising=False)
        monkeypatch.setattr(config, "TELEGRAM_INBOUND_ENABLED", True, raising=False)
        monkeypatch.setattr(config, "TELEGRAM_OWNER_USER_ID", "", raising=False)

        def gu(offset=None, timeout_s=0):
            self.offsets.append(offset)
            if self.status != 200:
                return self.status, ({"ok": False} | self.extra)
            return 200, {"ok": True, "result": [u for u in self.updates
                                                 if offset is None or u["update_id"] >= offset]}
        monkeypatch.setattr(telegram_transport, "get_updates", gu)
        monkeypatch.setattr(telegram_transport, "send_message",
                            lambda text, **kw: (self.sent.append(text), self.kw.append(kw)) and True)


def _u(uid, chat, text, date=None, user=None):
    m = {"chat": {"id": chat}, "text": text, "date": int(date or datetime.now(UTC).timestamp())}
    if user is not None:
        m["from"] = {"id": user}
    return {"update_id": uid, "message": m}


def test_poller_owner_only_offset_persisted_reply_once(monkeypatch):
    _seed_rooms({"cozinha": 18.9, "corredor": 20.1})
    tg = _Tg([_u(10, 42, "/conforto frio cozinha"), _u(11, 999, "/conforto quente"),
              _u(12, 42, "just chatting"), _u(13, 42, "/conforto")])
    tg.install(monkeypatch)
    assert cf.poll_telegram_once() == 2
    assert len(tg.sent) == 2
    assert tg.sent[0].startswith("Registrado: frio") and "cozinha 18.9 °C" in tg.sent[0]
    assert all(k == {"convert_markdown": False, "parse_mode": ""} for k in tg.kw)
    assert "Uso:" in tg.sent[1]
    rows = db.get_comfort_feedback(1)
    assert len(rows) == 1 and rows[0]["room"] == "cozinha" and rows[0]["source"] == "telegram"
    assert db.get_kv(cf.OFFSET_KEY) == "14"
    # "restart": a second poll resumes from the stored offset, nothing replayed
    assert cf.poll_telegram_once() == 0
    assert tg.offsets[-1] == 14 and len(tg.sent) == 2


def test_poller_swallows_http_errors(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "1:abc", raising=False)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42", raising=False)
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(telegram_transport.requests, "get", boom)
    monkeypatch.setattr(config, "TELEGRAM_INBOUND_ENABLED", True, raising=False)
    assert cf.poll_telegram_once() == 0
    assert telegram_transport.get_updates(None) == (0, None)
    assert db.get_kv(cf.OFFSET_KEY) is None


def test_poller_disabled_or_unconfigured(monkeypatch):
    tg = _Tg([_u(1, 42, "/conforto frio")])
    tg.install(monkeypatch)
    monkeypatch.setattr(config, "TELEGRAM_INBOUND_ENABLED", False, raising=False)
    assert cf.poll_telegram_once() == 0 and tg.offsets == []


def test_get_updates_wire(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "1:abc", raising=False)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42", raising=False)
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params, timeout=timeout)
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": [{"update_id": 5}]})
    monkeypatch.setattr(telegram_transport.requests, "get", fake_get)
    assert telegram_transport.get_updates(7) == (200, {"ok": True, "result": [{"update_id": 5}]})
    assert seen["url"].endswith("/bot1:abc/getUpdates")
    assert seen["params"]["offset"] == 7 and seen["params"]["timeout"] == 0 and seen["timeout"] == 10


# ------------------------------------------------------------------ weekly
WEEK = date(2026, 10, 5)  # Monday


def _fb(verdict, day_off, hour_local, *, band=None, indoor=None, room=None):
    at = datetime(2026, 10, 5 + day_off, hour_local, 0, tzinfo=LON).astimezone(UTC)
    with db._lock:
        c = db.get_connection()
        c.execute("INSERT INTO comfort_feedback (at_utc,source,verdict,room,indoor_c,band,created_at_utc)"
                  " VALUES (?,?,?,?,?,?,?)", (_z(at), "api", verdict, room, indoor, band, _z(at)))
        c.commit(); c.close()


def test_proposal_night_cold():
    floor = float(runtime_settings.get_setting("LP_W3_NIGHT_FLOOR_C"))
    _fb("cold", 0, 23, indoor=floor + 0.1); _fb("cold", 1, 3, indoor=floor)
    s = cf.weekly_summary(WEEK)
    assert s["n"] == 2 and s["cold"] == 2 and s["by_hour_bucket"]["night"]["cold"] == 2
    assert s["proposal"]["key"] == "LP_W3_NIGHT_FLOOR_C"
    assert s["proposal"]["suggested"] == pytest.approx(floor + 0.5)
    assert s["mean_indoor_when_cold"] == pytest.approx(floor + 0.05)
    assert cf.external_comfort_signal(WEEK) == s["proposal"]


def test_proposal_night_cold_needs_low_indoor():
    floor = float(runtime_settings.get_setting("LP_W3_NIGHT_FLOOR_C"))
    _fb("cold", 0, 23, indoor=floor + 2); _fb("cold", 1, 3, indoor=floor + 2)
    assert cf.weekly_summary(WEEK)["proposal"] is None


def test_proposal_peak_cold():
    _fb("cold", 0, 17, band="peak"); _fb("cold", 2, 18, band="peak")
    p = cf.weekly_summary(WEEK)["proposal"]
    assert p["key"] == "LP_W3_PEAK_COAST_DELTA_C"
    assert p["suggested"] == pytest.approx(p["current"] - 0.5)


def test_proposal_hot_inverse_and_blocked_by_cold():
    for d in range(3):
        _fb("hot", d, 14, band="day")
    p = cf.weekly_summary(WEEK)["proposal"]
    assert p["key"] == "LP_W3_PEAK_COAST_DELTA_C" and p["suggested"] == pytest.approx(p["current"] + 0.5)
    _fb("cold", 3, 10)
    assert cf.weekly_summary(WEEK)["proposal"] is None


def test_week_window_excludes_other_weeks():
    _fb("cold", -1, 17, band="peak"); _fb("cold", 7, 17, band="peak")
    assert cf.weekly_summary(WEEK)["n"] == 0


def test_auto_tune_off_by_default_and_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_settings, "set_setting",
                        lambda k, v, actor="api": calls.append((k, v, actor)) or v)
    _fb("cold", 0, 17, band="peak"); _fb("cold", 2, 18, band="peak")
    now = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)  # Sunday
    assert config.COMFORT_FEEDBACK_AUTO_TUNE is False
    assert cf.weekly_job(now)["proposal"] is not None
    assert calls == []
    with db._lock:
        c = db.get_connection()
        logged = c.execute("SELECT COUNT(*) FROM action_log WHERE device='comfort' AND action='weekly_summary'").fetchone()[0]
        c.close()
    assert logged == 1
    monkeypatch.setattr(config, "COMFORT_FEEDBACK_AUTO_TUNE", True)
    cf.weekly_job(now)
    assert len(calls) == 1 and calls[0][0] == "LP_W3_PEAK_COAST_DELTA_C" and calls[0][2] == "comfort_feedback_auto_tune"
    cur = float(runtime_settings.get_setting("LP_W3_PEAK_COAST_DELTA_C"))
    assert abs(calls[0][1] - cur) <= 0.5 + 1e-9


# ---------------------------------------------------------- review follow-ups
def test_inbound_default_off():
    assert type(config).__dict__.get("TELEGRAM_INBOUND_ENABLED") is False or config.TELEGRAM_INBOUND_ENABLED in (False, True)
    import os
    if "TELEGRAM_INBOUND_ENABLED" not in os.environ:
        assert config.TELEGRAM_INBOUND_ENABLED is False


def test_owner_other_slash_command_gets_no_reply(monkeypatch):
    tg = _Tg([_u(1, 42, "/other thing"), _u(2, 42, "/conforto morno")])
    tg.install(monkeypatch)
    assert cf.poll_telegram_once() == 1
    assert len(tg.sent) == 1 and tg.sent[0].startswith("Uso:")
    assert cf.handle_message("/start") is None


def test_edited_message_and_channel_post_skipped(monkeypatch):
    tg = _Tg([{"update_id": 1, "edited_message": {"chat": {"id": 42}, "text": "/conforto frio", "date": 1}},
              {"update_id": 2, "channel_post": {"chat": {"id": 42}, "text": "/conforto frio", "date": 1}}])
    tg.install(monkeypatch)
    assert cf.poll_telegram_once() == 0 and tg.sent == [] and db.get_comfort_feedback(1) == []
    assert db.get_kv(cf.OFFSET_KEY) == "3"


def test_backlog_stale_ignored_fresh_uses_message_time(monkeypatch):
    _seed_rates(NOW.date())
    now = datetime.now(UTC)
    old = now - timedelta(hours=2)
    fresh = now - timedelta(seconds=30)
    tg = _Tg([_u(1, 42, "/conforto frio", date=old.timestamp()),
              _u(2, 42, "/conforto frio", date=old.timestamp()),
              _u(3, 42, "/conforto ok", date=fresh.timestamp())])
    tg.install(monkeypatch)
    assert cf.poll_telegram_once(now) == 1
    assert tg.sent.count("mensagem antiga ignorada") == 1
    rows = db.get_comfort_feedback(1)
    assert len(rows) == 1 and rows[0]["verdict"] == "ok"
    got = datetime.fromisoformat(rows[0]["at_utc"].replace("Z", "+00:00"))
    assert abs((got - fresh).total_seconds()) < 2


def test_owner_user_id_guard(monkeypatch):
    tg = _Tg([_u(1, 42, "/conforto frio", user=7), _u(2, 42, "/conforto ok", user=8)])
    tg.install(monkeypatch)
    monkeypatch.setattr(config, "TELEGRAM_OWNER_USER_ID", "8", raising=False)
    assert cf.poll_telegram_once() == 1
    assert [r["verdict"] for r in db.get_comfort_feedback(1)] == ["ok"]


def test_failures_alert_once_and_back_off(monkeypatch):
    tg = _Tg([], status=409)
    tg.install(monkeypatch)
    alerts, defers = [], []
    from src import notifier
    monkeypatch.setattr(notifier, "notify_risk", lambda m, extra=None: alerts.append(m))
    monkeypatch.setattr(cf, "_defer_poll", lambda sec: defers.append(sec))
    for _ in range(5):
        cf.poll_telegram_once()
    assert len(alerts) == 1 and defers and defers[-1] == 900
    tg.status = 200
    cf.poll_telegram_once()
    assert db.get_kv(cf.FAIL_KEY) == "0"


def test_429_honours_retry_after(monkeypatch):
    tg = _Tg([], status=429, payload_extra={"parameters": {"retry_after": 77}})
    tg.install(monkeypatch)
    defers = []
    monkeypatch.setattr(cf, "_defer_poll", lambda sec: defers.append(sec))
    cf.poll_telegram_once()
    assert defers == [77]


def test_room_resolution_two_tokens_and_spaces():
    p = cf.parse_command("/conforto frio sala jantar muito frio", ["sala_jantar", "cozinha"])
    assert p["room"] == "sala_jantar" and p["note"] == "muito frio"
    p = cf.parse_command("/conforto frio Sala_Jantar", ["sala jantar"])
    assert p["room"] == "sala jantar"


def test_agile_tier_keys_map_to_peak():
    assert db.normalize_comfort_band("severe_peak") == "peak"
    assert db.normalize_comfort_band("expensive") == "peak"
    assert db.normalize_comfort_band("cheap") == "cheap"


def _tool(name):
    from src.mcp_server import build_mcp
    return build_mcp()._tool_manager._tools[name].fn


def test_mcp_record_comfort_feedback():
    _seed_rooms({"cozinha": 18.9, "sala_jantar": 20.0})
    rec = _tool("record_comfort_feedback")
    r = rec("frio", "Cozinha", "ta gelado")
    assert r["ok"] and r["row"]["source"] == "openclaw"
    assert r["row"]["verdict"] == "cold" and r["row"]["room"] == "cozinha" and r["row"]["note"] == "ta gelado"
    assert r["ack"].startswith("Registrado: frio")
    r = rec("QUENTE", "sala jantar")
    assert r["row"]["verdict"] == "hot" and r["row"]["room"] == "sala_jantar"
    r = rec("ok", "garagem", "x")          # unknown room -> note
    assert r["row"]["room"] is None and r["row"]["note"] == "garagem x"
    bad = rec("morno")
    assert bad["ok"] is False and "verdict" in bad["error"]
    assert rec("bem")["ok"] is False        # aliases are Telegram-only
    g = _tool("get_comfort_feedback")(30)
    assert g["ok"] and len(g["rows"]) == 3 and "weekly" in g


def test_auto_tune_real_set_setting_writes_config_audit(monkeypatch):
    monkeypatch.setattr(config, "COMFORT_FEEDBACK_AUTO_TUNE", True)
    _fb("cold", 0, 17, band="peak"); _fb("cold", 2, 18, band="peak")
    before = float(runtime_settings.get_setting("LP_W3_PEAK_COAST_DELTA_C"))
    cf.weekly_job(datetime(2026, 10, 11, 9, 0, tzinfo=UTC))
    assert float(runtime_settings.get_setting("LP_W3_PEAK_COAST_DELTA_C")) == pytest.approx(before - 0.5)
    rows = db.get_config_audit(key="LP_W3_PEAK_COAST_DELTA_C", limit=5)
    assert rows and rows[0].get("actor") == "comfort_feedback_auto_tune"
