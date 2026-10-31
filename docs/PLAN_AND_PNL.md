# Scenario LP, PnL semantics, tariff clamp, aggregation periods

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

### Scenario LP for peak-export robustness

**Where `peak_export` can even come from.** In the `normal` and `guests`
presets the LP constrains `exp <= pv_use`, so `peak_export` (battery→grid
price arbitrage) simply doesn't emerge. It is a **`vacation`-preset**
behaviour ONLY when `LP_BATTERY_EXPORT_ENABLED=true` (#873: with it false,
vacation uses the normal rules too - see "Vacation mode v2"). This is the
first and strongest gate, and it lives in the LP constraint, not in a flag.

**The one exception — `pre_negative_export`.** The `exp <= pv_use` cap is
relaxed to `exp <= pv_use + dis` on positive-price slots inside the
plunge-prep window (`LP_PRE_NEGATIVE_PREP_ENABLED`, default true;
`LP_PLUNGE_PREP_HOURS`), so the battery is deliberately drained to the grid
ahead of a negative-price window — sell high, refill at the paid negative
price. That branch is gated on `not vacation`, i.e. it fires **only in
`normal`/`guests`**, and `_slot_fox_tuple` maps those slots to the same
hardware action as `peak_export`: **ForceDischarge**. They are labelled
`pre_negative_export` precisely so they BYPASS the robustness filter below.
So "the battery never dumps to the grid outside vacation" is false — the
accurate statement is that `peak_export` (price arbitrage) never emerges
outside vacation.

When the LP *does* plan `peak_export`, three solves run under perturbed
forecasts (optimistic / nominal / pessimistic). A `peak_export` slot only
makes it onto Fox V3 when the **pessimistic** scenario also exports ≥
`LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH` (default 0.30 kWh) at that slot **and**
the economic margin (export price − future-refill shadow − battery wear)
clears `LP_PEAK_EXPORT_MIN_MARGIN_PENCE_PER_KWH`. Otherwise it's downgraded to
standard SelfUse (battery still covers load, no grid feed). Decisions are
persisted to `dispatch_decisions` with the per-scenario kWh values and the
margin components for full auditability.

**There is no `peak_export` kill switch env var.** `ENERGY_STRATEGY_MODE`
(`strict_savings` / `savings_first`) was **REMOVED** in PR C (mode-collapse
stack); `/api/v1/settings` still reports it as `"removed"` for back-compat and
setting it in `.env` is a silent no-op. The preset (`OPTIMIZATION_PRESET`) plus
the scenario filter (`filter_robust_peak_export` in
`src/scheduler/lp_dispatch.py`) are the only gates. The legacy
`EXPORT_DISCHARGE_MIN_SOC_PERCENT` live-SoC global gate is **also gone** (it
caused the 2026-04-28 incident where tomorrow's profitable peak-export
disappeared during a re-plan after today's discharge had drawn the battery
below 95 %).

See `docs/DISPATCH_DECISIONS.md` for the design rationale and decision rule.

### Daily PnL semantics — read this before quoting any £ figure

The MCP tools `get_energy_metrics`, `get_daily_brief`, `get_night_brief`, and
`get_tariff_comparison` (plus the markdown brief itself) all use the same
convention. **Don't paraphrase numbers from the markdown — pull the
structured fields:**

- `realised_cost_gbp` is **NET** and **INCLUDES** the daily standing charge.
  Formula: `Σ(slot_kwh × agile_p) + standing_pence_per_day − Σ(export_kwh × export_p)`.
- All shadow costs (`svt_shadow_gbp`, `fixed_shadow_gbp`,
  `fixed_tariff_shadow_gbp`) **also include** the standing charge — so
  `delta_vs_*_gbp` is real money saved, not energy-cost-only saved. Positive
  = Agile beat the shadow.
- `realised_import_gbp` is the energy-import side only (no standing, no
  export). Useful for breaking down "where did the cost come from".
- `export_revenue_gbp` and `export_kwh` are **measured** (from
  `pv_realtime_history` half-hour rollup × per-slot `agile_export_rates`).
- When `export_kwh == 0` but the LP committed `peak_export` slots, the brief
  surfaces a **forecasted** estimate flagged with 🔮 — that's the LP's
  `scen_pessimistic_exp_kwh × per-slot Outgoing Agile rate`. It is an
  estimate, not measurement; do not double-count.

`Mode:` line in the brief tells OpenClaw whether HEM is actively driving
Daikin (`active`) or just observing (`passive`). When passive, OpenClaw
should never suggest tactical Daikin actions ("preheat the tank now!") —
the heat pump runs on its own weather curve and HEM does not change
setpoints. Read `_mode_status_line()` in `src/analytics/daily_brief.py`.

### Tariff start clamp (PR #214, renamed #810)

```
SMART_TARIFF_START_DATE=2026-04-17    # start of HEM-managed smart-tariff history (Agile then Cosy)
# AGILE_TARIFF_START_DATE is the back-compat alias (same value); either name works.
```

Period aggregations (`compute_period_pnl` and everything that delegates to it
— weekly/monthly/MTD/YTD) clamp their `start_day` upward to this value.
Pre-Agile days were on a different tariff and would otherwise pollute the
realised cost + shadow comparisons. When clamped:
- Response carries `clamped: true`, `clamp_reason`, `requested_start`.
- `label` gains a `(since YYYY-MM-DD)` suffix so OpenClaw renders an honest
  qualifier on the YTD/monthly figure.
- `n_days` reflects the *actual on-Agile* window, not the requested range.

Leave empty to disable. Invalid ISO date logs a warning and disables clamp.

### Aggregation periods (PR #213)

Five PnL scopes are exposed by the same shape:

| Function | MCP path | Range |
|---|---|---|
| `compute_daily_pnl(day)` | `get_energy_metrics.pnl.daily` | a single local day |
| `compute_weekly_pnl(end_day)` | `get_energy_metrics.pnl.weekly` | trailing 7 days ending on `end_day` |
| `compute_monthly_pnl(end_day)` | `get_energy_metrics.pnl.monthly` | full calendar month containing `end_day` |
| `compute_mtd_pnl(end_day)` | `get_energy_metrics.pnl.month_to_date` | 1st of month → `end_day` (partial) |
| `compute_ytd_pnl(end_day)` | `get_energy_metrics.pnl.year_to_date` | Jan 1 of year → `end_day` |

All five funnel through `compute_period_pnl(start_day, end_day)`. Each
period dict carries the same breakdown as a daily dict — `kwh`,
`realised_cost_gbp`, `realised_import_gbp`, `export_revenue_gbp`,
`export_kwh`, `standing_charge_gbp`, `svt_shadow_gbp`, `fixed_shadow_gbp`,
`delta_vs_*` — plus `period_start`, `period_end`, `n_days`, `label`.

`mcp.get_tariff_comparison` accepts three input modes (mutually exclusive):

```jsonc
{ "date": "2026-05-01" }                              // single day (default = yesterday)
{ "period": "week"|"month"|"mtd"|"ytd" }              // anchored on today
{ "start_date": "...", "end_date": "..." }            // custom inclusive range
```

---

