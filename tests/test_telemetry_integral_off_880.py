"""#880 - the telemetry integral must not model heating/DHW kWh while the
Altherma is OFF (Onecta holiday / onOffMode=off)."""
from __future__ import annotations

import sqlite3
import sys
import types
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import db
from src.config import config, cop_at_temperature
from src.daikin import service as svc
from src.physics import get_daikin_heating_kw

DAY = date(2026, 10, 28)
ISO = DAY.isoformat()
BASE = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
FLAGS = ("is_on", "tank_on", "holiday_active", "tank_holiday_active")
KW5 = float(get_daikin_heating_kw(5.0))


@pytest.fixture(autouse=True)
def _init(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    db.init_db()


def _seed(per_sample=None, **flags):
    """7 samples 00:00..03:00, 30 min apart -> buckets 0 (4 intervals) and 1 (2).
    ``per_sample`` maps index -> flag dict (overrides ``flags``)."""
    for i in range(7):
        row = {
            "fetched_at": (BASE + timedelta(minutes=30 * i)).timestamp(),
            "source": "live", "tank_temp_c": 45.0, "indoor_temp_c": 21.0,
            "outdoor_temp_c": 5.0, "tank_target_c": 45.0, "lwt_actual_c": 35.0,
            "mode": "heating", "weather_regulation": 1,
        }
        row.update(flags)
        row.update((per_sample or {}).get(i, {}))
        db.insert_daikin_telemetry(row)


def _sql(stmt):
    with db._lock:
        c = db.get_connection()
        try:
            c.execute(stmt)
            c.commit()
        finally:
            c.close()


def _rows():
    return {r["bucket_idx"]: r for r in db.get_daikin_consumption_2hourly_range(ISO, ISO)}


def _golden_dhw_per_interval() -> float:
    loss = 1.3e-5 * (45.0 - 21.0) * float(getattr(config, "DHW_TANK_LITRES", 200)) * 0.5
    cop = max(1.5, cop_at_temperature(config.DAIKIN_COP_CURVE, 5.0)
              * float(getattr(config, "COP_DHW_PENALTY", 0.5)))
    return loss / cop


def test_normal_device_matches_golden_model():
    _seed()
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert KW5 > 0
    assert out[0]["kwh_heating"] == pytest.approx(round(KW5 * 0.5 * 4, 3))
    assert out[1]["kwh_heating"] == pytest.approx(round(KW5 * 0.5 * 2, 3))
    assert out[0]["kwh_dhw"] == pytest.approx(round(_golden_dhw_per_interval() * 4, 3), abs=1e-3)
    assert not out[0]["off"] and not out[1]["off"]


def test_explicit_on_flags_identical_to_null_flags():
    _seed()
    null_out = svc.compute_daikin_2hourly_telemetry(DAY)
    _sql("UPDATE daikin_telemetry SET is_on=1, tank_on=1, holiday_active=0, tank_holiday_active=0")
    assert svc.compute_daikin_2hourly_telemetry(DAY) == null_out


@pytest.mark.parametrize("flags", [
    {"holiday_active": True, "tank_holiday_active": True},
    {"is_on": False, "tank_on": False},
    {"holiday_active": True, "tank_on": False},
])
def test_off_zero_and_source(flags):
    _seed(**flags)
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out and all(b["kwh_total"] == 0 and b["off"] for b in out.values())
    res = svc.sync_daikin_2hourly_telemetry(DAY)
    assert res["written"] == len(out)
    rows = _rows()
    assert rows and all(r["source"] == "telemetry_integral_off" and r["kwh_total"] == 0
                        for r in rows.values())


def test_tank_only_holiday_zeroes_dhw_only():
    _seed(tank_holiday_active=True)
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_dhw"] == 0 and out[0]["kwh_heating"] > 0 and not out[0]["off"]
    svc.sync_daikin_2hourly_telemetry(DAY)
    assert all(r["source"] == "telemetry_integral" for r in _rows().values())


def test_climate_only_off_zeroes_heating_only():
    _seed(holiday_active=True)
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_heating"] == 0 and out[0]["kwh_dhw"] > 0 and not out[0]["off"]


def test_null_to_off_transition_on_deploy_day():
    """Samples before the deploy have NULL flags; NULL<->off keeps the model."""
    off = {k: True for k in ("holiday_active", "tank_holiday_active")}
    _seed(per_sample={i: off for i in range(4, 7)})
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_heating"] == pytest.approx(round(KW5 * 0.5 * 4, 3))  # incl. NULL->off interval
    assert not out[0]["off"]
    assert out[1]["kwh_total"] == 0 and out[1]["off"]


def test_mixed_bucket_only_on_intervals_count():
    off = {"holiday_active": True, "tank_holiday_active": True}
    # samples 0,1,2 off; 3.. unknown: intervals 0,1 are off-off (0), 2,3 are modelled
    _seed(per_sample={0: off, 1: off, 2: off})
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_heating"] == pytest.approx(round(KW5 * 0.5 * 2, 3))
    assert not out[0]["off"]
    svc.sync_daikin_2hourly_telemetry(DAY)
    assert _rows()[0]["source"] == "telemetry_integral"


def test_boundary_on_to_off_interval_is_half():
    on = {"is_on": True, "tank_on": True}
    off = {"is_on": False, "tank_on": False}
    _seed(per_sample={**{i: on for i in range(0, 4)}, **{i: off for i in range(4, 7)}})
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_heating"] == pytest.approx(round(KW5 * 0.5 * 3.5, 3))
    assert not out[0]["off"]
    assert out[1]["kwh_total"] == 0 and out[1]["off"]


def test_historical_rows_before_migration_unchanged(monkeypatch, tmp_path):
    """A pre-#880 daikin_telemetry (no flag columns) with rows migrates to NULL
    flags and integrates exactly as before."""
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE daikin_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at REAL NOT NULL, source TEXT NOT NULL,
            tank_temp_c REAL, indoor_temp_c REAL, outdoor_temp_c REAL, tank_target_c REAL,
            lwt_actual_c REAL, mode TEXT, weather_regulation INTEGER)""")
    for i in range(7):
        con.execute(
            "INSERT INTO daikin_telemetry (fetched_at, source, tank_temp_c, indoor_temp_c,"
            " outdoor_temp_c, tank_target_c, lwt_actual_c, mode, weather_regulation)"
            " VALUES (?, 'live', 45, 21, 5, 45, 35, 'heating', 1)",
            ((BASE + timedelta(minutes=30 * i)).timestamp(),))
    con.commit()
    con.close()
    monkeypatch.setattr(config, "DB_PATH", path, raising=False)
    db.init_db()
    with db._lock:
        c = db.get_connection()
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(daikin_telemetry)")}
            nn = c.execute("SELECT COUNT(*) FROM daikin_telemetry WHERE is_on IS NOT NULL OR tank_on IS NOT NULL"
                           " OR holiday_active IS NOT NULL OR tank_holiday_active IS NOT NULL").fetchone()[0]
            c.execute("SELECT is_on, holiday_active FROM daikin_telemetry_iso LIMIT 1")  # view has the cols
        finally:
            c.close()
    assert set(FLAGS) <= cols and nn == 0
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert out[0]["kwh_heating"] == pytest.approx(round(KW5 * 0.5 * 4, 3))
    assert out[0]["kwh_dhw"] == pytest.approx(round(_golden_dhw_per_interval() * 4, 3), abs=1e-3)
    assert not out[0]["off"]


def test_estimate_row_inherits_flags_and_keeps_bucket_off(monkeypatch):
    """Quota-blocked estimator row copies the live seed's flags (#880 review)."""
    now = datetime.now(UTC)
    db.insert_daikin_telemetry({
        "fetched_at": now.timestamp() - 4 * 3600, "source": "live", "tank_temp_c": 45.0,
        "indoor_temp_c": 21.0, "outdoor_temp_c": 8.0, "is_on": False, "tank_on": False,
        "holiday_active": True, "tank_holiday_active": True,
    })
    monkeypatch.setattr(svc, "should_block", lambda v: True)
    monkeypatch.setattr(svc, "get_cached_devices",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("no fetch")))
    st = svc.get_lp_state_cached_or_estimated(now_utc=now)
    assert st["source"] == "estimate"
    with db._lock:
        c = db.get_connection()
        try:
            r = c.execute("SELECT is_on, tank_on, holiday_active, tank_holiday_active "
                          "FROM daikin_telemetry WHERE source='estimate'").fetchone()
        finally:
            c.close()
    assert tuple(r) == (0, 0, 1, 1)
    # and the integral: off(live), estimate(inherited off), off(live) stays off
    off = {k: True for k in ("holiday_active", "tank_holiday_active")}
    _seed(per_sample={i: off for i in range(7)})
    _sql(f"UPDATE daikin_telemetry SET source='estimate' WHERE fetched_at = "
         f"{(BASE + timedelta(minutes=90)).timestamp()}")
    out = svc.compute_daikin_2hourly_telemetry(DAY)
    assert all(b["kwh_total"] == 0 and b["off"] for b in out.values())


