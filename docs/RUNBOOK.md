# Operational Runbook

Day-to-day operations, deployment, and debugging for the Home Energy Manager on Hetzner.

---

## Infrastructure

Prod runs the **immutable Docker image** (cutover 2026-04-25). There is no
editable code on the host and no venv — `git pull` / `pip install` on the
server do nothing. The full install/rollback runbook is `deploy/README.md`;
this section is the day-to-day summary.

| Component | Detail |
|-----------|--------|
| Server | Hetzner (Tailscale: `<hem-host>.ts.net`, `100.x.y.z`) |
| SSH | `ssh root@<hem-host>.ts.net` |
| Host dir | `/srv/hem` — the ONLY thing that survives a redeploy |
| Image | `ghcr.io/albinati/home-energy-manager:<sha>` (linux/arm64), built by CI on push to `main` |
| Container | `hem` (uid 1001, read-only rootfs, tmpfs `/tmp`) |
| Active DB | `/srv/hem/data/energy_state.db` → `/app/data/energy_state.db` inside the container |
| Config | `/srv/hem/.env` (mounted ro; perms `640 root:1001`) |
| Image pin | `HEM_IMAGE_TAG` in **`/srv/hem/.compose.env`** (systemd `EnvironmentFile`) — putting it in `.env` is a silent no-op |
| Compose | `/srv/hem/compose.yaml` |
| Service | `systemd: hem.service` (wraps `docker compose up`) |
| Logs | `journalctl -u hem -f` (journald driver) |
| API | `http://127.0.0.1:8000` (loopback) + Tailscale interface |
| UI | `hem-ui` container, Tailscale funnel on `:8443` |

### Common commands

```bash
# Status
systemctl is-active hem
curl -fsS http://127.0.0.1:8000/api/v1/health     # → status, version, revision SHA
docker exec hem cat /app/.git-sha                 # build SHA actually running

# Logs (tail)
journalctl -u hem -n 100 --no-pager

# Restart (after .env / .compose.env changes — docker compose down + up)
systemctl restart hem

# Anything that needs the venv runs INSIDE the container
docker exec hem python -m src.cli <subcommand>

# DB queries
docker exec hem python -c "import sqlite3; conn = sqlite3.connect('/app/data/energy_state.db'); ..."
```

> One-off diagnostic scripts: the image rootfs is read-only and `scripts/` is
> not baked in, so `scp` the script to `/srv/hem/data/` and run
> `docker exec hem python /app/data/<script>.py`.

---

## Deploy checklist

Deploy = **pin a new image tag and restart**. Never `git pull` on the host.

```bash
# 1. Wait for CI to publish the image for the SPECIFIC commit you want.
#    Check the run for that SHA in .github/workflows/docker-publish.yml.

# 2. Guard the manifest BEFORE touching anything — a piped `docker pull` that
#    fails silently followed by a restart is an outage.
docker manifest inspect ghcr.io/albinati/home-energy-manager:sha-<sha> >/dev/null

# 3. Pin the tag and restart.
sed -i "s|^HEM_IMAGE_TAG=.*|HEM_IMAGE_TAG=sha-<sha>|" /srv/hem/.compose.env
systemctl restart hem
```

Same shape for the UI (`HEM_UI_IMAGE_TAG`, image
`ghcr.io/albinati/home-energy-manager-ui`) — and use
`docker compose up -d --no-deps hem-ui` if you want to redeploy the UI without
bouncing the control loop.

**After deploy, verify:**
1. `curl http://127.0.0.1:8000/api/v1/health` → `{"status":"ok"}` and the expected `revision`
2. `docker exec hem cat /app/.git-sha` — matches the SHA you pinned
3. `journalctl -u hem -n 50` — no Python errors
4. `curl http://127.0.0.1:8000/api/v1/optimization/status` → backend + preset look right
5. Trigger a fresh plan (admin bearer required): `POST /api/v1/optimization/propose` → simulate + (auto-)apply → Fox V3 upload

