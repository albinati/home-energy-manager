# Key .env settings (annotated)

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

## Key `.env` settings to know

```
TUNING_REVIEW_ENABLED=true                      # #832 — weekly suggestions-only review (Sun 09:00 local; see its subsection)
OCTOPUS_TARIFF_STRUCTURE=auto                   # #804 — auto|banded|dynamic (dynamic = kill switch)
FOX_SCHEDULER_WRITE_VERSION=v2                  # Open API version for the scheduler WRITE (#777).
                                                 # On 2026-08-06 Fox broke `/op/v3/device/scheduler/enable`
                                                 # for this device: it returns `41200 Failed to load data`
                                                 # for EVERY payload — including the byte-identical group
                                                 # set the same device had accepted hours earlier — while
                                                 # `/op/v0`, `/op/v1` and `/op/v2` all accept it and the v3
                                                 # READ keeps working. Same account, SN, key and signature,
                                                 # so the route itself is the fault; a bogus v3 path returns
                                                 # HTML/404, not 41200, so the endpoint is reached. Nothing
                                                 # changed on our side (container up 13 days, 0 restarts).
                                                 # COST: each failed upload silently leaves the PREVIOUS
                                                 # schedule live, so prod ran a 2-day-old single-group
                                                 # schedule for ~37 h and lost a whole negative-price window
                                                 # (-4.2 p/kWh) with the battery at 16 %.
                                                 # v2 differs from v3 in the body: no `isDefault`, and a
                                                 # per-group `enable: 1`. Precedent: Fox broke the same
                                                 # endpoint with the same errno on 2024-09-13 and fixed it
                                                 # server-side on 2024-09-18. Set `v3` to go back once they
                                                 # fix it. Reads + the v0 flag write are untouched.
DAIKIN_TOKEN_FILE=/app/data/.daikin-tokens.json # absolute inside container; compose pins it
DAIKIN_HTTP_429_MAX_RETRIES=0                   # fail fast on rate limit — do not remove
OPENCLAW_READ_ONLY=false                        # the ONLY hardware-write kill switch (true = safe/dev)
DB_PATH=/app/data/energy_state.db               # absolute inside container
HEM_OPENCLAW_TOKEN_FILE=/app/data/.openclaw-token  # bearer token for /mcp; lifespan creates if missing
HEM_OPENCLAW_TOKEN=                             # leave empty: the file above is the source of truth
API_HOST=0.0.0.0                                # bind inside the namespace; compose ports do the gating
PLAN_AUTO_APPROVE=true                          # default: simulate → auto-apply; set false for explicit consent
PLAN_APPROVAL_TIMEOUT_SECONDS=300               # grace window advertised to OpenClaw for Telegram/Discord buttons
DHW_TEMP_NORMAL_C=45.0                          # restore/safe-default tank target (45 °C = sufficient for normal use)
TARGET_DHW_TEMP_MIN_GUESTS_C=48.0              # guest-mode LP floor (multiple showers at 20:30–22:00).
                                                 # Code default = 48.0 (src/config.py) — calibrated in PR G
                                                 # (2026-05-23) from empirical tank physics: 48 °C ≈ 6 showers.
# DHW_PEAK_TANK_STRATEGY was REMOVED 2026-05-21 (Epic 14, #386). The dispatch
# layer always uses the IDLE behaviour (tank_power=True, tank_temp=DHW_TEMP_NORMAL_C)
# during peak / peak_export windows. Prod telemetry (30d, 8 completed peak
# windows) showed median tank decay 0.00 °C/h — the tank coasts essentially
# perfectly even when held warm — and SHUTDOWN attempts failed 27% of the time
# with READ_ONLY_CHARACTERISTIC errors. Leaving the env line in /srv/hem/.env
# is harmless; remove on next .env touch.
# IMPORTANT: tank pre-charge above DHW_TEMP_NORMAL_C only happens when there's
# an economic reason. The dispatch layer (src/scheduler/lp_dispatch.py, the
# `elif tank_pow:` branch) always FLOORS the setpoint at DHW_TEMP_COMFORT_C
# (48 °C) and picks the CEILING from the slot kind:
#   `solar_charge` → DHW_TEMP_PV_ABUNDANCE_TARGET_C (free PV; storage ceiling)
#   everything else (`negative`, `cheap`) → DHW_TEMP_MAX_C
# There is no separate "cheap → 48 °C" rule: 48 is the FLOOR, and a cheap slot
# rides the LP's own target up to DHW_TEMP_MAX_C. Peak avoidance does NOT
# trigger pre-charging — see issue #322 for conditional shutdown commit.
DHW_TEMP_MAX_C=60                               # hard tank ceiling (code default 60 °C in src/config.py)
# "Banho extra hoje" (#901, Home Água quente card, admin): tank target the one-tap boost
# commands. Clamped to min(DHW_TEMP_MAX_C, t_hp_max_c=50): above the cliff every degree is
# immersion. The firmware only reheats when target - tank > the reheat differential (~6 C,
# resolve_reheat_differential_c), so inside that band the boost also turns DHW Powerful on
# (resistance, COP ~1); beyond it the plain target suffices (heat pump). The plan then leaves
# the tank alone for USER_OVERRIDE_RESPECT_HOURS (4 h), except rows at/above this target and
# Powerful rows (negative-price boosts); afterwards the schedule's target is re-commanded.
DHW_USER_BOOST_TARGET_C=50
DHW_TEMP_COMFORT_C=48                           # tank floor whenever the LP plans DHW heat (runtime-tunable)
DHW_TEMP_PV_ABUNDANCE_TARGET_C=60               # tank ceiling during solar_charge / solar_preheat slots. Code
                                                 # default = 60 (src/runtime_settings.py); runtime-tunable via
                                                 # PUT /api/v1/settings or MCP `set_setting`.
                                                 # NOT a comfort target — PR H (#399) reframed it as the STORAGE
                                                 # CEILING for free PV: the LP's dynamic per-slot reward decides
                                                 # how much heat actually lands (priority battery → tank → export),
                                                 # so raising it does not force the tank to 60 °C.

# --- Forecast night bias (issue #324, minimal) ---
FORECAST_NIGHT_TEMP_BIAS_C=0                    # subtract this from Open Meteo's `temperature_c` when the LP
                                                 # reads forecast slots inside the configured night window.
                                                 # SET TO 0 ON 2026-06-12: the learned per-hour microclimate
                                                 # offset (get_micro_climate_offset_by_hour_c, fed by
                                                 # forecast_skill_log) already corrects sensor-vs-forecast gaps
                                                 # adaptively, so the static -3 double-corrected — skill data
                                                 # showed the raw night residual was only +0.2..+0.7 °C by June
                                                 # (the -3 was calibrated on one cold 2026-05-12 observation).
                                                 # Keep at 0 unless the learned offset is disabled; the LP would
                                                 # otherwise budget nights ~3 °C colder than reality all winter.
                                                 # The CODE default is 0.0 since #702 (it was -3.0 for weeks after
                                                 # prod pinned 0, so every dev/test/fresh deploy silently ran the
                                                 # double-correction). This .env line is now belt-and-braces, not
                                                 # load-bearing; `.env.example` pins it too.
FORECAST_NIGHT_START_HOUR_UTC=21                # bias active from (inclusive)
FORECAST_NIGHT_END_HOUR_UTC=6                   # bias active until (exclusive); wraps midnight when start > end

# --- Slot-centre forecast sampling (2026-06-17) ---
PV_FORECAST_SLOT_CENTRE_SAMPLING=true           # a 30-min slot's energy is `kw × 0.5h`; the honest
                                                 # representative power is the value at the slot CENTRE
                                                 # (start+15min), not the start instant. Sampling at the
                                                 # start attributed each slot's PV energy ~15 min too LATE
                                                 # vs the realised trapezoidal roll-up — a deterministic
                                                 # +15 min lag confirmed over 21 prod days
                                                 # (scripts/diag/pv_time_lag.py; the chart "offset" the user
                                                 # spotted, NOT a UTC/BST bug — timezones audited clean).
                                                 # true → interpolate the weather drivers (temp/rad/cloud/
                                                 # Quartz estimated_pv_kw) at the slot centre in
                                                 # forecast_to_lp_inputs. Calibration/night-bias/scale stay
                                                 # keyed to the slot-START hour (a slot belongs to its start
                                                 # hour; centre never crosses the hour for :00/:30 starts), so
                                                 # the calibration tables are UNAFFECTED — no recompute needed.
                                                 # ROLLBACK: set false in /srv/hem/.env + `systemctl restart
                                                 # hem` for INSTANT rollback to legacy slot-start sampling (no
                                                 # redeploy); or git-revert the PR. NB this leaves the variable
                                                 # forecast-SKILL residual (−57..+65 min, regime-dependent)
                                                 # untouched — that's a separate Quartz/Open-Meteo calibration
                                                 # story, revisit via pv_time_lag.py on post-#564 realised data.

