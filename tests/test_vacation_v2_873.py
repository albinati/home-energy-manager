"""#873 - vacation mode v2: mandatory return time, Onecta holiday window,
scheduled recovery, normal battery economics, reduced load. Wire-level: the
Daikin service / client and the MPC entry point are mocked, nothing touches the
network or hardware."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import db, runtime_settings as rts, vacation
from src.config import config
from src.daikin.service import DaikinError
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

LONDON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    type(config)._overrides.clear()
    rts.clear_cache()
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    # no daemon threads / real sleeps: tests run the side effects inline
    monkeypatch.setattr(vacation, "BACKGROUND", False)
    monkeypatch.setattr(vacation, "_MPC_RETRY_WAIT_S", 0.0)
    monkeypatch.setattr(vacation, "_HOLIDAY_POLL_WAIT_S", 0.0)
    yield
    type(config)._overrides.clear()
    rts.clear_cache()


class _World:
    """Recorder for every outbound effect, in call order."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        self.risks: list[str] = []
        self.infos: list[str] = []
        self.holiday_fail = False
        self.mpc_results: list[bool] = []   # popped per call; empty -> True
        self.jobs: list[dict] = []
        self.kv_at_post: str | None = None
        self.devices = [SimpleNamespace(is_on=False, tank_on=True, tank_temperature=44.0)]
        from src import notifier
        from src.scheduler import runner

        svc = vacation.daikin_service
        monkeypatch.setattr(svc, "set_holiday_mode", self._holiday)
        monkeypatch.setattr(svc, "set_power", lambda on, actor="api": self.calls.append(("set_power", on)))
        monkeypatch.setattr(svc, "set_tank_power", lambda on, actor="api": self.calls.append(("set_tank_power", on)))
        monkeypatch.setattr(svc, "force_refresh_devices", self._refresh)
        monkeypatch.setattr(svc, "get_cached_devices", self._refresh_cached)
        monkeypatch.setattr(runner, "bulletproof_mpc_job", self._mpc)
        monkeypatch.setattr(
            runner, "_background_scheduler",
            SimpleNamespace(add_job=lambda fn, trig, **kw: self.jobs.append({"fn": fn, "trig": trig, **kw}),
                            remove_job=lambda jid: None),
        )
        monkeypatch.setattr(notifier, "notify_risk", lambda m, extra=None: self.risks.append(m))
        monkeypatch.setattr(notifier, "notify_action_confirmation", lambda m: self.infos.append(m))

    def _holiday(self, enabled, start_date=None, end_date=None, actor="api"):
        self.calls.append(("holiday", enabled, start_date, end_date))
        if not enabled:
            self.preset_at_off = config.OPTIMIZATION_PRESET
        self.kv_at_post = db.get_kv(vacation.KV_END_AT)
        if self.holiday_fail:
            raise DaikinError("HTTP 500: boom")

    def _refresh(self, actor, min_interval_seconds=None):
        self.calls.append(("read",))
        if getattr(self, "device_seq", None):
            return SimpleNamespace(devices=[self.device_seq.pop(0)])
        return SimpleNamespace(devices=self.devices)

    def _refresh_cached(self, allow_refresh=True, actor="x", **kw):
        self.calls.append(("read",))
        return SimpleNamespace(devices=self.devices)

    def _mpc(self, **kw):
        self.calls.append(("mpc", kw))
        return self.mpc_results.pop(0) if self.mpc_results else True

    def writes(self):
        return [c[0] if c[0] != "holiday" else ("holiday", c[1]) for c in self.calls if c[0] != "read"]


def _set_vacation(hours_ahead: float = 72.0) -> datetime:
    ret = datetime.now(UTC) + timedelta(hours=hours_ahead)
    rts.set_setting("VACATION_RETURN_AT", ret.astimezone(LONDON).strftime("%Y-%m-%dT%H:%M"))
    rts.set_setting("OPTIMIZATION_PRESET", "vacation")
    return ret


# ---------------------------------------------------------------------------
# (a) activation: kv first, failing POST still schedules recovery + notifies
# ---------------------------------------------------------------------------

