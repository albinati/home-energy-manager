# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Major versions track significant LP / dispatch architecture iterations. Minor versions add features behind feature flags or under runtime tuning. Patch versions are bug-fixes that preserve behaviour on every released config.

## [Unreleased]

_Nothing yet._

## [14.0.0] — 2026-07-06

**Headline: the house learns to feel itself.** For its whole life the HEM
optimised a house it could not feel — the Altherma exposes no room stat, so
indoor temperature was never measured and heating rode a blind weather curve.
v14 closes that loop end to end: **measure → learn → use → keep.** An ESPHome
room sensor now streams the indoor temperature in; the W2 learner distils the
building's own thermal physics (τ / UA / C) from how it cools at night; the LP
regains the indoor-temperature state variable it lost in Phase B, so it can
pre-heat off cheap power and coast the peaks while holding a comfort floor; and
a tiered data lifecycle keeps every reading — full-resolution and ML-ready —
without ever growing the storage-constrained box. The thermal machinery ships
dormant-by-design (summer offers no signal to learn from and no heating to
optimise) with the whole state made watchable, so it self-activates as the
weather turns.

### W1 — the house gets a thermometer

- **Indoor ingestion, internet-exposed but contained.** An ESPHome sensor
  POSTs to `POST /api/v1/sensors/indoor` through the existing hem-ui Tailscale
  funnel (`:8443`), carrying a **scoped `HEM_SENSOR_INGEST_TOKEN`** that unlocks
  that one write route and nothing else — never an admin read, never another
  endpoint. A firmware/network leak can only post fake temperatures to one path;
  rotate the token to revoke a device. (#646)
- **Lossless per-device logging.** Two sinks: `room_temperature_history` keeps
  only the in-band temperature the model reads, while `device_reading_log` is a
  lossless audit of *everything* a device sends (temp / humidity / pressure /
  MAC / and any extra field via a `payload_json` blob), so no future signal is
  dropped for want of a migration. (#647)
- **Read like tank/Fox, not polled.** The freshest reading folds into
  `/cockpit/now` (dropping the fictional Daikin room-temp read that never
  worked), and the cockpit grew an Outside | Inside climate hero, an indoor
  climate card, and a heating chart that draws the realised indoor line beside
  the plan. (#648–#653, #655)

### W2 — the house learns its own physics

- **Thermal learner.** τ from the overnight indoor-decay curve (integral ODE
  fit), UA re-fit against heating-degree days using the *measured* indoor
  baseline, and `C = τ·UA` — a single `building_thermal_calibration` row,
  refreshed nightly, gated behind quality thresholds so it only trusts clean
  signal. (#641)
- **Made observable.** A "Thermal model" card surfaces the effective τ / UA / C,
  a `defaults | learned` badge, and the convergence toward activation
  (`0/5 cold-decay nights · 1/20 heating days`) — honest about the summer
  dormancy rather than forcing garbage. (#656)

### W3 — the house heats to comfort, timed to price

- **`t_in` restored to the LP.** The indoor-temperature decision variable +
  RC thermal dynamics that Phase B removed (there was no sensor then) are back
  behind `LP_W3_TIN_ENABLED` (default **off** → byte-identical to today). When
  on: a soft comfort floor (night 17.5 °C / day setpoint) that can never go
  Infeasible, a gentle-recovery cap on the pump's own contribution (no morning
  blast), and the per-slot heating floor superseded so the LP can front-load
  heat into cheap slots and coast peaks. An adversarial review caught — and this
  release fixes — a warm-weather infeasibility where the recovery cap fought the
  RC equality. (#657)
- **Surfaced in the UI.** The thermal card shows the active strategy
  (`weather curve` → `comfort-optimised`), and the heating chart carries the
  LP's committed indoor plan as a dashed line beside the realised sensor line.
  Both dormant until the flag flips. (#658)

### Data — keep everything, grow nothing

- **Tiered sensor-data lifecycle.** The append-only sensor tables had no
  retention. Rather than delete history (valuable for future ML), it is tiered:
  **HOT** raw in SQLite (90 d indoor / 30 d device-log); **WARM** a permanent
  15-min rollup for long-term trends; **COLD** the full-resolution raw *and* a
  wide, 15-min-aligned ML-ready join (indoor / outdoor / LWT / tank / SoC / heat
  / price / tier) gzip-archived to monthly files **before** anything is pruned.
  ~15× smaller than raw SQLite, nothing deleted without a compressed copy
  landing first. (#659)
- **Long-term indoor-trend card** on Insights (mean + min/max band, 30 d / 90 d /
  1 y), reading the permanent rollup so it outlives the raw. (#660)

### Also in this release

- **Negative-window holds use Fox Backup mode** — an owner decision validated
  against 35 days of prod work-mode telemetry; the slot labeller now ranks
  negative price above solar_charge (the real 2026-06-28 root cause). (#630,
  #631, #632, #635)
- **DHW forecast: per-bucket shape corrector** + a one-shot Telegram ping when
  the enable gate is met, and the firmware legionella cycle is now budgeted in
  the LP. (#640, #642, #643, #645)
- **CI no longer hangs** — `pytest-timeout` + a job cap + a conftest guard that
  blocks a test from fanning out live Octopus calls. (#654)
- **UI hardening** — interval-true chart geometry, committed-forecast overlays
  on week/month charts, sliding day navigation, and a design-token conformity
  pass. (#621–#628, #636, #637)

## [13.0.0] — 2026-07-02

**Headline: measure, then trust.** v13 is a month of instrument-building and
the corrections those instruments forced. The measurement-integrity audit found
(and fixed) the Fox client inflating winter solar ~8×, the DHW forecast running
+45% with the wrong shape, a deterministic +15-min PV attribution lag, and an
LWT offset that had been phantom-heating the house all June. On top of the
now-trustworthy numbers sits the winter-readiness LP stack: scenario solves
that perturb PV (calibrated from 27 days of measured error), a
newsvendor-style pessimistic charge floor on every committed plan
(under-charging for the evening peak costs ~4× over-charging), and a live
negative-window audit whose fixes let the LP finally collect paid imports at
full depth. The system now watches itself — nightly LP health self-check,
actuation-freshness alerts, a guests-elevation detector that *asks* instead of
guessing — and deploys collapsed to one command with auto-rollback. The
cockpit finished its redesign into an ops console that loads in 233 ms.

**Theme (2026-06-06): active space-heating control + a self-correcting solar
forecast + a UI that explains itself.** HEM now (optionally) drives the Daikin
leaving-water-temperature offset to pre-heat the house off cheap power and coast
the peak; the PV forecast closes a feedback loop on its own realised error so it
stops being chronically pessimistic about the morning sun; and the dashboard
gained a heating-plan timeline, a load-composition view, and a weather card. The
two control-side features ship **off by default** (`DAIKIN_LWT_PREHEAT_ENABLED`,
`PV_RECENT_BIAS_ENABLED`) — enabled on the prod lab to observe before promotion.

### Added — active climate control
- **Heuristic LWT pre-heat** (#481, PR #482). Price-tier offset: `+BOOST` in
  cheap slots, `PEAK_SETBACK` in peak, neutral otherwise, only while the firmware
  is plausibly heating (`outdoor < DAIKIN_WEATHER_CURVE_HIGH_C`). Integer offset,
  quota-safe (idempotency + deterministic write-budget cap + zone-off skip).
  First active space-heating since the 2026-05-09 climate-hands-off freeze; the
  `state_machine` `lwt_offset` strip is gated on the new flag so the offset
  actually reaches the device. Knobs: `DAIKIN_LWT_PREHEAT_{ENABLED,BOOST_C,
  PEAK_SETBACK_C,COMFORT_BAND_C}` (sensor-ready comfort guard pre-wired).
- **Thermal-coherence smoothing of the offset** (PR #485). A building's thermal
  mass has a multi-hour time constant, so the per-slot offset is smoothed into
  sustained blocks (`DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS`, default 4 = 2 h):
  short 0-gaps between equal blocks are bridged, sub-2 h blocks dropped. Kills
  the `+3/0/+3/0` chatter at the cheap threshold and the wasted Daikin writes.
- **LWT-offset drift backstop** (#461 ask 1, PR #492). When HEM owns the offset
  (pre-heat enabled) and the live device holds a non-zero offset no plan slot
  justifies, the heartbeat resets it to 0 — after respecting a still-in-effect
  user gesture for `USER_OVERRIDE_RESPECT_HOURS`. Catches a manual offset
  (Onecta / physical) that has no paired restore. Mirrors the tank-power drift
  backstop; gated by `LWT_OFFSET_DRIFT_{CHECK_ENABLED,AUTO_RECOVER}`.

### Added — appliance scheduling
- **Learned `typical_kw` from measured history** (#222). The cycle-energy
  estimate the LP and dispatch use now prefers the rolling mean of recent
  completed runs' measured `actual_kwh` (`kW = actual_kwh / duration`) over the
  static registration default, once `APPLIANCE_LEARNED_KW_MIN_SAMPLES` (3) runs
  exist — `db.appliance_learned_typical_kw` over the last
  `APPLIANCE_LEARNED_KW_LOOKBACK` (10). Fixes the ~3× over-estimate (registration
  0.5 kW vs measured ~0.2–0.4 kW eco cycles) that made the LP route around the
  wash more than necessary. Applied at `_arm_or_replan` (window picker) and both
  LP residual-load overlays; the σ-based safety margin (#235) is unchanged.
  `GET /api/v1/appliances` now returns `learned_typical_kw` + `learned_samples`
  + `effective_typical_kw` so the learning is visible.

### Added — adaptive solar forecast
- **PV recent-bias corrector** (#486, PRs #488 + #489). A convergent feedback
  loop: per UTC hour, the recency-weighted mean of `actual/forecast` from
  `pv_error_log` (the committed forecast's own residual error) nudges the
  day-ahead PV forecast — **warm-started** to the full measured correction from
  history, then damped-accumulated nightly for stable tracking. Because it's
  driven by realised error (not clear-sky potential), genuine morning shade is
  left alone while systematic under-forecast is corrected. Fixes the
  overnight-over-import-then-midday-export pattern (audited: morning forecast was
  2.1–2.7× too low). Separate from the calibration tables (no training
  contamination). Knobs: `PV_RECENT_BIAS_*`. New `pv_recent_bias` table; nightly
  refresh chained after the `pv_error_log` rebuild.

### Added — dashboard (Preact/ECharts SPA)
- **Heating-plan timeline** (#481 follow-up, PR #484/#485). One continuous
  D-1·today·D+1 chart in the Today's-plan idiom: outdoor temp → weather-curve LWT
  (faint) → actual radiator LWT (curve + offset, bold — the gap *is* the offset)
  → tank target, with heating-on shading, negative-price bands, now-marker.
  Backed by a deterministic `/api/v1/daikin/heating-plan` endpoint (recomputed
  per slot, no overlapping `action_schedule` rows).
- **Weather card** (PR #487). Apple/Tesla-style, solar-home tuned: current
  condition from cloud cover, today's solar generation sparkline + kWh expected,
  and an hourly strip (temp / sky / PV potential). `/weather` slots now carry
  `cloud_cover_pct` + `irradiance_wm2`. All inline SVG, no chart engine.
- **Load details** (PR #483/#485). The old "Energy flow" widget is now
  load-specific: day view is a stacked composition (base + appliances + heat
  pump = where the energy goes) with a household-demand forecast overlay; the
  grid/solar/export series moved out.

### Added — LP load forecast
- **Day-of-week residual-load forecast v2** (#477, PR #478). Day-of-week buckets
  + measured-Daikin-split calibration + median/p75 spread, one unified builder
  across the 6 call sites; scenario variance from the spread. Kill-switch
  `LP_RESIDUAL_PROFILE_V2`; Insights "when you spend the most" heatmap.

### Changed/Fixed — hero money reframe + correct British Gas comparison (2026-06-07, UI Phase 3a)
- **The hero "saved vs fixed" used the wrong shadow.** It read the generic
  `delta_vs_fixed_real` (~23p fixed rate + the *Agile* standing) and labelled it
  "British Gas", inflating the saving (£5.80 vs the correct £5.10). The realised
  net (−£0.26 credit) was always correct — the £0.59 standing charge legitimately
  eats most of the negative-price energy credit. Fix: `/energy/today-cumulative`
  now exposes the **configured fixed-tariff** fields (`delta_vs_fixed_tariff_real_gbp`,
  `fixed_tariff_shadow_real_gbp`, `fixed_tariff_label` — British Gas's own
  20.7p + 41.14p standing, import basis) + concrete `earnings_today_gbp`
  (negative-import credit + export). No `pnl.py` change — the British-Gas fields
  were already correct; the hero just used the wrong one.
- **Hero reframed:** one deduped British-Gas line (was 4 "vs fixed" renders), the
  day's bill ("Conta hoje: crédito £X"), and the concrete "⚡ foi pago £Y (£A
  import negativo + £B export)" shown only on credit days. The
  import/standing/export `CostBreakdownChart` is replaced by an **SVG price
  timeline** (today's import price by tier + now-marker; no ECharts — the hero is
  above the fold).
- **Appliance widget** now shows the **next/cheapest available** window even when
  none is below threshold ("próxima HH:MM · Xp (sem janela barata)") instead of
  "Sem janela barata" — `compute_appliance_window_suggestions(always=True)` +
  `meets_threshold`. The push/nudge path keeps the threshold.

### Added — hero "saved today" + appliance-schedule widget (2026-06-07, UI Phase 2)
- **Hero shows today's real-money savings** at a glance, independent of the period
  selector: "Hoje: crédito £X · economizou £Y (Z% abaixo do fixo)". Reuses
  `compute_daily_pnl` — `/api/v1/energy/today-cumulative` now also returns
  `realised_net_cost_gbp` + `delta_vs_{fixed,svt}_real_gbp` + `*_shadow_real_gbp`.
- **New Appliances widget** (medium, next to Heating) — at a glance: running /
  scheduled (window + avg price, from `/appliances/jobs`) / idle-with-a-cheap-
  window-ahead ("janela paga|barata HH:MM — carregue + Smart Control", from a new
  read-only `GET /api/v1/appliances/suggestions` wrapping
  `compute_appliance_window_suggestions`) / register hint when none. Best-effort:
  degrades to empty when SmartThings/Fox unconfigured.

### Changed — cockpit performance: server-side TTL caches + Cache-Control (2026-06-07)
- **The cockpit loaded slowly** because `/weather` + `/pv/today` hit Open-Meteo and
  `/energy/period` (day/week) hit Fox ESS on EVERY request with no server-side cache
  (0.5–2 s blocking HTTP each). Added in-process TTL caches (no Redis — single
  container): a shared forecast cache (`weather.fetch_forecast_cached`, 15 min) feeds
  both `/weather` and `/pv/today` from one fetch; a day/week period-insights cache
  (20 min, mirrors the existing 1 h month cache) makes period-nav instant. Plus short
  `Cache-Control` (`private, max-age=…`) on the read-heavy cockpit GETs so a hard
  refresh / tab-return doesn't re-hit everything. Knobs
  `WEATHER_FORECAST_CACHE_TTL_SECONDS`, `ENERGY_PERIOD_CACHE_TTL_SECONDS`.
- **Better Fox realtime freshness without a request-path fetch:** the pv-telemetry
  background job is now the canonical Fox-snapshot refresher (forces a read older than
  `FOX_SNAPSHOT_REFRESH_MAX_AGE_SECONDS`=60 s), so lowering `PV_TELEMETRY_INTERVAL_MINUTES`
  (e.g. 5→2) tightens the cockpit snapshot to ~2 min while the API reads stay cache-only
  (never fetch on the request path). ~720 Fox calls/day, well under budget; zero Daikin impact.

### Added — pin maxSoc on negative-hold so solar can't waste paid-import headroom (2026-06-07)
- **`negative_hold` (Backup) slots now emit `maxSoc = reserve floor`** so PV can't
  trickle-charge the battery during the hold phase of a negative-price window
  (Tracked by #498). Live data showed Backup with `maxSoc=None` let free solar
  creep the battery 10→21% mid-window — banking free PV at the exact time you'd
  rather be *paid* to import. With the pin, surplus PV exports at SEG and the
  battery refills from the paid force-charge instead. Pure post-solve Fox dispatch
  mapping (`_slot_fox_tuple`) — the LP objective is unchanged. **The Fox wiki says
  Backup normally lets PV charge and the maxSoc-pin is undocumented → it must be
  empirically confirmed to clip PV on the H1.** Gated + kill-switch
  `LP_NEGATIVE_HOLD_PIN_MAXSOC` (default true). No simultaneous import/export and
  no PV curtailment exist on this inverter (researched) — this is the one real
  lever.

### Added — proactive appliance load nudge (2026-06-07)
- **Nudge the user to LOAD the washer/dishwasher for an upcoming negative/cheap
  window** (Tracked by #498). HEM can't load the machine (the physical
  Smart-Control button is the consent gate), so when day-ahead rates land
  (`octopus_fetch`) and a registered appliance is *idle*, it pushes ONE Telegram
  nudge with the recommended run window, deadline, and est. cost (negative = paid
  to run). Debounced once per appliance per window via `runtime_settings`
  (restart-safe). Negative-only push by default; the morning/night brief carries
  a softer cheap-window suggestion line (pull). Reuses the existing cheapest-
  window picker + deadline roll — both already prefer negative slots; the only
  gap was the heads-up. New `AlertType.APPLIANCE_WINDOW_NUDGE`; knobs
  `APPLIANCE_WINDOW_NUDGE_{ENABLED,PRICE_THRESHOLD_P,HORIZON_HOURS,BRIEF_THRESHOLD_P}`.
  Note: the deadline already rolls to tomorrow when passed, so a stale 07:00
  deadline was never the blocker — the missing load + nudge was. Skips the nudge
  when the machine is already running/paused or loaded with remote control on
  (live SmartThings check — the active-job check alone misses a manually started
  cycle); degrades to nudging if SmartThings is unreachable.

### Added — legionella tank stand-off (2026-06-07)
- **HEM stands off the DHW tank during the firmware's weekly thermal-shock
  cycle** (Tracked by #498). The Onecta firmware owns the tank during legionella,
  so any tank PATCH HEM sends is arbitrated/overridden (wasted quota + churn +
  `READ_ONLY`). The reconciler now skips tank-device writes inside a configured
  window and leaves those rows pending so they resume once it closes. **TANK ONLY
  — LWT / space-heating still fire.** Window defaults to Sunday 11:00 UTC for
  120 min (ramp + ~1 h hold); knobs `DHW_LEGIONELLA_STANDOFF_{ENABLED,DOW,
  START_HOUR_UTC,START_MINUTE_UTC,DURATION_MINUTES}`. Telemetry:
  `legionella_tank_standoff` in `action_log`. The LP already budgets the cycle's
  heat-up energy, so only the actuation side needed the guard.

### Fixed — 2026-06-07 negative-price-window incident (live)
- **LWT drift backstop reset a legitimate offset from a completed row** (#497).
  Pre-fire idempotency marks an applied `lwt_preheat` row `completed`; the drift
  backstop only treated `pending`/`active` rows as justification, so a still-in-
  window completed +10 was reset to 0 mid paid-window. The backstop now honours a
  `completed` row whose window still covers now.
- **DHW cycle-split dropped the live cycle's negative-price boost** (#499,
  Tracked by #498). The tank "day" anchors at `DHW_WARMUP_START_HOUR_LOCAL`
  (13:00), so before that hour *now* is inside yesterday's cycle; the writer only
  emitted today+tomorrow and the past-date guard dropped the rest, so an early-
  morning paid boost (e.g. 04:00→12:00 UTC) was lost on every overnight re-plan.
  New `generate_daily_tank_schedule(boosts_only_as_of=)` re-emits just the live
  cycle's `tank_negative_boost` rows at their natural (stable) start.
- **Respect a manual LWT/tank gesture until the planned window ends** (#499).
  `USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END` (default true) keeps a manual override
  in effect while the overridden row's own `end_time > now`, not just the fixed
  `USER_OVERRIDE_RESPECT_HOURS` — so a hand-set tank during a multi-hour boost is
  left alone for the whole window. The live `user_gesture_still_in_effect` check
  stays the safety gate (revert → HEM resumes). No new Daikin polling.
- **Boost recovery idempotency + override window-end boundary** (#500, review-
  caught). The recovery clipped each boost to the advancing LP-horizon start, so
  `upsert_action` (keyed on `start_time`) inserted a fresh row per re-plan instead
  of refreshing one; now emits the stable natural start. `find_recent_user_override`
  compared a `Z`-form `end_time` against a `+00:00` now (`'Z' > '+'`); normalised.
- **Live-cycle boost never fired (wrong `plan_date`)** (#501, caught by verifying
  the live fire). The heartbeat reconciler selects rows by
  `get_actions_for_plan_date(today_local)`, but the recovery stamped the boost
  with the live cycle's anchor date (yesterday) → today's reconcile never selected
  it. New `plan_date_override` files the recovered boost under today.
- **Fox V3 merge froze the battery** (#479, PR #480). `_coarse_merge_fox` took
  `max(minSocOnGrid)`, so a midday `solar_charge` hold (min 100) merged with the
  evening discharge windows (min 10) → the battery couldn't discharge through the
  peak it was charged for. Now only same-floor SelfUse windows merge.
- **`_tank_at` masked negative-price boost** in the heating-plan endpoint
  (review-caught, PR #484): the full-span setback row matched before the boost
  sub-interval. Boost windows are now preferred.

### Added — cockpit redesign → ops console, at 233 ms (2026-06-09 → 06-13)
- **Redesign phases 1–5** (#524–#531): Claude-Design handoff ported — visual
  foundation, Hero + Weather, consumption by-source + SoC, live band + radial
  tank gauge + chrome period control, fidelity pass. Non-negotiables held:
  deep-dark, borderless, no-emoji, semantic color (`DESIGN.md` added as the
  design-system source of truth, #600).
- **Ops console** (#552–#556): AlertStrip (caught a real Fox schedule drift on
  day one), Self-check panel, System-health card, Operate card (admin cluster:
  mode, replan, scheduler pause, appliance cancel), full-width power flow;
  ops-status endpoints + fair-compare TTL cache (#553); mode-aware
  `schedule_diff` fingerprint.
- **Performance** (#557–#559): the 8.5 s cockpit wall was the single-threaded
  event loop serializing synchronous PnL — not the server being busy. Lifetime
  aggregate endpoint + `/metrics` TTL cache + nginx viewer micro-cache (10 s,
  admin bypass) → **233 ms measured**. Layers 4–5 recorded as over-engineering
  and deferred (`docs/COCKPIT_PERF.md`).
- **June UI batch** (#580–#604): period-nav immutable past-period cache + Today
  button; real intraday consumption chart for past days; committed forecast vs
  actual-blend labelling; heatmap cold-rebuild 504 stampede fix; a11y HIGH
  fixes (chart text alternatives, modal focus trap, h1 outline); stale
  lazy-chunk auto-recover after deploys; hero weather 24 h range + 3-day
  forecast; Outgoing-Agile export framing; live-card polish.

### Added — self-hosted Quartz solar forecast (2026-06-12)
- **`hem-quartz` sidecar** (#542, PRs #544–#551): the expiring api.quartz.solar
  token turned out to be the commercial product; the site-level model is free.
  New container (python 3.11, xgboost site-level `quartz-solar-forecast`)
  mirrors the open API schema; provider falls back Quartz→Open-Meteo. Seven
  build fixes to get there on ARM64 (native runner — QEMU poisons numcodecs'
  SIMD probe; multi-stage build; cattrs/requests pins; tmpfs CWD cache).

### Fixed — measurement integrity (the mid-June audit)
- **Fox PV was `generation`, not PV** (#563, PR #564): the Fox client mapped
  AC output *including battery discharge* to `solar_kwh`, inflating winter
  solar/self-sufficiency ~8×. Now `PVEnergyTotal`. £/PnL unaffected (meter-
  based); Daikin/Octopus/Quartz audited clean of the same class.
- **Fox V3 uploads wedged for 41 h** (#561): two composing bugs — drift
  comparators not mode-aware (stale `fd_*` echo on SelfUse groups) and the
  in-flight bridge refusing whole uploads on overlap — let a stale schedule
  grid-force-charge at +13–15 p for ~41 h. Shared `_group_fingerprint` +
  all-groups bridge guard + drop-bridge-on-overlap. Plus **actuation-health
  alerts** (#562): `/status/alerts` gains an `actuation` block (Fox upload age,
  Daikin tank write age/fails, LWT fails; vacation-gated) → AlertStrip — the
  gap that let it run silent is closed.
- **DHW forecast recalibrated** (#536): was +45% with the wrong shape; new
  measured shape + trailing measured/nominal auto-scale. **Backfill trailing-
  window sweep** (#535) recovered a lost month of metered data + staleness
  alarm in the brief. **forecast_skill_log coherence** (#537): the skill log
  now measures the same PV forecast the LP consumes. `FORECAST_NIGHT_TEMP_BIAS_C`
  → 0 (the learned per-hour microclimate offset already corrects it; the
  static −3 double-corrected).
- **PV slot-centre sampling** (#602): a 30-min slot's honest representative
  power is the slot *centre*, not the start — start-sampling attributed PV
  ~15 min late, a deterministic lag confirmed over 21 prod days (the chart
  "offset" was not a timezone bug; tz audited clean end-to-end).
- **Tariff plumbing**: single source of truth for the Agile standing charge
  (#573); consistent tariff framing + live standing charge in Insights (#565).

### Fixed — LWT phantom heating (June)
- **The active LWT offset was waking the compressor in summer**: 0→4.6 kWh/day
  of phantom space heat since Jun 5. Outdoor-temperature cutoff on positive
  offsets + thermal-lag tail decontamination (#583); Insights residual/HP
  profiles decontaminated retroactively (#585); LWT demand gate + calibration-k
  decontamination + measured thermal constants (#541).

### Added — winter thermal groundwork (epic #540)
- **Winter thermal model** (docs, PR #539): measured UA ≈ 630 W/K via HDD
  regression, gap analysis, sensor-first W1–W4 build plan
  (`docs/WINTER_THERMAL_MODEL.md`). **W1 indoor-temperature ingestion pipe**
  shipped (#572).

### Added — load-forecast measurement loop
- **Per-slot `load_error_log`** (#569) + gated recent-bias corrector (default
  OFF, #570) + Insights load-accuracy card (#571) + negative-price slots
  excluded from the residual sample (#566) + heat-pump heatmap breakdown, Tank
  vs Heating (#568, #575). **ML feasibility study verdict: don't build** —
  6–9% OOS gain is marginal; the real lever is heat-pump *timing* (#603,
  read-only study).

### Fixed — negative-window dispatch (late June)
- **Powerful sustained through boost windows** (#606): Daikin auto-clears
  Powerful; a bounded-cadence backstop re-asserts it while a negative boost
  window covers now.
- **ForceCharge on `negative_hold`** (#607): SelfUse holds let the battery
  self-discharge into house load during paid-import slots.
- **Export-aware curtailment penalty** (#608) and **grid-import cap decoupled
  from the inverter rating** (#609, main fuse is the real limit).

### Added — the system watches itself
- **Nightly LP health self-check** (#611, review hardening #613): objective
  drift vs a rolling baseline, Infeasible count, scenario-spread sanity, floor
  insurance/slack — alerts only on regression, silent when healthy.
- **Guests base-load scaling + elevation detector that ASKS** (#610): the
  guests preset finally scales base load (×`LP_GUESTS_BASE_LOAD_SCALE`, 1.3);
  a nightly detector on sustained load elevation (ratio > 1.15, ≥3/4 days)
  prompts "visitas em casa?" via Telegram instead of auto-applying — the human
  knows, the system remembers.
- **One-command deploys** (`deploy/rollout.sh`, #617): manifest guard → tag pin
  → health-verify with auto-rollback → image prune (keep current + previous).

### Added — winter-readiness LP stack (2026-07-02 adversarial audit)
- The audit measured the LP capturing **68% of perfect-knowledge value** (June:
  realised £6.07 vs no-battery £38.75; policy-compatible recoverable gap only
  ~£5/mo). Four improvements followed:
- **Scenarios perturb PV** (#614): pessimistic ×0.85 / optimistic ×1.05,
  calibrated from 27 days of `pv_error_log` daily ratios (p25 = 0.883); PV-only
  perturbation preserves the calibrated COP series.
- **Pessimistic charge floor** (#615): scenario triggers re-solve the committed
  plan with a SOFT floor (slack + 50 p/kWh) at the pessimistic SoC trajectory —
  the newsvendor answer to "under-charging for the evening peak costs ~4×
  over-charging". June backtest: cost-neutral, empty-at-peak slots 4→1.
  Pre-negative-export and negative-price slots exempt. Floor insurance/slack
  feed the LP health monitor. Kill switch `LP_PESS_CHARGE_FLOOR_ENABLED`.
- **`dhw_error_log`** (#616): committed DHW forecast vs realised Daikin energy
  per local 2-h bucket (cron 04:24 UTC) — first prod day immediately surfaced
  the warmup bucket ~4× under-forecast. Plus **hold/fill class-aware
  ForceCharge merge**: `negative_hold` no longer swallowed by fill-to-100
  groups, so the fill defers to the deepest-priced slots.

### Fixed — live negative-window audit (2026-07-02, #618 → #619)
- Audited the running 15-slot window (−1.5..−4.4 p) against realised telemetry:
  captured well (+£0.30 net credit, battery 10→100% in-window, washer
  auto-scheduled at −0.53 p effective) — with three structural defects fixed
  same-day:
- **PV-sufficiency guard now exempts negative-price slots**: its premise
  ("grid-charging is wasteful when PV covers demand") inverts when the grid
  *pays* for import — it had pinned the plan to PV-only charge + curtailment
  across the whole paid window.
- **Directional MPC drift gate**: SoC running ahead of prediction while the
  plan reaches that level within 3 h is early arrival (Fox fills faster than
  the LP taper), not drift — kills the 5-min re-solve bursts whose group swaps
  glitched the battery into discharging at −3.3 p. Staleness-capped;
  below-prediction always fires.
- **Powerful stall backoff**: the tank's compressor-only DHW ceiling (~51 °C on
  hot days — manual Powerful via the app doesn't move it either) had HEM
  re-writing Powerful every 15 min for 5 h (24 writes ≈ 12% of the Daikin
  quota, zero gain). After 4 no-progress successful writes the cadence
  stretches ×4; progress, a colder tank, or a 6 h gap resets.

### Added — appliance UX (late June)
- **Prompt arm detection + quieter pings** (#605): arm-confirm and finished
  notifications only; the play-by-play is gone (pull-based preference).

### Maintenance
- Date-relative test flakes killed (#588, #612 — seed relative to the current
  clock, not July); README refresh for the public repo (#560); QA a11y
  ISSUE-001 (#601).

## [12.0.0] — 2026-05-20

**Headline: residual-class LP-Infeasibility surface closed.** The 60-day audit of `optimizer_log` found that 8 of 9 above-reserve Infeasible events clustered at the 21:00–21:25 BST tier-boundary fire — slot 0 of the new horizon falling inside the 21:30 BST evening shower window from a cold tank, physically un-liftable to 45 °C in a single 30-min slot. v12 ships the soft-floor fix plus a defensive layer for the remaining classes, snapshot-based diagnostics for any future Infeasibles, and a 200+ binary MILP cleanup.

Honest-mode regression: **−£1.09 over 14 d** of prod-snapshot replay (the new LP code is strictly cheaper than v11.x when applied to past snapshots with their own configs). Forward mode: +£0.88/14 d (≪ £0.06/day, within solver noise).

### Added — LP correctness + diagnostics
- **Shower-window soft floor** (#344). `tank[i+1] >= t_min_dhw` on shower-window slots is now `tank[i+1] + s_shower_lo[i] >= t_min_dhw` with a heavy 50 p/K-slot penalty (configurable via `LP_SHOWER_LO_PENALTY_PENCE_PER_DEGC_SLOT`). The LP heats as fast as physics allows; the slack quantifies the unavoidable deficit instead of returning Infeasible. **The actual residual-class fix.**
- **Appliance-aware infeasibility retry** (#342). When `_run_optimizer_lp` returns Infeasible AND the base_load had a non-zero appliance contribution, the optimizer drops the appliance kWh and re-solves once. Ships the appliance-blind plan if it clears (APScheduler cron untouched; appliance still fires at its planned time). Falls through to held-schedule on double-fail.
- **Infeasible-run input snapshot** (#341). `_persist_lp_snapshots` now runs on the Infeasible branch too, with a new `lp_status` column on `lp_inputs_snapshot` distinguishing successful solves from captured-but-unsolvable inputs. Per-slot solution rows are skipped (no decision vector exists).
- **Infeasible-snapshot replay** (#347). `lp_replay.replay_run()` learned to handle `lp_status='Infeasible'` snapshots — derives the slot window from `run_at_utc + horizon_hours` and pulls prices from `agile_rates`, then re-runs `solve_lp`. The diagnostic loop is closed: snapshot → reload → reproduce.
- **PV-sufficiency guard rail + daily PV-calibration refresh** (#331). In `strict_savings` mode, when forecast PV today ≥ battery headroom + remaining daytime load × margin, block grid→battery for pre-peak slots. Daily 04:30 UTC `pv_calibration_hourly` refresh.
- **Tariff-window-aware base-load forecast** (#311). Residual-load profile splits per `(hour, minute, tariff_kind)` instead of `(hour, minute)` alone — same clock time can have different typical loads under cheap vs peak.
- **Weekly legionella cycle as a tank-floor constraint** (#317). LP plans the cheapest pre-heat schedule for the configured `DHW_LEGIONELLA_DAY/HOUR` rather than the firmware ambushing the LP with sudden DHW draw.
- **Static-physics DHW draw model** (#299). Shower-window slots now subtract realistic hot-water draw from the tank energy balance — prior LP only saw standing loss (~0.5 °C/h) and missed the much bigger drop from someone actually showering.
- **Per-installation Daikin LWT→kW calibration** (#316 + #318 + #319 + #320). Replaces the hardcoded `_KW_PER_DEGC_LWT` constant with a value fitted to this installation's recent telemetry. Recalibrates inline at LP solve.

### Added — testing / observability
- **76 → 1102 tests, +200 in v12.** New coverage: ``test_lp_shower_floor_soft``, ``test_lp_drop_mode_mutex``, ``test_lp_infeasible_appliance_retry``, ``test_lp_infeasible_snapshot_replay``, ``test_lp_hp_min_on``, ``test_lp_appliance_real_solver``, ``test_solve_lp_boundary_matrix``, ``test_soc_below_reserve_feasibility``, ``test_heuristic_fallback`` extensions.
- **Regression-gate `--vs-ref=<ref>` mode** (#334). Compares the current branch against any past ref's LP solve quality on the same prod snapshots — a clean per-PR delta instead of drifting against a stale frozen baseline.
- **T+14 post-deploy comparison script** (#335).
- **SoC-boundary matrix + heuristic Fox V3 safety invariant** (#340). 11-case sweep of `initial.soc_kwh` from 0 → `soc_max` proves no infeasibility region; xfail test enforces "heuristic must never ship `ForceCharge[fdPwr=3000, fdSoc=95]` defaults".
- **Real-pipeline appliance integration test** (#346). Stubs only `solve_lp`; exercises real `appliance_dispatch.reconcile()` + `appliance_load_profile_kw` + base_load arithmetic. Catches regressions the mocked-solver tests would miss.

### Changed — LP modeling + performance
- **Dropped DHW/space mode mutex** (#343). `m_dhw + m_space ≤ 1` and the two binaries it enforced are gone. The Daikin Altherma firmware interleaves DHW and space heating inside a 30-min slot; the mutex misrepresented the hardware. Aggregate cap `e_dhw + e_space ≤ max_hp_kwh × hp_on` preserved; climate-curve physics ceiling on `e_space` applied directly. Removes 192 binaries per 96-slot solve.
- **`LP_HP_MIN_ON_SLOTS` default 2 → 1** (#345). Daikin firmware already enforces compressor short-cycle protection; the LP's per-startup binary was redundant. Removes another ~96 binaries per solve. Set explicitly in `.env` to restore.
- **Hold previous schedule on LP Infeasible** (#338). Replaces the destructive heuristic fallback that emitted `ForceCharge[fdPwr=3000, fdSoc=95]` defaults — measured at +£0.35-1.30/day vs LP objective on the days a fallback fired. Fox V3 is daily-cyclic; the last successful plan stays in effect.
- **`soc[0].lowBound` relaxed below reserve** (#339). The hard equality `soc[0] == initial.soc_kwh` could not be satisfied when realtime SoC dipped below `MIN_SOC_RESERVE_PERCENT × BATTERY_CAPACITY_KWH`. Slot 0 now allows `[0, soc_max]`; subsequent slots keep the reserve floor.
- **`MIN_SOC_RESERVE_PERCENT` 15 → 10** (deployed config). Matches Fox `minSocOnGrid` hardware floor.
- **`t_in[i]` indoor-temp variable dropped from LP** (#310 — Phase B1). The Daikin Altherma has no room sensor; the comfort-band model was fiction. Space-heating demand now driven by `get_daikin_heating_kw(t_outdoor)` directly.
- **PV-abundance threshold relaxed** (#293). Dropped `max_batt_kwh` term so abundance triggers on realistic sunny days instead of peak-summer-noon territory only.
- **PV-abundance tank ceiling lift** (#287 + #292). Separate caps for negative-price (65 °C) vs PV-abundance (55 °C, runtime-tunable via `DHW_TEMP_PV_ABUNDANCE_TARGET_C`).
- **Daikin write-budget guard + Sunday legionella skip** (#289). Coalesces low-value pairs + drops trailing pre-heat actions when the 200/day Daikin Onecta quota is tight; never coalesces / drops `max_heat` (negative-price) or `shutdown` (peak).

### Changed — dispatch hygiene
- **Climate fields stripped on every Daikin write** (#321). The 2026-05-11 climate-strip incident — dispatch was sometimes writing `climate_on` + `lwt_offset` even when the LP didn't plan space heating.
- **DHW peak strategy = idle (default)** (#296). Tank stays at `NORMAL_C` (45) during peak instead of shutting off; eliminates the firmware-fight cost spike when peak ends.
- **Post-shower overnight tank idle** (#298). LP-driven low-target idling after the evening shower window — the tank stays at backup target until the next productive (solar/negative) window.
- **Overnight idle resets only on PV / negative** (#302). Cheap-grid battery-charge slots no longer count as "time to start tank heating".
- **Per-day DHW draw normalisation** (#304). Daily shower litres divided by *that day's* shower-slot count, not the horizon-wide total — fixes per-day under-modelling on 48 h horizons.
- **Daikin Onecta `stepValue=1` quantisation** (#318). `tank_temp` + `lwt_offset` rounded to int before write.

### Changed — forecast + brief
- **Forecast night temperature bias** (#329). New `FORECAST_NIGHT_TEMP_BIAS_C` subtracts from Open-Meteo overnight forecast (W4 1DZ microclimate runs colder than the ~10 km grid forecast).
- **Runtime-tunable PV abundance target + night temp bias** (#329). Both can be re-tuned without redeploying via `PUT /api/v1/settings`.
- **Phase A data-quality additions** (#308). Brief surfaces a Fox-vs-meter audit line; heartbeat no longer pings the Daikin API (saves ~10 % of the daily quota); backfill deduplicates re-fetches.
- **Daikin budget-guard drops in morning brief** (#315).

### Fixed
- **Three bugs in Daikin LWT→kW calibration** (#319) that made the previous calibration a silent no-op.
- **Yesterday warm-start removed from PV today-factor** (#333). Unobserved hours now use `tf=1.0` instead of yesterday's median ratio — yesterday's bias was leaking into today's calibration when no live observations existed yet.
- **Real-money PnL from measured grid import** (#307). Replaces the LP's planned-import number with the metered actual; brief £ figures now match what Octopus billed.
- **DHW peak-strategy leak** (#321) — `DHW_TEMP_PEAK_C` setting was being mis-applied.
- **`compute_today_pv_correction_factor` undefined variables** in cloud-aware recompute (#252 — already fixed pre-v12 but documented for completeness).
- **`OPENCLAW_READ_ONLY` blocks direct REST hardware writes** (#251 — pre-v12 but reaffirmed).

### Removed
- **`EXPORT_DISCHARGE_MIN_SOC_PERCENT` live-SoC global gate** (per v11.0.0; cleanup completed in v12). Replaced by scenario-LP filter (`src/scheduler/lp_dispatch.py:filter_robust_peak_export`).
- **Mode-binary variables `m_dhw[i]` + `m_space[i]`** (#343).
- **`LP_HP_MIN_ON_SLOTS=2` constraint block** when default = 1 (#345 — guard skips when `min_on ≤ 1`).
- **OperationMode `simulation` / `operational` distinction** (2026-04-23, pre-v12). `OPENCLAW_READ_ONLY` is the only kill switch.

### Migration notes (v11.0.0 → v12.0.0)
- **Default config changes**: ``LP_HP_MIN_ON_SLOTS`` 2 → 1. Set explicitly in your `.env` to keep the v11 behaviour.
- **New env knob**: ``LP_SHOWER_LO_PENALTY_PENCE_PER_DEGC_SLOT`` (default 50.0). Lowering risks weaker shower comfort guarantees; raising can re-introduce Infeasibles.
- **DB schema**: ``lp_inputs_snapshot.lp_status`` column added (nullable, NULL = "Optimal" for legacy rows). Applied via the auto-migration block in `src/db.py`.
- **No prod credential changes**. No new MCP tools. No Daikin / Fox API surface changes.

---

## [11.0.0] — 2026-05-06

V11 stack landed in one session (14 PRs):
- Quartz live (`open.quartz.solar` adapter).
- Per-hour microclimate offset.
- Regression baseline locked.
- Scenario LP for peak-export robustness.

Estimated improvement: ~£245–270/yr vs the prior British Gas fixed tariff.

See git log between v10.3.0 and v11.0.0 for the full per-commit detail.

## [10.3.0] — 2026-04-29

- Docker immutable cutover (HEM live as `hem.service` from a single GHCR image).
- OpenClaw bind setup post-Docker; pinned bridge + extra_hosts.
- HTTP MCP transport replacing the per-call stdio subprocess.

## [10.0.x] — 2026-04-22 → 2026-04-25

- 75-tool MCP surface stabilised.
- PnL semantics fixed (real-money fields from measured import).
- V13 nightly post-hoc consumption backfill.
- T+14 audit script for post-deploy comparison.

## [9.x] — 2026-03 → 2026-04

V9 LP redesign:
- Simplified HP model: 1 binary `hp_on[i]` + continuous `e_hp[i]`.
- Piecewise-linear inverter stress cost.
- Terminal SoC constraint.
- HP minimum on-time (since reverted in v12.0.0 — see Changed).
- TV penalties + price quantisation.

## [1.x] — pre-2026-03

Initial heuristic dispatcher + the early Octopus + Fox + Daikin glue. The LP didn't exist yet; dispatch was rule-based on tariff tier. Kept for git-history continuity, not as a usable runtime.

---

[Unreleased]: https://github.com/albinati/home-energy-manager/compare/v13.0.0...main
[13.0.0]: https://github.com/albinati/home-energy-manager/compare/v12.0.0...v13.0.0
[12.0.0]: https://github.com/albinati/home-energy-manager/compare/v11.0.0...v12.0.0
[11.0.0]: https://github.com/albinati/home-energy-manager/compare/v10.3.0...v11.0.0
[10.3.0]: https://github.com/albinati/home-energy-manager/compare/v10.0.1...v10.3.0
[10.0.1]: https://github.com/albinati/home-energy-manager/compare/v10.0.0...v10.0.1
[10.0.0]: https://github.com/albinati/home-energy-manager/compare/v9.1.0...v10.0.0
