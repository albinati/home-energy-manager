"""Compare current load forecast accuracy vs the locked baseline.

Run AFTER any change that could affect load prediction — Daikin physics
calibration (Phase B), DHW draw learning (#196 V11-C), occupancy inference
(#197 V11-D), residual_load profile changes — to verify it actually improved
point-forecast accuracy on the trailing 30 days of prod data.

A change that doesn't reduce MAE/RMSE is noise; a change that grows |bias| is
a regression even if MAE looks similar.

Usage (on prod):

    docker exec hem python /tmp/check_load_forecast_accuracy.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    from src.analytics.load_forecast_accuracy import evaluate_load_forecast_accuracy

    current = evaluate_load_forecast_accuracy(window_days=30)
    baseline_path = (
        Path(__file__).parent.parent / "tests" / "fixtures" / "load_forecast_baseline.json"
    )
    if not baseline_path.exists():
        print(f"Baseline not found at {baseline_path}; printing current only.")
        print(json.dumps(current, indent=2))
        return 0

    baseline = json.loads(baseline_path.read_text())

    print("LOAD FORECAST ACCURACY: current vs baseline\n")
    print(f"{'metric':<28} | {'baseline':>10} | {'current':>10} | {'Δ':>10}")
    print("-" * 70)
    for k in (
        "mae_kwh_per_slot",
        "rmse_kwh_per_slot",
        "bias_kwh_per_slot",
        "mape_pct",
    ):
        b = baseline["overall"].get(k, 0.0)
        c = current["overall"].get(k, 0.0)
        delta = c - b
        marker = ""
        if k in ("mae_kwh_per_slot", "rmse_kwh_per_slot", "mape_pct"):
            marker = " ⬇️ better" if delta < -0.001 else (" ⚠️ worse" if delta > 0.001 else "")
        elif k == "bias_kwh_per_slot":
            marker = " ⬇️ closer" if abs(c) < abs(b) else (" ⚠️ further" if abs(c) > abs(b) else "")
        print(f"{k:<28} | {b:>10.4f} | {c:>10.4f} | {delta:>+10.4f}{marker}")

    n_b = baseline["overall"]["n"]
    n_c = current["overall"]["n"]
    print(f"\nSamples: baseline={n_b}, current={n_c}")

    # Daikin daily diagnostic
    bd = baseline.get("daikin_daily_check", {})
    cd = current.get("daikin_daily_check", {})
    if bd.get("n_days") or cd.get("n_days"):
        print("\nDaikin daily check (predicted vs Onecta-measured):")
        print(f"{'metric':<28} | {'baseline':>10} | {'current':>10} | {'Δ':>10}")
        print("-" * 70)
        for k in ("mae_kwh_per_day", "rmse_kwh_per_day", "bias_kwh_per_day", "mape_pct"):
            b = bd.get(k, 0.0)
            c = cd.get(k, 0.0)
            print(f"{k:<28} | {b:>10.4f} | {c:>10.4f} | {c - b:>+10.4f}")

    # Verdict
    mae_b = baseline["overall"]["mae_kwh_per_slot"]
    mae_c = current["overall"]["mae_kwh_per_slot"]
    rmse_b = baseline["overall"]["rmse_kwh_per_slot"]
    rmse_c = current["overall"]["rmse_kwh_per_slot"]
    if mae_c < mae_b and rmse_c < rmse_b:
        print("\n✅ IMPROVEMENT — both MAE and RMSE reduced.")
        return 0
    if mae_c > mae_b * 1.05 or rmse_c > rmse_b * 1.05:
        print("\n❌ REGRESSION — MAE or RMSE grew >5% vs baseline.")
        return 1
    print("\n➖ NEUTRAL — accuracy roughly unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
