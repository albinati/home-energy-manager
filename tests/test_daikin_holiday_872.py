"""#872 - Daikin holiday mode: detection, gates, LP `off` state, exit handling.

Wire-level: a mocked client must see ZERO calls while the flag is set, rows stay
PENDING, and the LP plans zero Daikin draw (no W3 slack variables).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.daikin import service as daikin_service
from src.daikin.client import DaikinClient, DaikinError
from src.daikin.models import DaikinDevice
from src.daikin_bulletproof import apply_scheduled_daikin_params, detect_user_override
from src.runtime_settings import clear_cache

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    db.init_db()
    clear_cache()
    from src import state_machine as sm
    sm._HOLIDAY_SKIP_LOGGED.clear()
    yield
    clear_cache()


def _insert_row(action_type: str, params: dict, *, start=None, end=None) -> int:
    start = start or (NOW - timedelta(minutes=10))
    end = end or (NOW + timedelta(minutes=50))
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO action_schedule
               (date, start_time, end_time, device, action_type, params, status, created_at)
               VALUES (?, ?, ?, 'daikin', ?, ?, 'pending', ?)""",
            (NOW.date().isoformat(), start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"), action_type,
             json.dumps(params), NOW.isoformat()),
        )
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    return rid


def _rows():
    return db.get_actions_for_plan_date(NOW.date().isoformat(), device="daikin")


def _skip_logs():
    return db.get_action_logs(device="daikin", action="holiday_mode_skip")


# ---------------------------------------------------------------- (h) parsing
def test_parse_without_holiday_keys_gives_none() -> None:
    c = DaikinClient.__new__(DaikinClient)
    mps = [
        {"managementPointType": "climateControl", "embeddedId": "climateControlMainZone",
         "onOffMode": {"value": "on"}},
        {"managementPointType": "domesticHotWaterTank", "embeddedId": "domesticHotWaterTank",
         "onOffMode": {"value": "on"}},
    ]
    dev = c._parse_device("gw", "x", mps, {})
    assert dev.holiday_active is None and dev.tank_holiday_active is None
    assert dev.holiday_window is None
    assert not daikin_service.daikin_holiday_active("climate")


def test_parse_with_holiday_keys() -> None:
    c = DaikinClient.__new__(DaikinClient)
    mps = [
        {"managementPointType": "climateControl", "embeddedId": "climateControlMainZone",
         "onOffMode": {"value": "off"},
         "isHolidayModeActive": {"settable": False, "value": True},
         "holidayMode": {"settable": True,
                         "value": {"enabled": False, "startDate": "2026-10-28", "endDate": "2026-11-01"}}},
        {"managementPointType": "domesticHotWaterTank", "embeddedId": "domesticHotWaterTank",
         "isHolidayModeActive": {"settable": False, "value": False}},
    ]
    dev = c._parse_device("gw", "x", mps, {})
    assert dev.holiday_active is True
    assert dev.tank_holiday_active is False
    assert dev.holiday_window["endDate"] == "2026-11-01"  # informational only


def _cache(monkeypatch, dev):
    monkeypatch.setattr(
        daikin_service, "get_cached_devices",
        lambda **kw: daikin_service.CachedDevices(
            devices=[dev], fetched_at_wall=0.0, age_seconds=12.0, stale=False, source="cache"),
    )


def test_holiday_state_and_service_guard(monkeypatch) -> None:
    dev = DaikinDevice(id="gw", name="x", holiday_active=True, tank_holiday_active=False)
    _cache(monkeypatch, dev)
    st = daikin_service.holiday_state()
    assert st["climate"] is True and st["tank"] is False and st["cache_age_seconds"] == 12.0
    with pytest.raises(DaikinError, match=r"\[holiday\]"):
        daikin_service.set_lwt_offset(1.0)
    # tank point is NOT in holiday -> the guard passes (and then needs a client: not our concern)
    daikin_service._passive_guard("set tank power", "tank")


