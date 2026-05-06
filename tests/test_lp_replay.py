"""LP replay/backtest harness tests.

Layer 1 (single-run replay), Layer 2 (chained day replay), Layer 3 (cadence
sweep). Builds synthetic snapshots so we don't depend on prod history.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config as app_config
from src.scheduler.lp_optimizer import LpInitialState, LpPlan, solve_lp
from src.scheduler.lp_replay import (
    LpDayReplayResult,
    _apply_cadence_filter,
    list_run_ids_for_date,
    replay_day,
    replay_run,
    resolve_run_id_for_date,
    sweep_cadences,
)
from src.weather import (
    HourlyForecast,
    WeatherLpSeries,
    compute_heating_demand_factor,
    estimate_pv_kw,
    forecast_to_lp_inputs,
)


@pytest.fixture(autouse=True)
def _db_ready():
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _fast_solver(monkeypatch):
    """Match test_lp_optimizer's fixture so solves are fast and deterministic."""
    monkeypatch.setattr(app_config, "LP_CBC_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)


# ---------------------------------------------------------------------------
# Helpers — build a LpPlan, then persist it as if it were the original solve.
# ---------------------------------------------------------------------------

def _build_forecast(slots: list[datetime], temp_c: float = 10.0, rad_wm2: float = 400.0) -> list[HourlyForecast]:
    """Hourly forecast covering the slot range — replay reads these back from DB."""
    out: list[HourlyForecast] = []
    seen_hours: set[datetime] = set()
    for st in slots:
        hour_anchor = st.replace(minute=0, second=0, microsecond=0)
        if hour_anchor in seen_hours:
            continue
        seen_hours.add(hour_anchor)
        out.append(HourlyForecast(
            time_utc=hour_anchor,
            temperature_c=temp_c,
            cloud_cover_pct=0.0,  # matches what replay reconstructs from snapshot
            shortwave_radiation_wm2=rad_wm2,
            estimated_pv_kw=estimate_pv_kw(rad_wm2),
            heating_demand_factor=compute_heating_demand_factor(temp_c),
        ))
    return out


def _series(n: int, base: datetime) -> tuple[list[datetime], WeatherLpSeries, list[HourlyForecast]]:
    """Build slots + WeatherLpSeries through the SAME path replay uses.

    Replay rebuilds WeatherLpSeries by calling forecast_to_lp_inputs on rows
    pulled from meteo_forecast_history. To make the round-trip test fair we
    drive the original solve through the same function so PV/COP arrays match.
    """
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    forecast = _build_forecast(slots)
    w = forecast_to_lp_inputs(forecast, slots, pv_scale=1.0)
    return slots, w, forecast


def _solve_baseline(
    n: int,
    base: datetime,
    *,
    soc0: float = 4.0,
    tank0: float = 44.0,
    indoor0: float = 20.5,
    prices: list[float] | None = None,
    base_load: list[float] | None = None,
) -> tuple[LpPlan, list[datetime], list[float], list[float], LpInitialState, list[HourlyForecast]]:
    slots, w, forecast = _series(n, base)
    if prices is None:
        prices = [12.0] * n
    if base_load is None:
        base_load = [0.4] * n
    initial = LpInitialState(soc_kwh=soc0, tank_temp_c=tank0, indoor_temp_c=indoor0)
    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=w,
        initial=initial,
        tz=ZoneInfo("Europe/London"),
    )
    return plan, slots, prices, base_load, initial, forecast


