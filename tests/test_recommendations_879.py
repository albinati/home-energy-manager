"""#879 PR 2 — Insights recommendations composer + GET /api/v1/insights/recommendations."""
from __future__ import annotations

import threading
import time
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
YDAY = TODAY - timedelta(days=1)
CHEAP, DAYP, PEAKP = 12.4868, 25.4461, 38.174


def _price(h: int) -> float:
    if h in (4, 5, 6, 13, 14, 15, 22, 23):
        return CHEAP
    if h in (16, 17, 18):
        return PEAKP
    return DAYP


def _z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_rates(days: int = 40) -> None:
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
    monkeypatch.setattr(config, "EXPORT_RATE_PENCE", 15.0, raising=False)
    monkeypatch.setattr(fair_compare, "cached_fair_comparison", lambda s, e, n=4: {"tariffs": []})
    _seed_rates()


# ------------------------------------------------------------- telemetry seeding
def _seed(days: list[date], hours: list[int], fn) -> None:
    """5-minute samples over whole local hours; ``fn(day, h, m) -> dict`` of kW columns."""
    for d in days:
        seen = set()
        for h in hours:
            for mm in range(0, 61, 5):
                t = datetime(d.year, d.month, d.day, h, 0, tzinfo=LON) + timedelta(minutes=mm)
                if t in seen:
                    continue
                seen.add(t)
                vals = {"solar_power_kw": 0.0, "load_power_kw": 0.4, "grid_import_kw": 0.0, "grid_export_kw": 0.0,
                        "battery_charge_kw": 0.0, "battery_discharge_kw": 0.0}
                lt = t.astimezone(LON)
                vals.update(fn(d, lt.hour, lt.minute))
                db.save_pv_realtime_sample(_z(t), soc_pct=50.0, **vals)


def _cooking_days() -> list[date]:
    return [YDAY - timedelta(days=k) for k in range(rec.LOOKBACK_DAYS)]


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
    edges = rec._cooking_edges(windows, LON)
    assert edges["breakfast"]["edge"] == (7, 0) and edges["lunch"]["edge"] == (13, 0)


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
    it = rec.washer_item(TODAY, LON, TODAY - timedelta(days=6))
    assert it is not None and it["kind"] == "status" and it["money_label"] == "ok"
    assert it["evidence"]["runs"] == 3 and it["evidence"]["cheap_runs"] == 3
    assert "3 lavagens" in it["detail"] and "todas na faixa barata" in it["detail"]
    assert it["evidence"]["cost_pence"] == pytest.approx(3 * 0.4 * CHEAP, abs=0.2)
    assert it["evidence"]["counterfactual_pence"] == pytest.approx(3 * 0.4 * DAYP, abs=0.2)
    assert "p)" not in it["headline"]                          # no £ in the headline


def test_washer_cost_integrates_over_the_cycle_slots():
    """A cycle that STARTS in the cheap band but runs into the peak costs more than start x kWh."""
    aid = db.add_appliance(vendor="st", vendor_device_id="w2", name="Lavadora", device_type="washer")
    d = YDAY
    start = datetime(d.year, d.month, d.day, 15, 0, tzinfo=LON)       # 15-16 cheap, 16-17 peak
    jid = db.create_appliance_job(
        appliance_id=aid, armed_at_utc=_z(start), deadline_utc=_z(start + timedelta(hours=10)),
        duration_minutes=120, planned_start_utc=_z(start), planned_end_utc=_z(start + timedelta(hours=2)))
    db.update_appliance_job(jid, status="completed", actual_start_utc=_z(start), actual_kwh=1.0)
    it = rec.washer_item(TODAY, LON, TODAY - timedelta(days=6))
    # 4 slots of 0.25 kWh: two cheap (15:00,15:30), two peak (16:00,16:30)
    assert it["evidence"]["cost_pence"] == pytest.approx(0.5 * CHEAP + 0.5 * PEAKP, abs=0.2)


