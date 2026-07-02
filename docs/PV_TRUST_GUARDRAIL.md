# PV-Trust Guard Rail — design doc

**Status:** decision made — ship the hard rail + daily PV-calibration refresh.
**Owner:** Luis
**Last revised:** 2026-05-15

## 1. Incident — 2026-05-15

The LP solved at **06:55 UTC** and committed `cheap` (grid → battery) slots
for 08:00–09:30 UTC. By 10:00 UTC the battery was 86% SoC, by 12:00 UTC it
was 100%, and from then onward every mid-day PV kWh has been exported at
the Outgoing rate instead of self-consumed.

| Slot (UTC) | Solar kW | Load kW | Grid imp kW | Grid exp kW | Batt chg kW | SoC % |
|---|---|---|---|---|---|---|
| 07:30 | 0.7 | 0.9 | 0.3 | — | 0.1 | 8 |
| **08:00** | **1.2** | **3.2** | **6.1** | — | **4.1** | **15** |
| **08:30** | **0.8** | **3.4** | **6.4** | — | **3.7** | **33** |
| **09:00** | **0.7** | **0.6** | **3.6** | — | **3.6** | **50** |
| **09:30** | **1.3** | **0.7** | **3.6** | — | **4.2** | **69** |
| 10:00 | 2.1 | 0.6 | 0.8 | — | 2.1 | 86 |
| 11:30 | 2.1 | 0.7 | 0 | 0.5 | 1.5 | 96 |
| **12:00** | **2.6** | **0.7** | — | **1.9** | — | **100** |
| 13:00 | 2.6 | 0.7 | — | 1.9 | — | 100 |
| 14:00 | 0.95 | 0.6 | — | 0.3 | — | 100 |
| 16:00 | 1.5 | 0.6 | — | 0.9 | — | 99 |

Cost of the bad call:
- Grid imported 08:00–10:00 UTC: ~14 kWh @ avg 14.3 p/kWh → **£2.00 spend**
- PV exported once battery was full (12:00–16:00 UTC, and counting):
  ~6 kWh at ~10 p/kWh of opportunity loss = ~£0.60

## 2. Root cause — two layered failures

### 2a — The system's existing per-hour calibration was 7 days stale

`pv_calibration_hourly` is the per-UTC-hour factor table that translates
Open-Meteo / Quartz radiance forecasts into expected per-slot PV kWh. It
was **last refreshed 2026-05-08** with a 14-day window, **7 days stale** by
2026-05-15. The W4 1DZ site has a strong AM-over / PM-under asymmetry:

| hours UTC | residual `actual/predicted` ratio (last 30 days) |
|---|---|
| AM (5–11) | **0.65** mean — forecast over-predicts mornings by 35% |
| PM (12–18) | **1.11** mean — forecast under-predicts afternoons by 11% |

And the bias is **not stable week-to-week**:

| | AM mean | PM mean |
|---|---:|---:|
| Week 19 (May 4–10) | 0.67 | **1.22** |
| Week 20 (May 11–17) | 0.62 | **0.98** |

**AM bias** is structural — likely sub-optimal angle-of-incidence on the
aggregate SSW array; an AM-side obstruction is plausible but not yet
verified.

**PM bias** has two components: a slow weather-dependent drift of ~25%
between adjacent weeks, and a *data-confirmed* fixed late-PM cliff at
~17:00 UTC (50–72% drop in 30 min on 6 of 10 clear days, far beyond what
geometry can account for — consistent with a due-west obstruction at
sun azimuth ~270°, elevation ~21°).

**A fortnightly refresh can't track either component.**

### 2b — No hard "today's PV will fill the battery" rule

Even with a perfectly calibrated forecast, the LP's economic objective will
still grid-charge in cheap morning slots when peak prices later are high
enough. The household policy is `strict_savings` (peak_export OFF, prefer
self-consumption to arbitrage), but nothing in the LP encoded:
> "If today's PV is forecast to fill the battery anyway, don't grid-charge."

That makes the LP susceptible to the 2026-05-15 incident pattern *even with
a correct forecast*.

