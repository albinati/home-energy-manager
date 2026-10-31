# Thermal control and LWT (W3 model, coast, backstops, lift COP, emitter lag)

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

See also [WINTER_THERMAL_MODEL.md](WINTER_THERMAL_MODEL.md) (separate document, not merged).

## Thermal control on Cosy — band rule, W3 thermal model, LP-owned LWT (#808)

Space heating is shaped by the Daikin **LWT offset** rows (`lwt_preheat` +
`restore` in `action_schedule`), written by `_write_lwt_preheat_actions` at
every dispatch. Two sources are computed EVERY time and diffed into
`action_log` (`lwt_source_diff`: `source_used`, `n_differ`, `mean_abs_diff`,
disagreeing `windows`); **`DAIKIN_LWT_SOURCE`** (runtime-tunable, `PUT
/api/v1/settings`, no restart) picks which one reaches the device:

- `tier` — the price-band rule: `cheap` → `+DAIKIN_LWT_PREHEAT_BOOST_C` (3),
  `peak` → `DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C` (−2), `standard` → 0, negative
  → `+DAIKIN_LWT_PREHEAT_NEGATIVE_BOOST_C`. On a banded tariff the band comes
  from `plan.price_band` (no threshold comparisons). This is the kill switch.
- `lp` — the LP's own W3 thermal plan (`LP_W3_TIN_ENABLED=true`, RC model
  with learned τ / UA / C, soft 3-level comfort floor: night `LP_W3_NIGHT_FLOOR_C` 22–07 (code default 17.5,
  **prod 20** since 2026-10-09), **peak band = `INDOOR_SETPOINT_C −
  LP_W3_PEAK_COAST_DELTA_C`** (code default 1.0, **prod 0**), setpoint otherwise,
  plus the soft ceiling `LP_W3_CEILING_C`, #841). `plan.lwt_offset_c` is
  TRANSLATED, never written raw: a slot the LP left without space heat while
  the weather curve would run the compressor is a deliberate coast → the
  setback (the inverse physics returns `OPTIMIZATION_LWT_OFFSET_MIN` = −10
  there); outdoor ≥ cutoff → no write; clamp `DAIKIN_LWT_LP_OFFSET_MIN/MAX`
  (±5); then the same smoothing / restore / quota cap / pre-fire idempotency
  / drift backstop as the tier rule. Falls back to `tier` when the LP had no
  indoor trajectory (stale sensor, passive mode, flag off) — the diff row
  says `lp_available=false`.