# ----------------------------------------------------------------- cooking
def _cooking_fn(breakfast_kw: float, lunch_kw: float):
    def fn(d, h, m):
        if h in (7,) and m < 30:
            return {"grid_import_kw": breakfast_kw}
        if h == 12 and d.weekday() >= 5:
            return {"grid_import_kw": lunch_kw}
        return {"grid_import_kw": 0.4}                    # fridge, router, standby
    return fn


def test_cooking_priced_on_realised_import_above_baseload():
    _seed(_cooking_days(), [2, 3, 7, 8, 12], _cooking_fn(1.4, 3.0))
    items = {i["key"]: i for i in rec._cooking_items(TODAY, LON)}
    diff = DAYP - CHEAP
    b = items["breakfast"]
    # 07:00 slot imports 0.7 kWh vs 0.2 baseload: 0.5 kWh excess per morning
    assert b["evidence"]["excess_kwh_per_day"] == pytest.approx(0.5, abs=0.06)   # trapezoid smears the 07:30 step
    assert b["money_pence"] == pytest.approx(-b["evidence"]["excess_kwh_per_day"] * diff * 7, abs=0.6)
    assert b["kind"] == "suggestion" and b["action"] is None
    assert "07:00" in b["headline"] and "custam metade" not in b["headline"]
    assert "04–07" in b["detail"] and "medido" not in b["detail"]
    lu = items["lunch_weekend"]
    # 12:00-13:00 imports 3.0 kWh vs 0.4 baseload: 2.6 kWh excess per weekend lunch
    assert lu["evidence"]["excess_kwh_per_lunch"] == pytest.approx(2.6, abs=0.15)
    assert lu["money_pence"] == pytest.approx(-lu["evidence"]["excess_kwh_per_lunch"] * diff, abs=0.6)
    assert "13:00" in lu["headline"]


def test_cooking_omitted_for_baseload_only():
    _seed(_cooking_days(), [2, 3, 7, 8, 12], _cooking_fn(0.4, 0.4))
    assert rec._cooking_items(TODAY, LON) == []


def test_cooking_omitted_when_the_battery_covers_the_house():
    # the battery (charged 04-07) carries the morning and the sun carries the weekend lunch: no import at all
    _seed(_cooking_days(), [2, 3, 7, 8, 12], _cooking_fn(0.0, 0.0))
    out = rec._cooking_items(TODAY, LON)
    assert out == [] or all(i["money_pence"] == 0 for i in out)
    assert out == []


def test_cooking_omitted_without_data_or_on_dynamic(monkeypatch):
    assert rec._cooking_items(TODAY, LON) == []
    from src.analytics import load_expected

    monkeypatch.setattr(load_expected, "band_windows_for_day", lambda d, tz=None: ([], "dynamic"))
    assert rec._cooking_items(TODAY, LON) == []


# ------------------------------------------------------------------ dinner
def _scorecards(bad_dow: int | None, n_days: int = 28, under: bool = True):
    for k in range(1, n_days + 1):
        d = TODAY - timedelta(days=k)
        bad = bad_dow is not None and d.weekday() == bad_dow
        db.upsert_cosy_scorecard({
            "date": d.isoformat(), "score": "ideal", "peak_import_kwh": 0.8 if bad else 0.0,
            "import_kwh": 5.0, "import_cost_gbp": 1.0, "avg_import_p": 12.0, "ideal_avg_import_p": 12.5,
            "net_cost_gbp": 1.0,
            "payload": {"bands": [{"is_peak": True, "under_forecast": bool(bad and under)}]}})


def test_dinner_problem_weekday_priced_against_the_day_band():
    _scorecards(bad_dow=1)
    it = rec.dinner_item(TODAY, LON)
    assert it is not None and it["kind"] == "suggestion" and it["who"] == "Jantar"
    assert "a terça é o problema" in it["headline"]
    # dinner after 19:00 lands in the DAY band, not the cheap one
    assert it["money_pence"] == pytest.approx(-0.8 * (PEAKP - DAYP), abs=0.3)
    assert it["evidence"]["bad_days"] == 4 and it["evidence"]["of_days"] == 4
    assert "19:00" in it["detail"] and "16–19" in it["detail"]
    assert "compra" not in it["detail"].lower().replace("comprou", "")   # no "HEM compra +X kWh" option


