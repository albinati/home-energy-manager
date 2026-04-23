# home-energy-manager — Claude context

## Deployment (native, no Docker)

As of 2026-04-18 the service runs **natively on the host as root** — Docker has been removed.

| Thing | Path / value |
|---|---|
| Python | `/root/home-energy-manager/.venv/bin/python` (3.12.3) |
| SQLite DB | `/root/home-energy-manager/data/energy_state.db` |
| Daikin token | `/root/home-energy-manager/data/.daikin-tokens.json` |
| API server | `http://127.0.0.1:8000` |
| Systemd unit | `home-energy-manager.service` |
| Config | `/root/home-energy-manager/.env` |
| Run command | `.venv/bin/python -m src.cli serve` |

### Service management

```bash
systemctl status home-energy-manager
systemctl restart home-energy-manager
journalctl -u home-energy-manager -f          # live logs
curl http://127.0.0.1:8000/api/v1/health      # quick health check
```

### BOOT.md is outdated — ignore it
The old BOOT.md refers to a `venv/` and `daemon start`. The active venv is `.venv/`, the service is systemd-managed, and `src.cli serve` is the entrypoint (not `daemon start`).

---

## Daikin Onecta — token management

Tokens live at `data/.daikin-tokens.json`. The access token expires every **3 hours**; the service auto-refreshes it via the refresh_token as long as the refresh_token is valid (~30 days).

### Refresh access token (refresh_token still valid)

```bash
cd /root/home-energy-manager
.venv/bin/python - <<'EOF'
import json, time
from src.daikin.auth import refresh_tokens

tokens = json.load(open("data/.daikin-tokens.json"))
new = refresh_tokens(tokens)
new["obtained_at"] = int(time.time())
json.dump(new, open("data/.daikin-tokens.json", "w"), indent=2)
print("Done. Expires in", new["expires_in"], "s")
EOF
```

Then `systemctl restart home-energy-manager` so the service picks it up.

### Full re-auth (refresh_token expired or 401 after refresh)

The auth flow starts a local HTTP server on port 18080 and opens a browser to the Daikin login page. On this headless VPS you need to either:

**Option A — SSH port-forward (recommended):**
```bash
# On your local machine:
ssh -L 18080:localhost:18080 root@116.203.242.63
# Then on the server:
cd /root/home-energy-manager
DAIKIN_REDIRECT_URI=http://localhost:18080/callback .venv/bin/python -m src.daikin.auth
# Open the printed URL in your local browser, log in, approve
```

**Option B — run auth, copy the code manually:**
```bash
cd /root/home-energy-manager
.venv/bin/python -m src.daikin.auth --code CODE
# where CODE is the `code=` param from the redirect URL
```

After successful auth, new tokens are written to `DAIKIN_TOKEN_FILE` (i.e. `data/.daikin-tokens.json` when run from the project root with the env loaded). Restart the service.

### Check current token state

```bash
python3 - <<'EOF'
import json, datetime, time
d = json.load(open("/root/home-energy-manager/data/.daikin-tokens.json"))
print("obtained:", datetime.datetime.fromtimestamp(d["obtained_at"]))
print("expires :", datetime.datetime.fromtimestamp(d["obtained_at"] + d["expires_in"]))
print("expired :", time.time() > d["obtained_at"] + d["expires_in"])
print("has refresh_token:", bool(d.get("refresh_token")))
EOF
```

### Daikin API daily rate limit

- **Limit:** 200 requests/day, resets ~midnight UTC.
- On 2026-04-18 the limit was exhausted during migration testing.
- **`DAIKIN_HTTP_429_MAX_RETRIES=0`** is set in `.env` so the client fails fast on 429 instead of sleeping for `Retry-After` seconds (which Daikin sets to ~86400 on daily-limit exhaustion). Without this the server would hang for hours on startup.
- When rate-limited, Daikin MCP tools return errors immediately. The service still starts and everything else (Fox ESS, Octopus, SQLite) works normally.
- **Nightly plan push is UTC-anchored:** `bulletproof_plan_push_job` fires at `LP_PLAN_PUSH_HOUR:LP_PLAN_PUSH_MINUTE` in **UTC** (default `00:05 UTC`) so the first dispatches of each new plan land on a fresh quota day. Other cron jobs (Octopus fetch, daily brief, MPC re-solves) still run in `BULLETPROOF_TIMEZONE`.

### Legionella thermal-shock cycle

