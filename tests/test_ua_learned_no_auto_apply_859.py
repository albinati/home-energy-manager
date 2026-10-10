"""#859 — a learned UA is shown, never applied, unless BUILDING_UA_LEARNED_AUTO_APPLY."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src import db
from src.analytics import thermal_learning as tl
from src.config import config


@pytest.fixture
def tmpdb(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr("src.config.config.DB_PATH", str(Path(td) / "t.db"))
        db.init_db()
        yield


def _cal(monkeypatch, ua, auto):
    row = {"tau_hours": 82.7, "c_kwh_per_k": 16.54, "c_source": "tau_x_env_ua",
           "c_ua_basis_w_per_k": 200.0, "ua_w_per_k": ua}
    monkeypatch.setattr(tl, "_calibration_row", lambda: dict(row))
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 200.0, raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_LEARNED_AUTO_APPLY", auto, raising=False)


def test_default_is_off():
    assert config.BUILDING_UA_LEARNED_AUTO_APPLY is False


def test_off_learned_650_returns_pin_and_c_from_pin(monkeypatch):
    _cal(monkeypatch, 650.0, False)
    assert tl.get_building_ua_w_per_k() == 200.0
    assert tl.get_learned_ua_w_per_k() == 650.0
    assert tl.ua_effective_source() == "pin"
    res = tl.thermal_mass_resolution()
    assert res["ua_eff_w_per_k"] == 200.0
    assert res["c_kwh_per_k"] == pytest.approx(16.54)
    assert res["c_recomputed"] is False


def test_on_learned_650_applies_old_behaviour(monkeypatch):
    _cal(monkeypatch, 650.0, True)
    assert tl.get_building_ua_w_per_k() == 650.0
    assert tl.ua_effective_source() == "learned"
    res = tl.thermal_mass_resolution()
    assert res["c_recomputed"] is True
    assert res["c_kwh_per_k"] == pytest.approx(82.7 * 650 / 1000)


def test_learned_none_or_out_of_bounds_is_pin(monkeypatch):
    for ua in (None, 2000.0):
        _cal(monkeypatch, ua, True)
        assert tl.get_building_ua_w_per_k() == 200.0
        assert tl.ua_effective_source() == "pin"


def _refresh(monkeypatch, ua):
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 200.0, raising=False)
    monkeypatch.setattr(db, "get_indoor_readings_range", lambda s, e: [
        {"captured_at": "2026-11-04T03:00:00Z", "room": "a", "temp_c": 20.0}])
    monkeypatch.setattr(tl, "_outdoor_series", lambda a, b: [])
    monkeypatch.setattr(tl, "select_decay_episodes", lambda *a, **k: [])
    monkeypatch.setattr(tl, "fit_tau", lambda *a, **k: {
        "status": "ok", "tau_hours": 60.0, "r2_median": 0.9, "episodes": 5})
    monkeypatch.setattr(tl, "_ua_fit_from_db", lambda *a, **k: {
        "status": "ok", "ua_w_per_k": ua, "r2": 0.9, "samples": 30, "assumed_cop": 3.0})
    return tl.refresh_building_thermal_calibration()


def test_refresh_off_stores_learned_but_c_from_pin_and_notifies_once(monkeypatch, tmpdb):
    monkeypatch.setattr(config, "BUILDING_UA_LEARNED_AUTO_APPLY", False, raising=False)
    sent = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, **k: sent.append(msg))
    assert _refresh(monkeypatch, 650.0)["status"] == "ok"
    row = db.get_building_thermal_calibration()
    assert row["ua_w_per_k"] == 650.0
    assert row["c_ua_basis_w_per_k"] == 200.0
    assert row["c_kwh_per_k"] == pytest.approx(12.0)
    assert row["c_source"] == "tau_x_env_ua"
    assert tl.get_building_ua_w_per_k() == 200.0
    assert len(sent) == 1 and "650" in sent[0]
    _refresh(monkeypatch, 652.0)  # same 10 W/K bucket -> deduped
    assert len(sent) == 1



def test_refresh_on_stamps_c_from_learned_and_no_alert(monkeypatch, tmpdb):
    monkeypatch.setattr(config, "BUILDING_UA_LEARNED_AUTO_APPLY", True, raising=False)
    sent = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, **k: sent.append(msg))
    _refresh(monkeypatch, 650.0)
    row = db.get_building_thermal_calibration()
    assert row["c_source"] == "tau_x_learned_ua" and row["c_ua_basis_w_per_k"] == 650.0
    assert tl.get_building_ua_w_per_k() == 650.0
    assert sent == []


def test_no_alert_within_25_percent(monkeypatch, tmpdb):
    monkeypatch.setattr(config, "BUILDING_UA_LEARNED_AUTO_APPLY", False, raising=False)
    monkeypatch.setattr(config, "BUILDING_UA_W_PER_K", 200.0, raising=False)
    sent = []
    monkeypatch.setattr("src.notifier.notify_risk", lambda msg, **k: sent.append(msg))
    assert tl.notify_ua_learned_pending(240.0) is False
    assert sent == []


def test_lp_and_estimator_get_the_pin(monkeypatch):
    """Every consumer funnels through get_building_ua_w_per_k -> the pin."""
    _cal(monkeypatch, 650.0, False)
    from src.daikin import estimator
    import inspect
    assert "get_building_ua_w_per_k" in inspect.getsource(estimator)
    from src.scheduler import lp_optimizer
    assert "get_building_ua_w_per_k()" in inspect.getsource(lp_optimizer)
    assert tl.get_building_ua_w_per_k() == 200.0


def test_thermal_calibration_endpoint_exposes_fields(monkeypatch, tmpdb):
    _cal(monkeypatch, 650.0, False)
    from src.api.routers import sensors
    import asyncio
    fn = [getattr(sensors, n) for n in dir(sensors) if n.endswith("thermal_calibration")]
    assert fn
    out = fn[0]()
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    eff = out["effective"]
    assert eff["ua_learned_w_per_k"] == 650.0 and eff["ua_pinned_w_per_k"] == 200.0
    assert eff["ua_effective_source"] == "pin" and eff["ua_w_per_k"] == 200.0