def test_dinner_not_attributed_without_underforecast_is_generic_and_unpriced():
    _scorecards(bad_dow=1, under=False)
    it = rec.dinner_item(TODAY, LON)
    assert it["kind"] == "status" and it["who"] == "Pico" and it["money_pence"] is None
    assert "jantar" not in it["headline"].lower()


def test_dinner_all_covered_is_status():
    _scorecards(bad_dow=None)
    it = rec.dinner_item(TODAY, LON)
    assert it["kind"] == "status" and it["money_label"] == "ok"
    assert it["evidence"]["covered_days_week"] == it["evidence"]["days_week"] == 6   # today is not scored yet
    assert "não comprou além do normal" not in it["detail"]


def test_dinner_omitted_with_few_scorecards():
    _scorecards(bad_dow=1, n_days=9)
    assert rec.dinner_item(TODAY, LON) is None


# ------------------------------------------------------------------ drying
def _plan(monkeypatch, *, pv=0.7, pv_use=0.0, soc=9.8):
    tom = TODAY + timedelta(days=1)
    slots = []
    i = 0
    for h in range(24):
        for m in (0, 30):
            st = datetime(tom.year, tom.month, tom.day, h, m, tzinfo=LON).astimezone(UTC)
            in_band = 13 <= h < 16
            slots.append({"slot_time_utc": _z(st), "slot_index": i,
                          "export_kwh": 0.9 if in_band else 0.0,          # may include BATTERY export: must be ignored
                          "pv_forecast_kwh": pv if in_band else 0.0, "pv_use_kwh": pv_use if in_band else 0.0,
                          "soc_kwh": soc})
            i += 1
    monkeypatch.setattr(db, "find_latest_optimizer_run_id", lambda: 7)
    monkeypatch.setattr(db, "get_lp_solution_slots", lambda rid: slots)
    monkeypatch.setattr(config, "BATTERY_CAPACITY_KWH", 10.36, raising=False)


def test_drying_omitted_on_cosy_because_export_beats_the_cheap_band(monkeypatch):
    _plan(monkeypatch)                      # 4.2 kWh of unused PV, export 15p >= cheap 12.5p
    assert rec.drying_item(TODAY, LON) is None


def test_drying_priced_against_the_cheapest_band_when_export_pays_less(monkeypatch):
    _plan(monkeypatch)
    monkeypatch.setattr(config, "EXPORT_RATE_PENCE", 8.0, raising=False)
    it = rec.drying_item(TODAY, LON)
    assert it is not None and it["evidence"]["surplus_kwh"] == pytest.approx(4.2)
    assert it["money_pence"] == pytest.approx(-2.0 * (CHEAP - 8.0), abs=0.2)     # capped at 2 kWh
    assert "13–16" in it["headline"]


def test_drying_surplus_is_pv_not_lp_export(monkeypatch):
    _plan(monkeypatch, pv=0.7, pv_use=0.7)                    # all PV used: LP export_kwh (0.9/slot) is the battery's
    monkeypatch.setattr(config, "EXPORT_RATE_PENCE", 8.0, raising=False)
    assert rec.drying_item(TODAY, LON) is None


# ----------------------------------------------------------------- showers
def _rows(*pairs):
    def utc(h):
        return _z(datetime(TODAY.year, TODAY.month, TODAY.day, h, tzinfo=LON))
    return [{"action_type": a, "start_utc": utc(s), "end_utc": utc(e), "tank_temp_c": t} for a, s, e, t in pairs]


