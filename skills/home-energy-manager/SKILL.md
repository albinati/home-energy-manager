---
name: home-energy-manager
description: OpenClaw skill — talk to the Home Energy Manager standalone app via MCP tools. The app owns ALL hardware logic, schedules, and consent gates. OpenClaw NEVER writes directly to Daikin or Fox ESS — it asks the app, which decides whether to proceed.
metadata: {"openclaw": {"requires": {"env": ["HOME_ENERGY_API_URL"]}, "primaryEnv": "HOME_ENERGY_API_URL", "emoji": "🏠"}}
---

# Home Energy Manager (OpenClaw ↔ app interface)

**Home Energy Manager** is a **standalone service** and the **single source of truth** for the site: it stores Agile tariffs, optimises Fox + heat-pump schedules, runs a heartbeat to apply them, and enforces all consent, quota, and safety logic.

**OpenClaw is a remote interface only.** It reads status and reports, proposes plans, and requests hardware changes — but ALL writes go through app-level gates (plan consent, rate limits, daily API quotas, `confirmed=True` flags). OpenClaw must **never bypass** these gates by hitting hardware APIs directly.

**Base URL**: Set `HOME_ENERGY_API_URL` to the running app (e.g. `http://192.168.1.100:8000`).

## How to discover available actions

Before doing anything, fetch the capabilities list:

```
GET {HOME_ENERGY_API_URL}/api/v1/openclaw/capabilities
```

Returns every action, its parameters, validation ranges, and whether it requires confirmation.

## How to read status

**Daikin heat pump status:**
```
GET {HOME_ENERGY_API_URL}/api/v1/daikin/status
```
Returns: `is_on`, `mode`, `room_temp`, `target_temp`, `outdoor_temp`, `lwt`, `lwt_offset`, `tank_temp`, `tank_target`, `weather_regulation`.

**Fox ESS battery status:**
```
GET {HOME_ENERGY_API_URL}/api/v1/foxess/status
```
Returns: `soc` (battery %), `solar_power`, `grid_power`, `battery_power`, `load_power`, `work_mode`.

## Data report (energy, cost, charts)

```
GET {HOME_ENERGY_API_URL}/api/v1/energy/report
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=month&month=YYYY-MM
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=year&year=YYYY
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=day&date=YYYY-MM-DD
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=week&date=YYYY-MM-DD
```

| Field | Description |
|-------|-------------|
| `period` | `"day"` \| `"week"` \| `"month"` \| `"year"` |
| `period_label` | Human label, e.g. `"Feb 2026"` |
| `energy` | `import_kwh`, `export_kwh`, `solar_kwh`, `load_kwh`, `charge_kwh`, `discharge_kwh` |
| `cost` | `net_cost_pounds`, `import_cost_pounds`, `export_earnings_pounds`, etc. |
| `heating_estimate_kwh` | Estimated heating consumption |
| `equivalent_gas_cost_pounds` | Same period on gas |
| `gas_comparison_ahead_pounds` | Positive = ahead vs gas |
| `chart_data` | Per-day array for charts |
| `heating_analytics` | `heating_percent_of_cost`, `degree_days`, `temp_bands`, etc. |
| `summary` | Short narrative for TTS/chat — use this to speak the report |

Use `summary` for voice; structured fields for exact numbers and charts.

## How to execute actions

> **IMPORTANT — MCP-first, not raw API.**
> All hardware changes MUST go through the MCP tools below (or `/api/v1/openclaw/execute` for HTTP mode). **Never invent raw `PATCH` calls to Daikin or Fox ESS cloud APIs.** The app owns the write path and enforces safety gates OpenClaw cannot replicate.

### The hardware write pipeline (enforced by the app)

```
OpenClaw request
      │
      ▼
plan_consent gate ──── pending_approval? ──── YES → block (requires_confirmation: true)
      │                                                       ↓
      │ NO (approved / no plan)          show user get_pending_approval(), ask approve or reject
      ▼
rate-limit check (5 s cooldown per action type)
      │
      ▼
daily quota check (Daikin ≤ 180 req/day enforced)
      │
      ▼
OPENCLAW_READ_ONLY gate
      │
      ▼
hardware write → cache invalidation + quota log
```

Every step is enforced server-side. OpenClaw cannot skip any gate.

### When a plan is pending: show the user first

If any Daikin write returns `{"ok": false, "requires_confirmation": true}`:

```
1. get_pending_approval()             → show the pending plan summary to the user
2. Ask: "Approve and let it run, or reject to make manual changes?"
3a. approve_optimization_plan(plan_id)  → plan active, hardware changes on next heartbeat
3b. reject_optimization_plan(plan_id)   → plan cleared, manual changes unblocked
     then: set_daikin_lwt_offset(offset, confirmed=True)  ← only AFTER rejection
```

**Never pass `confirmed=True` silently.** Only use it after the user has explicitly acknowledged they are overriding a pending plan.

### Daikin manual writes (MCP tools)

All Daikin write tools accept `confirmed` (default `False`). Plan consent gate is enforced server-side.

| Tool | Parameters | Notes |
|------|-----------|-------|
| `set_daikin_power(on, confirmed?)` | `on: bool` | Climate on/off. Blocked by pending plan unless `confirmed=True`. |
| `set_daikin_temperature(temperature, mode?, confirmed?)` | `15–30°C` | **BLOCKED when weather regulation is active** — use `set_daikin_lwt_offset` instead. |
| `set_daikin_lwt_offset(offset, mode?, confirmed?)` | `-10 to +10` | Primary control when weather regulation is on. |
| `set_daikin_mode(mode, confirmed?)` | `heating \| cooling \| auto \| fan_only \| dry` | Operation mode. |
| `set_daikin_tank_temperature(temperature, confirmed?)` | `30–65°C` | DHW tank setpoint. |
| `set_daikin_tank_power(on, confirmed?)` | `on: bool` | DHW tank on/off. |

**Response when plan is pending and `confirmed=False`:**
```json
{"ok": false, "requires_confirmation": true,
 "warning": "WARNING: plan lp-2026-04-19 is pending your approval. Pass confirmed=True to override..."}
```

### Fox ESS manual write (MCP tool)

| Tool | Parameters | Notes |
|------|-----------|-------|
| `set_inverter_mode(mode)` | `Self Use \| Feed-in Priority \| Back Up \| Force charge \| Force discharge` | Overrides Fox Scheduler V3 immediately. Use for emergencies only. |

### HTTP fallback (if not using MCP)

```
POST {HOME_ENERGY_API_URL}/api/v1/openclaw/execute
Content-Type: application/json

{"action": "<action_name>", "parameters": {<params>}}
```

Actions requiring a 2-step confirmation return a `confirmation_token`. Re-send with the token to confirm. Tokens expire after 5 minutes. See `GET /api/v1/openclaw/capabilities` for the full action list.

**When the API returns 403** (recommendation-only mode): only suggest actions; tell the user to apply via dashboard or CLI. Do not retry.

## Critical rules (always follow)

1. **Read before write.** Always check status (`get_daikin_status`, `get_soc`) before making any change.
2. **Never bypass plan consent.** If a tool returns `requires_confirmation: true`, stop and show `get_pending_approval()`. Do NOT silently pass `confirmed=True`.
3. **Weather regulation**: When Daikin `weather_regulation` is `true`, CANNOT set room temperature — use `set_daikin_lwt_offset` instead.
4. **Daikin cloud quota**: 200 req/day hard limit. The app caches device state for 30 minutes. Do NOT poll in loops.
5. **Fox ESS quota**: 1440 req/day. The app's cached realtime path handles this.
6. **Rate limiting**: App enforces 5-second cooldown between writes of the same type. On 429 from the app, wait 5 s and retry once.
7. **OPENCLAW_READ_ONLY=true** (default): write tools return an error. User must enable writes explicitly.
8. **Never suggest switching to operational mode without explicit user consent.**

## Error reference

| Code | Meaning | Action |
|------|---------|--------|
| `requires_confirmation: true` | Plan pending — consent gate blocked | Show `get_pending_approval()`, ask user to confirm or reject |
| `400` | Invalid params (range, mode) | Fix params |
| `403` | OPENCLAW_READ_ONLY mode | Only recommend; user applies via dashboard |
| `404` | Device not found | Check `get_daikin_status` |
| `409` | Blocked (e.g. weather regulation on) | Use `set_daikin_lwt_offset` instead |
| `410` | Confirmation token expired | Restart the 2-step flow |
| `429` | Rate limited | Wait 5 s; if Daikin quota mentioned, wait until next day |
| `502` | Device cloud API error | Log and retry later |
| `503` | Service/credentials not configured | Check `.env` |

---

## Optimization Engine (Bulletproof)

The system runs a **PuLP MILP optimizer** (`USE_BULLETPROOF_ENGINE=true`) managing Fox ESS battery and Daikin ASHP around Octopus Agile half-hourly prices. All automation is **consent-driven**:

1. Plan is **proposed** (non-blocking — returns immediately; optimizer runs in background)
2. User is **notified** via OpenClaw with full schedule summary
3. User **approves** (or plan auto-approves after consent timeout)
4. Hardware writes happen on the next 30-min heartbeat tick

### Plan consent lifecycle

```
propose_optimization_plan()
    │  (returns immediately — optimizer runs in background thread)
    ▼
plan computed → stored in plan_consent table (status: pending_approval)
    │
    ▼
notification sent to user (plan summary + 48-slot schedule)
    │
    ├─ user: approve_optimization_plan(plan_id)
    │      → Fox Scheduler V3 uploaded, Daikin schedule activated
    │
    ├─ user: reject_optimization_plan(plan_id)
    │      → plan discarded, Daikin holds last state
    │
    └─ no response before expires_at
           → auto-approved with "[auto-approved after Xm timeout]" notification
```

**Idempotency**: If the same plan (same content hash) is re-proposed while pending, no duplicate notification is sent.
**Cooldown**: `PLAN_REGEN_COOLDOWN_SECONDS` (default 300 s) prevents rapid re-planning spam.
**Auto-approve**: When `PLAN_AUTO_APPROVE=true`, every plan is immediately approved and a `[AUTO-APPLIED]` notification is sent.

### Operation Modes

| Mode | Behaviour |
|------|-----------|
| `simulation` | Default. Computes plans, logs what it WOULD do, sends notifications. No hardware writes. |
| `operational` | Writes to Fox ESS and Daikin on each 30-min heartbeat using the approved plan. |

**Always confirm with the user before switching to operational mode.**

### Household Presets

| Preset | Behaviour |
|--------|-----------|
| `normal` | Standard comfort, optimise cost within bounds |
| `guests` | Higher DHW (48°C+), warmer rooms, less aggressive cost-cutting |
| `travel` / `away` | Frost protection only, max battery export during peak, DHW off except Legionella |
| `boost` | Temporary full-comfort override, ignores price |

### OpenClaw Optimization Workflow

**Standard flow:**
```
1. get_optimization_status           → check mode, preset, consent state, cooldown remaining
2. propose_optimization_plan         → triggers optimizer in background; returns plan_id immediately
3. [wait for notification, or call get_optimization_plan to see result]
4. [show plan summary to user]
5. approve_optimization_plan(plan_id) → ONLY AFTER user confirms
6. [plan activates on next 30-min heartbeat]
```

**Changing settings:**
```
set_optimization_preset(preset)      → normal / guests / travel / away / boost
set_optimizer_backend(backend)       → lp (PuLP MILP) | heuristic (legacy)
set_operation_mode(mode)             → simulation / operational
```

**Emergency rollback:**
```
rollback_config()                    → restore latest snapshot, force simulation mode
get_config_snapshots()               → list available snapshots
```

### MCP Tools Reference

| Tool | Description |
|------|-------------|
| `get_optimization_status` | Full status: mode, preset, optimizer backend, consent state, cooldown remaining |
| `get_optimization_plan` | 48-slot plan with per-slot prices, Fox/Daikin actions, weather notes |
| `propose_optimization_plan` | Trigger optimizer (non-blocking) — returns plan_id immediately |
| `approve_optimization_plan(plan_id)` | Approve pending plan — ONLY call after showing user the summary |
| `reject_optimization_plan(plan_id)` | Reject pending plan |
| `get_pending_approval` | Show the current pending plan consent record |
| `set_optimization_preset(preset)` | Switch household preset |
| `set_optimizer_backend(backend)` | `lp` (PuLP) or `heuristic` (legacy) |
| `set_operation_mode(mode)` | Switch simulation / operational |
| `rollback_config(snapshot_id?)` | Restore config snapshot |
| `get_config_snapshots` | List available snapshots |
| `set_auto_approve(enabled)` | Enable/disable automatic plan approval |
| `get_energy_metrics` | Daily/weekly/monthly PnL vs SVT/fixed shadow, VWAP, slippage, SLA, SoC |
| `get_schedule` | Today's SQLite actions + last Fox V3 snapshot |
| `get_daily_brief` | Morning report on demand (same as 08:00 notification) |
| `get_battery_forecast` | SoC + `daily_targets` snapshot |
| `get_weather_context` | 48h forecast + live Daikin temps |
| `get_action_log` / `get_optimizer_log` | Audit trail |
| `override_schedule` | Manual heating boost (**requires `OPENCLAW_READ_ONLY=false`**) |
| `acknowledge_warning` | Dismiss repeating risk alerts (e.g. low SoC + peak price) |
| `get_daikin_status` | Live Daikin device state (from cache; no cloud call unless stale) |
| `get_soc` | Current Fox ESS battery state of charge |