def test_overwrite_rules_for_off_source():
    _seed(holiday_active=True, tank_holiday_active=True)
    db.upsert_daikin_consumption_2hourly(
        date=ISO, bucket_idx=1, kwh_total=0.0, kwh_heating=0.0, kwh_dhw=0.0,
        source="telemetry_integral_off")
    db.upsert_daikin_consumption_2hourly(
        date=ISO, bucket_idx=0, kwh_total=2.0, kwh_heating=2.0, kwh_dhw=0.0,
        source="onecta_cache")
    res = svc.sync_daikin_2hourly_telemetry(DAY)
    dec = {d["bucket_idx"]: d["decision"] for d in res["buckets"]}
    assert dec[1] == "refresh_telemetry"
    assert dec[0].startswith("onecta_authoritative")
    assert _rows()[0]["source"] == "onecta_cache"


def test_runner_onecta_zero_stays_onecta_cache_when_integral_agrees_off(monkeypatch):
    """Real path: Onecta rollup writes a metered 0, then the integral (unit off) must
    NOT relabel it telemetry_integral_off."""
    from src.daikin.service import CachedDevices
    from src.scheduler import runner

    _seed(holiday_active=True, tank_holiday_active=True)

    class _Client:
        def get_daily_consumption_from_cache(self, devices=None):
            return {}

        def get_2hourly_consumption_from_cache(self, devices=None):
            return {ISO: {0: {"total_kwh": 0.0, "heating_kwh": 0.0, "dhw_kwh": 0.0},
                          1: {"total_kwh": 0.0, "heating_kwh": 0.0, "dhw_kwh": 0.0}}}

    fake_main = types.ModuleType("src.api.main")
    fake_main.get_daikin_client = lambda: _Client()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.api.main", fake_main)
    monkeypatch.setattr(
        "src.daikin.service.get_cached_devices",
        lambda **kw: CachedDevices(devices=[object()], fetched_at_wall=0.0, age_seconds=5.0,
                                   stale=False, source="cache"),
    )
    real_sync = svc.sync_daikin_2hourly_telemetry
    monkeypatch.setattr("src.daikin.service.sync_daikin_2hourly_telemetry",
                        lambda d: real_sync(DAY))

    runner.bulletproof_daikin_consumption_rollup_job(two_hourly_only=True)
    rows = _rows()
    assert rows[0]["source"] == "onecta_cache" and rows[1]["source"] == "onecta_cache"
    # Onecta missed nothing and the unit was off: a LATER integral run still leaves them.
    runner.bulletproof_daikin_consumption_rollup_job(two_hourly_only=True)
    assert all(r["source"] == "onecta_cache" for r in _rows().values())


