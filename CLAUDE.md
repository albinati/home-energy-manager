# home-energy-manager — Claude context

## Deployment (Docker, immutable image)

> **Cutover status (2026-04-25):** branch `feat/docker-immutable-deploy` brings
> Docker back as an *immutable* deployment. Until the cutover runs against
> Hetzner, the live server may still match the older "native systemd" layout —
> `git log main` is authoritative for what is actually serving traffic.
> Cutover runbook lives at `deploy/README.md`.

The HEM runs as a single container pulled from GHCR. **Code is never editable
on the host** after cutover — only `/srv/hem/data/` (state) and
`/srv/hem/.env` (secrets). This puts the application code out of OpenClaw's
reach.

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

### BOOT.md is outdated — ignore it
Refers to `venv/` and `daemon start`. Neither applies in either dev (`.venv/`
+ `src.cli serve`) or prod (containerised).

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

The auth flow starts a callback server on **port 8080** (the previous CLAUDE.md
said 18080 — that was wrong; the code at `src/daikin/auth.py:328` always bound
8080). Use the one-shot compose file:

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

### Legionella thermal-shock cycle

Daikin Onecta firmware runs the weekly thermal-shock cycle autonomously (default Sunday 11:00 UTC; user-reconfigurable on the unit). **HEM does not schedule this cycle** — the old `DHW_LEGIONELLA_*` *scheduling* vars are gone. Python ignores unrecognised keys in `.env` so lingering entries are harmless; delete them on your next `.env` touch.

**Tank stand-off (2026-06-07).** Because the firmware OWNS the DHW tank during the cycle, any tank PATCH HEM sends in that window is arbitrated/overridden (wasted Daikin quota + churn + `READ_ONLY`). So the reconciler now **skips tank-device writes inside a configured stand-off window and leaves those rows pending** so they resume the moment the window closes (the firmware leaves the tank hot; HEM's next warmup/setback then brings it back to plan). **TANK ONLY — LWT / space-heating rows still fire** (legionella is a DHW-tank cycle). The LP budgets the cycle's heat-up energy via `DHW_LEGIONELLA_BUDGET_KWH` (default 3.5 kWh electric, spread across the stand-off window in `forecast_dhw_load_per_slot`) — added in #643 (2026-07-05) after a Sunday audit showed the post-K1 forecast carried NO legionella term (~0.5 budgeted vs ~3-3.5 kWh drawn; the battery discharged into the un-budgeted cycle and hit the SoC floor mid-heat-up). Telemetry: `legionella_tank_standoff` events in `action_log`. New `.env` knobs (defaults = Sunday 11:00 UTC, 120 min — covers the ramp from the overnight setback up to ~60 °C plus the firmware's ~1 h hold):

```
DHW_LEGIONELLA_STANDOFF_ENABLED=true            # master switch (the ONLY tank-write block during the cycle)
DHW_LEGIONELLA_STANDOFF_DOW=6                    # weekday, Mon=0 .. Sun=6 (datetime.weekday())
DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC=11        # window start (UTC); must not cross midnight
DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC=0
DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES=120     # ramp + ~1 h hold; tune from `legionella_tank_standoff` telemetry
```

Helper: `src/state_machine.py:in_legionella_standoff(now_utc)`. The guard sits in `_reconcile_daikin_actions` before the pending→active transition. If a `shutdown`/`max_heat` action overlaps the cycle window outside the guard, Onecta firmware still arbitrates.

### User-override propagation (Epic 14, #386 — 2026-05-21)

When the user manually changes tank state (Onecta app / physical button),
the active `action_schedule` row gets `overridden_by_user_at` set by the
reactive detector in `src/daikin_bulletproof.py`. The pre-fire reconciler
in `src/state_machine.py` now does two things:

1. **Idempotency** — before firing a pending row, compare live device
   state against the row's params. If they match, mark the row `completed`
   with `error_msg='noop (state matched pre-fire)'` and skip the API call.
   This is what drops the `READ_ONLY_CHARACTERISTIC` 400s and the
   redundant solar_preheat writes that overlapping replan rows produced.
2. **Override inheritance** — for non-`restore` rows, look up the most
   recent `overridden_by_user_at` row for the same device within
   `USER_OVERRIDE_RESPECT_HOURS` (default 4h) — OR, when
   `USER_OVERRIDE_RESPECT_UNTIL_WINDOW_END` (default true, 2026-06-07),
   while the overridden row's own `end_time` is still in the future. The
   latter keeps a hand-set tank/LWT respected for the WHOLE planned window
   (e.g. a multi-hour negative-price boost), not just the fixed grace. If
   the user's gesture is still in effect (live state still contradicts the
   override row), mark the new row overridden too and skip. The live
   `user_gesture_still_in_effect` check is the safety gate — revert the
   manual change and HEM resumes at once, so a long window can't wedge the
   schedule. One notification fires per *source* user gesture, not per
   suppressed downstream row. Restore rows are exempt so the system can
   always return to baseline.

