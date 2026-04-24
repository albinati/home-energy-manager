---
name: home-energy-manager
description: OpenClaw skill — interface to the Home Energy Manager. The app owns ALL hardware logic, schedules, quotas, and consent. OpenClaw reads status, proposes plans, and requests changes. It never writes directly to Daikin or Fox ESS.
metadata: {"openclaw": {"requires": {"env": ["HOME_ENERGY_API_URL"]}, "primaryEnv": "HOME_ENERGY_API_URL", "emoji": "🏠"}}
---

# Home Energy Manager

The app is the **single planning brain** for the site. It fetches Octopus Agile tariffs, runs a PuLP MILP optimizer, uploads a Fox ESS Scheduler V3, and executes a 2-minute heartbeat. **In v10 (default) the Daikin runs autonomously on its own firmware curve** — the app no longer writes to it; instead it predicts the Daikin's electrical draw and treats it as a fixed thermal load when planning Fox/grid/PV.

**OpenClaw is a read/propose/request interface only.** All hardware writes are enforced server-side through plan consent, daily API quota, rate-limit, read-only, and `DAIKIN_CONTROL_MODE` gates. You cannot bypass them.

**MCP is the only sanctioned channel.** Do not edit `.env`, `src/`, or any file on the app host; do not run shell commands against it; do not make direct HTTP calls to Daikin Onecta or Fox ESS cloud. If a new capability is needed, request a new MCP tool. See `docs/OPENCLAW_BOUNDARY.md` for the full sanctioned surface and out-of-bounds list.

For safe what-if exploration (e.g. "what would tomorrow's plan look like with guests over?"), use `simulate_plan` — read-only, quota-free, zero hardware impact.

Automated user notifications (plans, alerts, briefs) are sent only through the **OpenClaw Gateway** `POST /hooks/agent` path — not via a separate `openclaw` CLI on the app host. Configure `OPENCLAW_HOOKS_URL` and `OPENCLAW_HOOKS_TOKEN` on the server (see `docs/RUNBOOK.md`).

**Base URL**: `HOME_ENERGY_API_URL` (e.g. `http://192.168.1.100:8000`)

---

## Reading status

Use MCP tools first — they serve from cache and never burn API quota unnecessarily.

### Live state (realtime / today)

| Tool | What it returns |
|------|----------------|
| `get_daikin_status` | `is_on`, `mode`, `room_temp`, `outdoor_temp`, `lwt`, `lwt_offset`, `tank_temp`, `tank_target`, `weather_regulation`, `control_mode` (v10: `passive`/`active`) |
| `get_soc` | Fox ESS battery `soc` %, solar power, grid power, work mode |
| `get_cockpit_now` | **One-call aggregator** — current slot + price, SoC/solar/load/grid, Daikin temps, Fox mode, next transition, per-source freshness. Prefer this for "where are we right now" questions. |
| `get_system_timezone` | Planner tz (`Europe/London`) + UTC plan-push tz + current `now_utc`/`now_local`. Always use this to interpret the ISO timestamps coming back from every other tool. |
| `get_schedule` | Today's action schedule (Daikin rows + Fox V3 snapshot) |
| `get_optimization_status` | Preset, optimizer backend, consent state, cooldown |
| `get_optimization_plan` | Full 48-slot plan with Fox + Daikin actions |
| `get_optimization_inputs` | **What the next LP solve will see** — prices + weather (half-hour interpolated), base-load profile, initial state with per-field source, thresholds, config snapshot. Useful before calling `propose_optimization_plan` to predict the outcome. |
| `get_daily_brief` | Morning report on demand |
| `get_battery_forecast` | SoC + daily target snapshot |
| `get_weather_context` | 48h forecast + live Daikin temps |
| `get_energy_metrics` | Daily/weekly/monthly PnL, VWAP, slippage, SoC |

### Historical navigation (hours / days / weeks / months)

Use these to compare periods, investigate a past decision, or build context before making a recommendation.