def _persist_plan_as_run(
    plan: LpPlan,
    slots: list[datetime],
    prices: list[float],
    base_load: list[float],
    initial: LpInitialState,
    forecast: list[HourlyForecast],
    *,
    run_at_utc: datetime,
    plan_date: str,
    config_snapshot: dict | None = None,
) -> int:
    """Insert optimizer_log + lp_inputs_snapshot + lp_solution_snapshot + meteo_forecast_history rows."""
    run_id = db.log_optimizer_run({
        "run_at": run_at_utc.isoformat(),
        "rates_count": len(slots),
        "cheap_slots": 0,
        "peak_slots": 0,
        "standard_slots": len(slots),
        "negative_slots": 0,
        "target_vwap": float(sum(prices) / len(prices)) if prices else 0.0,
        "actual_agile_mean": float(sum(prices) / len(prices)) if prices else 0.0,
        "battery_warning": False,
        "strategy_summary": "test",
        "fox_schedule_uploaded": False,
        "daikin_actions_count": 0,
    })

    inputs_row = {
        "run_at_utc": run_at_utc.isoformat(),
        "plan_date": plan_date,
        "horizon_hours": int(len(slots) * 0.5),
        "soc_initial_kwh": initial.soc_kwh,
        "tank_initial_c": initial.tank_temp_c,
        "indoor_initial_c": initial.indoor_temp_c,
        "soc_source": initial.soc_source,
        "tank_source": initial.tank_source,
        "indoor_source": initial.indoor_source,
        "base_load_json": json.dumps(base_load),
        "micro_climate_offset_c": 0.0,
        "config_snapshot_json": json.dumps(config_snapshot or {}),
        "price_quantize_p": 1.0,
        "peak_threshold_p": float(plan.peak_threshold_pence),
        "cheap_threshold_p": float(plan.cheap_threshold_pence),
        "daikin_control_mode": "active",
        "optimization_preset": "normal",
        "energy_strategy_mode": "savings_first",
    }

    solution_rows: list[dict] = []
    for i, st in enumerate(plan.slot_starts_utc):
        solution_rows.append({
            "slot_index": i,
            "slot_time_utc": st.isoformat(),
            "price_p": float(plan.price_pence[i]),
            "import_kwh": float(plan.import_kwh[i]),
            "export_kwh": float(plan.export_kwh[i]),
            "charge_kwh": float(plan.battery_charge_kwh[i]),
            "discharge_kwh": float(plan.battery_discharge_kwh[i]),
            "pv_use_kwh": float(plan.pv_use_kwh[i]),
            "pv_curtail_kwh": float(plan.pv_curtail_kwh[i]),
            "dhw_kwh": float(plan.dhw_electric_kwh[i]),
            "space_kwh": float(plan.space_electric_kwh[i]),
            "soc_kwh": float(plan.soc_kwh[i + 1]) if i + 1 < len(plan.soc_kwh) else 0.0,
            "tank_temp_c": float(plan.tank_temp_c[i + 1]) if i + 1 < len(plan.tank_temp_c) else 0.0,
            "indoor_temp_c": float(plan.indoor_temp_c[i + 1]) if i + 1 < len(plan.indoor_temp_c) else 0.0,
            "outdoor_temp_c": float(plan.temp_outdoor_c[i]) if i < len(plan.temp_outdoor_c) else 10.0,
            "lwt_offset_c": float(plan.lwt_offset_c[i]) if i < len(plan.lwt_offset_c) else 0.0,
        })

    db.save_lp_snapshots(run_id, inputs_row, solution_rows)

    # Weather snapshot — fetched a second BEFORE run_at_utc so
    # get_meteo_forecast_history_latest_before(run_at_utc) finds it. Round-trip
    # the test's hourly forecast list so replay reconstructs an identical series.
    fetch_at = (run_at_utc - timedelta(seconds=1)).isoformat()
    forecast_rows = [
        {
            "slot_time": f.time_utc.isoformat(),
            "temp_c": float(f.temperature_c),
            "solar_w_m2": float(f.shortwave_radiation_wm2),
            "cloud_cover_pct": float(f.cloud_cover_pct),
        }
        for f in forecast
    ]
    db.save_meteo_forecast_history(fetch_at, forecast_rows)

    return run_id


# ---------------------------------------------------------------------------
# Layer 1 tests — replay_run
# ---------------------------------------------------------------------------

def test_replay_run_honest_round_trip_matches_original_within_tolerance():
    """Honest mode on identical inputs should reproduce the original plan.

    We assert per-slot dispatch parity (the strong signal) and cost-at-actual
    parity (the £-PnL signal). We do NOT assert objective_pence parity because
    the snapshot's reconstructed objective only includes the grid component
    (Σ import·price − Σ export·export_price), while the LP's true objective
    also includes cycle / comfort / tank-overshoot soft penalties — they are
    not equal even at zero solver noise.
    """
    base = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    plan, slots, prices, base_load, initial, forecast = _solve_baseline(12, base)
    run_id = _persist_plan_as_run(
        plan, slots, prices, base_load, initial, forecast,
        run_at_utc=base, plan_date="2026-07-01",
    )

    result = replay_run(run_id, mode="honest")
    assert result.ok, result.error
    assert result.run_id == run_id
    assert result.plan_date == "2026-07-01"
    # Per-slot dispatch should match — every d_*_kwh near zero.
    for d in result.slot_diffs:
        for key in ("d_import_kwh", "d_export_kwh", "d_charge_kwh", "d_discharge_kwh", "d_dhw_kwh", "d_space_kwh"):
            assert abs(d[key]) < 0.01, f"slot {d['slot_index']} {key}={d[key]}"
    # Cost priced at actual rates: original and replayed must match — same
    # dispatch on the same prices. (The snapshot's price_p IS the actual price
    # since Octopus publishes day-ahead and we replay against the same row.)
    assert abs(result.delta_cost_at_actual_p) < 0.5