# --- PV forecast rail + adaptive bias (#762, 2026-07-24) -----------------------
PV_CEILING_MARGIN=1.15                          # rail = PV_CAPACITY_KWP × 0.5h × margin (≈2.59 kWh/slot).
                                                 # A SAFETY RAIL, not a calibration: its ONLY correct failure
                                                 # mode is being too LOOSE. A rail that binds on real
                                                 # generation truncates the committed forecast AND censors the
                                                 # error signal the bias corrector trains on.
                                                 # NOT derated by PV_SYSTEM_EFFICIENCY (that's an expected-yield
                                                 # derate, not a limit — 1.9125 already sat below the 1.93 kWh
                                                 # best slot on record) and NOT derived from realised history
                                                 # (pv_error_log is pruned at METEO_FORECAST_HISTORY_RETENTION_DAYS
                                                 # =30, so any trailing window is really "max of last 30 days",
                                                 # which lags the spring clear-sky ramp and collapses after a
                                                 # run of overcast days).
PV_RECENT_BIAS_MIN=0.6                          # clamp TIGHTENED from [0.4, 2.5]. This corrector is a NUDGE on
PV_RECENT_BIAS_MAX=1.4                          # top of the trained calibration tables, never the dominant term.
PV_RECENT_BIAS_MIN_DAYS=3                       # an hour needs samples spanning ≥3 distinct DAYS. A half-hour
                                                 # slot yields 2 samples/hour/day, so the old bare `n>=2` gate
                                                 # let ONE day's weather set the correction — replayed against
                                                 # the real prod DB, a single 96 %-cloud day (23/07) produced
                                                 # factors slammed against BOTH clamps. One day is weather,
                                                 # not bias.
