# home-energy-manager — Claude context

> Keep this file under ~60k chars. Claude Code caps instruction files at 150k chars in total (this file + `~/.claude/CLAUDE.md`); detailed design notes go under `docs/` and this file keeps summaries + pointers.


## Deployment (Docker, immutable image)

The HEM runs as a single container pulled from GHCR (the immutable-Docker
cutover completed on 2026-04-25). **Code is never editable on the host** —
only `/srv/hem/data/` (state) and `/srv/hem/.env` (secrets). This puts the
application code out of OpenClaw's reach. Deploy/rollback runbook lives at
`deploy/README.md`; the image tag in use is pinned by `HEM_IMAGE_TAG` in
`/srv/hem/.compose.env` (systemd `EnvironmentFile`).

| Thing | Path / value |
|---|---|
| Image | `ghcr.io/albinati/home-energy-manager:<sha>` (linux/arm64) |
| Container | `hem` (uid 1001 inside, read-only rootfs, tmpfs `/tmp`) |
| State volume | `/srv/hem/data/` (DB, Daikin tokens, OpenClaw token, snapshots) |
| Config file | `/srv/hem/.env` (mounted ro into the container) |
| Compose | `/srv/hem/compose.yaml` |
| Systemd unit | `hem.service` (wraps `docker compose up`) |
| API server | `http://127.0.0.1:8000` (loopback) + Tailscale interface |
| MCP transport | `http://127.0.0.1:8000/mcp` (bearer-guarded HTTP, see below) |
| Build entrypoint | `tini → python -m src.cli serve` (set in `Dockerfile`) |

### Service management

```bash
systemctl status hem
systemctl restart hem                          # docker compose down + up
journalctl -u hem -f                           # live logs (journald driver)
curl http://127.0.0.1:8000/api/v1/health       # → status, version, revision SHA, mcp_token_present
docker exec hem cat /app/.git-sha              # build SHA inside the container
```

### Running CLI commands inside the container

`bin/serve`, `bin/mcp`, `bin/start`, `bin/stop` are **dev-only** (used on a
local sim box checkout). In prod, anything that needs the venv goes through
the running container:

```bash
docker exec hem python -m src.cli <subcommand>
```

---

## Daikin Onecta — token management

Tokens live at `data/.daikin-tokens.json`. The access token expires every
**3 hours**; the service auto-refreshes it via the refresh_token as long as
the refresh_token is valid (~30 days).

### Refresh access token (refresh_token still valid)

```bash
docker exec hem python - <<'EOF'
import json, time
from src.daikin.auth import refresh_tokens

tokens = json.load(open("/app/data/.daikin-tokens.json"))
new = refresh_tokens(tokens)
new["obtained_at"] = int(time.time())
json.dump(new, open("/app/data/.daikin-tokens.json", "w"), indent=2)
print("Done. Expires in", new["expires_in"], "s")
EOF

systemctl restart hem
```

### Full re-auth (refresh_token expired or 401 after refresh)

The auth flow starts a callback server on **port 8080** (an older CLAUDE.md said
18080 — that was wrong; both `HTTPServer(...)` binds in `src/daikin/auth.py`
hard-code 8080). Use the one-shot compose file:

```bash
# 1. From your laptop, tunnel :8080:
ssh -L 8080:localhost:8080 root@<hem-host>.ts.net

# 2. On the host, launch the auth-only container:
docker compose -f /srv/hem/compose.daikin-auth.yaml run --rm daikin-auth

# 3. Open the URL the flow prints in your local browser, log in, approve.
#    New tokens land in /srv/hem/data/.daikin-tokens.json. Container exits.

# 4. Restart hem so the service picks up the new tokens.
systemctl restart hem
```

If you need to update `.env` (rare — only if `DAIKIN_REDIRECT_URI` changes),
remount it `rw` for that one run by editing `compose.daikin-auth.yaml`.

### Check current token state

