"""Smoke tests for the new dispatch endpoints.

Uses FastAPI TestClient against a fresh SQLite path so we don't pollute prod.
The Fox V3 live readout in ``/foxess/schedule_diff`` is best-effort: in tests
the FoxESS client may not have credentials, so the endpoint should still
respond with ``ok=False``/``live_error`` rather than crashing.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """Point the DB at a fresh temp file for each test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    # Ensure config and db re-read DB_PATH.
    from src import config as _config
    monkeypatch.setattr(_config.config, "DB_PATH", db_path, raising=False)
    from src import db as _db
    _db.init_db()
    yield


def _seed_run_with_decisions(run_id: int = 99) -> None:
    """Insert a synthetic optimizer_log row + a few dispatch_decisions."""
    from src import db
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO optimizer_log (id, run_at, rates_count, cheap_slots, peak_slots, "
            "standard_slots, negative_slots, target_vwap, actual_agile_mean, "
            "battery_warning, strategy_summary, fox_schedule_uploaded, "
            "daikin_actions_count) VALUES (?, ?, 48, 0, 1, 47, 0, 12, 12, 0, 'test', 1, 0)",
            (run_id, "2026-04-29T12:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    db.upsert_dispatch_decision(
        run_id=run_id,
        slot_time_utc="2026-04-29T17:00:00+00:00",
        lp_kind="peak_export",
        dispatched_kind="peak_export",
        committed=True,
        reason="robust",
        scen_optimistic_exp_kwh=2.10,
        scen_nominal_exp_kwh=1.84,
        scen_pessimistic_exp_kwh=1.40,
    )
    db.upsert_dispatch_decision(
        run_id=run_id,
        slot_time_utc="2026-04-29T17:30:00+00:00",
        lp_kind="peak_export",
        dispatched_kind="standard",
        committed=False,
        reason="pessimistic_disagrees",
        scen_optimistic_exp_kwh=1.95,
        scen_nominal_exp_kwh=1.20,
        scen_pessimistic_exp_kwh=0.05,
    )


def test_decisions_endpoint_with_explicit_run_id():
    _seed_run_with_decisions(run_id=99)
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/optimization/decisions/99")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == 99
    assert len(body["decisions"]) == 2
    assert body["summary"]["peak_export_committed"] == 1
    assert body["summary"]["peak_export_dropped"] == 1
    assert body["summary"]["drop_reasons"]["pessimistic_disagrees"] == 1


def test_decisions_endpoint_latest_alias():
    _seed_run_with_decisions(run_id=99)
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/optimization/decisions/latest")
    assert resp.status_code == 200, resp.text
    assert resp.json()["run_id"] == 99


def test_decisions_endpoint_404_when_no_runs():
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/optimization/decisions/latest")
    assert resp.status_code == 404


def test_decisions_endpoint_400_on_bad_run_id():
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/optimization/decisions/not_a_number")
    assert resp.status_code == 400


def test_schedule_diff_endpoint_handles_no_recorded_state():
    """When fox_schedule_state is empty, the endpoint should still answer."""
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/foxess/schedule_diff")
    # With no creds in test env the live read fails, but the endpoint should
    # still return 200 with live_error populated.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "any_drift" in body
    assert "live_groups" in body
    assert "recorded_groups" in body


def test_timeline_endpoint_returns_empty_when_no_runs():
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/scheduler/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["executed"] == []
    assert body["planned"] == []


def _seed_scenario_batch(batch_id: int = 99) -> None:
    """Insert 3 scenario rows for one batch."""
    from src import db
    db.upsert_scenario_solve_log(
        batch_id=batch_id, nominal_run_id=batch_id, scenario_kind="optimistic",
        lp_status="Optimal", objective_pence=-15.0,
        perturbation_temp_delta_c=1.0, perturbation_load_factor=0.9,
        peak_export_slot_count=2, duration_ms=2400,
    )
    db.upsert_scenario_solve_log(
        batch_id=batch_id, nominal_run_id=batch_id, scenario_kind="nominal",
        lp_status="Optimal", objective_pence=-12.5,
        perturbation_temp_delta_c=0.0, perturbation_load_factor=1.0,
        peak_export_slot_count=2, duration_ms=0,
    )
    db.upsert_scenario_solve_log(
        batch_id=batch_id, nominal_run_id=batch_id, scenario_kind="pessimistic",
        lp_status="Optimal", objective_pence=-9.0,
        perturbation_temp_delta_c=-1.5, perturbation_load_factor=1.15,
        peak_export_slot_count=1, duration_ms=2200,
    )


def test_scenarios_endpoint_returns_three_rows_with_summary():
    _seed_run_with_decisions(run_id=99)
    _seed_scenario_batch(batch_id=99)
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/optimization/scenarios/99")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_id"] == 99
    # Ordered optimistic → nominal → pessimistic regardless of insertion order.
    kinds = [r["scenario_kind"] for r in body["scenarios"]]
    assert kinds == ["optimistic", "nominal", "pessimistic"]
    # Summary correctness.
    assert body["summary"]["objectives_pence"]["pessimistic"] == -9.0
    assert body["summary"]["peak_export_slot_counts"]["pessimistic"] == 1
    assert body["summary"]["max_duration_ms"] == 2400
    assert body["summary"]["any_failure"] is False