def test_showers_status_uses_predicted_tank_not_warmup_target(monkeypatch):
    from src import dhw_demand, dhw_policy

    rows = _rows(("tank_warmup", 13, 22, 47), ("tank_setback", 22, 23, 37))
    monkeypatch.setattr(dhw_policy, "dhw_schedule_rows_for_day", lambda d, **kw: rows)
    monkeypatch.setattr(dhw_policy, "_legionella_standoff_window_utc", lambda d: None)
    monkeypatch.setattr(dhw_demand, "required_tank_temp_for_n_showers", lambda n, **kw: 36.0 + 2.0 * n)
    monkeypatch.setattr(rec, "_predicted_evening_tank", lambda d, tz: 41.0)    # coasted down from 47
    it = rec.showers_item(TODAY, LON)
    assert it["headline"] == "Água a 47 °C das 13:00 às 22:00; depois cai para 37"
    assert it["evidence"]["showers_fit"] == 2 and "cabem 2 banhos" in it["detail"]


def test_showers_kept_on_a_legionella_day(monkeypatch):
    from src import dhw_policy

    rows = _rows(("legionella_cycle", 12, 14, 60))
    monkeypatch.setattr(dhw_policy, "dhw_schedule_rows_for_day", lambda d, **kw: rows)
    monkeypatch.setattr(dhw_policy, "_legionella_standoff_window_utc", lambda d: None)
    it = rec.showers_item(TODAY, LON)
    assert it is not None and "60 °C" in it["headline"] and "12:00" in it["headline"]


def test_showers_omitted_without_schedule(monkeypatch):
    from src import dhw_policy

    monkeypatch.setattr(dhw_policy, "dhw_schedule_rows_for_day", lambda d, **kw: [])
    assert rec.showers_item(TODAY, LON) is None


# ------------------------------------------------------------------ tuning
def _tune(ws, key, cur, new, pence, hrs, verdict, payload=None):
    return {"week_start": ws, "key": key, "current_value": cur, "suggested_value": new,
            "delta_pence_per_week": pence, "delta_comfort_hours": hrs, "verdict": verdict,
            "payload": {"body": {"value": new}, **(payload or {})}}


def test_tuning_items_with_feedback(monkeypatch):
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    monkeypatch.setattr(config, "LP_W3_NIGHT_FLOOR_C", 20.0, raising=False)
    ws = (TODAY - timedelta(days=3)).isoformat()
    db.save_tuning_suggestions([
        _tune(ws, "DHW_TEMP_NORMAL_C", 45, 44, -9, 0.0, "recommended",
              {"shower_days_below_current": 0, "shower_days_below_suggested": 0}),
        _tune(ws, "LP_W3_NIGHT_FLOOR_C", 20, 19.5, -32, 0.3, "trade-off"),
    ])
    now = datetime.now(UTC)
    for _ in range(5):
        db.insert_comfort_feedback(verdict="ok", source="api", now=now - timedelta(hours=1))
    db.insert_comfort_feedback(verdict="cold", source="api", room="cozinha", now=now - timedelta(hours=2))
    items = {i["key"]: i for i in rec.tuning_items(TODAY, LON)}
    w = items["tune_DHW_TEMP_NORMAL_C"]
    assert w["kind"] == "setting" and w["action"] == {"setting_key": "DHW_TEMP_NORMAL_C", "from": 45, "to": 44}
    assert w["headline"] == "Baixar o alvo do tanque 45 → 44 °C" and w["money_pence"] == -9
    assert "mesmo conforto" in w["detail"] and "−9p" not in w["detail"] + w["headline"]
    n = items["tune_LP_W3_NIGHT_FLOOR_C"]
    assert n["action"]["to"] == 19.5 and "−32p" not in n["headline"]
    assert "0,3 h a mais abaixo do piso" in n["detail"] and "mesmo conforto" not in n["detail"]
    assert "Você disse ok 5× e frio 1×" in n["detail"] and "cozinha" in n["detail"]


def test_tuning_tradeoff_with_extra_shower_day_is_not_same_comfort(monkeypatch):
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    ws = (TODAY - timedelta(days=3)).isoformat()
    db.save_tuning_suggestions([
        _tune(ws, "DHW_TEMP_NORMAL_C", 45, 43, -20, 0.0, "trade-off",
              {"shower_days_below_current": 0, "shower_days_below_suggested": 1})])
    w = rec.tuning_items(TODAY, LON)[0]
    assert "mesmo conforto" not in w["detail"] and "+1 dia com banho abaixo do mínimo" in w["detail"]


