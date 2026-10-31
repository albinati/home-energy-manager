# Comfort feedback, scorecard, tuning review, auto-tune

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

### Comfort feedback loop (#833)

The owner's "how does the house feel?" closes the loop. Three inputs, one table
(`comfort_feedback`: verdict cold|ok|hot, optional room/note, plus the context at
that instant — aggregate + per-room indoor, outdoor, active LWT offset, price
band, `DAIKIN_LWT_SOURCE`):

- **API**: `POST /api/v1/comfort/feedback` (admin; `{verdict, room?, note?}`),
  `GET /api/v1/comfort/feedback?days=30` (viewer; rows + per-room counts + weekly summary).
- **Home**: admin-only Cold / OK / Hot buttons (+ room select) in the Heating card.
- **OpenClaw / MCP (this household)**: tools `record_comfort_feedback(verdict, room?,
  note?)` (verdict cold|ok|hot|frio|quente; room matched against fresh sensor rooms;
  stored with `source="openclaw"`) and `get_comfort_feedback(days)`.
- **Telegram poller (OFF by default, `TELEGRAM_INBOUND_ENABLED=false`)**. Precondition:
  the bot token must be EXCLUSIVE to HEM — Telegram allows one `getUpdates` consumer
  and no webhook; here OpenClaw owns the bot, so the poller stays off (code kept for a
  future dedicated bot). When on: short-poll every `TELEGRAM_INBOUND_POLL_SECONDS`
  (own job `telegram_inbound_poll`, offset in `kv_state`), PRIVATE chat with
  `TELEGRAM_CHAT_ID` only (optional `TELEGRAM_OWNER_USER_ID` also checks `from.id`),
  stale messages (older than max(300 s, 2x poll)) are ignored, 3 consecutive failures
  -> one alert + 15 min back-off (429 honours `retry_after`). Commands:
  `/conforto frio|ok|quente [cômodo] [nota]` / `/comfort cold|ok|hot [room] [note]`;
  `/conforto` alone = usage + rooms; other slash commands get no reply.
- **Weekly** (`src/analytics/comfort_feedback.py`, Sunday 08:45 local): summary to
  `action_log` (device `comfort`, action `weekly_summary`). Proposal rules: >=2 cold
  at night at/below floor+0.3 -> `LP_W3_NIGHT_FLOOR_C` +0.5 (cap 22); >=2 cold in
  the peak band -> `LP_W3_PEAK_COAST_DELTA_C` -0.5 (min 0); >=3 hot and no cold ->
  the inverse. `external_comfort_signal(week_start)` exposes it to the suggestions
  story. **Nothing is applied** unless `COMFORT_FEEDBACK_AUTO_TUNE=true` (default
  false); then only that one bounded +-0.5 change per week via
  `runtime_settings.set_setting` (action_log `auto_tune`).

### Cosy daily scorecard (#831)

`src/analytics/cosy_scorecard.py` scores YESTERDAY (local day) once a day:
job `cosy_scorecard` at `COSY_SCORECARD_HOUR_LOCAL:MINUTE` (07:30
`BULLETPROOF_TIMEZONE`, before the 08:00 brief; a one-shot 120 s after boot
back-fills any missing day of the last 7) persists one row in
`cosy_scorecard_daily` (indexed: score, peak/import kWh, import £, avg vs ideal
p, net £; everything else in `payload_json`). Read-only — it NEVER changes a
setting. Payload sections, each guarded on its own: `spend` (the SAME
`plan_fronts.spend_section` the Home uses, so the score agrees), `bands` (per
tariff window: import kWh/£, load, PV, battery discharge, forecast-vs-actual load
error + `under_forecast`), `battery` (planned vs realised SoC at 07:00 / 16:00 /
00:00, pessimistic-floor binding slots, cycles), `comfort` (per room min/max/mean,
hours below the night floor and below the peak-coast floor, using
`INDOOR_COMFORT_AGGREGATE`), `tank` (°C at each shower-window entry vs floor,
window decision, `coast_check` ratio), `lwt` (preheat/restore rows, `daikin_write_verify`
success/unverified/mismatch, last `lwt_source_diff`), `ops` (Daikin calls, Fox
failures), `money` (net £, delta vs fixed, rolling-7 mean). Surfaces:
`GET /api/v1/scorecard/cosy?days=14` (viewer, clamp 1..90, newest first), the
Insights "Cosy scorecard" card, ONE line in the morning brief. One `notify_risk`
per condition per date (dedupe via `acknowledge_warning`, keys `cosy_*_<date>`):
3 consecutive days of peak-band load under-forecast, tank below a shower floor at
entry, any write-verify mismatch, Daikin calls > `COSY_SCORECARD_QUOTA_ALERT` (150).
`COSY_SCORECARD_ENABLED=false` switches the job off.