Both behaviours are gated by `PREFIRE_STATE_MATCH_ENABLED` (default true).
Set to `false` for instant rollback. Telemetry shows up in `action_log` as
`prefire_state_match` and `prefire_override_inherited` events.

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

### Appliance dispatch

LP solver hooks `appliance_dispatch.reconcile()` at the start of each
solve. Reads each registered appliance's `remoteControlEnabled` via
SmartThings; when true, picks the cheapest contiguous window before the
deadline, includes the planned kWh in the LP residual-load profile, and
registers a one-shot APScheduler `DateTrigger` cron at the chosen time.
Cron fires `setMachineState run` after pre-fire safety re-check.
Cancelling on the unit before fire time → next LP solve drops the cron
and re-plans without the load. Physical Smart Control button on the
appliance IS the consent gate — no MCP confirm.

`device_type` accepts `washer | dryer | dishwasher` — same SmartThings
`setMachineState run` command for all three. Register multiple devices
via the discover/register MCP tools or REST endpoints.

**Learned `typical_kw` (#222).** The cycle-energy estimate prefers the rolling
mean of recent completed runs' measured `actual_kwh` (SmartThings energy
counter, #235) over the static registration `typical_kw`, once
`APPLIANCE_LEARNED_KW_MIN_SAMPLES` (3) runs exist — `db.appliance_learned_typical_kw`
over the last `APPLIANCE_LEARNED_KW_LOOKBACK` (10). Registration default 0.5 kW
over-estimated eco cycles ~3× (real ~0.2–0.4 kW), so the LP used to route around
the wash more than necessary. `GET /api/v1/appliances` surfaces
`learned_typical_kw` / `learned_samples` / `effective_typical_kw`.

---

## Indoor temperature sensor ingestion (#540 W1)

The Altherma has no room stat, so the house's indoor temperature was never
measured — the winter thermal model (#540) needs it. An ESPHome room sensor
pushes readings to **`POST /api/v1/sensors/indoor`** (`src/api/routers/sensors.py`):
batch (1–2000 readings), idempotent on `(captured_at, room)`, stored in
`room_temperature_history`. Downstream: LP initial state + dispatch comfort
guard read the freshest reading (`INDOOR_SENSOR_STALE_MINUTES=30`); the W2
thermal learner reads the history. Read back via `GET /api/v1/sensors/indoor`
and `GET /api/v1/sensors/thermal-calibration`.

**Full per-device logging (#540 W1c).** The endpoint has TWO sinks:
`room_temperature_history` keeps ONLY the in-band `temp_c` (what the LP/thermal
model read), while `device_reading_log` is the **lossless audit of everything a
device sends** — typed columns for the known metrics (temp/humidity/pressure/
mac/device_id) + a `payload_json` blob so any extra field (2nd temperature,
RSSI, battery…) survives with no migration. `IndoorReading` is `extra="allow"`;
`temp_c` is optional (a humidity-only device still logs) and an out-of-band temp
(85 °C fault) is logged but NOT routed to thermal history. Dedup on
`(device_key, captured_at)`, `device_key = mac|device_id|source|room`. Read
back: `GET /api/v1/sensors/devices` (one row per device + latest metrics) and
`GET /api/v1/sensors/device-log?device=&hours=` (raw rows w/ full `payload`),
both viewer. POST returns `{received, written (temp rows), logged (device-log)}`.

**Network path (sensor at home → HEM on Hetzner).** The sensor is on the house
LAN; the HEM is a cloud box behind Tailscale — ESPHome can't join the tailnet.
It reuses the **existing `hem-ui` Tailscale funnel (`:8443`)**, which already
publishes `/api/` with valid TLS (and does NOT expose `/mcp`) — no new proxy,
port, or funnel. The sensor POSTs to
`https://<host>.ts.net:8443/api/v1/sensors/indoor` carrying a **scoped**
`HEM_SENSOR_INGEST_TOKEN` — NOT admin. (The funnel is a dumb TLS tunnel to one
local port with no path ACL, so route-level containment is done entirely by the
scoped token in the middleware, below — not by the funnel.)

**Scoped token (`ApiV1RoleAuth.ingest_tokens`).** `middleware.py:_ingest_allowed`
lets this token satisfy ONLY a *write* to `/api/v1/sensors/indoor` — never an
admin read (Settings/Journal) or any other write. So a firmware/network leak can
only post fake temperatures to that one endpoint; rotate the token to revoke a
device. Empty `HEM_SENSOR_INGEST_TOKEN` → feature off (admin-only, as before).

Full deploy (token mint + smoke test) + ESPHome YAML skeleton: `deploy/README.md` §12.

---

## Key `.env` settings to know

```
DAIKIN_TOKEN_FILE=/app/data/.daikin-tokens.json # absolute inside container; compose pins it
DAIKIN_HTTP_429_MAX_RETRIES=0                   # fail fast on rate limit — do not remove
OPENCLAW_READ_ONLY=false                        # the ONLY hardware-write kill switch (true = safe/dev)
DB_PATH=/app/data/energy_state.db               # absolute inside container
HEM_OPENCLAW_TOKEN_FILE=/app/data/.openclaw-token  # bearer token for /mcp; lifespan creates if missing
HEM_OPENCLAW_TOKEN=                             # leave empty: the file above is the source of truth
API_HOST=0.0.0.0                                # bind inside the namespace; compose ports do the gating
PLAN_AUTO_APPROVE=true                          # default: simulate → auto-apply; set false for explicit consent
PLAN_APPROVAL_TIMEOUT_SECONDS=300               # grace window advertised to OpenClaw for Telegram/Discord buttons
DHW_TEMP_NORMAL_C=45.0                          # restore/safe-default tank target (45 °C = sufficient for normal use)
TARGET_DHW_TEMP_MIN_GUESTS_C=55.0              # guest-mode LP floor (multiple showers at 20:30–22:00)
# DHW_PEAK_TANK_STRATEGY was REMOVED 2026-05-21 (Epic 14, #386). The dispatch
# layer always uses the IDLE behaviour (tank_power=True, tank_temp=DHW_TEMP_NORMAL_C)
# during peak / peak_export windows. Prod telemetry (30d, 8 completed peak
# windows) showed median tank decay 0.00 °C/h — the tank coasts essentially
# perfectly even when held warm — and SHUTDOWN attempts failed 27% of the time
# with READ_ONLY_CHARACTERISTIC errors. Leaving the env line in /srv/hem/.env
# is harmless; remove on next .env touch.
# IMPORTANT: tank pre-charge above 45 °C only happens when there's
# an economic reason — `negative` (paid to import → 65 °C max),
# `solar_charge` (free PV → DHW_TEMP_PV_ABUNDANCE_TARGET_C, default
# 45 → runtime-tunable per household occupancy), `cheap` (modest
# → 48 °C). Peak avoidance does NOT trigger pre-charging — see
# issue #322 for conditional shutdown commit.
DHW_TEMP_PV_ABUNDANCE_TARGET_C=45               # tank target during solar_charge / solar_preheat slots. Runtime-
                                                 # tunable via runtime_settings (PUT /api/v1/settings or MCP
                                                 # `set_setting`). Raise per household: single ~42, family of 4
                                                 # ~50, larger ~55. Default lowered from 55 → 45 (#325, 2026-05-12)
                                                 # after Daikin telemetry showed overnight DHW reheat = 0 even at 45.

# --- Forecast night bias (issue #324, minimal) ---
FORECAST_NIGHT_TEMP_BIAS_C=0                    # subtract this from Open Meteo's `temperature_c` when the LP
                                                 # reads forecast slots inside the configured night window.
                                                 # SET TO 0 ON 2026-06-12: the learned per-hour microclimate
                                                 # offset (get_micro_climate_offset_by_hour_c, fed by
                                                 # forecast_skill_log) already corrects sensor-vs-forecast gaps
                                                 # adaptively, so the static -3 double-corrected — skill data
                                                 # showed the raw night residual was only +0.2..+0.7 °C by June
                                                 # (the -3 was calibrated on one cold 2026-05-12 observation).
                                                 # Keep at 0 unless the learned offset is disabled; the LP would
                                                 # otherwise budget nights ~3 °C colder than reality all winter.
FORECAST_NIGHT_START_HOUR_UTC=21                # bias active from (inclusive)
FORECAST_NIGHT_END_HOUR_UTC=6                   # bias active until (exclusive); wraps midnight when start > end

# --- Slot-centre forecast sampling (2026-06-17) ---
PV_FORECAST_SLOT_CENTRE_SAMPLING=true           # a 30-min slot's energy is `kw × 0.5h`; the honest
                                                 # representative power is the value at the slot CENTRE
                                                 # (start+15min), not the start instant. Sampling at the
                                                 # start attributed each slot's PV energy ~15 min too LATE
                                                 # vs the realised trapezoidal roll-up — a deterministic
                                                 # +15 min lag confirmed over 21 prod days
                                                 # (scripts/diag/pv_time_lag.py; the chart "offset" the user
                                                 # spotted, NOT a UTC/BST bug — timezones audited clean).
                                                 # true → interpolate the weather drivers (temp/rad/cloud/
                                                 # Quartz estimated_pv_kw) at the slot centre in
                                                 # forecast_to_lp_inputs. Calibration/night-bias/scale stay
                                                 # keyed to the slot-START hour (a slot belongs to its start
                                                 # hour; centre never crosses the hour for :00/:30 starts), so
                                                 # the calibration tables are UNAFFECTED — no recompute needed.
                                                 # ROLLBACK: set false in /srv/hem/.env + `systemctl restart
                                                 # hem` for INSTANT rollback to legacy slot-start sampling (no
                                                 # redeploy); or git-revert the PR. NB this leaves the variable
                                                 # forecast-SKILL residual (−57..+65 min, regime-dependent)
                                                 # untouched — that's a separate Quartz/Open-Meteo calibration
                                                 # story, revisit via pv_time_lag.py on post-#564 realised data.

# --- Scenario LP for peak-export robustness (see docs/DISPATCH_DECISIONS.md) ---
LP_SCENARIO_OPTIMISTIC_TEMP_DELTA_C=1.0          # +°C applied to outdoor forecast
LP_SCENARIO_OPTIMISTIC_LOAD_FACTOR=0.90          # multiplier on base-load profile
LP_SCENARIO_PESSIMISTIC_TEMP_DELTA_C=-1.5        # −°C; pessimistic case for cold-night protection
LP_SCENARIO_PESSIMISTIC_LOAD_FACTOR=1.15         # 15 % uplift on base load
LP_SCENARIO_OPTIMISTIC_PV_FACTOR=1.05            # ×PV in the optimistic solve (2026-07-02 LP audit)
LP_SCENARIO_PESSIMISTIC_PV_FACTOR=0.85           # ×PV in the pessimistic solve — models a cloud
                                                 # surprise; calibrated from 27d of pv_error_log
                                                 # (daily Σactual/Σforecast p25=0.883). 1.0 = legacy
                                                 # (no PV perturbation, pessimistic kept nominal PV)
LP_PESS_CHARGE_FLOOR_ENABLED=true                # PR B (2026-07-02 audit) — newsvendor charge floor:
                                                 # scenario-bearing triggers re-solve the committed plan
                                                 # with a SOFT floor at the pessimistic solve's SoC
                                                 # trajectory (under-charging for the evening peak costs
                                                 # ~4× over-charging; June backtest: cost-neutral,
                                                 # empty-at-peak slots 4→1). Since PR B scenarios run on
                                                 # those triggers even without peak_export slots.
                                                 # false = instant rollback to median-sized charging.
LP_PESS_CHARGE_FLOOR_TOLERANCE_KWH=0.2           # subtracted from the pessimistic SoC before flooring
LP_PESS_CHARGE_FLOOR_HOURS=24                    # floor only the first N horizon hours (rest is replanned)
LP_PESS_CHARGE_FLOOR_SLACK_PENALTY_PENCE=50.0    # slack penalty — floor behaves hard, can't go Infeasible
LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH=0.30        # commit peak_export only when pessimistic exports ≥ this
LP_SCENARIOS_ON_TRIGGER_REASONS=cron,plan_push,octopus_fetch,tier_boundary
                                                 # which triggers run the 3-pass solve
LP_PLUNGE_PREP_HOURS=12                          # PR #218 — bound the pre-plunge constraint
                                                 # to N hours ahead. With the unbounded default
                                                 # (whole 48 h horizon, pre-#218), days where the
                                                 # next negative slot was >24 h away starved the
                                                 # battery — only 33 % of charge slots in cheap
                                                 # quartile. 12 h covers same-day pre-plunge
                                                 # without blocking next-night arbitrage.
LOG_LEVEL=INFO                                   # raise to DEBUG for deep-dive diagnostics

# --- V12 — twice-daily digest + tier-boundary MPC ---
BRIEF_MORNING_HOUR=8                             # local TZ (default 08:00)
BRIEF_MORNING_MINUTE=0
BRIEF_NIGHT_HOUR=22                              # local TZ (default 22:00)
BRIEF_NIGHT_MINUTE=0
NOTIFY_TARIFF_TRANSITIONS=false                  # mute heartbeat cheap/peak pings
                                                 # (negative-price 🔵 always pings regardless)
TIER_BOUNDARY_LEAD_MINUTES=5                     # MPC fires this far before each tier transition
PLAN_REVISION_MIN_SOC_DELTA_PERCENT=10.0         # PLAN_REVISION ping threshold (any one is enough)
PLAN_REVISION_MIN_GRID_DELTA_KWH=1.0
MPC_DRIFT_HYSTERESIS_TICKS=1                     # bumped down 2→1 (V12) — catches heating ramp faster
# LP_MPC_HOURS was REMOVED post-V12 — the fixed-hour MPC cron is gone entirely
# (no longer a config attribute; setting it in .env is a silent no-op). The
# event-driven model (tier_boundary + octopus_fetch + drift + forecast_revision
# + dynamic_replan) covers every signal change.

# --- V13 — nightly post-hoc consumption backfill ---
CONSUMPTION_BACKFILL_HOUR=4                      # local TZ (default 04:00) — Octopus consumption
CONSUMPTION_BACKFILL_MINUTE=0                    # endpoint lags ~24h, 04:00 the next day is safe

# --- Brief expansion (#207 follow-up) — net cost + tariff comparisons ---
# Realised cost in compute_daily_pnl + brief markdown is NET and INCLUDES the
# daily standing charge (apples-to-apples vs shadows). Set MANUAL_STANDING_CHARGE
# above for the Agile standing fee. Set the FIXED_TARIFF_* trio to surface a
# "vs <label>" comparison line in the brief + MCP (e.g. previous fixed tariff).
# Leave any of the FIXED_TARIFF_* values at 0 / empty to suppress the line.
FIXED_TARIFF_LABEL=British Gas Fixed v58         # display label (free text)
FIXED_TARIFF_RATE_PENCE=20.70                    # flat unit rate
FIXED_TARIFF_STANDING_PENCE_PER_DAY=41.14        # daily standing charge
```

`EXPORT_DISCHARGE_MIN_SOC_PERCENT` was **removed** (was the live-SoC global gate that
spuriously dropped tomorrow's peak-export when live SoC was below 95 %). The scenario
LP filter (`src/scheduler/lp_dispatch.py:filter_robust_peak_export`) replaces it.
`EXPORT_DISCHARGE_FLOOR_SOC_PERCENT` is unrelated and still in use — it's the `fdSoC`
parameter sent to Fox in the ForceDischarge group.

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

### Scenario LP for peak-export robustness

When the LP plans `peak_export` (battery → grid arbitrage), three solves run
under perturbed forecasts (optimistic / nominal / pessimistic). A
`peak_export` slot only makes it onto Fox V3 when the **pessimistic** scenario
also exports ≥ `LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH` (default 0.30 kWh) at
that slot. Otherwise it's downgraded to standard SelfUse (battery still
covers load, no grid feed). Decisions are persisted to `dispatch_decisions`
with the per-scenario kWh values for full auditability.

`ENERGY_STRATEGY_MODE=strict_savings` is the kill switch — drops every
`peak_export` regardless of scenarios. Default is `savings_first` which
trusts the LP + scenario filter. The legacy `EXPORT_DISCHARGE_MIN_SOC_PERCENT`
live-SoC global gate is **gone** (caused the 2026-04-28 incident where
tomorrow's profitable peak-export disappeared during a re-plan after today's
discharge had drawn the battery below 95 %).

See `docs/DISPATCH_DECISIONS.md` for the design rationale and decision rule.

### Daily PnL semantics — read this before quoting any £ figure

The MCP tools `get_energy_metrics`, `get_daily_brief`, `get_night_brief`, and
`get_tariff_comparison` (plus the markdown brief itself) all use the same
convention. **Don't paraphrase numbers from the markdown — pull the
structured fields:**

- `realised_cost_gbp` is **NET** and **INCLUDES** the daily standing charge.
  Formula: `Σ(slot_kwh × agile_p) + standing_pence_per_day − Σ(export_kwh × export_p)`.
- All shadow costs (`svt_shadow_gbp`, `fixed_shadow_gbp`,
  `fixed_tariff_shadow_gbp`) **also include** the standing charge — so
  `delta_vs_*_gbp` is real money saved, not energy-cost-only saved. Positive
  = Agile beat the shadow.
- `realised_import_gbp` is the energy-import side only (no standing, no
  export). Useful for breaking down "where did the cost come from".
- `export_revenue_gbp` and `export_kwh` are **measured** (from
  `pv_realtime_history` half-hour rollup × per-slot `agile_export_rates`).
- When `export_kwh == 0` but the LP committed `peak_export` slots, the brief
  surfaces a **forecasted** estimate flagged with 🔮 — that's the LP's
  `scen_pessimistic_exp_kwh × per-slot Outgoing Agile rate`. It is an
  estimate, not measurement; do not double-count.

`Mode:` line in the brief tells OpenClaw whether HEM is actively driving
Daikin (`active`) or just observing (`passive`). When passive, OpenClaw
should never suggest tactical Daikin actions ("preheat the tank now!") —
the heat pump runs on its own weather curve and HEM does not change
setpoints. Read `_mode_status_line()` in `src/analytics/daily_brief.py`.

### Tariff start clamp (PR #214)

```
AGILE_TARIFF_START_DATE=2026-04-01    # household joined Octopus Agile on this date
```

Period aggregations (`compute_period_pnl` and everything that delegates to it
— weekly/monthly/MTD/YTD) clamp their `start_day` upward to this value.
Pre-Agile days were on a different tariff and would otherwise pollute the
realised cost + shadow comparisons. When clamped:
- Response carries `clamped: true`, `clamp_reason`, `requested_start`.
- `label` gains a `(since YYYY-MM-DD)` suffix so OpenClaw renders an honest
  qualifier on the YTD/monthly figure.
- `n_days` reflects the *actual on-Agile* window, not the requested range.

Leave empty to disable. Invalid ISO date logs a warning and disables clamp.

### Aggregation periods (PR #213)

Five PnL scopes are exposed by the same shape:

| Function | MCP path | Range |
|---|---|---|
| `compute_daily_pnl(day)` | `get_energy_metrics.pnl.daily` | a single local day |
| `compute_weekly_pnl(end_day)` | `get_energy_metrics.pnl.weekly` | trailing 7 days ending on `end_day` |
| `compute_monthly_pnl(end_day)` | `get_energy_metrics.pnl.monthly` | full calendar month containing `end_day` |
| `compute_mtd_pnl(end_day)` | `get_energy_metrics.pnl.month_to_date` | 1st of month → `end_day` (partial) |
| `compute_ytd_pnl(end_day)` | `get_energy_metrics.pnl.year_to_date` | Jan 1 of year → `end_day` |

All five funnel through `compute_period_pnl(start_day, end_day)`. Each
period dict carries the same breakdown as a daily dict — `kwh`,
`realised_cost_gbp`, `realised_import_gbp`, `export_revenue_gbp`,
`export_kwh`, `standing_charge_gbp`, `svt_shadow_gbp`, `fixed_shadow_gbp`,
`delta_vs_*` — plus `period_start`, `period_end`, `n_days`, `label`.

`mcp.get_tariff_comparison` accepts three input modes (mutually exclusive):

```jsonc
{ "date": "2026-05-01" }                              // single day (default = yesterday)
{ "period": "week"|"month"|"mtd"|"ytd" }              // anchored on today
{ "start_date": "...", "end_date": "..." }            // custom inclusive range
```

---

## User notifications — direct Telegram (preferred) vs OpenClaw hook (fallback)

As of 2026-05-09 HEM POSTs notifications **straight to the Telegram Bot API**
when configured, bypassing OpenClaw's `/hooks/agent` LLM-shaping path. The
older flow paid for an Anthropic API call inside OpenClaw on *every* brief,
plan revision, tier-boundary ping, and appliance lifecycle event — to
re-shape Markdown HEM had already formatted. Direct Telegram removes that
tax while keeping action_log + stdout unchanged.

Transport is selected at delivery time by `src/notifier.py`:

1. **`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` set** → POST to
   `https://api.telegram.org/bot<TOKEN>/sendMessage` via
   `src/telegram_transport.py`. HTML parse mode; `**bold**` and `` `code` ``
   in the source are converted; structured `push_alert` events get bespoke
   per-event rendering (no JSON dump). Plan-proposed messages ship the
   schedule inside `<pre>` for monospace alignment. OpenClaw is **not** called.
2. **Telegram unset, `OPENCLAW_HOOKS_URL` + `OPENCLAW_HOOKS_TOKEN` set** →
   legacy hook path (LLM-shaped). Kept for rollback.
3. **Neither configured** → stdout + action_log only.

`OPENCLAW_NOTIFY_ENABLED=false` is the master mute for both transports
(stdout + action_log keep running). Per-AlertType routing (enable/disable,
severity, silent flag) still flows through the `notification_routes`
SQLite table; `target_override` / `channel_override` only apply to the
OpenClaw fallback path — the Telegram chat is global.

### `.env` keys

```
TELEGRAM_BOT_TOKEN=123456:ABC...        # from @BotFather; sensitive
TELEGRAM_CHAT_ID=7964600619             # same value as OPENCLAW_NOTIFY_TARGET
TELEGRAM_API_BASE_URL=https://api.telegram.org   # override only for testing
TELEGRAM_TIMEOUT_SECONDS=10
```

When the user's Anthropic-token budget is the concern, leaving the OpenClaw
hook path enabled is fine *as long as Telegram is also configured* — the
Telegram path short-circuits and OpenClaw is never reached for messaging.
To roll back, simply unset `TELEGRAM_BOT_TOKEN` (or `TELEGRAM_CHAT_ID`)
and restart `hem.service`. The OpenClaw hook config is untouched.

### Interactive accept/reject buttons (deferred)

Telegram supports `reply_markup.inline_keyboard` with `callback_data` for
plan-approval buttons, but receiving the callback needs either polling
`getUpdates` or a webhook ingress. Since `PLAN_AUTO_APPROVE=true` is the
default and timeouts auto-accept, the current Telegram path ships the
plan as plain text with a clear "Auto-applies in N min unless rejected"
line and instructions to reject via the `reject_plan` MCP tool. Wiring
buttons is a future follow-up.

---

## OpenClaw MCP integration

OpenClaw (running at `http://127.0.0.1:18789`) connects to this project via two channels:

1. **MCP HTTP transport** — the FastMCP server is mounted by
   `src/api/main.py` under `/mcp`, guarded by a bearer token
   (`src/api/middleware.py:BearerAuthMiddleware`). The 75 tools (Fox ESS,
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
ui/                        # Epic 13b — SPA container (nginx + static assets)
  Dockerfile               # nginx:alpine + envsubst for runtime config
  conf/nginx.conf.template # reverse-proxies /api → hem; SPA fallback to cockpit.html
  html/                    # one HTML page per route (cockpit, history, forecast, insights, workbench, settings)
  src/{js,css}/            # vanilla JS + CSS, served by nginx; bearer injected by _api.js
  ui-entrypoint.sh         # writes /config.js with bearer + apiBase at container boot
.github/workflows/ui-publish.yml  # builds + pushes ghcr.io/<owner>/home-energy-manager-ui on push to main (paths-scoped)
quartz/                    # #542 — self-hosted Quartz solar-forecast sidecar (hem-quartz service)
  Dockerfile               # python:3.12-slim + quartz-solar-forecast (xgboost site-level model, MIT)
  app.py                   # FastAPI mirroring open.quartz.solar POST /forecast/ schema; lazy model warm-up
.github/workflows/quartz-publish.yml  # builds + pushes ghcr.io/<owner>/home-energy-manager-quartz (paths-scoped)
src/
  cli/__main__.py          # entrypoint: `python -m src.cli serve` (PID 1 in the container, behind tini)
  api/main.py              # FastAPI app + lifespan (token bootstrap, MCP session manager, scheduler) — JSON API only since B5
  api/middleware.py        # BearerAuthMiddleware (/mcp) + ApiV1BearerAuth (/api/v1/*, gated by HEM_UI_AUTH_REQUIRED)
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
  notifier.py              # OpenClaw hooks delivery — all notifications via POST /hooks/agent
  config.py                # all env-var config (Config dataclass)
  physics.py               # DHW setpoint calculations
  mcp_server.py            # FastMCP `build_mcp()` (HTTP in prod, stdio for dev)
bin/                       # dev-local launchers (./bin/serve, ./bin/mcp) — NOT used in prod
data/                      # state (DB + tokens). On the host: bind-mounted at /srv/hem/data → /app/data
.env                       # secrets + config (host: /srv/hem/.env mounted ro into the container)
.venv/                     # Python 3.12.3 venv for dev local (the prod image carries /opt/venv inside)
```

---

## What changed on 2026-04-25 (re-introduction of Docker, immutable)

- Image `ghcr.io/albinati/home-energy-manager` published from CI on every push to `main`
- MCP transport moved from per-call stdio subprocess (`./bin/mcp`) to long-lived
  HTTP under `/mcp`, guarded by `BearerAuthMiddleware` (token at
  `data/.openclaw-token`, generated by the lifespan on first boot)
- Singleton flock removed from `src/mcp_server.py` — container is the singleton; dev local is single-user
- OpenClaw runs as user `openclaw` (uid 2000), not in the docker group, no write access to `/srv/hem/data/`
- Daikin OAuth port corrected: **8080** (not 18080 as the prior CLAUDE.md claimed)
- Re-auth flow now via one-shot container (`deploy/compose.daikin-auth.yaml`)
- Rollback procedure documented in `deploy/README.md` § 8

### What changed on 2026-04-18 (the *first* Docker → native migration, now reversed)

Kept here for rollback context only. The 2026-04-25 work pulls back from this:
the issue with native-on-host was OpenClaw having read/write access to the
running code — security regression.

---

## Design System

Read `DESIGN.md` (repo root) before any visual or UI change. It is the source of
truth for color, typography, spacing, layout, motion, and the cockpit's
non-negotiables (deep-dark, borderless, no-emoji, semantic color, system fonts,
~0.2s perf budget). Token values are authoritative in `ui/src/styles/tokens.css`;
DESIGN.md is the rules + rationale. In QA, flag any UI code that deviates from it.