def test_tuning_comfort_first_rows_are_included_without_a_saving(monkeypatch):
    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", 21.0, raising=False)
    ws = (TODAY - timedelta(days=3)).isoformat()
    db.save_tuning_suggestions([_tune(ws, "INDOOR_SETPOINT_C", 21, 21.5, 15, -1.5, "comfort-first")])
    it = rec.tuning_items(TODAY, LON)[0]
    assert it["kind"] == "setting" and it["money_pence"] is None and it["money_label"] is None


def test_tuning_rows_go_away_after_the_value_is_applied(monkeypatch):
    ws = (TODAY - timedelta(days=3)).isoformat()
    db.save_tuning_suggestions([_tune(ws, "DHW_TEMP_NORMAL_C", 45, 44, -9, 0.0, "recommended")])
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    assert len(rec.tuning_items(TODAY, LON)) == 1
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 44.0, raising=False)       # applied
    assert rec.tuning_items(TODAY, LON) == []
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 46.0, raising=False)       # drifted: `from` is the LIVE value
    assert rec.tuning_items(TODAY, LON)[0]["action"]["from"] == 46


def test_tuning_empty():
    assert rec.tuning_items(TODAY, LON) == []


# ------------------------------------------------------------ week summary
def _day_fn(spec):
    def fn(d, h, m):
        return spec(h)
    return fn


def test_battery_arbitrage_formula_per_slot():
    # 04-07 grid charge 2 kW; day band 10-12 discharge 1 kW; peak 17-18 discharge 2 kW
    def spec(h):
        return {"grid_import_kw": 2.0 if 4 <= h < 7 else 0.0, "battery_charge_kw": 2.0 if 4 <= h < 7 else 0.0,
                "battery_discharge_kw": 1.0 if 10 <= h < 12 else (2.0 if h == 17 else 0.0),
                "solar_power_kw": 3.0 if h == 13 else 0.0, "grid_export_kw": 1.0 if h == 13 else 0.0}
    _seed([YDAY], list(range(0, 24)), _day_fn(spec))
    out = rec._energy_battery_solar(YDAY, YDAY, LON)
    arb = 2.0 * DAYP + 2.0 * PEAKP - 6.0 * CHEAP
    assert out["battery"]["arbitrage_gbp"] == pytest.approx(arb / 100.0, abs=0.03)
    assert out["battery"]["cheap_bought_kwh"] == pytest.approx(6.0, abs=0.3)
    assert out["energy"]["by_band"]["cheap"] == pytest.approx(6.0, abs=0.3)
    assert out["energy"]["by_band"]["solar"] == pytest.approx(2.0, abs=0.2)     # 3 kWh PV - 1 kWh exported
    assert sum(out["energy"]["shares"].values()) == pytest.approx(1.0, abs=0.01)
    assert out["solar"]["self_use_share"] == pytest.approx(2 / 3, abs=0.03)
    assert out["solar"]["export_kwh"] == pytest.approx(1.0, abs=0.1)
    assert "formula" in out["battery"] and "formula" in out["energy"]


def test_battery_window_mixing_grid_import_and_pv_charge_is_split_per_slot():
    """Day band holds a PV-charged stretch (10-11, no import): its charge is NOT grid charge and
    is credited at the export rate; per-window netting would have called it grid charge."""
    def spec(h):
        return {"grid_import_kw": 2.0 if 4 <= h < 7 else 0.0,
                "battery_charge_kw": 2.0 if (4 <= h < 7 or h == 10) else 0.0,
                "solar_power_kw": 3.0 if h == 10 else 0.0,
                "battery_discharge_kw": 2.0 if 17 <= h < 19 else 0.0}
    _seed([YDAY], list(range(0, 24)), _day_fn(spec))
    out = rec._energy_battery_solar(YDAY, YDAY, LON)
    # grid charge 6 kWh @ cheap; PV charge 2 kWh forgoes 15p export; discharge 4 kWh in the peak
    expected = (4.0 * PEAKP - 6.0 * CHEAP - 2.0 * 15.0) / 100.0
    assert out["battery"]["arbitrage_gbp"] == pytest.approx(expected, abs=0.03)
    assert out["battery"]["cheap_bought_kwh"] == pytest.approx(6.0, abs=0.3)


