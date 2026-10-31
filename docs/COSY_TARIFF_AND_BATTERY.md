# Cosy tariff structure, battery policy, plan fronts

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

## Tariff structure — Cosy (banded) vs Agile (dynamic), #803/#804

The household moved from Agile to **Cosy Octopus** (`E-1R-COSY-22-12-08-H`,
effective 2026-10-06): three flat bands on the LOCAL clock — cheap 12.49p
(04–07, 13–16, 22–24), day 25.45p, peak 38.17p (16–19). Every price classifier
was percentile-based and misread a 3-level day (the q75 of a Cosy day IS the
day band, so 13 h of day band read as "peak"; the strict `< cheap_thr` heartbeat
never saw the cheap band). `src/energy/tariff_structure.py` is now the ONE
place that decides `banded` vs `dynamic` and derives thresholds:

- **banded** (≤ `TARIFF_BANDED_MAX_LEVELS` distinct levels) → `cheap_thr` /
  `peak_thr` are the **midpoints between bands** (Cosy: 18.97 / 31.81), so every
  existing `<`/`<=`/`>`/`>=` consumer classifies the bands correctly. The LP
  plan carries `tariff_structure_kind` + per-slot `price_band`.
- **dynamic** (Agile) → bit-for-bit the historical formulas (`dynamic_rule="lp"`
  = q25/q75 in `solve_lp`; `"legacy"` = `min(mean×0.85,q25)` / `max(q75,25p)`
  in `_classify_slots`, `/api/v1/agile/day`, `analytics.patterns`).
- Horizon filler: on a TOU-family code the tail of the 48 h window is filled
  from the stored rows bucketed by LOCAL (hour, minute)
  (`band_profile_local`, DST-safe); Agile keeps the UTC-keyed 28-day priors.
  Synthetic rows keep `fetched_at="prior"` (dhw_policy treats anything else as
  REAL) and add `prior_source="prior_band"|"prior"`.
- Consumers with an ABSOLUTE pence floor keep it on top of the band threshold
  (`dhw_policy._evening_peak_entry_hour`, brief `_tariff_peak_windows_summary`).
- Kill switch: `OCTOPUS_TARIFF_STRUCTURE=dynamic` → pre-#804 behaviour everywhere.

## Battery policy on Cosy — battery-only arbitrage, no grid at peak (#806)

Household policy for autumn/winter: **the battery never discharges to the grid
and the 16–19 peak band is never bought from the grid.**

- `LP_BATTERY_EXPORT_ENABLED=false` (prod; code default `true`): `exp <= pv_use`
  in EVERY preset (vacation included), the pre-negative drain relaxation and
  the export rank bonus are off, and `build_fox_groups_from_lp` downgrades any
  `peak_export` / `pre_negative_export` slot to `standard` (`action_log`
  `export_slot_suppressed`) — **no ForceDischarge group can be uploaded**.
  Incidental PV surplus still exports (curtailing it earns nothing).
- `LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH=100` (prod; default 0): soft cost on
  `imp` inside the PEAK band (`plan.price_band == "peak"`, banded tariffs only;
  `LP_PEAK_IMPORT_PENALTY_APPLY_DYNAMIC=true` extends it to Agile via
  `price >= peak_thr`). A cost, never a constraint → a genuine shortfall still
  imports instead of going Infeasible. `plan.peak_import_kwh` records what the
  committed plan still buys at peak (target 0). The scenario solves share the
  term, so the pessimistic SoC floor at 16:00 (`LP_PESS_CHARGE_FLOOR_SCOPE=
  peak_entry`) already covers the whole peak under p75 load / PV×0.85.
- Fox shape 16–19 = **SelfUse(reserve)**: the battery serves the house. Not
  Backup (never discharges to loads → house grid-fed at 38p).
- Heartbeat **peak-import guard** (`PEAK_IMPORT_GUARD_*`): in the PEAK band,
  grid import ≥ 0.3 kW for 2 consecutive ticks → ONE `notify_risk` per peak
  window (`warning_key=peak_import_<date>_<HHMM>`) + `action_log`
  `peak_import_guard` + `PEAK_IMPORT_GUARD_ACTION=replan` (MPC re-solve,
  `trigger_reason=peak_import`, runs the scenario stack). `none` = alert only.

### How much to buy in each cheap window — probabilistic load (#818)

Prod `load_error_log` (60 d to 2026-10-06) showed the committed load forecast
under-forecast the 16–19 band on 22/30 days (mean +0.93 kWh/day, p90 +2.2 —
electric cooking at dinner; weekend lunch 12–13 is 2.6–3.3 kWh/h). Three facts
drive the design:

- **The LP plans against per-slot medians**; the only uncertainty it carries is
  the per-slot p75 that the pessimistic scenario uses, and the charge floor is
  taken from that solve. Slot-sum p75 (Tue 16–19: 3.85 kWh) ≈ the realised
  BAND p90 (4.0), while slot-sum p90 (6.5) overshoots ~60 % (tails don't add).
  So `LP_LOAD_EXPENSIVE_BAND_QUANTILE` stays `p75`; `p90` is an over-insurance
  knob (day+peak bands of a banded tariff only; Agile never touches it).
- **The profile learns the WHOLE window now.** `residual_load_profile_v2`
  dropped every sample without a `meteo_forecast_value` outdoor temperature
  (30-day retention) → a 120-day window learned from 31 days (8 weekends).
  Fallback: `execution_log.daikin_outdoor_temp` by UTC hour
  (`outdoor_from_daikin_samples` in the profile). Window length barely matters
  (30/60/120-day backtests within 0.1 kWh); the recent-bias corrector
  (`LOAD_RECENT_BIAS_ENABLED`) is **−6.7 % MAE out-of-sample** — keep it off.
- **Every cheap→non-cheap boundary is a charge-decision boundary.**
  `LP_PESS_CHARGE_FLOOR_BAND_EXITS=true` (default; banded tariffs only; lives
  INSIDE `LP_PESS_CHARGE_FLOOR_SCOPE=peak_entry` — prod — and is inert under
  `trajectory`, which floors every slot anyway) adds the 07:00 (after 04–07)
  and the midnight (22–24 → next day's 00–04) cheap exits to the floor set — buying 07–13 at 25.45p instead of
  12.49p costs 13p/kWh on 4.6 (weekday) – 8.1 (weekend) kWh. Entries INTO
  cheap (13:00, 22:00) and the 19:00 peak→day entry are never floored (the
  latter would make the nominal plan hold charge through the peak).

`GET /api/v1/load/expected?date=` (`src/analytics/load_expected.py`) is the
household-level view: per tariff window of the day, **band-sum p50/p75/p90
over same-day-type history** (weekday/weekend, `LOAD_EXPECTED_HISTORY_DAYS`
60), the committed plan's kWh, realised so far, and the committed forecast's
error history in that block. Feeds the Home consumption card.
### Plan per front — `GET /api/v1/plan/fronts` (#821)

One viewer-safe read (`?date=YYYY-MM-DD`, default today local; 400 on a bad date or outside [today-7, today+1])
for the Home page, composed by `src/analytics/plan_fronts.py`: `tariff` windows
(`band_windows_for_day`), `battery` (contiguous same-kind windows from the latest
LP run's slots — grid_charge / pv_charge / export / hold (Fox Backup group) /
self_use / idle — plus per-tariff-window planned vs realised grid import, SoC at
entry and `floored` = window entry slot in the pessimistic floor's
`entry_slots` — a CANDIDATE boundary persisted only when a re-solve ran, so read it as "candidate boundary in a run where the floor bound"; `floor_binding_slots` gives the count), `tank` (telemetry, `resolve_tank_params` + `coast_check`
calibration, `read_window_decision`, windows from the shared
`dhw_policy.dhw_schedule_rows_for_day` that `/daikin/dhw-schedule` also uses,
shower floors with an honest coast prediction from the warmup target — the LP slot `tank_temp_c` is the pinned phase target, not a physical temperature), `heating` (LWT rows from
`action_schedule`: boost/setback/restore with source lp|tier, gate state,
predicted indoor), `consumption` (exactly `expected_load_by_band`), `spend` and
`compare` (`cached_fair_comparison`, 900 s cache shared with `/tariffs/fair-compare`, month-to-date, 4 tariffs; week/month PnL rollups cached 600 s).
Every section is guarded on its own (`{"error": ...}`); the response is cached
60 s per `(DB_PATH, date)` (the comparison takes seconds), bypassed when
`now_utc` is passed.

**Billing cycle (#879).** `billing` section: the Octopus bill cycle containing `day`
(`OCTOPUS_BILLING_CYCLE_DAY`, runtime, 1..28, default 21 → 21 Oct … 20 Nov):
`cycle_start/end`, `days_elapsed/total`, `realised_gbp` (net incl. standing, `_period` 600 s
cache, clamped by `SMART_TARIFF_START_DATE`), `forecast_cycle_gbp = realised + realised/days ×
days_remaining`, `compare` (`cached_fair_comparison` over the cycle so far; `delta_gbp` =
other − current, positive = other costs more) and `winner`. `spend.pv_kwh` = realised PV today.
`/cockpit/now` also carries `daikin.holiday`, `quota` (fox/daikin used/budget) and
`plan.pushed_at_utc` for the Home footer. The Home route (`ui/src/routes/landing.tsx`) reads
only `/cockpit/now`, `/plan/fronts`, `/status/alerts` (+ `/whoami` from the shell) and loads no
ECharts; pure helpers in `ui/src/components/home/simple/logic.ts` (`npm test` in `ui/`).

**Spend score.** `ideal_avg_import_p` = cheapest band price (banded) or the
day's q25 (dynamic); `ideal_max_p = ideal × SPEND_SCORE_IDEAL_RATIO` (1.15),
`above_min_p` = `cheap_thr` (banded) or q50. `ideal` when avg ≤ ideal_max and
peak import ≤ 0.1 kWh (peak clause skipped on dynamic tariffs), `below` when avg
≤ above_min, else `above`. Basis is realised once ≥ 1 kWh was imported, else the
committed-plan forecast.