### Weekly fine-tuning review (#832) — suggestions only

`src/tuning_review.py`, APScheduler job `tuning_review_weekly` (Sunday 09:00 local:
`TUNING_REVIEW_ENABLED=true`, `TUNING_REVIEW_DOW=6`, `TUNING_REVIEW_HOUR_LOCAL=9`).
Replays the last 7 complete local days (`replay_day`, forward mode, live DB,
`TUNING_REVIEW_CADENCE=stride:4` recalcs per day) with the control and each knob
at +/-1 step (`LP_W3_NIGHT_FLOOR_C` 0.5, `LP_W3_PEAK_COAST_DELTA_C` 0.5,
`INDOOR_SETPOINT_C` 0.5, `DHW_TEMP_NORMAL_C` 1,
`LP_LOAD_EXPENSIVE_BAND_QUANTILE` p75<->p90 — now a runtime setting), via
`lp_overrides.patched_config` (in-memory, restored; **it never calls
`set_setting`**). Cost = replayed plan cost under actual prices; comfort = hours
the predicted indoor trajectory is below the CONTROL's night floor / peak floor
plus the tank shortfall at 20:00 vs `shower_windows`. Verdicts: `recommended`
(saves >= 5 p/week, hours-below +<=0.5, no extra shower-shortfall day),
`trade-off` (saves but costs comfort), `comfort-first` (cheapest zero-hours variant
when the control has hours-below > 0); the rest is dropped. Rows land in
`tuning_suggestions` with a PUT-ready payload (`PUT /api/v1/settings/{key}/simulate`
then `PUT /api/v1/settings/{key}` + `X-Simulation-Id`, body `{"value": ...}`).
`GET /api/v1/tuning/suggestions?weeks=4` (viewer), `POST /api/v1/tuning/run`
(admin, single-flight 10 min, 409 when busy). One Telegram summary (top 3), muted
when nothing is recommended. Insights card `TuningSuggestionsCard` (admin Apply =
the Settings simulate->confirm->apply flow). Story-3 plug point:
`external_comfort_signal(week_start)` (returns None today; the dict is stored in
`payload.external_comfort`, ranking untouched). Skips when < `TUNING_REVIEW_MIN_DAYS`
(3) replayable days, or when `DAIKIN_CONTROL_MODE=passive` / `LP_W3_TIN_ENABLED=false` (W3 knobs would be inert; forward mode uses the LIVE process config, recorded as `payload.context`). `DHW_DYNAMIC_BOOST_HOLD_HOURS` is NOT swept (its only reader is the persisted nightly window decision, never re-resolved by a replay). Replays seed W3 from `lp_inputs_snapshot.indoor_initial_c` and chain `plan.indoor_temp_c`. Single-flight has no TTL (variants share `config._overrides`); the per-day budget (~9 min) stops the run (status `partial`, persisted in the payload).


### Closed-loop auto-tune (#876) — story 1: ledger

