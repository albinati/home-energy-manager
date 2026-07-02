"""GET /api/v1/pv/today — planned-vs-realised PV roll-up + accuracy."""
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
    # No network / forecast provider in tests — degrade planned line to zero
    # so the test isolates the realised roll-up + accuracy maths.
    import src.weather as weather
    monkeypatch.setattr(weather, "fetch_forecast", lambda hours=48: [])
    yield db_path


def _seed_constant_solar(day, hour_utc: int, kw: float, *, minutes=(0, 15, 30, 45, 60)):
    """Insert constant-kW solar samples across one hour of ``day`` (UTC)."""
    for m in minutes:
        ts = datetime(day.year, day.month, day.day, hour_utc, 0, tzinfo=UTC) + timedelta(minutes=m)
        db.save_pv_realtime_sample(
            ts.isoformat().replace("+00:00", "Z"),
            solar_power_kw=kw,
            source="test",
        )


def test_solar_rollup_integrates_to_kwh():
    day = (datetime.now(UTC) - timedelta(days=1)).date()
    _seed_constant_solar(day, 10, 3.0)  # 3 kW held for one hour → ~3 kWh
    rollup = db.half_hourly_solar_kwh_for_day(day)
    total = sum(rollup.values())
    assert 2.5 <= total <= 3.5, f"expected ~3 kWh integrated, got {total}"


def test_pv_today_shape_and_accuracy():
    day = (datetime.now(UTC) - timedelta(days=1)).date()  # fully elapsed
    _seed_constant_solar(day, 10, 3.0)

    resp = asyncio.run(pv_router.get_pv_today(date=day.isoformat()))

    assert resp["date"] == day.isoformat()
    assert len(resp["slots"]) == 48
    # Every slot carries the full overlay key set; forecast is 0 (no provider
    # in test); price/load/kind are null (no rates/profile/runs seeded).
    expected_keys = {"slot_utc", "pv_forecast_kwh", "pv_planned_kwh", "pv_actual_kwh", "import_price_p", "base_load_kwh", "load_forecast_kwh", "kind"}
    assert all(set(s) == expected_keys for s in resp["slots"])
    assert all(s["pv_forecast_kwh"] == 0.0 for s in resp["slots"])
    # No optimizer run seeded → committed-plan PV is null everywhere.
    assert all(s["pv_planned_kwh"] is None for s in resp["slots"])
    assert resp["plan_committed_at"] is None

    realised = [s["pv_actual_kwh"] for s in resp["slots"] if s["pv_actual_kwh"] is not None]
    assert realised, "expected at least one slot with realised PV"
    assert 2.5 <= sum(realised) <= 3.5

    acc = resp["accuracy"]
    assert acc is not None
    assert acc["slots_compared"] >= 1
    assert acc["forecast_kwh"] == 0.0
    assert 2.5 <= acc["actual_kwh"] <= 3.5
    # Forecast is zero, so bias (actual − forecast) == realised total, and MAE
    # is the mean realised kWh per compared slot.
    assert acc["bias_kwh"] == pytest.approx(acc["actual_kwh"], abs=1e-6)


def test_pv_today_future_day_has_no_realised():
    day = (datetime.now(UTC) + timedelta(days=1)).date()  # all slots in the future
    resp = asyncio.run(pv_router.get_pv_today(date=day.isoformat()))
    assert len(resp["slots"]) == 48
    assert all(s["pv_actual_kwh"] is None for s in resp["slots"])
    assert resp["accuracy"] is None


def test_pv_today_rejects_bad_date():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(pv_router.get_pv_today(date="not-a-date"))


