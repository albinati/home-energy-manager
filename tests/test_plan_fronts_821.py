"""#821 — GET /api/v1/plan/fronts: per-front plan composition."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src import db
from src.analytics import plan_fronts as pf
from src.analytics.load_expected import BandWindow
from src.config import config

LON = ZoneInfo("Europe/London")
COSY = "E-1R-COSY-22-12-08-H"
DAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 14, 30, tzinfo=UTC)


def _price(h: int) -> float:
    if h in (4, 5, 6, 13, 14, 15, 22, 23):
        return 12.4868
    if h in (16, 17, 18):
        return 38.174
    return 25.4461


def _z(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _seed_cosy_rates(d: date) -> None:
    rows = []
    for h in range(24):
        for m in (0, 30):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC)
            rows.append({"valid_from": _z(t), "valid_to": _z(t + timedelta(minutes=30)),
                         "value_inc_vat": _price(h)})
    db.save_agile_rates(rows, COSY)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    pf.clear_cache()
    from src.analytics import load_expected

    load_expected.clear_cache()
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", COSY)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_STRUCTURE", "auto", raising=False)
    monkeypatch.setattr(config, "TARIFF_DISPLAY_NAME", "", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    from src.analytics import fair_compare

    monkeypatch.setattr(fair_compare, "compute_fair_comparison",
                        lambda s, e, max_tariffs=14: {"tariffs": []})


def _slot(h: int, m: int = 0, **kw):
    st = datetime(2026, 10, 7, h, m, tzinfo=LON).astimezone(UTC)
    base = {"slot_time_utc": _z(st), "_start": st, "import_kwh": 0.0, "export_kwh": 0.0,
            "charge_kwh": 0.0, "discharge_kwh": 0.0, "pv_use_kwh": 0.0, "soc_kwh": 5.0, "slot_index": 0}
    base.update(kw)
    return base


def _win(key, label, h0, h1, price):
    s = datetime(2026, 10, 7, h0, tzinfo=LON).astimezone(UTC)
    e = datetime(2026, 10, 7, h1, tzinfo=LON).astimezone(UTC) if h1 < 24 else s + timedelta(hours=h1 - h0)
    return BandWindow(key=key, label=label, start_utc=s, end_utc=e, price_p=price)


# ------------------------------------------------------------------ endpoint
def test_endpoint_shape_and_bad_date():
    from src.api.main import app

    _seed_cosy_rates(DAY)
    c = TestClient(app)
    r = c.get("/api/v1/plan/fronts?date=2026-10-07")
    assert r.status_code == 200
    body = r.json()
    for k in ("date", "now_utc", "tariff", "battery", "tank", "heating", "consumption", "spend", "compare"):
        assert k in body
    assert body["date"] == "2026-10-07"
    assert body["tariff"]["structure"] == "banded"
    assert c.get("/api/v1/plan/fronts?date=nope").status_code == 400


# ------------------------------------------------------------------- battery
def test_battery_kind_rule_and_windows():
    groups = pf.normalise_fox_groups([
        {"startHour": 22, "startMinute": 0, "endHour": 23, "endMinute": 59, "workMode": "Backup",
         "extraParam": {"minSocOnGrid": 10, "maxSoc": 10}},
    ])
    assert groups[0]["start_local"] == "22:00" and groups[0]["max_soc"] == 10
    slots = [
        _slot(14, 0, import_kwh=1.0, charge_kwh=0.9, soc_kwh=6.0),
        _slot(14, 30, import_kwh=1.0, charge_kwh=0.9, soc_kwh=7.0),
        _slot(16, 0, discharge_kwh=0.5, soc_kwh=6.5),
        _slot(20, 0),
        _slot(22, 0, import_kwh=0.3),
        _slot(22, 30, import_kwh=0.3),
    ]
    wins = pf.battery_windows(slots, groups, 10.0, LON)
    assert [w["kind"] for w in wins] == ["grid_charge", "self_use", "idle", "hold"]
    gc = wins[0]
    assert gc["start_local"] == "14:00" and gc["end_local"] == "15:00"
    assert gc["grid_kwh"] == 2.0 and gc["soc_start_pct"] == 60.0 and gc["soc_end_pct"] == 70.0
    assert wins[3]["fox_mode"] == "Backup" and wins[3]["grid_kwh"] == 0.6
    assert pf.battery_slot_kind(_slot(10, charge_kwh=0.5), None) == "pv_charge"
    assert pf.battery_slot_kind(_slot(10, export_kwh=0.5, pv_use_kwh=0.1), None) == "export"


def test_battery_by_band_planned_realised_and_floored():
    peak = _win("band_peak", "peak", 16, 19, 38.17)
    cheap = _win("band_cheap", "cheap", 13, 16, 12.49)
    planned = {cheap.start_utc + timedelta(minutes=30 * i): 0.5 for i in range(6)}
    realised = {_z(cheap.start_utc + timedelta(minutes=30 * i)): 0.4 for i in range(6)}
    lp = [_slot(16, 0, soc_kwh=9.0, slot_index=7)]
    now = datetime(2026, 10, 7, 14, 30, tzinfo=UTC)  # 15:30 local: cheap ongoing, peak upcoming
    rows = pf.battery_by_band([cheap, peak], planned, realised, lp, {7}, 10.0, now, LON)
    c, p = rows
    assert c["planned_import_kwh"] == 3.0 and c["realised_import_kwh"] == pytest.approx(0.4 * 5)
    assert p["realised_import_kwh"] is None and p["floored"] is True and p["soc_entry_pct"] == 90.0
    assert c["floored"] is False


# --------------------------------------------------------------------- spend
def test_spend_score_thresholds():
    s = pf.spend_score
    assert s(14.0, 0.0, 12.49, 18.97, 1.15) == "ideal"
    assert s(14.0, 0.5, 12.49, 18.97, 1.15) == "below"      # peak import spoils "ideal"
    assert s(18.97, 0.0, 12.49, 18.97, 1.15) == "below"
    assert s(18.98, 0.0, 12.49, 18.97, 1.15) == "above"
    assert s(None, 0.0, 12.49, 18.97, 1.15) is None
    assert s(14.0, 9.0, 12.49, 18.97, 1.15, banded=False) == "ideal"  # dynamic: no peak clause


def test_spend_section_basis_forecast_then_realised(monkeypatch):
    from src.analytics import pnl

    _seed_cosy_rates(DAY)
    cheap_start = datetime(2026, 10, 7, 13, 0, tzinfo=LON).astimezone(UTC)
    committed = {_z(cheap_start + timedelta(minutes=30 * i)): 1.0 for i in range(6)}
    monkeypatch.setattr(db, "committed_lp_field_by_slot", lambda d, f: committed if d == DAY else {})
    monkeypatch.setattr(pnl, "compute_period_pnl", lambda a, b, **k: {"n_days": 1, "realised_net_cost_gbp": 1.0,
                                                                     "import_kwh": 10.0, "import_cost_gbp": 2.0})
    monkeypatch.setattr(pnl, "compute_daily_pnl", lambda d: {"import_kwh": 0.4, "import_cost_gbp": 0.2})
    from src.analytics.load_expected import band_windows_for_day
    windows = band_windows_for_day(DAY, LON)[0]
    out = pf.spend_section(DAY, windows, NOW, LON)
    assert out["score_basis"] == "forecast" and out["score"] == "ideal"
    assert out["forecast_avg_import_p"] == pytest.approx(12.49, abs=0.01)
    assert out["ideal_avg_import_p"] == pytest.approx(12.49, abs=0.01)
    assert out["score_thresholds"]["above_min_p"] == pytest.approx(18.97, abs=0.05)
    assert out["period"]["week"]["avg_import_p"] == 20.0

    monkeypatch.setattr(pnl, "compute_daily_pnl", lambda d: {"import_kwh": 10.0, "import_cost_gbp": 2.5})
    out = pf.spend_section(DAY, windows, NOW, LON)
    assert out["score_basis"] == "realised" and out["realised_avg_import_p"] == 25.0
    assert out["score"] == "above"


# ------------------------------------------------------------------- heating
def test_heating_windows_and_by_band():
    rows = [
        {"action_type": "lwt_preheat", "start_time": "2026-10-07T03:00:00Z", "end_time": "2026-10-07T06:00:00Z",
         "params": {"lwt_offset": 3, "lp_optimizer": True}},
        {"action_type": "lwt_preheat", "start_time": "2026-10-07T15:00:00Z", "end_time": "2026-10-07T18:00:00Z",
         "params": {"lwt_offset": -2}},
        {"action_type": "restore", "start_time": "2026-10-07T06:00:00Z", "end_time": "2026-10-07T06:30:00Z",
         "params": {}},
        {"action_type": "tank_warmup", "start_time": "2026-10-07T12:00:00Z", "end_time": "2026-10-07T13:00:00Z",
         "params": {}},
    ]
    w = pf.heating_windows(rows, LON)
    assert [(x["kind"], x["source"], x["offset_c"]) for x in w] == [
        ("boost", "lp", 3.0), ("restore", None, 0.0), ("setback", "tier", -2.0)]
    assert w[0]["start_local"] == "04:00"
    cheap = _win("band_cheap", "cheap", 4, 7, 12.49)
    peak = _win("band_peak", "peak", 16, 19, 38.17)
    day = _win("band_day", "day", 19, 22, 25.4)
    lp = [_slot(4, 0, indoor_temp_c=20.0), _slot(5, 0, indoor_temp_c=21.0), _slot(16, 0, indoor_temp_c=22.0)]
    bb = pf.heating_by_band([cheap, peak, day], lp, w, LON)
    assert [b["offset_mode"] for b in bb] == ["boost", "setback", "neutral"]
    assert bb[0]["indoor_min_c"] == 20.0 and bb[0]["indoor_max_c"] == 21.0
    assert pf.heating_by_band([cheap], lp, [], LON)[0]["offset_mode"] is None
    st = pf._indoor_stats(lp, LON)
    assert st["min_c"] == 20.0 and st["at_16_c"] == 22.0 and st["at_07_c"] is None


# ---------------------------------------------------------------------- tank
def test_tank_windows_next_action_and_predicted(monkeypatch):
    from src import dhw_policy

    def rows(day, tz=None):
        if day != DAY:
            return []
        return [
            {"action_type": "tank_warmup", "start_utc": "2026-10-07T12:00:00Z",
             "end_utc": "2026-10-07T14:00:00Z", "tank_temp_c": 47},
            {"action_type": "tank_setback", "start_utc": "2026-10-07T14:00:00Z",
             "end_utc": "2026-10-08T12:00:00Z", "tank_temp_c": 37},
            {"action_type": "legionella_cycle", "start_utc": "2026-10-11T11:00:00Z",
             "end_utc": "2026-10-11T13:00:00Z", "tank_temp_c": 60},
        ]

    monkeypatch.setattr(dhw_policy, "dhw_schedule_rows_for_day", rows)
    w = pf.tank_windows(DAY, LON)
    assert [x["kind"] for x in w] == ["warmup", "setback"]  # legionella is another day
    assert w[0]["start_local"] == "13:00" and w[1]["tank_target_c"] == 37.0
    na = pf.next_tank_action(w, datetime(2026, 10, 7, 12, 30, tzinfo=UTC))
    assert na == {"kind": "setback", "start_local": "15:00", "tank_target_c": 37.0}
    assert pf.next_tank_action(w, datetime(2026, 10, 7, 20, 0, tzinfo=UTC)) is None
    lp = [_slot(20, 0, tank_temp_c=41.5)]
    assert pf.predicted_tank_at(lp, 20.0, LON) == 41.5
    assert pf.predicted_tank_at(lp, 7.0, LON) is None
    assert pf._fmt_hour(7.5) == "07:30"


def test_tank_section_reads_decision_and_model(monkeypatch):
    from src import dhw_policy

    monkeypatch.setattr(dhw_policy, "dhw_schedule_rows_for_day", lambda d, tz=None: [])
    t = pf.tank_section(DAY, NOW, LON, [])
    assert set(t) >= {"tank_now_c", "model", "decision", "windows", "showers", "next_action"}
    assert t["decision"]["arm"] in ("hold", "boost", "static")
    assert t["model"]["source"] and t["model"]["tau_hours"] > 0


# ----------------------------------------------------- consumption / compare
def test_consumption_is_load_expected_payload(monkeypatch):
    from src.analytics import load_expected

    sentinel = {"date": "x", "bands": [], "marker": 821}
    monkeypatch.setattr(load_expected, "expected_load_by_band", lambda d, **k: sentinel)
    _seed_cosy_rates(DAY)
    r = pf.plan_fronts(DAY, now_utc=NOW, use_cache=False)
    assert r["consumption"] is sentinel


def test_compare_rows_shape(monkeypatch):
    from src.analytics import fair_compare

    monkeypatch.setattr(fair_compare, "compute_fair_comparison", lambda s, e, max_tariffs=14: {
        "tariffs": [
            {"product_code": "COSY", "display_name": "Cosy (your tariff)", "net_pence": 2830.0,
             "is_current": True, "approximate": False},
            {"product_code": "AGILE", "display_name": "Agile", "net_pence": 2928.0,
             "is_current": False, "approximate": True},
        ]})
    c = pf.compare_section(DAY)
    assert c["period"] == "month" and c["period_start"] == "2026-10-01" and c["n_days"] == 7
    assert c["current"] == {"product_code": "COSY", "display_name": "Cosy (your tariff)", "net_gbp": 28.3}
    assert c["rows"][1]["delta_vs_current_gbp"] == 0.98 and c["rows"][1]["approximate"] is True
    assert "Cosy" in c["framing"]


# ---------------------------------------------------------- graceful errors
def test_section_error_isolated(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("fox down")

    monkeypatch.setattr(pf, "battery_section", boom)
    _seed_cosy_rates(DAY)
    r = pf.plan_fronts(DAY, now_utc=NOW, use_cache=False)
    assert "RuntimeError" in r["battery"]["error"]
    assert "windows" in r["tariff"] and "error" not in r["tariff"]
    assert "bands" in r["consumption"]


def test_cache_bypassed_with_explicit_now(monkeypatch):
    calls = []
    monkeypatch.setattr(pf, "tariff_section", lambda *a: calls.append(1) or {"windows": []})
    pf.plan_fronts(DAY, now_utc=NOW)
    pf.plan_fronts(DAY, now_utc=NOW)
    assert len(calls) == 2
