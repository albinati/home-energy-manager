# Epic: Phase 4 — Quota Hardening, User-Override Acceptance & OpenClaw MCP Boundary

Working branch: `feat/phase4-quota-openclaw-hardening`
Git preference: one commit per sub-issue on the branch (matches phase2/phase3).

---

## Preamble: Is OpenEMS a replacement?

Short answer: **No — not a drop-in.** OpenEMS is Java/OSGi backend + TypeScript/Angular UI; this codebase is Python/PuLP. It has no Daikin Onecta bundle, no Fox ESS H1/EP11 bundle, and is Modbus-oriented rather than cloud-API-oriented. Its scheduler paradigm is PLC-style fast control, not MPC/LP with half-hourly Agile horizons. A migration is a full rewrite in Java, plus writing two vendor bundles we'd own alone (no upstream community support for Onecta or Fox on OpenEMS).

What's worth borrowing from OpenEMS conceptually (and largely already in this repo via ADR-001):
- **Bridge / Service abstraction** separates HTTP transport from controller logic → matches `src/daikin/service.py` singleton.
- **Component sandboxing via OSGi bundles** → we achieve the same with the MCP tool surface, which is the proposal of Issue 4.5 below.
- **Long-term LAN/Modbus fallback** for vendor cloud independence → already on ADR-001 "Longer-term" roadmap ("Local Daikin polling (LAN)").

Conclusion: **stay on this stack**; this epic closes the remaining gaps identified after ADR-001 landed.

---

## Context

ADR-001 (`docs/ADR-001-api-quota-cache-layer.md`, 2026-04-19, implemented) cut Daikin calls from ~720/day to ~48/day via a persistent quota tracker and a TTL device cache with stale-fallback. After ADR-001, four residual gaps remain:

1. **Quota-accounting bypass.** `record_call("daikin", ...)` fires at the `src/daikin/service.py` wrapper layer. Two scheduler paths call `DaikinClient` directly and bypass both cache and accounting: `src/scheduler/daikin.py:78` (legacy LWT tick) and `src/scheduler/lp_initial_state.py:47`.
2. **`tank_power` / `tank_powerful` always write.** `src/daikin_bulletproof.py:41-42` short-circuits `daikin_device_matches_params()` to `False` whenever these fields appear in scheduled params, because the live values aren't parsed off the device snapshot. Every DHW dispatch slot pays 1–2 preventable PATCHes.
3. **No user-override acceptance loop.** If the user changes tank_temp via the Daikin Onecta app, the next heartbeat detects a mismatch and PATCHes it back to the LP-planned value — effectively fighting the user. This is the opposite of the paradigm shift in issue #30 (Daikin as passive load, LP concedes to native regulation except on rare exceptions).
4. **OpenClaw MCP boundary is documentation, not enforcement.** `skills/home-energy-manager/SKILL.md` declares OpenClaw "read/propose/request only" but nothing technically prevents OpenClaw from editing `.env`, running shell commands, or modifying `src/`. There is also no MCP wrapper exposing `run_lp_simulation()`, so OpenClaw cannot dry-run a hypothetical config change.

This epic closes those four gaps. It does **not** change the LP formulation (already audited linear), the `operation_mode` (stays `heating`), or the `setpointMode` (stays `weatherDependent`).

**Relationship to #30 (V8.2 paradigm shift):** Issue 4.3 of this epic is a prerequisite for #30 — before the LP can safely concede control to the user/Daikin native regulation, the system must first *recognise* user overrides and *stop fighting them*. If #30 lands before this epic, Issue 4.2 below may become obsolete (no DHW dispatch rows = no redundant PATCHes). Keep issues independent but coordinate execution order.

---

## Goal

After Phase 4 lands:
- [ ] 24 h Daikin call count stays ≤ 80 in steady-state (headroom: 180 budget, 200 hard limit).
- [ ] No code path reaches the Daikin Onecta transport layer without `record_call` firing.
- [ ] User mobile-app overrides persist through at least one full MPC cycle without being snapped back.
- [ ] OpenClaw can dry-run plans via MCP (`simulate_plan`) with zero hardware-write risk and zero Daikin quota cost.
- [ ] OpenClaw has no filesystem, shell, `.env`, or source-code write capability on this host.

## Non-goals

- Changing the LP formulation.
- Seasonal `heating`↔`cooling` automation.
- Moving off Onecta to local Modbus (tracked in ADR-001 Longer-term).
- Implementing #30 (V8.2 paradigm shift) — separate epic; this one enables it.

---

## Sub-issues (in suggested execution order)

Emoji markers: 🔴 blocker · 🟡 core · 🟢 polish