def test_pv_today_surfaces_committed_plan_pv():
    """The committed-plan PV (lp_solution_snapshot.pv_forecast_kwh) is surfaced
    as pv_planned_kwh, matched across the +00:00 (snapshot) vs ...Z (endpoint)
    slot-key formats, with plan_committed_at set."""
    day = (datetime.now(UTC) - timedelta(days=1)).date()
    run_at = datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)
    run_id = db.log_optimizer_run({"run_at": run_at.isoformat(), "rates_count": 48})

    # Two committed slots at 10:00 and 10:30 UTC, written in +00:00 isoformat
    # (exactly how the optimizer persists slot_time_utc).
    s0 = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)
    s1 = datetime(day.year, day.month, day.day, 10, 30, tzinfo=UTC)
    inputs_row = {
        "run_at_utc": run_at.isoformat(),
        "plan_date": day.isoformat(),
        "horizon_hours": 24,
        "soc_initial_kwh": 5.0, "tank_initial_c": 46.0, "indoor_initial_c": None,
        "soc_source": "test", "tank_source": "test", "indoor_source": "test",
        "base_load_json": "[]", "micro_climate_offset_c": 0.0,
        "config_snapshot_json": "{}",
        "price_quantize_p": 1.0, "peak_threshold_p": 25.0, "cheap_threshold_p": 12.0,
        "daikin_control_mode": "passive", "optimization_preset": "normal",
        "energy_strategy_mode": "n/a", "lp_status": "Optimal",
    }
    def _row(idx, st, pv_fc):
        return {
            "slot_index": idx, "slot_time_utc": st.isoformat(),
            "price_p": 15.0, "import_kwh": 0.0, "export_kwh": 0.0,
            "charge_kwh": 0.0, "discharge_kwh": 0.0, "pv_use_kwh": 0.0,
            "pv_curtail_kwh": 0.0, "pv_forecast_kwh": pv_fc, "dhw_kwh": 0.0,
            "space_kwh": 0.0, "soc_kwh": 5.0, "tank_temp_c": 46.0,
            "indoor_temp_c": None, "outdoor_temp_c": 12.0, "lwt_offset_c": 0.0,
        }
    solution_rows = [_row(0, s0, 1.25), _row(1, s1, 1.40)]
    db.save_lp_snapshots(run_id=run_id, inputs_row=inputs_row, solution_rows=solution_rows)

    resp = asyncio.run(pv_router.get_pv_today(date=day.isoformat()))
    by_key = {s["slot_utc"]: s for s in resp["slots"]}
    k0 = s0.isoformat().replace("+00:00", "Z")
    k1 = s1.isoformat().replace("+00:00", "Z")
    assert by_key[k0]["pv_planned_kwh"] == pytest.approx(1.25)
    assert by_key[k1]["pv_planned_kwh"] == pytest.approx(1.40)
    # Slots the run didn't cover stay null.
    other = next(s for s in resp["slots"] if s["slot_utc"] not in (k0, k1))
    assert other["pv_planned_kwh"] is None
    assert resp["plan_committed_at"] == run_at.isoformat().replace("+00:00", "Z")
    assert resp["plan_run_id"] == run_id


# ---------------------------------------------------------------------------
# Committed LOAD forecast on past days (the "forecast history disappears"
# report): /pv/today must serve the per-slot load the LP committed to on that
# day (stitched from the lp snapshots), not today's static dow×hour profile.
# ---------------------------------------------------------------------------

def _seed_lp_run_for_load(*, run_at, slot_starts, base_loads, dhw_kwhs, space_kwhs):
    import json as _json
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
        "base_load_json": _json.dumps(base_loads), "micro_climate_offset_c": 0.0,
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


def test_past_day_serves_committed_load_forecast():
    day = (datetime.now(UTC) - timedelta(days=3)).date()
    start = datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC)
    slot_starts = [start + timedelta(minutes=30 * i) for i in range(4)]
    _seed_lp_run_for_load(
        run_at=datetime(day.year, day.month, day.day, 0, 0, tzinfo=UTC),
        slot_starts=slot_starts,
        base_loads=[0.3, 0.4, 0.5, 0.6],
        dhw_kwhs=[0.1, 0.0, 0.2, 0.0],
        space_kwhs=[0.0, 0.2, 0.0, 0.1],
    )

    resp = asyncio.run(pv_router.get_pv_today(date=day.isoformat()))
    by_slot = {s["slot_utc"]: s for s in resp["slots"]}
    k0 = slot_starts[0].isoformat().replace("+00:00", "Z")
    k1 = slot_starts[1].isoformat().replace("+00:00", "Z")

    # Covered slots carry the COMMITTED values: base residual + total load.
    assert by_slot[k0]["base_load_kwh"] == pytest.approx(0.3)
    assert by_slot[k0]["load_forecast_kwh"] == pytest.approx(0.4)   # 0.3 + 0.1 dhw
    assert by_slot[k1]["base_load_kwh"] == pytest.approx(0.4)
    assert by_slot[k1]["load_forecast_kwh"] == pytest.approx(0.6)   # 0.4 + 0.2 space

    # Slots the solve did not cover have no committed total (base_load_kwh may
    # fall back to the live profile — None here, since no samples seeded).
    k_uncovered = datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    assert by_slot[k_uncovered]["load_forecast_kwh"] is None
