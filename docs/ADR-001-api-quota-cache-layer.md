# ADR-001 — API Quota & Cache Layer (Daikin / Fox ESS)

**Date:** 2026-04-19
**Status:** Implemented & deployed. Subsequent decisions build on this foundation — see [ADR-002 … ADR-006](#supersedes--extended-by).
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
- **Cold cache at startup**: first heartbeat after a restart will have no cached data; the next pre-slot window will populate it. The gap is at most 30 min. (Mitigated for the LP by ADR-004.)
- **Stale Daikin state during LP replan**: if the pre-slot refresh fails (quota exhausted or Daikin API down), the LP runs on the previous cache. This is acceptable — temperatures change slowly. (Further hardened by the physics estimator in ADR-004.)
- **`DaikinClient` still instantiated for write reconciliation**: `reconcile_daikin_schedule_for_date` receives a lightweight `DaikinClient` object but does not call `get_devices()` internally. Keep it this way — do not add device discovery inside reconcile.
- **`force_refresh` throttle is per-actor, not global**: two different actors (e.g. "api" and "heartbeat") can each trigger a refresh within the same 30-min window. This is intentional (UI refresh should not be blocked by a heartbeat refresh and vice versa) but means the theoretical maximum is 2× the per-actor rate. Still safely under budget.

---

## Supersedes / extended by

- **[ADR-002 — Phase 4 Daikin hardening](ADR-002-daikin-quota-integrity-hardening.md)** — moves `record_call` into `DaikinClient._get/_patch` so every HTTP call is counted uniformly (closes two bypasses), and adds user-override acceptance so mobile-app edits aren't fought by the heartbeat.
- **[ADR-003 — MCP boundary enforcement](ADR-003-mcp-boundary-enforcement.md)** — boot-time tool-surface audit, `simulate_plan` dry-run, singleton lock so OpenClaw respawns don't accumulate processes.
- **[ADR-004 — Daikin physics state estimator](ADR-004-daikin-physics-estimator.md)** — closed-form decay from the last live telemetry row so the LP keeps planning when the Onecta quota is exhausted.
- **[ADR-005 — Fox V3 scheduler idempotency](ADR-005-fox-scheduler-idempotency.md)** — skip the upload when the fingerprinted groups list equals what's already on the inverter.
- **[ADR-006 — Runtime-tunable settings](ADR-006-runtime-tunable-settings.md)** — comfort / strategy / MPC-cadence knobs live-editable via `/api/v1/settings`, no systemd restart.

---

## Open follow-ups

Items below were in scope at the original ADR date and remain open. Items that
landed are listed in the status block at the top.

- **Dashboard quota widget** — a small `#quota-status` card in `dashboard.html` showing Daikin and Fox used/remaining bars (today exposed only as JSON at `/api/v1/{daikin,foxess}/quota`).
- **Health-check integration** — include `quota_blocked` in `/api/v1/health` so external monitors can alert before the inverter is stranded.
- **Fox ESS work-mode echo in DB** — after every Fox V3 upload, read-back and persist the confirmed work mode. Currently `work_mode` in status shows `unknown` after a restart until the next realtime poll. Partially unblocked by PR #61's `warn_if_scheduler_v3_mismatch`.
- **Smarter MPC trigger** — re-run when Fox SoC deviates > 10 % from plan, or when PV drops > 50 % vs forecast. `LP_MPC_HOURS` fixed-cron is today's only trigger (now hot-reloadable via #52).
- **Local Daikin polling (LAN)** — some Altherma firmware exposes a local Modbus / BACnet interface. Zero quota, sub-second latency. Would complement (not replace) the estimator; unreliable across firmware revs.
- **Fox ESS local RS485 / Modbus-TCP** — inverter exposes this on the LAN. Tight SoC / power reads without cloud quota → could tighten the MPC loop from 300 s to 60 s.
- **Octopus Intelligent / Flux tariff support** — classifier + LP assume flat half-hourly Agile. Adding Intelligent Go (overnight 7.5 p window) or Flux (export premium) needs a tariff abstraction in `src/scheduler/agile.py`.
- **Battery degradation model** — `LP_CYCLE_PENALTY_PENCE_PER_KWH` is a flat pence value. Replace with a capacity-fade curve (DoD vs cycle count) so the optimizer naturally avoids deep cycling as the battery ages.

---

## Related files

| File | Role |
|---|---|
| `src/api_quota.py` | Per-vendor 24-h sliding quota tracker |
| `src/daikin/service.py` | Daikin cache singleton + `get_lp_state_cached_or_estimated` (#55) |
| `src/daikin/estimator.py` | Closed-form physics estimator for quota-exhausted fallback (#55) |
| `src/daikin/client.py` | Uniform `record_call` at transport layer (Phase 4.1) |
| `src/foxess/service.py` | Fox ESS cache + quota extension |
| `src/foxess/client.py` | `set_scheduler_v3` skip-when-unchanged guard (#38 / PR #61) |
| `src/scheduler/runner.py` | Pre-slot window gate, heartbeat cache-only reads |
| `src/config.py` | All quota / cache tuning constants |
| `src/db.py` | `api_call_log` (V5), `daikin_telemetry` (V9) tables |
| `src/api/main.py` | `/api/v1/{daikin,foxess}/quota` endpoints |
| `scripts/deploy_hetzner.sh` | Post-deploy Fox ESS Self Use safety reset |
| `tests/test_api_quota.py` | Quota tracker unit tests |
| `tests/test_daikin_service.py` | Cache / throttle / stale tests |
| `tests/test_daikin_estimator.py` | Estimator closed-form accuracy (#55) |
| `tests/test_daikin_quota_fallback.py` | Quota-exhausted → estimator path (#55) |
| `tests/test_foxess_service_quota.py` | Fox ESS quota + stale tests |
| `tests/test_foxess_scheduler_readback.py` | Fox idempotency / fingerprint tests (#38) |