def test_activation_failed_post_still_persists_and_schedules(monkeypatch):
    w = _World(monkeypatch)
    w.holiday_fail = True
    jobs: list[dict] = []
    from src.scheduler import runner

    fake = SimpleNamespace(add_job=lambda fn, trig, **kw: jobs.append({"fn": fn, "trig": trig, **kw}),
                           remove_job=lambda jid: None)
    monkeypatch.setattr(runner, "_background_scheduler", fake)
    _set_vacation(72)

    plan = vacation.activate(actor="test")

    assert plan is not None
    # kv was written BEFORE the POST was attempted
    assert w.kv_at_post and w.kv_at_post == db.get_kv(vacation.KV_END_AT)
    assert db.get_kv(vacation.KV_END_AT)
    # recovery job armed under the documented id
    assert jobs and jobs[0]["id"] == "vacation_end" and jobs[0]["fn"] is vacation.run_recovery
    # failed POST: one risk alert, activation info, replan still ran
    assert len(w.risks) == 1 and "Holiday não ativou" in w.risks[0]
    assert len(w.infos) == 1 and "Fora até" in w.infos[0]
    assert any(c[0] == "mpc" and c[1]["trigger_reason"] == "vacation_start" for c in w.calls)
    # order: holiday POST precedes the replan
    names = [c[0] for c in w.calls if c[0] in ("holiday", "mpc")]
    assert names == ["holiday", "mpc"]
    st = vacation.load_state()
    assert st["holiday_post"].startswith("failed") and st["holiday_until_sent"] is None


