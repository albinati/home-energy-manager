# Changelog

## v9.1.1 — 2026-04-21 — Phase 4: quota hardening, user-override acceptance, OpenClaw MCP boundary

Closes epic #39. Closes sub-issues #40 #41 #42 #43 #44.

### Added

- **`simulate_plan` MCP tool** — OpenClaw can dry-run a plan with whitelisted config overrides (occupancy, DHW temps, preset) without touching hardware or Daikin quota. Response includes `applied_overrides` + `ignored_overrides` so the caller can tell when keys are accepted but not yet wired.
- **User-override acceptance loop** — when you change the tank temp or climate on the Daikin Onecta app, the next heartbeat marks the action row as overridden, notifies once, and replans on top of the new reality. No more fighting the app. Unblocks the "Daikin as passive load" direction in #30.
- **`action_schedule.overridden_by_user_at`** column (nullable TEXT, added via idempotent ALTER TABLE migration).
- **OpenClaw MCP-only boundary** — `docs/OPENCLAW_BOUNDARY.md` enumerates the sanctioned MCP tool surface and explicit out-of-bounds list (no filesystem, no shell, no direct cloud HTTP). Boot-time `audit_mcp_tool_surface` warns loudly if any hardware-write tool lacks a `confirmed` parameter, and errors loudly if the FastMCP private tool registry is empty (catches silent API drift).
- **Transport-layer Daikin quota accounting** — every outbound HTTP call from `DaikinClient._get`/`_patch` is counted, success or failure, so ad-hoc callers (boot recovery, direct client usage) can no longer burn quota silently.
- **DHW dedup** — `DaikinDevice.tank_on` and `tank_powerful` are now parsed from Onecta; `daikin_device_matches_params` skips redundant `tank_power`/`tank_powerful` PATCHes per slot.

### Changed

