"""load_error_log — Phase-1 load forecast-vs-actual measurement (PV-error-log analog).

committed_load_forecast_by_slot stitches the committed (total, base) load per slot;
rebuild_load_error_log_for_date joins it with the realised total-load roll-up and
persists per-slot forecast/actual/error rows (idempotent on slot_time_utc).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


def _seed_lp_run(*, run_at: datetime, slot_starts: list[datetime],
                 base_loads: list[float], dhw_kwhs: list[float], space_kwhs: list[float]) -> int:
    n = len(slot_starts)
    run_id = db.log_optimizer_run({
        "run_at": run_at.isoformat(), "rates_count": n,
        "cheap_slots": 0, "peak_slots": 0, "standard_slots": n, "negative_slots": 0,
        "target_vwap": 0.0, "actual_agile_mean": 0.0, "battery_warning": False,
        "strategy_summary": "test", "fox_schedule_uploaded": False, "daikin_actions_count": 0,
    })
    inputs = {
        "run_at_utc": run_at.isoformat(), "plan_date": run_at.date().isoformat(),
        "horizon_hours": 24, "soc_initial_kwh": 5.0, "tank_initial_c": 45.0, "indoor_initial_c": 21.0,
        "soc_source": "test", "tank_source": "test", "indoor_source": "test",
        "base_load_json": json.dumps(base_loads), "micro_climate_offset_c": 0.0,
        "config_snapshot_json": "{}", "price_quantize_p": 0.0,
        "peak_threshold_p": 30.0, "cheap_threshold_p": 10.0,
        "daikin_control_mode": "passive", "optimization_preset": "test",
        "energy_strategy_mode": "savings_first",
    }
    solution = [{
        "slot_index": i, "slot_time_utc": start.isoformat(), "price_p": 20.0,
        "import_kwh": 0.0, "export_kwh": 0.0, "charge_kwh": 0.0, "discharge_kwh": 0.0,
        "pv_use_kwh": 0.0, "pv_curtail_kwh": 0.0,
        "dhw_kwh": dhw_kwhs[i], "space_kwh": space_kwhs[i],
        "soc_kwh": 5.0, "tank_temp_c": 45.0, "indoor_temp_c": 21.0,
        "outdoor_temp_c": 10.0, "lwt_offset_c": 0.0,
    } for i, start in enumerate(slot_starts)]
    db.save_lp_snapshots(run_id, inputs, solution)
    return run_id


def _seed_load_samples(slot_start: datetime, load_kw: float, n: int = 6) -> None:
    for i in range(n):
        ts = slot_start + timedelta(minutes=i * (30 // n))
        db.save_pv_realtime_sample(
            captured_at=ts.isoformat().replace("+00:00", "Z"),
            solar_power_kw=0.0, soc_pct=50.0, load_power_kw=load_kw,
            grid_import_kw=0.0, grid_export_kw=0.0,
            battery_charge_kw=0.0, battery_discharge_kw=0.0, source="seed",
        )


def _slots(day: date, n: int = 4) -> list[datetime]:
    base = datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC)
    return [base + timedelta(minutes=30 * i) for i in range(n)]


def test_committed_forecast_total_and_base() -> None:
    day = date(2026, 6, 10)
    slots = _slots(day)
    _seed_lp_run(run_at=datetime(2026, 6, 10, 0, 0, tzinfo=UTC), slot_starts=slots,
                 base_loads=[0.3, 0.4, 0.5, 0.6], dhw_kwhs=[0.1, 0.0, 0.2, 0.0],
                 space_kwhs=[0.0, 0.2, 0.0, 0.1])
    out = db.committed_load_forecast_by_slot(day)
    # slot 0: base 0.3 + dhw 0.1 + space 0.0 = total 0.4
    total0, base0 = out[slots[0].isoformat()]
    assert base0 == pytest.approx(0.3)
    assert total0 == pytest.approx(0.4)
    # slot 1: base 0.4 + 0.0 + 0.2 = 0.6
    total1, base1 = out[slots[1].isoformat()]
    assert base1 == pytest.approx(0.4)
    assert total1 == pytest.approx(0.6)


def test_committed_forecast_stitches_latest_eligible_run() -> None:
    """Two solves cover the same slot; the one whose run_at <= slot_start and is
    most recent wins (the plan as known when the slot began)."""
    day = date(2026, 6, 10)
    slots = _slots(day, 2)  # 08:00, 08:30
    # Early solve at 00:00 (eligible), base 1.0
    _seed_lp_run(run_at=datetime(2026, 6, 10, 0, 0, tzinfo=UTC), slot_starts=slots,
                 base_loads=[1.0, 1.0], dhw_kwhs=[0.0, 0.0], space_kwhs=[0.0, 0.0])
    # Later solve at 07:00 (still <= 08:00), base 2.0 — should win
    _seed_lp_run(run_at=datetime(2026, 6, 10, 7, 0, tzinfo=UTC), slot_starts=slots,
                 base_loads=[2.0, 2.0], dhw_kwhs=[0.0, 0.0], space_kwhs=[0.0, 0.0])
    out = db.committed_load_forecast_by_slot(day)
    _t, base0 = out[slots[0].isoformat()]
    assert base0 == pytest.approx(2.0)  # latest eligible solve


def test_rebuild_writes_forecast_actual_error() -> None:
    day = date(2026, 6, 10)
    slots = _slots(day, 3)
    _seed_lp_run(run_at=datetime(2026, 6, 10, 0, 0, tzinfo=UTC), slot_starts=slots,
                 base_loads=[0.3, 0.4, 0.5], dhw_kwhs=[0.0, 0.0, 0.0],
                 space_kwhs=[0.0, 0.0, 0.0])
    # Actual load 1.0 kW → 0.5 kWh/slot for the first two slots
    _seed_load_samples(slots[0], 1.0)
    _seed_load_samples(slots[1], 1.0)
    written = db.rebuild_load_error_log_for_date(day)
    assert written >= 3
    rows = {r["slot_time_utc"]: r for r in db.get_load_error_log_for_date(day)}
    k0 = slots[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rows[k0]["forecast_kwh"] == pytest.approx(0.3)
    assert rows[k0]["actual_kwh"] == pytest.approx(0.5)
    assert rows[k0]["error_kwh"] == pytest.approx(0.2)  # actual - forecast
    # Slot 2 had a forecast but no actual → actual/error NULL, row still present.
    k2 = slots[2].strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rows[k2]["forecast_kwh"] == pytest.approx(0.5)
    assert rows[k2]["actual_kwh"] is None
    assert rows[k2]["error_kwh"] is None


def test_rebuild_idempotent() -> None:
    day = date(2026, 6, 10)
    slots = _slots(day, 2)
    _seed_lp_run(run_at=datetime(2026, 6, 10, 0, 0, tzinfo=UTC), slot_starts=slots,
                 base_loads=[0.3, 0.4], dhw_kwhs=[0.0, 0.0], space_kwhs=[0.0, 0.0])
    _seed_load_samples(slots[0], 1.0)
    db.rebuild_load_error_log_for_date(day)
    n_first = len(db.get_load_error_log_for_date(day))
    db.rebuild_load_error_log_for_date(day)
    n_second = len(db.get_load_error_log_for_date(day))
    assert n_first == n_second  # no duplication


def test_backfill_range() -> None:
    for d in (date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)):
        slots = _slots(d, 2)
        _seed_lp_run(run_at=datetime(d.year, d.month, d.day, 0, 0, tzinfo=UTC),
                     slot_starts=slots, base_loads=[0.3, 0.4],
                     dhw_kwhs=[0.0, 0.0], space_kwhs=[0.0, 0.0])
        _seed_load_samples(slots[0], 1.0)
    res = db.backfill_load_error_log(date(2026, 6, 8), date(2026, 6, 10))
    assert res["days"] == 3
    assert res["rows"] >= 6


def test_error_log_endpoint_shape() -> None:
    """GET /api/v1/load/error-log aggregates the persisted log per local hour."""
    import asyncio
    from src.api.routers import pv as pv_router

    today = datetime.now(UTC).date()
    slots = _slots(today, 3)
    _seed_lp_run(run_at=datetime(today.year, today.month, today.day, 0, 0, tzinfo=UTC),
                 slot_starts=slots, base_loads=[0.3, 0.4, 0.5],
                 dhw_kwhs=[0.0, 0.0, 0.0], space_kwhs=[0.0, 0.0, 0.0])
    for s in slots:
        _seed_load_samples(s, 1.2)  # actual 0.6/slot → under-forecast (positive bias)
    db.rebuild_load_error_log_for_date(today)

    resp = asyncio.run(pv_router.get_load_error_log(window_days=7))
    assert set(resp.keys()) == {"window_days", "n_slots_logged", "overall", "per_hour_local"}
    assert resp["overall"]["n"] >= 3
    assert resp["overall"]["bias_kwh"] > 0  # actual > forecast → under-forecast
    import json as _json
    _json.dumps(resp)  # JSON-serialisable
