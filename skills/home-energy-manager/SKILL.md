---
name: home-energy-manager
description: OpenClaw skill — natural interface to the Home Energy Manager (HEM). HEM owns ALL hardware logic, schedules, quotas, and consent; OpenClaw reads status, runs what-ifs, proposes plans, and requests changes via the `hem__*` MCP tool surface. Use whenever the user asks about the battery, solar, heat pump, Octopus tariffs, today's plan, yesterday's PnL, or anything energy-related.
metadata: {"openclaw": {"requires": {"env": ["HEM_MCP_URL", "HEM_MCP_TOKEN_FILE"]}, "primaryEnv": "HEM_MCP_URL", "emoji": "🏠"}}
---

# Home Energy Manager (HEM)

HEM is the **single planning brain** for the site. It fetches Octopus Agile tariffs, runs a PuLP MILP optimizer over a 24–48h horizon, uploads a Fox ESS Scheduler V3, and writes a Daikin action_schedule that a 2-minute heartbeat applies. The app is the only thing that talks to Daikin Onecta or Fox ESS cloud — OpenClaw never bypasses it.

You reach HEM through one MCP server, registered in `openclaw.json` as `hem` (long-lived streamable-http transport at `http://127.0.0.1:8000/mcp/`, bearer-guarded). Tool names appear to OpenClaw with the `hem__` prefix — `hem__get_soc`, `hem__propose_optimization_plan`, etc.

**Three rules, in order:**

1. **MCP is the only sanctioned channel.** Do not edit `.env`, `src/`, or any file on the app host; do not run shell commands against the container; do not make direct HTTP calls to Daikin Onecta or Fox ESS cloud. If a new capability is missing, request a new MCP tool. See `docs/OPENCLAW_BOUNDARY.md` for the full sanctioned surface.
2. **Read before write.** Always pull current state (`hem__get_cockpit_now` or `hem__get_daikin_status` + `hem__get_soc`) before recommending or executing a hardware change. The cockpit aggregator is one call and serves from cache.
3. **Respect the consent gate.** When a hardware-write tool returns `requires_confirmation: true`, a plan is pending. Show `hem__get_pending_approval()` to the user, ask, then either `hem__confirm_plan` / `hem__reject_plan` — or pass `confirmed=true` only after explicit user acknowledgement.

For safe what-if exploration ("what would tomorrow's plan look like with guests over?"), use `hem__simulate_plan` — read-only, quota-free, zero hardware impact.

Automated user notifications (plans, alerts, briefs) flow out via the **OpenClaw Gateway** `POST /hooks/agent` path — HEM does not run its own Telegram/Discord; OpenClaw owns delivery. `OPENCLAW_HOOKS_URL` and `OPENCLAW_HOOKS_TOKEN` are set in HEM's `.env`.

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
| `get_pending_approval` | Plan currently waiting for user consent (plan_id, expiry, summary, Daikin actions). Use before recommending `confirm_plan` or `reject_plan`. |

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

## Worked compositions — common questions, end-to-end

Each recipe shows the tools to compose, in order, to answer a real user request. Pull data first, then reason; don't issue write calls until you've grounded the recommendation in current state.

### "Where are we right now?" / "Status?"

```
hem__get_cockpit_now()
  → SoC, current Agile slot kind (cheap/normal/peak), solar/load/grid power,
    Daikin temps + mode, next Fox transition, freshness per source.
  → Single call, cached, no quota burn. This is the right opener for almost
    any energy conversation.
```

If the user wants more depth, follow with `hem__get_schedule()` for today's planned actions and `hem__get_battery_forecast()` for the daily SoC target.

### "Should we charge tonight?" / "Will tomorrow be cheap?"

```
1. hem__get_optimization_inputs(horizon_hours=24)
     → Agile prices (interpolated to half-hour), weather, base-load profile,
       initial SoC/tank/indoor with provenance, cheap/peak thresholds,
       tomorrow_rates_available flag.
2. hem__simulate_plan()                  # current settings, no overrides
     → status="optimal" / objective_pence / per-slot decisions in result.
3. (optional) hem__simulate_plan(overrides={"residents": 4, "extra_visitors": 2})
     → re-solve with guests over to compare.
```

Read-only, no hardware impact. Use the LP objective and slot kinds to advise: "yes, the LP is already pulling from the grid in slots X..Y at <Np".

### "Approve / reject the plan I see in Telegram"