def test_battery_negative_arbitrage_week_is_negative():
    def spec(h):          # buys at the peak (grid charge), discharges in the cheap band
        return {"grid_import_kw": 2.0 if h == 17 else 0.0, "battery_charge_kw": 2.0 if h == 17 else 0.0,
                "battery_discharge_kw": 2.0 if h == 5 else 0.0}
    _seed([YDAY], list(range(0, 24)), _day_fn(spec))
    out = rec._energy_battery_solar(YDAY, YDAY, LON)
    assert out["battery"]["arbitrage_gbp"] == pytest.approx((2.0 * CHEAP - 2.0 * PEAKP) / 100.0, abs=0.03)
    assert out["battery"]["arbitrage_gbp"] < 0


def test_week_summary_complete_days_usual_and_compare(monkeypatch):
    from src.analytics import fair_compare
    from src.analytics import plan_fronts as pf

    seen: list[tuple[date, date]] = []

    def fake_period(s, e):
        seen.append((s, e))
        if e == YDAY:
            return {"n_days": (e - s).days + 1, "net_cost_gbp": 8.1, "per_day_gbp": 1.35}
        return {"n_days": 28, "net_cost_gbp": 42.0, "per_day_gbp": 1.5}

    base = {"approximate": False, "import_cost_pence": 0, "standing_pence": 0, "export_credit_pence": 0}
    monkeypatch.setattr(pf, "_period", fake_period)
    monkeypatch.setattr(fair_compare, "cached_fair_comparison", lambda s, e, n=4: {"tariffs": [
        {**base, "product_code": "COSY", "display_name": "Cosy (your tariff)", "net_pence": 810, "is_current": True},
        {**base, "product_code": "AGILE-24", "display_name": "Agile", "net_pence": 1010, "is_current": False}]})
    start = TODAY - timedelta(days=6)
    ws = rec.week_summary(start, TODAY, LON)
    # today's partial day is left out; usual = per-day mean of the previous 4 weeks x the same 6 days
    assert (start, YDAY) in seen and ws["complete_days"] == 6
    assert ws["net_gbp"] == 8.1 and ws["usual_gbp"] == 9.0
    assert ws["compare"][0]["short_label"] == "Agile" and ws["compare"][0]["delta_gbp"] == 2.0
    assert ws["current_label"] == "Cosy"


def test_week_summary_empty_when_no_complete_day():
    ws = rec.week_summary(TODAY, TODAY, LON)
    assert ws["complete_days"] == 0 and ws["net_gbp"] is None and ws["energy"] is None


def test_energy_and_battery_gated_on_banded_tariff():
    out = rec._energy_battery_solar(YDAY, YDAY, LON, banded=False)
    assert out["energy"] is None and out["battery"] is None


# ------------------------------------------------------------------- house
def test_house_omits_unidentifiable():
    assert rec.house_section(LON) == {}


def test_house_tank():
    db.upsert_dhw_calibration("coast_check", status="ok",
                              payload={"measured_c_per_h": 0.35, "ratio_median_recent": 1.02, "n_recent": 5},
                              n_samples=7, window_days=14)
    h = rec.house_section(LON)
    assert h["tank"] == {"c_per_h_night": 0.35, "ratio_median_recent": 1.02, "n_recent": 5}
    assert "cooling_c_per_h" not in h and "solar_gain" not in h


