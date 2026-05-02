"""Regression-baseline evaluator for load forecast accuracy (#231 analog).

Mirrors tests/test_pv_forecast_accuracy.py — locks the dict shape so any future
calibration improvement (Phase B Daikin physics correction, etc.) can be
measured against a stable baseline.
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


def _seed_lp_run(
    *,
    run_at: datetime,
    slot_starts: list[datetime],
    base_loads: list[float],
    dhw_kwhs: list[float],
    space_kwhs: list[float],
) -> int:
    """Insert one optimizer_log + matching lp_inputs_snapshot + lp_solution_snapshot rows.

    All three lists must be the same length (one per slot).
    """
    n = len(slot_starts)
    assert len(base_loads) == n == len(dhw_kwhs) == len(space_kwhs)

    run_id = db.log_optimizer_run({
        "run_at": run_at.isoformat(),
        "rates_count": n,
        "cheap_slots": 0, "peak_slots": 0, "standard_slots": n, "negative_slots": 0,
        "target_vwap": 0.0, "actual_agile_mean": 0.0,
        "battery_warning": False, "strategy_summary": "test",
        "fox_schedule_uploaded": False, "daikin_actions_count": 0,
    })

    inputs = {
        "run_at_utc": run_at.isoformat(),
        "plan_date": run_at.date().isoformat(),
        "horizon_hours": 24,
        "soc_initial_kwh": 5.0, "tank_initial_c": 45.0, "indoor_initial_c": 21.0,
        "soc_source": "test", "tank_source": "test", "indoor_source": "test",
        "base_load_json": json.dumps(base_loads),
        "micro_climate_offset_c": 0.0,
        "config_snapshot_json": "{}",
        "price_quantize_p": 0.0, "peak_threshold_p": 30.0, "cheap_threshold_p": 10.0,
        "daikin_control_mode": "passive", "optimization_preset": "test",
        "energy_strategy_mode": "savings_first",
    }
    solution = []
    for i, start in enumerate(slot_starts):
        solution.append({
            "slot_index": i,
            "slot_time_utc": start.isoformat(),
            "price_p": 20.0,
            "import_kwh": 0.0, "export_kwh": 0.0,
            "charge_kwh": 0.0, "discharge_kwh": 0.0,
            "pv_use_kwh": 0.0, "pv_curtail_kwh": 0.0,
            "dhw_kwh": dhw_kwhs[i], "space_kwh": space_kwhs[i],
            "soc_kwh": 5.0, "tank_temp_c": 45.0, "indoor_temp_c": 21.0,
            "outdoor_temp_c": 10.0, "lwt_offset_c": 0.0,
        })
    db.save_lp_snapshots(run_id, inputs, solution)
    return run_id


def _seed_load_samples(slot_start: datetime, load_kw: float, n_samples: int = 6) -> None:
    """Seed n_samples load_power_kw rows inside a 30-min slot window."""
    step_min = 30 // n_samples
    for i in range(n_samples):
        ts = slot_start + timedelta(minutes=i * step_min)
        db.save_pv_realtime_sample(
            captured_at=ts.isoformat().replace("+00:00", "Z"),
            solar_power_kw=0.0, soc_pct=50.0, load_power_kw=load_kw,
            grid_import_kw=0.0, grid_export_kw=0.0,
            battery_charge_kw=0.0, battery_discharge_kw=0.0, source="seed",
        )


def test_evaluator_returns_zero_when_no_data() -> None:
    from src.analytics.load_forecast_accuracy import evaluate_load_forecast_accuracy
    out = evaluate_load_forecast_accuracy(window_days=7)
    assert out["n_paired"] == 0
    assert out["overall"]["n"] == 0
    assert out["per_hour_local"] == {}
    assert out["daikin_daily_check"]["n_days"] == 0


def test_perfect_prediction_zero_error() -> None:
    """Predicted total = base_load + dhw + space. If actual matches exactly,
    MAE/RMSE/bias should all be ~0."""
    from src.analytics.load_forecast_accuracy import evaluate_load_forecast_accuracy

    yesterday = date.today() - timedelta(days=1)
    slot_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC).replace(hour=12)
    # Predicted: 0.20 base + 0.05 dhw + 0.10 space = 0.35 kWh per slot
    # Actual at 0.70 kW × 0.5 h = 0.35 kWh per slot — perfect match
    _seed_lp_run(
        run_at=slot_start - timedelta(hours=1),
        slot_starts=[slot_start],
        base_loads=[0.20], dhw_kwhs=[0.05], space_kwhs=[0.10],
    )
    _seed_load_samples(slot_start, load_kw=0.70)

    out = evaluate_load_forecast_accuracy(window_days=2)
    assert out["n_paired"] >= 1
    assert out["overall"]["mae_kwh_per_slot"] < 0.01
    assert abs(out["overall"]["bias_kwh_per_slot"]) < 0.01


def test_systematic_over_prediction_shows_negative_bias() -> None:
    """Predicted 0.55 but actual 0.22 — LP over-predicts (the live spot-check pattern).
    bias_kwh_per_slot should be NEGATIVE (actual − predicted < 0)."""
    from src.analytics.load_forecast_accuracy import evaluate_load_forecast_accuracy

    yesterday = date.today() - timedelta(days=1)
    slot_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC).replace(hour=3)
    # Predicted: 0.10 base + 0.20 dhw + 0.25 space = 0.55 kWh
    # Actual: 0.44 kW × 0.5 = 0.22 kWh
    _seed_lp_run(
        run_at=slot_start - timedelta(hours=1),
        slot_starts=[slot_start],
        base_loads=[0.10], dhw_kwhs=[0.20], space_kwhs=[0.25],
    )
    _seed_load_samples(slot_start, load_kw=0.44)

    out = evaluate_load_forecast_accuracy(window_days=2)
    assert out["overall"]["bias_kwh_per_slot"] < -0.2, (
        f"expected over-prediction (negative bias), got {out['overall']['bias_kwh_per_slot']}"
    )


def test_per_hour_breakdown_uses_local_time() -> None:
    """Hour buckets should be local (BULLETPROOF_TIMEZONE) since load is occupancy-driven.
    UTC 03:00 in Europe/London BST is local 04:00."""
    from src.analytics.load_forecast_accuracy import evaluate_load_forecast_accuracy

    yesterday = date.today() - timedelta(days=1)
    # Pick 03:00 UTC — in BST that's 04:00 local. (In GMT it's 03:00 local.)
    slot_start_utc = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC).replace(hour=3)
    _seed_lp_run(
        run_at=slot_start_utc - timedelta(hours=1),
        slot_starts=[slot_start_utc],
        base_loads=[0.30], dhw_kwhs=[0.10], space_kwhs=[0.10],
    )
    _seed_load_samples(slot_start_utc, load_kw=0.20)

    out = evaluate_load_forecast_accuracy(window_days=2)
    assert len(out["per_hour_local"]) == 1
    # Either 3 (GMT) or 4 (BST) is acceptable — exact value depends on DST
    h = next(iter(out["per_hour_local"].keys()))
    assert h in (3, 4)
    assert out["per_hour_local"][h]["n"] == 1


def test_dual_window_uses_latest_run_id() -> None:
    """When two LP runs predict the same slot, the evaluator must pick the latest."""
    from src.analytics.load_forecast_accuracy import evaluate_load_forecast_accuracy

    yesterday = date.today() - timedelta(days=1)
    slot_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC).replace(hour=14)

    # Older run: predicts 0.80 (badly wrong)
    _seed_lp_run(
        run_at=slot_start - timedelta(hours=12),
        slot_starts=[slot_start],
        base_loads=[0.50], dhw_kwhs=[0.15], space_kwhs=[0.15],
    )
    # Newer run: predicts 0.30 (close to actual)
    _seed_lp_run(
        run_at=slot_start - timedelta(minutes=30),
        slot_starts=[slot_start],
        base_loads=[0.20], dhw_kwhs=[0.05], space_kwhs=[0.05],
    )
    _seed_load_samples(slot_start, load_kw=0.60)  # actual = 0.30 kWh

    out = evaluate_load_forecast_accuracy(window_days=2)
    # Should pair against the newer 0.30 prediction → near-zero error
    assert out["n_paired"] == 1
    assert out["overall"]["mae_kwh_per_slot"] < 0.05


def test_daikin_daily_check_compares_predicted_vs_onecta() -> None:
    """Predicted Daikin daily = Σ (dhw + space) across slots. Compared against
    daikin_consumption_daily.kwh_total. Surfaces the daily physics-model bias."""
    from src.analytics.load_forecast_accuracy import evaluate_load_forecast_accuracy

    yesterday = date.today() - timedelta(days=1)
    base = datetime.combine(yesterday, datetime.min.time(), tzinfo=UTC)
    slots = [base.replace(hour=h) for h in range(0, 24, 6)]  # 4 slots
    # Each slot predicts 0.50 dhw + 0.50 space = 1.00 → daily total predicted = 4.0 kWh
    _seed_lp_run(
        run_at=base - timedelta(hours=1),
        slot_starts=slots,
        base_loads=[0.0] * 4, dhw_kwhs=[0.5] * 4, space_kwhs=[0.5] * 4,
    )
    # Actual Onecta says 3.0 kWh — physics over-predicted by 1.0 kWh
    db.upsert_daikin_consumption_daily(
        date=yesterday.isoformat(), kwh_total=3.0, source="onecta",
    )

    out = evaluate_load_forecast_accuracy(window_days=2)
    chk = out["daikin_daily_check"]
    assert chk["n_days"] == 1
    assert chk["mean_predicted_daikin_kwh_per_day"] == pytest.approx(4.0, abs=0.01)
    assert chk["mean_actual_daikin_kwh_per_day"] == pytest.approx(3.0, abs=0.01)
    # Onecta < predicted → bias = actual − pred = -1.0 (physics over-predicts)
    assert chk["bias_kwh_per_day"] == pytest.approx(-1.0, abs=0.01)
