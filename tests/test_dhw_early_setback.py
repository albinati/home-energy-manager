"""Early tank setback on evening shower drawdown.

When the household's showers drain the tank inside the evening window, the
heartbeat detector persists ``dhw_early_setback_<date>`` and pulls the
setback forward so the firmware doesn't reheat the freshly-drawn tank at
peak price from the battery. K1 (generate_daily_tank_schedule) and K2
(forecast_dhw_load_per_slot) both honour the persisted key — lockstep.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src import dhw_policy, state_machine
from src.config import config

TZ_LOCAL = ZoneInfo("Europe/London")
ANCHOR = date(2026, 6, 1)
# 20:40 BST on the anchor date (= 19:40 UTC): inside the armed [20, 22) window.
NOW_UTC = datetime(2026, 6, 1, 19, 40, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """Pin dhw_policy's clock so the fixed anchor date is never 'in the past'."""

    @classmethod
    def now(cls, tz=None):  # noqa: D401
        base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
        return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(dhw_policy, "datetime", _FrozenDatetime, raising=False)
    _db.init_db()
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13, raising=False)
    monkeypatch.setattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_SETBACK_C", 37.0, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0, raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    monkeypatch.setattr(config, "DHW_FORECAST_AUTOSCALE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_BUCKET_BIAS_ENABLED", False, raising=False)
    # Runtime-settings-backed PROPERTIES: setattr would invoke the setter,
    # which leaves a PERMANENT entry in the class-level Config._overrides dict
    # (monkeypatch teardown re-invokes the setter with the old value — the key
    # stays). setitem patches the dict itself, so teardown REMOVES the key and
    # later test modules fall back to env/DB as they expect (this exact leak
    # made test_daikin_passive_mode fail when run after this module).
    monkeypatch.setitem(config._overrides, "DHW_TEMP_NORMAL_C", 45.0)
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "normal")
    monkeypatch.setitem(config._overrides, "DHW_WARMUP_PRICE_AWARE_ENABLED", "false")
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    # Detector knobs + hardware gates.
    monkeypatch.setattr(config, "DHW_EARLY_SETBACK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_EARLY_SETBACK_TRIGGER_DELTA_C", 4.0, raising=False)
    monkeypatch.setattr(config, "DHW_EARLY_SETBACK_ARM_HOUR_LOCAL", 20, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    yield


def _local(hour: int, minute: int = 0, d: date = ANCHOR) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ_LOCAL)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Persist / read
# ---------------------------------------------------------------------------


def test_persist_read_roundtrip():
    assert dhw_policy.read_early_setback(ANCHOR) is None
    ts = NOW_UTC
    assert dhw_policy.persist_early_setback(ANCHOR, ts) is True
    got = dhw_policy.read_early_setback(ANCHOR)
    assert got == ts
    assert got.tzinfo is not None


def test_persist_first_write_wins():
    assert dhw_policy.persist_early_setback(ANCHOR, NOW_UTC) is True
    later = NOW_UTC + timedelta(minutes=30)
    assert dhw_policy.persist_early_setback(ANCHOR, later) is False
    assert dhw_policy.read_early_setback(ANCHOR) == NOW_UTC


def test_read_malformed_value_is_none():
    _db.set_runtime_setting("dhw_early_setback_2026-06-01", "not-a-datetime")
    assert dhw_policy.read_early_setback(ANCHOR) is None
    # Naive datetime (no offset) is also treated as absent — fail-safe.
    _db.set_runtime_setting("dhw_early_setback_2026-06-01", "2026-06-01T19:40:00")
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_key_swept_with_warmup_keys():
    old = ANCHOR - timedelta(days=10)
    _db.set_runtime_setting(f"dhw_early_setback_{old.isoformat()}", _utc_iso(NOW_UTC))
    dhw_policy._sweep_stale_warmup_keys(ANCHOR)
    assert _db.get_runtime_setting(f"dhw_early_setback_{old.isoformat()}") is None


# ---------------------------------------------------------------------------
# Generator truncation (K1)
# ---------------------------------------------------------------------------


