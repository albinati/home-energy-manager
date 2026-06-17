# Load-forecast ML feasibility — findings & recommendation

**Date:** 2026-06-17 · **Script:** `scripts/research/load_ml_feasibility.py` (read-only, dev venv)
· **Raw output:** `load_ml_feasibility_raw.md`

## Question

The user saw the cockpit **consumption** chart's load forecast lag/blur vs actual and asked
whether to enhance the load model with ML. Before building any ML infrastructure (the prod image
has no ML stack), we measured — out-of-sample, against the production v2 median baseline — whether
ML helps, and **where** the error actually lives.

## Method

- Read-only copy of the prod DB. Window **2026-05-03 → 2026-06-17** (bounded by where the measured
  Daikin split exists — needed to separate residual vs heat-pump load).
- Target: measured residual load per 30-min slot (`load − measured heat-pump`). Chronological
  70/30 train/test split (no shuffle → no leakage).
- Baselines: **v2 median** (`db.residual_load_profile_v2`, computed as-of the train end) and the
  **rejected additive corrector** (`load_bias.backtest_load_recent_bias`).
- ML candidates (dev-only sklearn): Ridge and `HistGradientBoostingRegressor` on temporal + causal
  lag features (and a weather-augmented GBM variant).

## Results (out-of-sample, test = 2026-06-03 → 06-17, 623 slots)

| model | MAE all | MAE residual slots (low-Daikin) | MAE heat-pump slots (high-Daikin) |
|---|---|---|---|
| **v2 median (prod)** | 0.191 | 0.164 | 0.216 |
| ridge temporal | 0.173 | **0.150** | 0.195 |
| gbm temporal | 0.182 | 0.153 | 0.208 |
| gbm temporal+weather | 0.183 | 0.145 | 0.218 |

- **ML beats the v2 median on residual slots by only ~6–9%** out-of-sample (0.164 → ~0.150 kWh/slot).
  Marginal, and on the *smaller* error component.
- **The rejected additive corrector made MAE −16% WORSE out-of-sample** (0.185 → 0.215) — a concrete
  reminder of how easily anything fit to this signal overfits. The modest GBM gain is real but small.
- **Error decomposition (the headline):** heat-pump-present slots carry MAE **0.241** vs **0.164** on
  residual slots — **+47% (+0.077 kWh/slot)**. The dominant, larger error is in slots where the heat
  pump runs, i.e. **heat-pump timing**, not the occupancy load.

## Recommendation

**Do not build a load ML model now.** The residual occupancy load is already near its noise floor
under the v2 median — ML buys a marginal ~6–9% MAE reduction that is (a) on the smaller error
component and (b) very unlikely to move dispatch £ (a similar-magnitude corrector was already
rejected and actually hurt out-of-sample). The bigger lever, by ~3× the error mass, is **heat-pump
timing** → this belongs to the **#540** winter-thermal / RC-learner work, not a generic load ML model.

If certainty is wanted before closing it out, the one remaining gate is an **LP-cost replay**
(`scripts/check_lp_regression.py` machinery) feeding the GBM residual forecast through historical
dispatch — but the priors (marginal MAE, rejected corrector, error-in-HP-timing) all point to a null
£ result, so it's low-value.

## Caveats (why this is directional, not definitive)

- **Thin data:** Daikin split only ~45 days, weather only ~30 days (retention), presence signal is
  effectively dead (1 row). Re-run `load_ml_feasibility.py` as history accrues for a firmer read.
- Per-slot heat-pump load is the 2-hourly measured bucket spread evenly over its four slots — a
  coarse allocation that adds noise to the high-Daikin slots specifically.