def test_live_persist_stores_flags_and_rejects_non_bool():
    dev = SimpleNamespace(
        tank_temperature=45.0,
        temperature=SimpleNamespace(room_temperature=21.0, outdoor_temperature=5.0),
        tank_target=45.0, leaving_water_temperature=30.0, operation_mode="heating",
        weather_regulation_enabled=True, is_on=False, tank_on=None,
        holiday_active=True, tank_holiday_active=False)
    svc._persist_daikin_telemetry_live([dev])
    dev2 = SimpleNamespace(
        tank_temperature=45.0,
        temperature=SimpleNamespace(room_temperature=21.0, outdoor_temperature=5.0),
        tank_target=45.0, leaving_water_temperature=30.0, operation_mode="heating",
        weather_regulation_enabled=True, is_on="yes", tank_on=1,
        holiday_active=object(), tank_holiday_active=None)
    svc._persist_daikin_telemetry_live([dev2])
    with db._lock:
        c = db.get_connection()
        try:
            rs = c.execute(
                "SELECT is_on, tank_on, holiday_active, tank_holiday_active FROM daikin_telemetry ORDER BY id"
            ).fetchall()
        finally:
            c.close()
    assert tuple(rs[0]) == (0, None, 1, 0)
    assert tuple(rs[1]) == (None, None, None, None)