**Rollback:** re-pin the previous `HEM_IMAGE_TAG` and `systemctl restart hem`.
Full procedure in `deploy/README.md` §8.

---

## .env key settings

| Variable | Production value | Notes |
|----------|-----------------|-------|
| `OPENCLAW_READ_ONLY` | `false` | **The only hardware-write kill switch.** `true` blocks all Fox/Daikin writes. |
| `PLAN_AUTO_APPROVE` | `true` (default) | Each plan is simulated, then auto-applied. Set `false` to require explicit `confirm_plan` / `reject_plan` per cycle. |
| `PLAN_APPROVAL_TIMEOUT_SECONDS` | `300` | Grace window advertised to OpenClaw for Telegram/Discord accept/reject buttons (auto-accept on timeout). |
| `PLAN_REGEN_COOLDOWN_SECONDS` | `300` | Prevents spam re-planning |
| `DAIKIN_DAILY_BUDGET` | `180` | Hard cap below Daikin's 200/day limit |
| `DAIKIN_HTTP_429_MAX_RETRIES` | `0` | **Must be set explicitly — the code default is `3`.** Daikin sets `Retry-After: ~86400` on daily-limit exhaustion, so a retrying client hangs for hours on startup. |
| `FOX_DAILY_BUDGET` | `1200` | Conservative cap below Fox's 1440/day |
| `LP_MPC_WRITE_DEVICES` | `false` | Force device writes on *manual* re-plans too; event triggers (drift, tier boundary, Octopus fetch…) always push to hardware regardless |
| `FORECAST_SOURCE` | `open_meteo` | Set to `quartz` to use Quartz PV nowcasts; Open-Meteo remains weather fallback/context |
| `QUARTZ_USERNAME` / `QUARTZ_PASSWORD` | required for Quartz | Auth0 login used to fetch the Quartz bearer token |
| `QUARTZ_CLIENT_ID` / `QUARTZ_AUDIENCE` | defaults in code | Quartz Auth0 client settings |
| `QUARTZ_GSP_ID` | optional | Hosted Quartz GSP mapping; omit for national default |
| `QUARTZ_MODEL_NAME` | `blend` | Quartz model selector for hosted API |
| `QUARTZ_TREND_ADJUSTER_ON` | `true` | Quartz trend-adjuster toggle |
| `QUARTZ_INSTALLED_CAPACITY_MW` | `0` | Optional downscale hint when Quartz returns capacity metadata |
| `LP_SOLAR_CHARGE_FOX_MODE` | `selfuse` | Fox mode for `solar_charge` slots (#679). `selfuse` (default) = plain SelfUse at reserve — PV fills, inverter NEVER auto-imports; rare discharge leak accepted (~1/30 days, handled at LP level). `backup_hold` = Backup(reserve,reserve) — strict no-discharge hold that also blocks the PV fill; same tuple as A1 pre-peak holds. `backup_fill` = Backup(reserve, LP-target) — PV fills toward target BUT **grid-imports toward maxSoc on fw < 1.55 (our H1 is 1.51); do NOT enable until fw ≥ 1.55**. On fw 1.51 Backup grid-import is maxSoc-driven, not minSoc-driven. The retired SelfUse(100,100) shape is never emittable (A0). A structural guard (`_guard_nonneg_backup_maxsoc`) clamps any Backup maxSoc > live SoC at a positive price → reserve. |
| `LP_POSITIVE_HOLD_ENABLED` | `true` | Honour LP battery-hold decisions at positive prices via pinned Backup (#679). `false` = byte-identical legacy. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | set | **The notification transport in use.** When both are set, HEM POSTs straight to `api.telegram.org` and OpenClaw is never called for messaging. |
| `OPENCLAW_HOOKS_URL` | fallback only | Full URL, e.g. `http://127.0.0.1:18789/hooks/agent`. Used **only** when Telegram is unconfigured. |
| `OPENCLAW_HOOKS_TOKEN` | fallback only | Same secret as Gateway `hooks.token` |
| `OPENCLAW_INTERNAL_API_BASE_URL` | `http://127.0.0.1:8000` | Inserted into hook payload so the agent can `GET /api/v1/optimization/plan` |

**The `.env` file is at `/srv/hem/.env`** (mounted read-only into the
container). Update it on the host, then `systemctl restart hem`.

Perms matter: the container reads it as uid 1001, so it must stay
`640 root:1001` — `chmod 600` breaks startup.

Use Python for safe edits (avoids shell quoting issues):

```bash
python3 - <<'EOF'
import re
f = "/srv/hem/.env"
content = open(f).read()
def set_key(text, key, value):
    p = rf"^{re.escape(key)}=.*$"
    r = f"{key}={value}"
    return re.sub(p, r, text, flags=re.MULTILINE) if re.search(p, text, re.MULTILINE) else text + f"\n{key}={value}\n"
content = set_key(content, "PLAN_AUTO_APPROVE", "true")
open(f, "w").write(content)
print("done")
EOF
systemctl restart hem
```

> `HEM_IMAGE_TAG` is **not** read from `.env` — it lives in
> `/srv/hem/.compose.env` (the systemd `EnvironmentFile`).

---

## DB: important tables

The active database is `/srv/hem/data/energy_state.db` on the host, bind-mounted
to `/app/data/energy_state.db` inside the container (`DB_PATH` in `.env`).

| Table | What it holds |
|-------|--------------|
| `agile_rates` | Octopus Agile half-hourly rates. Columns: `valid_from`, `valid_to`, `value_inc_vat`. |
| `action_schedule` | Daikin scheduled actions (pre_heat, shutdown, restore). Columns: `date`, `start_time`, `end_time`, `action_type`, `params`, `status`. |
| `action_log` | Every executed action + result. Audit trail. |
| `optimizer_log` | One row per optimizer run: `rates_count`, `cheap_slots`, `peak_slots`, `daikin_actions_count`, `fox_schedule_uploaded`. |
| `api_call_log` | Every cloud API call. Columns: `vendor` (fox/daikin), `kind`, `ts_utc`, `ok`. Use for quota auditing. |
| `fox_schedule_state` | Last uploaded Fox V3 schedule JSON. |
| `plan_consent` | Plan IDs, status (pending_approval/approved), hash. Note: Bulletproof plans are `applied` immediately; this table is for consent tracking + MCP visibility. |
| `execution_log` | Per-slot energy telemetry: consumption, price, SoC, cost vs shadow. |

### Useful queries

```python
# Today's rates
conn.execute("SELECT valid_from, value_inc_vat FROM agile_rates WHERE date(valid_from) >= date('now','localtime') ORDER BY valid_from").fetchall()

# Today's schedule
conn.execute("SELECT start_time, end_time, action_type, status FROM action_schedule WHERE date = date('now','localtime') ORDER BY start_time").fetchall()

# API quota last 24h
import datetime
since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).timestamp()
conn.execute("SELECT vendor, kind, COUNT(*) FROM api_call_log WHERE ts_utc > ? GROUP BY vendor, kind", (since,)).fetchall()

# Fox V3 schedule
import json
row = conn.execute("SELECT uploaded_at, groups_json FROM fox_schedule_state ORDER BY uploaded_at DESC LIMIT 1").fetchone()
print(json.dumps(json.loads(row[1]), indent=2))
```

---

## What the Bulletproof engine does on each optimizer run

1. Reads Agile rates from `agile_rates` table
2. Fetches weather / PV forecast (Quartz when configured, otherwise Open-Meteo)
3. Runs PuLP MILP solver → produces per-slot Fox + Daikin actions
4. **Uploads Fox Scheduler V3 immediately** (one call, `fox_schedule_uploaded=1` in optimizer_log)
5. Writes Daikin `action_schedule` rows for today
6. Stores plan in `plan_consent` with `status: pending_approval`
7. Sends a user notification (OpenClaw Gateway hook → your channel, e.g. Telegram) with the full schedule

**The Fox V3 schedule is already uploaded at propose time.** `confirm_plan` is user acknowledgement — it does not re-upload anything. This was confirmed live on 2026-04-19.

### Heartbeat (every `HEARTBEAT_INTERVAL_SECONDS`, default 300 s)

- Reads the Daikin **cache only** — `allow_refresh=False`, never a cloud call
- Reconciles `action_schedule`: executes any action whose `start_time ≤ now < end_time` and `status = pending`
- Logs each execution to `action_log`
- Every ~30 min: re-checks Fox scheduler flag and re-uploads V3 if it differs from DB (`heartbeat_reupload_scheduler_v3`)

---

## Notifications

`src/notifier.py` picks the transport **at delivery time**:

1. **`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` set → direct Telegram.** HEM POSTs
   to `https://api.telegram.org/bot<TOKEN>/sendMessage` via
   `src/telegram_transport.py`. **This is the production path**; OpenClaw is not
   called at all for messaging (it used to pay for an Anthropic API call inside
   OpenClaw on every brief just to re-shape Markdown HEM had already formatted).
2. **Telegram unset, `OPENCLAW_HOOKS_URL` + `OPENCLAW_HOOKS_TOKEN` set →** the
   legacy LLM-shaped hook path below. **Fallback only.**
3. **Neither configured →** stdout + `action_log` only.

`OPENCLAW_NOTIFY_ENABLED=false` is the master mute for both transports.
Per-`AlertType` routing lives in the `notification_routes` SQLite table;
`target_override` / `channel_override` apply **only** to the OpenClaw fallback —
the Telegram chat is global.

To roll back to the hook path, unset `TELEGRAM_BOT_TOKEN` (or
`TELEGRAM_CHAT_ID`) and restart `hem.service`.

### Fallback path — OpenClaw Gateway hooks

Official reference: **[Webhooks](https://docs.openclaw.ai/automation/cron-jobs#webhooks)** (same page also documents Gateway cron jobs—Home Energy Manager only uses the **HTTP hooks**, not `openclaw cron`).

When Telegram is unconfigured, `OPENCLAW_NOTIFY_ENABLED=true`, and a notify **target** is resolved (env or `notification_routes`), the service **`POST`s to `OPENCLAW_HOOKS_URL`** using the same contract as **`POST /hooks/agent`** in the docs: JSON body with `message`, `name`, `wakeMode`, `deliver`, `channel`, `to` (from your route), optional `agentId`, `timeoutSeconds`, and header **`Authorization: Bearer <token>`** (the docs also allow `x-openclaw-token`; we use Bearer only). There is no `openclaw message send` subprocess. Delivery runs in a background thread.

If the hook returns non-2xx or the request errors, the service logs **`[openclaw hooks] delivery failed`** — fix Gateway config or network; stdout + `action_log` still contain the notification text.

**Removed (breaking):** `OPENCLAW_CLI_PATH`, `OPENCLAW_CLI_TIMEOUT_SECONDS`, and `OPENCLAW_PLAN_NOTIFY_MODE`. Migrate: set hooks URL + token to match your Gateway.

#### Gateway `hooks.token` and Home Energy `OPENCLAW_HOOKS_TOKEN`

There is no separate “password from OpenClaw”—you **define one shared secret** and configure it in two places:

1. **OpenClaw Gateway** (`~/.openclaw/openclaw.json` or your config path): enable hooks and set `token` to a long random string (example shape from the docs):

   ```json5
   {
     hooks: {
       enabled: true,
       token: "your-shared-secret-here",
       path: "/hooks",
     },
   }
   ```

   Generate a secret locally, e.g. `openssl rand -hex 32`.

2. **Home Energy Manager `.env`:** set `OPENCLAW_HOOKS_TOKEN` to the **same** value as `hooks.token`.

3. **`OPENCLAW_HOOKS_URL`:** full URL to the agent hook, usually `http://127.0.0.1:18789/hooks/agent` if the Gateway listens on `18789` and uses the default `path` of `/hooks` (adjust host/port if the Gateway runs elsewhere).

Until both are set and the Gateway is running, notifications are only logged to stdout / `action_log`.

### Alert types and what triggers them

| Alert type | Trigger |
|-----------|---------|
| `plan_proposed` | New optimizer plan proposed |
| `strategy_update` | Strategy summary on startup/refresh |
| `cheap_window_start` | Cheap Agile window begins |
| `peak_window_start` | Peak Agile window begins |
| `morning_report` | 08:00 daily brief |
| `daily_pnl` | End-of-day P&L report |
| `risk_alert` | Low SoC + peak price, Fox scheduler mismatch |
| `action_confirmation` | Manual change applied / plan auto-approved |
| `critical_error` | Service error |

Configure routing via MCP: `set_notification_route(alert_type, enabled, severity, target_override)`.

**Nikola / main agent vs. hook deliveries:** Your interactive agent (e.g. Nikola) that uses the **home-energy-manager MCP** in chat is unchanged. Automated notifications are **separate `/hooks/agent` turns** for digest/delivery. To avoid mixing personas, set **`OPENCLAW_HOOKS_AGENT_ID`** to a **dedicated** digest-only agent in the Gateway; if empty, the Gateway default hook agent applies (see `hooks.allowedAgentIds`). See [docs/openclaw-nikola-plan-prompt.md](openclaw-nikola-plan-prompt.md).

**Gateway prerequisites:** enable `hooks` in OpenClaw config with a dedicated `hooks.token`, bind to loopback or Tailscale, and restrict `allowedAgentIds` if you use `OPENCLAW_HOOKS_AGENT_ID`.

**Manual test (after hooks are enabled):** replace `YOUR_HOOKS_TOKEN` and add your Telegram chat id as `to` if your Gateway requires a destination (same as production payloads):

```bash
curl -fsS -X POST http://127.0.0.1:18789/hooks/agent \
  -H "Authorization: Bearer YOUR_HOOKS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Ping from curl — reply with one line.","name":"Test","wakeMode":"now","deliver":true,"channel":"telegram","to":"YOUR_TELEGRAM_CHAT_ID","timeoutSeconds":60}'
```

**Agent prompt template:** see [docs/openclaw-nikola-plan-prompt.md](openclaw-nikola-plan-prompt.md).

---

## Cursor IDE (local checklist)

Seeing multiple entries such as “Cursor” and “Cursor-agent” can be normal (Composer vs Agent, or different profiles). To verify the setup:

- Open **Cursor Settings → Agent / MCP** and confirm the **home-energy-manager** MCP is listed and connects.
- Run a small **Agent** task that edits one file and confirm the diff applies.
- If two entries duplicate behaviour, disable the unused profile or extension.

---

## Daikin behaviour notes

- **Weather regulation** (`weather_regulation: true`): the heat pump controls LWT based on outdoor temperature. **Room temperature setpoint is ignored** — use `lwt_offset` to adjust heating intensity.
- **Manual changes are RESPECTED, not overwritten** (Epic 14, #386). When you
  change the tank in the Onecta app or on the unit, the reactive detector in
  `src/daikin_bulletproof.py` stamps `overridden_by_user_at` on the active
  `action_schedule` row, and the pre-fire reconciler in `src/state_machine.py`
  then **inherits** that override onto subsequent non-`restore` rows for the
  same device — for `USER_OVERRIDE_RESPECT_HOURS` (default 4 h), or, when
  `USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END=true` (default), for as long as the
  overridden row's own `end_time` is in the future. The safety gate is a live
  re-check: revert the manual change and HEM resumes at once. `restore` rows are
  exempt so the system can always return to baseline. Gated by
  `PREFIRE_STATE_MATCH_ENABLED` (default true). Telemetry: `prefire_state_match`
  and `prefire_override_inherited` in `action_log`.
  > An earlier revision of this runbook said the opposite ("manual changes are
  > overwritten by the next heartbeat"). That has not been true since #386.
- **`tank_power` / `tank_powerful`**: The Daikin device model does not expose live values for these booleans. The system conservatively always writes them when scheduled (cannot confirm current state from cache).

### Daikin quota protection

- **The heartbeat NEVER calls the Daikin API.** It passes `allow_refresh=False`
  unconditionally (`src/scheduler/runner.py`) — the Onecta 200-call/day quota is
  too tight to spend on a monitoring path. The cache stays warm via plan
  dispatch, the twice-daily briefs, and manual/MCP calls.
  > `DAIKIN_CALIBRATION_WINDOWS_LOCAL` and the "live `get_devices()` in the
  > Octopus pre-slot window" behaviour that earlier revisions of this runbook
  > described are **dead**: `_in_daikin_calibration_window` /
  > `_in_octopus_pre_slot_window` still exist in `runner.py` but nothing calls
  > them, and setting the env var changes nothing.
- Manual/MCP refreshes throttled to 30 min per actor
- Budget: `DAIKIN_DAILY_BUDGET=180` (hard cap: 200/day from Daikin Onecta)

---

## Fox ESS behaviour notes

- **Scheduler V3** is the primary control: time-period based work mode schedule, uploaded once per optimizer run
- **`work_mode: unknown`** in the status response is normal — it reflects the cached telemetry snapshot, not necessarily the V3 schedule state. The inverter operates on V3 independently.
- **`heartbeat_reupload_scheduler_v3`** in the action log means the heartbeat detected a mismatch between the DB schedule and the inverter and re-uploaded. This is normal and expected.
- Budget: `FOX_DAILY_BUDGET=1200` (hard cap: ~1440/day)

### ForceCharge `fdPwr` (grid import power) — LP vs heuristic

The Fox V3 `fdPwr` parameter tells the inverter how many Watts to draw from the grid during a ForceCharge window.

**LP backend (default):** `fdPwr` is derived from the MILP solution — specifically `grid_import_kwh[slot] × 2000 W` rounded up to the nearest 50 W. The LP already accounts for PV generation and home load when it decides how much to import, so `fdPwr` ends up being only what the grid actually needs to contribute. During a sunny morning cheap window this may be quite low (PV covers most of the charge); overnight with no PV it will be higher.

**Heuristic backend:** Uses static constants — `FOX_FORCE_CHARGE_MAX_PWR` (6000 W default) for negative-price slots, `FOX_FORCE_CHARGE_NORMAL_PWR` (3000 W default) for cheap slots. These are upper bounds and may request more grid import than necessary when PV is present.

**Configuration:**
- `FOX_FORCE_CHARGE_MAX_PWR` — hard ceiling (W). Set to your inverter's AC charge rating. Used by both backends as the per-slot cap.
- `FOX_FORCE_CHARGE_NORMAL_PWR` — heuristic-only fallback. Irrelevant when `OPTIMIZER_BACKEND=lp` (the default).
- `MAX_INVERTER_KW` — LP model constraint (kW). Must match your inverter nameplate. The LP will never plan more than `MAX_INVERTER_KW × 0.5 kWh` per slot regardless.


### MPC re-plan schedule

Re-planning is **event-driven** (the fixed-hour `LP_MPC_HOURS` cron was removed in V12):

| Trigger | When | Purpose |
|---|---|---|
| Octopus fetch job | ~16:05 local | **Critical**: tomorrow's rates arrive → LP replans the full horizon, adjusting tonight's discharge and overnight cheap strategy |
| `tier_boundary` | `TIER_BOUNDARY_LEAD_MINUTES` (5 min) before each tariff tier transition | Re-plan exactly when the price regime changes |
| `soc_drift` / `import_overshoot` / `pv_upside` / `pv_downside` / `load_upside` / `forecast_revision` | 5-min heartbeat / forecast refresh, threshold-gated | Live state or forecast diverged from the committed plan |
| `dynamic_replan` | one-shot when the plan was truncated to the Fox 8-group cap | Re-plan the truncated tail |
| Nightly push | 00:05 UTC (`LP_PLAN_PUSH_HOUR:MINUTE`) | Full next-day plan with final Agile rates |

Each re-plan pushes the revised Fox V3 schedule to hardware; identical schedules are skipped at the client layer.

### Fox V3 schedule structure

```json
[
  {"startHour": 0,  "startMinute": 0,  "endHour": 8,  "endMinute": 59,
   "workMode": "SelfUse",    "extraParam": {"minSocOnGrid": 10}},
  {"startHour": 9,  "startMinute": 0,  "endHour": 10, "endMinute": 30,
   "workMode": "SelfUse",    "extraParam": {"minSocOnGrid": 10}},
  {"startHour": 10, "startMinute": 30, "endHour": 10, "endMinute": 59,
   "workMode": "ForceCharge","extraParam": {"minSocOnGrid": 10, "fdSoc": 95, "fdPwr": 1150}},
  {"startHour": 13, "startMinute": 0,  "endHour": 14, "endMinute": 59,
   "workMode": "ForceCharge","extraParam": {"minSocOnGrid": 10, "fdSoc": 95, "fdPwr": 4700}},
  {"startHour": 15, "startMinute": 0,  "endHour": 16, "endMinute": 59,
   "workMode": "Backup",     "extraParam": {"minSocOnGrid": 10, "maxSoc": 10}},
  {"startHour": 17, "startMinute": 0,  "endHour": 21, "endMinute": 30,
   "workMode": "SelfUse",    "extraParam": {"minSocOnGrid": 10}}
]
```

Group end-minutes are **`:59` inclusive** for a full hour and **`:30` exclusive**
for a half-hour; any "which group covers minute *X*" logic must use
`gs <= m < ge` on local wall-clock — reuse `derive_fox_mode_from_schedule`
rather than re-deriving it.

> ⚠️ **`SelfUse minSocOnGrid=100` is RETIRED — never emit it.** It used to be
> the `solar_charge` shape ("battery holds charge, PV fills it"). **The H1
> firmware ignores the SelfUse group floor** and discharges straight through it:
> across 40,369 prod samples the battery discharged *below* that floor 40.6 % of
> the time. It was the 2026-07-10 leak, not a hold, and it is no longer emittable
> anywhere in the code (`_slot_fox_tuple` in `src/scheduler/optimizer.py`).
>
> Today `solar_charge` maps to **plain `SelfUse` at `MIN_SOC_RESERVE_PERCENT`**
> (`LP_SOLAR_CHARGE_FOX_MODE=selfuse`, the default): PV fills the battery and
> the inverter never auto-imports. The only *proven* no-discharge hold primitive
> on our hardware is **`Backup`** — that's what the LP's positive-price holds and
> `negative_hold` slots emit. Only the global reserve (`minSocOnGrid`) is honoured
> as a real floor; the hardware cannot do intermediate partial-discharge floors.

---

## Common operational scenarios

### System starts up after restart — what to check

```bash
# 1. Is it healthy?
curl http://127.0.0.1:8000/api/v1/health

# 2. Did it load today's rates?
curl http://127.0.0.1:8000/api/v1/optimization/status | python3 -m json.tool

# 3. Was a plan proposed? (check logs)
journalctl -u hem --since '5 minutes ago' --no-pager | grep 'plan_proposed\|MILP\|Fox\|Daikin'

# 4. If no plan yet, trigger one
curl -X POST http://127.0.0.1:8000/api/v1/optimization/propose -H 'Content-Type: application/json' -d '{}'
```

### Rates not loaded for today

The Octopus fetch runs at `OCTOPUS_FETCH_HOUR=16` (4pm) daily. If rates are missing:

```bash
# Force a fetch + re-plan
curl -X POST http://127.0.0.1:8000/api/v1/optimization/fetch-and-plan -H 'Content-Type: application/json' -d '{}'
```

### Daikin not responding to schedule

```bash
# Check action_schedule status
curl http://127.0.0.1:8000/api/v1/schedule | python3 -m json.tool

# Check action_log for errors
# (via MCP: get_action_log)

# Check Daikin quota
curl http://127.0.0.1:8000/api/v1/daikin/quota
```

### Fox ESS stuck in wrong mode

```bash
# Emergency: force Self Use (safe default)
curl -X POST http://127.0.0.1:8000/api/v1/foxess/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode":"Self Use","skip_confirmation":true}'

# Then re-propose to reload V3
curl -X POST http://127.0.0.1:8000/api/v1/optimization/propose \
  -H 'Content-Type: application/json' -d '{}'
```

### Temporarily freeze hardware writes (dry-run / panic stop)

`OPERATION_MODE` is gone. The only kill switch is `OPENCLAW_READ_ONLY`. To stop all
Fox/Daikin writes without restarting the service: nothing — it's an env-var gate
read at call time, so changing `.env` + restart picks it up persistently. For an
ad-hoc panic stop, prefer `POST /api/v1/scheduler/pause` (pauses the cron engine)
and emergency-force Fox to Self Use (see scenario above).

```bash
# Persistent dry-run: flip the kill switch in .env and restart
python3 - <<'EOF'
import re
f = "/srv/hem/.env"
t = open(f).read()
t = re.sub(r"^OPENCLAW_READ_ONLY=.*$", "OPENCLAW_READ_ONLY=true", t, flags=re.MULTILINE)
open(f, "w").write(t)
EOF
systemctl restart hem
```

To re-enable writes: flip back to `false` and restart.

---

## Systemd unit notes

File: `/etc/systemd/system/hem.service` (source of truth: `deploy/hem.service`).
It is a thin wrapper around `docker compose -f /srv/hem/compose.yaml up`.

- `EnvironmentFile=/srv/hem/.compose.env` — supplies `HEM_IMAGE_TAG`,
  `HEM_UI_IMAGE_TAG`, `HEM_TAILSCALE_IP` to compose interpolation. Application
  config comes from `/srv/hem/.env`, which compose mounts read-only into the
  container.
- Container hardening (read-only rootfs, `tmpfs /tmp`, `cap_drop`, memory limits)
  is declared in `deploy/compose.yaml`, not in the unit.
- Logs go to journald (`journalctl -u hem`).

After editing the unit file: `systemctl daemon-reload && systemctl restart hem`.
After editing `compose.yaml` or `.compose.env`: `systemctl restart hem` (compose
down + up).

---

## Backup

Everything worth backing up lives in `/srv/hem/` — the code is in the image.

```bash
# DB snapshot (safe while the container is running)
docker exec hem python -c "
import sqlite3
src = sqlite3.connect('/app/data/energy_state.db')
dst = sqlite3.connect('/app/data/backup-energy_state.db')
src.backup(dst); dst.close(); src.close()
print('ok')"

# Then pull it off-server (also grab .env + tokens — they are NOT in the image)
scp root@<hem-host>.ts.net:/srv/hem/data/backup-energy_state.db  ./
scp root@<hem-host>.ts.net:/srv/hem/.env                          ./
scp root@<hem-host>.ts.net:'/srv/hem/data/.daikin-tokens.json'    ./
```

> `scripts/deploy_hetzner.sh` is the **pre-cutover, native-systemd** deploy
> script. It is not used in prod any more (there is no editable checkout on the
> host) — kept in-repo for history only.
