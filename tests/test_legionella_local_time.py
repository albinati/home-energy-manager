"""DHW_LEGIONELLA_STANDOFF_START_LOCAL -- the stand-off on the unit's LOCAL clock."""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

import pytest

from src import db, dhw_policy, legionella_window
from src.config import config
from src.state_machine import in_legionella_standoff

BST_SUN = date(2026, 10, 18)  # BST until 2026-10-25
GMT_SUN = date(2026, 11, 1)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    legionella_window._WARNED_BAD_LOCAL.clear()
    for k, v in {
        "BULLETPROOF_TIMEZONE": "Europe/London",
        "DHW_LEGIONELLA_STANDOFF_ENABLED": True,
        "DHW_LEGIONELLA_STANDOFF_DOW": 6,
        "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC": 11,
        "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC": 0,
        "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES": 150,
        "DHW_LEGIONELLA_STANDOFF_START_LOCAL": "12:30",
        "DHW_LEGIONELLA_BUDGET_ENABLED": True,
        "DHW_LEGIONELLA_BUDGET_KWH": 3.5,
        "DAIKIN_MAX_HP_KW": 4.0,
        "DHW_FORECAST_AUTOSCALE_ENABLED": False,
        "DHW_BUCKET_BIAS_ENABLED": False,
    }.items():
        monkeypatch.setattr(config, k, v, raising=False)
    dhw_policy._autoscale_cache.clear()
    yield
    dhw_policy._autoscale_cache.clear()


def _u(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_window_bst_and_gmt():
    assert legionella_window.standoff_window_for_date(BST_SUN) == (
        _u(2026, 10, 18, 11, 30), _u(2026, 10, 18, 14, 0))
    assert legionella_window.standoff_window_for_date(GMT_SUN) == (
        _u(2026, 11, 1, 12, 30), _u(2026, 11, 1, 15, 0))


def test_predicate_bst_gmt_edges():
    assert in_legionella_standoff(_u(2026, 10, 18, 11, 30))
    assert not in_legionella_standoff(_u(2026, 10, 18, 11, 29))
    assert in_legionella_standoff(_u(2026, 10, 18, 13, 59))
    assert not in_legionella_standoff(_u(2026, 10, 18, 14, 0))
    assert in_legionella_standoff(_u(2026, 11, 1, 12, 30))
    assert not in_legionella_standoff(_u(2026, 11, 1, 11, 30))  # BST-style hour is outside in GMT
    assert not in_legionella_standoff(_u(2026, 11, 1, 15, 0))


def test_dow_is_local_date(monkeypatch):
    # Sat 23:30Z in BST is already Sunday 00:30 local.
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_LOCAL", "00:15")
    assert in_legionella_standoff(_u(2026, 10, 17, 23, 30))      # Sun 00:30 BST
    assert not in_legionella_standoff(_u(2026, 10, 18, 23, 30))  # Mon 00:30 BST
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_LOCAL", "12:30")
    assert not in_legionella_standoff(_u(2026, 10, 19, 11, 45))  # Monday
    assert legionella_window.standoff_window_for_date(date(2026, 10, 19)) is None


def test_disabled_flag(monkeypatch):
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", False)
    assert not in_legionella_standoff(_u(2026, 10, 18, 12, 0))


@pytest.mark.parametrize(
    "dow,h,m,dur", [(6, 11, 0, 120), (6, 11, 30, 240), (2, 5, 15, 45), (0, 0, 0, 30)]
)
def test_empty_reproduces_old_utc_formula(monkeypatch, dow, h, m, dur):
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_LOCAL", "")
    for k, v in (("DOW", dow), ("START_HOUR_UTC", h), ("START_MINUTE_UTC", m),
                 ("DURATION_MINUTES", dur)):
        monkeypatch.setattr(config, f"DHW_LEGIONELLA_STANDOFF_{k}", v)

    def old(now):  # the pre-change formula
        if now.weekday() != dow:
            return False
        s = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return s <= now < s + timedelta(minutes=dur)

    t = _u(2026, 10, 11, 0)
    while t < _u(2026, 10, 25, 0):
        assert in_legionella_standoff(t) == old(t), t
        t += timedelta(minutes=15)


@pytest.mark.parametrize("bad", ["25:00", "12:75", "noon", "1230", "12:5"])
def test_invalid_falls_back_and_warns_once(monkeypatch, caplog, bad):
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_LOCAL", bad)
    with caplog.at_level(logging.WARNING, logger="src.legionella_window"):
        assert in_legionella_standoff(_u(2026, 10, 18, 11, 0))   # UTC knobs: 11:00 + 150
        assert not in_legionella_standoff(_u(2026, 10, 18, 10, 59))
        assert in_legionella_standoff(_u(2026, 10, 18, 13, 29))
        assert not in_legionella_standoff(_u(2026, 10, 18, 13, 30))
    assert sum("START_LOCAL" in r.getMessage() for r in caplog.records) == 1


def test_standoff_windows_range():
    ws = legionella_window.standoff_windows(date(2026, 10, 12), date(2026, 11, 2))
    assert [w[0] for w in ws] == [
        _u(2026, 10, 18, 11, 30), _u(2026, 10, 25, 12, 30), _u(2026, 11, 1, 12, 30)]


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(db, "_db_path", lambda: path)
    db.init_db()


def _day_slots(day):
    start = datetime.combine(day, time(0, 0), tzinfo=UTC)
    return [start + timedelta(minutes=30 * i) for i in range(48)]


@pytest.mark.parametrize("day,first_slot", [(BST_SUN, 23), (GMT_SUN, 25)])  # 11:30Z / 12:30Z
def test_budget_lands_in_local_window_slots(tmp_db, day, first_slot):
    e, _ = dhw_policy.forecast_dhw_load_per_slot(_day_slots(day), mode="vacation")
    idx = list(range(first_slot, first_slot + 5))  # 150 min = 5 slots
    for i in idx:
        assert e[i] == pytest.approx(3.5 / 5)
    assert sum(e[i] for i in range(48) if i not in idx) == pytest.approx(0.0)


def test_budget_by_slot_helper_follows_local():
    from src.dhw.baseline import legionella_budget_by_slot

    out = legionella_budget_by_slot(_day_slots(BST_SUN), budget_kwh=5.0)
    assert [i for i, v in enumerate(out) if v] == [23, 24, 25, 26, 27]
    assert sum(out) == pytest.approx(5.0)
