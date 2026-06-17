# Load ML feasibility — findings

_Generated from a read-only copy of the prod DB. Window 2026-05-03..2026-06-17 (HP-split bound). Chronological 70/30 train/test. Directional — re-run as history accrues._

```

=== Residual-load forecast: MAE / bias vs measured residual (test, out-of-sample) ===
model                     MAE_all    bias  MAE_lowD  MAE_highD
v2_median (prod)           0.1912 +0.0086    0.1642     0.2164
ridge_temporal             0.1734 -0.0453    0.1498     0.1953
gbm_temporal               0.1818 -0.0200    0.1534     0.2081
gbm_temporal+weather       0.1827 +0.0063    0.1449     0.2177

Headline (low-Daikin / residual slots): v2 MAE 0.1642 -> gbm 0.1534 (+6.5% )

Error attribution (v2 residual + MEASURED hp vs total load):
  total MAE all slots : 0.2042
  MAE low-Daikin slots: 0.1643  (residual error)
  MAE high-Daikin slot: 0.2413  (residual+HP-allocation error)

Rejected additive corrector (load_bias backtest, for reference):
  in-sample MAE 0.2039->0.2096 (-2.78%, n=963) | out-of-sample MAE 0.185->0.2153 (-16.4%, n=459)

=== VERDICT ===
Marginal residual gain (7%). Unlikely to move dispatch £ — confirm with an LP-cost replay before any build.
High-Daikin slots carry ~+0.077 kWh more MAE than residual slots → the bigger lever is heat-pump TIMING (#540), not the residual load model.
```
