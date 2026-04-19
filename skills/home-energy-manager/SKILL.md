---
name: home-energy-manager
description: OpenClaw skill — talk to the Home Energy Manager standalone app (REST API). The app owns schedules and hardware; this skill is only a remote interface for status, reports, and confirmed actions.
metadata: {"openclaw": {"requires": {"env": ["HOME_ENERGY_API_URL"]}, "primaryEnv": "HOME_ENERGY_API_URL", "emoji": "🏠"}}
---

# Home Energy Manager (OpenClaw ↔ app interface)

**Home Energy Manager** is a **standalone service** and the **planning brain** for the site: it stores Agile tariffs, uses weather and load history, optimises Fox + heat-pump schedules, and runs a heartbeat to apply them. OpenClaw does not replace that service: it connects to it over HTTP using this skill.

**Base URL**: Set `HOME_ENERGY_API_URL` to the running app (e.g. `http://192.168.1.100:8000`).

## How to discover available actions

Before doing anything, fetch the capabilities list to see what's available and what constraints apply:

```
GET {HOME_ENERGY_API_URL}/api/v1/openclaw/capabilities
```

This returns every action you can take, its parameters, validation ranges, and whether it requires confirmation.

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

## Data report (energy, cost, charts — for OpenClaw)

All insight data is provided by the API as a **data report**. Use the report endpoint for a single response that includes every metric and a spoken summary.

**Full data report (recommended for OpenClaw):**
```
GET {HOME_ENERGY_API_URL}/api/v1/energy/report
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=month&month=YYYY-MM
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=year&year=YYYY
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=day&date=YYYY-MM-DD
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=week&date=YYYY-MM-DD
```

- **No query params**: current month’s report (same as `period=month&month=YYYY-MM` for this month).
- **period** = `day` | `week` | `month` | `year`. For **day/week** use `date=YYYY-MM-DD`. For **month** use `month=YYYY-MM`. For **year** use `year=YYYY`.

**Response (full data report):**

| Field | Description |
|-------|-------------|
| `period` | `"day"` \| `"week"` \| `"month"` \| `"year"` |
| `period_label` | Human label, e.g. `"Feb 2026"`, `"4–10 Feb 2026"` |
| `energy` | `import_kwh`, `export_kwh`, `solar_kwh`, `load_kwh`, `charge_kwh`, `discharge_kwh` |
| `cost` | `net_cost_pounds`, `import_cost_pounds`, `export_earnings_pounds`, `net_cost_pence`, etc. |
| `heating_estimate_kwh` | Estimated heating consumption (when available) |
| `equivalent_gas_cost_pounds` | What the same period would cost on gas |
| `gas_comparison_ahead_pounds` | Positive = ahead with solar + heat pump; negative = gas would be cheaper |
| `chart_data` | Array of `{ date, import_kwh, export_kwh, solar_kwh, load_kwh, charge_kwh, discharge_kwh }` for charts |
| `heating_analytics` | When available: `heating_percent_of_cost`, `heating_percent_of_consumption`, `degree_days`, `temp_bands`, etc. |
| `summary` | Short narrative for TTS/chat: cost, balance, gas comparison. Use this to speak the report. |

Use `summary` for voice answers; use the structured fields for exact numbers, charts, or follow-up questions. Returns 503 if Fox ESS is not configured; 400 for invalid params; 502 on Fox ESS errors.

**Legacy endpoints (still supported):**

- **Monthly only (no chart_data, no day/week/year):**  
  `GET {HOME_ENERGY_API_URL}/api/v1/energy/monthly?month=YYYY-MM`  
  Returns: same `energy`, `cost`, heating/gas fields as above.

- **Narrative only (no structured data):**  
  `GET {HOME_ENERGY_API_URL}/api/v1/energy/insights`  
  Returns: `{ "summary": "..." }` for current month. Prefer `/energy/report` to get data + summary in one call.

## How to execute actions

Use the unified execute endpoint:

```
POST {HOME_ENERGY_API_URL}/api/v1/openclaw/execute
Content-Type: application/json

{"action": "<action_name>", "parameters": {<params>}}
```

### Actions that do NOT require confirmation

These execute immediately:

| Action | Parameters | Notes |
|--------|-----------|-------|
| `daikin.temperature` | `{"temperature": 21}` | Range: 15-30°C. **BLOCKED when weather regulation is active** — use `daikin.lwt_offset` instead. |
| `daikin.lwt_offset` | `{"offset": -3}` | Range: -10 to +10. Works in all modes including weather regulation. |
| `daikin.mode` | `{"mode": "heating"}` | Options: `heating`, `cooling`, `auto`, `fan_only`, `dry` |
| `daikin.tank_temperature` | `{"temperature": 45}` | Range: 30-60°C |
| `foxess.charge_period` | `{"start_time": "00:30", "end_time": "05:00", "target_soc": 90}` | Optional: `period_index` (0 or 1) |

