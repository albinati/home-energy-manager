# Operational Runbook

Day-to-day operations, deployment, and debugging for the Home Energy Manager on Hetzner.

---

## Infrastructure

| Component | Detail |
|-----------|--------|
| Server | Hetzner (Tailscale: `openclaw-overbot.tail0dbf20.ts.net`, `100.104.115.85`) |
| SSH | `ssh root@openclaw-overbot.tail0dbf20.ts.net` |
| Project dir | `/root/home-energy-manager` |
| Active DB | `/root/home-energy-manager/data/energy_state.db` ← **not** the project root |
| Venv | `/root/home-energy-manager/.venv` |
| Service | `systemd: home-energy-manager.service` |
| Logs | `journalctl -u home-energy-manager -f` |
| API | `http://127.0.0.1:8000` (local only) |

### Common commands

```bash
# Status
systemctl is-active home-energy-manager
curl -fsS http://127.0.0.1:8000/api/v1/health

# Logs (tail)
journalctl -u home-energy-manager -n 100 --no-pager

# Restart (after .env changes — picks up new env)
systemctl restart home-energy-manager

# DB queries (use the data/ path)
cd /root/home-energy-manager
.venv/bin/python3 -c "import sqlite3; conn = sqlite3.connect('data/energy_state.db'); ..."

# Deploy
cd /root/home-energy-manager && ./scripts/deploy_hetzner.sh --backup
```

---

## Deploy checklist

Run from Hetzner (or via SSH):

```bash
./scripts/deploy_hetzner.sh --backup
```

The script: backs up DB → git pull → pip install → DB migration → restart → health check → Fox ESS safety reset.

**After deploy, verify:**
1. `curl http://127.0.0.1:8000/api/v1/health` → `{"status":"ok"}`
2. `journalctl -u home-energy-manager -n 20` — no Python errors
3. `curl http://127.0.0.1:8000/api/v1/optimization/status` → `operation_mode` is what you expect
4. If going operational: `curl -X POST http://127.0.0.1:8000/api/v1/optimization/propose -H 'Content-Type: application/json' -d '{}'` → triggers Fox V3 upload

**Known deploy timing quirk:** The health check in the deploy script uses `TimeoutStopSec=150` for the systemd unit (set 2026-04-19). The service takes ~25–30 s to become healthy after restart. The script waits 30 s max — if it says "health check not passing", the service is usually fine; check with `curl` manually.

---

## .env key settings

| Variable | Production value | Notes |
|----------|-----------------|-------|
| `OPERATION_MODE` | `operational` | `simulation` = no hardware writes |
| `PLAN_AUTO_APPROVE` | `false` | Set `true` only after several stable days |
| `PLAN_REGEN_COOLDOWN_SECONDS` | `300` | Prevents spam re-planning |
| `OPENCLAW_READ_ONLY` | `false` | Must be `false` for hardware writes via MCP |
| `OPENCLAW_CLI_TIMEOUT_SECONDS` | `180` | openclaw Telegram delivery takes ~50–120 s |
| `DAIKIN_DAILY_BUDGET` | `180` | Hard cap below Daikin's 200/day limit |
| `FOX_DAILY_BUDGET` | `1200` | Conservative cap below Fox's 1440/day |
| `OPENCLAW_PLAN_NOTIFY_MODE` | `direct` | Set `webhook` to POST plan events to OpenClaw Gateway `/hooks/agent` (agent summarizes before Telegram) |
| `OPENCLAW_HOOKS_URL` | (empty) | Full URL, e.g. `http://127.0.0.1:18789/hooks/agent` |
| `OPENCLAW_HOOKS_TOKEN` | (empty) | Same secret as Gateway `hooks.token` |
| `OPENCLAW_INTERNAL_API_BASE_URL` | `http://127.0.0.1:8000` | Inserted into hook payload so the agent can `GET /api/v1/optimization/plan` |

**The `.env` file is at `/root/home-energy-manager/.env`. Update it, then restart the service.**

Use Python for safe edits (avoids shell quoting issues):

```bash
python3 - <<'EOF'
import re
f = "/root/home-energy-manager/.env"
content = open(f).read()
def set_key(text, key, value):
    p = rf"^{re.escape(key)}=.*$"
    r = f"{key}={value}"
    return re.sub(p, r, text, flags=re.MULTILINE) if re.search(p, text, re.MULTILINE) else text + f"\n{key}={value}\n"
content = set_key(content, "OPERATION_MODE", "operational")
open(f, "w").write(content)
print("done")
EOF
```

---

## DB: important tables

The active database is always `data/energy_state.db` (set via `Environment=DB_PATH=...` in the systemd unit).

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
2. Fetches weather forecast (Open-Meteo)
3. Runs PuLP MILP solver → produces per-slot Fox + Daikin actions
4. **Uploads Fox Scheduler V3 immediately** (one call, `fox_schedule_uploaded=1` in optimizer_log)
5. Writes Daikin `action_schedule` rows for today
6. Stores plan in `plan_consent` with `status: pending_approval`
7. Sends Telegram notification with the full schedule

**The Fox V3 schedule is already uploaded at propose time.** `confirm_plan` is user acknowledgement — it does not re-upload anything. This was confirmed live on 2026-04-19.

### Heartbeat (every 2 minutes)