# THE 2026-07-21..23 INCIDENT. The old rail spread Fox DAILY totals over a fixed
# sinusoid — far flatter than a real PV curve — producing 1.32-1.43 kWh/slot at
# 11-13 UTC against a MEDIAN realised 1.35-1.45. It bound on 41 % of 09-16 UTC
# slots. Since pv_error_log stores the committed forecast AFTER clipping, every
# clipped slot read as "under-forecast" and pushed the bias factor UP, while the
# matching down-correction was masked by the clip — a RATCHET. Midday factors
# reached 1.72-2.50 against a measured residual of 0.87-0.90. The inflation only
# bites on CLOUDY days (when the un-inflated forecast would sit below the rail),
# so PV was over-forecast 24-27 %, the battery ended the day at 33-53 % instead
# of 93 %+, and daily net cost hit £1.50/£2.06/£2.35 vs a £0.20-1.00 baseline —
# losing to the old BG fixed tariff for the first time. Raw Quartz was FINE
# (23 Jul: predicted 12.89 kWh, realised 13.34); the correction stack destroyed
# a good signal.
# Two structural guards now: (1) pv_error_log stamps `ceiling_kwh` per slot, so
# censored rows are a RECORDED FACT — never re-derived from today's rail, which
# would silently pass the whole poisoned backlog as clean; NULL (pre-migration)
# rows are excluded as unknown-censored. (2) refresh_pv_recent_bias CLEARS the
# table when no usable samples remain, so a bad factor can't outlive its data.
# Telemetry: `censored_slots_excluded` / `unstamped_slots_excluded` in the
# refresh diag; a WARNING fires if the rail ever materially binds.