def test_activation_success_sends_window_and_reports_it(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(96)
    plan = vacation.activate(actor="test")
    hol = [c for c in w.calls if c[0] == "holiday"][0]
    assert hol[1] is True and hol[2] == plan["start_date"] and hol[3] == plan["end_date"]
    assert not w.risks
    assert vacation.status_block()["daikin_holiday_until_sent"] == plan["end_date"]


# ---------------------------------------------------------------------------
# (b) recovery sequence
# ---------------------------------------------------------------------------

def test_recovery_sequence_exact_order_and_idempotent(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    w.calls.clear()
    w.devices = [SimpleNamespace(is_on=False, tank_on=True, tank_temperature=44.0)]

    r = vacation.run_recovery("scheduled")

    assert r["status"] == "done"
    assert w.writes() == [("holiday", False), "set_power", "mpc"]  # tank already on: no tank write
    mpc = [c for c in w.calls if c[0] == "mpc"][0][1]
    assert mpc == {"force_write_devices": True, "bypass_cooldown": True, "trigger_reason": "vacation_end"}
    assert [c for c in w.calls if c[0] == "holiday"][0] == ("holiday", False, None, None)
    assert config.OPTIMIZATION_PRESET == "normal"
    assert config.VACATION_RETURN_AT == ""
    assert not db.get_kv(vacation.KV_END_AT)
    assert len(w.infos) == 2  # activation + recovery
    # running it twice is harmless
    n = len(w.calls)
    assert vacation.run_recovery("scheduled")["status"] == "noop"
    assert len(w.calls) == n


def test_recovery_powers_tank_only_when_off_and_survives_step_errors(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    w.calls.clear()
    w.holiday_fail = True  # holiday-off errors never block the next step
    w.devices = [SimpleNamespace(is_on=True, tank_on=False, tank_temperature=30.0)]
    vacation.run_recovery("scheduled")
    assert w.writes() == [("holiday", False), "set_tank_power", "mpc"]


# ---------------------------------------------------------------------------
# (c) fallback endDate arithmetic
# ---------------------------------------------------------------------------

def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_holiday_dates_job_day_minus_one():
    now = _utc(2026, 11, 2, 10)
    start, end, clamped = vacation.holiday_dates(_utc(2026, 11, 7, 6), now)
    assert (start, end, clamped) == ("2026-11-02", "2026-11-06", False)


def test_holiday_dates_same_day_edge_clamps_to_today():
    now = _utc(2026, 11, 2, 10)
    start, end, clamped = vacation.holiday_dates(_utc(2026, 11, 2, 22), now)
    assert (start, end, clamped) == ("2026-11-02", "2026-11-02", True)
    # next-day job: endDate = today (not clamped), firmware resumes 00:00 tomorrow = job day
    start, end, clamped = vacation.holiday_dates(_utc(2026, 11, 3, 4), now)
    assert (start, end, clamped) == ("2026-11-02", "2026-11-02", False)


def test_holiday_dates_use_the_local_day_across_dst():
    # BST ends 2026-10-25 01:00 UTC. 23:30 UTC on the 24th is 00:30 BST on the 25th:
    # the job's LOCAL day is the 25th, so endDate is the 24th (UTC date - 1 would be the 23rd).
    now = _utc(2026, 10, 20, 9)
    assert vacation.holiday_dates(_utc(2026, 10, 24, 23, 30), now)[1] == "2026-10-24"
    # Spring forward 2026-03-29 01:00 UTC: 00:30 UTC on the 29th is 00:30 GMT, local day 29th.
    now = _utc(2026, 3, 25, 9)
    assert vacation.holiday_dates(_utc(2026, 3, 29, 0, 30), now)[1] == "2026-03-28"
    # 23:30 UTC on 28 Mar is still 23:30 GMT on the 28th -> endDate 27th.
    assert vacation.holiday_dates(_utc(2026, 3, 28, 23, 30), now)[1] == "2026-03-27"
    # Day AFTER the change (BST): 23:30 UTC on the 29th is 00:30 BST on the 30th -> 29th.
    assert vacation.holiday_dates(_utc(2026, 3, 29, 23, 30), now)[1] == "2026-03-29"


def test_recovery_time_is_return_minus_hours_and_snaps_on_banded(monkeypatch):
    monkeypatch.setattr(config, "VACATION_RECOVERY_HOURS", 12, raising=False)
    ret = _utc(2026, 11, 7, 18)
    # dynamic / unknown tariff: no snap
    assert vacation.compute_recovery_at(ret) == _utc(2026, 11, 7, 6)

    from src.analytics import load_expected as le

    def _wins(day, tz=None):
        base = datetime(day.year, day.month, day.day, tzinfo=LONDON).astimezone(UTC)
        mk = lambda h0, h1, key: SimpleNamespace(  # noqa: E731
            key=key, start_utc=base + timedelta(hours=h0), end_utc=base + timedelta(hours=h1))
        return [mk(4, 7, "band_cheap"), mk(13, 16, "band_cheap"), mk(22, 24, "band_cheap"),
                mk(7, 13, "band_day")], "banded"

    monkeypatch.setattr(le, "band_windows_for_day", _wins)
    # target 06:00 UTC (06:00 GMT on 7 Nov) sits inside 04-07 -> snap to 04:00
    assert vacation.compute_recovery_at(ret) == _utc(2026, 11, 7, 4)
    # target 10:00 is 6 h after the 04:00 start (> 3 h bound) -> NOT snapped
    assert vacation.compute_recovery_at(_utc(2026, 11, 7, 22)) == _utc(2026, 11, 7, 10)
    # target 14:30 is 1.5 h into the 13-16 band -> snap to 13:00
    assert vacation.compute_recovery_at(_utc(2026, 11, 8, 2, 30)) == _utc(2026, 11, 7, 13)
    # never snap into the past: a 04:00 start before now+60s keeps the raw target
    now = _utc(2026, 11, 7, 5)
    assert vacation.compute_recovery_at(ret, now) == _utc(2026, 11, 7, 6)


# ---------------------------------------------------------------------------
# (d) load scale
# ---------------------------------------------------------------------------

def test_load_scale_applies_only_in_vacation(monkeypatch):
    from src.scheduler.optimizer import _vacation_base_load_scale

    monkeypatch.setattr(config, "LP_VACATION_LOAD_SCALE", 0.35, raising=False)
    for preset in ("normal", "guests"):
        monkeypatch.setattr(config, "OPTIMIZATION_PRESET", preset, raising=False)
        assert _vacation_base_load_scale() == 1.0
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    assert _vacation_base_load_scale() == 0.35


def test_simulation_load_builder_mirrors_scale(monkeypatch):
    from src.scheduler import lp_simulation

    monkeypatch.setattr(lp_simulation.db, "residual_load_profile_v2", lambda *a, **k: {})
    monkeypatch.setattr(lp_simulation.db, "lookup_residual_kwh", lambda *a, **k: 0.40)
    monkeypatch.setattr(config, "LP_LOAD_SCALE_FACTOR", 1.0, raising=False)
    monkeypatch.setattr(config, "LP_VACATION_LOAD_SCALE", 0.5, raising=False)
    slots = [datetime(2026, 6, 17, 18, 0, tzinfo=UTC) + timedelta(minutes=30 * i) for i in range(4)]
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    assert all(v == 0.40 for v in lp_simulation._build_load_profile(slots))
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    assert all(abs(v - 0.20) < 1e-9 for v in lp_simulation._build_load_profile(slots))


def test_load_scale_setting_bounds():
    spec = rts.SCHEMA["LP_VACATION_LOAD_SCALE"]
    assert (spec.min_value, spec.max_value) == (0.1, 1.0)
    assert rts.get_setting("LP_VACATION_LOAD_SCALE") == pytest.approx(0.35)
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("LP_VACATION_LOAD_SCALE", 0.05)


# ---------------------------------------------------------------------------
# (e) LP: vacation battery economics
# ---------------------------------------------------------------------------

def _lp_vacation(monkeypatch, *, export_enabled: bool):
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "LP_CBC_TIME_LIMIT_SECONDS", 15, raising=False)
    monkeypatch.setattr(config, "LP_INVERTER_STRESS_COST_PENCE", 0.0, raising=False)
    monkeypatch.setattr(config, "DHW_DAILY_SHOWER_LITRES", 0.0, raising=False)
    monkeypatch.setattr(config, "LP_PV_SUFFICIENCY_GUARD", False, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    monkeypatch.setattr(config, "LP_BATTERY_EXPORT_ENABLED", export_enabled, raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 12
    slots = [base + i * timedelta(minutes=30) for i in range(n)]
    w = WeatherLpSeries(
        slot_starts_utc=slots, temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[0.0] * n, cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[0.0] * n, cop_space=[3.0] * n, cop_dhw=[2.8] * n,
    )
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=[5.0] * 6 + [25.0] * 6,
        base_load_kwh=[0.3] * n,
        weather=w,
        initial=LpInitialState(soc_kwh=4.0, tank_temp_c=45.0),
        tz=LONDON,
        export_price_pence=[5.0] * n,
    )
    assert plan.ok, plan.status
    return plan


def test_lp_vacation_export_disabled_allows_cheap_band_grid_charging(monkeypatch):
    plan = _lp_vacation(monkeypatch, export_enabled=False)
    assert any(
        plan.battery_charge_kwh[i] > plan.pv_use_kwh[i] + 1e-6 for i in range(len(plan.battery_charge_kwh))
    ), f"expected a grid charge, chg={plan.battery_charge_kwh}"


def test_lp_vacation_export_enabled_keeps_pv_only_charging(monkeypatch):
    plan = _lp_vacation(monkeypatch, export_enabled=True)
    for i in range(len(plan.battery_charge_kwh)):
        assert plan.battery_charge_kwh[i] <= plan.pv_use_kwh[i] + 1e-6


def test_dispatch_helpers_follow_export_flag(monkeypatch):
    from src.scheduler.optimizer import _vacation_pv_only_battery

    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    monkeypatch.setattr(config, "LP_BATTERY_EXPORT_ENABLED", True, raising=False)
    assert _vacation_pv_only_battery() is True
    monkeypatch.setattr(config, "LP_BATTERY_EXPORT_ENABLED", False, raising=False)
    assert _vacation_pv_only_battery() is False
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "LP_BATTERY_EXPORT_ENABLED", True, raising=False)
    assert _vacation_pv_only_battery() is False


# ---------------------------------------------------------------------------
# (f) 422 without a return time (API + MCP-layer helper)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.main import app

    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    monkeypatch.setenv("REQUIRE_SIMULATION_ID", "false")
    return TestClient(app)


def test_put_vacation_without_return_at_is_422(client):
    r = client.put("/api/v1/settings/OPTIMIZATION_PRESET", json={"value": "vacation"})
    assert r.status_code == 422, r.text
    assert "VACATION_RETURN_AT" in r.text
    assert rts.get_setting("OPTIMIZATION_PRESET") == "normal"


def test_put_vacation_with_past_return_at_is_422(client):
    r = client.put("/api/v1/settings/OPTIMIZATION_PRESET",
                   json={"value": "vacation", "return_at": "2020-01-01T10:00"})
    assert r.status_code == 422, r.text


def test_legacy_alias_without_return_at_is_422(client):
    r = client.put("/api/v1/settings/OPTIMIZATION_PRESET", json={"value": "travel"})
    assert r.status_code == 422, r.text
    r = client.post("/api/v1/optimization/preset", json={"preset": "away"})
    assert r.status_code == 422, r.text


def test_batch_vacation_without_return_at_is_422_and_with_it_applies(client, monkeypatch):
    _World(monkeypatch)
    r = client.post("/api/v1/settings/batch", json={"changes": {"OPTIMIZATION_PRESET": "vacation"}})
    assert r.status_code == 422, r.text
    ret = (datetime.now(UTC) + timedelta(days=3)).astimezone(LONDON).strftime("%Y-%m-%dT%H:%M")
    # preset listed BEFORE the return time: the handler applies the return time first
    r = client.post("/api/v1/settings/batch", json={"changes": {
        "OPTIMIZATION_PRESET": "vacation", "VACATION_RETURN_AT": ret}})
    assert r.status_code == 200, r.text
    assert rts.get_setting("OPTIMIZATION_PRESET") == "vacation"
    assert r.json()["vacation"]["status"] == "activating"
    assert db.get_kv(vacation.KV_END_AT)


def test_mcp_preset_tool_requires_return_at(monkeypatch):
    import asyncio

    w = _World(monkeypatch)
    from src.mcp_server import build_mcp

    mcp = build_mcp()

    async def _call(args):
        _blocks, out = await mcp.call_tool("set_optimization_preset", args)
        return out

    bad = asyncio.run(_call({"preset": "vacation"}))
    assert bad["ok"] is False and "VACATION_RETURN_AT" in bad["error"]
    assert rts.get_setting("OPTIMIZATION_PRESET") == "normal"
    ret = (datetime.now(UTC) + timedelta(days=2)).astimezone(LONDON).strftime("%Y-%m-%dT%H:%M")
    ok = asyncio.run(_call({"preset": "vacation", "return_at": ret}))
    assert ok["ok"] is True and rts.get_setting("OPTIMIZATION_PRESET") == "vacation"
    assert ok["vacation"]["status"] == "activating"
    assert not w.risks


# ---------------------------------------------------------------------------
# (g) cancel early
# ---------------------------------------------------------------------------

def test_cancel_early_runs_recovery_immediately(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    w.calls.clear()
    # the owner comes back: preset -> normal while vacation_end_at is in the future
    prev = vacation.current_preset()
    rts.set_setting("OPTIMIZATION_PRESET", "normal")
    info = vacation.after_settings_applied(prev, {"OPTIMIZATION_PRESET": "normal"}, background=False)
    assert info == {"status": "recovering"}
    assert w.writes() == [("holiday", False), "set_power", "mpc"]
    assert not db.get_kv(vacation.KV_END_AT)
    assert any("canceladas" in m for m in w.infos)


# ---------------------------------------------------------------------------
# (h) boot re-arm
# ---------------------------------------------------------------------------

def test_rearm_from_kv_at_boot(monkeypatch):
    _World(monkeypatch)
    jobs: list[dict] = []
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_background_scheduler",
                        SimpleNamespace(add_job=lambda fn, trig, **kw: jobs.append({"trig": trig, **kw}),
                                        remove_job=lambda jid: None))
    assert vacation.rearm_from_kv() is None and not jobs  # nothing stored
    at = datetime.now(UTC) + timedelta(hours=30)
    db.set_kv(vacation.KV_END_AT, at.isoformat().replace("+00:00", "Z"))
    assert vacation.rearm_from_kv() == "scheduled"
    assert jobs[0]["id"] == "vacation_end" and jobs[0]["replace_existing"] is True
    assert abs((jobs[0]["trig"].run_date - at).total_seconds()) < 1
    # a recovery time already in the past fires shortly after boot, not never
    jobs.clear()
    db.set_kv(vacation.KV_END_AT, (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z"))
    assert vacation.rearm_from_kv() == "scheduled"
    lead = (jobs[0]["trig"].run_date - datetime.now(UTC)).total_seconds()
    assert 0 < lead <= 60


# ---------------------------------------------------------------------------
# (i) kill switch
# ---------------------------------------------------------------------------

def test_kill_switch_off_no_post_no_job_no_kv(monkeypatch):
    w = _World(monkeypatch)
    monkeypatch.setattr(config, "VACATION_DAIKIN_HOLIDAY_ENABLED", False, raising=False)
    jobs: list = []
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_background_scheduler",
                        SimpleNamespace(add_job=lambda *a, **k: jobs.append(1), remove_job=lambda j: None))
    _set_vacation(72)
    assert vacation.activate(actor="test") is None
    assert not [c for c in w.calls if c[0] == "holiday"]
    assert not jobs and not db.get_kv(vacation.KV_END_AT)
    assert vacation.rearm_from_kv() is None


# ---------------------------------------------------------------------------
# Wire: the HTTP request DaikinClient.set_holiday_mode sends
# ---------------------------------------------------------------------------

def test_client_holiday_mode_wire(monkeypatch):
    import urllib.request

    from src.daikin import client as dc

    seen: list = []

    class _Resp:
        def read(self):
            return b""

    def _urlopen(req, timeout=15):
        seen.append((req.get_method(), req.full_url, json.loads(req.data.decode())))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(dc.DaikinClient, "_headers", lambda self, force_refresh=False: {})
    monkeypatch.setattr(dc.DaikinClient, "_safe_record", staticmethod(lambda kind, ok: None))
    dev = SimpleNamespace(id="gw1", climate_mp_id="climateControlMainZone")
    c = dc.DaikinClient()
    c.set_holiday_mode(dev, True, "2026-11-02", "2026-11-06")
    c.set_holiday_mode(dev, False)
    assert seen[0][0] == "POST" and seen[0][1].endswith(
        "/gateway-devices/gw1/management-points/climateControlMainZone/holiday-mode")
    assert seen[0][2] == {"enabled": True, "startDate": "2026-11-02", "endDate": "2026-11-06"}
    assert seen[1][2] == {"enabled": False}
    with pytest.raises(ValueError):
        c.set_holiday_mode(dev, True)


def test_service_disable_allowed_while_holiday_active(monkeypatch):
    """The holiday gate must NOT block the disable (else it can never end)."""
    from src.daikin import service as svc

    sent: list = []
    dev = SimpleNamespace(holiday_active=True, tank_holiday_active=True)
    client = SimpleNamespace(set_holiday_mode=lambda d, en, s=None, e=None: sent.append((en, s, e)))
    monkeypatch.setattr(svc, "_require_client_and_devices", lambda actor: (client, [dev]))
    monkeypatch.setattr(svc, "should_block", lambda name: False)
    monkeypatch.setattr(svc, "invalidate_after_write", lambda: None)
    monkeypatch.setattr(config, "DAIKIN_HOLIDAY_DETECT_ENABLED", True, raising=False)
    svc.set_holiday_mode(False, actor="t")
    assert sent == [(False, None, None)]
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    with pytest.raises(DaikinError):
        svc.set_holiday_mode(False, actor="t")


def test_guests_detector_default_off_and_scorecard_marks_vacation(monkeypatch):
    import os

    if "GUESTS_DETECT_ENABLED" not in os.environ:
        assert type(config).GUESTS_DETECT_ENABLED is False  # #873: code default off
    from src.analytics.cosy_scorecard import evaluate_alerts

    row = {"date": "2026-11-03", "payload": {
        "vacation": True,
        "tank": {"showers": [{"label": "evening", "below_floor": True, "tank_c": 30,
                              "floor_c": 45, "entry_local": "20:00"}]}}}
    assert evaluate_alerts(row) == []
    row["payload"]["vacation"] = False
    assert len(evaluate_alerts(row)) == 1