### Actions that REQUIRE confirmation (2-step flow)

These are destructive or mode-changing operations. The API enforces a confirmation step:

| Action | Parameters |
|--------|-----------|
| `daikin.power` | `{"on": true}` or `{"on": false}` |
| `daikin.tank_power` | `{"on": true}` or `{"on": false}` |
| `foxess.mode` | `{"mode": "Self Use"}` — options: `Self Use`, `Feed-in Priority`, `Back Up`, `Force charge`, `Force discharge` |

**Step 1** — Send the action. You'll get back a `confirmation_token`:
```json
{
  "requires_confirmation": true,
  "action": {"action_id": "abc123...", "description": "Turn Daikin OFF", "status": "pending"},
  "message": "Confirmation required: Turn Daikin OFF. Re-send with confirmation_token='abc123...' to execute."
}
```

**Step 2** — Confirm by re-sending with the token:
```json
POST {HOME_ENERGY_API_URL}/api/v1/openclaw/execute
{"action": "daikin.power", "parameters": {"on": false}, "confirmation_token": "abc123..."}
```

Confirmation tokens expire after 5 minutes.

## Critical rules

1. **Always check status before making changes.** Read the current state to understand what mode the system is in.
2. **Weather regulation**: When `weather_regulation` is `true` in the Daikin status, you CANNOT set room temperature. Use `daikin.lwt_offset` to adjust heating intensity instead.
3. **Confirmation flow**: Never skip the 2-step confirmation for power and mode changes. Always tell the user what you're about to do and confirm the result.
4. **Rate limiting (internal)**: The API enforces a 5-second cooldown between commands of the same type. If you get a 429 response from the local API, wait 5 seconds and retry.
5. **Rate limiting (Daikin cloud)**: The Daikin Onecta Cloud API has a **200 requests/day** limit. This is a hard daily quota. Avoid polling status too frequently — 10-15 minute intervals are recommended for automated refreshes. A 429 from the Daikin cloud means you've hit the daily limit and must wait until the next day.
6. **Temperature ranges**: Room temp 15-30°C, tank temp 30-60°C, LWT offset -10 to +10. The API will reject out-of-range values.
7. **Fox ESS may be unavailable**: If Fox ESS returns a 503, it means credentials are not yet configured. Only Daikin operations will work.

## Recommendation-only mode (403)

If the API returns **403** on `POST /api/v1/openclaw/execute` with a message like "recommendation-only mode", the server is configured so OpenClaw must **not** execute changes. In that case:

- **Only recommend** actions to the user (e.g. "I suggest setting the temperature to 21°C" or "Consider switching to Feed-in Priority").
- Tell the user to apply changes themselves via the **dashboard** (web UI) or **CLI**.
- Do **not** retry execute or attempt to bypass; respect the safeguard.

## Error handling

- `400` — Invalid parameters (bad mode, out-of-range value)
- `403` — Recommendation-only mode: do not execute; suggest actions and tell the user to apply via dashboard/CLI.
- `404` — No devices found
- `409` — Action blocked (e.g. setting temperature during weather regulation)
- `410` — Confirmation token expired
- `429` — Rate limited. Check the error message: if it mentions "5 seconds", wait and retry; if it mentions "API rate limit exceeded", you've hit the Daikin daily limit.
- `502` — Upstream device API error
- `503` — Service not configured

---

## Optimization Engine (V7)

The system runs a **simulation-first, consent-driven** optimization engine that manages the Fox ESS battery and Daikin ASHP around Octopus Agile half-hourly tariff prices. All hardware automation requires:
1. A plan to be **proposed** and reviewed
2. The user to explicitly **approve** the plan
3. The system to be in **operational** mode (default is simulation)

### Operation Modes

| Mode | Behaviour |
|------|-----------|
| `simulation` | Default. Computes plans, logs what it WOULD do, sends notifications. No hardware writes. |
| `operational` | Writes to Fox ESS and Daikin on each 30-min tick using the approved plan. |

**Always confirm with the user before switching to operational mode.**

### Household Presets

| Preset | Behaviour |
|--------|-----------|
| `normal` | Standard comfort, optimize cost within bounds |
| `guests` | Higher DHW (48°C+), warmer rooms, less aggressive cost-cutting |
| `travel` / `away` | Frost protection only, max battery export during peak, DHW off except Legionella |
| `boost` | Temporary full-comfort override, ignores price for full-comfort heating |

