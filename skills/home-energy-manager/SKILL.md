---
name: home-energy-manager
description: OpenClaw skill — interface to the Home Energy Manager. The app owns ALL hardware logic, schedules, quotas, and consent. OpenClaw reads status, proposes plans, and requests changes. It never writes directly to Daikin or Fox ESS.
metadata: {"openclaw": {"requires": {"env": ["HOME_ENERGY_API_URL"]}, "primaryEnv": "HOME_ENERGY_API_URL", "emoji": "🏠"}}
---

# Home Energy Manager

The app is the **single planning brain** for the site. It fetches Octopus Agile tariffs, runs a PuLP MILP optimizer, uploads a Fox ESS Scheduler V3, writes Daikin action rows, and executes them on a 2-minute heartbeat.

**OpenClaw is a read/propose/request interface only.** All hardware writes are enforced server-side through plan consent, daily API quota, rate-limit, and read-only gates. You cannot bypass them.

**MCP is the only sanctioned channel.** Do not edit `.env`, `src/`, or any file on the app host; do not run shell commands against it; do not make direct HTTP calls to Daikin Onecta or Fox ESS cloud. If a new capability is needed, request a new MCP tool. See `docs/OPENCLAW_BOUNDARY.md` for the full sanctioned surface and out-of-bounds list.

For safe what-if exploration (e.g. "what would tomorrow's plan look like with guests over?"), use `simulate_plan` — read-only, quota-free, zero hardware impact.

Automated user notifications (plans, alerts, briefs) are sent only through the **OpenClaw Gateway** `POST /hooks/agent` path — not via a separate `openclaw` CLI on the app host. Configure `OPENCLAW_HOOKS_URL` and `OPENCLAW_HOOKS_TOKEN` on the server (see `docs/RUNBOOK.md`).

**Base URL**: `HOME_ENERGY_API_URL` (e.g. `http://192.168.1.100:8000`)

---

## Reading status

Use MCP tools first — they serve from cache and never burn API quota unnecessarily.

| Tool | What it returns |
|------|----------------|
| `get_daikin_status` | `is_on`, `mode`, `room_temp`, `outdoor_temp`, `lwt`, `lwt_offset`, `tank_temp`, `tank_target`, `weather_regulation` |
| `get_soc` | Fox ESS battery `soc` %, solar power, grid power, work mode |
| `get_schedule` | Today's action schedule (Daikin rows + Fox V3 snapshot) |
| `get_optimization_status` | Operation mode, preset, optimizer backend, consent state, cooldown |
| `get_optimization_plan` | Full 48-slot plan with Fox + Daikin actions |
| `get_energy_metrics` | Daily/weekly/monthly PnL, VWAP, slippage, SoC |
| `get_daily_brief` | Morning report on demand |
| `get_battery_forecast` | SoC + daily target snapshot |
| `get_weather_context` | 48h forecast + live Daikin temps |
| `get_action_log` | Audit trail of executed hardware actions |
| `get_optimizer_log` | Optimizer run history |

---

## The standard daily workflow

The system runs autonomously — you mostly observe and occasionally intervene.

```
Daily (automatic, ~16:05):
  Octopus fetch → optimizer runs → Fox V3 uploaded → Daikin rows written
  → notification sent to user with plan summary
  → user calls confirm_plan(plan_id) to approve
  → heartbeat executes Daikin actions; Fox follows V3 schedule

To check what's happening:
  get_optimization_status   → mode, preset, last plan time
  get_schedule              → today's Daikin + Fox actions and their status
  get_soc                   → live battery and solar

To re-plan manually:
  propose_optimization_plan → optimizer runs in background, returns plan_id immediately
                              (notification arrives shortly via OpenClaw Gateway hook → your channel)
  confirm_plan(plan_id)     → activate it
```

---

## Plan consent

Every plan goes through a consent lifecycle:

```
propose_optimization_plan()
    ↓ returns {plan_id, status: "applied"} immediately
optimizer runs in background thread
    ↓
plan stored → notification sent via OpenClaw Gateway hook with full schedule
    ↓
confirm_plan(plan_id)     → Fox V3 uploaded + Daikin rows activated
reject_plan(plan_id)      → plan discarded, system holds last state
    ↓ (if no response before expires_at)
auto-approved with "[auto-approved after Xm]" notification
```

**Key facts learned from going live:**
- `propose` returns `status: "applied"` immediately — the plan **is already written to the DB** and Fox V3 is uploaded on the same call. `confirm_plan` is for user acknowledgement, not for triggering hardware.
- Daikin actions in `action_schedule` start executing as soon as they are written, regardless of consent status. The consent gate only blocks **new** MCP hardware writes from OpenClaw.
- If you manually change Daikin (e.g. tank to 60°C), the next scheduled `restore` action will revert it automatically — no manual cleanup needed.
- Cooldown: `PLAN_REGEN_COOLDOWN_SECONDS` (default 300 s) — re-proposing within 5 minutes is silently rejected if the plan content hasn't changed.

---

## Manual hardware changes

### Before writing anything

Always check `get_daikin_status` first. If `weather_regulation: true`, you **cannot** set room temperature — use `set_daikin_lwt_offset` instead.

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
| `set_optimization_preset(preset)` | `normal` / `guests` / `travel` / `away` / `boost` |
| `set_operation_mode(mode)` | `simulation` (no hardware writes) / `operational` |
| `set_optimizer_backend(backend)` | `lp` (PuLP MILP, default) / `heuristic` (legacy) |
| `set_auto_approve(enabled)` | `true` = plans auto-apply with `[AUTO-APPLIED]` notification |
| `rollback_config(snapshot_id?)` | Restore last snapshot + force simulation mode |

**Presets:**

| Preset | Behaviour |
|--------|-----------|
| `normal` | Standard comfort, optimise cost |
| `guests` | Higher DHW (48°C+), warmer rooms |
| `travel` / `away` | Frost protection only, max battery export during peak |
| `boost` | Full comfort, ignores price — use sparingly |

**When to suggest `auto_approve`:** only after the user has been running in operational mode for several days and is happy with how plans look. Never suggest it on the first day or after a rollback.

---

## Notifications

| Tool | Use |
|------|-----|
| `list_notification_routes` | See current alert routing config |
| `set_notification_route(alert_type, ...)` | Mute, reroute, or change severity of an alert type |
| `test_notification(alert_type)` | Queue a test hook delivery (`OPENCLAW_HOOKS_*` must be set) |

Alert types: `morning_report`, `strategy_update`, `risk_alert`, `action_confirmation`, `critical_error`, `plan_proposed`, `cheap_window_start`, `peak_window_start`, `daily_pnl`.

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
2. **Never bypass plan consent.** On `requires_confirmation: true` → show `get_pending_approval()`, ask the user.
3. **Weather regulation**: `weather_regulation: true` → use `set_daikin_lwt_offset`, not temperature.
4. **Never switch to operational mode without explicit user consent.** Explain what hardware will change.
5. **Do not poll status in loops.** The app caches Daikin (30 min) and Fox (5 min). One call is enough.
6. **Fox V3 is uploaded once per optimizer run.** Do not spam `set_inverter_mode` — next plan run will overwrite it.
7. **Daikin quota: 180 req/day** (hard cap enforced by the app). If you see `stale: true` in a status response, the quota is exhausted — do not retry until next day.

## Error reference

| Response | Meaning | Action |
|----------|---------|--------|
| `requires_confirmation: true` | Plan pending consent gate | Show `get_pending_approval()`, ask user |
| `ok: false, stale: true` | Daikin quota exhausted | Report to user; do not retry |
| HTTP `403` | Read-only mode active | Only recommend; do not execute |
| HTTP `409` | Blocked (weather regulation) | Use `set_daikin_lwt_offset` |
| HTTP `429` | Rate limited | Wait 5 s; if "daily limit" mentioned, wait until tomorrow |
| HTTP `502` | Device cloud error | Log and retry later |
| HTTP `503` | Service not configured | Check credentials |