```
1. hem__get_pending_approval()
     → plan_id, expires_in_minutes, summary, daikin_actions[].
     → If pending=false, the plan was either auto-approved or never existed.
2. Show the user the summary + key Daikin moves. Ask explicitly.
3a. hem__confirm_plan(plan_id)           # user said yes
3b. hem__reject_plan(plan_id, reason="…") # user said no — schedule cleared.
                                            Then: hem__propose_optimization_plan()
                                            to rebuild with any new context.
```

If `PLAN_AUTO_APPROVE=true` (default), `confirm_plan` is a no-op acknowledgement — the plan was already written when proposed. Tell the user it's already live.

### "Force a fresh plan now"

```
1. hem__get_optimization_status()
     → confirms scheduler healthy, no Octopus fetch error, current preset.
2. hem__propose_optimization_plan()
     → returns immediately with status="planning" + plan_id.
     → Background optimizer runs; user gets a PLAN_PROPOSED hook when ready.
3. (poll if needed) hem__get_pending_approval()
     → once status flips, follow the approve/reject recipe above.
```

Cooldown: re-proposing within `PLAN_REGEN_COOLDOWN_SECONDS` (default 300 s) returns `cooldown_active: true`. Either wait or `reject_plan` first to bypass.

### "Boost the heat pump for 2 hours" (manual override)

```
1. hem__get_daikin_status()
     → check is_on, weather_regulation, control_mode.
     → If control_mode="passive": stop, tell user the firmware owns the heat
       pump in passive mode. Ask if they want to flip to "active" first.
     → If weather_regulation=true: room-temp writes are blocked; use
       hem__set_daikin_lwt_offset or hem__override_schedule instead.
2. hem__override_schedule(hours=2.0, lwt_offset=3.0, tank_temp=55)
     → inserts a pre_heat action + paired restore action in the schedule,
       so the heat pump returns to plan baseline automatically when the
       window expires. No silent leftover state.
3. hem__get_recent_triggers(limit=3)
     → confirm both actions landed (pre_heat + restore rows visible).
```

`override_schedule` requires `OPENCLAW_READ_ONLY=false`. If blocked, surface that fact rather than retrying.

### "Am I on the best Octopus tariff?"

```
1. hem__get_octopus_account()
     → confirm current product_code + import/export MPAN roles.
2. hem__get_tariff_recommendation(months_back=1)
     → quick verdict: best tariff + projected annual savings vs current.
3. (if user wants detail)
   hem__compare_tariffs(months_back=3, max_tariffs=15)
     → ranked list with annual cost, lock-in, exit fee, green flag.
4. (if user wants per-day breakdown)
   hem__compare_tariffs_dashboard(months_back=1, granularity="daily")
     → which tariff would have won each day this month.
```

Suggest a comparison after major usage shifts (new heat pump, EV, solar) or quarterly. The tool uses real Fox import/export kWh from the chosen window.

### "Why did the LP do X yesterday at 18:00?"

```
1. hem__find_lp_run_for_time(when_utc="2026-04-27T17:00:00Z")
     → returns run_id of the LP solve that was active.
2. hem__get_lp_solution(run_id)
     → per-slot decision vector (import/export/charge/discharge kWh, SoC
       trajectory, tank/indoor/outdoor temps, LWT offset).
3. hem__get_meteo_forecast_history(fetch_at_utc=...)
     → what the forecast looked like at solve time (not just the latest).
4. hem__get_cockpit_at(when="2026-04-27T18:00:00Z")
     → the cockpit frozen at that moment — what the LP decided AND what
       actually happened (joined with execution_log).
```

Use these to explain past decisions in the user's terms (price + temps + SoC), not just "the optimizer said so".

### "How did yesterday go?" / "PnL?"

```
1. hem__get_energy_metrics()
     → daily/weekly/monthly delta vs SVT shadow + fixed-tariff shadow,
       VWAP, slippage, arbitrage efficiency, peak-import %, current SoC.
2. hem__get_attribution_day()    # defaults to yesterday
     → solar attribution: % of solar to self-use / battery / export.
3. hem__get_fox_energy_range(start, end)
     → daily totals across a date range for trend comparisons.
```

`get_attribution_day` reads `fox_energy_daily`, populated by the nightly rollup — today's row only appears after rollover.

### "Mute the daily PnL alert until I'm back from holiday"