`src/auto_tune.py` is the ONE place for ledger semantics. Story 1 ships the ledger,
manual-always-wins, API/MCP and the kill switch; there is NO automatic apply path of
its own and NO judge yet (stories 2-3). Table `auto_tune_changes` (`key, old_value,
new_value, source tuning_review|comfort_feedback|learned_param|plan_pattern|manual,
applied_at, baseline_json, status evaluating|kept|rolled_back|superseded_manual|manual,
evaluated_at, evidence_json, frozen_until, note`; index `(key, applied_at)`). DB helpers
in `db.py`: `insert/get/update_auto_tune_change(s)`, `get_active_auto_tune_change`,
`auto_tune_frozen_until`.

- **Kill switch** `AUTO_TUNE_ENABLED` (runtime, code default **false**). Off = no automatic
  change is ever applied (`propose_change` -> `disabled`), bit-for-bit today's behaviour.
  The only thing that still happens when off: the owner's changes to allowlisted/ledger
  keys are RECORDED as `manual` rows (history for the drift cap / Settings surface).
- **Allowlist** `AUTO_TUNE_ALLOWLIST` (runtime, comma list, stored lower-case, matched
  case-insensitively; default = the 10 keys of the epic). **Hard `DENYLIST`** (constant, wins
  over the allowlist): `LP_W3_EMERGENCY_FLOOR_C, DHW_TEMP_MIN_FLOOR_C, DAIKIN_CONTROL_MODE,
  OPENCLAW_READ_ONLY, MIN_SOC_RESERVE_PERCENT, OPTIMIZATION_PRESET, VACATION_RETURN_AT,
  REQUIRE_SIMULATION_ID`, prefix `AUTO_TUNE_`, substring `LEGIONELLA` (the `DHW_LEGIONELLA_*`
  keys ARE runtime settings; the stand-off keys are env-only).
- **`propose_change(key, value, source, note=, now=)`** applies via
  `set_setting(actor="auto_tune:<source>")`, stores `baseline_json` (`baseline_metrics`: 7-day
  net GBP/day, hours-below-floor/day, shower-shortfall days, peak import kWh/day from
  `cosy_scorecard_daily`; `{}` when none), opens an `evaluating` row, logs `action_log`
  `auto_tune_apply` (device `auto_tune`); NO Telegram on apply in story 1 (story 2 adds it).
  Refusals (`{applied: False, reason}`), in this order: `disabled | denylisted | not_allowlisted |
  cron_reload (spec has cron_reload) | invalid | pinned (a `config._overrides` pin, e.g. a Workbench
  what-if, shadows the DB: the persisted value is the only reference, and it is refused rather than
  guessed) | unchanged | busy (one evaluating row) | frozen | step_too_large | drift_cap | away |
  no_baseline (< 5 usable scorecard days) | changed (the owner wrote between our check and our write)`
  (+ `ledger_error`: the insert failed after the write; the setting is restored, and one
  `notify_risk` fires if the restore failed too). Step = `auto_tune.STEPS` (SettingSpec has no
  step; fallback (max-min)/20), enum keys accept any one other value. Drift cap =
  `auto_tune.MAX_DRIFT` (comfort 1.5 C, DHW 3 C, else step x 3) from the owner's newest `manual`
  value, or the value before the first automated change. `away` = `kv_state vacation_end_at` set,
  preset `vacation`, or `daikin_holiday_active` with any point true. Enum step = adjacent values only.
  `baseline_metrics` excludes holiday/vacation days from every metric and ends at yesterday in
  `BULLETPROOF_TIMEZONE` (the scorecard key).