def test_scenarios_endpoint_latest_alias():
    _seed_run_with_decisions(run_id=99)
    _seed_scenario_batch(batch_id=99)
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/optimization/scenarios/latest")
    assert resp.status_code == 200, resp.text
    assert resp.json()["batch_id"] == 99


def test_scenarios_endpoint_returns_empty_when_no_scenarios_logged():
    """A run can exist in optimizer_log without an associated scenario batch
    (e.g. trigger reason was soc_drift). Endpoint should respond cleanly."""
    _seed_run_with_decisions(run_id=42)
    from src.api.main import app
    client = TestClient(app)
    resp = client.get("/api/v1/optimization/scenarios/42")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scenarios"] == []
    assert "note" in body


def test_schedule_diff_fingerprint_ignores_vendor_default_echo(monkeypatch):
    """Real prod shapes captured 2026-06-12: Fox echoes max_soc=100 where the
    upload recorded None, and returns stale fd_* values on Backup groups —
    the naive fingerprint reported 16 phantom diffs on identical schedules,
    which would have made the alert strip's drift chip permanent noise."""
    from src import db
    from src.config import config as app_config
    from fastapi.testclient import TestClient

    db.init_db()

    def rec(sh, sm, eh, em, mode, min_soc=10, fd_soc=None, fd_pwr=None, max_soc=None):
        # fox_schedule_state.groups_json / to_api_dict shape
        return {"startHour": sh, "startMinute": sm, "endHour": eh, "endMinute": em,
                "workMode": mode,
                "extraParam": {"minSocOnGrid": min_soc, "fdSoc": fd_soc,
                               "fdPwr": fd_pwr, "maxSoc": max_soc}}

    class LiveGroup:  # SchedulerGroup-like (attributes, NOT a dict)
        def __init__(self, sh, sm, eh, em, mode, min_soc=10,
                     fd_soc=None, fd_pwr=None, max_soc=None):
            self.start_hour, self.start_minute = sh, sm
            self.end_hour, self.end_minute = eh, em
            self.work_mode = mode
            self.min_soc_on_grid = min_soc
            self.fd_soc, self.fd_pwr, self.max_soc = fd_soc, fd_pwr, max_soc

    recorded = [
        rec(21, 0, 22, 30, "ForceDischarge", fd_soc=15, fd_pwr=3680, max_soc=None),
        rec(7, 0, 10, 59, "Backup", fd_soc=None, fd_pwr=None, max_soc=10),
        rec(11, 0, 11, 30, "ForceCharge", fd_soc=31, fd_pwr=3500, max_soc=None),
    ]
    live = [
        LiveGroup(21, 0, 22, 30, "ForceDischarge", fd_soc=15.0, fd_pwr=3680.0, max_soc=100.0),
        # Backup echoes STALE fd_* values the upload never set:
        LiveGroup(7, 0, 10, 59, "Backup", fd_soc=91.0, fd_pwr=2850.0, max_soc=10.0),
        LiveGroup(11, 0, 11, 30, "ForceCharge", fd_soc=31.0, fd_pwr=3500.0, max_soc=100.0),
    ]

    import src.api.routers.dispatch as dispatch_mod
    monkeypatch.setattr(db, "get_latest_fox_schedule_state",
                        lambda: {"groups": recorded})

    class _FakeState:
        groups = live

    class _FakeFox:
        def __init__(self, **_kw): ...
        def get_scheduler_v3(self):
            return _FakeState()

    monkeypatch.setattr(dispatch_mod, "FoxESSClient", _FakeFox)
    # foxess_client_kwargs() validates FOXESS_DEVICE_SN and raises in the test
    # env, which would short-circuit into the live_error path before the fake
    # client is ever constructed.
    monkeypatch.setattr(dispatch_mod.config, "foxess_client_kwargs", lambda: {})
    monkeypatch.setattr(app_config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    from src.api.main import app
    client = TestClient(app)
    body = client.get("/api/v1/foxess/schedule_diff").json()
    assert body["ok"] is True, body.get("live_error")
    assert body["any_drift"] is False, body["diffs"]
    assert body["diffs"]["only_live"] == []
    assert body["diffs"]["only_recorded"] == []

    # Counter-case: a REAL fd_soc change on a ForceDischarge group must still
    # register as drift — canonicalisation only forgives vendor default echo.
    live[0].fd_soc = 50.0
    body = client.get("/api/v1/foxess/schedule_diff").json()
    assert body["any_drift"] is True
    assert len(body["diffs"]["only_live"]) == 1
    assert len(body["diffs"]["only_recorded"]) == 1