### Planner backend (V8)

`OPTIMIZER_BACKEND` is `lp` (default, PuLP MILP in `src/scheduler/lp_optimizer.py`) or `heuristic` (legacy price-quantile classifier). Switch via env, `POST /api/v1/optimization/backend`, or MCP `set_optimizer_backend(backend)`.

### Weather-Aware Optimization

When `WEATHER_LAT`/`WEATHER_LON` are set, the solver:
- Fetches 48h temperature and solar radiation forecast (Open-Meteo, free, no key)
- Estimates PV generation (4.5kWp system)
- Boosts LWT pre-heating before cold slots even at standard rates
- Skips grid battery charging when solar is expected to fill the battery (>2kW forecast)

### OpenClaw Optimization Workflow

**Standard flow:**

```
1. get_optimization_status          → check mode, preset, optimizer backend, consent state
2. propose_optimization_plan        → compute plan, returns summary + plan_id
3. [show summary to user, ask for approval]
4. approve_optimization_plan(plan_id) → activate plan
5. [plan runs in simulation (shadow logs) or operational (hardware writes)]
```

**Changing settings:**
```
set_optimization_preset(preset)     → normal / guests / travel / away / boost
set_optimizer_backend(backend)      → lp | heuristic
set_operation_mode(mode)            → simulation / operational (requires approved plan for operational)
```

**Emergency rollback:**
```
rollback_config()                   → restore latest snapshot, force simulation mode
get_config_snapshots()              → list available snapshots
```

### MCP Tools (OpenClaw)

| Tool | Description |
|------|-------------|
| `get_optimization_status` | Full status: mode, preset, optimizer backend, cache, consent state |
| `get_optimization_plan` | 48-slot plan with per-slot prices, actions, weather notes |
| `propose_optimization_plan` | Compute plan and propose for consent (returns summary + plan_id) |
| `approve_optimization_plan(plan_id)` | Approve pending plan — only call after user confirms |
| `reject_optimization_plan(plan_id)` | Reject a pending plan |
| `set_optimization_preset(preset)` | Switch household preset |
| `set_optimizer_backend(backend)` | `lp` (PuLP) or `heuristic` (legacy) |
| `set_operation_mode(mode)` | Switch simulation / operational |
| `rollback_config(snapshot_id?)` | Restore config snapshot |
| `get_config_snapshots` | List available snapshots |
| `set_auto_approve(enabled)` | Enable/disable automatic plan approval |

### REST API Endpoints (mirrors MCP tools)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/optimization/status` | GET | Extended status with mode and consent |
| `/api/v1/optimization/plan` | GET | 48-slot solver plan |
| `/api/v1/optimization/dispatch-preview` | GET | Current slot dispatch hints |
| `/api/v1/optimization/propose` | POST | Propose plan for consent |
| `/api/v1/optimization/approve` | POST | Approve pending plan `{"plan_id": "..."}` |
| `/api/v1/optimization/reject` | POST | Reject plan `{"plan_id": "..."}` |
| `/api/v1/optimization/pending` | GET | Current consent state |
| `/api/v1/optimization/preset` | POST | Set preset `{"preset": "guests"}` |
| `/api/v1/optimization/backend` | POST | Set backend `{"backend": "lp"}` or `"heuristic"` |
| `/api/v1/optimization/mode` | POST | Set mode `{"mode": "operational"}` |
| `/api/v1/optimization/rollback` | POST | Restore latest snapshot |
| `/api/v1/optimization/snapshots` | GET | List snapshots |
| `/api/v1/optimization/auto-approve` | POST | Enable/disable auto-approve `{"enabled": true}` |
| `/api/v1/optimization/refresh` | POST | Refresh Agile rate cache |

### Config Snapshots

Before any mode transition a JSON snapshot is auto-saved to `data/config_snapshots/`. Snapshot contains current settings and live device state. Rollback restores runtime config and forces simulation mode. `.env` is never modified.

### Auto-Approve

When `PLAN_AUTO_APPROVE=true` (or toggled via `set_auto_approve`), every new plan proposed by `propose_optimization_plan` is immediately approved without waiting for user consent. This enables fully hands-off operation.

**Safety guarantees that still apply in auto-approve mode:**
- A notification is always sent to OpenClaw / stdout with the full plan summary
- The user can call `reject_optimization_plan(plan_id)` at any time to cancel
- `OPERATION_MODE` still gates hardware writes — auto-approve in simulation mode is harmless
- Rollback always works and forces simulation mode

**When to suggest enabling auto-approve:**
- User explicitly asks for it, or says "run it automatically"
- User has been running in simulation mode and is happy with how the plans look
- System is stable and no unexpected actions have occurred