def test_generate_truncates_boundary_to_fire_time():
    fire = _local(20, 40).astimezone(UTC)
    dhw_policy.persist_early_setback(ANCHOR, fire)
    rows = dhw_policy.generate_daily_tank_schedule(ANCHOR, mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    setback = next(r for r in rows if r["action_type"] == "tank_setback")
    assert warmup["end_time"] == _utc_iso(fire)
    assert setback["start_time"] == _utc_iso(fire)
    # Setback still runs to the next day's warmup.
    end = datetime.fromisoformat(setback["end_time"].replace("Z", "+00:00"))
    assert end.astimezone(TZ_LOCAL).hour == 13
    assert end.astimezone(TZ_LOCAL).date() == ANCHOR + timedelta(days=1)


def test_generate_without_key_is_legacy():
    rows = dhw_policy.generate_daily_tank_schedule(ANCHOR, mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    assert (
        datetime.fromisoformat(warmup["end_time"].replace("Z", "+00:00"))
        .astimezone(TZ_LOCAL).hour == 22
    )


def test_generate_ignores_key_outside_cycle_window():
    # Before the warmup start → clamp rejects it (cannot invert the cycle).
    dhw_policy.persist_early_setback(ANCHOR, _local(9, 0).astimezone(UTC))
    rows = dhw_policy.generate_daily_tank_schedule(ANCHOR, mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    assert (
        datetime.fromisoformat(warmup["end_time"].replace("Z", "+00:00"))
        .astimezone(TZ_LOCAL).hour == 22
    )


def test_generate_guests_ignores_key():
    dhw_policy.persist_early_setback(ANCHOR, _local(20, 40).astimezone(UTC))
    rows = dhw_policy.generate_daily_tank_schedule(ANCHOR, mode="guests")
    assert [r["action_type"] for r in rows] == ["tank_warmup"]
    warmup = rows[0]
    end = datetime.fromisoformat(warmup["end_time"].replace("Z", "+00:00"))
    assert end.astimezone(TZ_LOCAL).date() == ANCHOR + timedelta(days=1)


def test_build_early_setback_row_matches_generated_key():
    """The detector's immediate row and the regenerated schedule must share
    the upsert natural key (device, action_type, start_time)."""
    fire = _local(20, 40).astimezone(UTC)
    dhw_policy.persist_early_setback(ANCHOR, fire)
    immediate = dhw_policy.build_early_setback_row(ANCHOR, fire)
    rows = dhw_policy.generate_daily_tank_schedule(ANCHOR, mode="normal")
    regenerated = next(r for r in rows if r["action_type"] == "tank_setback")
    assert immediate["start_time"] == regenerated["start_time"]
    assert immediate["end_time"] == regenerated["end_time"]
    assert immediate["params"] == regenerated["params"]
    assert immediate["params"]["tank_temp"] == 37


# ---------------------------------------------------------------------------
# Forecast lockstep (K2)
# ---------------------------------------------------------------------------


def _evening_slots(d: date = ANCHOR) -> list[datetime]:
    """30-min slot starts 18:00 local → 13:00 local next day."""
    start = _local(18, 0, d).astimezone(UTC)
    return [start + timedelta(minutes=30 * i) for i in range(39)]


def test_forecast_pins_setback_after_fire_time():
    fire = _local(20, 40).astimezone(UTC)
    dhw_policy.persist_early_setback(ANCHOR, fire)
    slots = _evening_slots()
    e_dhw, tank_temps = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    setback_kwh = dhw_policy._SETBACK_MAINTENANCE_KWH
    for i, s in enumerate(slots):
        local = s.astimezone(TZ_LOCAL)
        if s >= fire and local.date() == ANCHOR:
            # 21:00 + 21:30 are shower-window slots — the override must beat
            # the shower phase: those slots go to setback level too.
            assert e_dhw[i] == pytest.approx(setback_kwh), local
            assert tank_temps[i] == pytest.approx(37.0), local
    # A shower slot BEFORE the fire keeps the shower budget (20:00→20:30).
    idx_2000 = next(
        i for i, s in enumerate(slots)
        if s.astimezone(TZ_LOCAL).hour == 20 and s.astimezone(TZ_LOCAL).minute == 0
    )
    assert e_dhw[idx_2000] == pytest.approx(dhw_policy._SHOWER_REHEAT_KWH)
    assert tank_temps[idx_2000] == pytest.approx(45.0)


def test_forecast_next_day_unaffected():
    fire = _local(20, 40).astimezone(UTC)
    dhw_policy.persist_early_setback(ANCHOR, fire)
    tomorrow = ANCHOR + timedelta(days=1)
    slots = _evening_slots(tomorrow)
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    idx_2100 = next(
        i for i, s in enumerate(slots)
        if s.astimezone(TZ_LOCAL).hour == 21 and s.astimezone(TZ_LOCAL).minute == 0
    )
    assert e_dhw[idx_2100] == pytest.approx(dhw_policy._SHOWER_REHEAT_KWH)


def test_forecast_without_key_keeps_shower_budget():
    slots = _evening_slots()
    e_dhw, _ = dhw_policy.forecast_dhw_load_per_slot(slots, mode="normal")
    idx_2100 = next(
        i for i, s in enumerate(slots)
        if s.astimezone(TZ_LOCAL).hour == 21 and s.astimezone(TZ_LOCAL).minute == 0
    )
    assert e_dhw[idx_2100] == pytest.approx(dhw_policy._SHOWER_REHEAT_KWH)


def test_forecast_guests_ignores_key():
    fire = _local(20, 40).astimezone(UTC)
    dhw_policy.persist_early_setback(ANCHOR, fire)
    slots = _evening_slots()
    e_dhw, tank_temps = dhw_policy.forecast_dhw_load_per_slot(slots, mode="guests")
    idx_2100 = next(
        i for i, s in enumerate(slots)
        if s.astimezone(TZ_LOCAL).hour == 21 and s.astimezone(TZ_LOCAL).minute == 0
    )
    assert e_dhw[idx_2100] == pytest.approx(dhw_policy._SHOWER_REHEAT_KWH)
    assert tank_temps[idx_2100] == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# Detector (heartbeat check)
# ---------------------------------------------------------------------------


def _mk_dev(tank_temp=38.0, tank_target=45.0):
    return SimpleNamespace(
        tank_temperature=tank_temp, tank_target=tank_target, tank_on=True,
    )


def _seed_warmup_row() -> int:
    """A dhw_policy warmup row covering NOW_UTC, as the plan writer creates it."""
    return _db.upsert_action(
        plan_date=str(ANCHOR),
        start_time=_utc_iso(_local(13, 0)),
        end_time=_utc_iso(_local(22, 0)),
        device="daikin",
        action_type="tank_warmup",
        params={"tank_power": True, "tank_temp": 45, "tank_powerful": False,
                "dhw_policy": True},
        status="active",
    )


def _action_row(aid: int) -> dict:
    rows = _db.get_actions_for_plan_date(str(ANCHOR))
    return next(r for r in rows if int(r["id"]) == aid)


def _seed_telemetry(temps: list[float], end_utc: datetime = NOW_UTC) -> None:
    """Insert samples 5 min apart ending just before *end_utc*."""
    for i, t in enumerate(reversed(temps)):
        _db.insert_daikin_telemetry({
            "fetched_at": (end_utc - timedelta(minutes=5 * (i + 1))).timestamp(),
            "source": "live",
            "tank_temp_c": t,
        })


def _run_detector(dev, actions):
    state_machine._check_dhw_shower_drawdown(
        actions, dev, NOW_UTC, trigger="test",
    )


def test_detector_fires_on_drawdown():
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 44.0, 38.5, 38.0])
    dev = _mk_dev(tank_temp=38.0)
    _run_detector(dev, [_action_row(aid)])

    fired = dhw_policy.read_early_setback(ANCHOR)
    assert fired == NOW_UTC
    # Warmup row completed so the reconciler stops asserting NORMAL.
    assert _action_row(aid)["status"] == "completed"
    # Pulled-forward setback row exists, pending, starting at the fire time.
    rows = _db.get_actions_for_plan_date(str(ANCHOR))
    setbacks = [r for r in rows if r["action_type"] == "tank_setback"]
    assert any(
        r["start_time"] == _utc_iso(NOW_UTC) and r["status"] == "pending"
        for r in setbacks
    )
    sb = next(r for r in setbacks if r["start_time"] == _utc_iso(NOW_UTC))
    params = json.loads(sb["params"]) if isinstance(sb["params"], str) else sb["params"]
    assert params["tank_temp"] == 37


def test_detector_idempotent_second_tick():
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    dev = _mk_dev(tank_temp=38.0)
    _run_detector(dev, [_action_row(aid)])
    n_before = len(_db.get_actions_for_plan_date(str(ANCHOR)))
    # Second tick: key already persisted → no new rows, no crash.
    _run_detector(dev, [_action_row(aid)])
    assert len(_db.get_actions_for_plan_date(str(ANCHOR))) == n_before


def test_detector_respects_kill_switch(monkeypatch):
    monkeypatch.setattr(config, "DHW_EARLY_SETBACK_ENABLED", False, raising=False)
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    _run_detector(_mk_dev(), [_action_row(aid)])
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_skips_guests_mode(monkeypatch):
    monkeypatch.setitem(config._overrides, "OPTIMIZATION_PRESET", "guests")
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    _run_detector(_mk_dev(), [_action_row(aid)])
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_skips_read_only(monkeypatch):
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True, raising=False)
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    _run_detector(_mk_dev(), [_action_row(aid)])
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_outside_armed_window():
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 38.5, 38.0], end_utc=_local(19, 0).astimezone(UTC))
    early = _local(19, 0).astimezone(UTC)  # 19:00 local < arm hour 20
    state_machine._check_dhw_shower_drawdown(
        [_action_row(aid)], _mk_dev(), early, trigger="test",
    )
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_requires_covering_warmup_row():
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    _run_detector(_mk_dev(), [])  # no rows at all
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_skips_when_boost_overlaps_evening():
    aid = _seed_warmup_row()
    _db.upsert_action(
        plan_date=str(ANCHOR),
        start_time=_utc_iso(_local(21, 0)),
        end_time=_utc_iso(_local(22, 0)),
        device="daikin",
        action_type="tank_negative_boost",
        params={"tank_power": True, "tank_temp": 60, "tank_powerful": True,
                "dhw_policy": True},
        status="pending",
    )
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    actions = _db.get_actions_for_plan_date(str(ANCHOR))
    _run_detector(_mk_dev(), actions)
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_below_threshold_no_fire():
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 43.5, 43.0])  # only 2 °C drop
    _run_detector(_mk_dev(tank_temp=43.0), [_action_row(aid)])
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_single_glitch_sample_no_fire():
    aid = _seed_warmup_row()
    # Newest sample low but the one before is still hot → not confirmed.
    _seed_telemetry([45.0, 45.0, 44.5, 38.0])
    _run_detector(_mk_dev(tank_temp=38.0), [_action_row(aid)])
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_live_reading_recovered_no_fire():
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    # Telemetry looks like a drawdown but the LIVE reading is hot again
    # (reheat nearly done / sensor recovered) → don't fire.
    _run_detector(_mk_dev(tank_temp=44.5), [_action_row(aid)])
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_diverged_live_target_no_fire():
    aid = _seed_warmup_row()
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    # User hand-set the target to 50 via the app → leave the tank alone.
    _run_detector(_mk_dev(tank_temp=38.0, tank_target=50.0), [_action_row(aid)])
    assert dhw_policy.read_early_setback(ANCHOR) is None


def test_detector_overridden_warmup_row_no_fire():
    aid = _seed_warmup_row()
    _db.mark_action_user_overridden(aid)
    _seed_telemetry([45.0, 45.0, 38.5, 38.0])
    _run_detector(_mk_dev(tank_temp=38.0), [_action_row(aid)])
    assert dhw_policy.read_early_setback(ANCHOR) is None