```bash
docker exec hem python - <<'EOF'
import json, datetime, time
d = json.load(open("/app/data/.daikin-tokens.json"))
print("obtained:", datetime.datetime.fromtimestamp(d["obtained_at"]))
print("expires :", datetime.datetime.fromtimestamp(d["obtained_at"] + d["expires_in"]))
print("expired :", time.time() > d["obtained_at"] + d["expires_in"])
print("age (days):", round((time.time() - d["obtained_at"]) / 86400, 1))
print("has refresh_token:", bool(d.get("refresh_token")))
EOF
```


### Daikin API daily rate limit

- **Limit:** 200 requests/day, resets ~midnight UTC.
- On 2026-04-18 the limit was exhausted during migration testing.
- **`DAIKIN_HTTP_429_MAX_RETRIES=0`** is set in `.env` so the client fails fast on 429 instead of sleeping for `Retry-After` seconds (which Daikin sets to ~86400 on daily-limit exhaustion). Without this the server would hang for hours on startup.
- When rate-limited, Daikin MCP tools return errors immediately. The service still starts and everything else (Fox ESS, Octopus, SQLite) works normally.
- **Nightly plan push is UTC-anchored:** `bulletproof_plan_push_job` fires at `LP_PLAN_PUSH_HOUR:LP_PLAN_PUSH_MINUTE` in **UTC** (default `00:05 UTC`) so the first dispatches of each new plan land on a fresh quota day. Other cron jobs (Octopus fetch, daily brief, MPC re-solves) still run in `BULLETPROOF_TIMEZONE`.


## Daikin control — summary

Full detail: [docs/DAIKIN/CONTROL_NOTES.md](docs/DAIKIN/CONTROL_NOTES.md)

