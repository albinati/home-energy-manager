"""Phase 3b backend: GET /api/v1/grid/today per-slot planned-vs-realised grid.

The Grid widget needs per-slot grid import/export — which /execution/today
never carried. This endpoint stitches the committed LP plan (import_kwh /
export_kwh) and rolls up realised grid traffic from pv_realtime_history. The
partition rule (actuals only for ELAPSED slots, planned for all) is the bit
worth pinning, so the chart never plots a future actual or a hole in the plan.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from src import db
from src.config import config


def _client(monkeypatch):
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    from src.api.main import app
    return TestClient(app)


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_grid_today_partitions_planned_and_actual(monkeypatch):
    db.init_db()
    client = _client(monkeypatch)

    # A past UTC day → every slot is elapsed, so seeded actuals must surface.
    day = (datetime.now(UTC) - timedelta(days=2)).date()
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    s0 = _z(day_start)                                   # 00:00
    s1 = _z(day_start + timedelta(minutes=30))           # 00:30

    # Committed plan covers both slots; realised only seeded for slot 0.
    monkeypatch.setattr(db, "committed_lp_field_by_slot",
                        lambda d, field: {s0: 0.8, s1: 0.5} if field == "import_kwh"
                        else {s0: 0.0, s1: 0.2})
    monkeypatch.setattr(db, "half_hourly_grid_import_kwh_for_day", lambda d: {s0: 0.91})
    monkeypatch.setattr(db, "half_hourly_grid_export_kwh_for_day", lambda d: {})
    monkeypatch.setattr(db, "get_rates_for_period", lambda *a, **k: [])
    monkeypatch.setattr(db, "find_run_for_time", lambda when: None)
    monkeypatch.setattr(db, "find_latest_optimizer_run_id", lambda: None)

    r = client.get(f"/api/v1/grid/today?date={day.isoformat()}")
    assert r.status_code == 200, r.text
    body = r.json()
    by = {s["slot_utc"]: s for s in body["slots"]}

    # Slot 0: plan + realised import both present; no export realised → null.
    assert by[s0]["import_planned_kwh"] == 0.8
    assert by[s0]["import_actual_kwh"] == 0.91
    assert by[s0]["export_actual_kwh"] is None
    # Slot 1: plan present, but NO realised import seeded → actual stays null
    # even though the slot is elapsed (no telemetry, not a zero).
    assert by[s1]["import_planned_kwh"] == 0.5
    assert by[s1]["export_planned_kwh"] == 0.2
    assert by[s1]["import_actual_kwh"] is None

    # Totals: planned sums everything; realised sums only what was measured.
    assert round(body["totals"]["import_planned_kwh"], 3) == 1.3
    assert round(body["totals"]["import_actual_kwh"], 3) == 0.91


def test_grid_today_future_day_has_plan_but_no_actuals(monkeypatch):
    """A future day: every slot is ahead of now → actuals null, plan still drawn."""
    db.init_db()
    client = _client(monkeypatch)

    day = (datetime.now(UTC) + timedelta(days=1)).date()
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    s0 = _z(day_start)

    monkeypatch.setattr(db, "committed_lp_field_by_slot",
                        lambda d, field: {s0: 1.2} if field == "import_kwh" else {})
    # Even if telemetry somehow existed, a future slot must not show an actual.
    monkeypatch.setattr(db, "half_hourly_grid_import_kwh_for_day", lambda d: {s0: 99.0})
    monkeypatch.setattr(db, "half_hourly_grid_export_kwh_for_day", lambda d: {})
    monkeypatch.setattr(db, "get_rates_for_period", lambda *a, **k: [])
    monkeypatch.setattr(db, "find_run_for_time", lambda when: None)
    monkeypatch.setattr(db, "find_latest_optimizer_run_id", lambda: None)

    body = client.get(f"/api/v1/grid/today?date={day.isoformat()}").json()
    by = {s["slot_utc"]: s for s in body["slots"]}
    assert by[s0]["import_planned_kwh"] == 1.2
    assert by[s0]["import_actual_kwh"] is None        # future → null, never 99
    assert body["totals"]["import_actual_kwh"] == 0.0


def test_committed_lp_field_by_slot_rejects_unknown_column():
    """Defensive: the field name is interpolated into SQL → must be whitelisted."""
    from datetime import date
    assert db.committed_lp_field_by_slot(date(2026, 1, 1), "import_kwh; DROP TABLE x") == {}
