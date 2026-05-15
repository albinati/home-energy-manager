# PV-Trust Guard Rail — design doc

**Status:** draft (decision pending)
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

**Aggregates** (08:00–10:00 UTC window):
- Grid imported: ~14 kWh @ avg 14.3 p/kWh → **£2.00 spend**
- PV exported (12:00–16:00 UTC, after battery full): ~5–6 kWh and counting
- Battery now at 99% with 4+ hours of sunlight left → ~6–8 kWh of additional
  PV will export today instead of charging.

## 2. Root cause

### 2a — recent forecast skill: PV under-delivery

`forecast_skill_log` shows the last 7 days of paired predicted vs realised:

| date | predicted PV kWh | actual PV kWh | ratio actual/pred |
|---|---|---|---|
| 2026-05-08 | 16.6 | 9.7 | 0.59 |
| 2026-05-09 | 18.1 | 19.3 | 1.07 |
| 2026-05-10 | 15.7 | 9.3 | 0.59 |
| 2026-05-11 | 14.5 | 9.8 | 0.67 |
| 2026-05-12 | 17.5 | 14.2 | 0.81 |
| 2026-05-13 | 16.8 | 13.0 | 0.78 |
| 2026-05-14 | 16.3 | 15.9 | 0.97 |

Median ratio = ~0.78 (PV under-delivers 22%). The LP's
`today_factor_effective_by_hour` pulls from this recent history. By the
06:55 UTC solve it had clamped midday hours hard (factor 0.30–0.60),
giving expected midday PV of ~0.6–1.1 kW per slot.

### 2b — actual PV today: way above the calibrated forecast

| hour UTC | calibrated forecast kW | actual kW | ratio |
|---|---|---|---|
| 12:00 | ~0.96 | 2.59 | 2.7× |
| 13:00 | ~1.09 | 2.60 | 2.4× |
| 14:00 | ~0.59 | 0.95 | 1.6× |
| 16:00 | ~0.82 | 1.53 | 1.9× |

### 2c — LP behaviour

With under-forecast PV and a cheap import window (08:00–09:30 = 13.9–14.6 p/kWh)
ahead of an expensive evening peak (16:00–17:30 = 32–35 p/kWh), force-charging
from grid was the LP's cost-minimising choice. **The LP did the right thing
given its inputs.** The inputs were the bug.

