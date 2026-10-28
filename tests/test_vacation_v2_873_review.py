"""#873 review fixes (B1, S1-S9, N1): races, retries, override traps, boot re-arm."""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import db, runtime_settings as rts, vacation
from src.config import config
from tests.test_vacation_v2_873 import (  # noqa: F401 - fixtures are discovered by import
    LONDON,
    _env,
    _set_vacation,
    _utc,
    _World,
    client,
)


def _future_local(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).astimezone(LONDON).strftime("%Y-%m-%dT%H:%M")


# --- B1: MPC returns False (no raise) -------------------------------------

def test_recovery_retries_mpc_when_it_returns_false(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    w.calls.clear()
    w.mpc_results = [False, False, True]
    r = vacation.run_recovery("scheduled")
    assert len([c for c in w.calls if c[0] == "mpc"]) == 3
    assert r["steps"]["mpc"] == "ok (attempt 3)"
    assert "falhas" not in w.infos[-1]


def test_recovery_reports_mpc_failure_when_it_never_runs(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    w.calls.clear()
    w.mpc_results = [False] * 5
    r = vacation.run_recovery("scheduled")
    assert r["steps"]["mpc"].startswith("failed")
    assert len([c for c in w.calls if c[0] == "mpc"]) == 5
    assert "houve falhas" in w.infos[-1]


def test_activation_replan_also_retries(monkeypatch):
    w = _World(monkeypatch)
    w.mpc_results = [False, True]
    _set_vacation(72)
    plan = vacation.activate(actor="test")
    assert plan["results"]["replan"] == "ok (attempt 2)"


def test_recovery_preset_is_normal_before_holiday_off(monkeypatch):
    """A concurrent heartbeat holiday_end solve must already see `normal`."""
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    assert config.OPTIMIZATION_PRESET == "vacation"
    vacation.run_recovery("scheduled")
    assert w.preset_at_off == "normal"


def test_recovery_order_reads_and_writes(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    w.calls.clear()
    w.devices = [SimpleNamespace(is_on=False, tank_on=True, tank_temperature=40.0)]
    vacation.run_recovery("scheduled")
    assert [c[0] for c in w.calls] == ["holiday", "read", "set_power", "read", "mpc"]


# --- S7: Onecta lag -------------------------------------------------------

def test_recovery_polls_until_holiday_flag_clears(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    w.calls.clear()
    held = SimpleNamespace(is_on=False, tank_on=True, holiday_active=True, tank_holiday_active=True)
    free = SimpleNamespace(is_on=False, tank_on=True, holiday_active=False, tank_holiday_active=False)
    w.device_seq = [held, held, free, free]
    r = vacation.run_recovery("scheduled")
    assert [c[0] for c in w.calls] == ["holiday", "read", "read", "read", "set_power", "read", "mpc"]
    assert len(r["steps"]["holiday_poll"]) == 3


# --- S1: activation / recovery race --------------------------------------

def test_immediate_recovery_skips_the_holiday_post(monkeypatch):
    w = _World(monkeypatch)
    monkeypatch.setattr(config, "VACATION_RECOVERY_HOURS", 12, raising=False)
    _set_vacation(6)  # return in 6 h, lead 12 h -> recovery is "now"
    plan = vacation.activate(actor="test")
    assert plan["recovery_immediate"] is True
    assert not [c for c in w.calls if c[0] == "holiday"]
    assert vacation.load_state()["holiday_post"] == "skipped_immediate"


def test_recovery_waits_for_inflight_activation(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    plan = vacation.begin_activation(actor="test")  # holds the activation lock
    done = threading.Event()
    t = threading.Thread(target=lambda: (vacation.run_recovery("cancel"), done.set()))
    t.start()
    assert not done.wait(0.3)  # blocked on the in-flight activation
    assert not [c for c in w.calls if c[0] == "holiday"]
    vacation.finish_activation(plan)
    t.join(10)
    assert done.is_set()
    assert [c[1] for c in w.calls if c[0] == "holiday"] == [True, False]  # ON strictly before OFF


# --- S2: bounded snap on a seeded DYNAMIC tariff --------------------------

def test_dynamic_tariff_recovery_is_not_snapped(monkeypatch):
    code = "E-1R-AGILE-24-10-01-H"
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", code, raising=False)
    base = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
    rates = [{
        "value_inc_vat": 8.0 + (i * 7) % 29,   # many distinct levels -> dynamic
        "valid_from": (base + timedelta(minutes=30 * i)).isoformat(),
        "valid_to": (base + timedelta(minutes=30 * (i + 1))).isoformat(),
    } for i in range(96)]
    db.save_agile_rates(rates, code)
    monkeypatch.setattr(config, "VACATION_RECOVERY_HOURS", 12, raising=False)
    assert vacation.compute_recovery_at(_utc(2026, 11, 7, 18)) == _utc(2026, 11, 7, 6)


# --- S6: no job -----------------------------------------------------------

def test_missing_recovery_job_alerts(monkeypatch):
    w = _World(monkeypatch)
    from src.scheduler import runner

    monkeypatch.setattr(runner, "_background_scheduler", None)
    _set_vacation(72)
    vacation.activate(actor="test")
    assert any("NÃO foi agendada" in m for m in w.risks)


# --- S9: activation failure ----------------------------------------------

def test_activation_failure_is_reported_not_raised(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(db, "set_kv", _boom)
    info = vacation.after_settings_applied("normal", {"OPTIMIZATION_PRESET": "vacation"}, background=False)
    assert info["status"] == "error"
    assert any("Ativação das férias falhou" in m for m in w.risks)
    assert vacation._activation_lock.acquire(timeout=1)  # lock was released
    vacation._activation_lock.release()


# --- S4: Z-suffixed / odd ISO input; DST parsing --------------------------

def test_parse_return_at_z_and_dst():
    assert vacation.parse_return_at("2030-01-02t18:00:00z") == datetime(2030, 1, 2, 18, 0, tzinfo=UTC)
    assert vacation.parse_return_at("2030-01-02T18:00:00+01:00") == datetime(2030, 1, 2, 17, 0, tzinfo=UTC)
    assert vacation.parse_return_at("garbage") is None
    assert vacation.parse_return_at("2026-10-24T23:30") == _utc(2026, 10, 24, 22, 30)   # BST
    assert vacation.parse_return_at("2026-10-25T23:30") == _utc(2026, 10, 25, 23, 30)   # GMT
    assert vacation.parse_return_at("2026-10-25T01:30") == _utc(2026, 10, 25, 0, 30)    # ambiguous -> first (BST)
    assert vacation.parse_return_at("2026-03-29T01:30") == _utc(2026, 3, 29, 1, 30)     # nonexistent -> pre-change offset


def test_put_z_suffixed_return_at_and_legacy_never_500(client, monkeypatch):
    _World(monkeypatch)
    ret = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.put("/api/v1/settings/OPTIMIZATION_PRESET", json={"value": "vacation", "return_at": ret})
    assert r.status_code == 200, r.text
    r = client.post("/api/v1/optimization/preset", json={"preset": "travel", "return_at": "nonsense"})
    assert r.status_code in (400, 422), r.text


# --- S3: override trap + legacy normal/guests -----------------------------

def test_legacy_guests_override_does_not_block_a_later_vacation_put(client, monkeypatch):
    w = _World(monkeypatch)
    r = client.post("/api/v1/optimization/preset", json={"preset": "guests"})
    assert r.status_code == 200, r.text
    assert rts.get_setting("OPTIMIZATION_PRESET") == "guests"        # persisted, not just an override
    assert not config.has_override("OPTIMIZATION_PRESET")
    config.OPTIMIZATION_PRESET = "guests"                            # simulate a pinned override
    r = client.put("/api/v1/settings/OPTIMIZATION_PRESET",
                   json={"value": "vacation", "return_at": _future_local(72)})
    assert r.status_code == 200 and r.json()["vacation"]["status"] == "activating"
    assert config.OPTIMIZATION_PRESET == "vacation"
    assert db.get_kv(vacation.KV_END_AT)
    assert [c for c in w.calls if c[0] == "holiday" and c[1] is True]


# --- cancel early over HTTP ----------------------------------------------

@pytest.mark.parametrize("mode", ["put", "batch"])
def test_cancel_early_via_put_and_batch(client, monkeypatch, mode):
    w = _World(monkeypatch)
    r = client.put("/api/v1/settings/OPTIMIZATION_PRESET",
                   json={"value": "vacation", "return_at": _future_local(72)})
    assert r.status_code == 200, r.text
    w.calls.clear()
    if mode == "put":
        r = client.put("/api/v1/settings/OPTIMIZATION_PRESET", json={"value": "normal"})
    else:
        r = client.post("/api/v1/settings/batch", json={"changes": {"OPTIMIZATION_PRESET": "normal"}})
    assert r.status_code == 200, r.text
    assert r.json()["vacation"]["status"] == "recovering"
    assert ("holiday", False, None, None) in w.calls and any(c[0] == "mpc" for c in w.calls)
    assert not db.get_kv(vacation.KV_END_AT)
    assert config.OPTIMIZATION_PRESET == "normal"


# --- S8: DELETE -----------------------------------------------------------

def test_delete_preset_runs_recovery_and_return_at_delete_is_refused(client, monkeypatch):
    w = _World(monkeypatch)
    client.put("/api/v1/settings/OPTIMIZATION_PRESET", json={"value": "vacation", "return_at": _future_local(72)})
    assert client.delete("/api/v1/settings/VACATION_RETURN_AT").status_code == 422
    w.calls.clear()
    assert client.delete("/api/v1/settings/OPTIMIZATION_PRESET").status_code == 200
    assert ("holiday", False, None, None) in w.calls
    assert not db.get_kv(vacation.KV_END_AT)


# --- S5 / S11: kill switch mid-trip + restart; scheduler boot hook --------

def test_kill_switch_flipped_mid_trip_still_ends_the_trip_after_restart(monkeypatch):
    w = _World(monkeypatch)
    _set_vacation(72)
    vacation.activate(actor="test")
    monkeypatch.setattr(config, "VACATION_DAIKIN_HOLIDAY_ENABLED", False, raising=False)
    w.jobs.clear()
    assert vacation.rearm_from_kv() == "scheduled" and w.jobs      # re-armed despite the switch
    w.calls.clear()
    vacation.run_recovery("scheduled")
    assert config.OPTIMIZATION_PRESET == "normal"
    assert ("holiday", False, None, None) in w.calls               # disabling is always allowed
    assert not db.get_kv(vacation.KV_END_AT)


def test_start_background_scheduler_rearms_vacation(monkeypatch):
    from apscheduler.schedulers import background as bg

    from src.scheduler import runner

    class _Fake:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: []

    seen: list[str] = []
    monkeypatch.setattr(bg, "BackgroundScheduler", _Fake)
    monkeypatch.setattr(runner, "_background_scheduler", None)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H", raising=False)
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "USE_BULLETPROOF_ENGINE", False, raising=False)
    monkeypatch.setattr(vacation, "rearm_from_kv", lambda: seen.append("rearm"))
    try:
        runner.start_background_scheduler()
    finally:
        runner._background_scheduler = None
    assert seen == ["rearm"]


# --- heartbeat holiday-exit overlapping the recovery ----------------------

def test_heartbeat_exit_tick_overlap_is_harmless(monkeypatch):
    """The #872 tick may fire its own holiday_end replan around the recovery; the
    preset is already normal by then and a second recovery is a noop."""
    w = _World(monkeypatch)
    from src.scheduler import runner

    _set_vacation(72)
    vacation.activate(actor="test")
    db.set_kv("daikin_holiday_active", json.dumps({"climate": True, "tank": True}))
    vacation.run_recovery("scheduled")
    assert config.OPTIMIZATION_PRESET == "normal"
    dev_after = SimpleNamespace(holiday_active=False, tank_holiday_active=False, is_on=True, tank_on=True,
                                holiday_window=None)
    w.calls.clear()
    assert runner._holiday_transition_tick([dev_after]) == "exited"
    assert config.OPTIMIZATION_PRESET == "normal"
    assert vacation.run_recovery("scheduled")["status"] == "noop"


# --- N1: load scale stops at the recovery time ---------------------------

def test_load_scale_stops_at_recovery_time(monkeypatch):
    from src.scheduler import lp_simulation

    monkeypatch.setattr(lp_simulation.db, "residual_load_profile_v2", lambda *a, **k: {})
    monkeypatch.setattr(lp_simulation.db, "lookup_residual_kwh", lambda *a, **k: 0.40)
    monkeypatch.setattr(config, "LP_LOAD_SCALE_FACTOR", 1.0, raising=False)
    monkeypatch.setattr(config, "LP_VACATION_LOAD_SCALE", 0.5, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    t0 = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)
    db.set_kv(vacation.KV_END_AT, (t0 + timedelta(minutes=60)).isoformat().replace("+00:00", "Z"))
    slots = [t0 + timedelta(minutes=30 * i) for i in range(4)]
    out = lp_simulation._build_load_profile(slots)
    assert [round(v, 3) for v in out] == [0.2, 0.2, 0.4, 0.4]
