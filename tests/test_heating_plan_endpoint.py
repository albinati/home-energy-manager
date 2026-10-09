"""GET /api/v1/daikin/heating-plan — deterministic per-slot heating timeline
across yesterday/today/tomorrow (#481 follow-up). Recomputes outdoor temp +
price tier + LWT offset + heating-on + tank target per slot; no dependence on
the (overlapping) action_schedule rows."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import config


def _tz():
    return ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))


def _seed_meteo(conn, slot_time_z: str, temp_c: float):
    conn.execute(
        "INSERT INTO meteo_forecast (forecast_date, slot_time, temp_c, solar_w_m2, cloud_cover_pct) "
        "VALUES (?, ?, ?, 0, 50)",
        (slot_time_z[:10], slot_time_z, temp_c),
    )


def _seed_rate(conn, tariff: str, vf_z: str, vt_z: str, p: float):
    conn.execute(
        "INSERT INTO agile_rates (tariff_code, valid_from, valid_to, value_inc_vat, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (tariff, vf_z, vt_z, p, vf_z),
    )


def test_heating_plan_cold_cheap_slot_boosts(monkeypatch):
    import src.db as db
    from src.api import main
    import asyncio

    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_BOOST_C", 3, raising=False)
    monkeypatch.setattr(config, "DAIKIN_WEATHER_CURVE_HIGH_C", 18.0, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_CHEAP_THRESHOLD_PENCE", 12.0, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PEAK_THRESHOLD_PENCE", 25.0, raising=False)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE", raising=False)
    # Disable smoothing so a single seeded slot's offset survives (smoothing has
    # its own tests); this checks the per-slot offset + curve-setpoint math.
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 1, raising=False)

    # A cold (5 °C) + cheap (5p) slot at 10:00 UTC today → boost +3.
    today = datetime.now(_tz()).date()
    slot = datetime(today.year, today.month, today.day, 10, 0, tzinfo=UTC)
    slot_z = slot.isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_meteo(conn, slot_z, 5.0)
            _seed_rate(conn, "E-1R-AGILE", slot_z,
                       (slot + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"), 5.0)
            conn.commit()
        finally:
            conn.close()

        resp = asyncio.run(main.daikin_heating_plan())

    assert resp["enabled"] is True
    assert len(resp["days"]) == 3
    assert [d["label"] for d in resp["days"]] == ["Yesterday", "Today", "Tomorrow"]
    # 3 days × 48 half-hour slots.
    assert len(resp["slots"]) == 144

    target = next((s for s in resp["slots"] if s["slot_utc"] == slot_z), None)
    assert target is not None, "expected the seeded 10:00Z slot"
    assert target["outdoor_c"] == 5.0
    assert target["price_p"] == 5.0
    assert target["tier"] == "cheap"
    assert target["heating_on"] is True
    assert target["lwt_offset_tier"] == 3  # cold + cheap → +BOOST (rule ghost)
    assert target["lwt_offset"] == 0 and target["offset_source"] == "none"  # nothing scheduled
    # Radiator setpoint = weather-curve base (at 5 °C) + offset; base in [18,50].
    assert target["lwt_base_c"] is not None and 18.0 <= target["lwt_base_c"] <= 50.0
    assert target["lwt_setpoint_c"] == round(min(50.0, target["lwt_base_c"] + 0), 1)  # written offset = 0
    # A tank target/kind is resolved for the slot (dhw_policy, allow_past).
    assert target["tank_kind"] in ("warmup", "setback", "boost")


def test_heating_plan_negative_slot_surfaces_boost(monkeypatch):
    # A negative-price slot inside today's tank cycle must render tank_kind
    # "boost" (60 °C), NOT the setback it sits inside. Regression for the
    # _tank_at masking bug (full-span setback row matched before the boost
    # sub-interval).
    import src.db as db
    from src.api import main
    import asyncio

    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)

    # 14:00 UTC today is inside today's warmup→next-warmup cycle.
    today = datetime.now(_tz()).date()
    slot = datetime(today.year, today.month, today.day, 14, 0, tzinfo=UTC)
    slot_z = slot.isoformat().replace("+00:00", "Z")
    end_z = (slot + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_meteo(conn, slot_z, 5.0)
            _seed_rate(conn, "E-1R-AGILE", slot_z, end_z, -5.0)  # paid to import
            conn.commit()
        finally:
            conn.close()
        resp = asyncio.run(main.daikin_heating_plan())

    target = next((s for s in resp["slots"] if s["slot_utc"] == slot_z), None)
    assert target is not None
    assert target["tier"] == "negative"
    assert target["tank_kind"] == "boost", f"boost masked by setback: {target}"
    expected = int(round(min(float(config.DHW_NEGATIVE_PRICE_BOOST_C), float(config.DHW_TEMP_MAX_C))))
    assert target["tank_temp_c"] == expected


def test_heating_plan_disabled_no_offset(monkeypatch):
    import src.db as db
    from src.api import main
    import asyncio

    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE", raising=False)
    today = datetime.now(_tz()).date()
    slot = datetime(today.year, today.month, today.day, 10, 0, tzinfo=UTC)
    slot_z = slot.isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_meteo(conn, slot_z, 5.0)
            _seed_rate(conn, "E-1R-AGILE", slot_z,
                       (slot + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"), 5.0)
            conn.commit()
        finally:
            conn.close()
        resp = asyncio.run(main.daikin_heating_plan())

    assert resp["enabled"] is False
    target = next((s for s in resp["slots"] if s["slot_utc"] == slot_z), None)
    assert target is not None
    # Feature off → no offset, but the rest of the timeline still renders.
    assert target["lwt_offset_tier"] is None
    assert target["outdoor_c"] == 5.0
    assert target["heating_on"] is True


# ---------------------------------------------------------------- #845 written offsets
def _insert_action(conn, *, date, start, end, action_type, offset, status="pending", created="2026-10-09T10:00:00+00:00"):
    import json
    conn.execute(
        "INSERT INTO action_schedule (date, start_time, end_time, device, action_type, params, status, created_at) "
        "VALUES (?, ?, ?, 'daikin', ?, ?, ?, ?)",
        (date, start, end, action_type, json.dumps({"lwt_offset": offset, "lp_optimizer": True}), status, created),
    )


def _run_plan(monkeypatch, seed):
    import asyncio
    import src.db as db
    from src.api import main

    monkeypatch.setattr(config, "DAIKIN_LWT_SOURCE", "lp", raising=False)
    monkeypatch.setattr(config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE", raising=False)
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            seed(conn)
            conn.commit()
        finally:
            conn.close()
        return asyncio.run(main.daikin_heating_plan())


def _slot(resp, dt):
    z = dt.isoformat().replace("+00:00", "Z")
    return next(s for s in resp["slots"] if s["slot_utc"] == z)


def test_heating_plan_written_offsets_from_schedule(monkeypatch):
    tmr = datetime.now(_tz()).date() + timedelta(days=1)
    d = tmr.isoformat()
    at = lambda h, m=0: datetime(tmr.year, tmr.month, tmr.day, h, m, tzinfo=UTC)  # noqa: E731
    z = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731

    def seed(conn):
        _insert_action(conn, date=d, start=z(at(0)), end=z(at(3, 30)), action_type="lwt_preheat", offset=-1)
        _insert_action(conn, date=d, start=z(at(3, 30)), end=z(at(3, 35)), action_type="restore", offset=0)
        _insert_action(conn, date=d, start=z(at(4)), end=z(at(6)), action_type="lwt_preheat", offset=10)
        _insert_action(conn, date=d, start=z(at(6)), end=z(at(9, 30)), action_type="lwt_preheat", offset=-2)

    resp = _run_plan(monkeypatch, seed)
    assert resp["lwt_source"] == "lp" and "coast_mode" in resp
    s = _slot(resp, at(4))
    assert s["lwt_offset"] == 10 and s["offset_source"] == "schedule"
    assert _slot(resp, at(7))["lwt_offset"] == -2
    assert _slot(resp, at(1))["lwt_offset"] == -1
    assert _slot(resp, at(3, 30))["lwt_offset"] == 0  # restore row
    gap = _slot(resp, at(18))
    assert gap["lwt_offset"] == 0 and gap["offset_source"] == "none"
    assert "lwt_offset_tier" in gap


def test_heating_plan_overlapping_rows_active_then_newest_wins(monkeypatch):
    tmr = datetime.now(_tz()).date() + timedelta(days=1)
    d = tmr.isoformat()
    at = lambda h, m=0: datetime(tmr.year, tmr.month, tmr.day, h, m, tzinfo=UTC)  # noqa: E731
    z = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731

    def seed(conn):
        _insert_action(conn, date=d, start=z(at(16)), end=z(at(19)), action_type="lwt_preheat", offset=-2,
                       status="pending", created="2026-10-09T10:00:00+00:00")
        _insert_action(conn, date=d, start=z(at(16, 30)), end=z(at(19)), action_type="lwt_preheat", offset=-3,
                       status="pending", created="2026-10-09T12:00:00+00:00")
        _insert_action(conn, date=d, start=z(at(20)), end=z(at(22)), action_type="lwt_preheat", offset=4,
                       status="pending", created="2026-10-09T13:00:00+00:00")
        _insert_action(conn, date=d, start=z(at(20)), end=z(at(22)), action_type="lwt_preheat", offset=2,
                       status="active", created="2026-10-09T09:00:00+00:00")

    resp = _run_plan(monkeypatch, seed)
    assert _slot(resp, at(16))["lwt_offset"] == -2      # only the older row covers 16:00
    assert _slot(resp, at(17))["lwt_offset"] == -3      # newest wins in the overlap
    assert _slot(resp, at(20))["lwt_offset"] == 2       # active beats newer pending


def test_heating_plan_past_slot_uses_device_offset(monkeypatch):
    yday = datetime.now(_tz()).date() - timedelta(days=1)
    slot = datetime(yday.year, yday.month, yday.day, 10, 0, tzinfo=UTC)

    def seed(conn):
        conn.execute(
            "INSERT INTO execution_log (timestamp, daikin_lwt_offset) VALUES (?, ?)",
            ((slot + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"), 5.0),
        )
        _insert_action(conn, date=yday.isoformat(), start=slot.isoformat().replace("+00:00", "Z"),
                       end=(slot + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                       action_type="lwt_preheat", offset=1, status="completed")

    resp = _run_plan(monkeypatch, seed)
    s = _slot(resp, slot)
    assert s["lwt_offset"] == 5 and s["offset_source"] == "device"
    s2 = _slot(resp, slot + timedelta(minutes=30))  # no device sample -> schedule row
    assert s2["lwt_offset"] == 1 and s2["offset_source"] == "schedule"