- `DaikinClient._get`/`_patch` route `record_call` through a `_safe_record` helper that swallows SQLite errors — accounting problems never shadow a successful HTTP response. [Review fix: PR #45]
- `DaikinDevice.is_on` is now `Optional[bool]`, defaulting to `None`. A parse glitch no longer false-flags `climate_on=True` rows as user-overridden. [Review fix]
- Boot-recovery hardening: `_reconcile_daikin_actions` uses a process-local `_FIRST_APPLIED_SESSION` map as the grace-period anchor. After a systemd restart mid-plan, the first reconcile tick pushes our value rather than treating the ancient `start_time` as evidence of a user override. [Review fix]
- `simulate_plan` serializes through `_optimizer_executor` (max_workers=1 queue shared with `propose_optimization_plan`) so concurrent simulates cannot race each other or a live optimizer run on `config.*` globals. [Review fix]
- `simulate_plan` validates override values per-key (type + range) before any config mutation; invalid values are rejected with a clear error. [Review fix]
- `simulate_plan` response shape normalized across success and error paths — same top-level keys every call. [Review fix]
- `run_lp_simulation(allow_daikin_refresh=False)` plumbs down to `read_lp_initial_state` so a cold MCP-process cache cannot burn Daikin quota during "read-only" simulation. [Review fix]
- Override rows past `end_time` now transition to `completed` instead of staying `active` forever. [Review fix]
- Config helper `env_int_at_least(name, default, minimum)` clamps `DAIKIN_OVERRIDE_GRACE_SECONDS` to ≥ 60 s so setting `0` in `.env` cannot self-DoS the system. [Review fix]
- Canonical `CREATE TABLE action_schedule` in `src/db.py` now declares `overridden_by_user_at` alongside the migration. [Review fix]
- `tests/test_api_quota.py` fixture uses `monkeypatch.setattr` on the live config instance instead of `importlib.reload`. Removes cross-test pollution that had been breaking `test_db_dhw_standing_loss`, `test_db_micro_climate`, and `test_bulletproof_db` when run in full-suite order. [Review fix]

### Known issues (to address in a follow-up)

- **Duplicate / overlapping `action_schedule` rows at a single slot** — observed in production (~30 rows converging on 21:00, including multiple "tank_power=True" for the same slot). Not caused by this PR; appears to be an LP-dispatch or replan-amnesia regression. Needs separate investigation — quota accounting + dedup in this PR will mask the symptom but not the root cause.
- **Daikin 200-call/day budget exhaustion** observed on 2026-04-21 in production. The fixes in this PR should substantially reduce call volume once deployed, but the dispatch-dup bug above is likely the primary cause — fixing it is the durable remedy.

## 2026-04-20 — Daikin reliability, notification deduplication, DHW tuning

### Daikin write fixes

- **`daikin_bulletproof.py` — tank power ordering:** `set_tank_power(True)` is now called *before* `set_tank_temperature`. Daikin Onecta returns `READ_ONLY_CHARACTERISTIC` on the `temperatureControl` endpoint when the tank is powered off. A 10 s settle sleep follows (Daikin cloud propagation lag; consistent with the existing 3-way valve settle). If temperature still fails after power-on (cloud lag race), the error is non-fatal and the heartbeat retries on the next tick.
- **`lp_dispatch.py` — LP float precision:** `lwt_offset` and `tank_temp` values from the PuLP solver are now `round(x, 1)` before being written to `action_schedule`. Raw LP floats like `-3.55e-15` (float epsilon) and `53.403446` caused `INVALID_CHARACTERISTIC_VALUE` / `READ_ONLY_CHARACTERISTIC` rejections from the Daikin API.
- **`lp_dispatch.py` — no `tank_temp` when tank off:** `tank_temp` is only included in action params when `tank_power=True`. Setting a target temperature on a powered-off tank is always rejected; the target is meaningless until the tank turns on.

### Notification deduplication

- **`runner.py` — slot-kind debounce:** `push_cheap_window_start` / `push_peak_window_start` now fire only when `slot_kind` *changes* (cheap→standard, standard→peak, etc.). Previously every heartbeat tick during a cheap window sent a fresh alert — up to 24 messages over a 2-hour window.
- **`octopus_fetch.py` — removed duplicate plan notification:** `notify_strategy_update` was firing immediately after the optimizer completed, alongside `notify_plan_proposed` from `_write_plan_consent`. Two hook POSTs per plan → two OpenClaw agent wake-ups → two Telegram messages. `notify_strategy_update` removed from the fetch path; `notify_plan_proposed` is the single source of truth.
- **`notifier.py` — suppress `unknown` fox_mode:** `CHEAP_WINDOW_START` hook payload omits `fox_mode` when the FoxESS V3 API returns `"unknown"` (which it always does — work mode is not exposed in the realtime endpoint). Removes confusing "FoxESS reported mode: unknown" noise from every cheap-window alert.

### Plan consent & config

- **`PLAN_AUTO_APPROVE=true`** — plans are applied immediately on generation. The `[AUTO-APPLIED]` notification confirms execution. Use `reject_plan(plan_id)` within the expiry window to roll back. This eliminates the `pending_approval` gate that caused repeated plan notifications across restarts.
- **`DHW_TEMP_NORMAL_C=45.0`** — restore and safe-default tank target reduced from 50 °C to 45 °C. 45 °C is sufficient for one sequential shower session plus one 5-min morning shower (confirmed usage profile). Saves ~5 °C of unnecessary thermal cycling on every restore action.
- **`TARGET_DHW_TEMP_MIN_GUESTS_C=55.0`** — raised from 48 °C to 55 °C. 48 °C was insufficient for multiple showers in the 20:30–22:00 window. Guest-mode plans now target 55 °C. `DHW_TEMP_CHEAP_C=60` and `DHW_TEMP_MAX_C=65` unchanged.

## v9.1.0 — 2026-04-19 — Hardening: peak sync, env cleanup, providers, tooling

- **Scheduler peak sync:** `scheduler_peak_contains_wall_time` / `utc_instant_in_scheduler_peak` in `agile.py`; `compute_lwt_adjustment` uses the same local-wall-clock rule as Agile slot peak detection (fixes BST skew for Daikin LWT).
- **Removed:** legacy `ALERT_OPENCLAW_URL` / `ALERT_CHANNEL` from `config` (use `OPENCLAW_*` only).
- **API:** British Gas provider entry stays in the enum but `is_configured=false` until integration exists; 503 messages no longer suggest `BRITISH_GAS_API_KEY`.
- **API:** energy provider stub routes moved to [`src/api/routers/energy_providers.py`](src/api/routers/energy_providers.py) and mounted from `main` (paths unchanged).
- **FoxESS:** removed `FoxESSClient.get_device_settings()` (unsupported by Open API; use `get_device_setting(key)`).

## 2026-04-19 — V9: solar_charge, MPC cadence, BST fix, preset DHW

### Solar-only charging (Fox ESS)
- **`solar_charge` slot kind** (`lp_dispatch.py`): LP slots where `battery_charge > 0` and `grid_import ≈ 0` are now `SelfUse minSocOnGrid=100%` instead of `ForceCharge`. Eliminates the "blind ForceCharge" that pulled up to 4.8 kW from grid during PV generation hours. Hardware-tested on 2026-04-19; saves ~£2.50–3.20/day on sunny days vs the prior schedule. Closes #14.
- `FOX_SOLAR_CHARGE_MIN_SOC_PERCENT` env var (default 100) controls the floor.
- Fox group builder extended to carry `minSocOnGrid` per-group through merge pipeline (4-tuple).

### MPC intra-day re-plans
- `LP_MPC_HOURS=6,9,12,15` — four checkpoints covering solar window start (09:00), mid-day (12:00), pre-peak (15:00), and morning anchor (06:00). Closes #13.
- `LP_MPC_WRITE_DEVICES=true` — MPC and Octopus-fetch-triggered re-plans now push to Fox/Daikin hardware. Previously compute-only.
- The Octopus fetch job at 16:05 already called `run_optimizer()`; with `LP_MPC_WRITE_DEVICES=true` this is now the critical post-rate-publish re-plan that adjusts the overnight 00:00–08:00 cheap strategy.
- API budget headroom confirmed: Fox ~384/day (27% of 1440), Daikin ~99/day (50% of 200).

### Bug fixes
- **BST timezone**: `agile.py:get_current_and_next_slots()` now converts UTC `valid_from` to `Europe/London` before comparing against `peak_start`/`peak_end`. Previously the 15:00 UTC slot (= 16:00 BST) was outside the peak window during summer.
- **False notifications**: `push_cheap_window_start` / `push_peak_window_start` removed from optimizer planning loop; re-emitted in `runner.py` heartbeat with live SoC + fox_mode. Eliminates "SoC=None" Telegram alerts.
- **Preset-aware DHW**: `lp_optimizer.solve_lp()` reads `OPTIMIZATION_PRESET` and selects `TARGET_DHW_TEMP_MIN_GUESTS_C` (48°C) or `TARGET_DHW_TEMP_MIN_NORMAL_C` (45°C). Previously hardcoded to normal.
- **Strategy string** now includes `solar=N` slot count.

### Issues closed
- #12 — FoxESS V3 has no native solar-only charge mode; `SelfUse + minSocOnGrid=100%` is the correct workaround.
- #13 — MPC frequency: `LP_MPC_HOURS=6,9,12,15` + `LP_MPC_WRITE_DEVICES=true`.
- #14 — Blind ForceCharge replaced by solar_charge logic.
- #16 — V8 refactor: notifications, BST fix, preset DHW, solar-only charging.

### Issues opened
- #18 — Daikin HTTP 400 payload pruning: `lwt_offset` is read-only when `climate_on=false` and zone already off; re-send without it on 400.

## 2026-04-19 — OpenClaw hook-only notifications

- **Breaking:** User-facing notifications no longer use `openclaw message send` (subprocess). All deliveries use **`POST` to `OPENCLAW_HOOKS_URL`** (Gateway `/hooks/agent`). Set **`OPENCLAW_HOOKS_URL`** and **`OPENCLAW_HOOKS_TOKEN`** when `OPENCLAW_NOTIFY_ENABLED=true`.
- **Removed env:** `OPENCLAW_CLI_PATH`, `OPENCLAW_CLI_TIMEOUT_SECONDS`, `OPENCLAW_PLAN_NOTIFY_MODE`.
- **Behaviour:** On hook failure, only stdout logs — no CLI fallback. See `docs/RUNBOOK.md` and `docs/openclaw-nikola-plan-prompt.md`.

## 2026-04-18 — V8 PuLP MILP planner

- **Default planner:** `OPTIMIZER_BACKEND=lp` runs `src/scheduler/lp_optimizer.solve_lp` (PuLP + CBC): battery, grid, PV curtailment, DHW tank + building thermal, COP vs outdoor temperature, discrete HP buckets. Dispatch: `src/scheduler/lp_dispatch.py`. Weather: `forecast_to_lp_inputs()` in `src/weather.py`.
- **Rollback:** `OPTIMIZER_BACKEND=heuristic` restores the price-quantile classifier (`_classify_slots`, overnight consolidation, pre-peak extension). API: `POST /api/v1/optimization/backend` with `{"backend":"heuristic"}` or `"lp"`.
- **Removed:** `TARGET_PRICE_PENCE` and `/api/v1/optimization/target-price` (replaced by backend switch). MCP: `set_optimizer_backend` replaces `set_target_price`.
- **Dependencies:** `pulp>=2.8` in `requirements.txt`.
- **PoC scripts removed:** `pulp_simulation.py`, `pulp_daikin_sim.py`, `run_tomorrow_pulp_solar.py` (superseded by `lp_optimizer.py`).

## 2026-04-17 — Remove V7 optimization stack

- **Single planner:** Only the Bulletproof path (`src/scheduler/optimizer.py`, SQLite, Fox Scheduler V3, heartbeat) schedules hardware. The parallel V7 package (`src/optimization/`: solver, dispatcher, consent, executor) was deleted so two schedulers cannot conflict.
- **Rollback:** An annotated git tag **`pre-v7-removal`** points at the last commit that still contained `src/optimization/`. Restore that tree with:
  `git checkout pre-v7-removal -- src/optimization`
  then rewire imports if you need to run it again.
- **API / MCP:** `/api/v1/optimization/*` and MCP tools keep similar names; **propose** runs `run_optimizer`. Consent **approve/reject** are no-ops. **GET …/plan** returns SQLite + Fox snapshot instead of a 48-slot solver table. **dispatch-preview** returns a retired notice.
- **New modules:** `src/agile_cache.py` (Agile rate cache for tariff tools), `src/presets.py` (`OperationPreset`), `src/config_snapshots.py` (snapshots without V7 consent).
- **Planner (heuristic mode):** cheap band from price quantiles + forecast solar skip. Heartbeat adds **MIN_SOC_RESERVE_PERCENT** vs peak price warning alongside the existing low-SoC alert.
