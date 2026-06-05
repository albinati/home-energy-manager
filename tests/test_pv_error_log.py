"""Per-slot committed PV forecast stitch + pv_error_log persistence (#462)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.api.routers import pv as pv_router


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "DB_PATH", str(db_path))
    db.init_db()
    import src.weather as weather
    monkeypatch.setattr(weather, "fetch_forecast", lambda hours=48: [])
    yield db_path


def _seed_run(run_at: datetime, slots: list[tuple[datetime, float]]) -> int:
    """Insert an optimizer_log run + its lp_solution_snapshot PV forecasts.

    slot_time_utc is stored in the +00:00 form the optimizer uses
    (``slot_start.isoformat()``)."""
    with db._lock:
        conn = db.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO optimizer_log (run_at) VALUES (?)",
                (run_at.isoformat().replace("+00:00", "Z"),),
            )
            run_id = int(cur.lastrowid)
            for i, (slot_dt, kwh) in enumerate(slots):
                conn.execute(
                    """INSERT INTO lp_solution_snapshot
                         (run_id, slot_index, slot_time_utc, pv_forecast_kwh)
                       VALUES (?, ?, ?, ?)""",
                    (run_id, i, slot_dt.isoformat(), kwh),
                )
            conn.commit()
            return run_id
        finally:
            conn.close()


def _day_slots(day, kwh: float, start_hour=0, end_hour=24):
    out = []
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    for i in range(start_hour * 2, end_hour * 2):
        out.append((base + timedelta(minutes=30 * i), kwh))
    return out


def test_stitch_picks_latest_eligible_run_per_slot():
    day = (datetime.now(UTC) - timedelta(days=2)).date()
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    # Early run at 00:05 forecasts the WHOLE day at 1.0.
    _seed_run(base + timedelta(minutes=5), _day_slots(day, 1.0))
    # Midday re-solve at 12:00 forecasts 12:00→ at 2.0.
    _seed_run(base + timedelta(hours=12), _day_slots(day, 2.0, start_hour=12))

    got = db.committed_pv_forecast_by_slot(day)
    # A 06:00 slot: only the early run is eligible (12:00 run_at > 06:00) → 1.0.
    k0600 = (base + timedelta(hours=6)).isoformat()
    assert got[k0600] == pytest.approx(1.0)
    # A 13:00 slot: both runs eligible; most recent (12:00) wins → 2.0.
    k1300 = (base + timedelta(hours=13)).isoformat()
    assert got[k1300] == pytest.approx(2.0)


def test_stitch_falls_back_to_earliest_when_none_eligible():
    day = (datetime.now(UTC) - timedelta(days=2)).date()
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    # Only run fired at 06:00, but it (re)forecast the 00:00 slot too. No run has
    # run_at <= 00:00, so the earliest (only) run is the fallback for that slot.
    _seed_run(base + timedelta(hours=6), _day_slots(day, 1.5))
    got = db.committed_pv_forecast_by_slot(day)
    k0000 = base.isoformat()
    assert got[k0000] == pytest.approx(1.5)


def test_rebuild_pv_error_log_joins_forecast_and_actual():
    day = (datetime.now(UTC) - timedelta(days=2)).date()
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    _seed_run(base + timedelta(minutes=5), _day_slots(day, 1.0))
    # Seed ~2 kWh actual across the 10:00 hour (two 30-min slots).
    for m in (0, 15, 30, 45, 60):
        ts = base + timedelta(hours=10, minutes=m)
        db.save_pv_realtime_sample(ts.isoformat().replace("+00:00", "Z"), solar_power_kw=2.0, source="test")

    written = db.rebuild_pv_error_log_for_date(day)
    assert written > 0
    rows = {r["slot_time_utc"]: r for r in db.get_pv_error_log_for_date(day)}
    k1000 = (base + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert k1000 in rows
    r = rows[k1000]
    assert r["forecast_kwh"] == pytest.approx(1.0)
    assert r["actual_kwh"] is not None and r["actual_kwh"] > 0
    # error = actual − forecast
    assert r["error_kwh"] == pytest.approx(r["actual_kwh"] - 1.0)


def test_rebuild_is_idempotent():
    day = (datetime.now(UTC) - timedelta(days=2)).date()
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    _seed_run(base + timedelta(minutes=5), _day_slots(day, 1.0))
    n1 = db.rebuild_pv_error_log_for_date(day)
    n2 = db.rebuild_pv_error_log_for_date(day)
    assert n1 == n2
    # No duplicate rows (PRIMARY KEY on slot_time_utc).
    rows = db.get_pv_error_log_for_date(day)
    keys = [r["slot_time_utc"] for r in rows]
    assert len(keys) == len(set(keys))


def test_pv_today_accuracy_uses_committed_plan_for_elapsed_slots():
    """The accuracy baseline must come from the committed plan, not the
    forward-only live forecast (which is 0 for elapsed slots)."""
    day = (datetime.now(UTC) - timedelta(days=1)).date()  # fully elapsed
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    # Committed plan forecasts every slot at 0.5 kWh.
    _seed_run(base + timedelta(minutes=5), _day_slots(day, 0.5))
    # Realised ~3 kWh across the 10:00 hour.
    for m in (0, 15, 30, 45, 60):
        ts = base + timedelta(hours=10, minutes=m)
        db.save_pv_realtime_sample(ts.isoformat().replace("+00:00", "Z"), solar_power_kw=3.0, source="test")

    resp = asyncio.run(pv_router.get_pv_today(date=day.isoformat()))
    acc = resp["accuracy"]
    assert acc is not None
    # Forecast baseline is NON-zero now (committed plan), unlike the old behaviour.
    assert acc["forecast_kwh"] > 0
    # Two elapsed 10:00 slots compared at 0.5 each → ~1.0 forecast baseline.
    assert acc["forecast_kwh"] == pytest.approx(1.0, abs=1e-6)
    # And the committed-plan line is populated per slot.
    assert any(s["pv_planned_kwh"] == pytest.approx(0.5) for s in resp["slots"])
