"""#879 PR 2 — Insights recommendations composer + GET /api/v1/insights/recommendations."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src import db
from src.analytics import recommendations as rec
from src.config import config

LON = ZoneInfo("Europe/London")
COSY = "E-1R-COSY-22-12-08-H"
TODAY = datetime.now(LON).date()


def _price(h: int) -> float:
    if h in (4, 5, 6, 13, 14, 15, 22, 23):
        return 12.4868
    if h in (16, 17, 18):
        return 38.174
    return 25.4461


def _z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_rates(days: int = 12) -> None:
    rows = []
    for k in range(-days, 3):
        d = TODAY + timedelta(days=k)
        for h in range(24):
            for m in (0, 30):
                t = datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC)
                rows.append({"valid_from": _z(t), "valid_to": _z(t + timedelta(minutes=30)),
                             "value_inc_vat": _price(h)})
    db.save_agile_rates(rows, COSY)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    rec.clear_cache()
    from src.analytics import fair_compare, load_expected, plan_fronts

    plan_fronts.clear_cache()
    load_expected.clear_cache()
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", COSY)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_STRUCTURE", "auto", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    monkeypatch.setattr(fair_compare, "cached_fair_comparison", lambda s, e, n=4: {"tariffs": []})
    _seed_rates()


def _profile(monkeypatch, per_slot: dict[tuple[int, int, int], float], total_days: int = 40):
    prof = {"day_counts": {"total": total_days}}
    monkeypatch.setattr(db, "residual_load_profile_v2", lambda **kw: prof)
    monkeypatch.setattr(db, "lookup_residual_kwh", lambda p, dow, h, m: per_slot.get((dow, h, m), 0.0))


# --------------------------------------------------------------- arithmetic
def test_pence_label_and_pt():
    assert rec.pence_label(-12.4) == "−12p"
    assert rec.pence_label(-35, "/sem") == "−35p/sem"
    assert rec.pence_label(4.6) == "+5p"
    assert rec.pt(0.3333, 2) == "0,33"


def test_band_prices_and_price_at():
    from src.analytics.load_expected import band_windows_for_day

    windows, structure = band_windows_for_day(TODAY, LON)
    assert structure == "banded"
    p = rec.band_prices(windows)
    assert round(p["cheap"], 2) == 12.49 and round(p["day"], 2) == 25.45 and round(p["peak"], 2) == 38.17
    assert round(rec.price_at_local(windows, 6, 30), 2) == 12.49
    assert round(rec.price_at_local(windows, 7, 0), 2) == 25.45


# ------------------------------------------------------------------ washer
def test_washer_omitted_without_appliance():
    assert rec.washer_item(TODAY, LON) is None


def test_washer_status_with_runs():
    aid = db.add_appliance(vendor="st", vendor_device_id="w1", name="Máquina de lavar",
                           device_type="washer", typical_kw=0.5)
    for k in (1, 2, 3):
        d = TODAY - timedelta(days=k)
        armed = datetime(d.year, d.month, d.day, 11, 0, tzinfo=LON)       # day band at request time
        start = datetime(d.year, d.month, d.day, 13, 0, tzinfo=LON)        # cheap band
        jid = db.create_appliance_job(
            appliance_id=aid, armed_at_utc=_z(armed), deadline_utc=_z(armed + timedelta(hours=10)),
            duration_minutes=60, planned_start_utc=_z(start), planned_end_utc=_z(start + timedelta(hours=1)))
        db.update_appliance_job(jid, status="completed", actual_start_utc=_z(start), actual_kwh=0.4)
    it = rec.washer_item(TODAY, LON)
    assert it is not None and it["kind"] == "status" and it["money_label"] == "ok"
    assert it["evidence"]["runs"] == 3 and it["evidence"]["cheap_runs"] == 3
    assert "3 lavagens" in it["detail"] and "todas na faixa barata" in it["detail"]
    assert it["evidence"]["cost_pence"] == pytest.approx(3 * 0.4 * 12.4868, abs=0.2)
    assert it["evidence"]["counterfactual_pence"] == pytest.approx(3 * 0.4 * 25.4461, abs=0.2)
    assert "sem o HEM" in it["detail"]


# ----------------------------------------------------------------- cooking
def test_cooking_items_arithmetic(monkeypatch):
    prof = {}
    for dow in range(7):
        for (h, m) in ((7, 0), (7, 30), (8, 0)):
            prof[(dow, h, m)] = 0.1333          # 0.4 kWh over 07:00-08:30 every day
    prof[(5, 12, 0)] = prof[(5, 12, 30)] = 1.4  # Saturday lunch 2.8 kWh
    prof[(6, 12, 0)] = prof[(6, 12, 30)] = 1.5  # Sunday lunch 3.0 kWh
    _profile(monkeypatch, prof)
    items = {i["key"]: i for i in rec._cooking_items(TODAY, LON)}
    diff = 25.4461 - 12.4868
    b = items["breakfast"]
    assert b["evidence"]["kwh_per_day"] == pytest.approx(0.4, abs=0.01)
    assert b["money_pence"] == pytest.approx(-0.4 * diff * 7, abs=0.6)
    assert b["money_label"].endswith("p/sem") and b["money_label"].startswith("−")
    assert b["kind"] == "suggestion" and b["action"] is None
    lu = items["lunch_weekend"]
    assert lu["money_pence"] == pytest.approx(-2.9 * diff, abs=0.6)
    assert "2,8–3,0 kWh" in lu["detail"]


def test_cooking_omitted_when_little_history_or_dynamic(monkeypatch):
    _profile(monkeypatch, {(0, 7, 0): 0.5}, total_days=10)
    assert rec._cooking_items(TODAY, LON) == []
    _profile(monkeypatch, {(0, 7, 0): 0.5}, total_days=40)
    from src.analytics import load_expected

    monkeypatch.setattr(load_expected, "band_windows_for_day", lambda d, tz=None: ([], "dynamic"))
    assert rec._cooking_items(TODAY, LON) == []


# ------------------------------------------------------------------ dinner
def _scorecards(bad_dow: int | None, n_days: int = 28):
    for k in range(1, n_days + 1):
        d = TODAY - timedelta(days=k)
        bad = bad_dow is not None and d.weekday() == bad_dow
        db.upsert_cosy_scorecard({
            "date": d.isoformat(), "score": "ideal", "peak_import_kwh": 0.8 if bad else 0.0,
            "import_kwh": 5.0, "import_cost_gbp": 1.0, "avg_import_p": 12.0, "ideal_avg_import_p": 12.5,
            "net_cost_gbp": 1.0, "payload": {}})


def test_dinner_problem_weekday():
    _scorecards(bad_dow=1)
    it = rec.dinner_item(TODAY, LON)
    assert it is not None and it["kind"] == "suggestion"
    assert "a terça é o problema" in it["headline"]
    assert it["money_pence"] == pytest.approx(-0.8 * (38.174 - 12.4868), abs=0.3)
    assert it["evidence"]["bad_days"] == 4 and it["evidence"]["of_days"] == 4
    assert "nas terças" in it["detail"]


def test_dinner_all_covered_is_status():
    _scorecards(bad_dow=None)
    it = rec.dinner_item(TODAY, LON)
    assert it["kind"] == "status" and it["money_label"] == "ok"
    assert it["evidence"]["covered_days_week"] == it["evidence"]["days_week"] == 6   # today itself is not scored yet


def test_dinner_omitted_with_few_scorecards():
    _scorecards(bad_dow=1, n_days=9)
    assert rec.dinner_item(TODAY, LON) is None


# ------------------------------------------------------------------ drying
def _plan(monkeypatch, export_per_slot: float, soc: float = 9.8):
    tom = TODAY + timedelta(days=1)
    slots = []
    i = 0
    for h in range(24):
        for m in (0, 30):
            st = datetime(tom.year, tom.month, tom.day, h, m, tzinfo=LON).astimezone(UTC)
            in_band = 13 <= h < 16
            slots.append({"slot_time_utc": _z(st), "slot_index": i,
                          "export_kwh": export_per_slot if in_band else 0.0,
                          "pv_forecast_kwh": 0.7 if in_band else 0.0, "soc_kwh": soc})
            i += 1
    monkeypatch.setattr(db, "find_latest_optimizer_run_id", lambda: 7)
    monkeypatch.setattr(db, "get_lp_solution_slots", lambda rid: slots)
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.36, raising=False)
    monkeypatch.setattr(config, "EXPORT_RATE_PENCE", 15.0, raising=False)


def test_drying_surplus(monkeypatch):
    _plan(monkeypatch, 0.7)           # 6 slots x 0.7 = 4.2 kWh
    it = rec.drying_item(TODAY, LON)
    assert it is not None and "com bateria cheia" in it["headline"]
    assert it["evidence"]["surplus_kwh"] == pytest.approx(4.2)
    assert it["money_pence"] == pytest.approx(-2.0 * (25.4461 - 15.0), abs=0.3)  # capped at 2 kWh


def test_drying_omitted_when_small_surplus(monkeypatch):
    _plan(monkeypatch, 0.1)
    assert rec.drying_item(TODAY, LON) is None


# ----------------------------------------------------------------- showers
def test_showers_status(monkeypatch):
    from src import dhw_demand, dhw_policy

    def utc(h):
        return _z(datetime(TODAY.year, TODAY.month, TODAY.day, h, tzinfo=LON))

    rows = [{"action_type": "tank_warmup", "start_utc": utc(13), "end_utc": utc(22), "tank_temp_c": 45},
            {"action_type": "tank_setback", "start_utc": utc(22), "end_utc": utc(23), "tank_temp_c": 37}]
    monkeypatch.setattr(dhw_policy, "dhw_schedule_rows_for_day", lambda d, **kw: rows)
    monkeypatch.setattr(dhw_policy, "_legionella_standoff_window_utc", lambda d: None)
    monkeypatch.setattr(dhw_demand, "required_tank_temp_for_n_showers", lambda n, **kw: 36.0 + 2.0 * n)
    it = rec.showers_item(TODAY, LON)
    assert it["headline"] == "Água a 45 °C das 13:00 às 22:00; depois cai para 37"
    assert it["evidence"]["showers_fit"] == 4 and "Cabem 4 banhos a 45 °C" in it["detail"]


def test_showers_omitted_without_schedule(monkeypatch):
    from src import dhw_policy

    monkeypatch.setattr(dhw_policy, "dhw_schedule_rows_for_day", lambda d, **kw: [])
    assert rec.showers_item(TODAY, LON) is None


# ------------------------------------------------------------------ tuning
def test_tuning_items_with_feedback():
    ws = (TODAY - timedelta(days=3)).isoformat()
    db.save_tuning_suggestions([
        {"week_start": ws, "key": "DHW_TEMP_NORMAL_C", "current_value": 45, "suggested_value": 44,
         "delta_pence_per_week": -9, "delta_comfort_hours": 0.0, "verdict": "recommended",
         "payload": {"body": {"value": 44}}},
        {"week_start": ws, "key": "LP_W3_NIGHT_FLOOR_C", "current_value": 20, "suggested_value": 19.5,
         "delta_pence_per_week": -32, "delta_comfort_hours": 0.3, "verdict": "trade-off",
         "payload": {"body": {"value": 19.5}}},
        {"week_start": ws, "key": "INDOOR_SETPOINT_C", "current_value": 21, "suggested_value": 20.5,
         "delta_pence_per_week": -50, "delta_comfort_hours": 2.0, "verdict": "comfort-first", "payload": {}},
    ])
    now = datetime.now(UTC)
    for _ in range(5):
        db.insert_comfort_feedback(verdict="ok", source="api", now=now - timedelta(hours=1))
    db.insert_comfort_feedback(verdict="cold", source="api", room="cozinha", now=now - timedelta(hours=2))
    items = {i["key"]: i for i in rec.tuning_items(TODAY, LON)}
    assert set(items) == {"tune_DHW_TEMP_NORMAL_C", "tune_LP_W3_NIGHT_FLOOR_C"}   # comfort-first dropped
    w = items["tune_DHW_TEMP_NORMAL_C"]
    assert w["kind"] == "setting" and w["action"] == {"setting_key": "DHW_TEMP_NORMAL_C", "from": 45, "to": 44}
    assert w["headline"] == "Baixar o alvo do tanque 45 → 44 °C" and w["money_pence"] == -9
    n = items["tune_LP_W3_NIGHT_FLOOR_C"]
    assert n["action"]["to"] == 19.5 and "−32p/semana" in n["headline"]
    assert "0,3 h a mais abaixo do piso" in n["headline"]
    assert "você disse ok 5× e frio 1×" in n["detail"] and "cozinha" in n["detail"]


def test_tuning_empty():
    assert rec.tuning_items(TODAY, LON) == []


# ------------------------------------------------------------ week summary
def test_energy_battery_solar_formula():
    # Yesterday: cheap 04-07 import & charge 2 kW; 10-12 discharge 1 kW (day band);
    # 17-18 discharge 2 kW (peak band); 13-14 PV 3 kW with 1 kW exported.
    d = TODAY - timedelta(days=1)
    t = datetime(d.year, d.month, d.day, tzinfo=LON).astimezone(UTC)
    end = t + timedelta(days=1)
    while t < end:
        h = t.astimezone(LON).hour
        db.save_pv_realtime_sample(
            _z(t), solar_power_kw=3.0 if h == 13 else 0.0, soc_pct=50.0, load_power_kw=1.0,
            grid_import_kw=2.0 if 4 <= h < 7 else 0.0, grid_export_kw=1.0 if h == 13 else 0.0,
            battery_charge_kw=2.0 if 4 <= h < 7 else 0.0,
            battery_discharge_kw=1.0 if 10 <= h < 12 else (2.0 if h == 17 else 0.0))
        t += timedelta(minutes=5)
    out = rec._energy_battery_solar(TODAY, LON)
    arb = 2.0 * (25.4461 - 12.4868) + 2.0 * (38.174 - 12.4868)     # 10-12: 2 kWh; 17-18: 2 kWh
    assert out["battery"]["arbitrage_gbp"] == pytest.approx(arb / 100.0, abs=0.03)
    assert out["battery"]["cheap_bought_kwh"] == pytest.approx(6.0, abs=0.3)
    assert out["energy"]["by_band"]["cheap"] == pytest.approx(6.0, abs=0.3)
    assert out["energy"]["by_band"]["solar"] == pytest.approx(2.0, abs=0.2)   # 3 kWh PV - 1 kWh exported
    assert sum(out["energy"]["shares"].values()) == pytest.approx(1.0, abs=0.01)
    assert out["solar"]["self_use_share"] == pytest.approx(2 / 3, abs=0.03)
    assert out["solar"]["export_kwh"] == pytest.approx(1.0, abs=0.1)
    assert "formula" in out["battery"] and "formula" in out["energy"]


def test_week_summary_net_usual_compare(monkeypatch):
    from src.analytics import fair_compare, pnl

    def fake_period(s, e, **kw):
        return {"n_days": (e - s).days + 1, "realised_net_cost_gbp": 8.1 if e == TODAY else 9.0}

    base = {"approximate": False, "import_cost_pence": 0, "standing_pence": 0, "export_credit_pence": 0}
    monkeypatch.setattr(pnl, "compute_period_pnl", fake_period)
    monkeypatch.setattr(fair_compare, "cached_fair_comparison", lambda s, e, n=4: {"tariffs": [
        {**base, "product_code": "COSY", "display_name": "Cosy (your tariff)", "net_pence": 810, "is_current": True},
        {**base, "product_code": "AGILE-24", "display_name": "Agile", "net_pence": 1010, "is_current": False}]})
    ws = rec.week_summary(TODAY, LON)
    assert ws["net_gbp"] == 8.1 and ws["usual_gbp"] == 9.0
    assert ws["compare"] and ws["compare"][0]["short_label"] == "Agile" and ws["compare"][0]["delta_gbp"] == 2.0


# ------------------------------------------------------------------- house
def test_house_omits_unidentifiable():
    assert rec.house_section(LON) == {}


def test_house_tank():
    db.upsert_dhw_calibration("coast_check", status="ok",
                              payload={"measured_c_per_h": 0.35, "ratio_median_recent": 1.02},
                              n_samples=7, window_days=14)
    h = rec.house_section(LON)
    assert h["tank"] == {"c_per_h_night": 0.35, "ratio_median_recent": 1.02}
    assert "cooling_c_per_h" not in h and "solar_gain" not in h


# ---------------------------------------------------- guard, cache, endpoint
def test_error_guard_drops_only_the_failing_item(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(rec, "washer_item", boom)
    monkeypatch.setattr(rec, "showers_item",
                        lambda e, tz: rec._item("showers", "Banhos", "drop", "h", "d", kind="status"))
    res = rec._build(TODAY)
    keys = [i["key"] for i in res["items"]]
    assert "washer" not in keys and "showers" in keys
    assert set(res) == {"generated_at", "week", "items", "week_summary", "house"}
    assert (date.fromisoformat(res["week"]["end"]) - date.fromisoformat(res["week"]["start"])).days == 6


def test_cache_ten_minutes(monkeypatch):
    calls = {"n": 0}

    def fake(end):
        calls["n"] += 1
        return {"items": [], "n": calls["n"]}

    monkeypatch.setattr(rec, "_build", fake)
    a = rec.recommendations(TODAY)
    b = rec.recommendations(TODAY)
    assert a is b and calls["n"] == 1
    t0 = rec.time.monotonic()
    monkeypatch.setattr(rec.time, "monotonic", lambda: t0 + 601)
    rec.recommendations(TODAY)
    assert calls["n"] == 2
    rec.recommendations(TODAY, use_cache=False)
    assert calls["n"] == 3


def test_endpoint(monkeypatch):
    from src.api.main import app

    monkeypatch.setattr(rec, "_build", lambda end: {"generated_at": "x", "week": {"end": end.isoformat()},
                                                    "items": [], "week_summary": {}, "house": {}})
    c = TestClient(app)
    r = c.get("/api/v1/insights/recommendations")
    assert r.status_code == 200 and r.json()["items"] == []
    r = c.get("/api/v1/insights/recommendations?end=2026-10-05")
    assert r.json()["week"]["end"] == "2026-10-05"
    assert c.get("/api/v1/insights/recommendations?end=nope").status_code == 400