# --------------------------------------------------------------- (g) kill switch
def test_kill_switch_disables_every_gate(monkeypatch) -> None:
    monkeypatch.setattr(config, "DAIKIN_HOLIDAY_DETECT_ENABLED", False)
    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=0.0,
                       holiday_active=True, tank_holiday_active=True)
    _cache(monkeypatch, dev)
    assert daikin_service.holiday_state()["climate"] is None
    daikin_service._passive_guard("set power")  # no raise
    client = MagicMock()
    assert apply_scheduled_daikin_params(dev, client, {"lwt_offset": 2.0}, trigger="t") is True
    client.set_lwt_offset.assert_called_once()
    from src import state_machine as sm
    rid = _insert_row("lwt_preheat", {"lwt_offset": 3.0})
    client2 = MagicMock()
    sm._reconcile_daikin_actions(_rows(), client2, dev, NOW, trigger="t")
    assert db.get_action_by_id(rid)["status"] != "pending"
    assert _skip_logs() == []


# ------------------------------------------------------------ (a)/(b) reconciler
def test_reconciler_holds_lwt_row_in_climate_holiday() -> None:
    from src import state_machine as sm
    dev = DaikinDevice(id="gw", name="x", is_on=False, lwt_offset=0.0, holiday_active=True)
    rid = _insert_row("lwt_preheat", {"lwt_offset": 3.0})
    rid2 = _insert_row("lwt_preheat", {"lwt_offset": 2.0})
    client = MagicMock()
    sm._reconcile_daikin_actions(_rows(), client, dev, NOW, trigger="heartbeat")
    assert client.method_calls == []  # ZERO client calls
    assert db.get_action_by_id(rid)["status"] == "pending"
    assert db.get_action_by_id(rid2)["status"] == "pending"
    assert len(_skip_logs()) == 1  # per dispatch, not per row
    sm._reconcile_daikin_actions(_rows(), client, dev, NOW, trigger="heartbeat")
    assert len(_skip_logs()) == 1  # same dispatch -> not logged again


def test_reconciler_holds_tank_row_in_tank_holiday_but_not_lwt() -> None:
    from src import state_machine as sm
    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=0.0, tank_on=False,
                       tank_target=45.0, holiday_active=False, tank_holiday_active=True)
    tank_rid = _insert_row("tank_warmup", {"tank_power": True, "tank_temp": 50.0})
    client = MagicMock()
    sm._reconcile_daikin_actions(_rows(), client, dev, NOW, trigger="heartbeat")
    assert client.method_calls == []
    assert db.get_action_by_id(tank_rid)["status"] == "pending"
    assert len(_skip_logs()) == 1


# ------------------------------------------------------------------------ (c)
def test_apply_scheduled_holiday_marks_error_msg() -> None:
    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=0.0, tank_on=True,
                       holiday_active=True, tank_holiday_active=False)
    client = MagicMock()
    assert apply_scheduled_daikin_params(dev, client, {"lwt_offset": 2.0}, trigger="t") is False
    assert client.method_calls == []
    logs = db.get_action_logs(device="daikin", action="scheduled_apply")
    assert logs and logs[0]["error_msg"] == "holiday_mode"


# ------------------------------------------------------------------------ (e)
def test_detect_user_override_ignores_holiday_off() -> None:
    started = NOW - timedelta(hours=2)
    base = dict(id="gw", name="x", is_on=False, tank_on=False, lwt_offset=0.0, tank_target=30.0)
    params = {"climate_on": True, "tank_power": True, "tank_temp": 50.0, "lwt_offset": 3.0}
    normal = DaikinDevice(**base)
    assert detect_user_override(normal, params, row_started_utc=started, now_utc=NOW)[0] is True
    hol = DaikinDevice(**base, holiday_active=True, tank_holiday_active=True)
    assert detect_user_override(hol, params, row_started_utc=started, now_utc=NOW) == (False, None)


# ------------------------------------------------------------------------ (d)
def _mk_dev(*, climate_on: bool, tank_on: bool) -> DaikinDevice:
    return DaikinDevice(id="gw", name="x", is_on=climate_on, tank_on=tank_on,
                        holiday_active=False, tank_holiday_active=False)


def _patch_runner(monkeypatch):
    from src.scheduler import runner
    calls: list = []
    monkeypatch.setattr(runner.daikin_service, "set_power",
                        lambda on, actor="api": calls.append(("set_power", on)))
    monkeypatch.setattr(runner.daikin_service, "set_tank_power",
                        lambda on, actor="api": calls.append(("set_tank_power", on)))
    monkeypatch.setattr(runner, "bulletproof_mpc_job",
                        lambda **kw: calls.append(("mpc", kw)) or True)
    return runner, calls