## 3. Decision — two-part fix

### Part A — Daily `pv_calibration_hourly` + `pv_calibration_hourly_cloud` refresh

New cron `bulletproof_pv_calibration_refresh_job` at **04:30 UTC daily** in
`src/scheduler/runner.py`. Calls
`compute_pv_calibration_hourly_table()` + `compute_pv_calibration_hourly_cloud_table()`
(both already exist in `src/weather.py`). Window = 30 days (default
`PV_CALIBRATION_WINDOW_DAYS`).

Schedule placement:
- 02:30 UTC: Fox energy rollup (PR #178)
- 02:35 UTC: Daikin consumption rollup (PR #178)
- 03:15 UTC: history retention prune
- 04:00 UTC: consumption backfill (V13/PR #190)
- 04:15 UTC: forecast_skill_log rebuild (already)
- **04:30 UTC: PV calibration refresh (new)**
- 08:00 BST = 07:00 UTC: morning brief uses the fresh factors
- 00:05 UTC next day: plan_push uses the fresh factors (cron anchored to UTC)

Best-effort: failures are logged, the LP keeps the previous table contents.

### Part B — Hard PV-sufficiency guard rail in `solve_lp`

In `strict_savings` mode, when
`Σ forecast PV today × LP_PV_SUFFICIENCY_MARGIN ≥ (battery headroom + Σ daytime load)`,
the LP adds `chg[i] ≤ pv_use[i]` to every today-slot strictly before the
first peak-tariff slot, **excluding negative-price slots** (2026-07-02
window audit: the guard's premise — grid-charging is wasteful when PV
covers demand — inverts when the grid pays for import, and the guard had
pinned the plan to PV-only charging + curtailment across a 15-slot paid
window). Mirrors the existing pre-plunge constraint pattern
at `lp_optimizer.py:794`, which carries the same `price ≥ 0` gate.
PV→battery stays allowed; grid→battery gets
blocked on the days the guard fires.

Defaults to ON in `strict_savings`, inert under `savings_first`.

### What was dropped — Option B (P75 PV-trust upward bias)

Earlier iterations of this design considered a P-th-percentile multiplier
derived from `forecast_skill_log` daily aggregates, applied as a flat
scalar on top of `today_factor`. Investigation revealed this is the wrong
tool for the AM/PM asymmetry — a single multiplier averages away the
exact pattern that matters. The per-hour `pv_calibration_hourly` (when
refreshed daily) already captures the same signal at the right granularity.
Carrying Option B as well would have been duplicative.

## 4. Validation

### Guard rail — 15-day replay

`scripts/replay_pv_trust.py --days 15 --as-of 2026-05-16` against prod
DB on 2026-05-15:

| Date | baseline | guard | Δ guard | fired | reason |
|---|---:|---:|---:|:---:|:---|
| 2026-05-01 | £2.19 | £2.19 | +£0.00 | n | insufficient_pv |
| 2026-05-02 | £2.97 | £2.97 | +£0.00 | n | insufficient_pv |
| 2026-05-03 | £5.04 | £5.04 | +£0.00 | n | insufficient_pv |
| 2026-05-04 | £4.51 | £4.51 | +£0.00 | n | insufficient_pv |
| 2026-05-05 | £5.57 | £5.57 | +£0.00 | n | insufficient_pv |
| 2026-05-06 | £7.30 | £7.30 | +£0.00 | n | insufficient_pv |
| 2026-05-07 | £4.55 | £4.55 | +£0.00 | n | insufficient_pv |
| 2026-05-08 | £0.11 | £0.11 | +£0.00 | **Y** | sufficient_pv |
| 2026-05-09 | £0.63 | £0.63 | +£0.00 | n | insufficient_pv |
| 2026-05-10 | £1.58 | £1.94 | **+£0.37** | **Y** | sufficient_pv |
| 2026-05-11 | £2.26 | £2.26 | +£0.00 | n | insufficient_pv |
| 2026-05-12 | £2.38 | £2.38 | +£0.00 | n | insufficient_pv |
| 2026-05-13 | £2.33 | £2.33 | +£0.00 | n | insufficient_pv |
| 2026-05-14 | £3.31 | £3.31 | +£0.00 | n | insufficient_pv |
| 2026-05-15 | £2.67 | £2.68 | +£0.01 | **Y** | sufficient_pv |
| **Total** | **£47.39** | **£47.76** | **+£0.38** | 3/15 | |
| **Annualised** | | | **+£9.20/yr** | | |

Reads:
- The guard fires on 3 of 15 days (the days with abundant forecast PV).
- Two of those days are zero-cost (LP already preferring PV under baseline);
  one (2026-05-10) is +£0.37 because the rail blocked a grid-charge that
  reality wouldn't have backfilled.
- Net annual cost ≈ £9.20/year. This is the **price of insurance** against
  the 2026-05-15 incident pattern.

**Caveat — model-vs-model.** Replay solves the LP at the snapshot prices
(Agile is day-ahead so "actual = snapshot" for £/kWh). It does NOT inject
actual-PV-vs-forecast-PV execution noise. The 2026-05-10 row (+£0.37) hints
at the tail risk in reality.

### Calibration refresh — no formal replay

Backtesting the refresh cron is non-trivial because the calibration tables
would have been different on each day if they had been refreshed daily.
The qualitative argument:

- AM bias is structural (~0.65 mean across 6 weeks of data); a daily
  refresh adjusts the per-hour factors to track this consistently.
- PM bias drifts 25% week-to-week; a daily refresh closes the lag from
  fortnightly to ~1 day.

The single biggest lever in §2a is replacing "7 days stale" with "always
fresh". Even at the same window size, daily refresh keeps the table aligned
with the most recent observations.

## 5. Rollback

Set in `.env`:
```
LP_PV_SUFFICIENCY_GUARD=false
```
Or flip `ENERGY_STRATEGY_MODE=savings_first` via MCP — the guard is inert
in that mode regardless.

For the cron, comment out the `add_job` for `bulletproof_pv_calibration_refresh`
in `runner.py` and restart `hem.service`. The previous fortnightly
`pv_calibration_hourly` regeneration path (whatever existed before — possibly
manual via the analytics script) keeps working.

## 6. Open questions (resolved)

1. ✅ Should the guard rail also trigger under `savings_first`?
   NO — `strict_savings` only, matches household policy.
2. ✅ Per-hour vs flat upward bias?
   Per-hour is the right granularity; the existing `pv_calibration_hourly`
   already provides it. Drop Option B entirely.
3. ✅ Refresh cadence?
   Daily at 04:30 UTC. Fortnightly was the de-facto status; daily is the
   minimum cadence that tracks week-to-week PM bias drift.
4. ✅ PV_CAPACITY tuning?
   Skip — calibration tables compensate algebraically. Bumping
   `PV_SYSTEM_EFFICIENCY` from 0.85 to 0.95 would not change LP behaviour.
5. ✅ Hour-18 outlier (P50 ratio = 1.61)?
   Real but tiny absolute kWh (~0.1 kWh × 20p = 2p/day). Not worth special
   handling.

## 7. Reference data

### Physical PV limits (since 2026-04-17 Agile start)

| Metric | Value | Date |
|---|---:|---|
| Peak instantaneous kW | **4.47 kW** | 2026-05-14 13:09 UTC |
| Max 30-min slot kWh | **1.93 kWh** | 2026-05-06 13:00 UTC |
| Max daily kWh | **19.35 kWh** | 2026-05-09 |
| Configured ceiling | 3.83 kW (1.91 kWh/slot) | `PV_CAPACITY_KWP × η = 4.5 × 0.85` |
| Inverter nameplate | 5.0 kW AC | Fox H1-5.0 |

Inverter clipping appears to bind at ~3.9 kW for sustained periods (top-30
peak observations cluster there). The few 4.1–4.5 kW observations are
heartbeat samples catching brief overshoots before clipping.

### Per-hour bias (last 30 days)

See §2a. The skill_log residual ratios after current `pv_calibration_hourly`
correction. Daily refresh shrinks these residuals further.
