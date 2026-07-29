"""Tests for the #767 morning-comfort guard: measured overnight drop, the
draw-aware deadband projection, and the sensor-silence detector.

The load-bearing idea is that the DECLARED draw model and the MEASURED transfer
disagree by 3x, and building a comfort guard on the wrong one is expensive in
opposite directions:

* declared 3 evening showers => 3.4 kWh thermal => **15.3 °C** on this cylinder;
* measured over 28 non-boost nights: median **5**, p75 6, p90 **10**, max 17.

Sizing a nightly lift against 15.3 buys heat on nine nights in ten to cover the
tenth. Sizing it against a draw-free coast (what the code did before) misses the
tenth entirely — which is the 2026-07-28 cold shower.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.dhw import calibration as cal
from src.dhw.params import resolve_overnight_drop_c

UTC = dt.timezone.utc
TZ = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    db.init_db()
    import src.state_machine as sm
    sm._SENSOR_SILENT_NOTIFIED = set()
    yield


def _night_rows(nights: list[tuple[float, float]], *, boost_on: set[int] | None = None):
    """(fetched_at, tank, target) with one setback reading and one morning
    reading per night. ``nights`` is [(tank_at_setback, tank_at_morning), ...]."""
    boost_on = boost_on or set()
    rows: list[tuple[float, float, float]] = []
    base = dt.date(2026, 6, 1)
    for i, (a, b) in enumerate(nights):
        day = base + dt.timedelta(days=i)
        tgt = 60.0 if i in boost_on else 37.0
        rows.append((dt.datetime.combine(day, dt.time(15, 30), UTC).timestamp(), a, tgt))
        rows.append((
            dt.datetime.combine(day + dt.timedelta(days=1), dt.time(6, 0), UTC).timestamp(),
            b, tgt,
        ))
    return rows


# ---------------------------------------------------------------------------
# The measured overnight drop
# ---------------------------------------------------------------------------


def test_overnight_drop_reports_the_tail_not_the_centre():
    """A comfort floor is a question about the tail. Ten quiet nights and two
    heavy ones must not average into 'fine'."""
    nights = [(47.0, 42.0)] * 10 + [(47.0, 30.0), (47.0, 33.0)]  # drops: 5 x10, 17, 14
    out = cal.fit_overnight_drop(_night_rows(nights), tz=TZ)

    assert out["status"] == "ok"
    assert out["nights"] == 12
    assert out["p50_c"] == pytest.approx(5.0)
    assert out["max_c"] == pytest.approx(17.0)
    assert out["p90_c"] > out["p50_c"], "the tail must be visible above the median"


def test_overnight_drop_excludes_boost_nights():
    """A `tank_negative_boost` to 60 is not a representative starting point, and
    its recovery shows up as a NEGATIVE drop that would flatter the tail."""
    nights = [(47.0, 42.0)] * 9 + [(41.0, 51.0)]  # last one gains 10 C
    out = cal.fit_overnight_drop(_night_rows(nights, boost_on={9}), tz=TZ)

    assert out["status"] == "ok"
    assert out["nights"] == 9
    assert out["min_c"] >= 0, "a boost night leaked into the distribution"


def test_overnight_drop_skips_below_the_gate():
    out = cal.fit_overnight_drop(_night_rows([(47.0, 42.0)] * 3), tz=TZ)
    assert out["status"] == "skipped"
    assert "night" in out["reason"]


def test_drop_reader_falls_back_to_the_measured_p90_not_the_declared_model():
    """The fallback is 10 °C — the p90 actually observed on this tank — and
    explicitly NOT the 15.3 °C the three-declared-showers model implies."""
    assert resolve_overnight_drop_c() == pytest.approx(10.0)


def test_drop_reader_refuses_a_negative_learned_value():
    """A comfort guard may never assume the tank GAINS heat overnight."""
    db.upsert_dhw_calibration(
        "overnight_drop", status="ok",
        payload={"p90_c": -3.0, "nights": 20}, n_samples=20, window_days=45,
    )
    assert resolve_overnight_drop_c() == pytest.approx(10.0)


def test_drop_reader_uses_a_fresh_learned_value():
    db.upsert_dhw_calibration(
        "overnight_drop", status="ok",
        payload={"p90_c": 7.5, "p50_c": 4.0, "nights": 30}, n_samples=30, window_days=45,
    )
    assert resolve_overnight_drop_c() == pytest.approx(7.5)
    assert resolve_overnight_drop_c("p50_c") == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# The draw-aware deadband projection
# ---------------------------------------------------------------------------


def test_crosses_evening_draw_only_when_the_draw_is_actually_in_between():
    from src.state_machine import _crosses_evening_draw

    noon = dt.datetime(2026, 7, 17, 13, 5)  # BST wall clock
    assert not _crosses_evening_draw(noon, 2.0, "normal"), "13:05 -> 15:05 has no draw"
    assert _crosses_evening_draw(noon, 20.0, "normal"), "13:05 -> 09:05 crosses 20:00"
    # Projecting TO the evening window: that window's own draw is not yet spent.
    evening_entry = dt.datetime(2026, 7, 17, 19, 5)
    assert not _crosses_evening_draw(evening_entry, 55 / 60, "normal")


def test_the_2026_07_28_cold_shower_would_have_been_caught(monkeypatch):
    """The real failure: warmup fires at 13:05 with the tank at 44 and a target
    of 47 — inside the deadband, so the firmware will not heat unaided. The
    draw-free projection said the morning would still clear its floor; the
    measured transfer says it lands ~34 and it did (31 °C, observed)."""
    from src.state_machine import _warmup_deadband_force_reason

    fire = dt.datetime(2026, 7, 28, 12, 5, tzinfo=UTC)  # 13:05 BST
    dev = SimpleNamespace(tank_temperature=44.0, tank_target=None)

    r = _warmup_deadband_force_reason(dev, {"tank_temp": 47, "tank_power": True}, fire)

    assert r is not None, "the guard must fire — this is the incident"
    assert r["window"] == "morning_reserve"
    assert r["projection_basis"].startswith("measured_overnight_drop")
    assert r["projected_c"] == pytest.approx(34.0, abs=0.1)  # 44 - p90 10

    # And here is the honest price of the morning floor on THIS night. The
    # cliff is 50 and the tank is 44, so `cliff - tank = 6.0` does not clear
    # `differential (6.0) + guard (0.5)` — the heat pump cannot be triggered at
    # all from inside the deadband, and Powerful (COP ~1 resistance) is the only
    # mechanism that adds heat. The guard is right to fire, and it is right that
    # this costs more than a heat-pump lift would: protecting a 44 °C tank
    # through three showers is simply not free on this cylinder.
    assert r["mechanism"] == "powerful"


def test_a_warm_enough_tank_still_gets_the_free_skip(monkeypatch):
    """The guard must not fire on every night — that is the failure mode of
    building it on the declared 15.3 °C draw. A tank at 47 clears a 37 floor
    against the measured p90 of 10."""
    from src.state_machine import _warmup_deadband_force_reason

    monkeypatch.setattr("src.dhw.comfort._setting", lambda key, default: (
        37.0 if key == "DHW_MORNING_RESERVE_C" else default))
    fire = dt.datetime(2026, 7, 28, 12, 5, tzinfo=UTC)
    dev = SimpleNamespace(tank_temperature=47.0, tank_target=None)

    r = _warmup_deadband_force_reason(dev, {"tank_temp": 47.5, "tank_power": True}, fire)

    assert r is None, "47 - 10 = 37 meets a 37 floor; no heat should be bought"


# ---------------------------------------------------------------------------
# Sensor silence
# ---------------------------------------------------------------------------


def _seed_device(device_key: str, room: str, received_at: dt.datetime) -> None:
    conn = db.get_connection()
    try:
        iso = received_at.isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO device_reading_log (device_key, dedup_key, room, received_at, "
            "captured_at, source, temp_c, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (device_key, f"{device_key}|{iso}", room, iso, iso, "esphome", 21.0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_sensor_silence_pages_once_per_episode(monkeypatch):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    _seed_device("AA:BB", "cozinha", now - dt.timedelta(hours=8))
    _seed_device("CC:DD", "corredor", now - dt.timedelta(hours=8))
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")
    sm._check_sensor_silence(now + dt.timedelta(minutes=10), trigger="hb")

    assert notify.call_count == 1, "a sustained outage pages once, not every tick"
    devices = notify.call_args.kwargs["devices"]
    assert {d["room"] for d in devices} == {"cozinha", "corredor"}
    assert all(d["silent_min"] >= 480 for d in devices)


def test_sensor_silence_is_quiet_while_readings_flow(monkeypatch):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    _seed_device("AA:BB", "cozinha", now - dt.timedelta(minutes=8))
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")

    notify.assert_not_called()


def test_sensor_silence_repages_after_recovery(monkeypatch):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    _seed_device("AA:BB", "cozinha", now - dt.timedelta(hours=8))
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")
    assert notify.call_count == 1

    # It reports again — the token must clear...
    _seed_device("AA:BB", "cozinha", now + dt.timedelta(minutes=1))
    sm._check_sensor_silence(now + dt.timedelta(minutes=2), trigger="hb")
    assert notify.call_count == 1

    # ...so a FRESH outage is news again.
    sm._check_sensor_silence(now + dt.timedelta(hours=4), trigger="hb")
    assert notify.call_count == 2


def test_a_retired_device_does_not_page_forever(monkeypatch):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    _seed_device("OLD:01", "quarto", now - dt.timedelta(days=30))
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")

    notify.assert_not_called()


def test_sensor_silence_judges_server_time_not_the_device_clock(monkeypatch):
    """A unit whose clock drifted could claim a fresh `captured_at` while
    sending nothing — exactly what this detector exists to catch (#700)."""
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    conn = db.get_connection()
    try:
        srv = (now - dt.timedelta(hours=8)).isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO device_reading_log (device_key, dedup_key, room, received_at, "
            "captured_at, source, temp_c, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            ("AA:BB", f"AA:BB|{srv}", "cozinha",
             srv,                                                                # server
             (now + dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),   # device lies
             "esphome", 21.0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")

    notify.assert_called_once()