def test_house_cooling_heating_solar_arithmetic(monkeypatch):
    daily = [{"payload": {
        "joint_fit": {"identifiable": True, "tau_h": 70.0, "c_kwh_per_k": 99.0, "solar_identifiable": True,
                      "solar_gain_kw_per_pv_kw": 0.2, "solar_fit": {"c_kwh_per_k": 16.5}},
        "night_rise_per_band": [{"n_slots": 5, "measured_rise_c": 0.64, "mean_offset_c": 3.0}],
        "emitter_tau_fit": {"best_tau_min": 35, "confidence": "ok"}}}]
    monkeypatch.setattr(db, "get_lwt_learning_daily", lambda n=14: daily)
    monkeypatch.setattr(db, "get_lwt_learning_rows", lambda a, b: [{"indoor_real_c": 21.0, "outdoor_real_c": 9.0}])
    monkeypatch.setattr(config, "LP_W3_SOLAR_GAIN_KW_PER_PV_KW", 0.0, raising=False)
    h = rec.house_section(LON)
    assert h["cooling_c_per_h"] == {"value": round(12.0 / 70.0, 3), "outdoor_c": 9.0, "tau_h": 70.0}
    # 5 slots span 4 intervals = 2 h: 0.64 / 2
    assert h["heating_c_per_h"] == {"value": 0.32, "offset_c": 3.0, "lag_min": 35}
    # solar fit's own C (16.5), not the headline 99: 0.2 x 3 kW x 3 h / 16.5
    assert h["solar_gain"]["value_c_per_sunny_afternoon"] == round(0.2 * 3 * 3 / 16.5, 2)
    assert h["solar_gain"]["applied"] is False and h["solar_gain"]["assumed_hours"] == 3.0


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
    assert res["week"]["start"] == (TODAY - timedelta(days=TODAY.weekday())).isoformat()


def test_cache_ten_minutes(monkeypatch):
    calls = {"n": 0}

    def fake(end, start=None):
        calls["n"] += 1
        return {"items": [], "n": calls["n"]}

    monkeypatch.setattr(rec, "_build", fake)
    a = rec.recommendations(TODAY)
    b = rec.recommendations(TODAY)
    assert a is b and calls["n"] == 1
    t0 = time.monotonic()
    monkeypatch.setattr(rec.time, "monotonic", lambda: t0 + 601)
    rec.recommendations(TODAY)
    assert calls["n"] == 2
    rec.recommendations(TODAY, use_cache=False)
    assert calls["n"] == 3


def test_single_flight_per_key(monkeypatch):
    calls = {"n": 0}

    def slow(end, start=None):
        calls["n"] += 1
        time.sleep(0.3)
        return {"items": []}

    monkeypatch.setattr(rec, "_build", slow)
    ts = [threading.Thread(target=rec.recommendations, args=(TODAY,)) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert calls["n"] == 1


def test_endpoint_and_range_clamps(monkeypatch):
    from src.api.main import app

    monkeypatch.setattr(config, "AGILE_TARIFF_START_DATE", "2026-04-17", raising=False)
    seen = {}

    def fake(end, start=None, use_cache=True):
        seen.update(end=end, start=start, use_cache=use_cache)
        return {"generated_at": "x", "week": {"end": end.isoformat()}, "items": [], "week_summary": {}, "house": {}}

    monkeypatch.setattr(rec, "recommendations", fake)
    c = TestClient(app)
    r = c.get("/api/v1/insights/recommendations")
    assert r.status_code == 200 and seen["end"] == TODAY and seen["start"] == TODAY - timedelta(days=TODAY.weekday())
    r = c.get("/api/v1/insights/recommendations?start=2026-10-05&end=2026-10-11&fresh=1")
    assert r.status_code == 200 and seen["start"] == date(2026, 10, 5) and seen["use_cache"] is False
    q = "/api/v1/insights/recommendations?"
    assert c.get(q + "end=nope").status_code == 400
    assert c.get(q + f"end={(TODAY + timedelta(days=1)).isoformat()}").status_code == 400     # future
    assert c.get(q + "end=2026-01-01&start=2025-12-30").status_code == 400                    # before first data day
    assert c.get(q + "start=2026-10-12&end=2026-10-11").status_code == 400                    # start > end
    assert c.get(q + "start=2026-10-01&end=2026-10-11").status_code == 400                    # > 7 days
