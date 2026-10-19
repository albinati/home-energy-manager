"""#865: import_overshoot MPC trigger fires at most once per completed slot."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.config import config
from src.scheduler import runner


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    kv: dict[str, str] = {}
    monkeypatch.setattr(runner.db, "get_kv", lambda k, *a, **kw: kv.get(k))
    monkeypatch.setattr(runner.db, "set_kv", lambda k, v, *a, **kw: kv.__setitem__(k, v))
    monkeypatch.setattr(runner, "_import_overshoot_last_slot", None, raising=False)
    monkeypatch.setattr(config, "MPC_EVENT_DRIVEN_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "MPC_IMPORT_OVERSHOOT_KWH_THRESHOLD", 0.5, raising=False)
    return kv


def _run(ticks, actual, planned):
    with patch.object(runner, "_actual_import_kwh_for_slot", return_value=actual), \
         patch.object(runner, "_lp_planned_import_kwh_at", return_value=planned), \
         patch.object(runner, "bulletproof_mpc_job") as job:
        for t in ticks:
            runner._maybe_fire_import_overshoot(t)
    return job


def test_many_ticks_same_slot_fire_once():
    base = datetime(2026, 10, 19, 3, 31, tzinfo=UTC)
    job = _run([base + timedelta(minutes=5 * i) for i in range(5)], 2.82, 1.90)
    assert job.call_count == 1
    assert job.call_args.kwargs["trigger_reason"] == "import_overshoot"


def test_next_slot_fires_again():
    t1 = datetime(2026, 10, 19, 3, 31, tzinfo=UTC)
    job = _run([t1, t1 + timedelta(minutes=5), t1 + timedelta(minutes=30),
                t1 + timedelta(minutes=35)], 2.82, 1.90)
    assert job.call_count == 2


def test_below_threshold_never_fires():
    base = datetime(2026, 10, 19, 3, 31, tzinfo=UTC)
    job = _run([base + timedelta(minutes=5 * i) for i in range(5)], 2.0, 1.90)
    assert job.call_count == 0


def test_kv_failure_falls_back_to_memory(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kv down")
    monkeypatch.setattr(runner.db, "get_kv", boom)
    monkeypatch.setattr(runner.db, "set_kv", boom)
    base = datetime(2026, 10, 19, 3, 31, tzinfo=UTC)
    job = _run([base + timedelta(minutes=5 * i) for i in range(4)], 2.82, 1.90)
    assert job.call_count == 1


def test_persisted_slot_survives_restart(_env):
    base = datetime(2026, 10, 19, 3, 31, tzinfo=UTC)
    _run([base], 2.82, 1.90)
    runner._import_overshoot_last_slot = None  # simulate restart
    job = _run([base + timedelta(minutes=5)], 2.82, 1.90)
    assert job.call_count == 0