| Tool | What it returns |
|------|----------------|
| `get_cockpit_at(when)` | **Historical replay** — the cockpit frozen at an ISO-UTC moment. Joins `lp_solution_snapshot` + `execution_log` + `agile_rates` so you see both what the LP decided AND what actually happened. E.g. `get_cockpit_at("2026-04-23T18:00:00Z")`. |
| `find_lp_run_for_time(when_utc)` | Which LP run was active at a given moment. Returns `run_id` → pair with `get_lp_solution(run_id)`. |
| `get_lp_solution(run_id)` | Per-slot decision vector (import/export/charge/discharge/dhw/space kWh, SoC trajectory, tank+indoor+outdoor temps) + inputs for a specific solve. |
| `get_meteo_forecast_history(fetch_at_utc)` | How a forecast evolved across LP runs — every slot's value as it was when each fetch happened (not just the latest). |
| `get_fox_energy_range(start, end)` | Daily Fox totals across a date range: solar / load / import / export / charge / discharge. Use for weekly/monthly trend comparisons. |
| `get_attribution_day(date)` | Solar attribution donut: what % of solar went to self-use / battery / export on a given day (defaults to yesterday). |
| `get_daikin_telemetry_history(limit)` | Recent Daikin readings tagged `live` (from Onecta) vs `estimate` (physics fallback). Useful for calibration + estimator sanity. |
| `get_action_log` | Full audit trail of executed hardware actions. |
| `get_optimizer_log` | Optimizer run history (per-run summary — pair with `get_lp_solution` for drill-down). |
| `get_config_audit(key?)` | Runtime-settings change log — explains why a past plan looked the way it did if a knob has moved since. |
| `get_recent_triggers(limit?)` | **What's just fired** — the cockpit's "Recent" strip as JSON. Includes `actor`, `started_at`, `duration_ms`, `result`. Filters out heartbeat + notification noise by default. |

---

## The standard daily workflow

The system runs autonomously — you mostly observe and occasionally intervene.

```
Daily (automatic, ~16:05 local; nightly push at LP_PLAN_PUSH_HOUR UTC):
  Octopus fetch → simulate (LP solve) → auto-approve (if PLAN_AUTO_APPROVE=true, default)
                                    → Fox V3 uploaded → Daikin rows written
                                    → notification: "[AUTO-APPLIED] …"

  If PLAN_AUTO_APPROVE=false:
  Octopus fetch → simulate → PLAN_PROPOSED hook (Telegram/Discord accept/reject buttons,
                                                  auto-accepts on timeout → applied)

To check what's happening:
  get_optimization_status   → preset, backend, last plan time, consent state
  get_schedule              → today's Daikin + Fox actions and their status
  get_soc                   → live battery and solar

To force a fresh simulate → apply cycle:
  propose_optimization_plan → optimizer runs in background, returns plan_id immediately.
                              Honors PLAN_AUTO_APPROVE.
  confirm_plan(plan_id)     → approve a pending plan (no-op if already auto-approved)
  reject_plan(plan_id)      → reject and clear the pending plan

For pure what-if (no DB write, no hardware, no quota):
  simulate_plan             → solves the LP with optional overrides, returns the plan
```

---

## Plan lifecycle: simulate → approve → live

`OPERATION_MODE` is retired. Every plan is simulated (the LP solve is itself the
pre-check), then approved, then applied. `PLAN_AUTO_APPROVE` decides whether
approval is implicit or explicit.

```
propose_optimization_plan()
    ↓
LP simulate (read-only solve)
    ↓
PLAN_AUTO_APPROVE=true (default)        PLAN_AUTO_APPROVE=false
    ↓                                   ↓
auto-approve + write                    PLAN_PROPOSED hook → user
    ↓                                   (Telegram/Discord accept/reject)
Fox V3 uploaded                         ↓
Daikin action_schedule written          confirm_plan()  → write + apply
    ↓                                   reject_plan()   → discard
"[AUTO-APPLIED] …" notification         timeout         → auto-accept + apply
```

The `PLAN_PROPOSED` hook payload carries `autoAcceptOnTimeout: true` and
`approvalTimeoutSeconds` (default 300 s). Clients rendering interactive buttons
**must** treat the button timeout as "approve" — silence should never veto.

**Things to remember:**
- When `PLAN_AUTO_APPROVE=true`, `propose_optimization_plan` already wrote the plan
  by the time the hook arrives. `confirm_plan` in that case is a no-op acknowledgement.
- When `PLAN_AUTO_APPROVE=false`, hardware is **not** touched until the user (or the
  timeout) approves. `reject_plan` cleanly discards the pending plan.
- Daikin `action_schedule` rows execute as soon as they are written. If you manually
  override Daikin mid-slot, the next scheduled `restore` action reverts it.
- Cooldown: `PLAN_REGEN_COOLDOWN_SECONDS` (default 300 s) — re-proposing within
  5 minutes with identical plan content is silently suppressed.
- Kill switch: `OPENCLAW_READ_ONLY=true` blocks every Fox/Daikin write at the gate,
  regardless of approval status. Use it for dev boxes and panic stops; never for
  "I want manual control" — use `reject_plan` + manual MCP writes for that.

---

## v10: Daikin control mode

Read `get_daikin_status.control_mode` before any Daikin write attempt:

| Mode | Behaviour |
|------|-----------|
| `passive` (default) | App **never** writes to Daikin. Firmware autonomous (its own weather-compensation curve, autonomous legionella cycle on Sundays ~11:00 local). All `set_daikin_*` MCP tools and `/api/v1/daikin/*` POSTs return an error. To make any Daikin change you must first flip the mode. |
| `active` | Legacy v9 control: app schedules Daikin actions (lwt offsets, tank setpoints, powerful mode, max-heat windows) per the LP plan. |

To flip modes: ask the user to confirm explicitly, then `PUT /api/v1/settings/DAIKIN_CONTROL_MODE {"value":"active"}` (or `"passive"`). **Never flip silently** — it changes who controls the heat pump.

When passive: tell the user that Daikin will NOT respond to any heat-pump-related request; all you can do is observe + report. Suggest a settings flip if they really need control.

---

## Manual hardware changes

Manual MCP writes (`set_inverter_mode`, `set_daikin_tank_temperature`, `propose_optimization_plan`) are timed end-to-end and persisted to `action_log` with `started_at`, `completed_at`, `duration_ms`, `actor="mcp"`. The cockpit's "Recent" strip + `get_recent_triggers` surface them within ~1 s of completion, including failure pills with the error message in the tooltip — so after a write, you can confirm it landed by calling `get_recent_triggers(limit=3)`.

### Before writing anything

Always check `get_daikin_status` first.
- If `control_mode: "passive"` → no `set_daikin_*` tool will work; report this and ask if the user wants to switch to `active`.
- If `weather_regulation: true` → you **cannot** set room temperature; use `set_daikin_lwt_offset` instead.

### The consent gate

If any Daikin write returns `{"ok": false, "requires_confirmation": true}`, a plan is pending approval. Workflow:

```
1. get_pending_approval()       → show the pending plan to the user
2. Ask: approve and let it run, or reject to make manual changes?
3a. confirm_plan(plan_id)       → plan approved, Daikin follows schedule
3b. reject_plan(plan_id)        → plan cleared, manual changes unblocked
    then: set_daikin_lwt_offset(offset, confirmed=True)
```

Never pass `confirmed=True` silently — only after the user has explicitly acknowledged they are overriding the pending plan.

### Daikin write tools

| Tool | Parameters | Notes |
|------|-----------|-------|
| `set_daikin_power(on, confirmed?)` | `on: bool` | Climate on/off |
| `set_daikin_lwt_offset(offset, confirmed?)` | `-10 to +10` | Use this when weather regulation is active |
| `set_daikin_temperature(temperature, confirmed?)` | `15–30°C` | Blocked when weather regulation is on |
| `set_daikin_mode(mode, confirmed?)` | `heating\|cooling\|auto\|fan_only\|dry` | |
| `set_daikin_tank_temperature(temperature, confirmed?)` | `30–65°C` | DHW setpoint |
| `set_daikin_tank_power(on, confirmed?)` | `on: bool` | DHW on/off |

### Fox ESS write tool

| Tool | Parameters | Notes |
|------|-----------|-------|
| `set_inverter_mode(mode)` | `Self Use\|Feed-in Priority\|Back Up\|Force charge\|Force discharge` | Emergency override only — next optimizer run will overwrite |

---

## Optimization settings

| Tool | Use |
|------|-----|
| `set_optimization_preset(preset)` | `normal` / `guests` / `travel` / `away` (v10: `boost` retired — silently aliased to `normal`) |
| `set_optimizer_backend(backend)` | `lp` (PuLP MILP, default) / `heuristic` (legacy) |
| `set_auto_approve(enabled)` | `true` (default) = plans simulate then auto-apply; `false` = wait for explicit consent |
| `rollback_config(snapshot_id?)` | Restore a saved config snapshot (preset, thresholds, targets) |

**Presets:**

| Preset | Behaviour |
|--------|-----------|
| `normal` | Standard comfort, optimise cost |
| `guests` | Higher DHW (48°C+), warmer rooms |
| `travel` / `away` | Frost protection only, max battery export during peak |
| ~~`boost`~~ | Retired in v10. Accepted with a deprecation log; silently aliased to `normal`. |

**When to suggest turning `auto_approve` OFF:** if the user wants to review every plan before it goes live (e.g. after a system change, during tariff experiments, or on the first few days of a new hardware setup). The default is ON.

---

## Notifications

| Tool | Use |
|------|-----|
| `list_notification_routes` | See current alert routing config |
| `set_notification_route(alert_type, ...)` | Mute, reroute, or change severity of an alert type |
| `test_notification(alert_type)` | Queue a test hook delivery (`OPENCLAW_HOOKS_*` must be set) |

Alert types: `morning_report`, `strategy_update`, `risk_alert`, `action_confirmation`, `critical_error`, `plan_proposed`, `cheap_window_start`, `peak_window_start`, `daily_pnl`.

