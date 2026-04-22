# ADR-002 — Daikin quota-integrity hardening (Phase 4)

**Date:** 2026-04-21
**Status:** Implemented (#45 merged 2026-04-21).
**Supersedes part of:** [ADR-001](ADR-001-api-quota-cache-layer.md) — specifically the
premise that _service-layer_ `record_call` is sufficient.

## Context

ADR-001 routed `record_call("daikin", kind)` through `src/daikin/service.py` wrappers.
Auditing the codebase post-landing surfaced two bypasses that dialled `DaikinClient`
directly, producing "silent" HTTP traffic invisible to both the 24 h budget tracker
and the TTL device cache:

- `src/scheduler/daikin.py:78` — legacy half-hourly LWT tick.
- `src/scheduler/lp_initial_state.py:47` — LP seed on every MPC replan.

Combined drift was up to ~100 unaccounted calls/day against a 180 budget. Separately,
whenever the user changed tank temperature or LWT via the Daikin Onecta mobile app,
the next heartbeat tick detected the mismatch and PATCHed the value **back** — a
hostile UX that the system had to stop doing before #30 (V8.2 paradigm shift) could
safely concede control to Daikin native regulation.

## Decision

1. **Move `record_call` to the transport layer.** Every `DaikinClient._get` and
   `DaikinClient._patch` records exactly once, on success **and** on failure (429s still
   count per Onecta's sliding-window policy). Service-layer `record_call` sites are
   removed; `should_block` checks stay at the service layer because they gate
   _whether_ to dial out.

2. **Route both bypasses through the cached service.** `legacy_lwt_tick` →
   `get_cached_devices(allow_refresh=True, max_age_seconds=DAIKIN_LEGACY_TICK_CACHE_MAX_AGE_SECONDS, ...)`;
   `lp_init` → same pattern with `DAIKIN_LP_INIT_CACHE_MAX_AGE_SECONDS`. New env knobs
   with sensible defaults (1200 s and 600 s) so these paths reuse the cache aggressively.

3. **User-override acceptance loop.** Add column `overridden_by_user_at` to
   `action_schedule` and env knob `DAIKIN_OVERRIDE_GRACE_SECONDS` (default 600 s,
   clamped ≥ 60 s). When a row has been `active` for ≥ grace, compare live Daikin
   state to the row's `params`; on divergence the row is marked overridden and the
   heartbeat stops re-PATCHing. The grace window absorbs Onecta cloud echo lag —
   without it, every freshly-active row false-flags on the first tick.

4. **Parse `tank_power` / `tank_powerful` into `DaikinDevice`** so
   `daikin_device_matches_params()` can compare rather than unconditionally write.
   Eliminates 2–12 redundant PATCHes/day on DHW dispatch windows.

## Consequences

### Good
- `grep -rn "DaikinClient()" src/` reaches exactly one production site
  (`src/daikin/service.py`).
- Observed 24 h Daikin call count drops below 80 with 180 budget — 55 % headroom.
- Mobile-app overrides persist through at least one full MPC cycle.
- No DHW short-cycling from the `tank_power` conservative-write fallback.

### Watch points
- The grace window is a heuristic — too short false-flags real plan executions; too
  long lets a planned action appear "user overridden" for minutes before correcting.
  600 s is the empirically-validated default and env-tunable per deployment.
- The `overridden_by_user_at` marker is advisory, not hard state. The next optimizer
  run recomputes a fresh plan that supersedes overridden rows naturally.

## Related files

- `src/daikin/client.py` — `_get`, `_patch`, `_parse_device` (tank_on / tank_powerful).
- `src/daikin/service.py` — removed service-level `record_call` sites.
- `src/daikin/models.py` — `tank_on`, `tank_powerful` on `DaikinDevice`.
- `src/daikin_bulletproof.py` — `daikin_device_matches_params` no longer blanket-bypasses DHW.
- `src/scheduler/daikin.py`, `src/scheduler/lp_initial_state.py` — now go through `get_cached_devices`.
- `src/db.py` — `action_schedule.overridden_by_user_at` column.
- `src/config.py` — `DAIKIN_LP_INIT_CACHE_MAX_AGE_SECONDS`, `DAIKIN_LEGACY_TICK_CACHE_MAX_AGE_SECONDS`, `DAIKIN_OVERRIDE_GRACE_SECONDS`.
- `tests/test_daikin_client_quota.py`, `tests/test_user_override.py`.