```
1. hem__list_notification_routes()
     → see current routing for daily_pnl + other alert types.
2. hem__set_notification_route(alert_type="daily_pnl", enabled=false)
     → mutes immediately, no service restart.
3. (optional) hem__test_notification(alert_type="risk_alert")
     → verify other critical alerts still deliver via the hook agent.
```

Re-enable on return: `hem__set_notification_route(alert_type="daily_pnl", enabled=true)`.

### "Switch to guest preset for the weekend"

```
1. hem__get_optimization_status()
     → confirm current preset (likely "normal").
2. hem__set_optimization_preset("guests")
     → updates runtime config (DHW floor lifts to TARGET_DHW_TEMP_MIN_GUESTS_C,
       comfort widens). NOT persisted to .env.
3. hem__propose_optimization_plan()
     → re-solve with the new preset; auto-applies if PLAN_AUTO_APPROVE=true.
4. On Sunday evening, repeat with preset="normal" to revert.
```

Preset changes are runtime-only — they don't survive a service restart. For a permanent change, edit `.env` (sysadmin task, not OpenClaw's).

### "Tune a runtime setting" (e.g. nudge the cheap-window threshold)

```
1. hem__list_settings()
     → every tunable + current value + env default + range + overridden flag.
2. hem__set_setting(key="LP_CHEAP_PRICE_PENCE", value=8.0)
     → DRY RUN by default — returns canonical value + cron_reload flag,
       does not persist.
3. hem__set_setting(key="LP_CHEAP_PRICE_PENCE", value=8.0, confirmed=true)
     → applies the change; for cron-class keys it also re-registers the cron.
4. hem__get_config_audit(key="LP_CHEAP_PRICE_PENCE")
     → confirm the change is logged with actor="mcp".
```

The dry-run is intentional — show the user the canonical value before committing, especially for schedule keys (LP_PLAN_PUSH_HOUR, LP_MPC_HOURS) that re-register cron jobs.

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

## Plan lifecycle terminology — be precise when answering "what is happening"

Use these terms exactly when answering questions about timing or status. Mixing
them up confuses users (especially around the daily 16:00 BST tariff publication
and the Fox V3 cyclic schedule).