- **Manual always wins.** `runtime_settings.set_setting` / `delete_setting` call
  `auto_tune.on_manual_change` (never raises). Actors `auto_tune:*` and the internal writers
  (`vacation_end`, `comfort_feedback_auto_tune`, `api_batch_rollback`, `workbench_promote_rollback`)
  are skipped; every other actor (`api`, `mcp`, `api_batch`, `workbench_promote`,
  `snapshot_restore`) is the owner; a write that does not change the value is ignored. For a key on the allowlist or with ledger rows: a `manual` row with
  `frozen_until = now + AUTO_TUNE_MANUAL_FREEZE_DAYS` (14), and any `evaluating` row of the key ->
  `superseded_manual` (`evidence {reason: manual_change, actor, manual_id}`), never rolled back. The hook
  takes the same lock as `propose_change`; if a proposer's write landed AFTER the owner's, the owner's
  value is restored. A failed batch / workbench promote that is undone (`api_batch_rollback`,
  `workbench_promote_rollback`) deletes the manual row it had just produced and re-opens the superseded one.
- **Verbs**: `rollback(id, reason=, evidence=, actor=)` (`set_setting` old value as
  `auto_tune:rollback`, row `rolled_back`, `frozen_until = now + AUTO_TUNE_ROLLBACK_FREEZE_DAYS` (28),
  `action_log auto_tune_rollback`, one `notify_risk` deduped `auto_tune_rollback_<id>`; only
  `evaluating`/`kept` automated rows, and only the NEWEST row of its key while the setting still holds its
  value, else `superseded`), `keep(id, evidence)`, `freeze(id | key=, days=)`,
  `ledger_state(key)`, `ledger_summary()`.