**When NOT to suggest it:**
- First time activating operational mode
- After a rollback or unexpected behaviour
- When the user's household situation is changing (guests, travel, etc.)

### Octopus Account Integration

The system has full authenticated access to the Octopus Energy REST API via the account's API key. This enables accurate smart meter data, auto-detection of MPAN roles, and current tariff discovery.

**Configuration (in `.env`):**
- `OCTOPUS_API_KEY` — Octopus API key (prefix `sk_live_`)
- `OCTOPUS_ACCOUNT_NUMBER` — e.g. `A-D78B434E`
- `OCTOPUS_MPAN_1` / `OCTOPUS_METER_SN_1` — first meter point
- `OCTOPUS_MPAN_2` / `OCTOPUS_METER_SN_2` — second meter point
- `OCTOPUS_GSP` — Grid Supply Point letter (e.g. `H` for London W4)

**Authentication:** HTTP Basic Auth with API key as username, empty password.

**MPAN roles (import vs export):**
The `auto_detect_octopus_setup` tool calls the Octopus account endpoint and uses the `is_export` flag to determine which MPAN is the import and which is export. Call this once after setup to confirm correct detection.

**MCP tools:**
- `get_octopus_account()` — account summary: current tariff, MPAN roles, GSP, detection source
- `get_octopus_consumption(mpan, serial, period_from, period_to, group_by)` — smart meter consumption (half-hourly or aggregated by day/week/month)
- `auto_detect_octopus_setup()` — detect MPAN roles and current tariff from account API, update runtime config

**REST endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/octopus/account` | GET | Account summary, current tariff, MPAN roles, GSP |
| `/api/v1/octopus/consumption` | GET | Consumption data for a MPAN (proxy to Octopus) |
| `/api/v1/octopus/auto-detect` | POST | Detect MPAN roles + current tariff, update runtime config |

**When to call auto-detect:**
- First-time setup (to confirm import vs export MPAN)
- If tariff comparison shows wrong "current tariff"
- After switching energy tariff

**Dashboard:** The Optimize tab includes an "Octopus Account" card showing current tariff, MPAN roles, GSP, and a one-click "Auto-detect" button.

---

### Tariff Comparison

The system fetches, simulates, and recommends the best electricity tariff from Octopus Energy. It uses real Octopus smart meter data (import + export) as the primary source, with Fox ESS daily data as fallback.

**Current tariff baseline:** Auto-detected from the Octopus account API (or set via `CURRENT_TARIFF_PRODUCT` in `.env`). All savings are calculated relative to this baseline.

**Data source priority (best → fallback):**
1. **Octopus half-hourly consumption** — exact slot-matched simulation for Agile, fetched via authenticated API
2. **Octopus day-aggregated consumption** — accurate daily totals for flat/TOU simulation
3. **Fox ESS daily energy breakdown** — fallback when Octopus API unavailable
4. **Synthetic defaults** — 8.5 import / 2.0 export kWh/day if all sources fail

**How it works:**
1. Fetches the Octopus product catalogue (public API, no auth)
2. Resolves regional tariff codes using `OCTOPUS_GSP` (e.g. `H` = London W4)
3. Retrieves standing charges and unit rates per product
4. Fetches per-day Octopus consumption for the comparison period (1–12 months)
5. Computes cost per tariff per day/week/month, identifies the winner for each period
6. Ranks tariffs by total cost and computes savings vs the current tariff
7. For Agile: if half-hourly consumption available, performs exact slot × price simulation

**Monthly cost calculation:** The `get_monthly_insights` and `get_period_insights` functions now use Octopus half-hourly consumption × half-hourly rates for true actual cost. Manual flat rate is used as fallback.

**MCP tools:**
- `list_available_tariffs(max_tariffs)` — browse available products
- `compare_tariffs(months_back, max_tariffs)` — total-period simulation and ranked comparison
- `get_tariff_recommendation(months_back)` — concise best-tariff recommendation
- `compare_tariffs_dashboard(months_back, granularity, max_tariffs)` — granular daily/weekly/monthly breakdown with win counts and savings vs current; includes `data_source` field showing whether Octopus or Fox ESS data was used

**REST endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/tariffs/available` | GET | List available Octopus tariff products |
| `/api/v1/tariffs/compare` | POST | Total-period simulation and ranked recommendation |
| `/api/v1/tariffs/dashboard` | POST | Granular daily/weekly/monthly comparison dashboard |