def test_replay_run_v11a_honest_fidelity_when_cloud_cover_persisted():
    """V11-A (#194): when meteo_forecast_history rows carry cloud_cover_pct,
    replay should report ``weather_fidelity == 'honest'`` rather than 'approx'.
    Pre-V11-A snapshots (NULL cloud) keep the legacy 'approx' label.
    """
    base = datetime(2026, 7, 4, 0, 0, tzinfo=UTC)
    # Build a forecast carrying real cloud cover (non-zero).
    slots = [base + timedelta(minutes=30 * i) for i in range(8)]
    forecast = []
    seen: set[datetime] = set()
    for st in slots:
        anchor = st.replace(minute=0, second=0, microsecond=0)
        if anchor in seen:
            continue
        seen.add(anchor)
        forecast.append(HourlyForecast(
            time_utc=anchor, temperature_c=12.0, cloud_cover_pct=45.0,
            shortwave_radiation_wm2=420.0,
            estimated_pv_kw=estimate_pv_kw(420.0),
            heating_demand_factor=compute_heating_demand_factor(12.0),
        ))
    w = forecast_to_lp_inputs(forecast, slots, pv_scale=1.0)
    initial = LpInitialState(soc_kwh=4.0, tank_temp_c=44.0, indoor_temp_c=20.5)
    plan = solve_lp(
        slot_starts_utc=slots, price_pence=[12.0] * 8, base_load_kwh=[0.4] * 8,
        weather=w, initial=initial, tz=ZoneInfo("Europe/London"),
    )
    run_id = _persist_plan_as_run(
        plan, slots, [12.0] * 8, [0.4] * 8, initial, forecast,
        run_at_utc=base, plan_date="2026-07-04",
    )

    result = replay_run(run_id, mode="honest")
    assert result.ok, result.error
    assert result.weather_fidelity == "honest", (
        f"expected 'honest' fidelity when cloud_cover_pct persisted, "
        f"got {result.weather_fidelity}"
    )


