# ADR-001 — API Quota & Cache Layer (Daikin / Fox ESS)

**Date:** 2026-04-19  
**Status:** Implemented & deployed  
**Context:** Daikin Onecta hard limit ≈ 200 calls/day; Fox ESS Open API ≈ 1440/day.  
Prior to this change every heartbeat tick, dashboard load, assistant context call, and agent question triggered a live HTTP fetch, regularly exhausting the Daikin budget before noon.

---

## Decision

Introduce a **two-level protection system**: a persistent quota tracker (SQLite) and a per-service in-memory cache with stale-data fallback.

### 1 — Persistent quota tracker (`src/api_quota.py`)

- `api_call_log` table in `energy_state.db` — one row per outbound HTTP call, columns: `vendor`, `kind`, `ts_utc`, `ok`.
- `record_call(vendor, kind)` — called by every service method that makes an HTTP request.
- `count_calls_24h(vendor)` — rolling 24-hour window count (no midnight reset cliff).
- `should_block(vendor)` — returns `True` when `count >= daily_budget`; callers must check before dialling out.
- Survives process restarts; prevents "fresh process, budget forgotten" exhaustion.

### 2 — Daikin service singleton (`src/daikin/service.py`)

```
DaikinService (module-level singleton)
├── get_cached_devices(allow_refresh, actor) → CachedDevices
│     ├── cache hit (age < TTL) → return cached, source="cache"
│     ├── quota blocked         → return stale cached, stale=True
│     └── cache miss + quota ok → fetch, record_call, refresh cache
├── force_refresh_devices(actor) → CachedDevices
│     └── throttled: min 30 min between calls per actor
├── set_power / set_temperature / set_lwt_offset / …
│     └── quota check → HTTP write → record_call → invalidate_cache
└── get_quota_status_daikin() → dict (for /api/v1/daikin/quota)
```

**Key constants (all tunable via `.env`):**

| Key | Default | Meaning |
|---|---|---|
| `DAIKIN_DAILY_BUDGET` | 180 | Stop at 90% of real 200 limit |
| `DAIKIN_DEVICES_CACHE_TTL_SECONDS` | 1800 | 30-min cache; matches one Octopus slot |
| `DAIKIN_FORCE_REFRESH_MIN_INTERVAL_SECONDS` | 1800 | Throttle UI/agent "refresh" clicks |
| `DAIKIN_SLOT_TRANSITION_WINDOW_SECONDS` | 300 | Width of pre-slot auto-refresh window |

**Effective Daikin call rate after this change:**  
2 auto-refreshes/hour × 24h = **48 calls/day** maximum (well under 180 budget). Manual/agent force-refreshes add at most 1 per 30 min = 48 more → still safely under 180.

### 3 — Fox ESS quota extension (`src/foxess/service.py`)

- All Fox HTTP calls (realtime, device list, mode writes, charge-period writes) routed through `record_call("foxess", kind)`.
- Realtime cache TTL raised **30 s → 300 s** (matches heartbeat interval; no benefit polling faster).
- `force_refresh_realtime(actor)` — per-actor throttle (60 s default).
- Stale fallback: if `should_block("foxess")` → return last cached value with `stale=True` rather than raising an exception.
- `get_refresh_stats_extended()` — exposes `quota_used_24h`, `quota_remaining_24h`, `daily_budget`, `blocked`, `cache_age_seconds`, `stale` for the status endpoint.

### 4 — Octopus pre-slot window gate (`src/scheduler/runner.py`)

```python
def _in_octopus_pre_slot_window(now, lead_seconds=300) -> bool:
    """True in [HH:25, HH:30) and [HH:55, HH:00) — 5 min before each Agile boundary."""
```

The heartbeat calls `get_cached_devices(allow_refresh=_in_octopus_pre_slot_window())`:  
- **Outside window**: reads from cache only (zero HTTP calls).  
- **Inside window**: one refresh allowed if cache is stale and quota permits, giving the LP replanner fresh Daikin state before the rate changes.

### 5 — Stale-data contract

Any caller receiving `CachedDevices(stale=True)` or a Fox status with `cache_stale=True` **must not raise an error** — it should use the value and log a warning. This is intentional: we prefer slightly stale data over a hard failure when the quota is exhausted.

### 6 — Visibility endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/v1/daikin/quota` | quota_used, remaining, budget, blocked, cache_age |
| `GET /api/v1/foxess/quota` | same + last_blocked_at, refresh_count_24h |
| `GET /api/v1/daikin/status?refresh=true` | forces a live fetch (subject to throttle + quota) |
| `GET /api/v1/foxess/status` | now includes all quota/cache fields |

---

## Consequences