# --- Scenario LP for peak-export robustness (see docs/DISPATCH_DECISIONS.md) ---
LP_SCENARIO_OPTIMISTIC_TEMP_DELTA_C=1.0          # +°C applied to outdoor forecast
LP_SCENARIO_OPTIMISTIC_LOAD_FACTOR=0.90          # multiplier on base-load profile
LP_SCENARIO_PESSIMISTIC_TEMP_DELTA_C=-1.5        # −°C; pessimistic case for cold-night protection
LP_SCENARIO_PESSIMISTIC_LOAD_FACTOR=1.15         # 15 % uplift on base load
LP_SCENARIO_OPTIMISTIC_PV_FACTOR=1.05            # ×PV in the optimistic solve (2026-07-02 LP audit)
LP_SCENARIO_PESSIMISTIC_PV_FACTOR=0.85           # ×PV in the pessimistic solve — models a cloud
                                                 # surprise; calibrated from 27d of pv_error_log
                                                 # (daily Σactual/Σforecast p25=0.883). 1.0 = legacy
                                                 # (no PV perturbation, pessimistic kept nominal PV)
LP_PESS_CHARGE_FLOOR_ENABLED=true                # PR B (2026-07-02 audit) — newsvendor charge floor:
                                                 # scenario-bearing triggers re-solve the committed plan
                                                 # with a SOFT floor at the pessimistic solve's SoC
                                                 # trajectory (under-charging for the evening peak costs
                                                 # ~4× over-charging; June backtest: cost-neutral,
                                                 # empty-at-peak slots 4→1). Since PR B scenarios run on
                                                 # those triggers even without peak_export slots.
                                                 # false = instant rollback to median-sized charging.
LP_PESS_CHARGE_FLOOR_TOLERANCE_KWH=0.2           # subtracted from the pessimistic SoC before flooring
LP_PESS_CHARGE_FLOOR_HOURS=24                    # floor only the first N horizon hours (rest is replanned)
LP_PESS_CHARGE_FLOOR_SLACK_PENALTY_PENCE=50.0    # slack penalty — floor behaves hard, can't go Infeasible
LP_SOC_RESERVE_RECOVERY_SLACK_PENALTY_PENCE=50.0 # #789 — when realtime SoC starts BELOW
                                                 # MIN_SOC_RESERVE_PERCENT, the forward reserve floor on
                                                 # soc[1..n] is solved SOFT at this rate (p per kWh of
                                                 # shortfall, per slot) instead of as a hard variable bound.
                                                 # #338/#339 relaxed soc[0] for the hard `soc[0] == measured`
                                                 # equality but left soc[1..n] hard — so the LP had to lift the
                                                 # battery over the reserve INSIDE the first 30-min slot, which
                                                 # the plunge-prep rule and the PV-sufficiency guard both make
                                                 # impossible at night (`chg[i] <= pv_use[i]`, pv=0). Result:
                                                 # 9 Infeasibles on 2026-08-22 20:22-23:05 UTC, held schedule
                                                 # ~3 h. Over 60 days: 0 Infeasible in 1732 solves starting
                                                 # at/above the reserve, 10 in 26 starting below it.
                                                 # A solve starting at/above the reserve is BIT-FOR-BIT
                                                 # unchanged — the relaxation only engages below it, and the
                                                 # penalty is far above any price spread so the floor still
                                                 # behaves hard whenever it is reachable. 0 = no penalty (the
                                                 # relaxation itself is unconditional: an Infeasible solve is
                                                 # never the safer outcome). Telemetry: `soc_reserve_recovery`
                                                 # in lp_inputs_snapshot.exogenous_snapshot_json, written ONLY
                                                 # when it fires, so the key's presence is the query.
LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH=0.30        # commit peak_export only when pessimistic exports ≥ this
LP_SCENARIOS_ON_TRIGGER_REASONS=plan_push,octopus_fetch,tier_boundary,soc_drift,import_overshoot,pv_upside,pv_downside,load_upside,forecast_revision,dynamic_replan,appliance_armed
                                                 # which triggers run the 3-pass solve. #668 added the
                                                 # event-driven re-solve reasons so the pessimistic charge
                                                 # floor also covers mid-day replans; `manual` stays out
                                                 # (interactive latency, not a drift context)
LP_PLUNGE_PREP_HOURS=12                          # PR #218 — bound the pre-plunge constraint
                                                 # to N hours ahead. With the unbounded default
                                                 # (whole 48 h horizon, pre-#218), days where the
                                                 # next negative slot was >24 h away starved the
                                                 # battery — only 33 % of charge slots in cheap
                                                 # quartile. 12 h covers same-day pre-plunge
                                                 # without blocking next-night arbitrage.
