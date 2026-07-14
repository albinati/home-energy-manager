# ADR-003 — MCP boundary enforcement & singleton

**Date:** 2026-04-22 (Phase 4.5 landed 2026-04-21 as part of #45; singleton added by PR #61).
**Status:** **Partially superseded (2026-04-25).** Controls 1 and 2 are still in
force. **Control 3 (the singleton `flock`) is GONE** — the production MCP moved
from a per-call stdio subprocess to a long-lived HTTP transport mounted at
`/mcp`, so the container *is* the singleton and the lock had nothing to guard.
`_acquire_singleton_lock` / `_lock_path` / `HEM_MCP_FORCE_KILL_PRIOR` and
`tests/test_mcp_singleton.py` no longer exist. See the docstring at the top of
`src/mcp_server.py`, and [`docs/OPENCLAW_BOUNDARY.md`](OPENCLAW_BOUNDARY.md) for the
full sanctioned surface.

## Context

`skills/home-energy-manager/SKILL.md` declared OpenClaw "read/propose/request only",
but nothing technically enforced that contract. OpenClaw could in principle edit
`.env`, run shell commands, or modify `src/` — the boundary was documentation, not
code. Adjacent to that, production saw **six parallel `python -m src.mcp_server`
processes** accumulating over a morning (08:00, 09:00, 10:00 spawn times) —
OpenClaw respawns the MCP over stdio and the prior subprocess never exits, so RAM
climbed past 3.3 GB with heavy swap (#60).

## Decision

Three independent controls, all in `src/mcp_server.py`:

1. **Boot-time tool-surface audit (`audit_mcp_tool_surface`, Phase 4.5).** On
   `build_mcp()` completion, every registered tool matching `_HARDWARE_WRITE_TOOL_PREFIXES`
   (`set_daikin_*`, `set_inverter_*`) is inspected for a `confirmed` parameter.
   Tools without it emit a WARN at startup that shows up in
   `journalctl -u home-energy-manager`. Clean surface = silent; regressions are
   loud and get fixed before they reach OpenClaw.

2. **`simulate_plan` dry-run (Phase 4.4).** Accepts a whitelist of override keys
   (`occupancy_mode`, `residents`, `extra_visitors`, `dhw_temp_normal_c`,
   `target_dhw_min_guests_c`), mutates `config` in-process, runs
   `run_lp_simulation(allow_daikin_refresh=False)`, and **always** restores the
   original config before returning — **zero hardware writes, zero Daikin quota
   cost**. Serialized against the live optimizer via
   `_optimizer_executor = ThreadPoolExecutor(max_workers=1)` so an agent can "what-if"
   without racing the real solve.

3. ~~**Singleton lock (PR #61, #60).**~~ **REMOVED 2026-04-25 — do not
   reintroduce.** `main()` used to acquire `fcntl.flock()` on `/run/hem-mcp.lock`,
   SIGTERM-ing the recorded PID on conflict, to stop OpenClaw's stdio respawns
   from piling up six `python -m src.mcp_server` processes. The whole failure
   mode disappeared when the production transport became **long-lived HTTP under
   `/mcp`** (bearer-guarded, mounted by `src/api/main.py`): there is exactly one
   MCP server because there is exactly one container. `./bin/mcp` still runs
   stdio for local dev, which is single-user by construction.

## Consequences

### Good
- ~~`pgrep -af 'src.mcp_server' | wc -l` stays ≤ 1 across OpenClaw restarts.~~
  (Now guaranteed structurally by the single container + HTTP transport.)
- Adding a new `set_*` tool without `confirmed=` produces a WARN at every boot
  until fixed — the boundary regression cost is "anyone tailing the journal
  sees it within seconds".
- `simulate_plan` gives OpenClaw a read-safe `what-if` path with no quota burn,
  unblocking "try a 2 °C warmer setpoint for the evening" requests.

### Watch points
- The audit is a **startup-only** check. Runtime dynamic tool registration would
  bypass it; we don't do that today, but any future "register tool on demand"
  feature needs to call `audit_mcp_tool_surface` again.
- The `/mcp` HTTP transport is guarded by `BearerAuthMiddleware`
  (`src/api/middleware.py`); the token lives at `/srv/hem/data/.openclaw-token`
  and the API lifespan generates it on first boot. That bearer, plus the loopback
  bind, is what replaces the old process-level singleton as the boundary.
- `simulate_plan` mutates and restores module-level `config`. Running two at
  once within the same process would race; the `max_workers=1` executor prevents
  that inside this repo, but external in-process callers should use the same
  serialization if they add new mutate-and-restore tools.

## Related files

- `src/mcp_server.py` — `audit_mcp_tool_surface`, `simulate_plan`, `main`.
  (`_acquire_singleton_lock` / `_lock_path` are gone; see Status.)
- `src/api/middleware.py` — `BearerAuthMiddleware` guards `/mcp`.
- `src/api/safeguards.py` — pending-action / confirmation / rate-limit /
  `audit_log` helpers that complement the `confirmed=` parameter. Note the
  `OPENCLAW_READ_ONLY` gate itself is **not** in this module: it is an inline
  `if config.OPENCLAW_READ_ONLY:` check on the write tools in `src/mcp_server.py`.
- `tests/test_mcp_boundary_selfcheck.py`, `tests/test_mcp_simulate_plan.py`.
- `docs/OPENCLAW_BOUNDARY.md` — sanctioned tool surface (keep in sync with any
  new tool).