- Reads Daikin cache (no cloud call unless stale)
- Reconciles `action_schedule`: executes any action whose `start_time ≤ now < end_time` and `status = pending`
- Logs each execution to `action_log`
- Every ~30 min: re-checks Fox scheduler flag and re-uploads V3 if it differs from DB (`heartbeat_reupload_scheduler_v3`)

---

## Notifications (OpenClaw / Telegram)

Notifications go via `openclaw message send` subprocess. The Telegram round-trip takes **50–120 seconds** — this is normal. Daemon threads handle delivery; the app never blocks.

**Known issue (2026-04-19):** `[openclaw timeout]` log lines appear when the delivery thread hits `OPENCLAW_CLI_TIMEOUT_SECONDS`. Bumped to `180` in production. The messages **do** arrive on Telegram despite the log warning.

**If `openclaw` fails with "Channel is required":** The server has multiple channels configured (discord + telegram). Always pass `--channel telegram` explicitly. The notifier code already does this via `OPENCLAW_NOTIFY_CHANNEL=telegram`.

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

### OpenClaw Gateway webhook (optional) — agent “mastiga” o plano

When `OPENCLAW_PLAN_NOTIFY_MODE=webhook` and `OPENCLAW_HOOKS_URL` + `OPENCLAW_HOOKS_TOKEN` are set, **`plan_proposed` notifications** are sent with `POST` to the Gateway (default path `/hooks/agent` per [OpenClaw Webhooks](https://openclaws.io/docs/automation/webhook)) instead of piping the long body through `openclaw message send`. The payload asks your agent (e.g. Nikola) to summarize in human language; **if the hook fails (non-2xx or network error), the service falls back to the direct CLI** so you still get a message.

**Gateway prerequisites:** enable `hooks` in OpenClaw config with a dedicated `hooks.token`, bind to loopback or Tailscale, and restrict `allowedAgentIds` if you use `OPENCLAW_HOOKS_AGENT_ID`.

**Manual test (after hooks are enabled):**

```bash
curl -fsS -X POST http://127.0.0.1:18789/hooks/agent \
  -H "Authorization: Bearer YOUR_HOOKS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Ping from curl — reply with one line.","name":"Test","wakeMode":"now","deliver":true,"channel":"telegram","timeoutSeconds":60}'
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
- **Manual changes are temporary.** Any manual tank/LWT change made outside the schedule will be overwritten by the next heartbeat action. This is intentional — the system always restores to the planned state.
- **Tank at 60°C manually set:** The `restore` action in the schedule resets it to 50°C at the configured time. No manual cleanup needed.
- **`tank_power` / `tank_powerful`**: The Daikin device model does not expose live values for these booleans. The system conservatively always writes them when scheduled (cannot confirm current state from cache).

### Daikin quota protection

- Live `get_devices()` call only in the 5-minute pre-slot window (`:25–:30` and `:55–:00` BST/UTC)
- Heartbeat uses cached state only
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


### Fox V3 schedule structure (example from 2026-04-19)

```json
[
  {"startHour": 12, "startMinute": 30, "endHour": 14, "endMinute": 59,
   "workMode": "ForceCharge", "extraParam": {"minSocOnGrid": 10, "fdSoc": 95}},
  {"startHour": 15, "startMinute": 0, "endHour": 22, "endMinute": 59,
   "workMode": "SelfUse", "extraParam": {"minSocOnGrid": 10}}
]
```

This means: charge to 95% SoC before peak (15:00–18:30), then self-use through peak.

---

## Common operational scenarios

### System starts up after restart — what to check

```bash
# 1. Is it healthy?
curl http://127.0.0.1:8000/api/v1/health

# 2. Did it load today's rates?
curl http://127.0.0.1:8000/api/v1/optimization/status | python3 -m json.tool

# 3. Was a plan proposed? (check logs)
journalctl -u home-energy-manager --since '5 minutes ago' --no-pager | grep 'plan_proposed\|MILP\|Fox\|Daikin'

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

### Going back to simulation mode

```bash
# Via API
curl -X POST http://127.0.0.1:8000/api/v1/optimization/mode \
  -H 'Content-Type: application/json' -d '{"mode":"simulation"}'

# Or edit .env + restart (persistent across restarts)
# python3 set_key script → OPERATION_MODE=simulation → systemctl restart home-energy-manager
```

---

## Systemd unit notes

File: `/etc/systemd/system/home-energy-manager.service`

Key settings as of 2026-04-19:
- `TimeoutStopSec=150` — allows openclaw notification threads to complete on shutdown (was 30, caused SIGKILL of openclaw processes)
- `MemoryHigh=200M / MemoryMax=400M` — PuLP optimizer can spike memory during solve
- `EnvironmentFile=/root/home-energy-manager/.env` — all config from `.env`
- `Environment=DB_PATH=/root/home-energy-manager/data/energy_state.db` — overrides `.env` DB_PATH to use `data/` subdirectory

After editing the unit file: `systemctl daemon-reload`

---

## Backup

```bash
# Backup DB to local (keeps 3 copies on Hetzner)
./scripts/deploy_hetzner.sh --backup-only

# To send backups off-server via Tailscale (saves Hetzner disk space):
export LOCAL_BACKUP_DEST='root@over-surface.tail0dbf20.ts.net:/root/em-backups'
./scripts/deploy_hetzner.sh --backup-only
# Requires SSH server running on over-surface (see deploy script comments)
```
