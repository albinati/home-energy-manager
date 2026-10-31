# Daikin control notes (cadence, holiday, vacation, legionella, tank model, user override)

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

### Daikin cadence — bounded heartbeat refresh + post-write verification (#809)

Phase A (#306) took the heartbeat off the Daikin API, so the reconciler and
the user-override detector compared against a cache up to 30+ min old. Now:

- **Heartbeat refresh** (`DAIKIN_HEARTBEAT_REFRESH_ENABLED`, code default
  false, **prod true**): at most one read per `DAIKIN_HEARTBEAT_REFRESH_SECONDS`
  (1800 → ≤ 48/day), only while `quota_remaining > DAIKIN_RESERVE_FOR_HEARTBEAT
  + DAIKIN_HEARTBEAT_REFRESH_MIN_HEADROOM` (30 + 40), still under the service's
  90 s floor and `should_block`. Gate: `runner._heartbeat_daikin_refresh_allowed`.
- **Post-write verify** (`DAIKIN_POST_WRITE_VERIFY_ENABLED`, default true): a
  successful `apply_scheduled_daikin_params` schedules ONE read (job id
  `daikin_verify_pending`; a second write before it fires MERGES its keys and
  pushes the fire time out) at `max(DAIKIN_POST_WRITE_VERIFY_SECONDS,
  DAIKIN_REFRESH_MIN_INTERVAL_SECONDS + 30)` — above the service's anti-burst
  floor, so the read is a real one. It compares ONLY the keys actually PATCHed
  (`written`), never the whole params dict (an `lwt_offset` skipped because the
  zone is off is not "unverified"). A read the service throttled/served from
  cache is logged `unverified`, never `success` (`source=cache_throttled`). On a
  mismatch it retries once (+180 s, Onecta propagation lag) and only then raises
  one `notify_risk` per write (deduped via `db.acknowledge_warning`). `action_log`
  `daikin_write_verify` carries `expected` / `actual` / `matched` / `fresh` /
  `cache_source` / `attempt`. The read also refreshes the cache for the next
  heartbeat. During the active-mode soak budget (100/day) the heartbeat refresh
  self-throttles off; verification reads are not headroom-gated (~20/day).
- Budget arithmetic (180/day rolling): heartbeat ≈ 48 + verify ≈ 10 + rollups 4
  + LP-init ≈ 2 + viewer boost ≤ 10 + writes ≈ 20 ≈ 95–100, leaving ~80 for
  429 retries and manual MCP use. Watch `GET /api/v1/daikin/quota`.


### Daikin holiday mode (#872)

The Onecta app can put the Altherma in holiday mode (climate AND DHW tank `onOffMode=off`,
`leavingWaterOffset` read-only, writes 400). HEM detects it from the **cached** `/gateway-devices`
payload (zero extra quota, lag up to `DAIKIN_HEARTBEAT_REFRESH_SECONDS`).

- **Signal = `isHolidayModeActive` per management point only** (`DaikinDevice.holiday_active` =
  climateControl, `tank_holiday_active` = domesticHotWaterTank; `None` when the key is absent).
  `holidayMode` (`holiday_window`) is informational: it did NOT reflect an accepted write. One reader:
  `daikin.service.holiday_state()` (`{climate, tank, window, cache_age_seconds}`, cache only) and
  `daikin_holiday_active("climate"|"tank")`.
- **Kill switch** `DAIKIN_HOLIDAY_DETECT_ENABLED` (runtime, default true): off = every flag reads
  `None`, no gate fires (bit-for-bit pre-#872).
- **Gates** (each point's own flag): `service._passive_guard` (`DaikinError("[holiday] ...")`),
  `apply_scheduled_daikin_params` (row logged `holiday_skip`, `error_msg="holiday_mode"`, only when
  the params touch a flagged point), the reconciler (rows stay PENDING inside their window, one
  `holiday_mode_skip` per held row; a row whose window ends during holiday completes silently with
  `error_msg="holiday_mode"`, no "window missed" warning; tank drift / divergence / warmup-settle / neg-boost / shower-drawdown / morning-cold
  checks and the LWT drift check are skipped per point), `write_daikin_from_lp_plan` (both points
  off: clear leftovers + write nothing; one point off: rows are written and the reconciler holds that
  point's rows), cold/warm backstops + `emergency_tick` (counters HOLD), `detect_user_override`
  (holiday-induced off is never an override), the actuation watchdog (tank age/failures and LWT
  failures suppressed per point).
- **LP `off` state** (`plan.daikin_state` = `active|passive|off`, also in `lp_inputs_snapshot`
  config snapshot): read ONCE per solve (`LpInitialState.holiday_climate/holiday_tank`, None = ask the
  service; replays pin False). Both points: reuses the passive machinery with an all-ZERO draw
  vector (NOT the firmware-curve prediction): `e_space = e_dhw = 0`, `hp_on = 0`, no W3 variables
  (so no comfort/ceiling/emergency slack), no shower floors, no DHW block. One point: only that
  draw is pinned to 0 (W3 off when the climate zone is off).
- **Transitions** (`runner._holiday_transition_tick`, right after the cached refresh): `kv_state`
  `daikin_holiday_active` ({climate, tank}, compared PER POINT); on change `action_log` `holiday_mode`
  `{event: entered|exited, climate, tank, changed, window}` + one notification per event kind. On EXIT of
  ANY point: with control active and not read-only, that point's cached `onOffMode` still off ->
  `set_power(True)` / `set_tank_power(True)` (`holiday_exit_power_on`), then ONE
  `bulletproof_mpc_job(force_write_devices=True, bypass_cooldown=True, trigger_reason="holiday_end")`
  (always; dispatches BOTH devices - Fox upload + Daikin rows - so the battery is prepared for the
  recovery). Passive/read-only: no power-on, and the notification says HEM is only observing. If the
  signal vanishes while a holiday is stored (kill switch off, payload without the keys) an `exited`
  event is written (`reason`) and the state cleared, so the scorecard flag never sticks.
- **Telemetry integral while OFF (#880)**: `daikin_telemetry` rows now carry nullable `is_on` /
  `tank_on` / `holiday_active` / `tank_holiday_active` (written by `_persist_daikin_telemetry_live`
  with an `isinstance(bool)` guard; `estimate` rows inherit them from the live seed).
  `compute_daikin_2hourly_telemetry` zeroes the space draw when both ends of an interval are known
  climate-off (holiday or `is_on=0`) and the DHW draw when known tank-off; an on->off boundary
  interval (one end known-on, the other known-off) integrates HALF the draw (residual: the switch-off
  instant inside it is unknown); NULL<->off keeps the model. A bucket whose every interval was fully
  off is written 0 kWh with `source='telemetry_integral_off'` (measured off, not "no data"; slot
  dicts gained an `off` key). Same overwrite rules as `telemetry_integral`; `onecta_cache` stays
  authoritative, and a metered 0 against an off bucket is kept (`onecta_agrees_off`). NULL
  (historical) rows keep the weather-curve model bit-for-bit. The integral acts on the PERSISTED flags
  regardless of `DAIKIN_HOLIDAY_DETECT_ENABLED` (a measurement of on/off state, not a control gate).
  **Learners:** holiday / unit-off days are excluded from live tau (`select_decay_episodes`), UA-coast
  and pump-off-delta fits (`lwt_learning.exclude_unit_off`: `telemetry_integral_off` buckets plus local
  days covered by a `holiday_mode` entered..exited window, helper `thermal_learning.holiday_days`);
  unoccupied nights have no internal gains and would bias tau short. Analyse them offline.
- **Surfaces**: `space_heating_gate_state()["daikin_holiday"]`, `/api/v1/daikin/status` `holiday`,
  MCP status `holiday`, brief `Mode:` says `holiday`; the Cosy scorecard payload carries `holiday`
  (from the `holiday_mode` events) and skips the tank-floor `notify_risk` on those days.
- **Write path (used by #873, see "Vacation mode v2")**: `POST /v1/gateway-devices/{id}/management-points/{climateMpId}/holiday-mode`,
  body `{"enabled": true, "startDate", "endDate"}` UNWRAPPED (no `value`) -> 204; disable is
  `{"enabled": false}` with NO dates. PUT -> CloudFront 504; PATCH on the characteristic -> 422.
  Code: `DaikinClient.set_holiday_mode` (a `_post`, same retry/quota pattern as `_patch`) and
  `daikin.service.set_holiday_mode` (passive guard applies; the HOLIDAY gate deliberately does NOT,
  so a holiday can always be turned off).


### Vacation mode v2 (#873)

`OPTIMIZATION_PRESET=vacation` is "away until <return>": one managed action, `src/vacation.py`.

- **Mandatory return time.** Runtime `VACATION_RETURN_AT` (local ISO datetime in
  `BULLETPROOF_TIMEZONE`, e.g. `2026-11-07T18:00`). Setting the preset to vacation (aliases
  travel/away included) without a FUTURE return time -> **422** from `PUT /api/v1/settings/{key}`
  (optional `return_at` in the body), `POST /api/v1/settings/batch` (+ both `/simulate` routes; the
  handler applies `VACATION_RETURN_AT` before the preset whatever the dict order), the legacy
  `POST /api/v1/optimization/preset` (`return_at` field) and a clear `{ok:false,error}` from the MCP
  `set_optimization_preset(preset, return_at)` / `set_setting`. The check lives in the CALLER layer
  (`vacation.check_preset_changes`), not in `rts.set_setting`, so internal writers (the recovery)
  never trip it. Side effects after the write: `vacation.after_settings_applied` (background thread).
- **Activation = `vacation_start`. ORDER MATTERS:** (1) `kv_state` `vacation_end_at` (= recovery fire
  time, ISO UTC) + `vacation_state` (JSON: return_at, recovery_at, start/end date, `holiday_post`,
  `holiday_until_sent`) and the `vacation_end` job armed; (2) POST the Onecta holiday window; (3)
  `bulletproof_mpc_job(force_write_devices=True, bypass_cooldown=True, trigger_reason="vacation_start")`;
  (4) ONE Telegram ("Fora até X; Daikin em holiday até Y; recuperação agendada Z"). A failed POST
  -> `notify_risk` ("Holiday não ativou na Daikin; aquecimento continua") but the preset applies and
  the recovery stays scheduled. `OPENCLAW_READ_ONLY` skips the POST (and the recovery's Daikin writes).
- **Firmware fallback window.** `startDate` = today (local), `endDate` (INCLUSIVE; firmware resumes
  at 00:00 the next day) = the recovery job's LOCAL day - 1 (`vacation.holiday_dates`): the firmware
  resumes at most 24 h BEFORE the HEM job, never after (an early resume costs a few hours of curve
  heating, a late one a cold arrival). If that is before today -> `endDate = startDate = today`
  (warned). Local day, so BST/GMT changes are right (23:30 UTC on 24 Oct 2026 is 25 Oct local).
- **Recovery time** = `VACATION_RETURN_AT - VACATION_RECOVERY_HOURS` (runtime, default 12, 0..72),
  snapped back to the START of a CHEAP band on a banded tariff
  (`analytics.load_expected.band_windows_for_day`, key `band_cheap`) - but ONLY when the target is
  inside that band or within 3 h after its start (a longer snap would heat through the day/peak
  bands), and never to a time before `now + 60 s`. Dynamic / unknown tariff: no snap. A time already
  inside `now + 60 s` is clamped to `now + 60 s` (`recovery_immediate`: the holiday-ON POST is then
  SKIPPED, it would only race the OFF); on boot a past time fires ~30 s later.
- **Recovery = `vacation_end`** (`run_recovery`, DateTrigger job id `vacation_end`, re-armed from
  `kv_state` by `start_background_scheduler` via `vacation.rearm_from_kv` - ALWAYS, also with the kill
  switch off; idempotent, single-flight; every step logged in `action_log` `vacation_end`, never
  blocking the next). It first WAITS for an in-flight activation (`_activation_lock`, held from
  `begin_activation` to `finish_activation`) so holiday-OFF can never overtake holiday-ON. Sequence:
  (1) preset back to normal (or the current guests/normal choice) + `VACATION_RETURN_AT` cleared -
  FIRST, so a concurrent #872 heartbeat `holiday_end` solve already plans a normal house; (2) holiday OFF
  (always allowed, also with the kill switch off); (3) force device read, polled up to 3 x 30 s until the
  holiday flags clear (Onecta lags; else the #872 gate rejects the power-on and the MPC plans
  `daikin_state=off`; attempts in `steps.holiday_poll`); (4) `set_power(True)` / `set_tank_power(True)`
  ONLY for a point whose cached `onOffMode` is off; (5) read back (only when something was written);
  (6) `bulletproof_mpc_job(force_write_devices=True, bypass_cooldown=True, trigger_reason="vacation_end")`
  - the job RETURNS False (no raise) when the dispatch lock is busy / scheduler paused / backend not lp,
  so it is retried up to 5 x 20 s and a final False is recorded as `failed` ("houve falhas" in the
  Telegram; same retry for `vacation_start`); (7) clear `kv_state`; (8) one Telegram with the plan's
  predicted indoor at the return time (when it has one) and the tank C. The #872 heartbeat exit tick may
  still fire its own `holiday_end` replan; harmless (preset already normal).
- **Cancel early.** Leaving vacation (preset -> normal) while `vacation_end_at` is stored runs the SAME
  recovery immediately (`trigger=cancel`) and clears `kv_state`.
- **While away.** Daikin is passive-like through the #872 detection (`DAIKIN_CONTROL_MODE` is NOT
  touched; LP `daikin_state=off`, no tank rows). Battery = NORMAL LP economics: the vacation-only
  `chg <= pv_use` / `exp` rule applies ONLY when `LP_BATTERY_EXPORT_ENABLED=true` (historical Agile
  arbitrage semantics, bit-for-bit); with it false (prod) vacation grid-charges in cheap bands, with
  the pessimistic floor and normal Fox shapes (`optimizer._vacation_pv_only_battery()` gates the
  `solar_charge` label, the SelfUse(reserve) shape and the A1-hold exclusion). Residual load x
  `LP_VACATION_LOAD_SCALE` (runtime, default 0.35, 0.1..1.0: an empty house measured 0.3-0.45 kW vs
  ~1 kW occupied; `optimizer._vacation_base_load_scale()`, mirrored in `lp_simulation`; exactly 1.0
  outside vacation). Peak-import penalty unchanged.
- **Kill switch** `VACATION_DAIKIN_HOLIDAY_ENABLED` (runtime, default true): false -> NEW vacations write no
  Onecta holiday, no `kv_state`, no recovery job (the bare pre-#873 preset; the return-time requirement
  still applies). It does NOT strand a stored trip: a stored recovery is still re-armed at boot and
  still resets the preset / turns holiday off.
- **Requires `DAIKIN_CONTROL_MODE=active`** (and not `OPENCLAW_READ_ONLY`): in passive every Daikin step
  fails by design (the service guard) and the recovery reports "houve falhas"; read-only skips them.
- **Threading.** `begin_activation` (validate, persist `kv_state`, arm the job) is SYNCHRONOUS in the
  request; only `finish_activation` (POST + replan + Telegram) and the cancel-early recovery run in a
  daemon thread (`vacation.BACKGROUND`, tests flip it). A failed `begin_activation` (e.g. a DB error)
  returns `vacation: {"status": "error"}` + `notify_risk`; no job armed (`inactive`/`error`) also raises
  a `notify_risk`. Every entry point clears a pinned in-memory `config` override of the preset /
  return-time keys (`vacation.persist_setting`, legacy normal/guests now persist too) and
  `DELETE /settings/OPTIMIZATION_PRESET` runs the same cancel hook (`DELETE VACATION_RETURN_AT` during
  vacation is a 422). `VACATION_RETURN_AT` accepts a trailing `Z`/offset.
- **Load scale cutoff.** `LP_VACATION_LOAD_SCALE` applies only to slots BEFORE the stored recovery time
  (`vacation.recovery_cutoff()`), so the plan solved the evening before provisions the arrival evening.
- **Guests demoted.** `GUESTS_DETECT_ENABLED` code default is now false. The Home ModeBox shows
  Normal | Férias (datetime-local return); `guests` stays in the enum, reachable only in
  Settings > Avançado > "Férias e Daikin".
- **Surfaces.** `GET /api/v1/cockpit/now` -> `vacation` and `space_heating_gate_state()["vacation"]`:
  `{active, return_at, recovery_at, daikin_holiday_until_sent, holiday_enabled}` (`daikin_holiday_until_sent`
  = the `endDate` WE sent; the characteristic itself is not trustworthy, see #872). Cosy scorecard payload
  `vacation` (from `vacation_start`/`vacation_end` events) suppresses the tank-floor `notify_risk`; the brief
  `Mode:` chip shows the return time.


### Legionella thermal-shock cycle

Daikin Onecta firmware runs the weekly thermal-shock cycle autonomously (default Sunday 11:00 UTC; user-reconfigurable on the unit). **HEM does not schedule this cycle** — the old `DHW_LEGIONELLA_*` *scheduling* vars are gone. Python ignores unrecognised keys in `.env` so lingering entries are harmless; delete them on your next `.env` touch.

**Tank stand-off (2026-06-07).** Because the firmware OWNS the DHW tank during the cycle, any tank PATCH HEM sends in that window is arbitrated/overridden (wasted Daikin quota + churn + `READ_ONLY`). So the reconciler now **skips tank-device writes inside a configured stand-off window and leaves those rows pending** so they resume the moment the window closes (the firmware leaves the tank hot; HEM's next warmup/setback then brings it back to plan). **TANK ONLY — LWT / space-heating rows still fire** (legionella is a DHW-tank cycle). The LP budgets the cycle's heat-up energy via `DHW_LEGIONELLA_BUDGET_KWH` (default 3.5 kWh electric, spread across the stand-off window in `forecast_dhw_load_per_slot`) — added in #643 (2026-07-05) after a Sunday audit showed the post-K1 forecast carried NO legionella term (~0.5 budgeted vs ~3-3.5 kWh drawn; the battery discharged into the un-budgeted cycle and hit the SoC floor mid-heat-up). Telemetry: `legionella_tank_standoff` events in `action_log`. New `.env` knobs (defaults = Sunday 11:00 UTC, 120 min — covers the ramp from the overnight setback up to ~60 °C plus the firmware's ~1 h hold):

```
DHW_LEGIONELLA_STANDOFF_ENABLED=true            # master switch (the ONLY tank-write block during the cycle)
DHW_LEGIONELLA_STANDOFF_DOW=6                    # weekday, Mon=0 .. Sun=6 (datetime.weekday())
DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC=11        # window start (UTC); must not cross midnight
DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC=0
DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES=120     # ramp + ~1 h hold; tune from `legionella_tank_standoff` telemetry
```

**Local-time window (preferred).** The unit runs the cycle on the LOCAL clock (Europe/London): past Sundays set to "11:00" started heating ~10:00 UTC in BST, so a fixed UTC window is wrong by 1 h for half the year. `DHW_LEGIONELLA_STANDOFF_START_LOCAL=HH:MM` (default empty) starts the window at that wall-clock time in `BULLETPROOF_TIMEZONE` on the LOCAL weekday `..._DOW`, lasting `..._DURATION_MINUTES` (must not cross local midnight). Recommended prod: `START_LOCAL=12:30` + `DURATION_MINUTES=150` (cycle at ~12:30-12:45 local, start of the Cosy 13-16 cheap band); then drop any oversized UTC bridge (e.g. 11:30 UTC + 240 min) — it also smeared the LP's `DHW_LEGIONELLA_BUDGET_KWH` over too many slots. Empty = the UTC knobs above, bit-for-bit (kill switch); an invalid value logs one warning and falls back to them. ONE helper, `src/legionella_window.py` (`standoff_window_for_date`, `standoff_windows`, `in_standoff_window`), serves the reconciler guard, the LP budget (`forecast_dhw_load_per_slot`, `legionella_budget_by_slot`), the bucket-bias exclusion (`dhw_bias`) and the schedule views.

Helper: `src/state_machine.py:in_legionella_standoff(now_utc)` (thin wrapper over `src/legionella_window.py`). The guard sits in `_reconcile_daikin_actions` before the pending→active transition. If a `shutdown`/`max_heat` action overlaps the cycle window outside the guard, Onecta firmware still arbitrates.


### Tank model — the ambient is the HOUSE (#819, 2026-10-07)

Owner: "agora demora mais pra esquentar e é mais rápido pra esfriar". Measured
overnight coast (live `daikin_telemetry`, 23–07 local, Oct 2026): **0.34–0.37
°C/h** at ~45 °C vs 0.24 in the July heatwave. The joint UA+ambient fit
(`dhw/calibration.fit_ua_and_ambient`) is unidentifiable on real data (constant
ambient → −11 °C, linear-in-outdoor → slope 2.4; both rejected, #772) so the
tank ran on the databook pair (2.44 W/K, 22.4 °C effective) — which predicts
0.25 °C/h in every season.

- **`fit_ua_indoor_ambient`**: UA alone, each coast episode's ambient FIXED to
  the measured house indoor (`room_temperature_history`, ≥ 3 readings in the
  episode). Prod copy: **UA 3.2–3.3 W/K, τ ≈ 69 h, R² 0.70, 18–24 episodes**,
  `ambient_model="indoor_measured"`. Preferred by `refresh_dhw_calibration`;
  the joint fit (with its linear rescue) is the fallback and is stored under
  `alternative` / `indoor_fit` for audit.
- **`resolve_tank_params(ambient_c=live)`** honours a live indoor reading ONLY
  for an indoor-fitted UA (`source="measured_indoor"`, UA cap 6 W/K); the
  joint fit keeps its effective ambient and the databook ignores it — pairing
  a UA with an ambient it was not fitted against just moves the error.
  `live_indoor_ambient_c()` is the one reader (stale > `INDOOR_SENSOR_STALE_MINUTES`
  → None → the fit's mean indoor). Consumers: LP block ambient per slot
  (`lp_optimizer` seeds from `initial.indoor_c`), dynamic window
  (`resolve_window_decision_local`), boost-lift budget, deadband warmup force
  (`state_machine._warmup_deadband_force_reason`), shadow baseline.
- **Coast check telemetry**: nightly `dhw_calibration` component `coast_check`
  (+ `action_log` `tank_coast_check`): last episode's measured vs model °C/h at
  the episode's indoor, and `ratio_median_recent` over the last 7 episodes
  (one night is ±30 % with 1 °C quantisation). The model side is the params
  that were STEERING before tonight's refit (read before the upsert), so it is
  an out-of-sample check, not the fit's own residual. Surfaced in
  `/api/v1/status/feedback` → `dhw.tank_model`.
- Live-ambient semantics: `resolve_tank_params(ambient_c=live)` CLAMPS the
  live reading to the fitted indoor range ±3 °C (within 8–32) — never rejects
  it (a 30.5 °C summer house or a 9.5 °C heating-fault house must not flip
  the planner back to the databook pair). The indoor join in the fit is an
  equal-weight mean across rooms of each room's episode mean (same definition
  as `get_latest_indoor_reading`), needing ≥ 3 distinct timestamps spanning
  ≥ 50 % of the episode.
- NOT done here (decision 2026-10-07): `DHW_LP_OWNED_ENABLED` stays off — the
  shadow gate read median **−6.4 p/day** (LP-owned dearer) over 20 days with
  the mis-specified coast on both arms. Re-read `evaluate_gate()` after ≥ 7
  days on the indoor-fitted tank.


### User-override propagation (Epic 14, #386 — 2026-05-21)

When the user manually changes tank state (Onecta app / physical button),
the active `action_schedule` row gets `overridden_by_user_at` set by the
reactive detector in `src/daikin_bulletproof.py`. The pre-fire reconciler
in `src/state_machine.py` now does two things:

1. **Idempotency** — before firing a pending row, compare live device
   state against the row's params. If they match, mark the row `completed`
   with `error_msg='noop (state matched pre-fire)'` and skip the API call.
   This is what drops the `READ_ONLY_CHARACTERISTIC` 400s and the
   redundant solar_preheat writes that overlapping replan rows produced.
2. **Override inheritance** — for non-`restore` rows, look up the most
   recent `overridden_by_user_at` row for the same device within
   `USER_OVERRIDE_RESPECT_HOURS` (default 4h) — OR, when
   `USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END` (default true, 2026-06-07),
   while the overridden row's own `end_time` is still in the future. The
   latter keeps a hand-set tank/LWT respected for the WHOLE planned window
   (e.g. a multi-hour negative-price boost), not just the fixed grace. If
   the user's gesture is still in effect (live state still contradicts the
   override row), mark the new row overridden too and skip. The live
   `user_gesture_still_in_effect` check is the safety gate — revert the
   manual change and HEM resumes at once, so a long window can't wedge the
   schedule. One notification fires per *source* user gesture, not per
   suppressed downstream row. Restore rows are exempt so the system can
   always return to baseline.

Both behaviours are gated by `PREFIRE_STATE_MATCH_ENABLED` (default true).
Set to `false` for instant rollback. Telemetry shows up in `action_log` as
`prefire_state_match` and `prefire_override_inherited` events.

---

