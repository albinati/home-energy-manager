# Dispatch Decisions — Forecast-Robust Peak-Export

This explainer covers the design and operational behaviour of the scenario-LP
robustness filter that gates `peak_export` (battery → grid arbitrage) decisions
before they reach the Fox V3 inverter. It is the source-of-truth doc for both
human reviewers and the OpenClaw skill (`skills/home-energy-manager/SKILL.md`).

## Why this exists

The 2026-04-28 incident:

* The LP correctly planned a profitable 18:00 BST peak-export ForceDischarge
  for both today and tomorrow (~£1.18 each via the negative-price soak →
  battery → grid arbitrage; export revenue is correctly priced per-slot via
  Outgoing Agile, PR #154).
* After today's discharge ran (16:48 UTC onwards), an MPC re-plan at 17:33 UTC
  fired with live SoC at 86 %.
* The dispatch path's global gate `_bulletproof_allow_peak_export_discharge`
  compared **live** SoC vs. `EXPORT_DISCHARGE_MIN_SOC_PERCENT=95` and returned
  `False`.
* Every `peak_export` slot then got reclassified by `lp_plan_to_slots` to
  `peak` → trivial SelfUse → filtered out by camada-0
  (`src/scheduler/optimizer.py:_merge_fox_groups`'s trivial-SelfUse drop).
* Tomorrow's perfectly valid 99 % → 58 % peak-export silently disappeared
  from Fox V3, even though the LP knew SoC would be 99 % by then (post-soak).

Root cause: the gate compared **live SoC at plan time** against a global
threshold, not **LP-predicted SoC at the future slot**. One boolean controlled
the entire 48 h horizon.

## The replacement: scenario LP

The LP itself stays unchanged. Existing safety budget — `MIN_SOC_RESERVE_PERCENT`
floor, S10.1 terminal-SoC soft-cost (5 p/kWh, #168), η = 0.92 round-trip
efficiency, per-slot Octopus Outgoing pricing — is intact. We do not stack
additional pessimism inside the LP.

What's new: the LP runs **three times** at high-stakes triggers under
perturbed forecast inputs.

| Scenario | Outdoor temp | Base load | Purpose |
|---|---|---|---|
| Optimistic | forecast + 1.0 °C | × 0.90 | Upper-bound view; informational only. |
| Nominal | forecast as-is | × 1.00 | The canonical solve — what a single-pass LP would have done. Always the plan that's logged + dispatched (subject to filtering below). |
| Pessimistic | forecast − 1.5 °C | × 1.15 | Stressed forecast; gates the commit. |

Each scenario solve uses the same `solve_lp` function; perturbations apply at
the **input** layer (`src/scheduler/scenarios.py:apply_scenario` shifts
`temperature_outdoor_c`, recomputes COP via `cop_at_temperature`, scales
`base_load_kwh`). PV irradiance is decoupled from air temperature so the
optimistic/pessimistic axes don't double-stress through the solar channel.

## The decision rule (V1 — maximin)

For each slot where the canonical (nominal) plan emits `kind="peak_export"`:

1. **Strict savings kill switch** — if `ENERGY_STRATEGY_MODE=strict_savings`,
   drop the slot. `dispatched_kind=standard`, `reason=strict_savings`.
2. **No scenarios run** — if the trigger reason is not in
   `LP_SCENARIOS_ON_TRIGGER_REASONS` (so we only have the nominal solve),
   commit the slot. `dispatched_kind=peak_export`, `reason=no_scenarios_run`.
3. **Pessimistic solve failed** — if scenarios ran but the pessimistic LP
   solve returned `ok=False`, commit (degraded mode — better to ship the
   nominal plan than nothing). `reason=pessimistic_failed`.
4. **Robust** — if `pessimistic.export_kwh[i] >= LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH`
   (default 0.30 kWh; small floor to allow rounding noise without false
   rejection), commit. `reason=robust`.
5. **Otherwise drop** — `dispatched_kind=standard`, `reason=pessimistic_disagrees`.
   The battery still self-uses (covers house load) but does not export to grid.

The optimistic scenario is recorded in `dispatch_decisions` for observability
but does **not** gate. Its purpose is to expose what arbitrage we declined for
safety, so the user / OpenClaw can see what was given up.

## Pattern fit and tradeoffs

The decision rule is **maximin / scenario-based robust optimisation**
([Bertsimas & Sim 2004, "Price of Robustness"](https://www.robustopt.com/references/Price%20of%20Robustness.pdf)).
Standard in newsvendor / inventory ("order *q* only if pessimistic demand
still profitable") and algorithmic trading ("enter position only if it
survives a stress test").

It is NOT the dominant pattern in energy dispatch. Stochastic MPC
([MDPI 2025](https://www.mdpi.com/2071-1050/17/17/7678)) typically minimises
*expected* cost across scenarios; CVaR-hedged dispatch
([arXiv 2024](https://arxiv.org/html/2501.08472v1)) ensures the 95th-percentile
tail is acceptable. EMHASS — the most-deployed open-source HEMS — does not
gate on scenarios at all and trusts point forecasts. Pure maximin can
over-conserve ("price of robustness"), giving up typical-day arbitrage to
protect rare bad tails.

Why maximin still wins for V1:

1. **No measured forecast-residual distribution yet.** S11.1 (`pnl_execution_log`,
   Epic #180) hasn't shipped, so we cannot estimate per-slot σ. Probability-
   weighted expected cost would use guessed weights — false precision.
2. **The user's stated objective is asymmetric.** "Profit when we can, never
   lose." Maximin matches the loss-averse utility function exactly.
3. **Tractable runtime cost.** ~3 × LP solve ≈ 3–9 s. Auditable decision logic
   that humans + OpenClaw can both read.

Mitigation against the price of robustness:
- Modest perturbations (−1.5 °C, 1.15× load), not extreme stress.
- Track the pessimistic-veto rate via `dispatch_decisions` and tune the
  perturbations down if vetoes exceed ~25 % of `peak_export` slots over a
  fortnight.
- Evolution path documented below.

## Worked example

LP run #220, 2026-04-28T17:33 UTC, after today's peak discharge ran (live
SoC 86 %). Pre-fix behaviour: tomorrow's planned peak-export at 17:00 UTC
silently dropped because the live-SoC gate was False.

Post-fix behaviour:

```
explain_dispatch_decisions(run_id=220) →
  slot_time_utc=2026-04-29T17:00:00+00:00
    lp_kind=peak_export
    dispatched_kind=peak_export        ← committed
    reason=robust
    scen_optimistic_exp_kwh=2.10
    scen_nominal_exp_kwh=1.84
    scen_pessimistic_exp_kwh=1.40      ← ≥ 0.30 floor → robust
    committed=True
```

Counter-example (a borderline slot we'd want to drop):

```
explain_dispatch_decisions(run_id=N) →
  slot_time_utc=2026-04-29T17:30:00+00:00
    lp_kind=peak_export
    dispatched_kind=standard           ← downgraded
    reason=pessimistic_disagrees
    scen_optimistic_exp_kwh=1.95
    scen_nominal_exp_kwh=1.20
    scen_pessimistic_exp_kwh=0.05      ← < 0.30 floor → drop
    committed=False
```

The user reading this in OpenClaw sees the £ headroom they declined for
safety (optimistic 1.95 kWh × ~32 p ≈ 62 p was on the table) and can decide
whether to relax the floor.

## Where the decision log lives

* SQLite table: `dispatch_decisions` (one row per (run_id, slot_time_utc) —
  schema in `src/db.py`). Per-slot decision rationale + the three scenario
  export values.
* SQLite table: `scenario_solve_log` (one row per (batch_id, scenario_kind)).
  Per-scenario solve summary: objective, lp_status, perturbation deltas
  applied, peak-export slot count, wall-clock duration_ms. `batch_id` equals
  the canonical (nominal) run's `optimizer_log.id`, so every successful
  3-pass solve writes three rows that share `batch_id` and `nominal_run_id`.
* HTTP API:
  * `GET /api/v1/optimization/decisions/{run_id|latest}` — per-slot rows + summary.
  * `GET /api/v1/optimization/scenarios/{batch_id|latest}` — per-scenario solve
    summary for one batch (returns empty `scenarios[]` with a `note` when the
    run didn't trigger scenarios).
  * `GET /api/v1/scheduler/timeline` — executed/ongoing/planned partition with
    decisions joined on.
  * `GET /api/v1/foxess/schedule_diff` — live Fox V3 vs. last HEM upload.
* MCP tools (one-to-one with the API endpoints): `explain_dispatch_decisions`,
  `get_scenario_batch`, `get_plan_timeline`, `get_fox_schedule_diff`,
  `simulate_peak_export_robustness`.

## Parallelism

The two side scenarios (optimistic, pessimistic) run **in parallel** via a
`ThreadPoolExecutor` with three workers. Each `solve_lp` invocation builds a
fresh `LpProblem` and a fresh HiGHS solver instance, so the threads don't
fight for shared solver state; the GIL releases during the C-extension solve,
giving real wall-clock speedup. Total latency drops from ~3 × single-solve
(sequential) to ~1 × single-solve (parallel) plus thread-pool overhead — typically
3–4 s instead of 9–12 s.

The `solve_scenarios_with_nominal` helper short-circuits the canonical solve:
when `_run_optimizer_lp` already has the nominal plan, only the two side
scenarios actually run, dropping latency further.

## Triggers that get the 3-pass scenario solve

Configured by `LP_SCENARIOS_ON_TRIGGER_REASONS` (default
`cron,plan_push,octopus_fetch`):

* `plan_push` — nightly at 00:05 UTC. The big one (tomorrow's plan committed).
* `cron` — hourly MPC fires from `LP_MPC_HOURS_LIST`.
* `octopus_fetch` — fires ~16:05 local right after Octopus publishes new
  rates. This is the natural pre-peak moment, ~55 min before the 17:00 BST
  peak; we deliberately did NOT add a separate 16:XX cron because the
  octopus_fetch trigger already covers it without top-of-hour collisions.

Other triggers (`soc_drift`, `forecast_revision`, `dynamic_replan`, `manual`)
run nominal-only to keep MPC re-plan latency low. Those committed plans
inherit `reason=no_scenarios_run` decisions — robust by trust, not by
verification.

## Configuration knobs

```
LP_SCENARIO_OPTIMISTIC_TEMP_DELTA_C   = +1.0      # forecast Δ for optimistic
LP_SCENARIO_OPTIMISTIC_LOAD_FACTOR    = 0.90      # base-load × this for optimistic
LP_SCENARIO_PESSIMISTIC_TEMP_DELTA_C  = -1.5      # forecast Δ for pessimistic (cold-snap proxy)
LP_SCENARIO_PESSIMISTIC_LOAD_FACTOR   = 1.15      # base-load × this for pessimistic
LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH  = 0.30      # commit threshold on pessimistic export
LP_SCENARIOS_ON_TRIGGER_REASONS       = cron,plan_push,octopus_fetch
ENERGY_STRATEGY_MODE                  = savings_first    # set to strict_savings to disable arbitrage entirely
LOG_LEVEL                             = INFO     # raise to DEBUG for deep-dive
```

`EXPORT_DISCHARGE_MIN_SOC_PERCENT` was **removed** in this work
(`feat/forecast-robust-dispatch`). The unrelated
`EXPORT_DISCHARGE_FLOOR_SOC_PERCENT` remains — it's the `fdSoC` parameter
sent to Fox in the ForceDischarge group, not a gate.

## Evolution path (deferred)

Once S11.1 (`pnl_execution_log`, Epic #180) ships:

1. **Data-calibrated perturbations.** Replace the fixed −1.5 °C / 1.15× with
   measured residual quantiles from the log (e.g., 90th-percentile cold-snap
   forecast error).
2. **Expected-cost decision rule.** Commit when mean profit across scenarios
   > 0 AND CVaR-95 ≥ −ε. Standard energy-MPC framing; less conservative than
   pure maximin without giving up the safety floor.
3. **Conformal prediction intervals.** Replace fixed perturbations with
   calibrated CIs from forecast-error history; commit when peak export price
   exceeds the upper CI bound. Adapts conservatism dynamically.

The `dispatch_decisions` table this work ships supports all three transitions
without further refactor: it persists per-scenario export values, so the data
needed to fit/validate any of them is already captured.

## Out of scope

* Forecast-skill measurement (S11.1 / S11.3, Epic #180).
* Plan-revision notification on material MPC re-solve (S11.2 / #182).
* Per-day backfill of historical forecasts.
* Daikin tank-reheat anomaly detection (S11.4 / #184).
* Multi-slot negative-pricing solver alternation (#57). Separate root cause
  (inverter-stress quadratic cost), not a forecast/dispatch issue.

## References

- Bertsimas, D., & Sim, M. (2004). *The Price of Robustness.* Operations
  Research 52(1). [PDF](https://www.robustopt.com/references/Price%20of%20Robustness.pdf)
- Stochastic MPC for residential solar+battery+EV — [MDPI 2025](https://www.mdpi.com/2071-1050/17/17/7678)
- Battery dispatch under forecast uncertainty — [arXiv 2501.08472](https://arxiv.org/html/2501.08472v1)
- Conformal prediction for time series — [arXiv 2010.09107](https://arxiv.org/html/2010.09107)
- EMHASS forecast-handling docs — [emhass.readthedocs.io](https://emhass.readthedocs.io/en/latest/forecasts.html)