| Term | Definition |
|---|---|
| `run_at` | UTC timestamp when the LP solver finished the run (column on `optimizer_log`). |
| `plan_date` | The local date the plan is anchored to (column on `lp_inputs_snapshot`). For runs after Octopus publishes ~16:00 local, this is usually *tomorrow*. |
| `horizon` | The 48 h window the LP optimises over (S10.2 / #169) — typically `run_at` rounded down to the half-hour, plus 48 h. |
| `executed` | Slots where `slot_time_utc < now`. Realised; further LP changes can't affect them. |
| `ongoing` | The single slot containing the current wall-clock time. |
| `planned` | Slots in the future that the most recent LP solve has decided actions for. |
| `dispatch decision` | Per-slot record of `lp_kind` (what LP planned) → `dispatched_kind` (what got into Fox V3) → `committed` flag → textual `reason`. Audit trail for every solve. |

**MCP tools that surface this:**
- `get_plan_timeline()` — partitions the active plan into executed / ongoing / planned with the dispatch decision attached. Always start here for "what's the status?"
- `explain_dispatch_decisions(run_id=None)` — full per-slot decision rows for the latest run (or any past run by id). Use this to answer "why did/didn't X happen?"
- `get_scenario_batch(batch_id=None)` — per-scenario LP solve summary for the 3-pass robustness batch tied to a run. Carries objectives, peak-export slot counts, perturbation deltas applied, wall-clock durations. Use this when a user asks "how different was pessimistic from nominal today?" or "did all three scenarios converge?"
- `get_fox_schedule_diff()` — live Fox V3 vs. last HEM upload. Use this to detect drift (manual Fox-app edit, failed upload, firmware quirk) before answering "what is the inverter doing right now?"

**Common mistakes to avoid:**
- *"The plan for today says…"* — ambiguous. Say "the plan run_id=N (run_at=…, plan_date=…) decided X for slot HH:MM."
- Conflating `plan_date` with `run_at`'s date. After 16:00 local, the active plan's `plan_date` is tomorrow, not today.
- Reading raw Fox V3 groups and concluding "tomorrow at 18:00 there's a ForceDischarge." Fox V3 is **cyclic** (no date — see below). The dispatch_decisions / plan_timeline tools are the source of truth for date-bound actions.

---

## Scenario-based peak-export robustness

The LP can decide to discharge the battery to grid during an Agile peak when the
arbitrage spread (Outgoing rate at peak − Incoming rate at the earlier charge
slot) covers the round-trip efficiency loss (η = 0.92). Because that decision is
rooted in the *forecast*, an unforecast cold night (Daikin radiator ramp) or
appliance spike could force buy-back at peak rates and flip the profit into a
loss.

Rather than disabling arbitrage (overcautious) or trusting the forecast blindly
(overconfident), HEM runs the LP **three times** at high-stakes triggers:

| Scenario | Outdoor temp | Base load | Purpose |
|---|---|---|---|
| Optimistic | forecast + 1.0 °C | × 0.90 | Upper-bound view; informational only. |
| Nominal | forecast as-is | × 1.00 | The canonical solve — what a single-pass LP would have done. |
| Pessimistic | forecast − 1.5 °C | × 1.15 | Stressed forecast; gates the commit. |

**Decision rule (V1 — maximin):** A `peak_export` slot is uploaded to Fox V3
**only if the pessimistic scenario also exports ≥ `LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH`
(default 0.30 kWh) at that slot.** Otherwise the slot is downgraded to standard
SelfUse and the inverter discharges only to cover load (no grid feed).

The kill switch is `ENERGY_STRATEGY_MODE=strict_savings`: every `peak_export`
slot is dropped regardless of scenarios. Use it when a user explicitly says
"never export to grid" — but warn them of the missed arbitrage £.

**When OpenClaw is asked "why is there no ForceDischarge tomorrow at 18:00?":**
1. Call `explain_dispatch_decisions()` for the latest run.
2. Find the slot in question.
3. Report the four numbers in plain English:
   ```
   Tomorrow 18:00 BST: LP wanted peak_export but the pessimistic scenario only
   projects 0.05 kWh export (well under the 0.30 kWh safety floor) — meaning
   if outdoor temp drops 1.5 °C below forecast and base load runs 15 % hot,
   the LP wouldn't choose to export this slot. The dispatch dropped it; the
   battery will still discharge to cover house load via SelfUse.
   ```
4. If the user wants to override, the answer is `set_setting("ENERGY_STRATEGY_MODE", "savings_first")` is already the default; the only knob to relax robustness is `LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH` (lower = less conservative). Don't recommend this casually.

**Triggers that get the 3-pass scenario solve** (default `LP_SCENARIOS_ON_TRIGGER_REASONS=cron,plan_push,octopus_fetch`):
- Nightly plan push (00:05 UTC)
- Hourly cron MPC fires (`LP_MPC_HOURS_LIST`)
- The Octopus fetch trigger (~16:05 local) — the natural pre-peak moment

Other triggers (`soc_drift`, `forecast_revision`, `dynamic_replan`, `manual`) run
the nominal solve only to keep MPC re-plan latency low. They commit
`peak_export` slots verbatim with `reason=no_scenarios_run`.

---

## Fox V3 cyclic schedule reasoning

The Fox V3 inverter's "Scheduler V3" stores wall-clock cyclic groups — each
group has `startHour:startMinute` and `endHour:endMinute` but **no date**.
A group "18:00–19:00 ForceDischarge" repeats every day at 18:00 local until
HEM uploads a new schedule.

This has two operational consequences OpenClaw must respect:

1. **Don't read Fox V3 state and conclude "the schedule for tomorrow is X."**
   The inverter doesn't have a notion of "tomorrow." The active LP plan
   (queryable via `get_plan_timeline`) is what knows about specific dates.
   Use `get_fox_schedule_diff()` only to verify "what HEM uploaded matches
   what the inverter is running" — not to read the plan.

2. **The 24 h dispatch cap (`src/scheduler/lp_dispatch.py:371`) is intentional.**
   The LP horizon is 48 h but only the first 24 h is sent to Fox V3. Otherwise
   a D+0 ForceCharge at 13:00 and a D+1 ForceCharge at 13:00 would collapse
   into one cyclic group (they have the same hour-of-day). The next MPC
   re-solve handles D+1 dispatch once D+1 becomes "today."

When a user asks "is the schedule overlapping my settings?" — call
`get_fox_schedule_diff()`. `any_drift=True` means HEM and Fox disagree;
investigate `diffs.only_live` (groups on Fox that HEM didn't send — most
likely a manual edit through the Fox app) and `diffs.only_recorded` (HEM
sent but Fox doesn't show — possibly a failed upload).

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

### Manual override window

| Tool | Parameters | Notes |
|------|-----------|-------|
| `override_schedule(hours, lwt_offset, tank_temp?)` | `hours: float = 2.0`, `lwt_offset: float = 3.0`, `tank_temp: float \| None` | Inserts a `pre_heat` action with paired `restore` so the system reverts cleanly. Requires `OPENCLAW_READ_ONLY=false`. Use this rather than chaining several `set_daikin_*` calls — the restore guarantees no leftover boost. |
| `acknowledge_warning(warning_key)` | `warning_key: str` | Suppresses repeat alerts for a known warning (e.g. low-SoC nag, quota-exhaustion banner). Use when the user has explicitly seen and decided to ignore. |

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

## Octopus account, consumption, and tariff comparison

HEM holds an authenticated Octopus session; use these tools when the user asks
about meter data, tariff fit, or wants to confirm the import/export MPAN roles.

| Tool | Use |
|------|-----|
| `get_octopus_account` | Current product code + import/export MPAN roles + GSP. Quick "what tariff am I on?" answer. |
| `get_octopus_consumption(group_by="day")` | Smart-meter consumption for a window. `group_by`: `day` / `week` / `month` / `None` (half-hourly). Defaults to import MPAN. Returns slot intervals + kWh + total. |
| `auto_detect_octopus_setup` | One-shot: re-detects which MPAN is import vs export and the active tariff product, updates runtime config. Run after first install or if MPAN roles look swapped. Runtime-only — does not persist to `.env`. |
| `list_available_tariffs(max_tariffs=15)` | Currently available Octopus electricity products + rates + standing charges + lock-in / exit-fee policy. Use to scout the market before a comparison. |
| `compare_tariffs(months_back, max_tariffs)` | Ranked simulation against the household's actual Fox ESS import/export kWh for the window. Returns annual cost, savings vs current, lock-in months, exit fees, green flag. Best for a "should I switch?" answer. |
| `get_tariff_recommendation(months_back=1)` | One-paragraph verdict: best tariff + projected annual savings vs current. Use for quick answers. |
| `compare_tariffs_dashboard(months_back, granularity)` | Per-period (`daily` / `weekly` / `monthly`) breakdown showing which tariff would have won each bucket + win counts. Best for "show me which tariff was cheapest each day this month". |

```
hem__get_energy_metrics              → daily PnL, VWAP, slippage vs SVT/fixed shadow
hem__compare_tariffs(months_back=3)  → ranked tariff simulation (best → worst)
hem__get_tariff_recommendation()     → one-line recommendation
```

Suggest a tariff comparison when: user asks "am I on the best tariff?", after major usage changes (new heat pump, solar, EV), or quarterly.

---

## Runtime settings — `list_settings`, `get_setting`, `set_setting`

A subset of HEM's `.env` knobs are exposed as runtime-tunable settings (cached
30 s). Some are schedule-class — changing them re-registers APScheduler cron
jobs in-process, no service restart needed.

| Tool | Use |
|------|-----|
| `list_settings` | Every tunable: current value, env default, range, `overridden` flag. Start here. |
| `get_setting(key)` | Read one value (e.g. `LP_PLAN_PUSH_HOUR`). |
| `set_setting(key, value)` | **Dry-run by default** — returns the canonical (post-validation) value + `cron_reload` flag, persists nothing. Show this to the user first. |
| `set_setting(key, value, confirmed=true)` | Persists the change; if `cron_reload` is true, re-registers the relevant cron job. |
| `get_config_audit(key?)` | Append-only log of every `set_setting` (and delete) with actor — explains why a past plan looked the way it did. |

Examples worth knowing: `LP_PLAN_PUSH_HOUR` / `LP_PLAN_PUSH_MINUTE` (UTC anchor for the nightly Daikin push), `LP_MPC_HOURS` (intra-day re-solve cadence), `LP_CHEAP_PRICE_PENCE` / `LP_PEAK_PRICE_PENCE` (slot-kind thresholds), `DHW_TEMP_NORMAL_C`, `TARGET_DHW_TEMP_MIN_GUESTS_C`. Always check `list_settings` for the live set — schema can drift.

For permanent changes (surviving container restart), the user must edit
`/srv/hem/.env` on the host and `systemctl restart hem`. That is a sysadmin
task, not an OpenClaw task.

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
