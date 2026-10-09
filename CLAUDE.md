# home-energy-manager — Claude context

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

### Daikin cadence — bounded heartbeat refresh + post-write verification (#809)

Phase A (#306) took the heartbeat off the Daikin API, so the reconciler and
the user-override detector compared against a cache up to 30+ min old. Now:

- **Heartbeat refresh** (`DAIKIN_HEARTBEAT_REFRESH_ENABLED`, code default
  false, **prod true**): at most one read per `DAIKIN_HEARTBEAT_REFRESH_SECONDS`
  (1800 → ≤ 48/day), only while `quota_remaining > DAIKIN_RESERVE_FOR_HEARTBEAT
  + DAIKIN_HEARTBEAT_REFRESH_MIN_HEADROOM` (30 + 40), still under the service's
  90 s floor and `should_block`. Gate: `runner._heartbeat_daikin_refresh_allowed`.
- **Post-write verify** (`DAIKIN_POST_WRITE_VERIFY_ENABLED`, default true): a
  successful `apply_scheduled_daikin_params` schedules ONE read (job id
  `daikin_verify_pending`; a second write before it fires MERGES its keys and
  pushes the fire time out) at `max(DAIKIN_POST_WRITE_VERIFY_SECONDS,
  DAIKIN_REFRESH_MIN_INTERVAL_SECONDS + 30)` — above the service's anti-burst
  floor, so the read is a real one. It compares ONLY the keys actually PATCHed
  (`written`), never the whole params dict (an `lwt_offset` skipped because the
  zone is off is not "unverified"). A read the service throttled/served from
  cache is logged `unverified`, never `success` (`source=cache_throttled`). On a
  mismatch it retries once (+180 s, Onecta propagation lag) and only then raises
  one `notify_risk` per write (deduped via `db.acknowledge_warning`). `action_log`
  `daikin_write_verify` carries `expected` / `actual` / `matched` / `fresh` /
  `cache_source` / `attempt`. The read also refreshes the cache for the next
  heartbeat. During the active-mode soak budget (100/day) the heartbeat refresh
  self-throttles off; verification reads are not headroom-gated (~20/day).
- Budget arithmetic (180/day rolling): heartbeat ≈ 48 + verify ≈ 10 + rollups 4
  + LP-init ≈ 2 + viewer boost ≤ 10 + writes ≈ 20 ≈ 95–100, leaving ~80 for
  429 retries and manual MCP use. Watch `GET /api/v1/daikin/quota`.

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

### Tank model — the ambient is the HOUSE (#819, 2026-10-07)

Owner: "agora demora mais pra esquentar e é mais rápido pra esfriar". Measured
overnight coast (live `daikin_telemetry`, 23–07 local, Oct 2026): **0.34–0.37
°C/h** at ~45 °C vs 0.24 in the July heatwave. The joint UA+ambient fit
(`dhw/calibration.fit_ua_and_ambient`) is unidentifiable on real data (constant
ambient → −11 °C, linear-in-outdoor → slope 2.4; both rejected, #772) so the
tank ran on the databook pair (2.44 W/K, 22.4 °C effective) — which predicts
0.25 °C/h in every season.

- **`fit_ua_indoor_ambient`**: UA alone, each coast episode's ambient FIXED to
  the measured house indoor (`room_temperature_history`, ≥ 3 readings in the
  episode). Prod copy: **UA 3.2–3.3 W/K, τ ≈ 69 h, R² 0.70, 18–24 episodes**,
  `ambient_model="indoor_measured"`. Preferred by `refresh_dhw_calibration`;
  the joint fit (with its linear rescue) is the fallback and is stored under
  `alternative` / `indoor_fit` for audit.
- **`resolve_tank_params(ambient_c=live)`** honours a live indoor reading ONLY
  for an indoor-fitted UA (`source="measured_indoor"`, UA cap 6 W/K); the
  joint fit keeps its effective ambient and the databook ignores it — pairing
  a UA with an ambient it was not fitted against just moves the error.
  `live_indoor_ambient_c()` is the one reader (stale > `INDOOR_SENSOR_STALE_MINUTES`
  → None → the fit's mean indoor). Consumers: LP block ambient per slot
  (`lp_optimizer` seeds from `initial.indoor_c`), dynamic window
  (`resolve_window_decision_local`), boost-lift budget, deadband warmup force
  (`state_machine._warmup_deadband_force_reason`), shadow baseline.
- **Coast check telemetry**: nightly `dhw_calibration` component `coast_check`
  (+ `action_log` `tank_coast_check`): last episode's measured vs model °C/h at
  the episode's indoor, and `ratio_median_recent` over the last 7 episodes
  (one night is ±30 % with 1 °C quantisation). The model side is the params
  that were STEERING before tonight's refit (read before the upsert), so it is
  an out-of-sample check, not the fit's own residual. Surfaced in
  `/api/v1/status/feedback` → `dhw.tank_model`.
- Live-ambient semantics: `resolve_tank_params(ambient_c=live)` CLAMPS the
  live reading to the fitted indoor range ±3 °C (within 8–32) — never rejects
  it (a 30.5 °C summer house or a 9.5 °C heating-fault house must not flip
  the planner back to the databook pair). The indoor join in the fit is an
  equal-weight mean across rooms of each room's episode mean (same definition
  as `get_latest_indoor_reading`), needing ≥ 3 distinct timestamps spanning
  ≥ 50 % of the episode.
- NOT done here (decision 2026-10-07): `DHW_LP_OWNED_ENABLED` stays off — the
  shadow gate read median **−6.4 p/day** (LP-owned dearer) over 20 days with
  the mis-specified coast on both arms. Re-read `evaluate_gate()` after ≥ 7
  days on the indoor-fitted tank.

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

## Tariff structure — Cosy (banded) vs Agile (dynamic), #803/#804

The household moved from Agile to **Cosy Octopus** (`E-1R-COSY-22-12-08-H`,
effective 2026-10-06): three flat bands on the LOCAL clock — cheap 12.49p
(04–07, 13–16, 22–24), day 25.45p, peak 38.17p (16–19). Every price classifier
was percentile-based and misread a 3-level day (the q75 of a Cosy day IS the
day band, so 13 h of day band read as "peak"; the strict `< cheap_thr` heartbeat
never saw the cheap band). `src/energy/tariff_structure.py` is now the ONE
place that decides `banded` vs `dynamic` and derives thresholds:

- **banded** (≤ `TARIFF_BANDED_MAX_LEVELS` distinct levels) → `cheap_thr` /
  `peak_thr` are the **midpoints between bands** (Cosy: 18.97 / 31.81), so every
  existing `<`/`<=`/`>`/`>=` consumer classifies the bands correctly. The LP
  plan carries `tariff_structure_kind` + per-slot `price_band`.
- **dynamic** (Agile) → bit-for-bit the historical formulas (`dynamic_rule="lp"`
  = q25/q75 in `solve_lp`; `"legacy"` = `min(mean×0.85,q25)` / `max(q75,25p)`
  in `_classify_slots`, `/api/v1/agile/day`, `analytics.patterns`).
- Horizon filler: on a TOU-family code the tail of the 48 h window is filled
  from the stored rows bucketed by LOCAL (hour, minute)
  (`band_profile_local`, DST-safe); Agile keeps the UTC-keyed 28-day priors.
  Synthetic rows keep `fetched_at="prior"` (dhw_policy treats anything else as
  REAL) and add `prior_source="prior_band"|"prior"`.
- Consumers with an ABSOLUTE pence floor keep it on top of the band threshold
  (`dhw_policy._evening_peak_entry_hour`, brief `_tariff_peak_windows_summary`).
- Kill switch: `OCTOPUS_TARIFF_STRUCTURE=dynamic` → pre-#804 behaviour everywhere.

## Battery policy on Cosy — battery-only arbitrage, no grid at peak (#806)

Household policy for autumn/winter: **the battery never discharges to the grid
and the 16–19 peak band is never bought from the grid.**

- `LP_BATTERY_EXPORT_ENABLED=false` (prod; code default `true`): `exp <= pv_use`
  in EVERY preset (vacation included), the pre-negative drain relaxation and
  the export rank bonus are off, and `build_fox_groups_from_lp` downgrades any
  `peak_export` / `pre_negative_export` slot to `standard` (`action_log`
  `export_slot_suppressed`) — **no ForceDischarge group can be uploaded**.
  Incidental PV surplus still exports (curtailing it earns nothing).
- `LP_PEAK_IMPORT_PENALTY_PENCE_PER_KWH=100` (prod; default 0): soft cost on
  `imp` inside the PEAK band (`plan.price_band == "peak"`, banded tariffs only;
  `LP_PEAK_IMPORT_PENALTY_APPLY_DYNAMIC=true` extends it to Agile via
  `price >= peak_thr`). A cost, never a constraint → a genuine shortfall still
  imports instead of going Infeasible. `plan.peak_import_kwh` records what the
  committed plan still buys at peak (target 0). The scenario solves share the
  term, so the pessimistic SoC floor at 16:00 (`LP_PESS_CHARGE_FLOOR_SCOPE=
  peak_entry`) already covers the whole peak under p75 load / PV×0.85.
- Fox shape 16–19 = **SelfUse(reserve)**: the battery serves the house. Not
  Backup (never discharges to loads → house grid-fed at 38p).
- Heartbeat **peak-import guard** (`PEAK_IMPORT_GUARD_*`): in the PEAK band,
  grid import ≥ 0.3 kW for 2 consecutive ticks → ONE `notify_risk` per peak
  window (`warning_key=peak_import_<date>_<HHMM>`) + `action_log`
  `peak_import_guard` + `PEAK_IMPORT_GUARD_ACTION=replan` (MPC re-solve,
  `trigger_reason=peak_import`, runs the scenario stack). `none` = alert only.

### How much to buy in each cheap window — probabilistic load (#818)

Prod `load_error_log` (60 d to 2026-10-06) showed the committed load forecast
under-forecast the 16–19 band on 22/30 days (mean +0.93 kWh/day, p90 +2.2 —
electric cooking at dinner; weekend lunch 12–13 is 2.6–3.3 kWh/h). Three facts
drive the design:

- **The LP plans against per-slot medians**; the only uncertainty it carries is
  the per-slot p75 that the pessimistic scenario uses, and the charge floor is
  taken from that solve. Slot-sum p75 (Tue 16–19: 3.85 kWh) ≈ the realised
  BAND p90 (4.0), while slot-sum p90 (6.5) overshoots ~60 % (tails don't add).
  So `LP_LOAD_EXPENSIVE_BAND_QUANTILE` stays `p75`; `p90` is an over-insurance
  knob (day+peak bands of a banded tariff only; Agile never touches it).
- **The profile learns the WHOLE window now.** `residual_load_profile_v2`
  dropped every sample without a `meteo_forecast_value` outdoor temperature
  (30-day retention) → a 120-day window learned from 31 days (8 weekends).
  Fallback: `execution_log.daikin_outdoor_temp` by UTC hour
  (`outdoor_from_daikin_samples` in the profile). Window length barely matters
  (30/60/120-day backtests within 0.1 kWh); the recent-bias corrector
  (`LOAD_RECENT_BIAS_ENABLED`) is **−6.7 % MAE out-of-sample** — keep it off.
- **Every cheap→non-cheap boundary is a charge-decision boundary.**
  `LP_PESS_CHARGE_FLOOR_BAND_EXITS=true` (default; banded tariffs only; lives
  INSIDE `LP_PESS_CHARGE_FLOOR_SCOPE=peak_entry` — prod — and is inert under
  `trajectory`, which floors every slot anyway) adds the 07:00 (after 04–07)
  and the midnight (22–24 → next day's 00–04) cheap exits to the floor set — buying 07–13 at 25.45p instead of
  12.49p costs 13p/kWh on 4.6 (weekday) – 8.1 (weekend) kWh. Entries INTO
  cheap (13:00, 22:00) and the 19:00 peak→day entry are never floored (the
  latter would make the nominal plan hold charge through the peak).

`GET /api/v1/load/expected?date=` (`src/analytics/load_expected.py`) is the
household-level view: per tariff window of the day, **band-sum p50/p75/p90
over same-day-type history** (weekday/weekend, `LOAD_EXPECTED_HISTORY_DAYS`
60), the committed plan's kWh, realised so far, and the committed forecast's
error history in that block. Feeds the Home consumption card.
### Plan per front — `GET /api/v1/plan/fronts` (#821)

One viewer-safe read (`?date=YYYY-MM-DD`, default today local; 400 on a bad date or outside [today-7, today+1])
for the Home page, composed by `src/analytics/plan_fronts.py`: `tariff` windows
(`band_windows_for_day`), `battery` (contiguous same-kind windows from the latest
LP run's slots — grid_charge / pv_charge / export / hold (Fox Backup group) /
self_use / idle — plus per-tariff-window planned vs realised grid import, SoC at
entry and `floored` = window entry slot in the pessimistic floor's
`entry_slots` — a CANDIDATE boundary persisted only when a re-solve ran, so read it as "candidate boundary in a run where the floor bound"; `floor_binding_slots` gives the count), `tank` (telemetry, `resolve_tank_params` + `coast_check`
calibration, `read_window_decision`, windows from the shared
`dhw_policy.dhw_schedule_rows_for_day` that `/daikin/dhw-schedule` also uses,
shower floors with an honest coast prediction from the warmup target — the LP slot `tank_temp_c` is the pinned phase target, not a physical temperature), `heating` (LWT rows from
`action_schedule`: boost/setback/restore with source lp|tier, gate state,
predicted indoor), `consumption` (exactly `expected_load_by_band`), `spend` and
`compare` (`cached_fair_comparison`, 900 s cache shared with `/tariffs/fair-compare`, month-to-date, 4 tariffs; week/month PnL rollups cached 600 s).
Every section is guarded on its own (`{"error": ...}`); the response is cached
60 s per `(DB_PATH, date)` (the comparison takes seconds), bypassed when
`now_utc` is passed.

**Spend score.** `ideal_avg_import_p` = cheapest band price (banded) or the
day's q25 (dynamic); `ideal_max_p = ideal × SPEND_SCORE_IDEAL_RATIO` (1.15),
`above_min_p` = `cheap_thr` (banded) or q50. `ideal` when avg ≤ ideal_max and
peak import ≤ 0.1 kWh (peak clause skipped on dynamic tariffs), `below` when avg
≤ above_min, else `above`. Basis is realised once ≥ 1 kWh was imported, else the
committed-plan forecast.

## Thermal control on Cosy — band rule, W3 thermal model, LP-owned LWT (#808)

Space heating is shaped by the Daikin **LWT offset** rows (`lwt_preheat` +
`restore` in `action_schedule`), written by `_write_lwt_preheat_actions` at
every dispatch. Two sources are computed EVERY time and diffed into
`action_log` (`lwt_source_diff`: `source_used`, `n_differ`, `mean_abs_diff`,
disagreeing `windows`); **`DAIKIN_LWT_SOURCE`** (runtime-tunable, `PUT
/api/v1/settings`, no restart) picks which one reaches the device:

- `tier` — the price-band rule: `cheap` → `+DAIKIN_LWT_PREHEAT_BOOST_C` (3),
  `peak` → `DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C` (−2), `standard` → 0, negative
  → `+DAIKIN_LWT_PREHEAT_NEGATIVE_BOOST_C`. On a banded tariff the band comes
  from `plan.price_band` (no threshold comparisons). This is the kill switch.
- `lp` — the LP's own W3 thermal plan (`LP_W3_TIN_ENABLED=true`, RC model
  with learned τ / UA / C, soft 3-level comfort floor: night `LP_W3_NIGHT_FLOOR_C` 22–07 (code default 17.5,
  **prod 20** since 2026-10-09), **peak band = `INDOOR_SETPOINT_C −
  LP_W3_PEAK_COAST_DELTA_C`** (code default 1.0, **prod 0**), setpoint otherwise,
  plus the soft ceiling `LP_W3_CEILING_C`, #841). `plan.lwt_offset_c` is
  TRANSLATED, never written raw: a slot the LP left without space heat while
  the weather curve would run the compressor is a deliberate coast → the
  setback (the inverse physics returns `OPTIMIZATION_LWT_OFFSET_MIN` = −10
  there); outdoor ≥ cutoff → no write; clamp `DAIKIN_LWT_LP_OFFSET_MIN/MAX`
  (±5); then the same smoothing / restore / quota cap / pre-fire idempotency
  / drift backstop as the tier rule. Falls back to `tier` when the LP had no
  indoor trajectory (stale sensor, passive mode, flag off) — the diff row
  says `lp_available=false`.
- **Comfort guard on LP offsets** (`_lp_offsets`): the LIVE reading guards only
  the slots within `INDOOR_SENSOR_STALE_MINUTES` of now (boost side vs the
  ceiling-based `_boost_guard_c`, cold side vs that slot's floor); far slots are
  never vetoed by a reading or by the predicted trajectory. (The per-slot
  `_indoor_for_slot_fn` trajectory guard of the first #808 cut no longer exists.)
- When `DAIKIN_LWT_SOURCE=lp` the LP's `e_space` ceiling is capped at the ±5
  clamp so the plan never assumes more lift than the device will get.
- **Plausibility gate** (`w3_trajectory_plausible`): the LP source is
  unavailable (diff row `lp_available=false`, `lp_reason`) when the plan
  carries comfort SLACK (`plan.comfort_slack_c` = FLOOR shortfall only, >
  `LP_W3_SLACK_TOL_C` 0.1 °C) in more than `LP_W3_MAX_SLACK_SLOTS` (4) slots —
  floor slack is only ever used when the pump cannot hold the per-slot floor,
  i.e. the RC model cannot hold the house (unfitted UA/k) — or when ceiling
  slack (`plan.comfort_slack_hi_c`) coincides with planned space heat in such a
  slot (overheating on purpose; a house that merely STARTS above the ceiling has
  unavoidable overshoot and is not a veto) — or when any predicted value is more
  than `LP_W3_IMPLAUSIBLE_BELOW_FLOOR_C` (2.0) under the night floor / 2× that
  above the ceiling (#841; was the setpoint). The predicted trajectory is
  NEVER used to veto the plan's own offsets (that was circular); the only guard
  on LP offsets is the LIVE reading, on slots near now, boost side. The `tier`
  rule never reads the trajectory, so `DAIKIN_LWT_SOURCE=tier` is a true kill
  switch even with W3 on. LP offsets are block-ified by SIGN before the
  `DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS` filter (`smooth_lp_offsets`).
- **Comfort policy knobs (#820, runtime-tunable, `PUT /api/v1/settings`):**
  `LP_W3_NIGHT_FLOOR_C` (how cold the house may drift 22–07; code default 17.5, prod 20),
  `LP_W3_PEAK_COAST_DELTA_C` (how far it may coast through the peak; code default 1.0, prod 0) and
  `INDOOR_SETPOINT_C` — plus **`INDOOR_COMFORT_AGGREGATE`** = `mean` (default)
  | `min` | `max` | `room:<name>`: which reading is THE house temperature that
  seeds `t_in[0]` and that the comfort guard compares against
  (`db.get_latest_indoor_reading` → `aggregate_indoor_c`). The owner accepts
  the kitchen colder than the corridor; `room:corredor` plans comfort on the
  living space while `rooms_c` / `spread_c` keep the cold room visible.
  The W2 learner fits on the room MEAN while W3 seeds from the aggregate, so a
  `min` with a large spread biases the RC seed; keep `mean` or `room:` of a
  representative room. The plausibility gate judges the trajectory against the
  floor/setpoint recorded ON the plan (`plan.w3_night_floor_c`), not live config.
- **UA must be model-consistent before W3 drives hardware.** The LP's pump
  model is `k × (LWT − 18)` with the learned `k` (prod 0.063 kW/°C) — at 5 °C
  outdoor it can hold the house only up to UA ≈ 200 W/K. The env default
  `BUILDING_UA_W_PER_K=600` (and C = τ·UA ≈ 50 kWh/K) makes the trajectory
  fall monotonically and the comfort slack dominate the objective. Prod pins
  `BUILDING_UA_W_PER_K=200` (provisional; C = 82.7 h × 200 ≈ 16.5 kWh/K). NB
  `get_building_ua_w_per_k()` PREFERS a learned value in (100, 1500): once
  `fit_ua_hdd` converges (≥ 20 heating days; last winter's HDD regression gave
  520–730 W/K, which the pump model cannot hold) it silently overrides the pin
  — the slack gate is what keeps a non-holdable model off the hardware. Kill
  switches: `LP_W3_TIN_ENABLED=false` (the model), `DAIKIN_LWT_SOURCE=tier`
  (the hardware path).
- Rollout: deploy with `tier` → read `lwt_source_diff` + `plan.indoor_temp_c`
  for a day (`lp_available=true`, boosts only in cheap bands, setbacks only in
  the peak, trajectory within 17–23 °C) → `PUT /api/v1/settings`
  `DAIKIN_LWT_SOURCE=lp`. Status: `space_heating_gate_state()` →
  `lwt_source`, `lwt_source_last_diff`.

### Comfort feedback loop (#833)

The owner's "how does the house feel?" closes the loop. Three inputs, one table
(`comfort_feedback`: verdict cold|ok|hot, optional room/note, plus the context at
that instant — aggregate + per-room indoor, outdoor, active LWT offset, price
band, `DAIKIN_LWT_SOURCE`):

- **API**: `POST /api/v1/comfort/feedback` (admin; `{verdict, room?, note?}`),
  `GET /api/v1/comfort/feedback?days=30` (viewer; rows + per-room counts + weekly summary).
- **Home**: admin-only Cold / OK / Hot buttons (+ room select) in the Heating card.
- **OpenClaw / MCP (this household)**: tools `record_comfort_feedback(verdict, room?,
  note?)` (verdict cold|ok|hot|frio|quente; room matched against fresh sensor rooms;
  stored with `source="openclaw"`) and `get_comfort_feedback(days)`.
- **Telegram poller (OFF by default, `TELEGRAM_INBOUND_ENABLED=false`)**. Precondition:
  the bot token must be EXCLUSIVE to HEM — Telegram allows one `getUpdates` consumer
  and no webhook; here OpenClaw owns the bot, so the poller stays off (code kept for a
  future dedicated bot). When on: short-poll every `TELEGRAM_INBOUND_POLL_SECONDS`
  (own job `telegram_inbound_poll`, offset in `kv_state`), PRIVATE chat with
  `TELEGRAM_CHAT_ID` only (optional `TELEGRAM_OWNER_USER_ID` also checks `from.id`),
  stale messages (older than max(300 s, 2x poll)) are ignored, 3 consecutive failures
  -> one alert + 15 min back-off (429 honours `retry_after`). Commands:
  `/conforto frio|ok|quente [cômodo] [nota]` / `/comfort cold|ok|hot [room] [note]`;
  `/conforto` alone = usage + rooms; other slash commands get no reply.
- **Weekly** (`src/analytics/comfort_feedback.py`, Sunday 08:45 local): summary to
  `action_log` (device `comfort`, action `weekly_summary`). Proposal rules: >=2 cold
  at night at/below floor+0.3 -> `LP_W3_NIGHT_FLOOR_C` +0.5 (cap 22); >=2 cold in
  the peak band -> `LP_W3_PEAK_COAST_DELTA_C` -0.5 (min 0); >=3 hot and no cold ->
  the inverse. `external_comfort_signal(week_start)` exposes it to the suggestions
  story. **Nothing is applied** unless `COMFORT_FEEDBACK_AUTO_TUNE=true` (default
  false); then only that one bounded +-0.5 change per week via
  `runtime_settings.set_setting` (action_log `auto_tune`).

### Cosy daily scorecard (#831)

`src/analytics/cosy_scorecard.py` scores YESTERDAY (local day) once a day:
job `cosy_scorecard` at `COSY_SCORECARD_HOUR_LOCAL:MINUTE` (07:30
`BULLETPROOF_TIMEZONE`, before the 08:00 brief; a one-shot 120 s after boot
back-fills any missing day of the last 7) persists one row in
`cosy_scorecard_daily` (indexed: score, peak/import kWh, import £, avg vs ideal
p, net £; everything else in `payload_json`). Read-only — it NEVER changes a
setting. Payload sections, each guarded on its own: `spend` (the SAME
`plan_fronts.spend_section` the Home uses, so the score agrees), `bands` (per
tariff window: import kWh/£, load, PV, battery discharge, forecast-vs-actual load
error + `under_forecast`), `battery` (planned vs realised SoC at 07:00 / 16:00 /
00:00, pessimistic-floor binding slots, cycles), `comfort` (per room min/max/mean,
hours below the night floor and below the peak-coast floor, using
`INDOOR_COMFORT_AGGREGATE`), `tank` (°C at each shower-window entry vs floor,
window decision, `coast_check` ratio), `lwt` (preheat/restore rows, `daikin_write_verify`
success/unverified/mismatch, last `lwt_source_diff`), `ops` (Daikin calls, Fox
failures), `money` (net £, delta vs fixed, rolling-7 mean). Surfaces:
`GET /api/v1/scorecard/cosy?days=14` (viewer, clamp 1..90, newest first), the
Insights "Cosy scorecard" card, ONE line in the morning brief. One `notify_risk`
per condition per date (dedupe via `acknowledge_warning`, keys `cosy_*_<date>`):
3 consecutive days of peak-band load under-forecast, tank below a shower floor at
entry, any write-verify mismatch, Daikin calls > `COSY_SCORECARD_QUOTA_ALERT` (150).
`COSY_SCORECARD_ENABLED=false` switches the job off.

### Weekly fine-tuning review (#832) — suggestions only

`src/tuning_review.py`, APScheduler job `tuning_review_weekly` (Sunday 09:00 local:
`TUNING_REVIEW_ENABLED=true`, `TUNING_REVIEW_DOW=6`, `TUNING_REVIEW_HOUR_LOCAL=9`).
Replays the last 7 complete local days (`replay_day`, forward mode, live DB,
`TUNING_REVIEW_CADENCE=stride:4` recalcs per day) with the control and each knob
at +/-1 step (`LP_W3_NIGHT_FLOOR_C` 0.5, `LP_W3_PEAK_COAST_DELTA_C` 0.5,
`INDOOR_SETPOINT_C` 0.5, `DHW_TEMP_NORMAL_C` 1,
`LP_LOAD_EXPENSIVE_BAND_QUANTILE` p75<->p90 — now a runtime setting), via
`lp_overrides.patched_config` (in-memory, restored; **it never calls
`set_setting`**). Cost = replayed plan cost under actual prices; comfort = hours
the predicted indoor trajectory is below the CONTROL's night floor / peak floor
plus the tank shortfall at 20:00 vs `shower_windows`. Verdicts: `recommended`
(saves >= 5 p/week, hours-below +<=0.5, no extra shower-shortfall day),
`trade-off` (saves but costs comfort), `comfort-first` (cheapest zero-hours variant
when the control has hours-below > 0); the rest is dropped. Rows land in
`tuning_suggestions` with a PUT-ready payload (`PUT /api/v1/settings/{key}/simulate`
then `PUT /api/v1/settings/{key}` + `X-Simulation-Id`, body `{"value": ...}`).
`GET /api/v1/tuning/suggestions?weeks=4` (viewer), `POST /api/v1/tuning/run`
(admin, single-flight 10 min, 409 when busy). One Telegram summary (top 3), muted
when nothing is recommended. Insights card `TuningSuggestionsCard` (admin Apply =
the Settings simulate->confirm->apply flow). Story-3 plug point:
`external_comfort_signal(week_start)` (returns None today; the dict is stored in
`payload.external_comfort`, ranking untouched). Skips when < `TUNING_REVIEW_MIN_DAYS`
(3) replayable days, or when `DAIKIN_CONTROL_MODE=passive` / `LP_W3_TIN_ENABLED=false` (W3 knobs would be inert; forward mode uses the LIVE process config, recorded as `payload.context`). `DHW_DYNAMIC_BOOST_HOLD_HOURS` is NOT swept (its only reader is the persisted nightly window decision, never re-resolved by a replay). Replays seed W3 from `lp_inputs_snapshot.indoor_initial_c` and chain `plan.indoor_temp_c`. Single-flight has no TTL (variants share `config._overrides`); the per-day budget (~9 min) stops the run (status `partial`, persisted in the payload).

### LWT coast mode, comfort backstop and learning log (#838)

- **`DAIKIN_LWT_COAST_MODE`** (runtime setting, `PUT /api/v1/settings`; code
  default `setback`): what an LP **coast slot** (planned `e_space ≈ 0` while the
  weather curve would run) writes when `DAIKIN_LWT_SOURCE=lp`. Three values:
  - `setback` — the fixed `DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C` (−2, pre-#838).
  - `lp` — **physics target** (`scheduler/lwt_coast.py:coast_target`): water just
    above the predicted room temperature cannot add heat, so the compressor stays
    off. `coast_lwt = indoor_pred[i] + DAIKIN_LWT_COAST_DELTA_C` (default 2.0);
    `offset = floor(coast_lwt − curve_lwt + 0.5)` (pipeline rounding), `curve_lwt` =
    `physics.get_lwt_base_c(forecast outdoor)`; clamped to
    `[DAIKIN_LWT_LP_OFFSET_MIN, 0]` (a coast slot never boosts). Falls back to the
    live indoor reading (near-now slots), then to the setback value.
  - `lp_raw` — the LP's raw `plan.lwt_offset_c[i]`, clamped only by
    `DAIKIN_LWT_LP_OFFSET_MIN/MAX` (±10 hard bound). The inverse physics of ZERO
    draw is the range minimum, so every pure coast slot becomes −10: experiments only.
  Heating (non-coast) slots always keep the LP's inverse-physics offset with the
  existing clamp. Outdoor cutoff, sign-block smoothing, restore rows, quota cap,
  pre-fire idempotency, live-only boost guard and the plausibility gate are
  unchanged. `coast_mode` is in `lwt_source_diff`, `space_heating_gate_state()`
  and the plan-fronts `heating.coast_mode`. **Smoothing (`smooth_lp_offsets`,
  #839 review):** HEATING blocks are split when a value is ≥ 2 °C from the
  block's first slot, at every `price_band` change and at every heating↔coast
  flip, then take the block mean; sub-blocks shorter than the minimum are merged
  back into the longer neighbour (planned heating is never dropped by a split —
  only a whole same-sign run shorter than the minimum is). COAST runs keep their own per-slot values (the
  forecast-driven depth); only value-runs shorter than
  `DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS` are merged into the longer neighbour (ties:
  the shallower), and a whole coast run shorter than the minimum is dropped.
- **Absolute LWT ceiling** `DAIKIN_LWT_ABS_MAX_C` (45; backup-heater exposure):
  on the LP heating path `off = min(off, max(0, floor(ABS_MAX − curve_lwt + 0.5)))`
  — it only blocks lift, never forces a setback; `lp_optimizer`'s `space_ceil_kwh`
  mirrors it (source=lp) so the plan never assumes lift the device won't get.
- **Comfort backstop** (`LWT_COMFORT_BACKSTOP_ENABLED=true`, `_MARGIN_C=0.5`,
  `_TICKS=2`; `scheduler/lwt_coast.py:backstop_tick`, called from the heartbeat
  before the reconciler): an ACTIVE `lwt_preheat` row with a negative offset AND
  the fresh aggregate indoor reading under the CURRENT floor − margin for N
  consecutive ticks (floor = night `LP_W3_NIGHT_FLOOR_C` in the LP night window,
  `INDOOR_SETPOINT_C − LP_W3_PEAK_COAST_DELTA_C` in the peak band, else the
  setpoint) → `apply_scheduled_daikin_params({"lwt_offset": 0})` (post-write verify
  runs), row set `completed` with `error_msg='comfort_backstop'` (the restore row
  then state-matches as a no-op), one `notify_risk` per window
  (`lwt_backstop_<date>_<HHMM>`), `action_log` `lwt_comfort_backstop`, then
  `bulletproof_mpc_job(bypass_cooldown=True, trigger_reason="lwt_backstop")` (no
  scenario stack). Needs `DAIKIN_CONTROL_MODE=active`, not `OPENCLAW_READ_ONLY`.
  The write uses `skip_if_matches=False` and the tick only counts as fired
  (row completed / notify / hold) when `apply_scheduled_daikin_params` returns
  True; otherwise `lwt_comfort_backstop` is logged `skipped` and the counter stays
  armed. A stale/absent sensor HOLDS the tick counter (no reset). **Anti-oscillation
  (#839):** firing records a hold (`LWT_COMFORT_BACKSTOP_HOLD_MINUTES`, 90; `kv_state`
  key `lwt_backstop_hold_until`, survives restarts, exposed as
  `space_heating_gate_state()["backstop_hold_until"]`): `_lp_offsets` AND
  `_tier_offsets` emit 0 for negative offsets on slots starting before it. The LP
  source also has a symmetric LIVE cold guard — a negative offset on a near-now slot
  (±`INDOOR_SENSOR_STALE_MINUTES`) is zeroed when the fresh reading ≤ that slot's
  floor − `LWT_COMFORT_BACKSTOP_MARGIN_C` (`lwt_source_diff.guards`). Notify dedupe
  key = the hold start.
- **Learning log** `lwt_learning_log` (PK `slot_time_utc`): PLANNED fields
  (`run_id, source, coast_mode, offset_lp_raw, offset_written` = after smoothing,
  `indoor_pred_c, floor_c, margin_c` = predicted headroom over the floor,
  `outdoor_fc_c, e_space_kwh, cop_space, price_band`, and for coast slots
  `curve_lwt_c, coast_target_lwt_c, coast_delta_c`) are upserted at every
  dispatch (latest plan wins, `written_at_utc` kept, filled slots untouched).
  Nightly `lwt_learning_job` (04:40 UTC) fills REALISED fields for yesterday's
  local day (per-room mean + min from `room_temperature_history`, outdoor and
  `lwt_actual` from live `daikin_telemetry`, device offset from `execution_log`,
  `kwh_heating` of `daikin_consumption_2hourly` prorated over its slots) and
  writes one `lwt_learning_daily` row (payload also carries the realised
  pump-off delta `lwt_actual − indoor` where heating kWh stayed ≈ 0 vs pump-on,
  to fit the real `DAIKIN_LWT_COAST_DELTA_C`) + `action_log` `lwt_learning_summary`.
  `run_id` is stamped AFTER `log_optimizer_run` (`lwt_coast.stamp_run_id`; rows carry
  `plan_updated_at_utc` as the plan token, `written_at_utc` stays the first write);
  `offset_written` is recorded after the quota-cap trim (NULL for dropped windows).
  Pruned after `LWT_LEARNING_RETENTION_DAYS` (120).
- **Reading the estimates (#843)**: coast-only data identifies **τ = C/UA only**.
  `ua_est_w_per_k` / `ua_est_night_w_per_k` (API + payload: `ua_from_tau_scaled_*`,
  `ua_est_circular: true`) is `C × decay rate` with C = τ × UA_pin (#841), so it returns ≈ the
  pin whenever nights cool at the calibration's τ — it CANNOT confirm or refute the 200
  pin; kept for audit (details block of the card), never a headline. UA and C come from
  the **episode estimator** `fit_ua_c_joint` (payload `joint_fit`, rolling `joint_window_days`=14).
  Heat input is ONLY Onecta-METERED buckets (`lwt_learning_log.heating_kwh_source` starts with
  `onecta`; `telemetry_integral` buckets are the weather-curve model `get_daikin_heating_kw ×
  dt`, not a measurement, and are excluded; 1.0-kWh phantom Onecta buckets are zeroed in
  `fill_realised` by `thermal_learning.sanitize_phantom_heating`). Samples: one per contiguous
  Onecta-heating EPISODE (+ a 4-slot coast tail for emitter lag; dropped when an adjacent
  bucket is unusable, e.g. metered 0 but `lwt_actual − indoor > 6 °C` = hidden heat the
  whole-kWh counter rounded away) and one per 2 h Onecta-zero coast block lying wholly in the
  local night (22–07: no solar). Model `ΔT = a·ΣQ_th − b·Σ(T−To)Δt + g·Σdt` (Q_th = COP(To) ×
  metered kWh, COP = the LP curve with its lift derate; a=1/C, b=1/τ, g=gain/C); 3-param OLS by
  hand, SEs σ²(XᵀX)⁻¹ + delta method (`ua_se`, `c_se`), with a method-of-moments correction for
  the counter's known quantisation noise (1/12 kWh² per bucket). Reported: `ua_w_per_k`,
  `c_kwh_per_k`, `tau_h`, `gain_kw`, `resid_rms_c`, `n_heat_episodes`, `n_coast_blocks` (no R²
  headline). Gate: ≥ 5 episodes AND ≥ 8 coast blocks AND a,b > 0, else `identifiable: false`
  with `reason` ∈ `too_few_heat_episodes | too_few_coast_blocks | nonphysical_fit | singular |
  no_measured_input`. Diagnostics: `tau_fixed` (b HARD-fixed to the learner's τ — a
  constraint, not a prior), `slot_fit` (old per-slot regression, biased by quantised/lagged
  heat), `coast_tau_h`, `consistency_flag: lag_or_gain_contamination_suspected` when the free τ
  and the coast-only τ differ by > 25 %, and `cop_sensitivity` (refit at COP ×0.8 / ×1.2 — COP
  is the weakest link; SEs are optimistic since COP, lag and quantisation are model error).
  Honest model checks: `pred_err_*` and `night_rise_per_band` (each cheap band with a
  positive written offset: measured vs predicted indoor rise from slots carrying BOTH readings;
  predicted only from slots sharing the first slot's `plan_updated_at_utc` token — `mixed_plans`
  rows are not comparable; `model_error_c` > 0 = the model is pessimistic). `k_est_kw_per_c` =
  median over 2-hour buckets of kWh ÷
  Σ((`lwt_actual` − 18)·Δt) (≥ 3 samples, `lwt_actual` > 20); learned k pin 0.063.
  Nothing is auto-applied (`thermal_calibration` is untouched); read via
  `GET /api/v1/thermal/lwt-learning?days=14` or the Insights "LWT learning" card.
  The scorecard `lwt` section carries `lwt_backstops` (count of fired backstops).
  **Discontinuity:** the circular `ua_est_*` scales with the C it is fitted with, and C
  went from 49.6 to 16.5 kWh/K at the #841 C fix (~3x): that series before/after is NOT
  comparable. The payload carries `c_kwh_per_k` per day.

### Banking heat in cheap bands — consistent C, comfort ceiling, ceiling-based boost guard (#841)

The LP plans "bank heat in the cheap bands, coast through day/peak"; three of our
own constants/guards used to stop it reaching the device:

- **C must match the EFFECTIVE UA.** `thermal_calibration.c_kwh_per_k` is
  `τ × UA` for the UA it was computed with (`c_ua_basis_w_per_k`, nullable
  column; pre-#841 rows = unknown). `get_building_thermal_mass_kwh_per_k()` /
  `thermal_mass_resolution()` return the stored C only when the basis equals
  `get_building_ua_w_per_k()` (±1 W/K); a different or unknown basis →
  `τ_eff × UA_eff / 1000` (logged once). Prod: 49.6 (τ 82.7 × env UA 600) →
  16.5 kWh/K under the 200 W/K pin — with 49.6 the plan could not hold the day
  floor, produced `comfort_slack` in 5–6 slots and the plausibility gate fell back
  to the tier rule. `/api/v1/sensors/thermal-calibration` `effective` carries
  `c_basis_ua_w_per_k` + `c_recomputed`; the Insights "LWT learning" card shows
  τ / UA / C (+ basis).
- **`LP_W3_CEILING_C`** (runtime setting, default 23.0, 18..28, validated
  `>= INDOOR_SETPOINT_C + 0.5`): W3 soft upper bound `t_in[i+1] − s_hi[i] <= ceiling`,
  slack penalised like the floor slack (`LP_W3_COMFORT_PEN_PENCE_PER_DEGC_SLOT`).
  `plan.w3_ceiling_c` records it; `plan.comfort_slack_c` stays FLOOR slack only
  (the model-health signal), `plan.comfort_slack_hi_c` is the ceiling overshoot.
  The plausibility gate counts a slot with ceiling slack only when the plan HEATS
  in it (`space_electric_kwh > 0`) — a house that starts at 23.5 against a 23
  ceiling has unavoidable overshoot and must not disable the LP source.
  Its upper bound is `ceiling + 2·LP_W3_IMPLAUSIBLE_BELOW_FLOOR_C` (was
  setpoint-based). ONE reader for the ceiling:
  `lwt_coast.effective_w3_ceiling_c()` = `max(LP_W3_CEILING_C, INDOOR_SETPOINT_C + 0.5)`
  (LP, boost guard, gate fallback); both settings are validated against each other.
  `lwt_learning_log.ceiling_c` is stored per slot; `plan/fronts` `heating.ceiling_c`
  reads the ceiling the latest plan of the day was solved with.
- **Boost guard vs the ceiling.** A positive offset (`_preheat_lwt_offset` tier
  rule, `_lp_offsets` near-now live guard) is zeroed when the LIVE indoor reading
  `>= LP_W3_CEILING_C − DAIKIN_LWT_PREHEAT_COMFORT_BAND_C` (22.5) instead of
  `setpoint + band` (21.5). The predicted trajectory still never vetoes offsets
  (the LP already enforces the ceiling). The backstop's floor logic is unchanged.
- Telemetry: scorecard `lwt.heating_kwh_by_band` (cheap/standard/peak, 2-hourly
  `kwh_heating` prorated per 30-min slot) + `lwt.indoor_min_c/indoor_max_c` (aggregate);
  `plan/fronts` `heating.ceiling_c`.
- **What the LP actually writes at prod constants** (UA 200 W/K, C 16.5 kWh/K,
  floor 21 / night 20 / peak delta 0, ceiling 23): cheap bands +10 (the lift cap),
  day-band slots −1/−2 ("hold 21"), peak coast −10. The ceiling is a BOUND, not a
  target, and banking is capped at ~0.5 K per cheap band: net input per slot is
  `(min(RADIATOR_MAX_KW, lift ceiling)·COP − UA·ΔT)/C` ≈ 0.09 K at UA 200 / C 16.5
  (the LP's COP comes from `DAIKIN_COP_CURVE`, 4.1–4.5 at 6–9 °C, NOT
  `weather.cop_space`). "Coast through 07–13" needs a lower fitted UA or a bigger
  pump; the ceiling only starts to bind for a small-C / leaky / strong-pump house.
- `thermal_mass_resolution` recomputes C only for `tau_x_env_ua` / `tau_x_learned_ua`
  rows (a measured C is kept); an out-of-bounds stored C is replaced by τ×UA_eff and
  reported (`c_recomputed`, `c_reason`); the refresh stamps C from the BOUNDED UA
  (an out-of-bounds HDD fit never stamps C).
- Owner comfort policy (2026-10-09, mean across sensors): day 21 floor / 23 ceiling,
  night (22–07) 20 floor (`LP_W3_NIGHT_FLOOR_C=20`, `LP_W3_PEAK_COAST_DELTA_C=0`).

## Key `.env` settings to know

```
TUNING_REVIEW_ENABLED=true                      # #832 — weekly suggestions-only review (Sun 09:00 local; see its subsection)
OCTOPUS_TARIFF_STRUCTURE=auto                   # #804 — auto|banded|dynamic (dynamic = kill switch)
FOX_SCHEDULER_WRITE_VERSION=v2                  # Open API version for the scheduler WRITE (#777).
                                                 # On 2026-08-06 Fox broke `/op/v3/device/scheduler/enable`
                                                 # for this device: it returns `41200 Failed to load data`
                                                 # for EVERY payload — including the byte-identical group
                                                 # set the same device had accepted hours earlier — while
                                                 # `/op/v0`, `/op/v1` and `/op/v2` all accept it and the v3
                                                 # READ keeps working. Same account, SN, key and signature,
                                                 # so the route itself is the fault; a bogus v3 path returns
                                                 # HTML/404, not 41200, so the endpoint is reached. Nothing
                                                 # changed on our side (container up 13 days, 0 restarts).
                                                 # COST: each failed upload silently leaves the PREVIOUS
                                                 # schedule live, so prod ran a 2-day-old single-group
                                                 # schedule for ~37 h and lost a whole negative-price window
                                                 # (-4.2 p/kWh) with the battery at 16 %.
                                                 # v2 differs from v3 in the body: no `isDefault`, and a
                                                 # per-group `enable: 1`. Precedent: Fox broke the same
                                                 # endpoint with the same errno on 2024-09-13 and fixed it
                                                 # server-side on 2024-09-18. Set `v3` to go back once they
                                                 # fix it. Reads + the v0 flag write are untouched.
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
TARGET_DHW_TEMP_MIN_GUESTS_C=48.0              # guest-mode LP floor (multiple showers at 20:30–22:00).
                                                 # Code default = 48.0 (src/config.py) — calibrated in PR G
                                                 # (2026-05-23) from empirical tank physics: 48 °C ≈ 6 showers.
# DHW_PEAK_TANK_STRATEGY was REMOVED 2026-05-21 (Epic 14, #386). The dispatch
# layer always uses the IDLE behaviour (tank_power=True, tank_temp=DHW_TEMP_NORMAL_C)
# during peak / peak_export windows. Prod telemetry (30d, 8 completed peak
# windows) showed median tank decay 0.00 °C/h — the tank coasts essentially
# perfectly even when held warm — and SHUTDOWN attempts failed 27% of the time
# with READ_ONLY_CHARACTERISTIC errors. Leaving the env line in /srv/hem/.env
# is harmless; remove on next .env touch.
# IMPORTANT: tank pre-charge above DHW_TEMP_NORMAL_C only happens when there's
# an economic reason. The dispatch layer (src/scheduler/lp_dispatch.py, the
# `elif tank_pow:` branch) always FLOORS the setpoint at DHW_TEMP_COMFORT_C
# (48 °C) and picks the CEILING from the slot kind:
#   `solar_charge` → DHW_TEMP_PV_ABUNDANCE_TARGET_C (free PV; storage ceiling)
#   everything else (`negative`, `cheap`) → DHW_TEMP_MAX_C
# There is no separate "cheap → 48 °C" rule: 48 is the FLOOR, and a cheap slot
# rides the LP's own target up to DHW_TEMP_MAX_C. Peak avoidance does NOT
# trigger pre-charging — see issue #322 for conditional shutdown commit.
DHW_TEMP_MAX_C=60                               # hard tank ceiling (code default 60 °C in src/config.py)
DHW_TEMP_COMFORT_C=48                           # tank floor whenever the LP plans DHW heat (runtime-tunable)
DHW_TEMP_PV_ABUNDANCE_TARGET_C=60               # tank ceiling during solar_charge / solar_preheat slots. Code
                                                 # default = 60 (src/runtime_settings.py); runtime-tunable via
                                                 # PUT /api/v1/settings or MCP `set_setting`.
                                                 # NOT a comfort target — PR H (#399) reframed it as the STORAGE
                                                 # CEILING for free PV: the LP's dynamic per-slot reward decides
                                                 # how much heat actually lands (priority battery → tank → export),
                                                 # so raising it does not force the tank to 60 °C.

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
                                                 # The CODE default is 0.0 since #702 (it was -3.0 for weeks after
                                                 # prod pinned 0, so every dev/test/fresh deploy silently ran the
                                                 # double-correction). This .env line is now belt-and-braces, not
                                                 # load-bearing; `.env.example` pins it too.
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

# --- PV forecast rail + adaptive bias (#762, 2026-07-24) -----------------------
PV_CEILING_MARGIN=1.15                          # rail = PV_CAPACITY_KWP × 0.5h × margin (≈2.59 kWh/slot).
                                                 # A SAFETY RAIL, not a calibration: its ONLY correct failure
                                                 # mode is being too LOOSE. A rail that binds on real
                                                 # generation truncates the committed forecast AND censors the
                                                 # error signal the bias corrector trains on.
                                                 # NOT derated by PV_SYSTEM_EFFICIENCY (that's an expected-yield
                                                 # derate, not a limit — 1.9125 already sat below the 1.93 kWh
                                                 # best slot on record) and NOT derived from realised history
                                                 # (pv_error_log is pruned at METEO_FORECAST_HISTORY_RETENTION_DAYS
                                                 # =30, so any trailing window is really "max of last 30 days",
                                                 # which lags the spring clear-sky ramp and collapses after a
                                                 # run of overcast days).
PV_RECENT_BIAS_MIN=0.6                          # clamp TIGHTENED from [0.4, 2.5]. This corrector is a NUDGE on
PV_RECENT_BIAS_MAX=1.4                          # top of the trained calibration tables, never the dominant term.
PV_RECENT_BIAS_MIN_DAYS=3                       # an hour needs samples spanning ≥3 distinct DAYS. A half-hour
                                                 # slot yields 2 samples/hour/day, so the old bare `n>=2` gate
                                                 # let ONE day's weather set the correction — replayed against
                                                 # the real prod DB, a single 96 %-cloud day (23/07) produced
                                                 # factors slammed against BOTH clamps. One day is weather,
                                                 # not bias.
# THE 2026-07-21..23 INCIDENT. The old rail spread Fox DAILY totals over a fixed
# sinusoid — far flatter than a real PV curve — producing 1.32-1.43 kWh/slot at
# 11-13 UTC against a MEDIAN realised 1.35-1.45. It bound on 41 % of 09-16 UTC
# slots. Since pv_error_log stores the committed forecast AFTER clipping, every
# clipped slot read as "under-forecast" and pushed the bias factor UP, while the
# matching down-correction was masked by the clip — a RATCHET. Midday factors
# reached 1.72-2.50 against a measured residual of 0.87-0.90. The inflation only
# bites on CLOUDY days (when the un-inflated forecast would sit below the rail),
# so PV was over-forecast 24-27 %, the battery ended the day at 33-53 % instead
# of 93 %+, and daily net cost hit £1.50/£2.06/£2.35 vs a £0.20-1.00 baseline —
# losing to the old BG fixed tariff for the first time. Raw Quartz was FINE
# (23 Jul: predicted 12.89 kWh, realised 13.34); the correction stack destroyed
# a good signal.
# Two structural guards now: (1) pv_error_log stamps `ceiling_kwh` per slot, so
# censored rows are a RECORDED FACT — never re-derived from today's rail, which
# would silently pass the whole poisoned backlog as clean; NULL (pre-migration)
# rows are excluded as unknown-censored. (2) refresh_pv_recent_bias CLEARS the
# table when no usable samples remain, so a bad factor can't outlive its data.
# Telemetry: `censored_slots_excluded` / `unstamped_slots_excluded` in the
# refresh diag; a WARNING fires if the rail ever materially binds.

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
LP_SOC_RESERVE_RECOVERY_SLACK_PENALTY_PENCE=50.0 # #789 — when realtime SoC starts BELOW
                                                 # MIN_SOC_RESERVE_PERCENT, the forward reserve floor on
                                                 # soc[1..n] is solved SOFT at this rate (p per kWh of
                                                 # shortfall, per slot) instead of as a hard variable bound.
                                                 # #338/#339 relaxed soc[0] for the hard `soc[0] == measured`
                                                 # equality but left soc[1..n] hard — so the LP had to lift the
                                                 # battery over the reserve INSIDE the first 30-min slot, which
                                                 # the plunge-prep rule and the PV-sufficiency guard both make
                                                 # impossible at night (`chg[i] <= pv_use[i]`, pv=0). Result:
                                                 # 9 Infeasibles on 2026-08-22 20:22-23:05 UTC, held schedule
                                                 # ~3 h. Over 60 days: 0 Infeasible in 1732 solves starting
                                                 # at/above the reserve, 10 in 26 starting below it.
                                                 # A solve starting at/above the reserve is BIT-FOR-BIT
                                                 # unchanged — the relaxation only engages below it, and the
                                                 # penalty is far above any price spread so the floor still
                                                 # behaves hard whenever it is reachable. 0 = no penalty (the
                                                 # relaxation itself is unconditional: an Infeasible solve is
                                                 # never the safer outcome). Telemetry: `soc_reserve_recovery`
                                                 # in lp_inputs_snapshot.exogenous_snapshot_json, written ONLY
                                                 # when it fires, so the key's presence is the query.
LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH=0.30        # commit peak_export only when pessimistic exports ≥ this
LP_SCENARIOS_ON_TRIGGER_REASONS=plan_push,octopus_fetch,tier_boundary,soc_drift,import_overshoot,pv_upside,pv_downside,load_upside,forecast_revision,dynamic_replan,appliance_armed
                                                 # which triggers run the 3-pass solve. #668 added the
                                                 # event-driven re-solve reasons so the pessimistic charge
                                                 # floor also covers mid-day replans; `manual` stays out
                                                 # (interactive latency, not a drift context)
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
MPC_DRIFT_HYSTERESIS_TICKS=1                     # bumped down 2→1 (V12) — catches heating ramp faster
# PLAN_REVISION_MIN_SOC_DELTA_PERCENT / PLAN_REVISION_MIN_GRID_DELTA_KWH were
# REMOVED with the PLAN_REVISION ping itself — neither is a config attribute
# any more; setting them in .env is a silent no-op.
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

**Where `peak_export` can even come from.** In the `normal` and `guests`
presets the LP constrains `exp <= pv_use`, so `peak_export` (battery→grid
price arbitrage) simply doesn't emerge. It is a **`vacation`-preset**
behaviour (nobody home → max arbitrage). This is the first and strongest
gate, and it lives in the LP constraint, not in a flag.

**The one exception — `pre_negative_export`.** The `exp <= pv_use` cap is
relaxed to `exp <= pv_use + dis` on positive-price slots inside the
plunge-prep window (`LP_PRE_NEGATIVE_PREP_ENABLED`, default true;
`LP_PLUNGE_PREP_HOURS`), so the battery is deliberately drained to the grid
ahead of a negative-price window — sell high, refill at the paid negative
price. That branch is gated on `not vacation`, i.e. it fires **only in
`normal`/`guests`**, and `_slot_fox_tuple` maps those slots to the same
hardware action as `peak_export`: **ForceDischarge**. They are labelled
`pre_negative_export` precisely so they BYPASS the robustness filter below.
So "the battery never dumps to the grid outside vacation" is false — the
accurate statement is that `peak_export` (price arbitrage) never emerges
outside vacation.

When the LP *does* plan `peak_export`, three solves run under perturbed
forecasts (optimistic / nominal / pessimistic). A `peak_export` slot only
makes it onto Fox V3 when the **pessimistic** scenario also exports ≥
`LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH` (default 0.30 kWh) at that slot **and**
the economic margin (export price − future-refill shadow − battery wear)
clears `LP_PEAK_EXPORT_MIN_MARGIN_PENCE_PER_KWH`. Otherwise it's downgraded to
standard SelfUse (battery still covers load, no grid feed). Decisions are
persisted to `dispatch_decisions` with the per-scenario kWh values and the
margin components for full auditability.

**There is no `peak_export` kill switch env var.** `ENERGY_STRATEGY_MODE`
(`strict_savings` / `savings_first`) was **REMOVED** in PR C (mode-collapse
stack); `/api/v1/settings` still reports it as `"removed"` for back-compat and
setting it in `.env` is a silent no-op. The preset (`OPTIMIZATION_PRESET`) plus
the scenario filter (`filter_robust_peak_export` in
`src/scheduler/lp_dispatch.py`) are the only gates. The legacy
`EXPORT_DISCHARGE_MIN_SOC_PERCENT` live-SoC global gate is **also gone** (it
caused the 2026-04-28 incident where tomorrow's profitable peak-export
disappeared during a re-plan after today's discharge had drawn the battery
below 95 %).

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

### Tariff start clamp (PR #214, renamed #810)

```
SMART_TARIFF_START_DATE=2026-04-17    # start of HEM-managed smart-tariff history (Agile then Cosy)
# AGILE_TARIFF_START_DATE is the back-compat alias (same value); either name works.
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
  src/routes/              # Preact + TypeScript route components: landing.tsx (`/`),
                           #   insights.tsx (`/insights`), report.tsx (`/report`),
                           #   settings.tsx (`/settings`) — four routes, no per-route HTML page
  src/components|styles/   # Preact components + CSS tokens (see DESIGN.md)
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
