# ADR-006 — Runtime-tunable settings (no-restart tuning)

**Date:** 2026-04-22 (PR #63, closes #52).
**Status:** Implemented.
**Complements:** [ADR-001](ADR-001-api-quota-cache-layer.md),
[ADR-003](ADR-003-mcp-boundary-enforcement.md).

## Context

Every tunable knob was an env var baked in at process start: `DHW_TEMP_COMFORT_C`,
`OPTIMIZATION_PRESET`, `LP_MPC_HOURS`, etc. Tuning DHW ceilings or MPC cadence
meant editing `/root/home-energy-manager/.env` and `systemctl restart` —
friction that kept operators from iterating, and made OpenClaw agents unable to
retune comfort on the user's behalf.

## Decision

**Split `EnvConfig` vs `RuntimeSettings`.** Env vars remain the source of truth
for credentials, hardware nameplates, infrastructure, and safety gates (anything
where a restart is a feature, not a bug). A focused list of comfort / strategy /
schedule knobs moves to a DB-backed `runtime_settings` table with an in-memory
cache; `config.*` stays the expression every call-site already uses.

### Storage (V10 migration)

`runtime_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)` —
string-only storage; the service layer (`src/runtime_settings.py`) handles
coercion per the schema.

### Schema (`src/runtime_settings.py::SCHEMA`)

| Group | Keys | `cron_reload` |
|---|---|---|
| Comfort | `DHW_TEMP_COMFORT_C`, `DHW_TEMP_NORMAL_C`, `INDOOR_SETPOINT_C` | — |
| Strategy | `OPTIMIZATION_PRESET`, `ENERGY_STRATEGY_MODE` | — |
| Schedule | `LP_PLAN_PUSH_HOUR`, `LP_PLAN_PUSH_MINUTE`, `LP_MPC_HOURS` | ✅ |

Each spec carries type, range (`min_value`/`max_value`) or enum, an `env_default`
lambda, and a `cron_reload` flag. Unknown keys are rejected — schema-driven, not
opt-in extension.

### Cache

TTL (30 s) + monotonic **version counter**. Reads short-circuit on version
match; `set_setting` bumps the version so two PUTs within the TTL window are
both visible without sleeping. TTL catches out-of-band DB writes (someone typing
`UPDATE runtime_settings ...` by hand).

### `config.*` integration

Each runtime-tunable knob is a `@property` on `Config` that reads through
`runtime_settings.get_setting`. The singleton keeps a class-level `_overrides`
dict that `@setter`s write to, so:

- `setattr(config, "DHW_TEMP_COMFORT_C", 52)` — used by `simulate_plan` and every
  pytest `monkeypatch.setattr` in the repo — round-trips **in-memory**, no DB
  pollution.
- `runtime_settings.set_setting(...)` or `PUT /api/v1/settings/...` — persists
  to the DB and invalidates the cache.

This deliberately decouples "test / dry-run mutate" from "operator update":
neither path pollutes the other, and legacy call-sites (e.g. simulate_plan's
save-and-restore pattern) keep working with zero code changes.

### Surface

- REST: `GET /api/v1/settings`, `GET|PUT|DELETE /api/v1/settings/{key}`.
- MCP: `list_settings`, `get_setting`, `set_setting(confirmed=False)` (dry-run by
  default so agents can show a diff before applying).

### Cron hot-reload (user-chosen option (a))

`scheduler.runner.reregister_cron_jobs(reason)` rebuilds `bulletproof_plan_push`
and `bulletproof_mpc_*` from freshly-read config while leaving the heartbeat
thread and unrelated jobs untouched. The PUT handler invokes this when the
updated key has `cron_reload=True`. Previously the author's fallback-plan was
"restart required for cadence knobs" (option (b)); we rejected that as UX-hostile.

## Consequences

### Good
- `curl -X PUT .../settings/DHW_TEMP_COMFORT_C -d '{"value": 52}'` takes effect
  within 30 s in the next LP solve — no restart, no downtime.
- Cron cadence changes (`LP_MPC_HOURS=[4,10,15]`) re-register live — the new
  trigger fires on its next scheduled time, the old one stops firing
  immediately.
- **Zero-risk rollback**: delete the row
  (`DELETE /api/v1/settings/{key}`) and the env default reasserts on next read.
  Migration from env-only → DB-backed is implicit (DB empty = env semantics).

### Watch points
- **Validation is source-of-truth in Python, not DB.** A manual `UPDATE
  runtime_settings ...` bypasses the schema; the next coerce-on-read fails and
  falls back to env default with a log line. This is intentional — we don't want
  SQL-level check constraints drifting from the code's validators.
- **The `_overrides` dict is class-level, not instance-level.** There's a single
  `config` singleton, but if anyone ever constructs a second `Config()`, the
  dicts are still shared — by design, but worth noting if the singleton
  assumption ever changes.
- **Adding new knobs** is a 3-line change: one schema entry + one `@property` +
  one `@setter`. Tests (`tests/test_runtime_settings.py`) establish the pattern.
- **Cron reload is per-key.** If we add another cadence knob we must also tag
  it `cron_reload=True` **and** ensure `reregister_cron_jobs` teardown matches
  any new job IDs.

## Related files

- `src/runtime_settings.py` — schema, cache, validation.
- `src/config.py` — `@property`+`@setter` pairs and the `_overrides` dict.
- `src/db.py` — V10 `runtime_settings` migration + `get_/set_/delete_/list_runtime_setting` helpers.
- `src/api/main.py` — `/api/v1/settings` endpoints.
- `src/mcp_server.py` — `list_settings`, `get_setting`, `set_setting`.
- `src/scheduler/runner.py::reregister_cron_jobs`.
- `tests/test_runtime_settings.py`.