- **Comfort guard on LP offsets** (`_lp_offsets`): the LIVE reading guards only
  the slots within `INDOOR_SENSOR_STALE_MINUTES` of now (boost side vs the
  ceiling-based `_boost_guard_c`, cold side vs that slot's floor); far slots are
  never vetoed by a reading or by the predicted trajectory. (The per-slot
  `_indoor_for_slot_fn` trajectory guard of the first #808 cut no longer exists.)
- When `DAIKIN_LWT_SOURCE=lp` the LP's `e_space` ceiling is capped at the ±5
  clamp so the plan never assumes more lift than the device will get.
- **Demand gate blocks boosts, never coasts (#847).** The gate
  (`DAIKIN_LWT_PREHEAT_MIN_TRAILING_HEATING_KWH` over the trailing
  `DAIKIN_LWT_PREHEAT_DEMAND_LOOKBACK_HOURS`) only excludes 2-h buckets overlapped by
  POSITIVE-offset windows (`get_nonzero_lwt_offset_windows(positive_only=True)`); coasts
  cannot wake the compressor, and excluding them made an `lp` plan close the gate on
  itself (2026-10-09 22:55Z: no rows written, rows already cleared, device on the curve
  off the battery for 4 h). Closed gate = `_write_lwt_preheat_actions` zeroes positive
  offsets and still writes the negative/zero rows (`action_log` `lwt_demand_gate`:
  `measured_kwh`, `excluded_buckets`, `windows_suppressed`; one `notify_risk` per local
  day under `source=lp` with space heat planned). **Hysteresis:**
  `DAIKIN_LWT_PREHEAT_DEMAND_HOLD_HOURS` (24; 0 = off) keeps the gate open after it was
  last measured open (`kv_state` `lwt_demand_gate_open_until`) unless outdoor >= cutoff.
  **Never empty:** `write_daikin_from_lp_plan` snapshots the pending `lwt_preheat`/`restore`
  rows before the regime's `clear_actions_in_range` and re-inserts them if the LWT writer
  skipped (quota headroom) or raised (`lwt_rows_preserved`). Status:
  `space_heating_gate_state()` has `demand_gate_hold_until`, `demand_gate_held`,
  `excluded_buckets`, `demand_gate_reason`; scorecard `lwt.demand_gate_closed_dispatches`
  + `lwt.demand_gate_windows_suppressed`. **Under the LP source the plan's own demand IS
  the signal:** when `source_used == "lp"` and `sum(plan.space_electric_kwh) > 0` the
  measured gate is BYPASSED (reason `lp_plan_demand`, `lwt_source_diff.guards.
  demand_gate_bypassed_lp_plan`; the per-slot outdoor cutoff still applies) — with
  `coast_mode=lp` the compressor is off outside boosts, so nearly all measured heating
  lands in positive windows and the measured gate would close every other day. The tier
  source keeps the measured gate. A closed gate with NO planned space heat writes no rows
  at all (a summer setback is quota churn). Restored rows are re-stamped with the
  CURRENT dispatch `plan_date` (the heartbeat reconciles `date == today` only), are
  restored on an exception only when the writer wrote nothing, and never overwrite a new
  pending row at the same `start_time`. The warm-outdoor hold override trusts live
  telemetry only when <= 3 h old (else the plan's first-slot outdoor).
- **Plausibility gate** (`w3_trajectory_plausible`): the LP source is
  unavailable (diff row `lp_available=false`, `lp_reason`) when the plan
  carries comfort SLACK (`plan.comfort_slack_c` = FLOOR shortfall only, >
  `LP_W3_SLACK_TOL_C` 0.1 °C) in more than `LP_W3_MAX_SLACK_SLOTS` (4) slots —
  floor slack is only ever used when the pump cannot hold the per-slot floor,
  i.e. the RC model cannot hold the house (unfitted UA/k) — or when ceiling
  slack (`plan.comfort_slack_hi_c`) coincides with planned space heat in such a
  slot (overheating on purpose; a house that merely STARTS above the ceiling has
  unavoidable overshoot and is not a veto) — or when any predicted value is more
  than `LP_W3_IMPLAUSIBLE_BELOW_FLOOR_C` (2.0) under the night floor / 2× that
  above the ceiling (#841; was the setpoint). The predicted trajectory is
  NEVER used to veto the plan's own offsets (that was circular); the only guard
  on LP offsets is the LIVE reading, on slots near now, boost side. The `tier`
  rule never reads the trajectory, so `DAIKIN_LWT_SOURCE=tier` is a true kill
  switch even with W3 on. LP offsets are block-ified by SIGN before the
  `DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS` filter (`smooth_lp_offsets`).
- **Comfort policy knobs (#820, runtime-tunable, `PUT /api/v1/settings`):**
  `LP_W3_NIGHT_FLOOR_C` (how cold the house may drift 22–07; code default 17.5, prod 20),
  `LP_W3_PEAK_COAST_DELTA_C` (how far it may coast through the peak; code default 1.0, prod 0) and
  `INDOOR_SETPOINT_C` — plus **`INDOOR_COMFORT_AGGREGATE`** = `mean` (default)
  | `min` | `max` | `room:<name>`: which reading is THE house temperature that
  seeds `t_in[0]` and that the comfort guard compares against
  (`db.get_latest_indoor_reading` → `aggregate_indoor_c`). The owner accepts
  the kitchen colder than the corridor; `room:corredor` plans comfort on the
  living space while `rooms_c` / `spread_c` keep the cold room visible.
  The W2 learner fits on the room MEAN while W3 seeds from the aggregate, so a
  `min` with a large spread biases the RC seed; keep `mean` or `room:` of a
  representative room. The plausibility gate judges the trajectory against the
  floor/setpoint recorded ON the plan (`plan.w3_night_floor_c`), not live config.
- **UA must be model-consistent before W3 drives hardware.** The LP's pump
  model is `k × (LWT − 18)` with the learned `k` (prod 0.063 kW/°C) — at 5 °C
  outdoor it can hold the house only up to UA ≈ 200 W/K. The env default
  `BUILDING_UA_W_PER_K=600` (and C = τ·UA ≈ 50 kWh/K) makes the trajectory
  fall monotonically and the comfort slack dominate the objective. Prod pins
  `BUILDING_UA_W_PER_K=200` (provisional; C = 82.7 h × 200 ≈ 16.5 kWh/K). **#859:** the HDD-regression UA
  (in (100, 1500); last winter 520-730 W/K, which the pump model cannot hold)
  is stored and SHOWN only: `get_building_ua_w_per_k()` returns the pin unless
  `BUILDING_UA_LEARNED_AUTO_APPLY=true` (code default false = kill switch back to the
  old "learned wins" behaviour); `refresh_building_thermal_calibration` stamps
  C = τ × pin accordingly. When a learned UA differs from the pin by > 25 %, one
  `notify_risk` (dedupe per 10 W/K bucket, `ua_learned_pending_<bucket>`) +
  `action_log` `ua_learned_pending`. `/sensors/thermal-calibration` `effective`
  carries `ua_learned_w_per_k` / `ua_pinned_w_per_k` / `ua_effective_source`
  (`pin|learned`), shown on the LWT learning card next to the #843 joint-fit UA
  (the better estimator). Kill
  switches: `LP_W3_TIN_ENABLED=false` (the model), `DAIKIN_LWT_SOURCE=tier`
  (the hardware path).
- Rollout: deploy with `tier` → read `lwt_source_diff` + `plan.indoor_temp_c`
  for a day (`lp_available=true`, boosts only in cheap bands, setbacks only in
  the peak, trajectory within 17–23 °C) → `PUT /api/v1/settings`
  `DAIKIN_LWT_SOURCE=lp`. Status: `space_heating_gate_state()` →
  `lwt_source`, `lwt_source_last_diff`.


### `/daikin/heating-plan` = written offsets (#845, supersedes #829)

`GET /api/v1/daikin/heating-plan` (Home chart, D-1/D/D+1) reports what the device
is/was actually given, not the tier rule: per slot `lwt_offset` = covering
`action_schedule` row (`lwt_preheat`/`restore`, device daikin; active > pending >
completed, then newest `created_at`/id among overlaps) with `offset_source`
`schedule`; past slots with an `execution_log.daikin_lwt_offset` sample report it
(`device`); no row -> 0/`none`. `lwt_setpoint_c` = curve base + written offset.
The old recompute survives only as `lwt_offset_tier` (the chart's "rule (ghost)"
line). Top level carries `lwt_source` / `coast_mode`. One `action_schedule` read
(`db.get_actions_for_plan_dates`) + one `execution_log` read per request. In
`/plan/fronts` `heating.windows[].source` comes from `lwt_learning_log.source` of
the window's first slot (fallback: last `lwt_source_diff.source_used`), never from
`params.lp_optimizer`; overlapping rows of the same kind+offset collapse (active,
then newest, kept). The Home plan cards have a Today/Tomorrow toggle (default
Tomorrow when local time >= 16:00 and tomorrow has scheduled rows).


### LWT coast mode, comfort backstop and learning log (#838)

- **`DAIKIN_LWT_COAST_MODE`** (runtime setting, `PUT /api/v1/settings`; code
  default `setback`): what an LP **coast slot** (planned `e_space ≈ 0` while the
  weather curve would run) writes when `DAIKIN_LWT_SOURCE=lp`. Three values:
  - `setback` — the fixed `DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C` (−2, pre-#838).
  - `lp` — **physics target** (`scheduler/lwt_coast.py:coast_target`): water just
    above the predicted room temperature cannot add heat, so the compressor stays
    off. `coast_lwt = indoor_pred[i] + DAIKIN_LWT_COAST_DELTA_C` (default 2.0);
    `offset = floor(coast_lwt − curve_lwt + 0.5)` (pipeline rounding), `curve_lwt` =
    `physics.get_lwt_base_c(forecast outdoor)`; clamped to
    `[DAIKIN_LWT_LP_OFFSET_MIN, 0]` (a coast slot never boosts). Falls back to the
    live indoor reading (near-now slots), then to the setback value.
  - `lp_raw` — the LP's raw `plan.lwt_offset_c[i]`, clamped only by
    `DAIKIN_LWT_LP_OFFSET_MIN/MAX` (±10 hard bound). The inverse physics of ZERO
    draw is the range minimum, so every pure coast slot becomes −10: experiments only.
  Heating (non-coast) slots always keep the LP's inverse-physics offset with the
  existing clamp. Outdoor cutoff, sign-block smoothing, restore rows, quota cap,
  pre-fire idempotency, live-only boost guard and the plausibility gate are
  unchanged. `coast_mode` is in `lwt_source_diff`, `space_heating_gate_state()`
  and the plan-fronts `heating.coast_mode`. **Smoothing (`smooth_lp_offsets`,
  #839 review):** HEATING blocks are split when a value is ≥ 2 °C from the
  block's first slot, at every `price_band` change and at every heating↔coast
  flip, then take the block mean; sub-blocks shorter than the minimum are merged
  back into the longer neighbour (planned heating is never dropped by a split —
  only a whole same-sign run shorter than the minimum is). COAST runs keep their own per-slot values (the
  forecast-driven depth); only value-runs shorter than
  `DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS` are merged into the longer neighbour (ties:
  the shallower), and a whole coast run shorter than the minimum is dropped.
- **Absolute LWT ceiling** `DAIKIN_LWT_ABS_MAX_C` (45; backup-heater exposure):
  on the LP heating path `off = min(off, max(0, floor(ABS_MAX − curve_lwt + 0.5)))`
  — it only blocks lift, never forces a setback; `lp_optimizer`'s `space_ceil_kwh`
  mirrors it (source=lp) so the plan never assumes lift the device won't get.
- **Comfort backstop** (`LWT_COMFORT_BACKSTOP_ENABLED=true`, `_MARGIN_C=0.5`,
  `_TICKS=2`; `scheduler/lwt_coast.py:backstop_tick`, called from the heartbeat
  before the reconciler): an ACTIVE `lwt_preheat` row with a negative offset AND
  the fresh aggregate indoor reading under the CURRENT floor − margin for N
  consecutive ticks (floor = night `LP_W3_NIGHT_FLOOR_C` in the LP night window,
  `INDOOR_SETPOINT_C − LP_W3_PEAK_COAST_DELTA_C` in the peak band, else the
  setpoint) → `apply_scheduled_daikin_params({"lwt_offset": 0})` (post-write verify
  runs), row set `completed` with `error_msg='comfort_backstop'` (the restore row
  then state-matches as a no-op), one `notify_risk` per window
  (`lwt_backstop_<date>_<HHMM>`), `action_log` `lwt_comfort_backstop`, then
  `bulletproof_mpc_job(bypass_cooldown=True, trigger_reason="lwt_backstop")` (no
  scenario stack). Needs `DAIKIN_CONTROL_MODE=active`, not `OPENCLAW_READ_ONLY`.
  The write uses `skip_if_matches=False` and the tick only counts as fired
  (row completed / notify / hold) when `apply_scheduled_daikin_params` returns
  True; otherwise `lwt_comfort_backstop` is logged `skipped` and the counter stays
  armed. A stale/absent sensor HOLDS the tick counter (no reset). **Anti-oscillation
  (#839):** firing records a hold (`LWT_COMFORT_BACKSTOP_HOLD_MINUTES`, 90; `kv_state`
  key `lwt_backstop_hold_until`, survives restarts, exposed as
  `space_heating_gate_state()["backstop_hold_until"]`): `_lp_offsets` AND
  `_tier_offsets` emit 0 for negative offsets on slots starting before it. The LP
  source also has a symmetric LIVE cold guard — a negative offset on a near-now slot
  (±`INDOOR_SENSOR_STALE_MINUTES`) is zeroed when the fresh reading ≤ that slot's
  floor − `LWT_COMFORT_BACKSTOP_MARGIN_C` (`lwt_source_diff.guards`). Notify dedupe
  key = the hold start.
- **Warm-side backstop (#855)** (`LWT_WARM_BACKSTOP_ENABLED=true`, `_TICKS=2`,
  `_HOLD_MINUTES=60`; `scheduler/lwt_coast.py:warm_backstop_tick`, heartbeat right after
  the cold tick): an ACTIVE `lwt_preheat` row with a POSITIVE offset and the fresh
  aggregate indoor reading `>= effective_w3_ceiling_c() - DAIKIN_LWT_PREHEAT_COMFORT_BAND_C`
  (22.5 at ceiling 23) for N consecutive ticks -> `apply_scheduled_daikin_params({"lwt_offset": 0})`,
  row `completed` with `error_msg='warm_backstop'`, `action_log` `lwt_warm_backstop`, then
  `bulletproof_mpc_job(bypass_cooldown=True, trigger_reason="lwt_warm_backstop")`. No Telegram
  (system working as intended); counted in scorecard `lwt.warm_backstops`. Same gating as
  the cold one (active, not read-only, fired only when apply returns True, stale sensor holds
  the counter). Own counter and own hold (`kv_state` `lwt_warm_backstop_hold_until`,
  `space_heating_gate_state()["warm_backstop_hold_until"]`): `_lp_offsets`/`_tier_offsets`
  zero POSITIVE offsets on slots starting before it. Cold acts only on negative rows, warm
  only on positive rows, so the two cannot fight. `/daikin/heating-plan` caps a
  `warm_backstop` row at its `executed_at` like `comfort_backstop`.
- **Learning log** `lwt_learning_log` (PK `slot_time_utc`): PLANNED fields
  (`run_id, source, coast_mode, offset_lp_raw, offset_written` = after smoothing,
  `indoor_pred_c, floor_c, margin_c` = predicted headroom over the floor,
  `outdoor_fc_c, e_space_kwh, cop_space, price_band`, and for coast slots
  `curve_lwt_c, coast_target_lwt_c, coast_delta_c`) are upserted at every
  dispatch (latest plan wins, `written_at_utc` kept, filled slots untouched).
  Nightly `lwt_learning_job` (04:40 UTC) fills REALISED fields for yesterday's
  local day (per-room mean + min from `room_temperature_history`, outdoor and
  `lwt_actual` from live `daikin_telemetry`, device offset from `execution_log`,
  `kwh_heating` of `daikin_consumption_2hourly` prorated over its slots) and
  writes one `lwt_learning_daily` row (payload also carries the realised
  pump-off delta `lwt_actual − indoor` where heating kWh stayed ≈ 0 vs pump-on,
  to fit the real `DAIKIN_LWT_COAST_DELTA_C`) + `action_log` `lwt_learning_summary`.
  `run_id` is stamped AFTER `log_optimizer_run` (`lwt_coast.stamp_run_id`; rows carry
  `plan_updated_at_utc` as the plan token, `written_at_utc` stays the first write);
  `offset_written` is recorded after the quota-cap trim (NULL for dropped windows).
  Pruned after `LWT_LEARNING_RETENTION_DAYS` (120).
- **Holiday / unit-off days (#880)**: excluded from live tau, UA-coast and pump-off-delta fits (and the
  joint fit's rows); analyse them offline. See the Daikin holiday section.
- **Reading the estimates (#843)**: coast-only data identifies **τ = C/UA only**.
  `ua_est_w_per_k` / `ua_est_night_w_per_k` (API + payload: `ua_from_tau_scaled_*`,
  `ua_est_circular: true`) is `C × decay rate` with C = τ × UA_pin (#841), so it returns ≈ the
  pin whenever nights cool at the calibration's τ — it CANNOT confirm or refute the 200
  pin; kept for audit (details block of the card), never a headline. UA and C come from
  the **episode estimator** `fit_ua_c_joint` (payload `joint_fit`, rolling `joint_window_days`=14).
  Heat input is ONLY Onecta-METERED buckets (`lwt_learning_log.heating_kwh_source` starts with
  `onecta`; `telemetry_integral` buckets are the weather-curve model `get_daikin_heating_kw ×
  dt`, not a measurement, and are excluded; 1.0-kWh phantom Onecta buckets are zeroed in
  `fill_realised` by `thermal_learning.sanitize_phantom_heating`). Samples: one per contiguous
  Onecta-heating EPISODE (+ a 4-slot coast tail for emitter lag; dropped when an adjacent
  bucket is unusable, e.g. metered 0 but `lwt_actual − indoor > 6 °C` = hidden heat the
  whole-kWh counter rounded away) and one per 2 h Onecta-zero coast block lying wholly in the
  local night (22–07: no solar). Model `ΔT = a·ΣQ_th − b·Σ(T−To)Δt + g·Σdt` (Q_th = COP(To) ×
  metered kWh, COP = the LP curve with its lift derate; a=1/C, b=1/τ, g=gain/C); 3-param OLS by
  hand, SEs σ²(XᵀX)⁻¹ + delta method (`ua_se`, `c_se`), with a method-of-moments correction for
  the counter's known quantisation noise (1/12 kWh² per bucket). Reported: `ua_w_per_k`,
  `c_kwh_per_k`, `tau_h`, `gain_kw`, `resid_rms_c`, `n_heat_episodes`, `n_coast_blocks` (no R²
  headline). Gate: ≥ 5 episodes AND ≥ 8 coast blocks AND a,b > 0, else `identifiable: false`
  with `reason` ∈ `too_few_heat_episodes | too_few_coast_blocks | nonphysical_fit | singular |
  no_measured_input`. Diagnostics: `tau_fixed` (b HARD-fixed to the learner's τ — a
  constraint, not a prior), `slot_fit` (old per-slot regression, biased by quantised/lagged
  heat), `coast_tau_h`, `consistency_flag: lag_or_gain_contamination_suspected` when the free τ
  and the coast-only τ differ by > 25 %, and `cop_sensitivity` (refit at COP ×0.8 / ×1.2 — COP
  is the weakest link; SEs are optimistic since COP, lag and quantisation are model error).
  Honest model checks: `pred_err_*` and `night_rise_per_band` (each cheap band with a
  positive written offset: measured vs predicted indoor rise from slots carrying BOTH readings;
  predicted only from slots sharing the first slot's `plan_updated_at_utc` token — `mixed_plans`
  rows are not comparable; `model_error_c` > 0 = the model is pessimistic). `k_est_kw_per_c` =
  median over 2-hour buckets of kWh ÷
  Σ((`lwt_actual` − 18)·Δt) (≥ 3 samples, `lwt_actual` > 20); learned k pin 0.063.
  Nothing is auto-applied (`thermal_calibration` is untouched); read via
  `GET /api/v1/thermal/lwt-learning?days=14` or the Insights "LWT learning" card.
  The scorecard `lwt` section carries `lwt_backstops` (count of fired backstops).
  **Discontinuity:** the circular `ua_est_*` scales with the C it is fitted with, and C
  went from 49.6 to 16.5 kWh/K at the #841 C fix (~3x): that series before/after is NOT
  comparable. The payload carries `c_kwh_per_k` per day.


### Banking heat in cheap bands — consistent C, comfort ceiling, ceiling-based boost guard (#841)

The LP plans "bank heat in the cheap bands, coast through day/peak"; three of our
own constants/guards used to stop it reaching the device:

- **C must match the EFFECTIVE UA.** `thermal_calibration.c_kwh_per_k` is
  `τ × UA` for the UA it was computed with (`c_ua_basis_w_per_k`, nullable
  column; pre-#841 rows = unknown). `get_building_thermal_mass_kwh_per_k()` /
  `thermal_mass_resolution()` return the stored C only when the basis equals
  `get_building_ua_w_per_k()` (±1 W/K); a different or unknown basis →
  `τ_eff × UA_eff / 1000` (logged once). Prod: 49.6 (τ 82.7 × env UA 600) →
  16.5 kWh/K under the 200 W/K pin — with 49.6 the plan could not hold the day
  floor, produced `comfort_slack` in 5–6 slots and the plausibility gate fell back
  to the tier rule. `/api/v1/sensors/thermal-calibration` `effective` carries
  `c_basis_ua_w_per_k` + `c_recomputed`; the Insights "LWT learning" card shows
  τ / UA / C (+ basis).
- **`LP_W3_CEILING_C`** (runtime setting, default 23.0, 18..28, validated
  `>= INDOOR_SETPOINT_C + 0.5`): W3 soft upper bound `t_in[i+1] − s_hi[i] <= ceiling`,
  slack penalised like the floor slack (`LP_W3_COMFORT_PEN_PENCE_PER_DEGC_SLOT`).
  `plan.w3_ceiling_c` records it; `plan.comfort_slack_c` stays FLOOR slack only
  (the model-health signal), `plan.comfort_slack_hi_c` is the ceiling overshoot.
  The plausibility gate counts a slot with ceiling slack only when the plan HEATS
  in it (`space_electric_kwh > 0`) — a house that starts at 23.5 against a 23
  ceiling has unavoidable overshoot and must not disable the LP source.
  Its upper bound is `ceiling + 2·LP_W3_IMPLAUSIBLE_BELOW_FLOOR_C` (was
  setpoint-based). ONE reader for the ceiling:
  `lwt_coast.effective_w3_ceiling_c()` = `max(LP_W3_CEILING_C, INDOOR_SETPOINT_C + 0.5)`
  (LP, boost guard, gate fallback); both settings are validated against each other.
  `lwt_learning_log.ceiling_c` is stored per slot; `plan/fronts` `heating.ceiling_c`
  reads the ceiling the latest plan of the day was solved with.
- **Boost guard vs the ceiling.** A positive offset (`_preheat_lwt_offset` tier
  rule, `_lp_offsets` near-now live guard) is zeroed when the LIVE indoor reading
  `>= LP_W3_CEILING_C − DAIKIN_LWT_PREHEAT_COMFORT_BAND_C` (22.5) instead of
  `setpoint + band` (21.5). The predicted trajectory still never vetoes offsets
  (the LP already enforces the ceiling). The backstop's floor logic is unchanged.
- Telemetry: scorecard `lwt.heating_kwh_by_band` (cheap/standard/peak, 2-hourly
  `kwh_heating` prorated per 30-min slot) + `lwt.indoor_min_c/indoor_max_c` (aggregate);
  `plan/fronts` `heating.ceiling_c`.
- **What the LP actually writes at prod constants** (UA 200 W/K, C 16.5 kWh/K,
  floor 21 / night 20 / peak delta 0, ceiling 23): cheap bands +10 (the lift cap),
  day-band slots −1/−2 ("hold 21"), peak coast −10. The ceiling is a BOUND, not a
  target, and banking is capped at ~0.5 K per cheap band: net input per slot is
  `(min(RADIATOR_MAX_KW, lift ceiling)·COP − UA·ΔT)/C` ≈ 0.09 K at UA 200 / C 16.5
  (the LP's COP comes from `DAIKIN_COP_CURVE`, 4.1–4.5 at 6–9 °C, NOT
  `weather.cop_space`). "Coast through 07–13" needs a lower fitted UA or a bigger
  pump; the ceiling only starts to bind for a small-C / leaky / strong-pump house.
  With `LP_COP_LWT_OFFSET_PENALTY_PER_K` > 0 (#863) the cheap-band boost is priced at its
  lower lift COP, so the +10 cap is no longer the default there (fixture: +3 at 0.025).
- `thermal_mass_resolution` recomputes C only for `tau_x_env_ua` / `tau_x_learned_ua`
  rows (a measured C is kept); an out-of-bounds stored C is replaced by τ×UA_eff and
  reported (`c_recomputed`, `c_reason`); the refresh stamps C from the BOUNDED UA
  (an out-of-bounds HDD fit never stamps C).
- Owner comfort policy (2026-10-09, mean across sensors): day 21 floor / 23 ceiling,
  night (22–07) 20 floor (`LP_W3_NIGHT_FLOOR_C=20`, `LP_W3_PEAK_COAST_DELTA_C=0`).


### Solar + internal gains in the W3 RC model (#854)

The RC equation used to carry NO gains, so a sunny afternoon was planned like an
overcast one and boosted +9/+10 "to hold 21". Now
`C·ΔT = e_space·COP + q_gain·Δt − UA·(T_in − T_out)·Δt` with
`q_gain[i] = LP_W3_INTERNAL_GAIN_KW + LP_W3_SOLAR_GAIN_KW_PER_PV_KW × pv_kw[i]`
(thermal kW; `pv_kw` = the SAME per-slot PV the battery side uses, `pv_avail / 0.5 h`,
so the optimistic/pessimistic scenario PV factors scale the gain automatically).

- Knobs (runtime-tunable, `PUT /api/v1/settings`, no restart, Settings > comfort group):
  `LP_W3_INTERNAL_GAIN_KW` (code default **0**, 0..1) and `LP_W3_SOLAR_GAIN_KW_PER_PV_KW`
  (code default **0**, 0..2). **Both 0 = the pre-#854 equation, bit-for-bit** (the gain term
  is not even added to the constraint) = the kill switch.
- The gentle-recovery cap still bounds only the HEATING-driven rise (`q_heat/C`), never the
  net delta, so gains cannot make it infeasible; the soft ceiling still binds (a slot that
  heats never plans a ceiling overshoot). A gain-heavy sunny window can legitimately push the
  predicted indoor above the ceiling with `e_space = 0`: that is ceiling SLACK without planned
  heat, which the plausibility gate ignores.
- Recorded on the plan (`w3_internal_gain_kw`, `w3_solar_gain_kw_per_pv_kw`, `w3_gain_kw[i]`),
  in `lp_inputs_snapshot` config snapshot, and per slot in `lwt_learning_log.gain_kw`
  (`pv_real_kw` = realised PV mean kW, filled by the nightly job from `pv_realtime_history`).
- **Learned values (not auto-applied).** `fit_ua_c_joint` adds a solar regressor
  `Σ pv_kw·Δt` (realised PV) to the episode model and ALSO admits DAYTIME coast blocks
  (Onecta heat 0, offset <= 0, realised PV on every slot) — used ONLY by the solar fit; the
  headline UA/C/gain fit is unchanged (night coast + heating episodes). Payload (`joint_fit`):
  `solar_gain_kw_per_pv_kw` ± `solar_gain_se`, `internal_gain_kw` ± `internal_gain_se`,
  `n_day_coast_blocks`, `solar_identifiable`, `solar_reason` ∈ `no_daytime_coast_blocks |
  too_few_daytime_coast_blocks (< 6) | singular | nonphysical_fit`, and the full 4-parameter
  `solar_fit`. Read it on the LWT learning card next to the pinned values; the internal gain
  is collinear with UA on night-coast data (only a coarse estimate), the solar coefficient is
  the useful one. Suggested path: let it accrue ~2 weeks of mixed sun/cloud days, then
  `PUT /api/v1/settings` the learned (rounded, conservative) values.
- **Residual check** (`lwt_learning_daily.payload.pred_resid`): realised minus PLANNED indoor
  (positive = plan too cold = missing gains) for day vs night and, for daytime slots, by PV
  tercile. High-PV tercile clearly above the low one = the model lacks solar gain.


### LWT lift COP + emergency floor (#863)

Prod 2026-10-17/18: afternoon +4/+8 boosts banked nothing (13-16 heating 2.0-2.5 kWh, later
bands did not heat less) because the LP priced +10 K of LWT at the curve COP.

- **Lift-segment COP, exact marginal** (`LP_COP_LWT_OFFSET_PENALTY_PER_K`, runtime, code
  default **0** = bit-for-bit, range 0..0.1; proposed prod 0.025). With E(x) = compressor
  draw at offset x above the curve and m(x) = max(`LP_COP_LIFT_MIN_MULTIPLIER`, 1 - p*x),
  thermal output is Th(x) = E(x)*cop_space*m(x): +5 K at 0.025 costs ~12.5 % of the WHOLE
  heat output. Penalty > 0 and not passive: `e_space = e_base + sum(e_lift_k)`; `e_base`
  (<= curve draw) at `cop_space`; ONE segment per whole K of lift (`ceil(L)`, last partial;
  `L = _slot_lift`) so the COP at a physical offset never depends on the clamp; segment COP =
  `(Th(x1)-Th(x0))/(E(x1)-E(x0))`, clipped non-increasing (concave) so the LP fills in order
  with NO binaries; the `max_hp` clamp trims the top segments (floor + widths ==
  `space_ceil_kwh`). RC equation, radiator cap and gentle-recovery cap use the thermal sum.
  If the older `LP_COP_LIFT_PENALTY_PER_KELVIN` is also > 0 the segments use the curve COP
  WITHOUT that derate (warned once; the new penalty replaces it over the lift range). Plan:
  `cop_lwt_offset_penalty_per_k`, `cop_space_effective` (thermal/electric, also into
  `lwt_learning_log.cop_space`), `space_floor_kwh`, `space_lift_widths_kwh`,
  `space_lift_cops`; knob in the `lp_inputs_snapshot` config snapshot. Fixture (UA 200,
  C 16.5, Cosy, 12 C, start 21, tier source): 13-16 lift 1.00 -> 0.24 kWh, max offset +10 -> +3.
- **Emergency floor in the LP** (`LP_W3_EMERGENCY_FLOOR_C` runtime, 20, 15..22; env-only
  `LP_W3_EMERGENCY_PEN_PENCE_PER_DEGC_SLOT`, **code default 0 = no variables, bit-for-bit**;
  prod sets 1000 via `.env`): `t_in[i+1] + s_em[i] >= min(emergency, that slot's comfort
  floor)` on every slot - `lwt_coast.effective_emergency_floor_c` is the ONE rule (night
  floor at night, setpoint - peak delta in the peak band, else setpoint). No cross-validation
  between the settings. Plan: `comfort_slack_emergency_c`, `w3_emergency_floor_c`; the
  plausibility gate returns `emergency_slack` for a plan that STARTED at/above the floor yet
  uses it (LP source unavailable, tier rule used).
- **Live emergency = alert + replan, NEVER a device write** (`lwt_coast.emergency_tick`,
  heartbeat after the cold/warm backstops; `LWT_EMERGENCY_ENABLED` **code default false**
  (prod sets true), `_TICKS` 2, `_REPLAN_COOLDOWN_MINUTES` 60): fresh aggregate indoor below
  the effective floor of the CURRENT slot for N ticks (stale sensor holds the counter) ->
  `action_log` `lwt_emergency`, one `notify_risk` per local day (`lwt_emergency_<date>`),
  `bulletproof_mpc_job(bypass_cooldown=True, trigger_reason="lwt_emergency")` at most once per
  cooldown (`kv_state` `lwt_emergency_last_replan`). The replan's LP plans the heat through the
  normal dispatch path (smoothing, ABS_MAX clamp, rows, reconciler); there is no dispatch
  hold. Skipped (counter stays armed) when the cold or warm backstop replanned this same tick.
  Needs active mode, not read-only, `LP_W3_TIN_ENABLED`. Scorecard `lwt.emergencies`.
- **Rollout:** deploy at penalty 0 / emergency pen 0 (no change), replay 7 days with
  `patched_config` at 0 vs 0.025 and ceiling 23 vs 21.5, then `PUT /api/v1/settings` and set
  the two emergency env knobs. The comfort-band retune (setpoint 21 / ceiling 21.5, lower
  `DAIKIN_LWT_PREHEAT_COMFORT_BAND_C`) is a settings change, not code.


### Emitter lag (#866)

Prod step tests (2026-10-18/19): a +10 offset reaches LWT target ~25 min later and the room starts
rising 30-40 min after the write; after a setback the room keeps receiving heat ~35-40 min. The W3 RC
equation used to put `e_space*COP` into `t_in` in the SAME slot.

- **`LP_W3_EMITTER_TAU_MIN`** (runtime, comfort group, 0..120, **code default 0 = no variables, bit-for-bit**;
  also in the `lp_overrides` WHITELIST and the `lp_inputs_snapshot` config snapshot). tau > 0, W3 on, not
  passive: loop heat `h[0..n]` (kWh_th above room) and delivered `d[i]`, `d = a*h + b*q`, `h' = h + q - d`
  (a = 1-exp(-dt/tau), b = 1-(tau/dt)*a, `q` = `_thermal_space(i)` incl. #863 lift segments). The RC equation
  uses `d`; the radiator cap and gentle-recovery cap stay on PRODUCTION `q`; comfort/ceiling/emergency
  constraints are on the room as before. Heat still in the loop at the horizon end is not credited (slight
  bias against late heating). `space_electric_kwh` / `lwt_offset_c` stay PRODUCTION, so dispatch, the
  `lwt_learning_log` planned fields and `plan/fronts` keep their meaning. Plan fields: `w3_emitter_tau_min`,
  `w3_emitter_h0_kwh`, `w3_delivered_kwh` (per slot).
- **`h[0]`** (`lp_optimizer._emitter_h0_kwh`): steady state `P * tau` of the supply temperature the pump is
  on, `P = k*(lwt-18)*COP` (kWh_th per h). `lwt` = `LpInitialState.lwt_actual_c` (freshest live
  `daikin_telemetry` <= 30 min) ONLY if plausible: `lwt <= min(50, curve(outdoor) + OPTIMIZATION_LWT_OFFSET_MAX
  + 5)`. A tank reheat reads 45-50 C on the same sensor (3-way valve: no heat to the radiators), so an
  implausible, stale or absent reading falls back to the weather CURVE at the current outdoor temperature
  PLUS the offset the device is on now (`db.get_active_lwt_offset`: the active `lwt_preheat`/`restore` row,
  else the latest `execution_log.daikin_lwt_offset` <= 2 h old, else 0), so a coast at -10 seeds ~0 through
  the ramp and a +5 boost seeds the steady state at curve+5 (0 above `DAIKIN_WEATHER_CURVE_HIGH_C`). When the
  offset is known the plausibility cap tightens to `min(50, curve + max(0, offset) + 5)`. COP is lift-derated
  like the #863 segments (`max(LP_COP_LIFT_MIN_MULTIPLIER, 1 - p*(lwt-curve))`) when the reading implies lift.
  Continuity: scaled by `clip((lwt - indoor - 1)/3, 0, 1)` (0 at <= 1 K above the room, full at >= 4 K) so
  there is no cliff. `LpInitialState.emitter_h0_kwh` (explicit) wins over all of that: chained replays carry
  the predicted end-of-segment `plan.w3_emitter_heat_kwh[idx]`. EVERY run (also at tau 0) persists
  `exogenous_snapshot_json.emitter = {lwt_actual_c, lwt_offset_c, indoor_c}`; `h0_kwh` + `tau_min` only when
  tau > 0. A replay honours the persisted `h0_kwh` only if the snapshot's `tau_min` equals the replay's tau,
  otherwise it recomputes h0 from the persisted LWT/offset (then the curve fallback), so a 7-day tau > 0 replay
  of runs recorded at tau 0 is seeded. Pre-#866 snapshots have no `emitter` key and replay with the
  steady-state-at-curve fallback.
- **Plausibility gate**: with tau > 0, `w3_trajectory_plausible` ignores floor and emergency slack in the first
  `ceil(tau/30)+1` slots (heat produced now only reaches the room later, so slack there after a cold start
  is unavoidable); slack later in the plan still vetoes.
- **Min-block smoother**: `DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS` (4) erases more short peak coasts at tau > 0
  (lag-shaped plans have shorter distinct runs). Existing behaviour, not changed here.
- **Report-only fit** (`fit_emitter_tau`, nightly `lwt_learning_daily.payload.emitter_tau_fit`): over 14 days,
  windows of 4 constant-offset slots + a >= 3 K device-offset step + 6 slots. The room is simulated per minute
  with the LP draw model x COP and compared with the logged slot MEAN (`indoor_real_c`), with the start
  temperature and a constant gain solved per event; tau in 0..90 step 10, least SSE. Windows with a DHW reheat
  are dropped (tank temperature rise in `daikin_telemetry`, or `lwt_actual` > curve + max(offset over this and
  the previous 2 slots) + 5 K or > 50 C). Reports `best_tau_min`, `sse_by_tau_min`, `n_events` (after
  filtering), `n_events_dropped_dhw`, `n_events_tank_blind`, `sse_gap_vs_zero`, `sse_spread` and `confidence` (`ok`
  needs >= 6 events, spread >= 0.25, dropped-DHW <= kept and no kept window with a > 40 min hole in the tank
  telemetry (the DHW filter is blind there); else `low`; `none` below 3 events). Known biases: the write moment inside a slot is not
  modelled. NEVER applied.
- Rollout: deploy at 0, read the fit + the ~30-40 min table, replay 7 days at 0 vs tau, then
  `PUT /api/v1/settings` `LP_W3_EMITTER_TAU_MIN`. Kill switch: set it back to 0.