- **Comfort feedback (#833)** goes through `propose_change(source="comfort_feedback")` when
  `AUTO_TUNE_ENABLED`. NOTE: with the ledger ON the legacy flag no longer gates anything - the weekly
  comfort change IS applied even if `COMFORT_FEEDBACK_AUTO_TUNE=false` (the ledger replaces the gate).
  With the ledger off, `COMFORT_FEEDBACK_AUTO_TUNE=true` keeps its old direct write (no ledger row,
  internal actor) except that it skips a key the owner froze (`auto_tune_frozen_until`).
- **API** (`src/api/routers/auto_tune.py`): `GET /api/v1/tuning/ledger?limit=&key=` (viewer),
  `POST .../ledger/propose` (`{key, value, source}`; `manual_test` is stored as `tuning_review`;
  refusal = 409 with `detail.reason`), `POST .../ledger/{id}/rollback` (`{reason}`),
  `POST .../ledger/{id}/freeze` (`{days}`); writes are admin (401 without an admin token, as for every
  `/api/v1` write). `REQUIRE_SIMULATION_ID=true` makes `propose` and `rollback` answer 409
  `simulation_required` (story 1 does not implement the simulate flow for ledger writes; use
  `PUT /settings/{key}`); `invalid` -> 422, rollback needs a `reason`. `GET /api/v1/settings` items carry `auto_tune {active, frozen_until, last_status}` for
  allowlisted / ledger-touched keys. **MCP**: `get_auto_tune_ledger(limit)`, `rollback_auto_tune_change(id, reason, confirmed=False)` (dry-run
  by default; refuses under `OPENCLAW_READ_ONLY`).
  Scorecard payload `auto_tune.active_change {id, key, new_value, status, day_n_of_7}` for the SCORED day (change
  applied before the day ended and not evaluated before it started; `day_n_of_7` uncapped).
- Runtime knobs: `AUTO_TUNE_ENABLED`, `AUTO_TUNE_ALLOWLIST`, `AUTO_TUNE_MANUAL_FREEZE_DAYS`,
  `AUTO_TUNE_ROLLBACK_FREEZE_DAYS`.

### Closed-loop auto-tune (#876) — story 3: judge

`src/auto_tune_judge.py` judges the single `evaluating` ledger row (never `manual` /
`superseded_manual`). Two daily cron jobs on the LOCAL clock, `COSY_SCORECARD_HOUR_LOCAL:MINUTE`
+ 10 min (`auto_tune_tripwires`; `auto_tune_verdict` 30 s later; `CronTrigger` with the
`BULLETPROOF_TIMEZONE` zone, so DST-safe). Both are single-flight (`_job_lock`), never raise, and
are no-ops with `AUTO_TUNE_ENABLED=false` (a change left `evaluating` stays so; `action_log`
`auto_tune_judge_disabled`; the owner can still `POST /tuning/ledger/{id}/rollback`).
Away days (scorecard `holiday` / `vacation`, or away right now for today) never count anywhere.
Every verdict outcome writes an `action_log` `auto_tune_verdict` row.

- **`intent`** (nullable `auto_tune_changes.intent`, `ALTER TABLE` guard; exposed on every ledger row
  and `ledger_state`): `saving | trade_off | comfort_up | comfort_down | learned`.
  `auto_tune.propose_change(..., intent=None, expected_comfort_hours=None)` stores it; when None it is
  derived (`auto_tune.derive_intent`): source `comfort_feedback` -> `comfort_up` / `comfort_down` by
  direction (floor up, peak delta down, setpoint up, DHW up = up), `learned_param` -> `learned`, else
  `saving`. `expected_comfort_hours` (the review row's predicted comfort cost) is stored in
  `evidence_json` and read by the `trade_off` rule. The verdict judges each bet against ITS clauses (the
  default rule would roll back whole classes by design: a "cold" floor raise costs money, a "hot"
  lowering sits under the old floor, a `trade-off` row exceeds the 0.5 h band by definition).
- **Tripwires** (`tripwire_check(change, now) -> {name, evidence} | None`, data readers only). Class
  and direction come from the key and `old_value -> new_value`; a tripped wire calls
  `auto_tune.rollback(id, reason="tripwire:<name>")` (restore, 28 d freeze, ONE notify, log). Reports
  closer than 1 h count once (per-room double reports, double taps).

  | Class (key, direction) | Tripwire (since `applied_at`) |
  |---|---|
  | comfort loosening: `LP_W3_NIGHT_FLOOR_C` down, `INDOOR_SETPOINT_C` down, `LP_W3_CEILING_C` down, `LP_W3_PEAK_COAST_DELTA_C` up | `cold_feedback` (>= 2 `cold` verdicts >= 1 h apart inside any 3-day window), `lwt_emergency` (any), `comfort_backstop` (any `lwt_comfort_backstop` with result `ok` = fired) |
  | `LP_W3_CEILING_C` raised | `hot_feedback` (same burst rule) or `warm_backstop` (any `lwt_warm_backstop` fire) |
  | DHW loosening: `DHW_TEMP_NORMAL_C` / `DHW_TEMP_COMFORT_C` down | `shower_floor`: scorecard days AFTER the apply day with `tank.any_below_floor`, only when their count exceeds the baseline's `shower_shortfall_days` rate x days. The apply-day evening shower is NOT counted (half old policy) |
  | `LP_LOAD_EXPENSIVE_BAND_QUANTILE` p90 -> p75 only (p75 -> p90 only adds insurance: no wire) | `peak_import`: scorecard `peak_import_kwh` > `AUTO_TUNE_PEAK_TRIP_KWH` (1.0) AND > baseline `peak_import_kwh_per_day` + that limit (a cold snap must not roll back more insurance) |
  | learned: `LP_W3_SOLAR_GAIN_KW_PER_PV_KW`, `LP_W3_INTERNAL_GAIN_KW`, `LP_W3_EMITTER_TAU_MIN` | `pred_err`: `lwt_learning_daily.pred_err_mean_c` (abs) > 1.5x the baseline AND >= +0.2 C, on 2 consecutive days. Baseline = mean over the 7 days before the apply day (>= 3 needed); without one there is no tripwire and `kv_state` `auto_tune_tripwire_<id>.notes` says `no_pred_err_baseline`. (`pred_err_mean_c` is a signed mean, so its abs can hide cancelling errors; the p90 column is the stricter alternative if this proves too quiet) |
  | tightening (the inverse directions) | none (cost verdict only) |

  Each run stores `{checked_at, tripped, notes}` in `kv_state` `auto_tune_tripwire_<id>`; the scorecard
  `payload.auto_tune.active_change` carries `tripwire_checked` / `tripped` (state at scorecard build time,
  so the 07:30 build shows the previous day's check).
- **Day-7 verdict.** Window = the 7 local days `apply_day .. apply_day+6` (the apply day is replayed on both
  arms: a valid counterfactual over the same inputs; story 1 numbers it `day_n_of_7 = 1`). Due when
  `local_today >= apply_day + 7`, so a Sunday 09:20 apply is judged the NEXT Sunday 07:40:30, and the ledger
  is free before the 08:45 comfort job (the old `apply_day+1..+7` window judged it Monday and halved the
  cadence). Lived signals: cold feedback AFTER = from `applied_at` to now; BEFORE = the 7x24 h before
  `applied_at`, normalised for away days in it (scaled by 7 / usable days; no usable day = clause skipped).
  Order in `verdict_job`: tripwires first, then the replay with `tuning_review.evaluate` (forward mode, live
  DB, `TUNING_REVIEW_CADENCE`), interleaved day by day so a budget stop leaves the same days on both arms:
  arm OLD = `patched_config({key: old_value})`, arm NEW = `patched_config({key: new_value})`. **Both arms are
  replays**: realised cost carries execution noise (device lag, write failures, replans) that a replay lacks,
  and nothing proves forward-mode replay reproduces the committed plans, so mixing a realised and a replayed
  arm would put that gap into the verdict. The comfort yardstick (`tuning_review.yardstick`) is taken under
  the OLD value for both arms (otherwise lowering a floor would trivially win), except `comfort_down` (new
  value). Rule per intent (`decide`, bands scale with `n_days/7` on a partial verdict):

  | intent | KEEP only if |
  |---|---|
  | `saving` | cost(new) <= old + `AUTO_TUNE_MIN_GAIN_PENCE` (5 p/week); hours below floor <= old + 0.5 h; no extra shower-shortfall day; cold after <= before + 1 |
  | `trade_off` | still SAVES >= min gain; hours <= old + `expected_comfort_hours` (default 2x tolerance) + tolerance; no extra shower day; cold guard |
  | `comfort_up` | hours below floor not worse; cost <= old + 2x min gain; no extra shower day; cold after <= before |
  | `comfort_down` | (hours under the NEW floors) hours band 0.5 h; cost not worse; no extra shower day; cold guard |
  | `learned` | cost <= old + min gain AND lived prediction error (`pred_err_mean_c`, abs, 7 days after vs 7 before) not worse by > 50 % and 0.2 C |

  A KEEP is re-checked against the tripwires first. `evidence_json`: both arms (`arm_old`/`arm_new`),
  per-clause deltas, `intent`, `rule` (`all_clauses_passed` | `failed:<clauses>` | `tripwire:<name>`),
  `cadence`, `window`, `days_replayed`, `days_skipped_away`, `days_failed` (replay failed) vs
  `days_budget_cut`, `partial`, `cold_feedback`, `yardstick`, `deferred`, `busy_retries`. Rollback goes
  through `auto_tune.rollback` (which sends the ONE rollback message and now also CLEARS a pinned in-memory
  override of the key); KEEP is silent unless `AUTO_TUNE_NOTIFY_KEPT=true`.
- **Refusals are terminal.** If `rollback` refuses (value moved by someone else, a newer row, invalid
  restore) the row is closed as `superseded_manual` with `evidence.reason="rollback_refused:<why>"`,
  `action_log` `auto_tune_rollback_refused` and one `notify_risk` (`auto_tune_rollback_refused_<id>`):
  it must not stay `evaluating`, replay daily and block the ledger. `keep` returning False is reported
  `keep_refused`, never `kept`.
- **Budget, deferral, undecidable.** `AUTO_TUNE_VERDICT_BUDGET_SECONDS` (1800, clamped to 45 min so the
  verdict always ends before the Sunday 08:45 comfort job; `register_jobs` warns when judge time + budget
  would not) caps the whole replay. >= 4 common days on both arms decide (`partial=true` if the budget cut
  it); fewer defer to the next day (`evidence.deferred += 1`, status stays `evaluating`). An exception from
  the replay / `day_comfort` / `_aggregate` is a deferral too (`reason=replay_error:<Type>`), never a daily
  `error` loop. A busy replay lock (a weekly review running) retries next day WITHOUT counting
  (`busy_retries`); being away now waits too. On the 4th failed attempt (3 deferrals) the change is KEPT with
  `reason="undecidable"` plus one `notify_risk`: it already passed the tripwires for ~10 days, and an
  unjudgeable change must neither block the ledger forever nor be reverted on missing data. A key that cannot
  be replayed with its old value defers the same way (never a trivial keep of two identical arms).
- **Replay whitelist (#790 exposure).** `LP_W3_CEILING_C`, `LP_W3_SOLAR_GAIN_KW_PER_PV_KW` and
  `LP_W3_INTERNAL_GAIN_KW` were not in `lp_overrides.WHITELIST`, so `patched_config` silently ignored them;
  they are added (comfort group, **promotable**), which also makes them three new fields in the Workbench
  editor and promotable through the Workbench. `workbench_simulate` returns 409 while a tuning review / verdict
  replay holds `tuning_review._running`.
- **Known limitation (follow-up).** `patched_config` patches the process-wide `config` singleton, not a
  thread-local one: while a verdict attempt replays, the whole process (heartbeat, API reads, the dispatch
  job) can read the OLD-arm value of the key, ~40 s per verdict attempt measured. Thread-local overrides are
  tracked in a separate follow-up; until then the single-flight lock + the daily window are the mitigation.
- Env knobs: `AUTO_TUNE_PEAK_TRIP_KWH`, `AUTO_TUNE_MIN_GAIN_PENCE`, `AUTO_TUNE_VERDICT_BUDGET_SECONDS`,
  `AUTO_TUNE_NOTIFY_KEPT` (all env-only, read at call time). Kill switch: `AUTO_TUNE_ENABLED=false`.

### Closed-loop auto-tune (#876) — story 2: weekly apply

`src/auto_tune_apply.py` turns AT MOST ONE suggestion of the Sunday tuning review into a ledger change
per ISO week (`auto_tune.propose_change(source="tuning_review")` - every story-1 guard still applies;
this module never writes a setting itself).

- **Job** `auto_tune_weekly_apply`: `TUNING_REVIEW_DOW`/`HOUR_LOCAL` + env `AUTO_TUNE_APPLY_DELAY_MINUTES`
  (default 20 -> Sunday 09:20 local, `BULLETPROOF_TIMEZONE`, rolls to the next weekday past midnight),
  `runner.register_auto_tune_apply_job`. Always registered; `AUTO_TUNE_ENABLED` (runtime) and
  `TUNING_REVIEW_ENABLED` are checked at run time. `no_review` when the newest `tuning_suggestions`
  `week_start` is not the week the review covers (`review local date - 1 - (TUNING_REVIEW_DAYS-1)`, the
  review date being now minus the apply delay, so a delay crossing local midnight still matches). If the
  review is still running the job backs off 5 min ONCE (one-shot `auto_tune_weekly_apply_retry`), then
  `review_running`.
- **Quota**: `weekly_quota_used(now)` = non-manual ledger rows with `applied_at` in the current local ISO
  week (any source; a rollback does not free it; a DB error FAILS CLOSED = spent). The comfort-feedback job (08:45) checks it before its
  own proposal (`action_log` comfort/`auto_tune` `skipped=weekly_quota`); the review apply (09:20) sees
  a spent week as `weekly_quota`. `kv_state` `auto_tune_last_apply_week` is informational only.
- **Cold week = no loosening (review fix)**: with >= 1 `cold` feedback in the review's week, ANY candidate
  with `delta_comfort_hours > 0` is skipped (`cold_feedback`), whatever its verdict; a review row on the
  same key as the 08:45 comfort job's proposal (read from its `weekly_summary` action_log of the last 24 h)
  that moves the other way is skipped (`conflicts_comfort`). `trade-off` additionally needs comfort cost
  <= 2 x `TUNING_REVIEW_COMFORT_TOLERANCE_H` and feedback on >= 2 distinct local days.
- **Selection** (`select_weekly_change` / `evaluate_candidates`, pure): `recommended` first (largest
  weekly saving, tie: smaller `delta_comfort_hours`); `comfort-first` only when the control had
  `hours_below_current > 0` AND the week had >= 1 `cold` feedback; `trade-off` only when the week had
  >= 2 feedback days and NO `cold`, it saves money, loosens comfort (hours up, capped) and adds no
  shower-shortfall day. Only rows of the NEWEST build (`built_at_utc`) of the week are considered. Any candidate is skipped (reason recorded, next one tried) when: key not allowlisted, frozen,
  review `current_value` != live persisted value (`stale`), already at the value, or more than one
  `auto_tune` step away (`step_too_large`). Comfort signal = `comfort_feedback.weekly_summary(week_start)`.
- **Outcome**: every run writes `action_log` device `auto_tune` action `weekly_apply` (applied / reason /
  candidates considered with per-candidate skip reasons / comfort counts). Reasons: `disabled`,
  `review_disabled`, `weekly_quota`, `no_review`, `busy`, `nothing_eligible`, any `propose_change`
  refusal (`away`, `frozen`, `no_baseline`, ...), `error`. Never raises.
- **Telegram** (`notifier.notify_auto_tune_applied`, AlertType `auto_tune` with its own default route,
  enabled and NOT silent, honours `OPENCLAW_NOTIFY_ENABLED`): ONE pt-BR message per applied change (label
  from `KEY_LABELS_PT`, "Simulação da revisão (N dias): −32p/sem, +0,3 h abaixo do piso" - a REPLAY delta,
  hours clause omitted when |h| < 0.05, "(parcial)" for a partial review, "Avalio por 7 dias e reverto sozinho se piorar", rollback endpoint). Nothing
  is sent when nothing is applied, except `notify_auto_tune_held` (busy / frozen with a saving >= 20
  p/week) which is OFF unless env `AUTO_TUNE_NOTIFY_SKIPPED=true`.
- **Surfaces**: `GET /api/v1/tuning/ledger` adds `next_apply_at` (null when `AUTO_TUNE_ENABLED` or
  `TUNING_REVIEW_ENABLED` is off), `apply_enabled` and `weekly_quota_used`. While a change is
  `evaluating`, the other Insights tuning rows are `kind="suggestion"` with no Apply button ("Aguarde:
  outra mudança em avaliação"). The Insights
  tuning items carry `ledger {status, applied_at, day_n, id}` when the key has an
  `evaluating|kept|rolled_back` row in the last 14 days; when the evaluating change IS that suggestion
  the item is `kind="status"` ("Aplicado dom 02/11 · avaliando dia 3/7") with no Apply button.
- **Known edges**: the comfort job's window is the trailing Mon..Sun (08:45) while
  `weekly_summary(week_start)` for the review is Sun..Sat, so a feedback on the boundary day may count in
  one and not the other. A non-Sunday `TUNING_REVIEW_DOW` splits the shared quota across ISO weeks (the
  comfort job and the apply can both fire in one calendar week); the ledger `busy` check is the real
  one-at-a-time guard. Deploy with story 3 (the judge, #889) or keep `AUTO_TUNE_ENABLED=false`: without
  it an `evaluating` row is never closed and every later week is `busy`.
- **Kill switches**: `AUTO_TUNE_ENABLED=false` -> the review stays suggestions-only, bit-for-bit;
  `TUNING_REVIEW_ENABLED=false` -> no review, hence no apply.