### REST API Endpoints (mirrors MCP tools)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/optimization/status` | GET | Extended status with mode and consent |
| `/api/v1/optimization/plan` | GET | 48-slot solver plan |
| `/api/v1/optimization/dispatch-preview` | GET | Current slot dispatch hints |
| `/api/v1/optimization/propose` | POST | Trigger optimizer (non-blocking) |
| `/api/v1/optimization/approve` | POST | Approve pending plan `{"plan_id": "..."}` |
| `/api/v1/optimization/reject` | POST | Reject plan `{"plan_id": "..."}` |
| `/api/v1/optimization/pending` | GET | Current consent state |
| `/api/v1/optimization/preset` | POST | Set preset `{"preset": "guests"}` |
| `/api/v1/optimization/backend` | POST | Set backend `{"backend": "lp"}` |
| `/api/v1/optimization/mode` | POST | Set mode `{"mode": "operational"}` |
| `/api/v1/optimization/rollback` | POST | Restore latest snapshot |
| `/api/v1/optimization/snapshots` | GET | List snapshots |
| `/api/v1/optimization/auto-approve` | POST | Enable/disable auto-approve `{"enabled": true}` |
| `/api/v1/optimization/refresh` | POST | Refresh Agile rate cache |
| `/api/v1/metrics` | GET | Energy metrics |
| `/api/v1/schedule` | GET | Today's schedule |
| `/api/v1/weather` | GET | Weather context |

### Auto-Approve

When `PLAN_AUTO_APPROVE=true`, plans are immediately approved. The user still receives a `[AUTO-APPLIED]` notification with the full plan summary and can reject if needed.

**When to suggest enabling auto-approve:**
- User explicitly asks for it, or says "run automatically"
- User has been happy with simulation mode plans for several days
- System is stable with no unexpected actions

**When NOT to suggest it:**
- First time activating operational mode
- After a rollback or unexpected behaviour
- Household situation is changing (guests, travel, maintenance)

### Critical rules for optimization

1. **Never switch to operational without user consent.** Always explain what hardware will change.
2. **Never approve a plan without showing the user the summary first** (unless auto-approve is explicitly on and user has enabled it).
3. In operational mode, the system writes to hardware every 30 minutes. Notify the user if anything fails.
4. Rollback is always safe — forces simulation mode automatically.
5. The `boost` preset ignores price — use sparingly.
6. Fox ESS Scheduler V3 is uploaded once per day. Do NOT spam mode changes.

---

## Octopus Account Integration

**MCP tools:**
- `get_octopus_account()` — account summary: current tariff, MPAN roles, GSP
- `get_octopus_consumption(mpan, serial, period_from, period_to, group_by)` — smart meter consumption
- `auto_detect_octopus_setup()` — detect MPAN roles and current tariff, update runtime config

**REST endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/octopus/account` | GET | Account summary, current tariff, MPAN roles, GSP |
| `/api/v1/octopus/consumption` | GET | Consumption data for a MPAN |
| `/api/v1/octopus/auto-detect` | POST | Detect MPAN roles + current tariff |

---

## Tariff Comparison

**MCP tools:**
- `list_available_tariffs(max_tariffs)`
- `compare_tariffs(months_back, max_tariffs)`
- `get_tariff_recommendation(months_back)`
- `compare_tariffs_dashboard(months_back, granularity, max_tariffs)`

**Suggest a comparison when:** user asks "am I on the best tariff?", after major usage changes (new heat pump, solar, EV), or quarterly.

**REST endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/tariffs/available` | GET | List available Octopus tariff products |
| `/api/v1/tariffs/compare` | POST | Total-period simulation and ranked recommendation |
| `/api/v1/tariffs/dashboard` | POST | Granular daily/weekly/monthly comparison dashboard |

---

## Deploy invariants

Every deployment via `scripts/deploy_hetzner.sh` automatically runs a **safety reset** after the health check:
- Fox ESS work mode set to **`Self Use`** — prevents inverter stranding after code updates.
- Scheduler V3 flags cleared.

**Daikin refresh schedule (quota protection):**
- Heartbeat reads Daikin device state from cache only (no API call).
- A live refresh only happens in the **5-minute pre-slot window** before each Octopus half-hour boundary (`:55-:00` and `:25-:30`).
- Manual/MCP refreshes throttled to 30 minutes per actor (hard cap: ≤180 req/day).
- Quota tracked in `api_call_log` SQLite table. When exhausted, cached value returned with `stale=true`.