def test_exit_transition_powers_on_then_replans(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": True}))
    ev = runner._holiday_transition_tick([_mk_dev(climate_on=False, tank_on=False)])
    assert ev == "exited"
    assert calls == [
        ("set_power", True),
        ("set_tank_power", True),
        ("mpc", {"bypass_cooldown": True, "trigger_reason": "holiday_end"}),
    ]
    logs = db.get_action_logs(device="daikin", action="holiday_mode")
    assert logs[0]["params"]["event"] == "exited"
    assert db.get_action_logs(device="daikin", action="holiday_exit_power_on")
    # idempotent: the next tick sees no change
    calls.clear()
    assert runner._holiday_transition_tick([_mk_dev(climate_on=False, tank_on=False)]) is None
    assert calls == []


def test_exit_transition_already_on_only_replans(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": True}))
    assert runner._holiday_transition_tick([_mk_dev(climate_on=True, tank_on=True)]) == "exited"
    assert [c[0] for c in calls] == ["mpc"]


def test_exit_read_only_never_powers_on(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True, raising=False)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": False}))
    assert runner._holiday_transition_tick([_mk_dev(climate_on=False, tank_on=False)]) == "exited"
    assert [c[0] for c in calls] == ["mpc"]


def test_enter_transition_logs_once(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    dev = DaikinDevice(id="gw", name="x", holiday_active=True, tank_holiday_active=True,
                       holiday_window={"startDate": "2026-10-28", "endDate": "2026-11-01"})
    assert runner._holiday_transition_tick([dev]) == "entered"  # no stored state + active = entered


def test_enter_from_inactive(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": False, "tank": False}))
    dev = DaikinDevice(id="gw", name="x", holiday_active=True, tank_holiday_active=True)
    assert runner._holiday_transition_tick([dev]) == "entered"
    assert runner._holiday_transition_tick([dev]) is None
    assert calls == []
    assert len(db.get_action_logs(device="daikin", action="holiday_mode")) == 1


def test_transition_kill_switch_and_missing_keys(monkeypatch) -> None:
    runner, calls = _patch_runner(monkeypatch)
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": True}))
    # payload without the keys: no signal
    assert runner._holiday_transition_tick([DaikinDevice(id="gw", name="x")]) is None
    monkeypatch.setattr(config, "DAIKIN_HOLIDAY_DETECT_ENABLED", False)
    assert runner._holiday_transition_tick([_mk_dev(climate_on=False, tank_on=False)]) is None
    assert calls == []


# ------------------------------------------------------------------------ (f)
def _solve(init_kwargs: dict, *, w3: bool = False, monkeypatch=None):
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    n = 8
    start = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    slots = [start + timedelta(minutes=30 * i) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[5.0] * n,
        shortwave_radiation_wm2=[50.0] * n,
        cloud_cover_pct=[80.0] * n,
        pv_kwh_per_slot=[0.1] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    init = LpInitialState(soc_kwh=5.0, tank_temp_c=45.0,
                          indoor_temp_c=(19.0 if w3 else None), **init_kwargs)
    return solve_lp(
        slot_starts_utc=slots, price_pence=[10.0] * n, base_load_kwh=[0.4] * n,
        weather=weather, initial=init, tz=ZoneInfo("Europe/London"),
    )


def test_lp_off_state_plans_zero_daikin_draw(monkeypatch) -> None:
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    plan = _solve({"holiday_climate": True, "holiday_tank": True}, w3=True)
    assert plan.ok
    assert plan.daikin_state == "off"
    assert all(abs(x) < 1e-9 for x in plan.space_electric_kwh)
    assert all(abs(x) < 1e-9 for x in plan.dhw_electric_kwh)
    assert plan.indoor_temp_c == []  # W3 comfort/ceiling/emergency not built at all
    assert not any(plan.comfort_slack_c or [])
    # same inputs without holiday: active state
    normal = _solve({"holiday_climate": False, "holiday_tank": False})
    assert normal.daikin_state == "active"


def test_lp_off_state_overrides_passive_prediction(monkeypatch) -> None:
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False, raising=False)
    passive = _solve({"holiday_climate": False, "holiday_tank": False})
    assert passive.daikin_state == "passive"
    assert sum(passive.space_electric_kwh) > 0  # firmware-curve prediction
    off = _solve({"holiday_climate": True, "holiday_tank": True})
    assert off.daikin_state == "off"
    assert sum(off.space_electric_kwh) == 0 and sum(off.dhw_electric_kwh) == 0


def test_lp_partial_holiday_zeroes_only_that_point(monkeypatch) -> None:
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False, raising=False)
    tank_off = _solve({"holiday_climate": False, "holiday_tank": True})
    assert tank_off.ok and sum(tank_off.dhw_electric_kwh) == 0
    climate_off = _solve({"holiday_climate": True, "holiday_tank": False})
    assert climate_off.ok and sum(climate_off.space_electric_kwh) == 0


def test_lp_kill_switch_ignores_holiday(monkeypatch) -> None:
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_HOLIDAY_DETECT_ENABLED", False)
    plan = _solve({"holiday_climate": True, "holiday_tank": True})
    assert plan.daikin_state == "active"


# ---------------------------------------------------------- writer / watchdog
def test_write_daikin_from_lp_plan_holiday_writes_no_rows(monkeypatch) -> None:
    from src.scheduler import lp_dispatch
    monkeypatch.setattr(lp_dispatch, "_daikin_holiday_all", lambda: True)
    plan = _solve({"holiday_climate": True, "holiday_tank": True})
    _insert_row("lwt_preheat", {"lwt_offset": 1.0},
                start=plan.slot_starts_utc[0], end=plan.slot_starts_utc[-1])
    n = lp_dispatch._write_daikin_from_lp_plan_impl(NOW.date().isoformat(), plan, [])
    assert n == 0
    assert _rows() == []


def test_lwt_backstops_hold_in_holiday(monkeypatch) -> None:
    from src.scheduler import lwt_coast
    dev = DaikinDevice(id="gw", name="x", holiday_active=True)
    lwt_coast.reset_backstop()
    monkeypatch.setattr(lwt_coast.db, "get_latest_indoor_reading",
                        lambda **kw: pytest.fail("must not read the sensor in holiday"))
    out = lwt_coast.backstop_tick(now_utc=NOW, plan_date=NOW.date().isoformat(), dev=dev,
                                  client=MagicMock(), in_peak=False)
    assert out["fired"] is False
    out = lwt_coast.warm_backstop_tick(now_utc=NOW, plan_date=NOW.date().isoformat(), dev=dev,
                                       client=MagicMock())
    assert out["fired"] is False


def test_emergency_tick_silent_in_holiday(monkeypatch) -> None:
    from src.scheduler import lwt_coast
    monkeypatch.setattr(config, "LWT_EMERGENCY_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LP_W3_TIN_ENABLED", True, raising=False)
    monkeypatch.setattr(lwt_coast, "_holiday_climate", lambda dev=None: True)
    monkeypatch.setattr(lwt_coast.db, "get_latest_indoor_reading",
                        lambda **kw: pytest.fail("must not read the sensor in holiday"))
    replan = MagicMock()
    out = lwt_coast.emergency_tick(now_utc=NOW, plan_date=NOW.date().isoformat(), replan_fn=replan)
    assert out["fired"] is False
    replan.assert_not_called()


def test_actuation_watchdog_suppressed_in_holiday() -> None:
    from src.analytics.actuation_health import actuation_issues, evaluate_actuation_health
    raw = {"fox_upload_at": NOW.isoformat(), "tank_last_at": (NOW - timedelta(hours=40)).isoformat(),
           "tank_failed_24h": 5, "lwt_failed_24h": 5}
    kw = dict(fox_stale_hours=30, tank_stale_hours=30, failed_threshold=3, dhw_mode="normal")
    kinds = {k for k, _ in actuation_issues(evaluate_actuation_health(raw, NOW, **kw))}
    assert {"tank_stale", "tank_failing", "lwt_failing"} <= kinds
    hol = evaluate_actuation_health(raw, NOW, holiday={"climate": True, "tank": True}, **kw)
    assert actuation_issues(hol) == []


def test_scorecard_skips_tank_floor_alert_on_holiday_day() -> None:
    from src.analytics import cosy_scorecard as cs
    low = {"label": "morning", "tank_c": 30, "floor_c": 40, "entry_local": "07:00", "below_floor": True}
    row = {"date": "2026-10-30", "payload": {"tank": {"showers": [low]}}}
    assert any("tank_floor" in k for k, _ in cs.evaluate_alerts(row))
    row["payload"]["holiday"] = True
    assert not any("tank_floor" in k for k, _ in cs.evaluate_alerts(row))