LOG_LEVEL=INFO                                   # raise to DEBUG for deep-dive diagnostics

# --- V12 — twice-daily digest + tier-boundary MPC ---
BRIEF_MORNING_HOUR=8                             # local TZ (default 08:00)
BRIEF_MORNING_MINUTE=0
BRIEF_NIGHT_HOUR=22                              # local TZ (default 22:00)
BRIEF_NIGHT_MINUTE=0
NOTIFY_TARIFF_TRANSITIONS=false                  # mute heartbeat cheap/peak pings
                                                 # (negative-price 🔵 always pings regardless)
TIER_BOUNDARY_LEAD_MINUTES=5                     # MPC fires this far before each tier transition
MPC_DRIFT_HYSTERESIS_TICKS=1                     # bumped down 2→1 (V12) — catches heating ramp faster
# PLAN_REVISION_MIN_SOC_DELTA_PERCENT / PLAN_REVISION_MIN_GRID_DELTA_KWH were
# REMOVED with the PLAN_REVISION ping itself — neither is a config attribute
# any more; setting them in .env is a silent no-op.
# LP_MPC_HOURS was REMOVED post-V12 — the fixed-hour MPC cron is gone entirely
# (no longer a config attribute; setting it in .env is a silent no-op). The
# event-driven model (tier_boundary + octopus_fetch + drift + forecast_revision
# + dynamic_replan) covers every signal change.

# --- V13 — nightly post-hoc consumption backfill ---
CONSUMPTION_BACKFILL_HOUR=4                      # local TZ (default 04:00) — Octopus consumption
CONSUMPTION_BACKFILL_MINUTE=0                    # endpoint lags ~24h, 04:00 the next day is safe

