"""#850 -- Fox group comparators: order-insensitive, unspecified maxSoc is a wildcard."""
from unittest.mock import MagicMock

import pytest

from src import db, state_machine as sm
from src.foxess.models import SchedulerGroup, fingerprints_match

SG = SchedulerGroup


def _hw():
    return [
        SG(4, 0, 5, 59, "ForceCharge", min_soc_on_grid=10, fd_soc=80, fd_pwr=3650, max_soc=100),
        SG(6, 30, 6, 59, "ForceCharge", min_soc_on_grid=10, fd_soc=95, fd_pwr=2750, max_soc=100),
        SG(13, 0, 13, 59, "Backup", min_soc_on_grid=10, fd_soc=100, fd_pwr=3400, max_soc=10),
        SG(14, 0, 15, 59, "ForceCharge", min_soc_on_grid=10, fd_soc=100, fd_pwr=2875, max_soc=100),
        SG(22, 0, 23, 59, "ForceCharge", min_soc_on_grid=10, fd_soc=70, fd_pwr=2225, max_soc=100),
    ]


def _sql():
    return [
        SG(6, 30, 6, 59, "ForceCharge", min_soc_on_grid=10, fd_soc=95, fd_pwr=2750),
        SG(13, 0, 13, 59, "Backup", min_soc_on_grid=10, max_soc=10),
        SG(14, 0, 15, 59, "ForceCharge", min_soc_on_grid=10, fd_soc=100, fd_pwr=2875),
        SG(22, 0, 23, 59, "ForceCharge", min_soc_on_grid=10, fd_soc=70, fd_pwr=2225),
        SG(4, 0, 5, 59, "ForceCharge", min_soc_on_grid=10, fd_soc=80, fd_pwr=3650),
    ]


def test_prod_pair_equal_both_ways_of_signature_and_differs():
    assert sm._schedule_signature(_hw()) == sm._schedule_signature(list(reversed(_hw())))
    assert not sm._fox_schedule_differs(_hw(), _sql())


def test_fingerprints_match_is_order_insensitive():
    a = [g.fingerprint() for g in _sql()]
    b = [g.fingerprint() for g in _hw()]
    assert fingerprints_match(a, b)


@pytest.mark.parametrize("mutate", [
    lambda g: setattr(g[0], "fd_soc", 85),
    lambda g: setattr(g[1], "work_mode", "SelfUse"),
    lambda g: setattr(g[2], "start_minute", 30),
    lambda g: g.pop(),
])
def test_real_difference_detected(mutate):
    hw = _hw()
    mutate(hw)
    assert sm._fox_schedule_differs(hw, _sql())


def test_explicit_stored_maxsoc_stays_distinct():
    sql = _sql()
    sql[0].max_soc = 90  # we asked for 90, device says 100
    assert sm._fox_schedule_differs(_hw(), sql)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from src import config as _config
    monkeypatch.setattr(_config.config, "DB_PATH", db_path, raising=False)
    db.init_db()
    yield


def _wire(monkeypatch, stored, hw_groups):
    monkeypatch.setattr(db, "get_latest_fox_schedule_state",
                        lambda: {"groups": [g.to_api_dict() for g in stored]})
    monkeypatch.setattr(sm.config, "OPENCLAW_READ_ONLY", False, raising=False)
    fox = MagicMock()
    fox.api_key = "k"
    fox.get_scheduler_flag.return_value = True
    hw = MagicMock()
    hw.enabled = True
    hw.groups = hw_groups
    fox.get_scheduler_v3.return_value = hw
    return fox


def test_heartbeat_does_not_reupload_identical_schedule(monkeypatch):
    fox = _wire(monkeypatch, _sql(), _hw())
    sm.heartbeat_repair_fox_scheduler(fox)
    assert fox.set_scheduler_v3.call_count == 0


def test_heartbeat_reuploads_on_real_difference(monkeypatch):
    hw = _hw()
    hw[0].fd_soc = 85
    fox = _wire(monkeypatch, _sql(), hw)
    sm.heartbeat_repair_fox_scheduler(fox)
    assert fox.set_scheduler_v3.call_count == 1