def test_replay_run_falls_back_to_approx_when_cloud_cover_null():
    """Pre-V11-A snapshots (cloud_cover_pct stored as NULL) → 'approx' fidelity,
    and replay still succeeds. Backwards-compat for snapshots collected before
    this PR landed."""
    base = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    plan, slots, prices, base_load, initial, forecast = _solve_baseline(8, base)
    run_id = _persist_plan_as_run(
        plan, slots, prices, base_load, initial, forecast,
        run_at_utc=base, plan_date="2026-07-04",
    )
    # Strip the cloud_cover_pct column to simulate a pre-V11-A row.
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE meteo_forecast_history SET cloud_cover_pct = NULL "
            "WHERE forecast_fetch_at_utc < ?",
            (base.isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()

    result = replay_run(run_id, mode="honest")
    assert result.ok, result.error
    assert result.weather_fidelity == "approx"


def test_replay_run_missing_weather_returns_clean_error():
    """A run with no meteo_forecast_history snapshot should fail-fast cleanly."""
    base = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    plan, slots, prices, base_load, initial, _forecast = _solve_baseline(8, base)

    # Persist run + snapshots but DON'T add meteo_forecast_history.
    run_id = db.log_optimizer_run({
        "run_at": base.isoformat(), "rates_count": 8,
        "cheap_slots": 0, "peak_slots": 0, "standard_slots": 8, "negative_slots": 0,
        "target_vwap": 12.0, "actual_agile_mean": 12.0, "battery_warning": False,
        "strategy_summary": "no-weather", "fox_schedule_uploaded": False, "daikin_actions_count": 0,
    })
    inputs_row = {
        "run_at_utc": base.isoformat(),
        "plan_date": "2026-07-01",
        "horizon_hours": 4,
        "soc_initial_kwh": initial.soc_kwh,
        "tank_initial_c": initial.tank_temp_c,
        "indoor_initial_c": initial.indoor_temp_c,
        "soc_source": "test", "tank_source": "test", "indoor_source": "test",
        "base_load_json": json.dumps(base_load),
        "micro_climate_offset_c": 0.0,
        "config_snapshot_json": json.dumps({}),
        "price_quantize_p": 1.0, "peak_threshold_p": 0.0, "cheap_threshold_p": 0.0,
        "daikin_control_mode": "active", "optimization_preset": "normal",
        "energy_strategy_mode": "savings_first",
    }
    solution_rows = []
    for i, st in enumerate(slots):
        solution_rows.append({
            "slot_index": i, "slot_time_utc": st.isoformat(),
            "price_p": prices[i], "import_kwh": 0.0, "export_kwh": 0.0,
            "charge_kwh": 0.0, "discharge_kwh": 0.0, "pv_use_kwh": 0.0, "pv_curtail_kwh": 0.0,
            "dhw_kwh": 0.0, "space_kwh": 0.0, "soc_kwh": 4.0, "tank_temp_c": 44.0,
            "indoor_temp_c": 20.5, "outdoor_temp_c": 10.0, "lwt_offset_c": 0.0,
        })
    db.save_lp_snapshots(run_id, inputs_row, solution_rows)

    result = replay_run(run_id, mode="honest")
    assert not result.ok
    assert result.error is not None
    assert "weather snapshot missing" in result.error


def test_replay_run_unknown_run_id_returns_error():
    result = replay_run(999999, mode="honest")
    assert not result.ok
    assert "no lp_inputs_snapshot" in (result.error or "")


def test_replay_run_forward_mode_changes_under_config_drift(monkeypatch):
    """Forward mode uses today's config; mutating config should change the result."""
    base = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
    plan, slots, prices, base_load, initial, forecast = _solve_baseline(8, base)
    # Capture current battery capacity so we can stash it in the snapshot.
    snap_capacity = float(app_config.BATTERY_CAPACITY_KWH)
    run_id = _persist_plan_as_run(
        plan, slots, prices, base_load, initial, forecast,
        run_at_utc=base, plan_date="2026-07-02",
        config_snapshot={"BATTERY_CAPACITY_KWH": snap_capacity},
    )

    # Honest replay should match.
    honest = replay_run(run_id, mode="honest")
    assert honest.ok

    # Forward replay with reduced battery → different decisions / objective.
    monkeypatch.setattr(app_config, "BATTERY_CAPACITY_KWH", snap_capacity * 0.5)
    forward = replay_run(run_id, mode="forward")
    assert forward.ok
    # We don't assert a specific delta sign — just that it differs measurably.
    assert abs(forward.replayed_objective_pence - honest.replayed_objective_pence) > 1e-6


# ---------------------------------------------------------------------------
# Layer 2 tests — replay_day chain
# ---------------------------------------------------------------------------

def test_resolve_run_id_for_date_picks_first_run_of_local_day():
    base_utc = datetime(2026, 7, 3, 4, 0, tzinfo=UTC)  # 05:00 BST
    plan, slots, prices, base_load, initial, forecast = _solve_baseline(8, base_utc)
    rid_a = _persist_plan_as_run(
        plan, slots, prices, base_load, initial, forecast,
        run_at_utc=base_utc, plan_date="2026-07-03",
    )
    later = base_utc + timedelta(hours=6)
    plan2, slots2, _, _, _, forecast2 = _solve_baseline(8, later)
    rid_b = _persist_plan_as_run(
        plan2, slots2, prices, base_load, initial, forecast2,
        run_at_utc=later, plan_date="2026-07-03",
    )

    assert resolve_run_id_for_date("2026-07-03", which="first") == rid_a
    assert resolve_run_id_for_date("2026-07-03", which="last") == rid_b
    assert resolve_run_id_for_date("not-a-date") is None
    assert resolve_run_id_for_date("2026-07-99") is None  # ISO parser rejects


def test_replay_day_original_cadence_chains_runs_and_produces_active_slots():
    """Two runs through the day, chained, scoring the chained dispatch.

    Uses afternoon UTC + warmer initial state so the second run remains feasible
    once the first run's plan-tail state is propagated into it. This is the
    intended fidelity behaviour of multi-run replay — but for solver feasibility
    we need inputs with enough slack that drift doesn't hit a binding constraint.
    """
    base_utc = datetime(2026, 7, 4, 11, 0, tzinfo=UTC)

    # Run 1 at 12:00 BST (covers 4 slots = 2 hrs). Warm start, mid SoC.
    plan1, s1, p1, bl1, init1, fc1 = _solve_baseline(
        4, base_utc, soc0=6.0, tank0=48.0, indoor0=21.5,
    )
    _persist_plan_as_run(
        plan1, s1, p1, bl1, init1, fc1,
        run_at_utc=base_utc, plan_date="2026-07-04",
    )

    # Run 2 at 14:00 BST — covers next 4 slots
    t2 = base_utc + timedelta(hours=2)
    plan2, s2, p2, bl2, init2, fc2 = _solve_baseline(
        4, t2, soc0=6.0, tank0=48.0, indoor0=21.5,
    )
    _persist_plan_as_run(
        plan2, s2, p2, bl2, init2, fc2,
        run_at_utc=t2, plan_date="2026-07-04",
    )

    result = replay_day("2026-07-04", cadence="original", mode="honest")
    assert result.ok, result.error
    assert len(result.recalc_run_ids) == 2
    assert len(result.runs) == 2
    for r in result.runs:
        assert r.ok, r.error
    # Active slots: run 1 covers [t0, t2), run 2 covers [t2, end-of-day). Run 1's
    # plan has 4 slots all in [t0, t2). Run 2's plan has 4 slots — all active.
    assert len(result.active_slots) == 8
    assert isinstance(result.total_replayed_cost_p, float)
    assert isinstance(result.total_original_cost_p, float)


def test_replay_day_unknown_date_returns_clean_error():
    result = replay_day("2099-12-31", cadence="original", mode="honest")
    assert not result.ok
    assert "no optimizer_log rows" in (result.error or "")


def test_replay_day_bad_cadence_string_surfaced():
    base_utc = datetime(2026, 7, 5, 0, 0, tzinfo=UTC)
    plan, s, p, bl, init, fc = _solve_baseline(4, base_utc)
    _persist_plan_as_run(plan, s, p, bl, init, fc, run_at_utc=base_utc, plan_date="2026-07-05")

    result = replay_day("2026-07-05", cadence="hourly", mode="honest")
    assert not result.ok
    assert "bad cadence" in (result.error or "")


# ---------------------------------------------------------------------------
# Cadence DSL parser
# ---------------------------------------------------------------------------

def test_apply_cadence_filter_dsl():
    runs = [(1, "t1"), (2, "t2"), (3, "t3"), (4, "t4"), (5, "t5")]

    assert _apply_cadence_filter(runs, "original") == runs
    assert _apply_cadence_filter(runs, "first") == [(1, "t1")]
    assert _apply_cadence_filter(runs, "first:3") == runs[:3]
    assert _apply_cadence_filter(runs, "stride:2") == [(1, "t1"), (3, "t3"), (5, "t5")]
    assert _apply_cadence_filter(runs, "subset:0,2,4") == [(1, "t1"), (3, "t3"), (5, "t5")]
    # Empty subset is allowed (caller surfaces "matched zero runs").
    assert _apply_cadence_filter(runs, "subset:") == []

    with pytest.raises(ValueError):
        _apply_cadence_filter(runs, "first:zero")
    with pytest.raises(ValueError):
        _apply_cadence_filter(runs, "first:-1")
    with pytest.raises(ValueError):
        _apply_cadence_filter(runs, "stride:0")
    with pytest.raises(ValueError):
        _apply_cadence_filter(runs, "garbage")


# ---------------------------------------------------------------------------
# Layer 3 tests — sweep_cadences
# ---------------------------------------------------------------------------

def test_sweep_cadences_runs_all_cadences_and_picks_a_winner():
    base_utc = datetime(2026, 7, 6, 11, 0, tzinfo=UTC)
    # Three runs spaced 2h apart, with mildly different prices so cadence
    # subsets diverge measurably. Afternoon + warm state for chain feasibility.
    for i in range(3):
        t = base_utc + timedelta(hours=2 * i)
        prices = [12.0 + i] * 4  # different per run
        plan, s, p, bl, init, fc = _solve_baseline(
            4, t, soc0=6.0, tank0=48.0, indoor0=21.5, prices=prices,
        )
        _persist_plan_as_run(plan, s, p, bl, init, fc, run_at_utc=t, plan_date="2026-07-06")

    result = sweep_cadences(
        "2026-07-06",
        cadences=["original", "first", "stride:2"],
        mode="honest",
    )
    assert result.ok, result.error
    assert len(result.rows) == 3
    assert all(r.ok for r in result.rows)
    assert result.best_cadence_label in ("original", "first", "stride:2")


def test_sweep_cadences_no_runs_for_date_returns_error_per_row_then_top_level_failure():
    result = sweep_cadences("2099-12-31", cadences=["original"], mode="honest")
    assert not result.ok
    assert result.rows and not result.rows[0].ok
