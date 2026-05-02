"""Compare current PV forecast accuracy vs the post-PR-#230 baseline.

Run AFTER any calibration change (cloud-aware bias, MOS regression, quantile
bands, etc.) to verify the change actually improved point-forecast accuracy
on the trailing 30 days of prod data. A change that doesn't reduce MAE/RMSE
is noise; a change that grows bias (especially > 0.1 kW absolute) is a
regression even if MAE looks similar.

Usage (on prod):

    docker exec hem python /tmp/check_pv_forecast_accuracy.py

Or copy this script into the container and run from src.weather directly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    from src.weather import evaluate_pv_forecast_accuracy

    current = evaluate_pv_forecast_accuracy(window_days=30)
    baseline_path = Path(__file__).parent.parent / "tests" / "fixtures" / "pv_forecast_baseline.json"
    if not baseline_path.exists():
        print(f"Baseline not found at {baseline_path}; printing current only.")
        print(json.dumps(current, indent=2))
        return 0

    baseline = json.loads(baseline_path.read_text())

    print("PV FORECAST ACCURACY: current vs baseline (post-PR-#230)\n")
    print(f"{'metric':<15} | {'baseline':>10} | {'current':>10} | {'Δ':>10}")
    print("-" * 55)
    for k in ("mae_kw", "rmse_kw", "bias_kw", "mape_pct"):
        b = baseline["overall"].get(k, 0.0)
        c = current["overall"].get(k, 0.0)
        delta = c - b
        marker = ""
        if k in ("mae_kw", "rmse_kw", "mape_pct"):
            marker = " ⬇️ better" if delta < -0.001 else (" ⚠️ worse" if delta > 0.001 else "")
        elif k == "bias_kw":
            marker = " ⬇️ closer" if abs(c) < abs(b) else (" ⚠️ further" if abs(c) > abs(b) else "")
        print(f"{k:<15} | {b:>10.4f} | {c:>10.4f} | {delta:>+10.4f}{marker}")

    n_b = baseline["overall"]["n"]
    n_c = current["overall"]["n"]
    print(f"\nSamples: baseline={n_b}, current={n_c}")

    # Verdict
    mae_b = baseline["overall"]["mae_kw"]
    mae_c = current["overall"]["mae_kw"]
    rmse_b = baseline["overall"]["rmse_kw"]
    rmse_c = current["overall"]["rmse_kw"]
    if mae_c < mae_b and rmse_c < rmse_b:
        print("\n✅ IMPROVEMENT — both MAE and RMSE reduced.")
        return 0
    if mae_c > mae_b * 1.05 or rmse_c > rmse_b * 1.05:
        print("\n❌ REGRESSION — MAE or RMSE grew >5% vs baseline.")
        return 1
    print("\n➖ NEUTRAL — accuracy roughly unchanged. Consider whether the change is worth shipping.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