def test_daily_integral_off_day_writes_zero_row(monkeypatch):
    monkeypatch.setattr(svc, "heating_daily_kwh", lambda *a, **k: None)
    monkeypatch.setattr(svc, "should_block", lambda _v: False)
    _seed(holiday_active=True)
    row = svc.sync_daikin_daily(DAY)
    assert row is not None
    assert row["kwh_total"] == 0 and row["source"] == "telemetry_integral_off"


# ---- learners exclude unit-off / holiday days --------------------------------------
def _log_holiday(event: str, ts: datetime):
    db.log_action(device="daikin", action="holiday_mode",
                  params={"event": event, "climate": True, "tank": True},
                  result="ok", trigger="test")
    _sql("UPDATE action_log SET timestamp='%s' WHERE id=(SELECT MAX(id) FROM action_log)"
         % ts.strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_holiday_days_helper():
    from zoneinfo import ZoneInfo

    from src.analytics.thermal_learning import holiday_days
    _log_holiday("entered", datetime(2026, 10, 27, 9, 42, tzinfo=UTC))
    _log_holiday("exited", datetime(2026, 10, 29, 10, 0, tzinfo=UTC))
    got = holiday_days(date(2026, 10, 25), date(2026, 10, 31), ZoneInfo("UTC"))
    assert got == {date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)}


def test_decay_selector_skips_off_buckets_and_holiday_days():
    from zoneinfo import ZoneInfo

    from src.analytics.thermal_learning import _blocked_intervals
    tz = ZoneInfo("UTC")
    rows = [{"date": ISO, "bucket_idx": 1, "kwh_heating": 0.0, "kwh_dhw": 0.0,
             "source": "telemetry_integral_off"},
            {"date": ISO, "bucket_idx": 2, "kwh_heating": 0.0, "kwh_dhw": 0.0,
             "source": "onecta_cache"}]
    b = _blocked_intervals(rows, [], tz, heating_contam_kwh=0.1, dhw_contam_kwh=0.8, settle_hours=0.0)
    assert len(b) == 1 and b[0][0] == datetime(2026, 10, 28, 2, 0, tzinfo=UTC)


def test_lwt_learning_excludes_unit_off_rows_and_holiday_days():
    from zoneinfo import ZoneInfo

    from src.analytics import lwt_learning as ll
    tz = ZoneInfo("UTC")
    rows = [
        {"slot_time_utc": "2026-10-26T01:00:00Z", "heating_kwh_source": "onecta_cache"},
        {"slot_time_utc": "2026-10-26T01:30:00Z", "heating_kwh_source": "telemetry_integral_off"},
        {"slot_time_utc": "2026-10-28T01:00:00Z", "heating_kwh_source": "onecta_cache"},
    ]
    kept = ll.exclude_unit_off(rows, tz, days={date(2026, 10, 28)})
    assert [r["slot_time_utc"] for r in kept] == ["2026-10-26T01:00:00Z"]