### When each alert fires

- `plan_proposed` — new LP plan produced (either auto-applied or awaiting approval). Includes `plan_id` for follow-up `confirm_plan` / `reject_plan`.
- `action_confirmation` — **fires on plan approve + reject** (both the canonical `confirm_plan`/`reject_plan` and the aliased `approve_optimization_plan`/`reject_optimization_plan`). Use this to know a user decision landed.
- `risk_alert` — safety issues: Daikin auth circuit tripped (refresh_token likely dead; run `python -m src.daikin.auth` to re-auth), low SoC warnings, quota exhaustion.
- `cheap_window_start` / `peak_window_start` — LP classified the next slot as cheap or peak; fires once at slot start.
- `morning_report` — nightly summary on the daily brief cron.
- `daily_pnl` — end-of-day PnL once execution_log is complete.
- `strategy_update` — optimizer changed its mind about a slot kind after a re-solve.
- `critical_error` — unrecoverable: config corrupt, DB locked, migration failure.

---

## Energy reports and tariff comparison

```
get_energy_metrics              → daily PnL, VWAP, slippage vs SVT/fixed shadow
compare_tariffs(months_back)    → ranked tariff simulation (best → worst)
get_tariff_recommendation()     → one-line recommendation
```

Suggest a tariff comparison when: user asks "am I on the best tariff?", after major usage changes (new heat pump, solar, EV), or quarterly.

---

## Critical rules

1. **Read before write.** Always call `get_daikin_status` or `get_soc` before making changes.
2. **Check `control_mode` first.** If `passive`, do not attempt any `set_daikin_*` write — report and ask.
3. **Never bypass plan consent.** On `requires_confirmation: true` → show `get_pending_approval()`, ask the user.
4. **Weather regulation**: `weather_regulation: true` → use `set_daikin_lwt_offset`, not temperature.
5. **Never flip `DAIKIN_CONTROL_MODE` silently.** It changes whether the firmware or the app drives the heat pump.
6. **Do not poll status in loops.** The app caches Daikin (30 min) and Fox (5 min). One call is enough.
7. **Fox V3 is uploaded once per optimizer run.** Do not spam `set_inverter_mode` — next plan run will overwrite it.
8. **Daikin quota: 180 req/day** (hard cap enforced by the app). If you see `stale: true` in a status response, the quota is exhausted — do not retry until next day.
9. **`OPENCLAW_READ_ONLY=true` blocks every hardware write.** If writes are silently skipped, check this first. Do not try to "bypass" it — it's the intended kill switch.

## Error reference

| Response | Meaning | Action |
|----------|---------|--------|
| `requires_confirmation: true` | Plan pending consent gate | Show `get_pending_approval()`, ask user |
| `passive_mode: true, ok: false` | v10: `DAIKIN_CONTROL_MODE=passive`, all Daikin writes blocked | Report; ask user if they want to switch to `active` |
| `ok: false, stale: true` | Daikin quota exhausted | Report to user; do not retry |
| HTTP `403` | `OPENCLAW_READ_ONLY=true` — writes disabled at the gate | Only recommend; report the gate, do not attempt to bypass |
| HTTP `409` `PassiveModeLocked` | v10: Daikin write blocked at API layer | Same as `passive_mode: true` — ask user to flip mode |
| HTTP `409` `SimulationIdRequired` | Only fires when `REQUIRE_SIMULATION_ID=true` is set (HTTP callers). Call the paired `/simulate` endpoint first, then send the returned `X-Simulation-Id`. MCP tools are exempt — use the MCP equivalent if you hit this. |
| HTTP `409` (other) | Blocked (e.g. weather regulation) | Use `set_daikin_lwt_offset` |
| HTTP `429` | Rate limited | Wait 5 s; if "daily limit" mentioned, wait until tomorrow |
| HTTP `502` | Device cloud error | Log and retry later |
| HTTP `503` | Service not configured | Check credentials |

---

## Web cockpit (simulate-before-apply on the HTTP side)

The web cockpit at `/` funnels every settings/plan write through a preview → modal
→ confirm flow. Each endpoint has a paired `/simulate` route that returns an
`ActionDiff`; the modal shows it; confirmation applies. The `REQUIRE_SIMULATION_ID`
flag (default `false`) can be flipped to enforce this on all HTTP callers, third-
party scripts included.

**MCP tools are exempt** from `REQUIRE_SIMULATION_ID`. Hardware-write MCP tools
still require their own `confirmed=True` flag, and the plan pipeline still runs
the simulate-approve-apply lifecycle described above. The simulate step there is
the LP solve itself; the approve step is `PLAN_AUTO_APPROVE` or an explicit
`confirm_plan` / timeout-accepted hook response.
