"""#831 — daily Cosy scorecard: build, persist, alerts, job, API, brief line."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src import db
from src.analytics import cosy_scorecard as cs
from src.analytics import plan_fronts as pf
from src.analytics import pnl as _pnl_mod
from src.config import config

_REAL_PNL = _pnl_mod.compute_daily_pnl  # captured before the autouse stub

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
    monkeypatch.setattr(pnl, "compute_daily_pnl", lambda d, **kw: {
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
    assert row["import_kwh"] == pytest.approx(7.8, abs=0.15)  # realised from the grid roll-up
    assert row["peak_import_kwh"] == pytest.approx(1.8, abs=0.1)
    assert row["ideal_avg_import_p"] == pytest.approx(12.49, abs=0.01)
    assert row["avg_import_p"] == pytest.approx(18.41, abs=0.4)
    assert row["score"] == "below"  # (realised) peak > 0.1 kWh so not ideal; avg under the day-band midpoint
    assert row["net_cost_gbp"] == 1.9
    assert p["has_telemetry"] is True

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
    assert cs.fire_alerts(row) == 2  # a verify mismatch is NOT re-alerted (verifier notifies)
    assert cs.fire_alerts(row) == 0  # deduped by date key
    assert len(sent) == 2
    keys = {e["warning_key"] for _, e in sent}
    assert keys == {"cosy_tank_floor_2026-10-05", "cosy_quota_2026-10-05"}
    # a different date alerts again
    assert cs.fire_alerts(_alert_row("2026-10-06", calls=151)) == 1
    # quota unknown (day older than api_call_log retention) -> no alert
    assert cs.evaluate_alerts(_alert_row("2026-10-07", calls=None)) == []


def test_peak_under_forecast_needs_three_consecutive_days(sent):
    for d in ("2026-10-03", "2026-10-04"):
        r = _alert_row(d, under=True)
        r["payload"]["peak_under_forecast"] = True
        db.upsert_cosy_scorecard(r)
    assert cs.fire_alerts(_alert_row("2026-10-05", under=True)) == 1
    assert "cosy_peak_underforecast_2026-10-03" in {e["warning_key"] for _, e in sent}
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


def test_build_is_db_only_no_http(monkeypatch):
    """H1/H2: the build must not touch Octopus (standing/catalogue) nor the PnL period roll-ups."""
    import requests
    from src.analytics import fair_compare, pnl

    def boom(*a, **k):
        raise AssertionError("network/catalogue touched")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(fair_compare, "current_import_standing_pence", boom)
    from src.energy import octopus_products

    monkeypatch.setattr(octopus_products, "_get_json", boom)
    monkeypatch.setattr(pf, "spend_section", boom)
    monkeypatch.setattr(pf, "_period", boom)
    # the REAL pnl must run with standing_source="manual" (no catalogue lookup)
    seen = {}

    def real(d, **kw):
        seen.update(kw)
        return _REAL_PNL(d, **kw)

    monkeypatch.setattr(pnl, "compute_daily_pnl", real)
    _seed_day()
    row = cs.build_scorecard(DAY)
    assert seen == {"standing_source": "manual"}
    assert row["import_kwh"] is not None and row["payload"]["money"].get("error") is None


def test_peak_only_small_import_is_realised_not_ideal():
    """H3: 0.6 kWh imported entirely at peak must not score ideal."""
    _seed_rates(DAY)
    t = datetime(2026, 10, 5, 16, 0, tzinfo=LON).astimezone(UTC)
    for i in range(0, 37):  # 16:00..19:00 at 0.2 kW => 0.6 kWh
        db.save_pv_realtime_sample(_z(t + timedelta(minutes=5 * i)), solar_power_kw=0.0, soc_pct=50.0,
                                   load_power_kw=1.0, grid_import_kw=0.2, grid_export_kw=0.0,
                                   battery_charge_kw=0.0, battery_discharge_kw=0.0)
    row = cs.build_scorecard(DAY)
    assert row["peak_import_kwh"] == pytest.approx(0.6, abs=0.05)
    assert row["score"] != "ideal"
    assert row["score"] == "above"
    assert row["avg_import_p"] == pytest.approx(38.17, abs=0.1)


def test_fox_failures_and_verify_counts():
    """H4 + M1: foxess device rows; only final verify failures count as mismatch."""
    _seed_day()
    now = datetime.now(UTC)
    day = (now.astimezone(LON)).date()
    db.log_action(device="foxess", action="set_work_mode", params={}, result="failure", trigger="t")
    db.log_action(device="foxess", action="set_work_mode", params={}, result="success", trigger="t")
    db.log_action(device="daikin", action="daikin_write_verify", params={"attempt": 1}, result="failure", trigger="p")
    db.log_action(device="daikin", action="daikin_write_verify", params={"attempt": 2}, result="failure", trigger="p")
    db.log_action(device="daikin", action="daikin_write_verify", params={"attempt": 2}, result="success", trigger="p")
    db.log_action(device="daikin", action="daikin_write_verify", params={"attempt": 1}, result="unverified", trigger="p")
    tz = LON
    a, b = pf._local_day_bounds(day, tz)
    assert cs._ops(a, b)["fox_failures"] == 1
    wv = cs._lwt(day, tz, a, b)["write_verify"]
    assert wv == {"success": 1, "unverified": 1, "mismatch": 1, "retried": 1}
    assert cs._ops(a, b)["daikin_calls"] == 0
    old_a = a - timedelta(days=5)
    assert cs._ops(old_a, old_a + timedelta(days=1))["daikin_calls"] is None  # beyond 48 h retention


def test_count_calls_between_and_soc_at():
    from src import api_quota

    api_quota.record_call("daikin", "read") if hasattr(api_quota, "record_call") else None
    t = datetime.now(UTC).timestamp()
    n = api_quota.count_calls_between("daikin", t - 60, t + 60)
    assert n >= 0 and api_quota.count_calls_between("daikin", t + 60, t + 120) == 0
    base = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    db.save_pv_realtime_sample("2026-10-05T12:00:00+00:00", soc_pct=40.0)
    db.save_pv_realtime_sample("2026-10-05T12:10:00Z", soc_pct=44.0)
    assert db.get_soc_pct_at(base) == 40.0
    assert db.get_soc_pct_at(base + timedelta(minutes=8)) == 44.0
    assert db.get_soc_pct_at(base + timedelta(minutes=45)) is None


def test_tank_with_data():
    day = date.today() - timedelta(days=1)
    t = datetime(day.year, day.month, day.day, 20, 0, tzinfo=LON).astimezone(UTC)
    for dt_, v in ((0, 38.0), (300, 38.5)):
        db.insert_daikin_telemetry({"fetched_at": t.timestamp() + dt_, "source": "live", "tank_temp_c": v,
                                    "indoor_temp_c": 21.0, "outdoor_temp_c": 10.0})
    out = cs._tank(day, LON)
    ev = next(s for s in out["showers"] if s["entry_local"] == "20:00")
    assert ev["tank_c"] == 38.0 and ev["below_floor"] is True
    assert out["any_below_floor"] is True


def test_dst_day_50_slots():
    d = date(2026, 10, 25)  # clocks go back: 25 h local day
    _seed_rates(d)
    _seed_telemetry(d)
    row = cs.build_scorecard(d)
    assert row["payload"]["has_telemetry"] is True
    assert row["import_kwh"] == pytest.approx(7.8, abs=0.3)  # 2 kW x 3 h + 0.6 kW x 3 h
    a, b = pf._local_day_bounds(d, LON)
    assert (b - a) == timedelta(hours=25)


def test_backfill_gaps_skip_no_telemetry_and_rescore_recent():
    today = DAY + timedelta(days=3)  # last 3 days: 10-02, 10-03, 10-04... seed telemetry for 10-05 => today-3
    assert cs.backfill_missing(days=3, today=today) == []  # no telemetry at all -> skipped
    _seed_day(DAY)
    assert "2026-10-05" in cs.backfill_missing(days=3, today=today)
    # built just now (< 48 h after the day end in wall-clock terms) -> re-scored
    row = cs.build_scorecard(DAY)
    row["built_at_utc"] = "2026-10-06T07:30:00Z"  # 7.5 h after the day closed
    db.upsert_cosy_scorecard(row)
    assert "2026-10-05" in cs.backfill_missing(days=3, today=today)
    row["built_at_utc"] = "2026-10-09T07:30:00Z"  # > 48 h after: settled
    db.upsert_cosy_scorecard(row)
    assert "2026-10-05" not in cs.backfill_missing(days=3, today=today)