# --- Brief expansion (#207 follow-up) — net cost + tariff comparisons ---
# Realised cost in compute_daily_pnl + brief markdown is NET and INCLUDES the
# daily standing charge (apples-to-apples vs shadows). Set MANUAL_STANDING_CHARGE
# above for the Agile standing fee. Set the FIXED_TARIFF_* trio to surface a
# "vs <label>" comparison line in the brief + MCP (e.g. previous fixed tariff).
# Leave any of the FIXED_TARIFF_* values at 0 / empty to suppress the line.
FIXED_TARIFF_LABEL=British Gas Fixed v58         # display label (free text)
FIXED_TARIFF_RATE_PENCE=20.70                    # flat unit rate
FIXED_TARIFF_STANDING_PENCE_PER_DAY=41.14        # daily standing charge
```

`EXPORT_DISCHARGE_MIN_SOC_PERCENT` was **removed** (was the live-SoC global gate that
spuriously dropped tomorrow's peak-export when live SoC was below 95 %). The scenario
LP filter (`src/scheduler/lp_dispatch.py:filter_robust_peak_export`) replaces it.
`EXPORT_DISCHARGE_FLOOR_SOC_PERCENT` is unrelated and still in use — it's the `fdSoC`
parameter sent to Fox in the ForceDischarge group.

## Residual-load profile v3 (#922, epic #920)

Runtime settings (admin UI / `PUT /api/v1/settings`), read by `db.residual_load_profile_v2`:

| Key | Default | Meaning |
|---|---|---|
| `LP_RESIDUAL_PROFILE_V3` | false | Master kill switch. false = the v2 builder, byte-identical. |
| `LP_RESIDUAL_STATISTIC` | mean | `mean` or `median` for the central value (the scenario spread is centre + tier p75/p90 minus tier median, whichever statistic). |
| `LP_RESIDUAL_DOW_SHRINK` | 0.5 | 0..1. value = w x weekday profile + (1-w) x all-days profile per (hour, minute). |
| `LP_RESIDUAL_AWAY_HARD_EXCLUDE` | true | Drop days with a vacation preset (`lp_inputs_snapshot`), Daikin holiday (`daikin_telemetry.holiday_active` on >= 50% of the day's rows) and the first day back. Other non-normal presets (guests) are dropped too but reported as `non_representative_days` (the optimizer already applies `_guests_scale`; no return day). The 0.4 x median heuristic stays as fallback. |

v3 only runs when `LP_RESIDUAL_PROFILE_V2` is true (it is the parent switch).

What v3 changes:
- Heat-pump energy is subtracted per 30-min slot from the MEASURED Onecta 2h buckets. `telemetry_integral` rows are a weather-curve model and are ignored; those buckets get `daily - sum(Onecta)` spread by model weight. A daily row is only trusted if it was fetched after its local day ended; if the Onecta buckets exceed the day total by more than 1 kWh they are scaled down to it (`daily_scaled_days`). No usable daily row = physics.
- A bucket's HP is placed by water-filling: HP is the part of each slot's load above a common level L, with sum(max(0, load - L)) = HP. Metered HP above the bucket's total load (integer quantisation) carries into the next bucket of the day. Buckets with other than 4 slots (gaps, DST) are skipped.
- v2 summed physics per SAMPLE (~10/slot at 3-min telemetry), so its calibration factor was ~10x too small and the residual kept the HP draw.
- Completed appliance runs (`appliance_jobs.actual_kwh`) are removed from slot load, because the LP adds scheduled runs on top.
- Tier counts are slot-days. Spread semantics changed: `spread` = centre + (tier p75 - tier median) (p90 likewise), weekday-shrunk like the centre; it is a day-to-day slot quantile, no longer a sample quantile.
- `LP_LOAD_PROFILE_WINDOW_DAYS` (120) is unchanged; short windows backtest worse. Caches key on the new settings.


## Peak-band conformal margin (#923, epic #920)

Summing per-slot p75 spreads over the expensive band covers the band SUM on only 57-72% of days (slot errors are correlated; quantiles do not add). With `LP_PEAK_MARGIN_ENABLED=true` the pessimistic scenario floors the expensive band (Cosy peak 16-19; Agile `expensive`/`severe_peak` windows) at `nominal + margin`.

- **Residual basis.** Daily error = sum over band slots of (measured load - measured heat pump) minus the committed BASE forecast (`load_error_log.forecast_base_kwh`). The measured HP is #922's hybrid split (`db.measured_residual_by_local_slot`: Onecta buckets + daily remainder + water-fill, never `telemetry_integral`; days without a measured split are skipped). So the margin does not move when the LP's heat-pump model changes.
- **Conformal.** Margin = the `ceil((n+1)q)`-th smallest of the last N days' errors (capped at n), clamped to `[0, LP_PEAK_MARGIN_MAX_KWH]`. `coverage_oos_pct` is rolling OUT-OF-SAMPLE (each day against the margin from the days before it, at least 14 prior), `n_oos` its sample size.
- **Same forecaster only.** Each run stores `exogenous_snapshot["base_forecaster_key"]` (`v3|<statistic>|<dow shrink>|away=<0/1>|bias=<0/1>` or `v2|bias=<0/1>`). Only history days whose latest run (within 36 h before 16:00 local) has the CURRENT key are used; runs with no key count as `v2|bias=0`. After changing any base-forecaster setting the history restarts (margin off, legacy spread, until 14 matching days).
- **Never lowers the pessimistic band.** Per local-day group the band sum is `max(sum of the spread-perturbed band, sum(nominal) + margin)`; when the margin wins it is spread by the nominal shape. Continuous at margin 0. Other slots keep the spread. A band only partly inside the horizon gets the margin scaled by slots / typical band slots. On Agile the band length varies by day.
- **Off the solve path.** Complete days are cached per process. The nightly `bulletproof_load_error_log_job` runs `peak_margin.warm()` (backfills `load_error_log` for window days missing band rows via `rebuild_load_error_log_for_date`, then caches the margin); a solve only repairs yesterday. The first solve after a restart computes the window once.
- **Audit/endpoint.** Each solve logs `exogenous_snapshot["peak_band_margin"]`. `GET /api/v1/load/expected` returns `peak_margin` from the cache only (`fallback="not_computed"` when none; it describes TODAY regardless of `date`): `margin_kwh`, `coverage_oos_pct`, `n_oos`, `n_days` (usable), `min_days` (14), `enabled`, `fallback`.

| Key | Default | Meaning |
|---|---|---|
| `LP_PEAK_MARGIN_ENABLED` | false | Kill switch. false = summed slot spread, byte-identical. |
| `LP_PEAK_MARGIN_QUANTILE` | 0.75 | 0.5..0.95. |
| `LP_PEAK_MARGIN_WINDOW_DAYS` | 42 | 14..90 days of history. Fewer than 14 usable days = legacy. |
| `LP_PEAK_MARGIN_MAX_KWH` | 2.0 | Cap on the margin (also clamped >= 0). |

Peak-margin data rules (#923 follow-up): `measured_residual_by_local_slot` returns a slot only if its 2h bucket is MEASURED (Onecta-metered, or shaped from a COMPLETE daily row); a band with any unmeasured slot drops the day (an unmetered bucket would otherwise count the whole load as residual). A day is cached only when its daily Daikin row was fetched after the day ended; otherwise it is retried after a 1 h negative TTL (yesterday's daily row is partial until the ~02:35 UTC sync). The base compared against is the run's committed PRE-appliance residual (`base_load_components.residual_profile_kwh`) when stored, else `load_error_log.forecast_base_kwh`. The run per day prefers an Optimal `lp_status`. `load_error_log` retention is `max(90, METEO_FORECAST_HISTORY_RETENTION_DAYS)` days (~48 rows/day); away exclusion older than the 30-day `daikin_telemetry` holiday flags is covered by the preset (`lp_inputs_snapshot`, 90 d).


## W3 space-heating floor (#921)

Runtime settings (admin UI, Advanced > Aquecimento). The firmware keeps running its
weather curve; LWT offsets only trim the draw, never stop it, so W3 must not plan a full coast.
The floor is DEVICE-based (not the building model): the draw the curve still has at the deepest
offset the dispatcher writes for a heating slot, `max(OPTIMIZATION_LWT_OFFSET_MIN,
DAIKIN_LWT_LP_OFFSET_MIN)` (lp_dispatch._lp_offsets), so a slot planned at the floor is written
as exactly that offset. No gains term (the curve ignores sun).

| Key | Default | Meaning |
|---|---|---|
| `LP_W3_SPACE_FLOOR_ENABLED` | false | Kill switch. On: `e_space + (floor/max_hp_kwh)*e_dhw_compressor >= floor`, `floor = F*max(0, curve_kw(T_out) + k*min_offset)*0.5`, capped at min(ceiling, HP cap). Skipped when W3 is off, Daikin passive/off or climate on holiday. |
| `LP_W3_SPACE_FLOOR_FACTOR` | 1.0 (0-1.5) | F, multiplier on the minimum draw. |
| `LP_W3_SPACE_FLOOR_TRIM_KW_PER_K` | 0 (0-0.2) | k. 0 = model calibration `get_kw_per_degc_lwt()`. Measured on Onecta-only 2h buckets: 0.043 kW/K (95% CI 0.020-0.070, n=46). |

The DHW term is a linear time-share, so the floor is always satisfiable (legionella at full compressor included).

## LP initial state rolled forward to slot 0 (#929)

| Key | Default | Meaning |
|---|---|---|
| `LP_INITIAL_STATE_ROLL_FORWARD_ENABLED` | true | Kill switch (runtime setting). false = SoC and tank seeded with the values read at run time, exactly as before. |

Slot 0 is the next half-hour boundary, so the readings are up to ~29 min (+ cache age) stale. Oct 2026 audit over 782 runs: SoC |err| mean 0.43 kWh, p95 1.86, max 3.32; tank |err| mean 0.61 C, p95 4.0, max 15.3.
- SoC: measured Fox battery power (`batChargePower - batDischargePower`) held over reading -> slot 0, clamped to the inverter limit and [reserve, capacity]. No roll when the power is older than 5 min or absent. A ForceCharge/ForceDischarge group of the latest uploaded schedule that STARTS between the reading and slot 0 contributes its `fdPwr` from its start (stops at `fdSoc`), because the measured power cannot show it yet (the cheap-band start is the biggest residual).
- Tank: when the last two live samples show a rise >= 0.4 C the rate is extrapolated, capped at the setpoint (or `DHW_TEMP_MAX_C` when the telemetry setpoint is below the reading, i.e. Powerful/boost). Standing loss (tank params #819) is applied only when it exceeds 0.5 C: the sensor is quantised to 1 C and applying the raw loss made the audit error worse.
- The roll is recorded in the snapshot (`exogenous_snapshot.initial_roll_forward`). When W3 is enabled but there is no fresh indoor reading, a warning is logged and `exogenous_snapshot.w3_disabled_reason` = `no_fresh_indoor_reading`.
