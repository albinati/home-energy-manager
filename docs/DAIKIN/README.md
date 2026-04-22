# Daikin Onecta integration notes

Project-specific integration reference for the Daikin Onecta Cloud API.
For the canonical spec see the **[Onecta developer portal](https://developer.cloud.daikineurope.com/)**
and the `onecta-openapi.json` (OpenAPI 3, v1.0.0) alongside this file.

---

## What we use from Onecta

| Concept | Onecta name | Our code |
|---|---|---|
| Auth | OIDC authorization-code flow | `src/daikin/auth.py` |
| Device inventory | `GET /v1/gateway-devices` | `DaikinClient.get_devices` |
| Telemetry read | management-point characteristics | `DaikinClient._parse_device` |
| Temperature / mode writes | `PATCH .../characteristics/{name}` | `DaikinClient.set_temperature`, `set_power`, `set_lwt_offset`, `set_operation_mode`, `set_tank_temperature`, `set_tank_power`, `set_tank_powerful`, `set_weather_regulation` |

**Management points we touch** (Daikin Altherma air-to-water):

- `climateControl` — `onOffMode`, `temperatureControl.*.roomTemperature`, `temperatureControl.*.leavingWaterOffset`, `temperatureControl.*.leavingWaterTemperature`.
- `domesticHotWaterTank` — `onOffMode`, `powerfulMode`, `temperatureControl.*.domesticHotWaterTemperature`.
- `gateway` (read-only) — `firmwareVersion`, `ipAddress`, `macAddress`.

Anything outside that list (holiday mode, schedules, block schedules, air-purifier / air-to-air devices) is **out of scope** — the LP owns scheduling, we don't push Onecta-side schedules.

---

## Rate limits

Onecta enforces a sliding-window quota on every application:

- **200 requests / 24 h** (hard; we cap at `DAIKIN_DAILY_BUDGET=180`).
- **20 requests / minute** (hard).

Sliding windows mean there's **no midnight cliff** — an old call ages out 24 h after it was made. HTTP 429s **also count against the quota** (per Daikin: _"API calls with an HTTP 429 Too Many Requests response also contribute to your quota"_), so naive retry loops make things worse.

### Response headers

Every response includes:

```
X-RateLimit-Limit-minute: 20
X-RateLimit-Remaining-minute: 18
X-RateLimit-Limit-day:    200
X-RateLimit-Remaining-day: 173
RateLimit-Reset:          32
```

On 429 a `Retry-After: <seconds>` header is set — it grows on repeated violations.

### How we stay under the quota

1. **Persistent 24 h counter** (`src/api_quota.py` → `api_call_log` table). Survives restarts.
2. **`should_block("daikin")`** — every outbound call checks before dialling. Cached-only reads when blocked.
3. **Device cache** (`src/daikin/service.py`, TTL `DAIKIN_DEVICES_CACHE_TTL_SECONDS=1800`) — the heartbeat reads from cache; only the 5-minute pre-slot window (HH:25–30, HH:55–00) may refresh.
4. **`DAIKIN_HTTP_429_MAX_RETRIES=0`** on the prod `.env` — fail fast instead of sleeping for `Retry-After` seconds, which Daikin sets to ~86400 s on daily-limit exhaustion.
5. **Physics-based estimator fallback** (`src/daikin/estimator.py`, added by #55) — when quota is exhausted, `daikin_service.get_lp_state_cached_or_estimated()` walks tank + indoor temps forward from the last live row using passive thermal decay. LP keeps planning.

See **[../ADR-001-api-quota-cache-layer.md](../ADR-001-api-quota-cache-layer.md)** for the architectural decision and trade-offs.

---

## OAuth (OIDC) flow

Canonical docs: https://developer.cloud.daikineurope.com/ → Authorization.

**Scopes we request:** `openid onecta:basic.integration`.

**Refresh tokens rotate** on every exchange and are valid for ~1 year (our `.env` keeps them in `data/.daikin-tokens.json`, chmod 600). Access tokens expire every **3600 s** — `src/daikin/auth.refresh_tokens` proactively refreshes `DAIKIN_ACCESS_REFRESH_LEEWAY_SECONDS` (default 600 s) before expiry.

Full re-auth procedure (refresh token expired or revoked) lives in the root **[CLAUDE.md](../../CLAUDE.md#daikin-onecta--token-management)** — the interactive flow needs an SSH port-forward on the Hetzner VPS because the redirect URI binds to `localhost:18080`.

---

## Characteristic update pattern

### Simple (one-value characteristic)

```
PATCH /gateway-devices/{gatewayDeviceId}/management-points/{embeddedId}/characteristics/onOffMode
{"value": "on"}
```

### Object characteristic (path-qualified)

```
PATCH .../characteristics/temperatureControl
{
  "path":  "/operationModes/heating/setpoints/leavingWaterOffset",
  "value": 2
}
```

We use the object-characteristic form for every temperature/setpoint write — the generic wrapper is `DaikinClient._patch_characteristic`.

### Idempotency + echo lag

Daikin cloud lags the physical unit by tens of seconds. `DAIKIN_OVERRIDE_GRACE_SECONDS=600` (10 min) tells the heartbeat's user-override detector not to treat a freshly-pushed value as "user changed it back" before the cloud echo catches up (Phase 4.3).

---

## Weather-dependent mode (`weatherDependent`)

When the unit runs `weatherDependent` **setpointMode**, the **`roomTemperature`** setpoint is ignored — the unit derives LWT from the installer-configured weather curve. To adjust heating intensity we write **`leavingWaterOffset`** instead. `DaikinClient.set_temperature` dispatches automatically based on `device.weather_regulation_enabled`.

The weather-curve parameters (configured on the Daikin wall display, not exposed via API) are mirrored in `src/config.py`:

```
DAIKIN_WEATHER_CURVE_LOW_C       # outdoor temp at the curve's cold end
DAIKIN_WEATHER_CURVE_LOW_LWT_C
DAIKIN_WEATHER_CURVE_HIGH_C      # outdoor temp at the warm end
DAIKIN_WEATHER_CURVE_HIGH_LWT_C
DAIKIN_WEATHER_CURVE_OFFSET_C    # installer's offset
```

`src/physics.get_lwt_base_c()` and `get_daikin_heating_kw()` derive kW draw from these constants. Keep them in sync with the installer menu.

---

## Compressor-protection rule

From the Daikin guidelines: **do not start/stop the compressor more than once every 10 minutes.** Startups use ~3× steady-state power, and thrash shortens compressor life.

Our LP respects this via `LP_HP_MIN_ON_SLOTS` (default 2 = 1 hour minimum run); the heartbeat never issues `set_power(True)` followed by `set_power(False)` within the same 10-minute window (guarded by the slot-classifier + dispatch merging in `src/scheduler/lp_dispatch._merge_fox_groups` + Daikin-side equivalents).

---

## Legionella thermal-shock cycle

Onecta firmware runs the weekly thermal-shock autonomously (Sunday ~11:00 local). **The LP / dispatch layer does not schedule or override this cycle.** The legacy `DHW_LEGIONELLA_*` env vars are deprecated and slated for removal (see CLAUDE.md). If a `shutdown` or `max_heat` action overlaps the cycle, Onecta arbitrates.

---

## Supported gateways

We run against **`BRP069A6x` / `BRP069A7x`** (Altherma air-to-water). Other gateways in the Onecta catalogue (air-to-air, air purifier, gas boilers) are feature-detected by `DaikinClient.get_devices` but not driven by the planner — they'd need their own management-point handling.

Unsupported gateways listed by Daikin:
- `EKRACPUR1PA` (Daikin Home Controls) — ❌ no Onecta 3rd-party API.
- `EKRHH` (Daikin HomeHub) — ❌ local interface only.

---

## Troubleshooting reference

| Symptom | Likely cause | Where to look |
|---|---|---|
| `HTTP 401 Unauthorized` on every call | refresh token expired (>1 year) or user revoked consent | `CLAUDE.md` full re-auth procedure |
| `HTTP 429 Too Many Requests` bursts | sliding-window quota exhausted | `GET /api/v1/daikin/quota`, `api_call_log` |
| `tank_power` / `tank_powerful` re-writes each tick | Daikin's DHW management point didn't parse `onOffMode` / `powerfulMode` into `DaikinDevice` — fallback to conservative write (#4.2) | `src/daikin/client._parse_device` |
| User app change reverted after 2 min | override detection window not applied | `DAIKIN_OVERRIDE_GRACE_SECONDS`, `reconcile_daikin_schedule_for_date` |
| Cold-start logs "HTTP 429" in a loop | pre-#55 behaviour | should be one-shot now; see `src/daikin/service._cold_start_quota_logged` |