### 🟡 Issue 4.1 — Close the quota-accounting leak at the transport layer

**Context**
ADR-001 routes quota accounting through `src/daikin/service.py` wrappers (`record_call("daikin", kind, ok)`). Two scheduler paths bypass this wrapper and call `DaikinClient` directly, so their HTTP traffic is invisible to both the budget tracker and the TTL cache:
- `src/scheduler/daikin.py:78` — `client.get_devices()` in `run_daikin_scheduler_tick` (legacy half-hourly LWT tick).
- `src/scheduler/lp_initial_state.py:47` — `daikin.get_devices()` during LP state seeding on every MPC replan.

Effect: up to 2 calls × 48 slots + 6 replans = ~100 silent calls/day unaccounted for. Combined with the ~48/day counted calls, we drift close to the 180 budget without anyone noticing until the 200 hard limit is hit.

**Solution**
Move `record_call("daikin", kind, ok)` inside `DaikinClient._get` (kind=`"read"`) and `DaikinClient._patch` (kind=`"write"`) in `src/daikin/client.py:48-101`. Fire on success AND on `DaikinError`/HTTP errors — failed calls still count against Daikin's budget. Then remove the now-redundant `record_call` from the service-layer wrappers (audit every site found by `grep -n record_call src/daikin/service.py`). Keep `should_block` checks at the service layer — we still want to gate *whether* to dial out, just count *all* outbound traffic uniformly.

Then reroute the two bypass callers through the cached service:
- `src/scheduler/daikin.py:78` → `get_cached_devices(allow_refresh=True, max_age_seconds=DAIKIN_LEGACY_TICK_CACHE_MAX_AGE_SECONDS, actor="legacy_lwt_tick")`.
- `src/scheduler/lp_initial_state.py:47` → `get_cached_devices(allow_refresh=True, max_age_seconds=DAIKIN_LP_INIT_CACHE_MAX_AGE_SECONDS, actor="lp_init")`.

New config keys (in `src/config.py`, no hardcoding): `DAIKIN_LP_INIT_CACHE_MAX_AGE_SECONDS=600`, `DAIKIN_LEGACY_TICK_CACHE_MAX_AGE_SECONDS=1200`.

**Files:** `src/daikin/client.py`, `src/daikin/service.py`, `src/scheduler/daikin.py`, `src/scheduler/lp_initial_state.py`, `src/config.py`.

**Scope**
Minor refactor — no behaviour change other than accounting and caching. Audit grep for every `DaikinClient` instantiation in the repo to ensure no new bypass is introduced.

**Acceptance**
- [ ] `grep -rn "DaikinClient()" src/` finds only `src/daikin/service.py` (one call site) plus tests.
- [ ] `sqlite3 data/energy_state.db "SELECT count(*) FROM api_call_log WHERE vendor='daikin'"` increments by exactly 1 per HTTP call (verified via mocked urllib test).
- [ ] 24 h observation in operational mode: `daikin.quota_used_24h` in `/api/v1/daikin/quota` matches actual HTTP traffic logged in journal within ±2 calls.
- [ ] Unit tests: `tests/test_daikin_client_quota.py` covers success, 429, 401-retry, and network-error paths all calling `record_call` exactly once.

---

### 🟡 Issue 4.2 — Parse `tank_power` / `tank_powerful` to enable dedup

**Context**
`src/daikin_bulletproof.py:41-42`:
```python
if "tank_power" in params or "tank_powerful" in params:
    return False
```
The short-circuit exists because `DaikinDevice` doesn't currently expose live values for these fields, so we can't compare. As a result, every DHW dispatch slot sends 1–2 PATCHes that may already match live state. Combined with 4–6 DHW actions/day, that's up to 12 preventable writes. Low priority individually; meaningful in aggregate.

**Solution**
Extend `_parse_device()` in `src/daikin/client.py:114-200` to read the DHW management point's `onOffMode.value` into `device.tank_on: bool | None` and `powerfulMode.value` into `device.tank_powerful: bool | None`. Add the fields to `DaikinDevice` in `src/daikin/models.py`.

Update `daikin_device_matches_params()` in `src/daikin_bulletproof.py:22-43`:
- Replace the blanket short-circuit.
- When params contain `tank_power` and `device.tank_on is not None`: compare; no mismatch → no write. If `device.tank_on is None`, fall back to writing (conservative).
- Same pattern for `tank_powerful`.

**Files:** `src/daikin/models.py`, `src/daikin/client.py`, `src/daikin_bulletproof.py`.

**Scope**
Minor — parsing + comparison. No schema migration (fields are in-memory on the device model). Quota reduction of roughly `2 × n_dhw_dispatch_slots` PATCHes/day.

