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
    assert target["lwt_offset"] == 3  # cold + cheap → +BOOST
    # Radiator setpoint = weather-curve base (at 5 °C) + offset; base in [18,50].
    assert target["lwt_base_c"] is not None and 18.0 <= target["lwt_base_c"] <= 50.0
    assert target["lwt_setpoint_c"] == round(min(50.0, target["lwt_base_c"] + 3), 1)
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
    assert target["lwt_offset"] is None
    assert target["outdoor_c"] == 5.0
    assert target["heating_on"] is True