- **Cadence (#809):** heartbeat refresh `DAIKIN_HEARTBEAT_REFRESH_ENABLED` (prod true, <=48 reads/day, gated by quota headroom); post-write verify `DAIKIN_POST_WRITE_VERIFY_ENABLED` (default true) reads back only the PATCHed keys, retries once, then one `notify_risk`. Budget ~95-100 of 180/day; watch `GET /api/v1/daikin/quota`. Detail: `docs/DAIKIN/CONTROL_NOTES.md#daikin-cadence--bounded-heartbeat-refresh--post-write-verification-809`.
- **Holiday mode (#872):** detected from the cached payload, per management point (`isHolidayModeActive`). Kill switch `DAIKIN_HOLIDAY_DETECT_ENABLED`. Gates writes, reconciler, LP (`daikin_state=off`), learners; exit triggers power-on + one forced MPC. Detail: `#daikin-holiday-mode-872`.
- **Vacation v2 (#873):** `OPTIMIZATION_PRESET=vacation` needs a future `VACATION_RETURN_AT` (422 otherwise); activation posts an Onecta holiday window and arms the `vacation_end` recovery job (return time - `VACATION_RECOVERY_HOURS`, snapped to a cheap band). Kill switch `VACATION_DAIKIN_HOLIDAY_ENABLED`. Needs `DAIKIN_CONTROL_MODE=active`. Detail: `#vacation-mode-v2-873`.
- **Legionella:** the firmware runs the weekly cycle; HEM only STANDS OFF tank writes in a window (`DHW_LEGIONELLA_STANDOFF_*`, preferred `START_LOCAL`) and budgets `DHW_LEGIONELLA_BUDGET_KWH`. LWT rows still fire. Detail: `#legionella-thermal-shock-cycle`.
- **Tank model (#819):** UA fitted with ambient = measured indoor (`fit_ua_indoor_ambient`), prod UA ~3.2-3.3 W/K; `resolve_tank_params(ambient_c=live)` clamps the live reading. `DHW_LP_OWNED_ENABLED` stays off. Detail: `#tank-model--the-ambient-is-the-house-819-2026-10-07`.
- **User-override propagation (#386):** manual tank changes set `overridden_by_user_at`; the pre-fire reconciler skips state-matched rows and inherits overrides for `USER_OVERRIDE_RESPECT_HOURS` (4) or until the window end. Kill switch `PREFIRE_STATE_MATCH_ENABLED=false`. Detail: `#user-override-propagation-epic-14-386--2026-05-21`.

---


## SmartThings (Samsung) — OAuth + appliance scheduling

Mirror-of-Daikin OAuth Authorization Code flow. Tokens at
`data/.smartthings-tokens.json` (JSON with access_token + refresh_token +
expires_in + obtained_at + scope). Access token expires every 24h; auto-
refreshed by `src/smartthings/auth.py:get_valid_access_token` on every
device API call. Refresh circuit breaker after 3 consecutive failures
(15-min cooldown).

### One-time bootstrap (after `.env` has CLIENT_ID + CLIENT_SECRET set)

```bash
# 1. From your laptop, tunnel :8080:
ssh -L 8080:localhost:8080 root@<hem-host>.ts.net

# 2. On the host, launch the auth-only container:
docker compose -f /srv/hem/compose.smartthings-auth.yaml run --rm smartthings-auth

# 3. Open the URL the container prints in your local browser. Samsung
#    redirects to http://localhost:8080/oauth/smartthings/callback
#    (= the container, via your SSH tunnel). Tokens land in
#    /srv/hem/data/.smartthings-tokens.json. Container exits.

# 4. Restart hem so the service picks up the new tokens.
systemctl restart hem
```

### Refresh access token (refresh_token still valid)

Automatic — `get_valid_access_token` refreshes if within
`SMARTTHINGS_ACCESS_REFRESH_LEEWAY_SECONDS` (default 300) of expiry. On a
401 from the device API the client retries once with `force_refresh=True`.
No manual action needed unless the refresh circuit trips.

### Full re-auth (refresh circuit tripped or refresh_token revoked)

Same one-shot container as bootstrap (above) — overwrites the token file.

### Required `.env` keys

```
SMARTTHINGS_CLIENT_ID=<from `smartthings apps:create`>
SMARTTHINGS_CLIENT_SECRET=<from `smartthings apps:create` — sensitive>
SMARTTHINGS_REDIRECT_URI=http://localhost:8080/oauth/smartthings/callback
SMARTTHINGS_TOKEN_FILE=/app/data/.smartthings-tokens.json   # absolute inside container
APPLIANCE_DISPATCH_ENABLED=true
```


## Appliances, sensors, Cosy tariff, thermal control, auto-tune — summaries

- **Appliance dispatch** (`docs/APPLIANCES.md`): LP hooks `appliance_dispatch.reconcile()` each solve; the appliance's `remoteControlEnabled` is the consent gate; picks the earliest window within `APPLIANCE_EARLY_START_TOLERANCE_PENCE` (10) of the cheapest, `APPLIANCE_MAX_DELAY_HOURS`; learned `typical_kw` after `APPLIANCE_LEARNED_KW_MIN_SAMPLES` (3) runs. Picker "price" is p per 30-min slot, not p/kWh.
- **Indoor sensor ingestion #540** (`docs/SENSORS_INDOOR.md`): `POST /api/v1/sensors/indoor` (ESPHome) writes `room_temperature_history` (in-band temp) and the lossless `device_reading_log`; reaches HEM through the `hem-ui` funnel `:8443` with a scoped `HEM_SENSOR_INGEST_TOKEN` that can ONLY write that endpoint; empty token = feature off. Deploy: `deploy/README.md` section 12.
- **Cosy tariff & battery policy #803/#804/#806/#818/#821** (`docs/COSY_TARIFF_AND_BATTERY.md`): Cosy bands (cheap 12.49p 04-07/13-16/22-24, day 25.45p, peak 38.17p 16-19) via `src/energy/tariff_structure.py`, kill switch `OCTOPUS_TARIFF_STRUCTURE=dynamic`. Policy: `LP_BATTERY_EXPORT_ENABLED=false`, `LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH=100`, Fox shape 16-19 SelfUse(reserve), peak-import guard. Load: `LP_LOAD_EXPENSIVE_BAND_QUANTILE=p75`, `LP_PESS_CHARGE_FLOOR_BAND_EXITS`. Views: `GET /api/v1/load/expected`, `GET /api/v1/plan/fronts`, billing cycle `OCTOPUS_BILLING_CYCLE_DAY`.
- **Thermal control #808 and follow-ups** (`docs/THERMAL_CONTROL_LWT.md`): LWT offset rows (`lwt_preheat`/`restore`); `DAIKIN_LWT_SOURCE` = `tier` (band rule, the kill switch) | `lp` (W3 RC model, `LP_W3_TIN_ENABLED`), plausibility gate falls back to tier. Comfort knobs: `LP_W3_NIGHT_FLOOR_C` (prod 20), `LP_W3_PEAK_COAST_DELTA_C` (prod 0), `INDOOR_SETPOINT_C`, `LP_W3_CEILING_C` (23), `INDOOR_COMFORT_AGGREGATE`. UA pinned `BUILDING_UA_W_PER_K=200` (`BUILDING_UA_LEARNED_AUTO_APPLY` false). Coast `DAIKIN_LWT_COAST_MODE`; `DAIKIN_LWT_ABS_MAX_C` 45; cold/warm backstops and `LWT_EMERGENCY_ENABLED` alert+replan only (never a device write); gains `LP_W3_INTERNAL_GAIN_KW`/`LP_W3_SOLAR_GAIN_KW_PER_PV_KW` (0 = off); `LP_COP_LWT_OFFSET_PENALTY_PER_K` (prod 0.025); `LP_W3_EMITTER_TAU_MIN` (0 = off). `/daikin/heating-plan` reports written offsets (#845).
- **Comfort feedback #833, scorecard #831, tuning review #832** (`docs/AUTO_TUNE_AND_REVIEW.md`): `POST /api/v1/comfort/feedback`; scorecard job 07:30 local (`COSY_SCORECARD_ENABLED`); weekly review Sunday 09:00 (`TUNING_REVIEW_ENABLED`), suggestions only.
- **Auto-tune #876** (`docs/AUTO_TUNE_AND_REVIEW.md`): ledger table `auto_tune_changes`, kill switch `AUTO_TUNE_ENABLED` (default false), allowlist + hard denylist, manual change always wins, one change per ISO week, day-7 replay judge with tripwires and rollback. Never applies anything with the kill switch off.

## Key `.env` settings (compact)

Full annotated block (history, incidents, rationale): [docs/ENV_NOTES.md](docs/ENV_NOTES.md).

| Key | Prod / default | Meaning |
|---|---|---|
| `TUNING_REVIEW_ENABLED` | true | Weekly suggestions-only review |
| `OCTOPUS_TARIFF_STRUCTURE` | auto | auto/banded/dynamic (dynamic = kill switch) |
| `FOX_SCHEDULER_WRITE_VERSION` | v2 | Fox Open API version for scheduler WRITE (v3 broke, 41200) |
| `DAIKIN_TOKEN_FILE` | /app/data/.daikin-tokens.json | Absolute path in container |
| `DAIKIN_HTTP_429_MAX_RETRIES` | 0 | Fail fast on rate limit; do not remove |
| `OPENCLAW_READ_ONLY` | false | The ONLY hardware-write kill switch |
| `DB_PATH` | /app/data/energy_state.db | SQLite path in container |
| `HEM_OPENCLAW_TOKEN_FILE` / `HEM_OPENCLAW_TOKEN` | file / empty | Bearer token for /mcp; file is source of truth |
| `API_HOST` | 0.0.0.0 | Bind inside namespace; compose ports gate |
| `PLAN_AUTO_APPROVE` | true | Simulate then auto-apply; false = explicit consent |
| `PLAN_APPROVAL_TIMEOUT_SECONDS` | 300 | Grace window before auto-accept |
| `DHW_TEMP_NORMAL_C` | 45 | Restore/safe-default tank target |
| `TARGET_DHW_TEMP_MIN_GUESTS_C` | 48 | Guest-mode LP floor |
| `DHW_TEMP_COMFORT_C` | 48 | Tank floor whenever LP plans DHW heat |
| `DHW_TEMP_MAX_C` | 60 | Hard tank ceiling |
| `DHW_TEMP_PV_ABUNDANCE_TARGET_C` | 60 | Storage ceiling for free PV (not a comfort target) |
| `FORECAST_NIGHT_TEMP_BIAS_C` (+ `_START/_END_HOUR_UTC` 21/6) | 0 | Static night bias; keep 0, learned offset corrects |
| `PV_FORECAST_SLOT_CENTRE_SAMPLING` | true | Sample PV drivers at slot centre; false = rollback |
| `PV_CEILING_MARGIN` | 1.15 | PV safety rail; must only fail LOOSE (#762 ratchet) |
| `PV_RECENT_BIAS_MIN` / `_MAX` / `_MIN_DAYS` | 0.6 / 1.4 / 3 | Clamp and sample-day gate for the bias nudge |
| `LP_SCENARIO_*_TEMP_DELTA_C` / `_LOAD_FACTOR` / `_PV_FACTOR` | +1/0.90/1.05; -1.5/1.15/0.85 | Optimistic / pessimistic scenario perturbations |
| `LP_PESS_CHARGE_FLOOR_ENABLED` (+ `_TOLERANCE_KWH` 0.2, `_HOURS` 24, `_SLACK_PENALTY_PENCE` 50) | true | Newsvendor charge floor from pessimistic solve |
| `LP_SOC_RESERVE_RECOVERY_SLACK_PENALTY_PENCE` | 50 | Soft reserve floor when SoC starts below reserve (#789) |
| `LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH` | 0.30 | Commit peak_export only if pessimistic exports at least this |
| `LP_SCENARIOS_ON_TRIGGER_REASONS` | plan_push,octopus_fetch,tier_boundary,... | Which triggers run the 3-pass solve |
| `LP_PLUNGE_PREP_HOURS` | 12 | Bound pre-plunge constraint |
| `LOG_LEVEL` | INFO | DEBUG for diagnostics |
| `BRIEF_MORNING_HOUR/MINUTE`, `BRIEF_NIGHT_HOUR/MINUTE` | 8:00 / 22:00 | Digest times (local) |
| `NOTIFY_TARIFF_TRANSITIONS` | false | Mute cheap/peak pings |
| `TIER_BOUNDARY_LEAD_MINUTES` | 5 | MPC lead before each tier transition |
| `MPC_DRIFT_HYSTERESIS_TICKS` | 1 | Drift detection ticks |
| `CONSUMPTION_BACKFILL_HOUR/MINUTE` | 4:00 | Nightly Octopus consumption backfill |
| `FIXED_TARIFF_LABEL` / `_RATE_PENCE` / `_STANDING_PENCE_PER_DAY` | BG Fixed v58 / 20.70 / 41.14 | Comparison tariff line in brief/MCP (0/empty = off) |
| `DHW_PEAK_TANK_STRATEGY`, `PLAN_REVISION_MIN_*`, `LP_MPC_HOURS`, `EXPORT_DISCHARGE_MIN_SOC_PERCENT` | removed | Silent no-ops if still in .env |

`EXPORT_DISCHARGE_FLOOR_SOC_PERCENT` is unrelated and still used (`fdSoC` of the ForceDischarge group). Pre-charge above `DHW_TEMP_NORMAL_C` only happens with an economic reason (floor 48, ceiling from slot kind); see the doc.

---


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

### Plan lifecycle terminology — be precise across the day boundary

Octopus publishes the next day's Agile rates around **16:00 local**. Confusing
"today's plan" with "tomorrow's plan" across that boundary is the most common
source of stale-status questions. Use these terms exactly:

| Term | Definition |
|---|---|
| `run_at` | UTC timestamp the LP solver finished (column on `optimizer_log`). |
| `plan_date` | Local date the plan is anchored to (column on `lp_inputs_snapshot`). After ~16:00 local, this is **tomorrow**, not today. |
| `horizon` | The 48 h window the LP optimises over (S10.2 / #169). |
| `executed` / `ongoing` / `planned` | Slots before/at/after now. The `/api/v1/scheduler/timeline` endpoint partitions for you. |
| `dispatch decision` | Per-slot `lp_kind` → `dispatched_kind` → `committed` row written to `dispatch_decisions` after every LP solve. The audit trail. |

**Discoverability surfaces:**
- API: `GET /api/v1/scheduler/timeline`, `GET /api/v1/optimization/decisions/{run_id|latest}`, `GET /api/v1/foxess/schedule_diff`.
- MCP: `get_plan_timeline`, `explain_dispatch_decisions`, `get_fox_schedule_diff`, `simulate_peak_export_robustness`.


### Scenario LP and PnL semantics — summaries

Full detail: [docs/PLAN_AND_PNL.md](docs/PLAN_AND_PNL.md) (scenario LP, daily PnL, tariff start clamp, aggregation periods).

- **Scenario LP gates:** `peak_export` (price arbitrage) only emerges in `vacation` with `LP_BATTERY_EXPORT_ENABLED=true`; `pre_negative_export` is the exception (ForceDischarge ahead of a negative window, bypasses the robustness filter). A `peak_export` slot reaches Fox only if the pessimistic scenario also exports >= `LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH` and the margin clears (`filter_robust_peak_export`). No kill-switch env var.
- **PnL, quote structured fields not markdown:** `realised_cost_gbp` is NET and INCLUDES the daily standing charge.
- All shadow costs (`svt_shadow_gbp`, `fixed_shadow_gbp`, `fixed_tariff_shadow_gbp`) also include standing, so `delta_vs_*_gbp` is real money (positive = HEM beat the shadow).
- `realised_import_gbp` is energy import only (no standing, no export).
- `export_kwh` / `export_revenue_gbp` are measured; when 0 but the LP committed peak_export, the brief shows a forecast flagged with 🔮 (an estimate, never add it to measured).
- `Mode:` line tells whether HEM drives Daikin (`active`) or observes (`passive`); when passive never suggest tactical Daikin actions.
- `SMART_TARIFF_START_DATE` (alias `AGILE_TARIFF_START_DATE`) clamps period aggregations (`compute_period_pnl` and weekly/monthly/MTD/YTD); responses carry `clamped`, `label` suffix, honest `n_days`.

## User notifications — summary

Full detail: [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md).

Transport chosen at delivery time by `src/notifier.py`:
1. `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` set: POST straight to the Telegram Bot API (`src/telegram_transport.py`, HTML parse mode); OpenClaw not called.
2. Telegram unset, `OPENCLAW_HOOKS_URL` + `OPENCLAW_HOOKS_TOKEN` set: legacy LLM-shaped hook (rollback path).
3. Neither: stdout + action_log only.

`OPENCLAW_NOTIFY_ENABLED=false` is the master mute for both transports. Per-AlertType routing lives in the `notification_routes` table (overrides apply to the OpenClaw path only). Keys: `TELEGRAM_BOT_TOKEN` (sensitive), `TELEGRAM_CHAT_ID`, `TELEGRAM_API_BASE_URL` (testing only), `TELEGRAM_TIMEOUT_SECONDS` (10). Rollback: unset `TELEGRAM_BOT_TOKEN` and restart `hem.service`. Inline accept/reject buttons are deferred (plan ships as text; reject via `reject_plan` MCP tool).

---


## OpenClaw MCP integration

OpenClaw (running at `http://127.0.0.1:18789`) connects to this project via two channels:

1. **MCP HTTP transport** — the FastMCP server is mounted by
   `src/api/main.py` under `/mcp`, guarded by a bearer token
   (`src/api/middleware.py:BearerAuthMiddleware`). The 81 tools (Fox ESS,
   Daikin, Octopus tariffs, optimization) live in `src/mcp_server.py:build_mcp`
   and are unchanged by the transport switch.

   OpenClaw config (under `/home/openclaw/.openclaw/`):
   ```
   HEM_MCP_URL=http://127.0.0.1:8000/mcp
   HEM_MCP_TOKEN_FILE=/home/openclaw/.openclaw/hem-token
   ```
   The token at `hem-token` is a copy of `/srv/hem/data/.openclaw-token` (the
   HEM lifespan generates it on first boot if absent). After cutover, OpenClaw
   runs as user `openclaw` (uid 2000), **not in the docker group**, and has
   no write access to `/srv/hem/`.

2. **Skills** — `/srv/hem/skills/` (or wherever OpenClaw is configured to
   look) is loaded as an extra skill dir.

The MCP server is stateless (per-call); the API server holds all state in
SQLite under `/srv/hem/data/`.

### Legacy stdio transport (dev local only)

`./bin/mcp` and `python -m src.mcp_server` still run the stdio transport for
local development. The singleton flock that used to gate the stdio path was
removed when the production launcher moved to HTTP — see the docstring in
`src/mcp_server.py` for context.

---

## Project structure (key files)

```
Dockerfile                 # multi-stage build (builder venv → slim runtime + tini)
.dockerignore              # keeps tests/, scripts/, data/, .env, .venv/ out of the image
.github/workflows/docker-publish.yml   # builds and pushes ARM64 image to GHCR on push to main / tags
deploy/
  compose.yaml             # canonical compose for prod (hem + hem-ui services; read-only rootfs, tmpfs, cap_drop, mem limits)
  hem.service              # systemd wrapper around `docker compose up`
  compose.daikin-auth.yaml # one-shot OAuth re-enrollment container
  README.md                # cutover runbook (install, enroll, rollback, SPA cutover at §11)
ui/                        # SPA container (nginx serving a Vite build)
  Dockerfile               # node build stage → nginx:alpine + envsubst for runtime config
  conf/nginx.conf.template # reverse-proxies /api → hem; SPA fallback `try_files $uri /index.html`
  index.html               # single Vite entry — wouter resolves all routes client-side
  src/routes/              # Preact + TypeScript route components (#879 v2): landing.tsx (`/` Home:
                           #   status + 3 tiles + bill + one band strip + mode), insights.tsx (`/insights`:
                           #   recommendations, week, house; detail cards lazy behind "ver mais"),
                           #   settings.tsx (`/settings`: Conforto / Água quente / Conta + Avançado),
                           #   report.tsx (`/report` Journal, admin, not in the nav) — no per-route HTML page
  src/components/home/simple/      # Home v2 blocks (StatusLine, tiles, BandStrip, ModeBox) + pure logic.ts (tested)
  src/components/insights/simple/  # Insights v2 (Tiles, recommendations, DetailCards lazy chunk) + logic.ts
  src/components/insights/*.tsx    # the detailed cards DetailCards mounts (LoadPattern, CosyScorecard, LwtLearning…)
  src/components/settings|shell|common|report/  # settings groups + batch bar, top nav/alerts/footer, primitives, journal
  src/lib/endpoints.ts     # ONLY the wrappers a screen calls (unused ones were deleted in #879 PR 4)
  src/styles/              # tokens.css (authoritative tokens), base/shell/home-simple/insights-simple CSS
docs/ui/                   # reference screenshots (home / insights / settings, desktop + phone, admin + viewer)
  ui-entrypoint.sh         # writes /config.js with bearer + apiBase at container boot
.github/workflows/ui-publish.yml  # builds + pushes ghcr.io/<owner>/home-energy-manager-ui on push to main (paths-scoped)
quartz/                    # #542 — self-hosted Quartz solar-forecast sidecar (hem-quartz service)
  Dockerfile               # python:3.12-slim + quartz-solar-forecast (xgboost site-level model, MIT)
  app.py                   # FastAPI mirroring open.quartz.solar POST /forecast/ schema; lazy model warm-up
.github/workflows/quartz-publish.yml  # builds + pushes ghcr.io/<owner>/home-energy-manager-quartz (paths-scoped)
src/
  cli/__main__.py          # entrypoint: `python -m src.cli serve` (PID 1 in the container, behind tini)
  api/main.py              # FastAPI app + lifespan (token bootstrap, MCP session manager, scheduler) — JSON API only since B5
  api/middleware.py        # BearerAuthMiddleware (/mcp) + ApiV1RoleAuth (/api/v1/*: viewer-open reads,
                           #   admin-gated writes + scoped sensor-ingest token; gated by HEM_UI_AUTH_REQUIRED).
                           #   NB ApiV1BearerAuth also lives in this file but is NOT mounted — dead code.
  daikin/
    auth.py                # OAuth2 flow + token refresh (port 8080)
    client.py              # DaikinClient (wraps Onecta API)
  daikin_bulletproof.py    # apply_scheduled_daikin_params — ordered writes, float rounding, READ_ONLY guards
  scheduler/
    lp_dispatch.py         # LP plan → Fox V3 groups + Daikin action_schedule rows
    octopus_fetch.py       # Octopus Agile fetch → SQLite; triggers LP re-plan
    runner.py              # heartbeat tick, slot-kind notification debounce
    optimizer.py           # run_optimizer, _write_plan_consent (hash-gated notifications)
  state_machine.py         # recover_on_boot, apply_safe_defaults
  notifier.py              # notification delivery — direct Telegram preferred (telegram_transport.py);
                           #   OpenClaw POST /hooks/agent only as fallback when Telegram is unconfigured
  config.py                # all env-var config (Config dataclass)
  physics.py               # DHW setpoint calculations
  mcp_server.py            # FastMCP `build_mcp()` (HTTP in prod, stdio for dev)
bin/                       # dev-local launchers (./bin/serve, ./bin/mcp) — NOT used in prod
data/                      # state (DB + tokens). On the host: bind-mounted at /srv/hem/data → /app/data
.env                       # secrets + config (host: /srv/hem/.env mounted ro into the container)
.venv/                     # Python 3.12.3 venv for dev local (the prod image carries /opt/venv inside)
```

---


## Docker cutover history (summary)

- 2026-04-25: immutable Docker image from GHCR; MCP moved to HTTP `/mcp` (bearer); OpenClaw runs as uid 2000 with no write access to `/srv/hem/`; Daikin OAuth port is 8080; rollback in `deploy/README.md` section 8.
- 2026-04-18: first Docker-to-native migration, since reversed (OpenClaw had write access to running code).
- Full notes: [docs/archive/DOCKER_CUTOVER_2026-04.md](docs/archive/DOCKER_CUTOVER_2026-04.md).

---

## Design System

Read `DESIGN.md` (repo root) before any visual or UI change. It is the source of
truth for color, typography, spacing, layout, motion, the routes, and the UI's
non-negotiables (deep-dark, borderless, no-emoji, semantic color, system fonts,
~0.2s perf budget, every number once, honest £ or no £). The UI shows the Cosy plan
per band (never a price curve or percentile tiers); Agile vocabulary only when
`tariff_structure_kind === "dynamic"`. Token values are authoritative in `ui/src/styles/tokens.css`;
DESIGN.md is the rules + rationale. In QA, flag any UI code that deviates from it.

---

## Agent workflow — model routing (owner rule, 2026-10-28)

When Claude Code works on this repo, the orchestrating model (Fable) **only plans,
specifies, decides, reviews verdicts and merges**. It does not type code.

| Task | Agent / model |
|---|---|
| Writing code, tests, docs for a story | `Agent(model="sonnet")` in a worktree, one branch per story, PR with `Closes #N` |
| Adversarial review of a PR before merge | `Agent(model="opus")`, read-only (Plan or general-purpose), verdict MERGE / FIX-THEN-MERGE / DO-NOT-MERGE |
| Reading, inventories, lookups (files, APIs, logs) | `Agent(subagent_type="Explore", model="haiku")` |
| Orchestration, plan, decisions, memory | Fable (the main session) |

Review fixes go back to the same Sonnet agent (context intact); merge only when the review
is clean and CI is green. Never deploy to prod from an agent — the owner runs `rollout.sh`.
