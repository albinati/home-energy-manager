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

