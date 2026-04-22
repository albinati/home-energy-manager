# ADR-004 — Daikin physics state estimator (429 fallback)

**Date:** 2026-04-22 (PR #62, closes #55).
**Status:** Implemented.
**Extends:** [ADR-001 — API Quota & Cache Layer](ADR-001-api-quota-cache-layer.md).

## Context

Even with ADR-001's quota tracker and the ADR-002 transport-layer accounting, the
Daikin Onecta 200 req/day sliding window can exhaust legitimately — notably on
migration days (2026-04-18 live observation) and on any day with several forced
`force_refresh_devices` clicks from a dashboard. Symptoms at the 200-cap:

- The cold-start `get_devices()` failed with HTTP 429, logged a WARN, and retried
  every 2 minutes via the heartbeat → log-spam loop, no useful signal.
- The LP's `lp_initial_state.read_lp_initial_state()` fell back to config defaults
  (tank 50 °C, indoor 21 °C). Plans were plausible but drifted over hours.

A local LAN poll is the long-term fix (tracked in ADR-001 follow-ups) but depends
on Altherma firmware we can't guarantee. We wanted a same-day improvement.

## Decision

**Add a closed-form passive-decay state estimator, fed by a telemetry audit trail.**

### Data

`daikin_telemetry` (V9 migration) — per-fetch snapshot of tank / indoor / outdoor
/ target / LWT / mode, tagged `source='live'` on every successful refresh and
`source='estimate'` on each fallback walk. Indexed on `(source, fetched_at DESC)`
for O(log N) seed lookup.

### Logic

`src/daikin/estimator.py::estimate_state(last_live, now_utc, meteo_rows, ...)`
walks:

- Tank: `T(t) = T_indoor + (T0 − T_indoor) · exp(−UA_tank · t / C_tank)` —
  passive loss toward the room it lives in.
- Indoor: `T(t) = T_out + (T0 − T_out) · exp(−UA_bld · t / C_bld)` where
  `T_out` is the mean outdoor over the horizon from `meteo_forecast`.

Continuous-time exponential decay is **exact** for a constant ambient; no Euler
stepping, no dt sensitivity over short horizons. The constants
(`DHW_TANK_UA_W_PER_K`, `BUILDING_UA_W_PER_K`, `BUILDING_THERMAL_MASS_KWH_PER_K`,
`DHW_TANK_LITRES`, `DHW_WATER_CP`) are already calibrated for the LP itself, so
the estimator and LP share a single source of truth.

### Wrapper (`daikin_service.get_lp_state_cached_or_estimated`)

Three-stage fallback:

1. Fresh `source='live'` row within `DAIKIN_TELEMETRY_MAX_STALENESS_SECONDS`
   (default 1800) → return as `live`.
2. Quota has headroom → live fetch via `get_cached_devices`, persist row, return.
3. Else → estimator walk, persist as `estimate`, return with `stale=True`-ish
   metadata.

When nothing at all is available (first boot + quota exhausted), returns
`source='degraded'` with `None` temps so the LP falls back to config defaults
instead of crashing.

### Cold-start log collapse

`_cold_start_quota_logged` flag suppresses the 2-minute WARN loop — first 429 at
boot emits a single informational line (`"Daikin cold-start: quota exhausted,
using physics estimator — tank≈X°C, indoor≈Y°C"`), subsequent 429s log at DEBUG.
Flag resets on the next successful refresh so a later outage is still surfaced.

## Consequences

### Good
- LP can always seed, even with zero Daikin quota. Plan produced on 2026-04-18's
  migration-day quota exhaustion matched the best achievable given the
  information available.
- Closed-form math — the test suite asserts <0.5 °C error at 3 h vs the
  analytical expectation, which is the issue's acceptance criterion.
- One WARN per boot, not 720/day.

### Watch points
- **Passive-decay only.** If the planner has committed active heating during the
  estimation window, the current MVP ignores that contribution — LP will slightly
  over-heat rather than under-heat (the safe direction), but there's daylight for
  a follow-up that walks `action_schedule.e_dhw` / `e_space` into the physics.
- **Outdoor defaults to seed's recorded outdoor** when `meteo_forecast` is empty;
  if even that's missing, indoor holds (conservative). Watch the `source` field
  on `/api/v1/daikin/status` output — prolonged `estimate` without a `live`
  refresh points to a quota or auth failure worth investigating.
- **Storage growth**: ~48 live rows/day plus any fallbacks; negligible at current
  volumes but merits a periodic `DELETE WHERE fetched_at < NOW() - 30 d` if the
  table ever hits millions.

## Related files

- `src/daikin/estimator.py`, `src/daikin/service.py::get_lp_state_cached_or_estimated`.
- `src/scheduler/lp_initial_state.py` — seeds tank/indoor via the wrapper.
- `src/db.py` — V9 `daikin_telemetry` migration + `insert_daikin_telemetry`,
  `get_latest_daikin_telemetry`.
- `src/config.py` — `DAIKIN_TELEMETRY_MAX_STALENESS_SECONDS`.
- `tests/test_daikin_estimator.py`, `tests/test_daikin_quota_fallback.py`.
