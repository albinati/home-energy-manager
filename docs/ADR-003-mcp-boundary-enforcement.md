# ADR-003 — MCP boundary enforcement & singleton

**Date:** 2026-04-22 (Phase 4.5 landed 2026-04-21 as part of #45; singleton added by PR #61).
**Status:** Implemented. See also [`docs/OPENCLAW_BOUNDARY.md`](OPENCLAW_BOUNDARY.md) for the
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

3. **Singleton lock (PR #61, #60).** `main()` now:
   - Acquires `fcntl.flock()` on `/run/hem-mcp.lock` (prod-root writable; `/tmp`
     fallback). On conflict: SIGTERM the recorded PID, wait up to 2 s, retry once.
     Unkillable prior PID → `sys.exit(0)` instead of raising (OpenClaw would
     otherwise respawn in a tight loop).
   - Installs `SIGTERM` / `SIGHUP` handlers plus `atexit` so the lock is always
     released.
   - Wraps the FastMCP stdio loop in `try/finally` so EOF on stdin exits cleanly.

## Consequences

### Good
- `pgrep -af 'src.mcp_server' | wc -l` stays ≤ 1 across OpenClaw restarts.
- Adding a new `set_*` tool without `confirmed=` produces a WARN at every boot
  until fixed — the boundary regression cost is "anyone tailing the journal
  sees it within seconds".
- `simulate_plan` gives OpenClaw a read-safe `what-if` path with no quota burn,
  unblocking "try a 2 °C warmer setpoint for the evening" requests.

### Watch points
- The audit is a **startup-only** check. Runtime dynamic tool registration would
  bypass it; we don't do that today, but any future "register tool on demand"
  feature needs to call `audit_mcp_tool_surface` again.
- If `/run` is on tmpfs (systemd default), the lockfile survives reboot in name
  only — that's fine, `fcntl.flock` is the authoritative guard, the PID content is
  advisory. Clock skew between the lock holder and the challenger is irrelevant.
- `simulate_plan` mutates and restores module-level `config`. Running two at
  once within the same process would race; the `max_workers=1` executor prevents
  that inside this repo, but external in-process callers should use the same
  serialization if they add new mutate-and-restore tools.

## Related files

- `src/mcp_server.py` — `audit_mcp_tool_surface`, `simulate_plan`, `main`,
  `_acquire_singleton_lock`, `_lock_path`.
- `src/api/safeguards.py` — the `OPENCLAW_READ_ONLY` gate complements the
  `confirmed=` parameter; both must be on for hardware writes.
- `tests/test_mcp_boundary_selfcheck.py`, `tests/test_mcp_simulate_plan.py`,
  `tests/test_mcp_singleton.py`.
- `docs/OPENCLAW_BOUNDARY.md` — sanctioned tool surface (keep in sync with any
  new tool).
