"""#847 — the LWT demand gate blocks boosts, never coasts.

Prod incident 2026-10-09 22:55Z: with DAIKIN_LWT_SOURCE=lp the trailing window
was full of the plan's own (negative) offset windows, the gate excluded every
bucket, closed, and the writer returned having already cleared the pending rows.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config as app_config
from src.scheduler import lp_dispatch
from src.scheduler.lp_optimizer import LpPlan

TZ = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    p = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", p)
    monkeypatch.setattr(app_config, "DB_PATH", p, raising=False)
    db.init_db()
    for k, v in {
        "DAIKIN_LWT_PREHEAT_ENABLED": True, "DAIKIN_CONTROL_MODE": "active",
        "DAIKIN_LWT_PREHEAT_BOOST_C": 3, "DAIKIN_LWT_PREHEAT_NEGATIVE_BOOST_C": 5,
        "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C": -2, "DAIKIN_LWT_PREHEAT_COMFORT_BAND_C": 0.5,
        "OPTIMIZATION_LWT_OFFSET_MIN": -10.0, "OPTIMIZATION_LWT_OFFSET_MAX": 10.0,
        "DAIKIN_WEATHER_CURVE_HIGH_C": 18.0, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C": 15.0,
        "INDOOR_SETPOINT_C": 21.0, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS": 1,
        "BULLETPROOF_TIMEZONE": "Europe/London", "OCTOPUS_TARIFF_CODE": "E-1R-COSY-22-12-08-H",
        "DAIKIN_LWT_PREHEAT_MIN_TRAILING_HEATING_KWH": 0.5,
        "DAIKIN_LWT_PREHEAT_DEMAND_HOLD_HOURS": 0.0,
    }.items():
        monkeypatch.setattr(app_config, k, v, raising=False)
    monkeypatch.setitem(app_config._overrides, "DAIKIN_LWT_SOURCE", "tier")
    monkeypatch.setattr(db, "get_latest_indoor_reading", lambda max_age_minutes=30: None)
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 150)
    monkeypatch.setattr("src.notifier.notify_risk", lambda *a, **k: None)
    yield


def _plan(start):
    n = 8
    p = LpPlan(
        ok=True, status="Optimal", objective_pence=0.0,
        slot_starts_utc=[start + timedelta(minutes=30 * i) for i in range(n)],
        price_pence=[12.5] * 4 + [38.2] * 4, temp_outdoor_c=[5.0] * n,
        cheap_threshold_pence=18.97, peak_threshold_pence=31.81,
        tariff_structure_kind="banded", price_band=["cheap"] * 4 + ["peak"] * 4,
    )
    p.lwt_offset_c = [0.0] * n
    p.space_electric_kwh = [0.3] * n
    return p


def _start():
    return (datetime.now(UTC) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def _pending_offsets():
    out = []
    for d in {(_start() + timedelta(days=i)).date().isoformat() for i in (-1, 0, 1)}:
        for r in db.get_actions_for_plan_date(d, "daikin"):
            if r["action_type"] == "lwt_preheat" and r["status"] == "pending":
                out.append(r["params"]["lwt_offset"])
    return sorted(out)


def _seed_window(start_local, hours, offset, status="completed"):
    s = start_local.astimezone(UTC)
    db.upsert_action(
        plan_date=start_local.date().isoformat(),
        start_time=s.isoformat().replace("+00:00", "Z"),
        end_time=(s + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
        device="daikin", action_type="lwt_preheat", params={"lwt_offset": offset}, status=status,
    )


def test_negative_windows_do_not_contaminate_but_positive_do():
    y = (datetime.now(TZ) - timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    db.upsert_daikin_consumption_2hourly(
        date=y.date().isoformat(), bucket_idx=4, kwh_total=1.5, kwh_heating=1.5,
        kwh_dhw=0.0, source="onecta",
    )
    _seed_window(y, 2.0, -2)
    diag: dict = {}
    assert db.measured_space_heating_kwh_excluding_offset_windows(48, diag=diag) == pytest.approx(1.5)
    assert diag["excluded_buckets"] == 0
    # the old behaviour is still reachable explicitly
    assert db.measured_space_heating_kwh_excluding_offset_windows(48, positive_only=False) == 0.0
    _seed_window(y + timedelta(minutes=30), 1.0, 3)  # distinct start (natural-key upsert)
    diag = {}
    assert db.measured_space_heating_kwh_excluding_offset_windows(48, diag=diag) == 0.0
    assert diag["excluded_buckets"] >= 1


def test_closed_gate_writes_coasts_zeroes_boosts_and_logs(monkeypatch):
    monkeypatch.setattr(db, "measured_space_heating_kwh_excluding_offset_windows",
                        lambda *a, **k: 0.0)
    n = lp_dispatch._write_lwt_preheat_actions(_start().date().isoformat(), _plan(_start()), [])
    assert n >= 2
    offs = _pending_offsets()
    assert offs and all(o < 0 for o in offs), offs  # the peak setback survives, no +3
    rows = db.get_action_logs(device="daikin", action="lwt_demand_gate", limit=5)
    assert rows
    p = rows[0]["params"]
    assert p["windows_suppressed"] == 1 and p["measured_kwh"] == 0.0 and "excluded_buckets" in p


def test_open_gate_unchanged(monkeypatch):
    monkeypatch.setattr(db, "measured_space_heating_kwh_excluding_offset_windows",
                        lambda *a, **k: 2.0)
    lp_dispatch._write_lwt_preheat_actions(_start().date().isoformat(), _plan(_start()), [])
    assert _pending_offsets() == [-2, 3]
    assert not db.get_action_logs(device="daikin", action="lwt_demand_gate", limit=5)


def test_hysteresis_holds_across_local_day_boundary(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_DEMAND_HOLD_HOURS", 24.0, raising=False)
    now = {"t": datetime(2026, 10, 9, 21, 0, tzinfo=UTC)}

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now["t"].astimezone(tz) if tz else now["t"].replace(tzinfo=None)

    monkeypatch.setattr(lp_dispatch, "datetime", _FrozenDT)
    measured = {"v": 2.0}
    monkeypatch.setattr(db, "measured_space_heating_kwh_excluding_offset_windows",
                        lambda *a, **k: measured["v"])
    monkeypatch.setattr(db, "get_latest_daikin_telemetry", lambda source=None: {"outdoor_temp_c": 9.0})
    assert lp_dispatch._space_heating_demand_present() is True  # measured open, hold armed
    measured["v"] = 0.0
    now["t"] = datetime(2026, 10, 9, 23, 5, tzinfo=UTC)  # 00:05 BST next local day
    assert lp_dispatch._space_heating_demand_present() is True  # held
    assert lp_dispatch.space_heating_gate_state()["demand_gate_hold_until"] == "2026-10-10T21:00:00Z"
    # warm outdoor overrides the hold
    monkeypatch.setattr(db, "get_latest_daikin_telemetry", lambda source=None: {"outdoor_temp_c": 16.0})
    assert lp_dispatch._space_heating_demand_present() is False
    monkeypatch.setattr(db, "get_latest_daikin_telemetry", lambda source=None: {"outdoor_temp_c": 9.0})
    now["t"] = datetime(2026, 10, 10, 21, 30, tzinfo=UTC)  # hold expired
    assert lp_dispatch._space_heating_demand_present() is False


def _fake_impl(plan_date, plan, forecast):
    """Stand-in for the regime body: it clears the window like every real regime
    does, THEN runs the LWT writer."""
    ws = plan.slot_starts_utc[0].isoformat().replace("+00:00", "Z")
    we = (plan.slot_starts_utc[-1] + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    db.clear_actions_in_range(ws, we, device="daikin")
    return lp_dispatch._write_lwt_preheat_actions(plan_date, plan, forecast)


def test_wire_gate_closed_replan_keeps_pending_coasts(monkeypatch):
    monkeypatch.setattr(lp_dispatch, "_write_daikin_from_lp_plan_impl", _fake_impl)
    measured = {"v": 2.0}
    monkeypatch.setattr(db, "measured_space_heating_kwh_excluding_offset_windows",
                        lambda *a, **k: measured["v"])
    plan, pd = _plan(_start()), _start().date().isoformat()
    lp_dispatch.write_daikin_from_lp_plan(pd, plan, [])
    assert _pending_offsets() == [-2, 3]
    measured["v"] = 0.0  # gate closes
    lp_dispatch.write_daikin_from_lp_plan(pd, plan, [])
    assert _pending_offsets() == [-2]  # coast survives, boost gone, horizon not empty


def test_wire_quota_skip_restores_cleared_rows(monkeypatch):
    monkeypatch.setattr(lp_dispatch, "_write_daikin_from_lp_plan_impl", _fake_impl)
    monkeypatch.setattr(db, "measured_space_heating_kwh_excluding_offset_windows",
                        lambda *a, **k: 2.0)
    plan, pd = _plan(_start()), _start().date().isoformat()
    lp_dispatch.write_daikin_from_lp_plan(pd, plan, [])
    assert _pending_offsets() == [-2, 3]
    monkeypatch.setattr("src.api_quota.quota_remaining", lambda vendor: 0)  # writer skips
    lp_dispatch.write_daikin_from_lp_plan(pd, plan, [])
    assert _pending_offsets() == [-2, 3]
    assert db.get_action_logs(device="daikin", action="lwt_rows_preserved", limit=2)


def test_state_exposes_new_fields(monkeypatch):
    monkeypatch.setattr(db, "get_latest_daikin_telemetry", lambda source=None: {"outdoor_temp_c": 5.0})
    st = lp_dispatch.space_heating_gate_state()
    assert "demand_gate_hold_until" in st and "excluded_buckets" in st