### Good
- Daikin API calls reduced from ~720/day (30-s heartbeat) to ≤ 48/day automatic + bounded manual.
- Fox ESS calls reduced from ~2880/day to ~288/day (300 s TTL × 24 h).
- System degrades gracefully on quota exhaustion (stale data, not crash).
- Call counts survive restarts via SQLite — no "fresh process, full budget" false sense of safety.
- Quota state is observable via dedicated API endpoints and the dashboard.

### Trade-offs / watch points
- **Cold cache at startup**: first heartbeat after a restart will have no cached data; the next pre-slot window will populate it. The gap is at most 30 min.
- **Stale Daikin state during LP replan**: if the pre-slot refresh fails (quota exhausted or Daikin API down), the LP runs on the previous cache. This is acceptable — temperatures change slowly.
- **`DaikinClient` still instantiated for write reconciliation**: `reconcile_daikin_schedule_for_date` receives a lightweight `DaikinClient` object but does not call `get_devices()` internally. Keep it this way — do not add device discovery inside reconcile.
- **`force_refresh` throttle is per-actor, not global**: two different actors (e.g. "api" and "heartbeat") can each trigger a refresh within the same 30-min window. This is intentional (UI refresh should not be blocked by a heartbeat refresh and vice versa) but means the theoretical maximum is 2× the per-actor rate. Still safely under budget.

---

## Future improvements

### Short-term
- [ ] **Dashboard quota widget**: add a small `#quota-status` card to `dashboard.html` showing Daikin and Fox used/remaining bars — currently only available via JSON endpoints.
- [ ] **Health check integration**: include `quota_blocked` in `/api/v1/health` response so monitoring alerts fire before the inverter is stranded.
- [ ] **Deploy script health check timing**: the script's 30 s poll loop fires before uvicorn is ready (startup takes ~5 s for PuLP). Add a `sleep 6` before the first health-check curl, or switch to a retry-with-backoff loop.

### Medium-term
- [ ] **Per-slot Daikin telemetry snapshot**: log outdoor temp, LWT, tank temp, and room temp to `execution_log` at each reconcile tick so the LP can use actual Daikin state as initial conditions rather than API-fetched values (reduces cold-start dependency on live Daikin reads).
- [ ] **Fox ESS work-mode echo in DB**: after every Fox V3 upload, read back the confirmed work mode and store it in SQLite. Currently `work_mode` in the status response shows "unknown" after a restart until the next realtime poll.
- [ ] **Smarter MPC trigger**: today MPC re-runs at fixed `LP_MPC_HOURS`. Consider re-triggering when Fox SoC deviates > 10% from plan, or when a cloud event causes PV to drop > 50% vs forecast (already available in `src/weather.py`).

### Longer-term
- [ ] **Local Daikin polling (LAN)**: Daikin Altherma units expose a local Modbus/BACnet or P1 interface on some firmware versions. If reachable on the LAN, replace the cloud `get_devices()` call with a local read — zero quota cost, sub-second latency.
- [ ] **Fox ESS local RS485**: the inverter exposes a Modbus-TCP interface on the local network. Real-time SoC and power readings without cloud quota would allow a tighter MPC loop (e.g. 60 s vs current 300 s).
- [ ] **Octopus Intelligent / Flux tariff support**: the rate classifier and LP model assume flat half-hourly Agile. Adding Intelligent Go (overnight 7.5p window) or Flux (export premium) would require a tariff abstraction layer in `src/scheduler/agile.py`.
- [ ] **Battery degradation model**: the LP cycle penalty (`LP_CYCLE_PENALTY_PENCE_PER_KWH`) is a flat pence value. Replace with a capacity-fade curve (DoD vs cycle count) so the optimizer naturally avoids deep cycling as the battery ages.

---

## Related files

| File | Role |
|---|---|
| `src/api_quota.py` | Quota tracker (new) |
| `src/daikin/service.py` | Daikin cache/singleton (new) |
| `src/foxess/service.py` | Fox ESS cache + quota extension |
| `src/scheduler/runner.py` | Pre-slot window gate, heartbeat cache-only reads |
| `src/config.py` | All quota/cache tuning constants |
| `src/db.py` | `api_call_log` table schema + migration |
| `src/api/main.py` | All Daikin reads → service; new quota endpoints |
| `src/api/models.py` | `FoxESSStatusResponse` quota fields |
| `scripts/deploy_hetzner.sh` | Post-deploy Fox ESS Self Use safety reset |
| `tests/test_api_quota.py` | Quota tracker unit tests |
| `tests/test_daikin_service.py` | Service cache / throttle / stale tests |
| `tests/test_foxess_service_quota.py` | Fox ESS quota + stale tests |
