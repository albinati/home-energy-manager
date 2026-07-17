"""resolve_tank_params: the one door a learned value walks through.

It has to let a good, fresh fit in — and turn everything else away in favour of the
databook, which is a perfectly good tank. Staleness is a first-class reason to fall
back: a summer-fitted ambient must not steer a winter plan.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db as _db
from src.config import config
from src.dhw import params
from src.dhw.model import TankParams


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "cal.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(_db, "_db_path", lambda: path)
    _db.init_db()
    monkeypatch.setattr(config, "DHW_CALIBRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_CALIBRATION_MAX_AGE_DAYS", 45.0, raising=False)
    return path


def _store(status="ok", ua=2.48, ambient=23.8, age_days=1.0):
    payload = {"status": status, "ua_w_per_k": ua, "ambient_c": ambient,
               "r2": 0.81, "episodes": 18}
    _db.upsert_dhw_calibration("ua_ambient", status=status, payload=payload,
                               n_samples=18, r2=0.81, window_days=21)
    # Backdate fitted_at for the staleness cases.
    if age_days != 1.0:
        stamp = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
        conn = _db.get_connection()
        try:
            conn.execute("UPDATE dhw_calibration SET fitted_at_utc=? WHERE component='ua_ambient'",
                         (stamp,))
            conn.commit()
        finally:
            conn.close()


def test_no_calibration_yet_uses_the_databook(tmp_db):
    p = params.resolve_tank_params()
    assert p.source == "databook"
    assert p == TankParams()


def test_a_good_fresh_fit_steers_the_model(tmp_db):
    _store(ua=2.48, ambient=23.8)
    p = params.resolve_tank_params()
    assert p.source == "measured"
    assert p.ua_w_per_k == pytest.approx(2.48)
    assert p.ambient_c == pytest.approx(23.8)
    # Everything NOT learned stays databook — COP, caps, cliff.
    assert p.t_hp_max_c == TankParams().t_hp_max_c
    assert p.litres == TankParams().litres


def test_a_skipped_fit_falls_back(tmp_db):
    _store(status="skipped")
    assert params.resolve_tank_params().source == "databook"


def test_a_stale_fit_falls_back_rather_than_steering_a_new_season(tmp_db):
    """The cost of a merge-preserving store: a component that never re-fits keeps its
    value forever. Right for a quiet week, wrong for a quiet season."""
    _store(age_days=120.0)
    assert params.resolve_tank_params().source == "databook"


def test_an_out_of_range_value_is_rejected_at_the_door(tmp_db):
    """The fit's own bounds already ran, but the value about to steer a real heat
    pump gets one last independent check."""
    _store(ua=99.0)
    assert params.resolve_tank_params().source == "databook"


def test_the_kill_switch_forces_the_databook(tmp_db, monkeypatch):
    _store()
    monkeypatch.setattr(config, "DHW_CALIBRATION_ENABLED", False, raising=False)
    assert params.resolve_tank_params().source == "databook"


def test_a_db_failure_degrades_to_the_databook(tmp_db, monkeypatch):
    def _boom(component):
        raise RuntimeError("db gone")

    monkeypatch.setattr(_db, "get_dhw_calibration", _boom)
    assert params.resolve_tank_params().source == "databook"


# --- #732: resolve_reheat_differential_c ----------------------------------------

def _store_diff(status="ok", value=6.5, age_days=1.0):
    payload = {"status": status, "differential_c": value,
               "threshold_c": value, "n_misclassified": 1, "n_episodes": 6}
    _db.upsert_dhw_calibration("reheat_differential", status=status, payload=payload,
                               n_samples=6, window_days=21)
    if age_days != 1.0:
        stamp = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
        conn = _db.get_connection()
        try:
            conn.execute(
                "UPDATE dhw_calibration SET fitted_at_utc=? WHERE component='reheat_differential'",
                (stamp,))
            conn.commit()
        finally:
            conn.close()


def test_reheat_differential_fallback_when_unfitted(tmp_db):
    from src.dhw.params import resolve_reheat_differential_c
    assert resolve_reheat_differential_c() == 6.0


def test_reheat_differential_uses_measured_value(tmp_db):
    from src.dhw.params import resolve_reheat_differential_c
    _store_diff(value=6.5)
    assert resolve_reheat_differential_c() == 6.5


def test_reheat_differential_stale_or_bad_falls_back(tmp_db):
    from src.dhw.params import resolve_reheat_differential_c
    _store_diff(value=6.5, age_days=90.0)
    assert resolve_reheat_differential_c() == 6.0
    _store_diff(status="inconsistent", value=6.5)
    assert resolve_reheat_differential_c() == 6.0
    _store_diff(status="ok", value=15.0)  # out of [2, 12] — re-clamp at the door
    assert resolve_reheat_differential_c() == 6.0