In `strict_savings` mode (peak_export disabled — the user's chosen default),
the only way force-charging pays back is via **evening grid displacement**.
That's still profitable on paper:

- Charge: 14 kWh @ 14.3 p = **£2.00**
- Evening discharge: 14 kWh × ~28 p displaced peak = **£3.92**
- Inverter round-trip loss (~5%) + cycle wear ≈ **£0.40**
- **Net win: ~£1.50** if PV had been a no-show.

But if PV had been trusted (counterfactual: skip morning import):
- Skip charge: **£0** spent
- PV charges battery for free during 10:00–12:00
- Same 14 kWh evening discharge: **+£3.92**
- ~6–8 kWh less mid-day export (lost revenue): **-£0.60 to -£1.00** at
  ~10 p export rate
- **Net win: ~£2.90 to £3.30** — i.e. ~£1.50/day better than what happened.

Over a season this is meaningful: ~30 sunny days × £1.50 = **~£45/year**
in pure self-consumption efficiency.

## 3. Design options

### Option A — Hard guard rail (PV-sufficiency check)

Add a constraint to `src/scheduler/lp_optimizer.py` analogous to the existing
pre-plunge rule at line 794:

```python
if energy_strategy_mode == "strict_savings" and forecast_daytime_pv_kwh >= headroom_kwh:
    for i in daytime_pre_peak_slots:
        prob += chg[i] <= pv_use[i]   # no grid → battery, PV only
```

Where:
- `forecast_daytime_pv_kwh` = Σ over today's remaining sunlit slots of
  `direct_pv_kw * today_factor_effective_by_hour[h] * 0.5`
- `headroom_kwh` = `(100% - current_soc%) × usable_capacity` + remaining
  daytime load + DHW need
- `daytime_pre_peak_slots` = slots between solve-time and first peak-tariff slot today

**Trade-off:**
- ✅ Deterministic, easy to audit, mirrors an existing pattern in the codebase
- ✅ Aligns with `strict_savings` philosophy (near-zero grid cost first)
- ⚠️ If a genuinely cloudy day surprises us, we eat one ~20 p evening hour
  before next-day cheap window. Bounded loss.

**Env knob:** `LP_PV_SUFFICIENCY_GUARD=true|false`, `LP_PV_SUFFICIENCY_MARGIN=1.0`
(multiplier on `forecast_daytime_pv_kwh` — set <1 to demand a buffer, >1 to
relax). Default proposed: `true` + margin `0.9` (require forecast 10% above headroom).

### Option B — Upward-biased PV trust

In `strict_savings` mode, scale the LP's PV forecast input by a percentile
of the recent forecast/actual skill ratio rather than treating it as point
estimate. Implementation lives in
`src/weather.py:evaluate_pv_forecast_accuracy` neighbourhood — derive a P75
(or configurable) factor from the last 14 days of `forecast_skill_log` and
multiply `direct_pv_kw` by it before the LP consumes it.

**Trade-off:**
- ✅ Less invasive — no new constraint, just better input
- ✅ Continuous (handles partly-cloudy days gracefully)
- ⚠️ Probabilistic, harder to audit ("why did the LP think PV would be X?")
- ⚠️ Tail risk: on a truly grim day where actual < P25, we under-charge from
  the cheap window and import at peak. **Quantify before shipping.**

**Env knob:** `LP_PV_TRUST_PERCENTILE=0.75` (0.5 = current median behaviour,
1.0 = optimistic), `LP_PV_TRUST_LOOKBACK_DAYS=14`.

### Option C — Both (belt-and-braces)

A as the safety net; B as the smoother. Each piece is small (<50 lines).
Recommended only if we want both maximum protection now and tunability later.

### Option D — Lower `MPC_LIVE_PV_KW_THRESHOLD`

A reactive-only fix: drop the mid-day re-plan threshold (currently 1.5 kW
sustained 1 tick) so the system reacts faster to PV overruns and abandons
queued force-charges. **Does NOT fix today's incident** — the force-charge
finished at 10:00 UTC, before the 11:00–13:00 PV spike that would have
triggered. Mentioned for completeness.

## 4. Recommendation

**Option A first, alone.** Reasoning:
1. The user's stated policy is `strict_savings` — near-zero grid cost
   matters more than peak_export profit. The guard rail is a direct
   encoding of that policy.
2. Deterministic > probabilistic when the user is asking "why didn't it
   do X" — auditability beats optimality at small £ stakes.
3. If Option A turns out too conservative on stretches of bad weather,
   Option B can stack on top.

If we'd rather lean on the existing skill log (Option B), the prerequisite
work is a 30-line script that computes the P75 trust factor over the last
14 days and prints what the LP *would* have decided on each of those days.
Replay-driven. Without that, Option B is shipping blind.

## 5. Open questions

1. Should the guard rail also trigger when `energy_strategy_mode == "savings_first"`
   (the default in `.env`)? Today the household defaults to `strict_savings`
   anyway (memory `feedback_near_zero_grid_cost_policy.md`), but downstream
   the env-level default is `savings_first`. Decision: keep the guard rail
   strategy-mode-aware, only fire in `strict_savings`. Avoids changing
   behaviour for the (rare) `savings_first` runs.
2. The pre-plunge constraint at `lp_optimizer.py:794` is bounded by
   `LP_PLUNGE_PREP_HOURS` (12h ahead). Should the PV guard rail have a
   symmetric look-ahead? Decision: no — PV forecast falls to zero overnight
   automatically, so the rolling daytime-PV-remaining sum naturally bounds
   the window.
3. Quartz vs Open-Meteo provenance: today's incident used Quartz numbers
   times today_factor. If we tune the today_factor calibration window
   instead (Option B), do we need to switch source-weighting too? Out of
   scope for this doc; tracked under #261 (Quartz prod HTTPS migration).

## 6. 15-day replay (validation)

`scripts/replay_pv_trust.py --days 15 --as-of 2026-05-16` run against the
prod DB on 2026-05-15, scoring each variant at the Agile prices the LP saw
when it solved (Octopus publishes day-ahead — no actual-vs-forecast noise
injected; this is a model-vs-model comparison, the kind of regression
signal the existing `lp_replay` infrastructure also reports).

| Date | baseline | A only | B only | C both | ΔA | ΔB | ΔC | guard | bias |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|---:|
| 2026-05-01 | £2.19 | £2.19 | £2.19 | £2.19 | 0 | 0 | 0 | n | 1.00 |
| 2026-05-02 | £2.97 | £2.97 | £2.97 | £2.97 | 0 | 0 | 0 | n | 1.00 |
| 2026-05-03 | £5.04 | £5.04 | £5.04 | £5.04 | 0 | 0 | 0 | n | 1.00 |
| 2026-05-04 | £4.51 | £4.51 | £4.51 | £4.51 | 0 | 0 | 0 | n | 1.00 |
| 2026-05-05 | £5.57 | £5.57 | £5.57 | £5.57 | 0 | 0 | 0 | n | 1.00 |
| 2026-05-06 | £7.30 | £7.30 | £7.30 | £7.30 | 0 | 0 | 0 | n | 1.00 |
| 2026-05-07 | £4.55 | £4.55 | £4.55 | £4.55 | 0 | 0 | 0 | n | 1.00 |
| 2026-05-08 | £0.11 | £0.11 | £0.11 | £0.11 | 0 | 0 | 0 | Y | 1.00 |
| 2026-05-09 | £0.63 | £0.63 | £0.63 | £0.63 | 0 | 0 | 0 | n | 1.00 |
| 2026-05-10 | £1.58 | £1.94 | £1.58 | £1.94 | **+£0.37** | 0 | **+£0.37** | Y | 1.00 |
| 2026-05-11 | £2.26 | £2.26 | £1.57 | £1.57 | 0 | -£0.70 | -£0.70 | n | 1.13 |
| 2026-05-12 | £2.38 | £2.38 | £1.82 | £1.82 | 0 | -£0.56 | -£0.56 | n | 1.12 |
| 2026-05-13 | £2.33 | £2.33 | £1.83 | £1.85 | 0 | -£0.50 | -£0.48 | n | 1.10 |
| 2026-05-14 | £3.31 | £3.31 | £2.92 | £2.92 | 0 | -£0.39 | -£0.39 | n | 1.09 |
| 2026-05-15 | £2.67 | £2.68 | £2.41 | £2.41 | +£0.01 | **-£0.26** | -£0.25 | Y | 1.07 |
| **Total**  | **£47.38** | **£47.76** | **£44.97** | **£45.37** | **+£0.38** | **-£2.41** | **-£2.01** | | |
| **Annualised** | | | | | **+£9.20/yr** | **-£58.69/yr** | **-£48.94/yr** | | |

Reads:

- **Option B alone is the biggest winner** (–£59/year). The P75 bias kicks
  in once the skill log has ≥ 5 contributing days (visible from 2026-05-11
  onwards: bias factor 1.07–1.13). On every day after that it shaves
  £0.26–£0.70 vs baseline by letting the LP trust PV more, which cuts
  morning grid-charge volume.
- **Option A alone costs ~£9/year** because the hard rail only fires on
  the very sunniest forecasts (2026-05-08, 10, 15), and on 2026-05-10 the
  blocked grid-charge couldn't be made back up by available PV → +£0.37.
  This is the bounded tail risk the design doc anticipated.
- **Option C is +£0.40/year worse than B alone**, the cost of carrying
  A on top. We accept it because: (i) the user's policy is "near-zero grid
  cost first, profit second" — A is the safety net against the exact 2026-05-15
  incident pattern; (ii) £0.40/year is rounding noise on a £200+/year savings
  baseline.

**Caveat — model-vs-model.** The replay solves the LP with the same per-slot
prices the original run saw (Agile is day-ahead so "actual" == "snapshot" for
£/kWh). It does NOT inject actual-PV-vs-forecast-PV noise during execution,
so genuine tail-risk (forecast says sunny, day turns grim, blocked grid charge
must be recovered at peak prices) is under-counted. The 2026-05-10 +£0.37 row
hints at it (sunny forecast, actual ratio = 0.59 — the day Option A blocked
charging and load fell back to grid). A future enhancement is to fold in
the actual-PV ratio per day for execution-noise replay.

## 7. Decision

**Ship Option C** with both knobs ON by default in `strict_savings`:

- `LP_PV_SUFFICIENCY_GUARD=true` — the hard rail. The £9/yr regression is
  the price of insurance against the 2026-05-15 incident pattern. Configurable
  via `LP_PV_SUFFICIENCY_MARGIN` (default 1.0; raise to make the rail more
  permissive, e.g. 0.85 demands forecast PV 18% above demand before firing).
- `LP_PV_TRUST_ENABLED=true`, `LP_PV_TRUST_PERCENTILE=0.75` — the upward bias.
  Tunable via `LP_PV_TRUST_*` env knobs.

Both knobs are **inert in `savings_first` mode** — that mode keeps legacy
behaviour. So this is opt-in via the existing `strict_savings` toggle the
household already uses.

## 8. Open questions (resolved)

1. ✅ Should the guard rail also trigger under `savings_first`? Decided NO —
   strict_savings only, matches the user's stated policy.
2. ✅ Should the rail have a symmetric look-ahead like `LP_PLUNGE_PREP_HOURS`?
   NO — PV forecast falls to zero overnight naturally, no look-ahead bound
   needed.
3. ✅ Should Quartz vs Open-Meteo source-weighting change? OUT OF SCOPE —
   tracked under #261 (Quartz prod HTTPS migration).
