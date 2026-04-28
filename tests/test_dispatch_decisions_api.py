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
