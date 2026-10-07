"""#831 — daily Cosy scorecard: build, persist, alerts, job, API, brief line."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src import db
from src.analytics import cosy_scorecard as cs
from src.analytics import plan_fronts as pf
from src.config import config

LON = ZoneInfo("Europe/London")
COSY = "E-1R-COSY-22-12-08-H"
DAY = date(2026, 10, 5)


def _z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _price(h: int) -> float:
    if h in (4, 5, 6, 13, 14, 15, 22, 23):
        return 12.4868
    if h in (16, 17, 18):
        return 38.174
    return 25.4461


def _seed_rates(d: date) -> None:
    rows = []
    for h in range(24):
        for m in (0, 30):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC)
            rows.append({"valid_from": _z(t), "valid_to": _z(t + timedelta(minutes=30)),
                         "value_inc_vat": _price(h)})
    db.save_agile_rates(rows, COSY)


def _seed_telemetry(d: date) -> None:
    t = datetime(d.year, d.month, d.day, tzinfo=LON).astimezone(UTC)
    end = t + timedelta(days=1, hours=1)
    while t < end:
        h = t.astimezone(LON).hour
        imp = 2.0 if 4 <= h < 7 else (0.6 if 16 <= h < 19 else 0.0)
        db.save_pv_realtime_sample(
            _z(t), solar_power_kw=0.0, soc_pct=50.0, load_power_kw=1.0, grid_import_kw=imp,
            grid_export_kw=0.0, battery_charge_kw=0.5 if 4 <= h < 7 else 0.0, battery_discharge_kw=0.0,
        )
        t += timedelta(minutes=5)


def _seed_indoor(d: date) -> None:
    rows = []
    t = datetime(d.year, d.month, d.day, tzinfo=LON).astimezone(UTC)
    for i in range(96):
        lt = (t + timedelta(minutes=15 * i)).astimezone(LON)
        temp = 16.0 if 2 <= lt.hour < 4 else 21.5
        rows.append({"captured_at": _z(t + timedelta(minutes=15 * i)), "room": "corredor", "temp_c": temp})
    db.save_indoor_readings(rows)


def _seed_load_error(d: date) -> None:
    conn = db.get_connection()
    try:
        t = datetime(d.year, d.month, d.day, tzinfo=LON).astimezone(UTC)
        for i in range(48):
            st = t + timedelta(minutes=30 * i)
            h = st.astimezone(LON).hour
            fc, ac = (0.5, 1.0) if 16 <= h < 19 else (0.5, 0.5)
            conn.execute(
                "INSERT OR REPLACE INTO load_error_log (slot_time_utc, forecast_kwh, forecast_base_kwh,"
                " actual_kwh, error_kwh, built_at_utc) VALUES (?,?,?,?,?,?)",
                (_z(st), fc, fc, ac, ac - fc, "2026-10-06T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    pf.clear_cache()
    from src.analytics import load_expected, pnl

    load_expected.clear_cache()
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", COSY)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_STRUCTURE", "auto", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    monkeypatch.setattr(config, "SMART_TARIFF_START_DATE", "", raising=False)
    monkeypatch.setattr(pnl, "compute_daily_pnl", lambda d: {
        "import_kwh": 7.8, "import_cost_gbp": 1.436, "realised_net_cost_gbp": 1.9,
        "delta_vs_fixed_tariff_real_gbp": 0.4,
    })
    sent: list[tuple[str, dict]] = []
    from src import notifier

    monkeypatch.setattr(notifier, "notify_risk", lambda msg, extra=None: sent.append((msg, extra or {})))
    return sent


@pytest.fixture
def sent(_env):
    return _env


def _seed_day(d: date = DAY) -> None:
    _seed_rates(d)
    _seed_telemetry(d)
    _seed_indoor(d)
    _seed_load_error(d)


def test_build_scorecard_seeded_day():
    _seed_day()
    row = cs.build_scorecard(DAY)
    p = row["payload"]
    assert row["date"] == "2026-10-05"
    assert p["structure"] == "banded"
    assert row["import_kwh"] == 7.8
    assert row["peak_import_kwh"] == pytest.approx(1.8, abs=0.1)
    assert row["ideal_avg_import_p"] == pytest.approx(12.49, abs=0.01)
    assert row["avg_import_p"] == pytest.approx(18.41, abs=0.05)
    assert row["score"] == "below"  # peak > 0.1 kWh so not ideal; avg under the day-band midpoint
    assert row["net_cost_gbp"] == 1.9

    by_key = {}
    for b in p["bands"]:
        by_key.setdefault(b["key"], []).append(b)
    peak = by_key["band_peak"][0]
    assert peak["is_peak"] and peak["import_kwh"] == pytest.approx(1.8, abs=0.1)
    assert peak["import_cost_gbp"] == pytest.approx(1.8 * 0.38174, abs=0.05)
    assert peak["load_error_kwh"] == pytest.approx(3.0, abs=0.01)  # 6 slots x +0.5
    assert peak["under_forecast"] is True
    cheap_am = next(b for b in by_key["band_cheap"] if b["start_local"] == "04:00")
    assert cheap_am["import_kwh"] == pytest.approx(6.0, abs=0.1)
    assert cheap_am["under_forecast"] is False
    assert p["peak_under_forecast"] is True

    assert p["battery"]["charge_kwh"] == pytest.approx(1.5, abs=0.1)
    assert p["battery"]["edges"][0]["realised_soc_pct"] == 50.0
    c = p["comfort"]
    assert c["hours_below_night_floor"] == pytest.approx(2.0, abs=0.01)
    assert c["hours_below_peak_floor"] == 0.0
    assert c["rooms"]["corredor"]["min_c"] == 16.0
    assert p["money"]["rolling7_mean_net_cost_gbp"] == 1.9
    assert p["ops"]["fox_failures"] == 0


def test_persist_roundtrip_and_rolling_mean():
    _seed_day()
    row = cs.build_scorecard(DAY)
    cs.persist_scorecard(row)
    cs.persist_scorecard(row)  # idempotent
    got = cs.get_scorecards(14)
    assert len(got) == 1 and got[0]["date"] == "2026-10-05"
    assert got[0]["payload"]["bands"]
    nxt = cs.build_scorecard(DAY + timedelta(days=1))
    assert nxt["payload"]["money"]["rolling7_mean_net_cost_gbp"] == 1.9  # one prior + today, both 1.9


def _alert_row(day: str, *, mismatch=0, below=False, calls=10, under=False) -> dict:
    return {"date": day, "payload": {
        "peak_under_forecast": under,
        "tank": {"showers": [{"label": "evening_showers", "entry_local": "20:00", "floor_c": 43.0,
                              "tank_c": 38.0 if below else 50.0, "below_floor": below}]},
        "lwt": {"write_verify": {"mismatch": mismatch}},
        "ops": {"daikin_calls": calls, "daikin_budget": 180},
    }, "score": None, "import_kwh": 1.0}


def test_alerts_fire_once_per_date(sent):
    row = _alert_row("2026-10-05", mismatch=2, below=True, calls=160)
    assert cs.fire_alerts(row) == 3
    assert cs.fire_alerts(row) == 0  # deduped by date key
    assert len(sent) == 3
    keys = {e["warning_key"] for _, e in sent}
    assert keys == {"cosy_tank_floor_2026-10-05", "cosy_verify_mismatch_2026-10-05", "cosy_quota_2026-10-05"}
    # a different date alerts again
    assert cs.fire_alerts(_alert_row("2026-10-06", mismatch=1)) == 1


def test_peak_under_forecast_needs_three_consecutive_days(sent):
    for d in ("2026-10-03", "2026-10-04"):
        r = _alert_row(d, under=True)
        r["payload"]["peak_under_forecast"] = True
        db.upsert_cosy_scorecard(r)
    assert cs.fire_alerts(_alert_row("2026-10-05", under=True)) == 1
    assert "cosy_peak_underforecast_2026-10-05" in {e["warning_key"] for _, e in sent}
    # gap day breaks the streak
    assert cs.evaluate_alerts(_alert_row("2026-10-08", under=True)) == []


def test_no_alert_on_quiet_day(sent):
    assert cs.fire_alerts(_alert_row("2026-10-05")) == 0 and not sent


def test_job_registration():
    from src.scheduler import runner

    class Fake:
        def __init__(self):
            self.jobs = []

        def add_job(self, fn, trigger=None, **kw):
            self.jobs.append((fn, trigger, kw))

    f = Fake()
    assert runner.register_cosy_scorecard_jobs(f, ZoneInfo("Europe/London")) is True
    ids = {kw["id"]: (fn, trig) for fn, trig, kw in f.jobs}
    assert ids["cosy_scorecard"][0] is runner.cosy_scorecard_job
    assert "hour='7'" in str(ids["cosy_scorecard"][1]) and "minute='30'" in str(ids["cosy_scorecard"][1])
    assert ids["cosy_scorecard_boot"][0] is runner.cosy_scorecard_boot_backfill_job


def test_job_registration_disabled(monkeypatch):
    from src.scheduler import runner

    monkeypatch.setattr(config, "COSY_SCORECARD_ENABLED", False)
    f = type("F", (), {"add_job": lambda *a, **k: pytest.fail("must not register")})()
    assert runner.register_cosy_scorecard_jobs(f, ZoneInfo("Europe/London")) is False


def test_job_scores_yesterday_and_never_raises(monkeypatch):
    from src.scheduler import runner

    seen = []
    monkeypatch.setattr(cs, "run_for_day", lambda d: seen.append(d) or {"date": d.isoformat()})
    runner.cosy_scorecard_job()
    assert seen == [datetime.now(LON).date() - timedelta(days=1)]
    monkeypatch.setattr(cs, "run_for_day", lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
    runner.cosy_scorecard_job()  # swallowed


def test_backfill_missing_scores_only_gaps(monkeypatch):
    today = DAY + timedelta(days=3)
    _seed_day(DAY)
    done = cs.backfill_missing(days=3, today=today)
    assert "2026-10-05" in done  # today-3
    assert cs.backfill_missing(days=3, today=today) == []  # second pass: nothing missing


def test_api_shape_and_clamp():
    from src.api.main import app

    _seed_day()
    cs.persist_scorecard(cs.build_scorecard(DAY))
    c = TestClient(app)
    r = c.get("/api/v1/scorecard/cosy?days=14")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 14 and len(body["rows"]) == 1
    row = body["rows"][0]
    for k in ("date", "score", "peak_import_kwh", "import_kwh", "avg_import_p", "ideal_avg_import_p",
              "net_cost_gbp", "bands", "comfort", "tank", "lwt", "ops", "money"):
        assert k in row
    assert c.get("/api/v1/scorecard/cosy?days=999").json()["days"] == 90
    assert c.get("/api/v1/scorecard/cosy?days=0").json()["days"] == 1


def test_brief_line_present_and_absent():
    today = DAY + timedelta(days=1)
    assert cs.brief_line(today) is None
    db.upsert_cosy_scorecard({
        "date": "2026-10-05", "score": "below", "peak_import_kwh": 0.0, "import_kwh": 14.0,
        "avg_import_p": 16.3, "ideal_avg_import_p": 12.5, "net_cost_gbp": 3.61, "payload": {},
    })
    line = cs.brief_line(today)
    assert line == "Cosy yesterday: 14.0 kWh import · 0.0 at peak · 16.3p avg (ideal 12.5) · below usual · £3.61"
    assert cs.brief_line(today + timedelta(days=1)) is None  # only yesterday's row counts


def test_morning_brief_includes_line(monkeypatch):
    from src.analytics import daily_brief

    monkeypatch.setattr(daily_brief, "_cosy_scorecard_line", lambda: "Cosy yesterday: X")
    assert "Cosy yesterday: X" in daily_brief.build_morning_payload()
    monkeypatch.setattr(daily_brief, "_cosy_scorecard_line", lambda: None)
    assert "Cosy yesterday" not in daily_brief.build_morning_payload()