**Pricing structures supported:**
- **Flat** — single unit rate, all hours (e.g. Flexible Octopus)
- **Time-of-use** — day/night rates with off-peak windows (Go, Cosy, Economy 7, Flux)
- **Half-hourly** — Agile, using real half-hourly consumption × rates for exact simulation
- **Tracker** — wholesale price + markup
- **Capped variable** — standard variable with Ofgem cap

**Policy factors considered:**
- Standing charge (p/day)
- Contract type (fixed / variable / rolling)
- Lock-in period (months)
- Exit fees (per fuel)
- Green tariff status
- Export payments (SEG / Outgoing Octopus)

**When to suggest a tariff comparison:**
- User asks "Am I on the best tariff?" or "Should I switch?"
- Annual cost discussions or energy reviews
- After significant changes to usage pattern (new heat pump, solar, EV)
- Periodically (quarterly is reasonable)

**Dashboard:** The Optimize tab includes a "Tariff Comparison" card with:
- **Hero banner** showing best tariff, current tariff, and potential annual savings
- **Win bar** — colour-coded bar showing which tariff won each period
- **Cost chart** — line chart comparing top 5 tariffs over time (daily/weekly/monthly)
- **Ranking table** — all tariffs ranked by annual cost with savings vs current, win counts, contract terms, and green status
- **Granularity toggle** — switch between daily, weekly, monthly views
- **Period selector** — 1/3/6/12 months of historical data
- **Data source badge** — shows whether Octopus smart meter or Fox ESS data was used

### Critical Rules for Optimization

1. **Never switch to operational without user consent.** Always explain what will happen.
2. **Never approve a plan without showing the user the summary first** (unless auto-approve is on).
3. In operational mode, the system writes to hardware every 30 minutes. Notify the user if anything fails.
4. Rollback is always safe — it forces simulation mode automatically.
5. The `boost` preset ignores price entirely — use sparingly (cold snaps, full house).
6. Fox ESS API limit is **200 req/day**. Optimization reads use the cached realtime path.

### Deploy invariants

Every deployment via `scripts/deploy_hetzner.sh` automatically runs a **safety reset** after the service passes its health check:

- Fox ESS work mode is set to **`Self Use`** via `POST /api/v1/foxess/mode` (`{"mode":"Self Use","skip_confirmation":true}`)

This ensures the inverter is never stranded in Agile/Force-charge/Force-discharge mode after a code update. The reset is idempotent and safe to run manually at any time.

**Daikin refresh schedule (quota protection):**
- Heartbeat reads Daikin device state from cache only (no API call).
- A live `get_devices()` call is only made in the **5-minute pre-slot window** before each Octopus half-hour boundary (`:55-:00` and `:25-:30`) — this gives the LP replanner fresh data with time to act.
- Manual/UI refreshes are throttled to 30 minutes per actor to stay within the **200 calls/day** Daikin limit.
- Quota is tracked in the `api_call_log` SQLite table (persists across restarts). When exhausted, the last cached value is returned with `stale=true`.

## Bulletproof mode (MCP-first)

When the stack runs with **`USE_BULLETPROOF_ENGINE=true`** (default), automation is **autonomous**: Octopus fetch → SQLite → optimizer → Fox **Scheduler V3** upload (one call/day) + Daikin rows in **`action_schedule`**. A **2-minute heartbeat** executes Daikin changes and logs telemetry; it does **not** spam Fox mode APIs.

**Prefer MCP** (`./bin/mcp` or `python -m src.mcp_server`) over raw REST for OpenClaw:

| Tool | Use |
|------|-----|
| `get_energy_metrics` | Daily/weekly/monthly PnL vs SVT/fixed shadow, VWAP, slippage, SLA, SoC |
| `get_schedule` | Today’s SQLite actions + last Fox V3 snapshot |
| `get_daily_brief` | Same content as the 08:00 webhook, on demand |
| `get_battery_forecast` | SoC + `daily_targets` snapshot |
| `get_weather_context` | 48h forecast + live Daikin temps |
| `get_action_log` / `get_optimizer_log` | Audit trail |
| `override_schedule` | Manual heating boost window (**requires `OPENCLAW_READ_ONLY=false`**) |
| `acknowledge_warning` | Dismiss repeating risk alerts (e.g. low SoC + peak price) |

**REST mirrors:** `GET /api/v1/metrics`, `GET /api/v1/schedule`, `GET /api/v1/weather`.

**Notifications** (OpenClaw webhook): morning report, strategy update after fetch, risk alerts, action confirmations, critical errors — see `src/notifier.py` (`AlertType`).

**Workflow:** Report performance with `get_energy_metrics` / `get_daily_brief`. Explain live behaviour with `get_schedule`. Use `override_schedule` only for explicit user-requested interventions.