Daikin Onecta firmware runs the weekly thermal-shock cycle autonomously (Sunday ~11:00 local). **The LP and dispatch layer do not schedule or override this cycle** — the `DHW_LEGIONELLA_*` env vars are deprecated (kept only so stale `.env` files don't error on load) and will be removed in a follow-up. If a `shutdown` or `max_heat` action happens to overlap the cycle window, Onecta firmware arbitrates.

---

## Key `.env` settings to know

```
DAIKIN_TOKEN_FILE=.daikin-tokens.json          # relative to cwd → data/ path set in systemd service
DAIKIN_HTTP_429_MAX_RETRIES=0                   # fail fast on rate limit — do not remove
OPENCLAW_READ_ONLY=false                        # the ONLY hardware-write kill switch (true = safe/dev)
DB_PATH=/root/home-energy-manager/data/energy_state.db   # set in systemd service env
PLAN_AUTO_APPROVE=true                          # default: simulate → auto-apply; set false for explicit consent
PLAN_APPROVAL_TIMEOUT_SECONDS=300               # grace window advertised to OpenClaw for Telegram/Discord buttons
DHW_TEMP_NORMAL_C=45.0                          # restore/safe-default tank target (45 °C = sufficient for normal use)
TARGET_DHW_TEMP_MIN_GUESTS_C=55.0              # guest-mode LP floor (multiple showers at 20:30–22:00)
```

### Plan lifecycle (simulate → approve → live)

As of 2026-04-23 the `OPERATION_MODE=simulation|operational` distinction is **gone**. The
system always targets live hardware; `OPENCLAW_READ_ONLY` is the only kill switch (kept
`true` on the local sim box).

Flow per optimizer run:

1. **Simulate** — LP solver produces a plan (read-only, no dial-out). Always happens.
2. **Approve** — if `PLAN_AUTO_APPROVE=true` (default), the plan is auto-approved and
   applied immediately. Otherwise `_write_plan_consent` marks it `pending_approval`
   and sends the `PLAN_PROPOSED` hook to OpenClaw with `autoAcceptOnTimeout: true` +
   `approvalTimeoutSeconds`. OpenClaw renders Telegram/Discord accept/reject buttons;
   no answer → auto-accept on timeout.
3. **Live** — Fox V3 uploaded + Daikin `action_schedule` rows written. Gated only by
   `OPENCLAW_READ_ONLY` and `DAIKIN_CONTROL_MODE`.

To force a fresh simulate/apply cycle: `propose_optimization_plan` (MCP) or
`POST /api/v1/optimization/propose` (web). Both honor `PLAN_AUTO_APPROVE`.
To preview without any write: `simulate_plan` (MCP) — zero hardware, zero quota.

---

## OpenClaw MCP integration

OpenClaw (running at `http://127.0.0.1:18789`) connects to this project via two channels:

1. **nikola MCP server** — started by openclaw via `/root/.openclaw/bin/nikola-mcp`:
   ```bash
   cd /root/home-energy-manager
   exec /root/home-energy-manager/.venv/bin/python -m src.mcp_server
   ```
   This exposes 35 tools (Fox ESS, Daikin, Octopus tariffs, optimization) to the LLM.

2. **Skills** — `/root/home-energy-manager/skills/` is loaded as an extra skill dir in openclaw.

The MCP server is stateless (per-call); the API server (`home-energy-manager.service`) holds all state in SQLite.

---

## Project structure (key files)

```
src/
  cli/__main__.py          # entrypoint: `python -m src.cli serve`
  api/main.py              # FastAPI app + lifespan (DB init, recover_on_boot, scheduler)
  daikin/
    auth.py                # OAuth2 flow + token refresh
    client.py              # DaikinClient (wraps Onecta API)
  daikin_bulletproof.py    # apply_scheduled_daikin_params — ordered writes, float rounding, READ_ONLY guards
  scheduler/
    lp_dispatch.py         # LP plan → Fox V3 groups + Daikin action_schedule rows
    octopus_fetch.py       # Octopus Agile fetch → SQLite; triggers LP re-plan
    runner.py              # heartbeat tick, slot-kind notification debounce
    optimizer.py           # run_optimizer, _write_plan_consent (hash-gated notifications)
  state_machine.py         # recover_on_boot, apply_safe_defaults
  notifier.py              # OpenClaw hooks delivery — all notifications via POST /hooks/agent
  config.py                # all env-var config (Config dataclass)
  physics.py               # DHW setpoint calculations
  mcp_server.py            # MCP server entrypoint (used by openclaw)
data/
  energy_state.db          # SQLite (migrated from Docker volume 2026-04-18)
  .daikin-tokens.json      # OAuth2 tokens (active)
.env                       # secrets + config
.venv/                     # Python 3.12.3 venv (use this, not venv/)
```

---

## What changed on 2026-04-18 (migration from Docker)

- Docker container (`home-energy-manager-energy-manager-1`) removed
- Docker volume data (`energy_state.db`, `.daikin-tokens.json`) migrated to `data/`
- `home-energy-manager.service` rewritten: native `.venv/bin/python -m src.cli serve`, no Docker
- `home-energy-manager` directory chowned to root (was uid 1000)
- `physics.py` restored from git HEAD (working copy had `calculate_dhw_setpoint` etc. stripped)
- `DAIKIN_HTTP_429_MAX_RETRIES=0` added to `.env`
- Daikin access token refreshed (valid ~3h; refresh_token intact)