**Acceptance**
- [ ] Device snapshot fetched via `get_cached_devices()` now includes `tank_on` and `tank_powerful` fields populated from Onecta.
- [ ] Unit test: fixture device `tank_on=True`; params `{"tank_power": True}` → `daikin_device_matches_params()` returns `True` (no write).
- [ ] Unit test: fixture device `tank_on=None` (unknown); params `{"tank_power": True}` → returns `False` (safe fallback, write).
- [ ] Integration: over 6 consecutive heartbeat ticks at a DHW hold window, exactly 1 `set_tank_power` PATCH occurs (at boundary), not 6.

---

### 🔴 Issue 4.3 — Mobile-app override acceptance loop

**Context**
When the user changes tank temperature via the Daikin Onecta mobile app — or any other out-of-band path — the next heartbeat tick reads the live state, compares against the scheduled action_schedule row, detects the mismatch via `daikin_device_matches_params()`, and PATCHes the value back. This is hostile UX: the user's explicit input is reversed within 2 minutes.

This also blocks the direction of travel in #30 (V8.2 paradigm shift): if we want the LP to concede control to Daikin native regulation most of the time, we first need the system to *recognize* user overrides and *stop fighting them*.

**Solution**
Introduce a "user override" state for `action_schedule` rows. Detection rule, checked at the heartbeat before `apply_scheduled_daikin_params`:
1. If the row has been in `active` state for ≥ `DAIKIN_OVERRIDE_GRACE_SECONDS` (default 600 s = 10 min, env-tunable), AND
2. The live value differs from the scheduled param by more than tolerance (same tolerances as `daikin_device_matches_params`), AND
3. No hardware apply has happened in the current slot (i.e. we didn't just write and see our own echo-lag):

Then mark the row `overridden_by_user_at = now()`, log to `execution_log` with `source="user_override"`, and skip the re-apply. The next MPC replan (on its normal checkpoint cadence) will read the current live state as initial condition via `read_lp_initial_state()` and plan on top of the new reality.

Notify the user once per override via `src/notifier.py` → OpenClaw Gateway: *"User override detected: tank_temp 45→55 °C at 20:14 — schedule will re-converge at 23:00 replan."*

**Schema change:** add `overridden_by_user_at REAL NULL` to `action_schedule` in `src/db.py`. Include a forward-compatible migration.

**Files:**
- `src/db.py` (schema + migration + selector that filters out overridden rows)
- `src/daikin_bulletproof.py` (detection before apply)
- `src/scheduler/runner.py` (heartbeat integration)
- `src/notifier.py` (override notification)
- `src/config.py` (`DAIKIN_OVERRIDE_GRACE_SECONDS`, `DAIKIN_OVERRIDE_TOLERANCE_TANK_C`, `DAIKIN_OVERRIDE_TOLERANCE_LWT_C`)

**Scope**
Core — touches DB schema, heartbeat path, and notification surface. Must be covered by integration tests against a mocked Daikin client.

**Acceptance**
- [ ] Schema migration adds `overridden_by_user_at` column; rollback safe.
- [ ] Integration test simulating a user change between heartbeat ticks marks the row as overridden on the next tick and issues zero PATCHes.
- [ ] Override event appears in `execution_log` with `source="user_override"`.
- [ ] One notification per override (not repeated each tick).
- [ ] Next MPC replan uses the live value as initial condition — verified by inspecting `read_lp_initial_state()` output post-override.
- [ ] Unit test: override detected only AFTER `DAIKIN_OVERRIDE_GRACE_SECONDS` elapses (prevents false positive on cloud-lag echo immediately after our own write).

---

### 🟡 Issue 4.4 — Expose `simulate_plan` as a first-class MCP tool

**Context**
`src/scheduler/lp_simulation.py:run_lp_simulation()` already exists and performs a full read-only LP solve with no DB/Fox/Daikin writes. But no MCP tool wraps it. OpenClaw's only plan-level tool is `propose_optimization_plan()`, which writes to `optimization_plans`, may auto-apply if `PLAN_AUTO_APPROVE=true`, and consumes Daikin quota (it re-reads device state as initial condition).

This means OpenClaw has no safe way to answer *"what would the plan look like if residents=4 and extra_visitors=2 for tomorrow's guest dinner?"* without risking a real hardware change.

**Solution**
Add MCP tool `simulate_plan(overrides: dict | None = None) -> dict` in `src/mcp_server.py`:
- Wraps `run_lp_simulation()`.
- `overrides` is a restricted whitelist — keys limited to: `occupancy_mode`, `residents`, `extra_visitors`, `dhw_temp_normal_c`, `target_dhw_min_guests_c`, `optimization_preset`. Anything outside the whitelist returns `{"ok": False, "error": "unsupported override key"}`.
- Simulation reads cached device state (no `allow_refresh`); explicitly does not call `record_call`.
- Returns a dict: `{"ok": bool, "plan_date": str, "total_pence": float, "slot_count": int, "dhw_schedule": [...], "fox_groups": [...], "soc_trajectory": [...], "objective_pence": float, "status": str}`.

Ensure `run_lp_simulation()` accepts the override dict (extend signature; default `None` keeps existing call sites working).

**Files:** `src/mcp_server.py`, `src/scheduler/lp_simulation.py`.

**Scope**
Minor — new MCP tool; existing simulation function extended additively. No schema change.

**Acceptance**
- [ ] MCP tool `simulate_plan` returns a plan dict given valid overrides.
- [ ] MCP tool rejects non-whitelisted override keys.
- [ ] `api_call_log` is not mutated by a `simulate_plan` call (verified by row count before/after).
- [ ] `action_schedule`, `optimization_plans`, and Fox V3 on the device are all unchanged after a `simulate_plan` call (verified by SQL + Fox read-back).
- [ ] Unit test covering: valid overrides, invalid key rejection, no-write invariant.

---

### 🟢 Issue 4.5 — OpenClaw MCP-only boundary enforcement

**Context**
`skills/home-energy-manager/SKILL.md` declares OpenClaw "read/propose/request only" and hardware writes are gated by `OPENCLAW_READ_ONLY` in `.env`. But OpenClaw has full shell and filesystem access on this host — it could edit `.env`, modify `src/`, or bypass the MCP surface entirely. That makes the documented boundary unenforced.

This matters because: (a) hardware-write authorization is meaningless if the tool-to-write can also edit the gate; (b) any future MCP surface reduction assumes the current surface is the only surface.

**Solution**
Three parts:

1. **Document the sanctioned surface** at `docs/OPENCLAW_BOUNDARY.md`: complete list of MCP tools exposed, plus explicit statement that `.env`, `src/`, shell, and git are all out-of-bounds. This is the reference any future change must cite.

2. **Self-check at MCP server boot** (`src/mcp_server.py`): on startup, log a warning if `OPENCLAW_READ_ONLY=true` is set AND any write-capable MCP tool is exposed without a simulation-mode guard or `confirmed=True` parameter. This catches regressions where a write tool is added without honouring the read-only flag.

3. **Coordinate OpenClaw config** (outside this repo): separate `OPENCLAW_READ_ONLY` (hardware-write gate, already exists) from a new expectation `OPENCLAW_FS_ACCESS=false` (filesystem gate, enforced in OpenClaw's own config). The latter is a note for the reviewer — we cannot change OpenClaw's config from this repo, but we can document the required setting.

**Files:** `docs/OPENCLAW_BOUNDARY.md` (new), `src/mcp_server.py` (self-check), `skills/home-energy-manager/SKILL.md` (tighten wording to reference boundary doc), coordination note for OpenClaw config.

**Scope**
Polish — mostly documentation plus a small boot-time check. No runtime behaviour changes unless the self-check trips on a pre-existing misconfiguration.

**Acceptance**
- [ ] `docs/OPENCLAW_BOUNDARY.md` exists and enumerates every MCP tool with its hardware-write implication.
- [ ] `SKILL.md` references the boundary doc.
- [ ] Boot-time self-check logs a WARN if a write-capable MCP tool is exposed without a simulation guard. Verified by forcibly breaking the invariant in a test and asserting the warning.
- [ ] Reviewer confirms the OpenClaw config note is actioned on the OpenClaw side before closing the issue.

---

## Verification for the epic as a whole

Daily probe in the journal (add to heartbeat output once a day):

```
daikin.calls_24h = N              # target < 80, warn > 120, hard-block @ 200
daikin.writes_skipped_dedup = X
daikin.cache_hits_24h = Y
daikin.user_overrides_24h = Z
```

Acceptance check one week after deploy: `journalctl -u home-energy-manager --since "7 days ago" | grep 'daikin.calls_24h' | awk '{print $NF}' | sort -n | tail -5` shows all values ≤ 80.

---

## Execution notes

- Branch: `feat/phase4-quota-openclaw-hardening`
- One PR per sub-issue, OR one epic PR with one commit per issue — confirm with the user before opening.
- PR body must list `Closes #<issue-number>` so GitHub auto-closes on merge (see `docs/phase2-epic-tasks.md`).
- Existing Phase 3 patterns: see `docs/phase3-epic-tasks.md` for formatting and the test-naming convention (`tests/test_<area>_<thing>.py`).
